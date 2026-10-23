"""Choosing the config entry for the actions that act on one Navidrome account. No HA imports."""
from __future__ import annotations


class TargetError(Exception):
    """No unambiguous config entry; the argument is the translation key of the error."""


def choose_entry(
    candidates: dict[str, set[str]],
    explicit: str | None = None,
    owner: str | None = None,
    tonie_id: str | None = None,
) -> str:
    """Pick the config entry for an action.

    candidates: loaded entry_id → Tonie IDs this entry knows (for the lookup by Tonie).
    explicit: config_entry_id given in the call. owner: entry of a tonie_sync entity in the call.
    Several entries without a clear choice raise TargetError instead of silently taking the first.
    """
    if not candidates:
        raise TargetError("not_set_up")
    if explicit is not None:
        if explicit not in candidates:
            raise TargetError("invalid_entry")
        if owner is not None and owner != explicit:
            raise TargetError("entry_mismatch")
        return explicit
    if owner is not None:
        if owner not in candidates:
            raise TargetError("invalid_entry")
        return owner
    if len(candidates) == 1:
        return next(iter(candidates))
    if tonie_id is not None:
        matches = [eid for eid, tonies in candidates.items() if tonie_id in tonies]
        if len(matches) == 1:
            return matches[0]
    raise TargetError("multiple_entries")
