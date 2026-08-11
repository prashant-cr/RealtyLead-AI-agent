"""CSV listing import.

Agents arrive with a spreadsheet exported from a portal or typed by hand, so the
parser is forgiving about shape and strict about facts: header names are matched
case-insensitively with common aliases, prices accept "85,00,000" / "85 lakh" /
"1.2 cr", and every row that cannot be read is reported with its line number
rather than silently dropped.

Nothing is written unless the whole file parses, so a partially-imported
inventory can never make the assistant quote a half-loaded catalogue.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from app.models import Listing
from app.models.enums import ListingStatus, PropertyType

MAX_ROWS = 2000
MAX_BYTES = 2 * 1024 * 1024

# Header aliases, lowercased and stripped of non-alphanumerics.
FIELD_ALIASES: dict[str, str] = {
    "title": "title",
    "name": "title",
    "property": "title",
    "propertyname": "title",
    "type": "property_type",
    "propertytype": "property_type",
    "city": "city",
    "locality": "locality",
    "area": "locality",
    "location": "locality",
    "state": "state",
    "price": "price",
    "priceinr": "price",
    "amount": "price",
    "cost": "price",
    "bhk": "bhk",
    "bedrooms": "bhk",
    "beds": "bhk",
    "carpetarea": "carpet_area_sqft",
    "carpetareasqft": "carpet_area_sqft",
    "sqft": "carpet_area_sqft",
    "area_sqft": "carpet_area_sqft",
    "description": "description",
    "notes": "description",
    "rera": "rera_id",
    "reraid": "rera_id",
    "rerano": "rera_id",
    "status": "status",
}

REQUIRED = ("title", "city", "price")

PROPERTY_TYPE_ALIASES = {
    "flat": PropertyType.FLAT,
    "apartment": PropertyType.FLAT,
    "builderfloor": PropertyType.FLAT,
    "villa": PropertyType.VILLA,
    "house": PropertyType.VILLA,
    "bungalow": PropertyType.VILLA,
    "plot": PropertyType.PLOT,
    "land": PropertyType.PLOT,
    "commercial": PropertyType.COMMERCIAL,
    "office": PropertyType.COMMERCIAL,
    "shop": PropertyType.COMMERCIAL,
}

_NON_ALNUM = re.compile(r"[^a-z0-9]")
_CRORE = re.compile(r"([\d.,]+)\s*(?:cr|crore)s?\b", re.IGNORECASE)
_LAKH = re.compile(r"([\d.,]+)\s*(?:l|lac|lakh|lakhs)\b", re.IGNORECASE)


@dataclass
class RowError:
    line: int
    message: str


@dataclass
class ImportResult:
    listings: list[Listing] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    skipped_blank: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


def _normalise_header(value: str) -> str:
    return _NON_ALNUM.sub("", value.strip().lower())


def parse_price(raw: str) -> Decimal:
    """Accepts 8500000, "85,00,000", "85 lakh", "1.2 cr", "₹85L"."""
    text = raw.strip().replace("₹", "").replace("rs.", "").replace("rs", "")
    if not text:
        raise ValueError("price is required")

    if match := _CRORE.search(text):
        return (Decimal(match.group(1).replace(",", "")) * Decimal(10_000_000)).quantize(
            Decimal("0.01")
        )
    if match := _LAKH.search(text):
        return (Decimal(match.group(1).replace(",", "")) * Decimal(100_000)).quantize(
            Decimal("0.01")
        )

    try:
        amount = Decimal(text.replace(",", "").replace(" ", ""))
    except InvalidOperation as exc:
        raise ValueError(f"{raw!r} is not a price") from exc
    if amount <= 0:
        raise ValueError("price must be greater than zero")
    return amount.quantize(Decimal("0.01"))


def parse_property_type(raw: str) -> PropertyType:
    key = _NON_ALNUM.sub("", raw.lower())
    if not key:
        return PropertyType.FLAT
    if key in PROPERTY_TYPE_ALIASES:
        return PROPERTY_TYPE_ALIASES[key]
    raise ValueError(
        f"{raw!r} is not a property type (use {', '.join(sorted({t.value for t in PropertyType}))})"
    )


def parse_int(raw: str, label: str) -> int | None:
    text = raw.strip()
    if not text:
        return None
    # "3 BHK" and "1,450 sqft" both appear in real exports.
    digits = re.sub(r"[^\d]", "", text)
    if not digits:
        raise ValueError(f"{raw!r} is not a valid {label}")
    return int(digits)


def parse_status(raw: str) -> ListingStatus:
    key = _NON_ALNUM.sub("", raw.lower())
    if not key:
        return ListingStatus.AVAILABLE
    for status in ListingStatus:
        if _NON_ALNUM.sub("", status.value) == key:
            return status
    raise ValueError(f"{raw!r} is not a listing status")


def parse_csv(content: bytes, agent_id: object) -> ImportResult:
    """Parse an uploaded CSV into unsaved Listing rows."""
    result = ImportResult()

    if len(content) > MAX_BYTES:
        result.errors.append(RowError(0, f"File is larger than {MAX_BYTES // 1024 // 1024} MB."))
        return result

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("latin-1")
        except UnicodeDecodeError:
            result.errors.append(RowError(0, "Could not read the file as text."))
            return result

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        result.errors.append(RowError(0, "The file is empty."))
        return result

    columns = [FIELD_ALIASES.get(_normalise_header(h), "") for h in header]
    missing = [name for name in REQUIRED if name not in columns]
    if missing:
        result.errors.append(
            RowError(
                1,
                f"Missing required column(s): {', '.join(missing)}. "
                f"Found: {', '.join(h.strip() for h in header if h.strip()) or 'nothing'}.",
            )
        )
        return result

    for line, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            result.skipped_blank += 1
            continue
        if len(result.listings) >= MAX_ROWS:
            result.errors.append(RowError(line, f"More than {MAX_ROWS} rows; split the file."))
            break

        values = {
            name: (row[index].strip() if index < len(row) else "")
            for index, name in enumerate(columns)
            if name
        }

        try:
            listing = Listing(
                agent_id=agent_id,
                title=_require(values.get("title", ""), "title"),
                city=_require(values.get("city", ""), "city"),
                locality=values.get("locality") or None,
                state=values.get("state") or None,
                price=parse_price(values.get("price", "")),
                property_type=parse_property_type(values.get("property_type", "")),
                status=parse_status(values.get("status", "")),
                bhk=parse_int(values.get("bhk", ""), "BHK"),
                carpet_area_sqft=parse_int(values.get("carpet_area_sqft", ""), "carpet area"),
                description=values.get("description") or None,
                rera_id=values.get("rera_id") or None,
                media_urls=[],
            )
        except ValueError as exc:
            result.errors.append(RowError(line, str(exc)))
            continue

        result.listings.append(listing)

    if not result.listings and not result.errors:
        result.errors.append(RowError(1, "No listings found in the file."))

    return result


def _require(value: str, label: str) -> str:
    if not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


SAMPLE_CSV = "\n".join(
    [
        "title,property_type,city,locality,price,bhk,carpet_area_sqft,rera_id,description",
        "3 BHK in Bopal,flat,Ahmedabad,Bopal,85 lakh,3,1450,"
        "PR/GJ/AHMEDABAD/AUDA/RAA12345/010124,East-facing with covered parking",
        "2 BHK near SG Highway,flat,Ahmedabad,SG Highway,6200000,2,1080,,Close to the metro",
        "Villa in Shela,villa,Ahmedabad,Shela,2.15 cr,4,3100,,Corner plot with garden",
        "",
    ]
)
