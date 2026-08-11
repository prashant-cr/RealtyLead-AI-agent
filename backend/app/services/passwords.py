"""Password hashing.

Uses `hashlib.scrypt` from the standard library rather than adding bcrypt or
argon2. scrypt is a memory-hard KDF designed for exactly this, it is in CPython's
stdlib (OpenSSL-backed), and it keeps the dependency list honest — CLAUDE.md asks
us to think before adding weight.

This is deliberately *not* the fast SHA-256 used for API tokens: those are
high-entropy random strings with no dictionary to attack, whereas passwords are
human-chosen and need the work factor.

Stored format: `scrypt$n$r$p$<salt-hex>$<hash-hex>`. Parameters travel with the
hash so they can be raised later without invalidating existing passwords.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

ALGORITHM = "scrypt"
# n=2^15 costs roughly 100ms and 32 MB per hash here. 2^16 was measured at 210ms
# and 64 MB, which is a real denial-of-service surface on an unauthenticated login
# endpoint — concurrent attempts multiply that memory. The parameters travel with
# each hash, so this can be raised later without invalidating stored passwords.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32
MAX_MEMORY = 128 * SCRYPT_N * SCRYPT_R * 2

MIN_PASSWORD_LENGTH = 10


class WeakPasswordError(ValueError):
    """The password does not meet the minimum policy."""


def validate(password: str) -> None:
    """Length is the only rule that reliably helps; complexity rules mostly don't."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > 1024:
        # Bound the work an unauthenticated caller can make us do.
        raise WeakPasswordError("Password must be at most 1024 characters.")


def hash_password(password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> str:
    validate(password)
    salt = secrets.token_bytes(SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, dklen=KEY_BYTES, maxmem=MAX_MEMORY
    )
    return f"{ALGORITHM}${n}${r}${p}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check. A missing or malformed hash is a failure, not an error."""
    if not stored:
        return False
    try:
        algorithm, n_raw, r_raw, p_raw, salt_hex, hash_hex = stored.split("$")
        if algorithm != ALGORITHM:
            return False
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False

    candidate = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=len(expected),
        maxmem=128 * n * r * 2,
    )
    return hmac.compare_digest(candidate, expected)


def needs_rehash(stored: str | None) -> bool:
    """True when a stored hash uses weaker parameters than we now require."""
    if not stored:
        return True
    try:
        algorithm, n_raw, r_raw, p_raw, _, _ = stored.split("$")
    except ValueError:
        return True
    return (
        algorithm != ALGORITHM
        or int(n_raw) < SCRYPT_N
        or int(r_raw) < SCRYPT_R
        or int(p_raw) < SCRYPT_P
    )
