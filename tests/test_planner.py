import pytest

from planner import (
    MODE_APPEND, MODE_REPLACE, assemble, build_plan, fingerprint, mapping_after_patch, songs_from_entries,
)


def ch(cid, seconds=100.0, title=None):
    return {"id": cid, "file": cid, "title": title or cid, "seconds": seconds, "transcoding": False}


def entries(*ids, dur=100):
    return [{"id": i, "title": f"T{i}", "artist": f"A{i}", "duration": dur} for i in ids]


def m(**kw):
    return {k: {"chapter_id": v, "file_id": v} for k, v in kw.items()}


def test_keys_for_duplicates_and_title():
    songs = songs_from_entries(entries("a", "b", "a", "a"))
    assert [s.key for s in songs] == ["a", "b", "a#2", "a#3"]
    assert songs[0].chapter_title == "Aa - Ta"
    assert songs_from_entries([{"id": "x", "title": "Solo"}])[0].chapter_title == "Solo"


def test_first_sync_appends_after_foreign():
    chapters = [ch("f1"), ch("f2")]
    plan = build_plan(chapters, songs_from_entries(entries("s1", "s2")), {}, [])
    assert [s.key for s in plan.uploads] == ["s1", "s2"]
    assert plan.removals == [] and plan.fits and not plan.unchanged
    out = assemble(plan, {"s1": "F1", "s2": "F2"})
    assert [c["id"] for c in out] == ["f1", "f2", "F1", "F2"]
    assert out[2]["title"] == "As1 - Ts1" and out[2]["seconds"] == 0
    mp = mapping_after_patch(plan, {"s1": "F1", "s2": "F2"}, None)
    assert mp["s1"] == {"chapter_id": "F1", "file_id": "F1", "upload_id": "F1", "title": "As1 - Ts1",
                        "pending": True, "after": None, "before": "s2"}
    assert mp["s2"]["after"] == "s1" and mp["s2"]["before"] is None


def test_unchanged_when_in_sync():
    chapters = [ch("f1"), ch("c1"), ch("c2")]
    plan = build_plan(chapters, songs_from_entries(entries("s1", "s2")), m(s1="c1", s2="c2"), [])
    assert plan.unchanged and not plan.uploads and not plan.removals


def test_removed_song_and_reorder_keep_foreign():
    chapters = [ch("c2"), ch("f1"), ch("c1"), ch("c3")]
    plan = build_plan(chapters, songs_from_entries(entries("s3", "s1")), m(s1="c1", s2="c2", s3="c3"), [])
    out = assemble(plan, {})
    # foreign chapter stays and moves to the start; managed ones in playlist order
    assert [c["id"] for c in out] == ["f1", "c3", "c1"]
    assert [c["id"] for c in plan.removals] == ["c2"]
    assert "s2" not in plan.mapping


def test_replace_mode_drops_foreign():
    chapters = [ch("f1", 300), ch("c1")]
    plan = build_plan(chapters, songs_from_entries(entries("s1", "s2")), m(s1="c1"), [], MODE_REPLACE)
    assert [c["id"] for c in plan.removals] == ["f1"]
    assert plan.foreign_seconds == 0 and plan.foreign_count == 0
    assert [c["id"] for c in assemble(plan, {"s2": "F2"})] == ["c1", "F2"]


def test_deleted_in_app_is_excluded_not_reuploaded():
    chapters = [ch("c2")]
    plan = build_plan(chapters, songs_from_entries(entries("s1", "s2")), m(s1="c1", s2="c2"), [])
    assert plan.uploads == [] and plan.newly_excluded == ["s1"]
    assert plan.excluded == ["s1"] and plan.excluded_active == ["s1"]
    assert plan.unchanged
    # the exclusion persists on the next run
    plan2 = build_plan(chapters, songs_from_entries(entries("s1", "s2")), plan.mapping, plan.excluded)
    assert plan2.uploads == [] and plan2.excluded == ["s1"] and plan2.newly_excluded == []


def test_exclusion_dropped_when_song_leaves_playlist_and_restore():
    plan = build_plan([], songs_from_entries(entries("s2")), {}, ["s1"])
    assert plan.excluded == []
    plan = build_plan([], songs_from_entries(entries("s1")), {}, ["s1"], restore_deleted=True)
    assert [s.key for s in plan.uploads] == ["s1"] and plan.excluded == []


