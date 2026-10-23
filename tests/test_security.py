"""Credentials never leak into messages; untrusted server values are sanitised; diagnostics are anonymised."""
import asyncio
import re

import aiohttp
from aiohttp import web
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from engine import SyncAbort, run_sync
from planner import safe_suffix, songs_from_entries
from privacy import REDACTED, redact_keys, replace_ids
from subsonic import SubsonicClient, SubsonicError, describe_error, redact
from targets import TargetError, choose_entry

TOKEN_URL = "http://navidrome.local:4533/rest/stream?u=alice&t=0123456789abcdef&s=a1b2c3&v=1.16.1&id=7"
LEAK = re.compile(r"(?<![\w-])[uts]=(?!\*)")


def no_leak(text):
    return "0123456789abcdef" not in text and "a1b2c3" not in text and "alice" not in text and not LEAK.search(text)


def test_redact_strips_query_and_auth_params():
    assert redact(f"error for {TOKEN_URL} here") == "error for http://navidrome.local:4533/rest/stream here"
    assert no_leak(redact("bad u=alice&t=0123456789abcdef&s=a1b2c3"))
    assert redact("plain message") == "plain message"


def test_describe_client_response_error_has_no_url():
    info = aiohttp.RequestInfo(URL(TOKEN_URL), "GET", CIMultiDictProxy(CIMultiDict()), URL(TOKEN_URL))
    err = aiohttp.TooManyRedirects(info, (), status=302)
    assert describe_error(err) == "TooManyRedirects (HTTP 302)"
    err = aiohttp.ClientResponseError(info, (), status=500, message="Internal")
    assert no_leak(describe_error(err))
    assert no_leak(describe_error(RuntimeError(f"GET {TOKEN_URL} failed")))
    assert describe_error(TimeoutError()) == "timeout"


def test_redirect_loop_does_not_leak_token():
    async def go():
        app = web.Application()

        async def loop(req):
            raise web.HTTPFound(str(req.rel_url))

        app.router.add_get("/rest/ping", loop)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as s:
                with pytest.raises(SubsonicError) as e:
                    await SubsonicClient(s, f"http://127.0.0.1:{port}/", "alice", "secret").ping()
        finally:
            await runner.cleanup()
        return e.value

    err = asyncio.run(go())
    assert "Redirect" in str(err) and no_leak(str(err)) and err.__cause__ is None and err.__suppress_context__


def test_upload_failure_message_does_not_leak_token():
    class Src:
        async def get_playlist(self, pid):
            return {"id": pid, "name": "P", "entry": [{"id": "s1", "title": "T", "duration": 100}]}

        async def download_song(self, sid, suffix="mp3"):
            raise RuntimeError(f"GET {TOKEN_URL} failed")

    class Tonie:
        async def get(self):
            return {"chapters": []}

    async def persist():
        pass

    with pytest.raises(SyncAbort) as e:
        asyncio.run(run_sync(source=Src(), tonie=Tonie(), ts={"mapping": {}, "excluded": []}, persist=persist,
                             report=lambda *a, **k: None, now=lambda: "now", playlist_id="p"))
    assert no_leak(str(e.value))


@pytest.mark.parametrize("value,expected", [
    ("flac", "flac"), ("MP3", "mp3"), ("m4a", "m4a"), (None, "mp3"), ("", "mp3"),
    ("../../x", "mp3"), ("mp3/../a", "mp3"), ("toolongx", "mp3"), ("mp 3", "mp3"), ("ogg\n", "ogg"),
])
def test_safe_suffix(value, expected):
    assert safe_suffix(value) == expected


def test_songs_from_entries_sanitises_suffix():
    assert songs_from_entries([{"id": "1", "suffix": "../evil"}])[0].suffix == "mp3"


def test_upload_filename_is_sanitised():
    names = []

    class Src:
        async def get_playlist(self, pid):
            return {"id": pid, "name": "P", "entry": [{"id": "../a b", "title": "T", "duration": 100}]}

        async def download_song(self, sid, suffix="mp3"):
            return bytearray(b"x"), "../exe"

    class Tonie:
        async def get(self):
            return {"chapters": []}

        async def upload(self, data, filename):
            names.append(filename)
            raise RuntimeError("stop")

    async def persist():
        pass

    with pytest.raises(SyncAbort):
        asyncio.run(run_sync(source=Src(), tonie=Tonie(), ts={"mapping": {}, "excluded": []}, persist=persist,
                             report=lambda *a, **k: None, now=lambda: "now", playlist_id="p"))
    assert names == ["___a_b.mp3"]


def test_choose_entry():
    one = {"e1": {"t1"}}
    two = {"e1": {"t1"}, "e2": {"t2"}}
    assert choose_entry(one) == "e1"
    assert choose_entry(two, explicit="e2") == "e2"
    assert choose_entry(two, owner="e1") == "e1"
    assert choose_entry(two, tonie_id="t2") == "e2"
    for kwargs, key in [
        ({}, "multiple_entries"), ({"tonie_id": "tx"}, "multiple_entries"), ({"explicit": "zz"}, "invalid_entry"),
        ({"explicit": "e1", "owner": "e2"}, "entry_mismatch"), ({"owner": "zz"}, "invalid_entry"),
    ]:
        with pytest.raises(TargetError, match=key):
            choose_entry(two, **kwargs)
    with pytest.raises(TargetError, match="not_set_up"):
        choose_entry({})
    two_same = {"e1": {"t1"}, "e2": {"t1"}}
    with pytest.raises(TargetError, match="multiple_entries"):
        choose_entry(two_same, tonie_id="t1")


def test_diagnostics_helpers_anonymise():
    data = {
        "entry": {"data": {"url": "http://nd.local", "username": "alice", "password": "pw"},
                  "options": {"tonies": {"ABC123": {"name": "X", "household_id": "HH1", "playlist": "p"}}}},
        "tonies": {"ABC123": {"status": {"attrs": {"note": "ABC123_status"}}}},
    }
    out = replace_ids(redact_keys(data, {"url", "username", "password", "household_id"}), {"ABC123": "tonie_1"})
    text = repr(out)
    assert "ABC123" not in text and "HH1" not in text and "alice" not in text and "pw'" not in text
    assert out["entry"]["data"]["url"] == REDACTED
    assert out["entry"]["options"]["tonies"]["tonie_1"]["playlist"] == "p"
    assert out["tonies"]["tonie_1"]["status"]["attrs"]["note"] == "tonie_1_status"
    assert data["entry"]["data"]["url"] == "http://nd.local"  # input not modified
