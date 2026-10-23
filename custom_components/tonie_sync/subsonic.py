"""Small Subsonic/OpenSubsonic client (Navidrome), aiohttp only, no HA imports."""
from __future__ import annotations

import hashlib
import re
import secrets
from typing import Any

import aiohttp

API_VERSION = "1.16.1"
CLIENT_NAME = "tonie_sync"
MAX_SONG_BYTES = 150 * 1024 * 1024  # guard against huge files in memory
TIMEOUT = aiohttp.ClientTimeout(total=60)
STREAM_TIMEOUT = aiohttp.ClientTimeout(total=600, sock_read=120)


# Query strings and auth parameters that must never end up in messages, logs or the store:
# u, t and s together form a reusable login token, p would be the password itself.
_URL_QUERY = re.compile(r"(\w+://[^\s?#'\"<>]*)\?[^\s'\"<>]*")
_AUTH_PARAM = re.compile(r"(?<![\w-])([utsp])=[^&\s'\"<>]*")


def redact(text: str) -> str:
    """Remove URL query strings and Subsonic auth parameters from a message."""
    return _AUTH_PARAM.sub(r"\1=***", _URL_QUERY.sub(r"\1", text))


def describe_error(err: BaseException) -> str:
    """Short, safe description of an exception: never contains a URL query or auth parameters."""
    if isinstance(err, aiohttp.ClientResponseError):
        return f"{type(err).__name__} (HTTP {err.status})"
    if isinstance(err, TimeoutError):
        return "timeout"
    text = str(err)
    if isinstance(err, SubsonicError):
        return redact(text)
    return redact(f"{type(err).__name__}: {text}" if text else type(err).__name__)


class SubsonicError(Exception):
    """Error from the server or the connection."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class SubsonicAuthError(SubsonicError):
    """Wrong username or password (Subsonic code 40/41)."""


def auth_params(user: str, password: str, salt: str | None = None) -> dict[str, str]:
    salt = salt or secrets.token_hex(8)
    token = hashlib.md5((password + salt).encode()).hexdigest()  # noqa: S324, mandated by the protocol
    return {"u": user, "t": token, "s": salt, "v": API_VERSION, "c": CLIENT_NAME, "f": "json"}


def _check(payload: Any) -> dict[str, Any]:
    resp = payload.get("subsonic-response") if isinstance(payload, dict) else None
    if not isinstance(resp, dict):
        raise SubsonicError("Unexpected response (no subsonic-response)")
    if resp.get("status") != "ok":
        err = resp.get("error") or {}
        code = err.get("code")
        msg = err.get("message") or "Unknown error"
        if code in (40, 41):
            raise SubsonicAuthError(msg, code)
        raise SubsonicError(msg, code)
    return resp


class SubsonicClient:
    def __init__(self, session: aiohttp.ClientSession, url: str, user: str, password: str) -> None:
        self._session = session
        self._base = url.rstrip("/") + "/rest/"
        self._user = user
        self._password = password

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, str]:
        p = auth_params(self._user, self._password)
        if extra:
            p.update({k: str(v) for k, v in extra.items()})
        return p

    async def _call(self, endpoint: str, _multi: list[tuple[str, str]] | None = None, **extra: Any) -> dict[str, Any]:
        params: Any = self._params(extra)
        if _multi:
            params = list(params.items()) + _multi  # repeated parameters (songId)
        try:
            async with self._session.get(self._base + endpoint, params=params, timeout=TIMEOUT) as r:
                if r.status in (401, 403):
                    raise SubsonicAuthError(f"HTTP {r.status}")
                if r.status >= 400:
                    raise SubsonicError(f"HTTP {r.status} for {endpoint}")
                data = await r.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            # "from None": the aiohttp error carries the request URL with the auth token
            raise SubsonicError(f"Cannot reach Navidrome: {describe_error(err)}") from None
        return _check(data)

    async def ping(self) -> None:
        await self._call("ping")

    async def get_playlists(self) -> list[dict[str, Any]]:
        resp = await self._call("getPlaylists")
        return list((resp.get("playlists") or {}).get("playlist") or [])

    async def get_playlist(self, playlist_id: str) -> dict[str, Any]:
        resp = await self._call("getPlaylist", id=playlist_id)
        pl = resp.get("playlist")
        if not isinstance(pl, dict):
            raise SubsonicError(f"Playlist {playlist_id} not found")
        if not isinstance(pl.get("entry"), list):
            # Navidrome only omits "entry" for a truly empty playlist
            if pl.get("songCount") == 0:
                pl["entry"] = []
            else:
                raise SubsonicError(f"Playlist {playlist_id}: response without entries (songCount={pl.get('songCount')})")
        return pl

    async def search_songs(self, query: str, count: int = 20) -> list[dict[str, Any]]:
        resp = await self._call("search3", query=query, songCount=count, artistCount=0, albumCount=0)
        return list((resp.get("searchResult3") or {}).get("song") or [])

    async def create_playlist(self, name: str, song_ids: list[str]) -> str | None:
        """Create a new playlist; returns its ID (if the server provides it)."""
        resp = await self._call("createPlaylist", _multi=[("songId", sid) for sid in song_ids], name=name)
        pl = resp.get("playlist")
        return str(pl["id"]) if isinstance(pl, dict) and pl.get("id") else None

    async def update_playlist(
        self, playlist_id: str, add_ids: list[str] | None = None, remove_indexes: list[int] | None = None
    ) -> None:
        """Append songs or remove them by position (0-based, as before the change)."""
        multi = [("songIdToAdd", sid) for sid in add_ids or []]
        multi += [("songIndexToRemove", str(i)) for i in remove_indexes or []]
        await self._call("updatePlaylist", _multi=multi, playlistId=playlist_id)

    async def _fetch(self, endpoint: str, **extra: Any) -> bytearray:
        async with self._session.get(
            self._base + endpoint, params=self._params(extra), timeout=STREAM_TIMEOUT
        ) as r:
            ctype = r.headers.get("Content-Type", "")
            if r.status >= 400:
                raise SubsonicError(f"HTTP {r.status} for {endpoint}")
            if "json" in ctype or "xml" in ctype:
                # Subsonic reports streaming errors as JSON/XML instead of audio
                try:
                    _check(await r.json(content_type=None))
                except ValueError:
                    pass
                raise SubsonicError(f"{endpoint} returned no audio ({ctype})")
            if r.content_length and r.content_length > MAX_SONG_BYTES:
                raise SubsonicError(f"File too large ({r.content_length} bytes)")
            buf = bytearray()
            async for chunk in r.content.iter_chunked(256 * 1024):
                buf.extend(chunk)
                if len(buf) > MAX_SONG_BYTES:
                    raise SubsonicError("File too large")
            if not buf:
                raise SubsonicError(f"{endpoint} returned an empty file")
            return buf  # no bytes() copy: that would double the peak memory

    async def download_song(self, song_id: str, suffix: str = "mp3") -> tuple[bytearray, str]:
        """MP3 via stream (192 kbit/s); falls back to the original via download.

        Returns (data, file extension).
        """
        try:
            return await self._fetch("stream", id=song_id, format="mp3", maxBitRate=192), "mp3"
        except SubsonicAuthError:
            raise
        except (SubsonicError, aiohttp.ClientError, TimeoutError) as first:
            try:
                return await self._fetch("download", id=song_id), (suffix or "mp3")
            except (SubsonicError, aiohttp.ClientError, TimeoutError) as err:
                raise SubsonicError(
                    f"Cannot load song {song_id}: {describe_error(first)}; fallback: {describe_error(err)}"
                ) from None
