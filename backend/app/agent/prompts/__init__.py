"""Versioned prompt loading.

Prompts live as ``<name>_v<n>.md`` files in this directory and are never inlined
in code — that way a prompt change is a reviewable diff with its own history.
Templates use ``$placeholder`` (``string.Template``) rather than ``str.format``
so prompt text can contain braces freely.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Any

PROMPT_DIR = Path(__file__).parent
_VERSION_RE = re.compile(r"_v(\d+)\.md$")


class PromptNotFoundError(LookupError):
    pass


@lru_cache
def latest_version(name: str) -> int:
    versions = [
        int(m.group(1))
        for path in PROMPT_DIR.glob(f"{name}_v*.md")
        if (m := _VERSION_RE.search(path.name))
    ]
    if not versions:
        raise PromptNotFoundError(f"no prompt files found for {name!r} in {PROMPT_DIR}")
    return max(versions)


@lru_cache
def load(name: str, version: int | None = None) -> str:
    """Return the raw template text for a prompt."""
    resolved = latest_version(name) if version is None else version
    path = PROMPT_DIR / f"{name}_v{resolved}.md"
    if not path.is_file():
        raise PromptNotFoundError(f"prompt {name!r} version {resolved} not found at {path}")
    return path.read_text(encoding="utf-8")


def render(name: str, version: int | None = None, **values: Any) -> str:
    """Render a prompt, failing loudly on a missing placeholder."""
    return Template(load(name, version)).substitute(values)


__all__ = ["PromptNotFoundError", "latest_version", "load", "render"]
