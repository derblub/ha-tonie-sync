"""Sync flow with fakes for Navidrome and the Tonie cloud."""
import asyncio
import copy

import pytest

from engine import NO_CHANGE, SyncAbort, run_sync
from const import STATE_CONFIRM, STATE_CURRENT, STATE_ERROR, STATE_TOO_LONG


def ch(cid, seconds=100.0, title=None):
    return {"id": cid, "file": cid, "title": title or cid, "seconds": seconds, "transcoding": False}


class FakeSource:
    def __init__(self, ids, dur=100, fail_download=None):
        self.entries = [{"id": i, "title": f"T{i}", "artist": f"A{i}", "duration": dur} for i in ids]
        self.fail_download = fail_download

    async def get_playlist(self, pid):
        return {"id": pid, "name": "P", "entry": list(self.entries)}

    async def download_song(self, sid, suffix="mp3"):
        if sid == self.fail_download:
            raise RuntimeError("gone")
        return b"mp3" + sid.encode(), "mp3"


class FakeTonie:
    def __init__(self, chapters, **extra):
        self.chapters = chapters
        self.extra = extra
        self.patches = []
        self.uploads = []
        self.on_get = []  # hooks per get() call (index = call number)
        self.gets = 0
        self.patch_error = None
        self.patch_applies = True

    async def get(self):
        if self.gets < len(self.on_get) and self.on_get[self.gets]:
            self.on_get[self.gets](self)
        self.gets += 1
        if self.chapters is None:
            return {**self.extra}
        return {"chapters": copy.deepcopy(self.chapters), **self.extra}

    async def upload(self, data, filename):
        fid = f"F-{filename.split('.')[0]}"
        self.uploads.append(fid)
        return fid

    async def patch(self, chapters):
        self.patches.append(copy.deepcopy(chapters))
        if self.patch_applies:
            self.chapters = copy.deepcopy(chapters)
        if self.patch_error:
            raise self.patch_error
        return {"chapters": copy.deepcopy(self.chapters)}


def run(source, tonie, ts=None, **kw):
    ts = {"mapping": {}, "excluded": []} if ts is None else ts
    saves = []

    async def persist():
        saves.append(copy.deepcopy(ts))

    async def go():
        return await run_sync(
            source=source, tonie=tonie, ts=ts, persist=persist, report=lambda *a, **k: None,
            now=lambda: "now", playlist_id="p", **kw,
        )

    return asyncio.run(go()), ts, saves


def ids(chs):
    return [c["id"] for c in chs]


# ── Review finding 1: broken chapter list ────────────────────────────────────

@pytest.mark.parametrize("chapters", [None, {}, "x"])
def test_missing_or_invalid_chapters_abort(chapters):
    tonie = FakeTonie(chapters)
    with pytest.raises(SyncAbort):
        run(FakeSource(["s1"]), tonie, {"mapping": {"s1": {"chapter_id": "c1", "file_id": "c1"}}, "excluded": []})
    assert tonie.patches == [] and tonie.uploads == []


def test_chapters_present_mismatch_aborts():
    tonie = FakeTonie([ch("f1")], chaptersPresent=3)
    with pytest.raises(SyncAbort, match="Inconsistent chapter count"):
        run(FakeSource(["s1"]), tonie)
    assert tonie.patches == []


# ── Normal case ──────────────────────────────────────────────────────────────

def test_first_sync_then_unchanged():
    tonie = FakeTonie([ch("f1")])
    out, ts, _ = run(FakeSource(["s1", "s2"]), tonie)
    assert out.state == STATE_CURRENT and out.uploaded == 2
    assert ids(tonie.chapters) == ["f1", "F-s1", "F-s2"]
    assert ts["pending"] is None and set(ts["mapping"]) == {"s1", "s2"}
    # second run: nothing to do
    tonie.chapters[1]["seconds"] = 100
    tonie.chapters[2]["seconds"] = 100
    out2, _, _ = run(FakeSource(["s1", "s2"]), tonie, ts)
    assert out2.message.startswith(NO_CHANGE) and len(tonie.patches) == 1