def test_capacity_counts_foreign_seconds():
    chapters = [ch("f1", 5000)]
    plan = build_plan(chapters, songs_from_entries(entries("s1", "s2", dur=300)), {}, [])
    assert not plan.fits
    assert plan.total_seconds == 5600 and plan.problems == ["4 min too long"]
    # in replace mode the foreign chapter does not count
    assert build_plan(chapters, songs_from_entries(entries("s1", dur=300)), {}, [], MODE_REPLACE).fits


def test_capacity_chapter_limit():
    chapters = [ch(f"f{i}", 1) for i in range(98)]
    plan = build_plan(chapters, songs_from_entries(entries("a", "b", dur=1)), {}, [])
    assert plan.problems == ["1 chapter(s) too many"]


def test_existing_managed_uses_chapter_seconds_or_duration():
    chapters = [ch("c1", 0), ch("c2", 50)]
    plan = build_plan(chapters, songs_from_entries(entries("s1", "s2", dur=100)), m(s1="c1", s2="c2"), [])
    assert plan.playlist_seconds == 150


def test_match_by_file_when_cloud_changes_id():
    chapters = [{"id": "new-id", "file": "F1", "title": "x", "seconds": 10}]
    plan = build_plan(chapters, songs_from_entries(entries("s1")), m(s1="F1"), [])
    assert plan.unchanged and plan.mapping["s1"]["file_id"] == "F1"


def test_mapping_after_patch_takes_returned_ids_by_position():
    plan = build_plan([ch("f1")], songs_from_entries(entries("s1")), {}, [])
    result = [ch("f1"), {"id": "cloud-id", "file": "other", "seconds": 0, "title": "As1 - Ts1", "transcoding": True}]
    mp = mapping_after_patch(plan, {"s1": "F1"}, result)
    assert mp["s1"]["chapter_id"] == "cloud-id" and mp["s1"]["upload_id"] == "F1" and mp["s1"]["pending"]


def test_fingerprint_changes_with_order_and_mode():
    a = songs_from_entries(entries("1", "2"))
    b = songs_from_entries(entries("2", "1"))
    assert fingerprint(a, MODE_APPEND) != fingerprint(b, MODE_APPEND)
    assert fingerprint(a, MODE_APPEND) != fingerprint(a, MODE_REPLACE)
    assert fingerprint(a, MODE_APPEND) == fingerprint(list(a), MODE_APPEND)


def test_inputs_not_mutated():
    chapters = [ch("c1")]
    mapping = m(s1="c1")
    build_plan(chapters, songs_from_entries(entries("s2")), mapping, ["x"])
    assert chapters == [ch("c1")] and mapping == m(s1="c1")


def test_foreign_keep_position_between_managed():
    chapters = [ch("c1"), ch("f1"), ch("c2"), ch("f2")]
    mp = m(s1="c1", s2="c2")
    plan = build_plan(chapters, songs_from_entries(entries("s1", "s2")), mp, [])
    assert plan.unchanged
    # song added: append at the end, foreign ones stay at their anchors
    plan = build_plan(chapters, songs_from_entries(entries("s1", "s2", "s3")), mp, [])
    assert [c["id"] for c in assemble(plan, {"s3": "F3"})] == ["c1", "f1", "c2", "f2", "F3"]
    # anchor removed → foreign chapter attaches to the previous remaining one
    plan = build_plan(chapters, songs_from_entries(entries("s2")), mp, [])
    assert [c["id"] for c in assemble(plan, {})] == ["f1", "c2", "f2"]


def test_position_remap_never_points_to_foreign():
    plan = build_plan([ch("f1")], songs_from_entries(entries("s1")), {}, [])
    # cloud returns a chapter with a foreign ID at the position of the new chapter
    result = [ch("x"), ch("f1")]
    mp = mapping_after_patch(plan, {"s1": "F1"}, result)
    assert mp["s1"]["chapter_id"] == "F1"


def test_single_lost_mapping_is_exclusion_not_mapping_lost():
    plan = build_plan([ch("f1")], songs_from_entries(entries("s1")), m(s1="gone"), [])
    assert not plan.mapping_lost and plan.excluded == ["s1"]
