"""Anonymising diagnostics data. No HA imports, so it is testable."""
from __future__ import annotations

from typing import Any

REDACTED = "**REDACTED**"


def redact_keys(obj: Any, keys: set[str]) -> Any:
    """Copy of obj with the values of the given keys replaced, at any depth."""
    if isinstance(obj, dict):
        return {k: (REDACTED if k in keys else redact_keys(v, keys)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_keys(v, keys) for v in obj]
    return obj


def replace_ids(obj: Any, aliases: dict[str, str]) -> Any:
    """Copy of obj with every occurrence of the IDs (in keys and string values) replaced by its alias."""
    ordered = sorted(aliases.items(), key=lambda kv: len(kv[0]), reverse=True)

    def sub(text: str) -> str:
        for real, alias in ordered:
            if real:
                text = text.replace(real, alias)
        return text

    def walk(o: Any) -> Any:
        if isinstance(o, dict):
            return {(sub(k) if isinstance(k, str) else k): walk(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [walk(v) for v in o]
        if isinstance(o, str):
            return sub(o)
        return o

    return walk(obj)