# ── Finding 2: changes between the first read and the PATCH are kept ─────────

def test_fresh_read_keeps_interim_changes():
    tonie = FakeTonie([ch("f1", title="old")])

    def rename(t):
        t.chapters[0]["title"] = "new"
        t.chapters.append(ch("f2", 50))

    tonie.on_get = [None, rename]
    out, _, _ = run(FakeSource(["s1"]), tonie)
    assert out.state == STATE_CURRENT
    sent = tonie.patches[0]
    assert sent[0]["title"] == "new" and ids(sent) == ["f1", "f2", "F-s1"]


def test_fresh_read_detects_managed_change():
    ts = {"mapping": {"s1": {"chapter_id": "c1", "file_id": "c1"}}, "excluded": []}
    tonie = FakeTonie([ch("c1")])

    def delete_managed(t):
        t.chapters = []

    tonie.on_get = [None, delete_managed]
    # deleted in the app in the meantime → excluded, not overwritten
    out, ts2, _ = run(FakeSource(["s1", "s2"]), tonie, copy.deepcopy(ts))
    assert ids(tonie.patches[0]) == ["F-s2"] and ts2["excluded"] == ["s1"]
    # with restore_deleted the plan would need another upload → abort instead of a partial list
    tonie = FakeTonie([ch("c1")])
    tonie.on_get = [None, delete_managed]
    with pytest.raises(SyncAbort, match="changed during the sync"):
        run(FakeSource(["s1", "s2"]), tonie, copy.deepcopy(ts), restore_deleted=True)
    assert tonie.patches == []


# ── Finding 3: empty or heavily shrunk playlist ──────────────────────────────

def _managed(n):
    chapters = [ch(f"c{i}") for i in range(n)]
    mapping = {f"s{i}": {"chapter_id": f"c{i}", "file_id": f"c{i}"} for i in range(n)}
    return chapters, mapping


def test_empty_playlist_needs_confirmation():
    chapters, mapping = _managed(2)
    tonie = FakeTonie(chapters)
    with pytest.raises(SyncAbort) as e:
        run(FakeSource([]), tonie, {"mapping": mapping, "excluded": []})
    assert e.value.state == STATE_CONFIRM and tonie.patches == []
    out, _, _ = run(FakeSource([]), tonie, {"mapping": mapping, "excluded": []}, confirm_removals=True)
    assert out.state == STATE_CURRENT and tonie.chapters == []


def test_mass_removal_threshold():
    chapters, mapping = _managed(8)
    tonie = FakeTonie(chapters)
    with pytest.raises(SyncAbort) as e:  # 5 of 8 removed
        run(FakeSource(["s0", "s1", "s2"]), tonie, {"mapping": dict(mapping), "excluded": []})
    assert e.value.state == STATE_CONFIRM
    out, _, _ = run(FakeSource([f"s{i}" for i in range(5)]), tonie, {"mapping": dict(mapping), "excluded": []})
    assert out.state == STATE_CURRENT and len(tonie.chapters) == 5  # 3 of 8 removed: allowed


def test_replace_mode_empty_playlist_blocked():
    tonie = FakeTonie([ch("f1"), ch("f2")])
    with pytest.raises(SyncAbort) as e:
        run(FakeSource([]), tonie, mode="replace")
    assert e.value.state == STATE_CONFIRM


# ── Finding 4: unknown duration, cloud capacity ──────────────────────────────

def test_unknown_duration_aborts():
    with pytest.raises(SyncAbort, match="Unknown duration"):
        run(FakeSource(["s1"]), FakeTonie([ch("f1", 0)]))
    with pytest.raises(SyncAbort, match="Unknown duration"):
        run(FakeSource(["s1"], dur=0), FakeTonie([]))
    t = ch("f1")
    t["transcoding"] = True
    with pytest.raises(SyncAbort, match="Unknown duration"):
        run(FakeSource(["s1"]), FakeTonie([t]))


