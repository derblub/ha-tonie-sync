"""Migration of the stored data."""
import copy

from const import STATES
from migrations import migrate_store

V1 = {
    "tonies": {
        "a": {"mapping": {}, "excluded": [], "last": {"state": "aktuell", "last_message": "x", "playlist_id": "p"}},
        "b": {"mapping": {}, "excluded": [], "last": {"state": "zu_lang"}},
        "c": {"mapping": {}, "excluded": [], "last": {"state": "bestaetigen"}},
        "d": {"mapping": {}, "excluded": [], "last": {"state": "fehler"}},
        "e": {"mapping": {}, "excluded": [], "last": {"state": "laeuft"}},
        "f": {"mapping": {}, "excluded": [], "last": {"state": "ausstehend"}},
        "g": {"mapping": {}, "excluded": []},
        "h": {"mapping": {}, "excluded": [], "last": {}},
    }
}


def test_v1_german_states_are_migrated():
    data = migrate_store(1, copy.deepcopy(V1))
    states = {tid: (ts.get("last") or {}).get("state") for tid, ts in data["tonies"].items()}
    assert states == {
        "a": "up_to_date", "b": "too_long", "c": "needs_confirmation", "d": "error", "e": "running",
        "f": "pending", "g": None, "h": None,
    }
    assert all(s in STATES for s in states.values() if s)
    assert data["tonies"]["a"]["last"] == {"state": "up_to_date", "last_message": "x", "playlist_id": "p"}


def test_current_version_and_garbage_untouched():
    v2 = {"tonies": {"a": {"last": {"state": "up_to_date"}}}}
    assert migrate_store(2, copy.deepcopy(v2)) == v2
    assert migrate_store(1, None) is None
    assert migrate_store(1, {"tonies": {"a": None}}) == {"tonies": {"a": None}}
