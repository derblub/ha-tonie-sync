import asyncio
import hashlib

import aiohttp
from aiohttp import web
import pytest

from subsonic import SubsonicAuthError, SubsonicClient, SubsonicError, _check, auth_params


def test_auth_params_token():
    p = auth_params("u", "pw", "abc")
    assert p["t"] == hashlib.md5(b"pwabc").hexdigest()
    assert p["s"] == "abc" and p["v"] == "1.16.1" and p["c"] == "tonie_sync" and p["f"] == "json"
    assert auth_params("u", "pw")["s"] != auth_params("u", "pw")["s"]


def test_check_errors():
    assert _check({"subsonic-response": {"status": "ok"}})["status"] == "ok"
    with pytest.raises(SubsonicAuthError):
        _check({"subsonic-response": {"status": "failed", "error": {"code": 40, "message": "x"}}})
    with pytest.raises(SubsonicError):
        _check({"subsonic-response": {"status": "failed", "error": {"code": 70}}})
    with pytest.raises(SubsonicError):
        _check({"foo": 1})


def ok(**kw):
    return web.json_response({"subsonic-response": {"status": "ok", **kw}})


async def _with_server(fn):
    app = web.Application()

    async def ping(req):
        assert req.query["u"] == "user"
        if req.query["t"] != hashlib.md5(("pw" + req.query["s"]).encode()).hexdigest():
            return web.json_response({"subsonic-response": {"status": "failed", "error": {"code": 40}}})
        return ok()

    async def playlist(req):
        return ok(playlist={"id": req.query["id"], "name": "P", "entry": [{"id": "1", "title": "T"}]})

    async def stream(req):
        if req.query["id"] == "bad":
            return web.json_response({"subsonic-response": {"status": "failed", "error": {"code": 70}}})
        assert req.query["format"] == "mp3" and req.query["maxBitRate"] == "192"
        return web.Response(body=b"ID3mp3", content_type="audio/mpeg")

    async def download(req):
        return web.Response(body=b"fLaC", content_type="audio/flac")

    async def search3(req):
        assert req.query["songCount"] == "20"
        return ok(searchResult3={"song": [{"id": "9", "title": req.query["query"]}]})

    async def create(req):
        assert req.query.getall("songId") == ["1", "2", "1"]
        return ok(playlist={"id": "new", "name": req.query["name"]})

    async def update(req):
        assert req.query["playlistId"] == "42"
        assert req.query.getall("songIdToAdd") == ["7"] and req.query.getall("songIndexToRemove") == ["0", "3"]
        return ok()

    app.router.add_get("/rest/updatePlaylist", update)
    app.router.add_get("/rest/search3", search3)
    app.router.add_get("/rest/createPlaylist", create)
    app.router.add_get("/rest/ping", ping)
    app.router.add_get("/rest/getPlaylist", playlist)
    app.router.add_get("/rest/stream", stream)
    app.router.add_get("/rest/download", download)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as s:
            await fn(s, f"http://127.0.0.1:{port}/")
    finally:
        await runner.cleanup()


def test_client_against_fake_server():
    async def run(session, url):
        c = SubsonicClient(session, url, "user", "pw")
        await c.ping()
        with pytest.raises(SubsonicAuthError):
            await SubsonicClient(session, url, "user", "wrong").ping()
        pl = await c.get_playlist("42")
        assert pl["id"] == "42" and pl["entry"][0]["id"] == "1"
        assert await c.download_song("1") == (b"ID3mp3", "mp3")
        assert await c.download_song("bad", "flac") == (b"fLaC", "flac")
        assert (await c.search_songs("Little Fox"))[0]["title"] == "Little Fox"
        assert await c.create_playlist("Tonie X", ["1", "2", "1"]) == "new"
        await c.update_playlist("42", ["7"], [0, 3])

    asyncio.run(_with_server(run))


def test_unreachable():
    async def run():
        async with aiohttp.ClientSession() as s:
            with pytest.raises(SubsonicError):
                await SubsonicClient(s, "http://127.0.0.1:9", "u", "p").ping()

    asyncio.run(run())