def test_cloud_capacity_used():
    tonie = FakeTonie([ch("f1", 100)], secondsPresent=100, secondsRemaining=150, chaptersPresent=1)
    with pytest.raises(SyncAbort) as e:
        run(FakeSource(["s1", "s2"]), tonie)  # 300 s > 250 s
    assert e.value.state == STATE_TOO_LONG


# ── Finding 5: PATCH failure and restart ─────────────────────────────────────

def test_patch_timeout_but_applied_is_resolved():
    tonie = FakeTonie([ch("f1")])
    tonie.patch_error = TimeoutError()
    out, ts, saves = run(FakeSource(["s1"]), tonie)
    assert out.state == STATE_CURRENT and "reading again" in out.message
    assert ts["mapping"]["s1"]["file_id"] == "F-s1" and ts["pending"] is None
    assert saves[0]["pending"]["uploaded"] == {"s1": "F-s1"}  # stored before the PATCH


def test_patch_failed_not_applied():
    tonie = FakeTonie([ch("f1")])
    tonie.patch_error = RuntimeError("500")
    tonie.patch_applies = False
    with pytest.raises(SyncAbort, match="Write failed"):
        run(FakeSource(["s1"]), tonie)


def test_pending_from_restart_is_resolved_no_duplicates():
    # PATCH arrived, HA restarted before saving: pending is in the store
    tonie = FakeTonie([ch("f1"), ch("F-s1")])
    ref = {"chapter_id": "F-s1", "file_id": "F-s1", "upload_id": "F-s1", "title": "As1 - Ts1", "pending": True}
    ts = {"mapping": {}, "excluded": [], "pending": {"mapping": {"s1": ref}, "uploaded": {"s1": "F-s1"}, "excluded": []}}
    out, ts, _ = run(FakeSource(["s1"]), tonie, ts)
    assert out.message.startswith(NO_CHANGE) and tonie.uploads == []
    assert ts["mapping"]["s1"]["file_id"] == "F-s1" and ts["pending"] is None


def test_pending_not_applied_is_dropped():
    tonie = FakeTonie([ch("f1")])
    ts = {"mapping": {}, "excluded": [], "pending": {"mapping": {}, "uploaded": {"s1": "F-old"}, "excluded": []}}
    out, ts, _ = run(FakeSource(["s1"]), tonie, ts)
    assert out.uploaded == 1 and ids(tonie.chapters) == ["f1", "F-s1"]


def test_upload_failure_patches_nothing():
    tonie = FakeTonie([ch("f1")])
    with pytest.raises(SyncAbort, match="orphaned"):
        run(FakeSource(["s1", "s2"], fail_download="s2"), tonie)
    assert tonie.patches == [] and tonie.uploads == ["F-s1"]


# ── Finding 7 ────────────────────────────────────────────────────────────────

def test_all_mappings_lost_aborts_with_hint():
    ts = {"mapping": {"s1": {"chapter_id": "a", "file_id": "a"}, "s2": {"chapter_id": "b", "file_id": "b"}},
          "excluded": []}
    tonie = FakeTonie([ch("x"), ch("y")])
    with pytest.raises(SyncAbort, match="restore_deleted"):
        run(FakeSource(["s1", "s2"]), tonie, ts)
    assert ts["excluded"] == [] and tonie.patches == []


# ── Cloud ID change after transcoding (observed on a real Tonie) ─────────────

UUID_NEW = "11111111-2222-4333-8444-555555555555"


def _rewrite(t, fid, file_prefix=False):
    """The cloud rewrites the freshly uploaded chapter: new UUID, transcoding done."""
    for c in t.chapters:
        if c["id"] == fid:
            c["id"] = UUID_NEW
            c["file"] = f"0123456789abcdef0123456789abcdef_{fid if file_prefix else UUID_NEW}"
            c["seconds"] = 101.0
            c["transcoding"] = False


