"""Matching of Tonie chapters to Navidrome songs. All artists and titles are made up."""
import asyncio

from matcher import (
    NONE, SURE, UNSURE, core_title, decide, default_playlist_name, loose, match_chapters, queries, splits,
)


def song(i, artist, title, duration, album="A"):
    return {"id": i, "artist": artist, "title": title, "duration": duration, "album": album}


def chap(title, seconds, cid="c"):
    return {"id": cid, "file": cid, "title": title, "seconds": seconds}


def test_loose_tolerates_broken_umlauts():
    assert loose("Jörg Glück") == loose("Jrg Glck") == "jrg glck"
    assert loose("Die Wald-Bären") == loose("Die Wald-Bren")
    assert loose("Für Mia!") == "fr mia"


def test_core_title_strips_suffixes():
    assert core_title("Kinder Boogie - Single Edit") == "kinder boogie"
    assert core_title("Little Fox Song (Radio Edit)") == "little fox song"
    assert core_title("Mix It Up") == "mix it up"


def test_splits_and_queries():
    s = splits("Paul Berg Kinderlieder handgemacht - Kinder Boogie - Single Edit")
    assert (None, "Paul Berg Kinderlieder handgemacht - Kinder Boogie - Single Edit") in s
    assert ("Paul Berg Kinderlieder handgemacht", "Kinder Boogie - Single Edit") in s
    q = queries("Paul Berg Kinderlieder handgemacht - Kinder Boogie - Single Edit")
    assert q[0] == "Kinder Boogie"
    assert queries("Lalalu")[0] == "Lalalu"


def test_artist_and_title_sure():
    m = decide(1, chap("Mia & Ben, Clara Nord - Little Fox Song", 137.7),
               [song("1", "Mia & Ben, Clara Nord", "Little Fox Song", 138),
                song("2", "Others", "Little Fox Song", 201)])
    assert m.confidence == SURE and m.song["id"] == "1"


def test_captain_sure():
    m = decide(1, chap("4Hamburg - Captain Bob from Boston", 135.3),
               [song("7", "4Hamburg", "Captain Bob from Boston", 135)])
    assert m.confidence == SURE


def test_single_edit_sure():
    m = decide(1, chap("Paul Berg Kinderlieder handgemacht - Kinder Boogie - Single Edit", 150),
               [song("9", "Paul Berg Kinderlieder handgemacht", "Kinder Boogie", 151)])
    assert m.confidence == SURE


def test_broken_umlauts_in_artist_sure():
    m = decide(1, chap("Lisa Sonnenfeld, Jrg Glck, Die Wald-Bren - Nachts im Wald", 184),
               [song("5", "Lisa Sonnenfeld, Jörg Glück & Die Wald-Bären", "Nachts im Wald", 183)])
    assert m.confidence == SURE


def test_title_only_sure_and_duration_matters():
    songs = [song("m", "Orchestra Uno", "Là Là Lu", 190), song("n", "The Puppets", "Lalalu", 191)]
    m = decide(1, chap("Lalalu", 190.2), songs)
    assert m.confidence == SURE and m.song["id"] == "n"
    m = decide(1, chap("Lalalu", 150), songs)
    assert m.confidence == UNSURE and "duration" in m.reason


def test_not_in_library():
    m = decide(1, chap("Lights Out - For Mia", 156.9), [song("x", "Some Singer", "Lights Out", 120)])
    assert m.confidence != SURE


def test_ambiguous_without_artist():
    songs = [song("a", "X", "Lullaby", 120, "Album 1"), song("b", "Y", "Lullaby", 121, "Album 2")]
    m = decide(1, chap("Lullaby", 120), songs)
    assert m.confidence == UNSURE and "ambiguous" in m.reason


def test_wrong_artist_unsure():
    m = decide(1, chap("Some Singer - Little Fox Song", 138), [song("1", "Mia & Ben", "Little Fox Song", 138)])
    assert m.confidence == UNSURE


def test_unknown_chapter_seconds_unsure():
    m = decide(1, chap("4Hamburg - Captain Bob from Boston", 0), [song("7", "4Hamburg", "Captain Bob from Boston", 135)])
    assert m.confidence == UNSURE


def test_no_candidates():
    assert decide(1, chap("Nothing", 100), []).confidence == NONE


def test_match_chapters_uses_search_and_cache():
    calls = []

    async def search(q):
        calls.append(q)
        return [song("1", "Mia & Ben, Clara Nord", "Little Fox Song", 138)] if "Little Fox" in q else []

    res = asyncio.run(match_chapters(
        [chap("Mia & Ben, Clara Nord - Little Fox Song", 137.7, "c1"), chap("Unknown", 10, "c2")], search))
    assert [m.confidence for m in res] == [SURE, NONE]
    assert calls[0] == "Little Fox Song" and res[0].as_dict()["match"]["id"] == "1"


def test_default_playlist_name():
    assert default_playlist_name("Kreativ-Tonie Bedtime") == "Tonie Bedtime"
    assert default_playlist_name("Creative-Tonie Bedtime") == "Tonie Bedtime"
    assert default_playlist_name("Sleepy Time") == "Tonie Sleepy Time"


def test_equivalent_copies_pick_real_album():
    songs = [song("u", "Tom und die Ottergang", "Ein kleiner Drache", 189, "[Unknown Album]"),
             song("s", "Tom und die Ottergang", "Ein kleiner Drache", 189, "Singles"),
             song("a", "Tom und die Ottergang", "Ein kleiner Drache", 189, "Drachenalbum")]
    m = decide(1, chap("Tom und die Ottergang - Ein kleiner Drache", 189.1), songs)
    assert m.confidence == SURE and m.song["id"] == "a" and "3 equivalent" in m.reason


def test_same_album_split_artists_are_copies():
    songs = [song("1", "Lisa Sonnenfeld", "Das Hüpflied", 183, "Die 30 besten Partylieder"),
             song("2", "Die Wald-Bären", "Das Hüpflied", 183, "Die 30 besten Partylieder")]
    assert decide(1, chap("Das Hüpflied", 183.1), songs).confidence == SURE


def test_different_recordings_stay_ambiguous():
    songs = [song("1", "X", "Lullaby", 120, "[Unknown Album]"), song("2", "Y", "Lullaby", 120, "Singles")]
    assert decide(1, chap("Lullaby", 120), songs).confidence == UNSURE