@pytest.mark.parametrize("file_prefix", [False, True])
def test_upload_id_change_then_removal(file_prefix):
    tonie = FakeTonie([ch("f1"), ch("f2")])
    src = FakeSource(["s1"])
    out, ts, _ = run(src, tonie)
    assert out.uploaded == 1 and ids(tonie.chapters) == ["f1", "f2", "F-s1"]
    tonie.chapters[2]["transcoding"] = True  # cloud is still transcoding

    # after transcoding the cloud rewrites the ID
    _rewrite(tonie, "F-s1", file_prefix)
    out, ts, _ = run(src, tonie, ts)
    assert out.message.startswith(NO_CHANGE)
    assert ts["mapping"]["s1"]["chapter_id"] == UUID_NEW and not ts["mapping"]["s1"]["pending"]
    assert ts["excluded"] == []

    # song removed from the playlist → chapter is removed, foreign ones stay
    out, ts, _ = run(FakeSource([]), tonie, ts, confirm_removals=True)
    assert ids(tonie.chapters) == ["f1", "f2"] and out.removed == 1


def test_id_change_never_maps_to_previously_foreign_same_title():
    tonie = FakeTonie([ch("f1")])
    out, ts, _ = run(FakeSource(["s1"]), tonie)
    # cloud replaces our chapter; there is also a previously foreign chapter with the same title
    tonie.chapters = [ch("f1", title="As1 - Ts1"), {**ch(UUID_NEW, 101), "title": "As1 - Ts1"}]
    out, ts, _ = run(FakeSource(["s1"]), tonie, ts)
    assert ts["mapping"]["s1"]["chapter_id"] == UUID_NEW


def test_id_change_ambiguous_aborts():
    tonie = FakeTonie([ch("f1")])
    out, ts, _ = run(FakeSource(["s1"]), tonie)
    tonie.chapters = [ch("f1"), {**ch("u1"), "title": "As1 - Ts1"}, {**ch("u2"), "title": "As1 - Ts1"}]
    with pytest.raises(SyncAbort, match="cannot be found unambiguously"):
        run(FakeSource(["s1"]), tonie, ts)
    assert len(tonie.patches) == 1


def test_really_deleted_upload_is_excluded():
    tonie = FakeTonie([ch("f1")])
    out, ts, _ = run(FakeSource(["s1"]), tonie)
    tonie.chapters = [ch("f1")]  # deleted in the app, no replacement
    out, ts, _ = run(FakeSource(["s1"]), tonie, ts)
    assert ts["excluded"] == ["s1"] and tonie.uploads == ["F-s1"]


def test_live_scenario_adopted_plus_added_song():
    # starting point like a Tonie after adopt: managed chapters with foreign ones in between
    chapters = [ch("c1"), ch("x1"), ch("c2"), ch("c3"), ch("x2")]
    mapping = {f"s{i}": {"chapter_id": f"c{i}", "file_id": f"c{i}"} for i in (1, 2, 3)}
    tonie = FakeTonie(chapters)
    ts = {"mapping": mapping, "excluded": []}
    out, ts, _ = run(FakeSource(["s1", "s2", "s3"]), tonie, ts)
    assert out.message.startswith(NO_CHANGE)
    out, ts, _ = run(FakeSource(["s1", "s2", "s3", "new"]), tonie, ts)  # song added
    assert out.uploaded == 1 and ids(tonie.chapters)[-1] == "F-new"
    _rewrite(tonie, "F-new")
    out, ts, _ = run(FakeSource(["s1", "s2", "s3", "new"]), tonie, ts)
    assert out.message.startswith(NO_CHANGE) and ts["mapping"]["new"]["chapter_id"] == UUID_NEW
    out, ts, _ = run(FakeSource(["s1", "s2", "s3"]), tonie, ts)  # song removed again
    assert out.removed == 1 and ids(tonie.chapters) == ["c1", "x1", "c2", "c3", "x2"]
