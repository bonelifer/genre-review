"""Deezer lyrics client: a second lyrics source after LRCLIB.

Tracks are found with Deezer's public search API (no login). Lyrics come
from Deezer's lyrics service (pipe.deezer.com), which needs a short-lived
token obtained with the ARL login cookie deemix uses. This service is not
a published API, so it may change without notice; every failure is
reported instead of guessed around.

The ARL is read fresh for every pass (from a file such as deemix's
~/.config/deemix/.arl, or from an environment value) and is never logged,
shown or stored.
"""

import json
import re
import time
import unicodedata
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

AUTH_URL = "https://auth.deezer.com/login/arl?jo=p&rto=c&i=c"
LYRICS_URL = "https://pipe.deezer.com/api"
SEARCH_URL = "https://api.deezer.com/search?"
USER_AGENT = "music-librarian 1.0 (https://github.com/bonelifer/music-librarian)"
TIMEOUT = 20
PAUSE = 1.0             # seconds between requests
ATTEMPTS = 3            # per request, for timeouts and server errors
TOKEN_SECONDS = 300     # Deezer's token lasts 6 minutes; renew after 5
LENGTH_TOLERANCE = 3    # seconds a match may differ from the file
SEARCH_RESULTS = 10
LYRICS_QUERY = ("query($id: String!) { track(trackId: $id) { lyrics { "
                "text synchronizedLines { lrcTimestamp line } } } }")


class DeezerError(Exception):
    """Deezer failed in a way that should stop the Deezer pass."""


def read_arl(arl_file: str = "", arl_value: str = "") -> str:
    """The ARL from arl_file when it has one, else arl_value ('' if none)."""
    if arl_file:
        try:
            text = Path(arl_file).read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text:
            return text
    return arl_value.strip()


def simple(text: str) -> str:
    """Lowercase letters and digits only, accents removed, for matching."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return re.sub(r"[^0-9a-z]", "",
                  "".join(c for c in decomposed
                          if not unicodedata.combining(c)).casefold())


def same_artist(file_artist: str, deezer_artist: str) -> bool:
    """True when Deezer's artist name appears in the file's artist credit.

    'Gene Autry with Carl Cotner' matches Deezer's 'Gene Autry', and
    'Austin Meade/Treaty Oak Revival' matches 'Austin Meade'.
    """
    wanted, found = simple(file_artist), simple(deezer_artist)
    return bool(wanted and found) and (found in wanted or wanted in found)


def to_lrc(lines) -> str:
    """Deezer's synchronized lines as LRC text ('' when there are none)."""
    rows = [f"{line.get('lrcTimestamp', '')}{line.get('line', '')}"
            for line in lines or [] if line.get("lrcTimestamp")]
    return "\n".join(rows)


class Deezer:
    """Polite, sequential Deezer client for search and lyrics."""

    def __init__(self, arl: str, opener=urlopen, sleep=time.sleep,
                 clock=time.monotonic):
        if not arl:
            raise DeezerError("no Deezer ARL found (check DEEZER_ARL_FILE "
                              "or DEEZER_ARL)")
        self.arl = arl
        self.opener = opener
        self.sleep = sleep
        self.clock = clock
        self.last = 0.0
        self.token = ""
        self.token_at = 0.0

    def send(self, request: Request):
        """Send one request at Deezer's pace; parsed JSON back.

        Retries timeouts and server errors; raises ConnectionError when
        Deezer cannot be reached and DeezerError for other HTTP errors.
        """
        delay = 2
        for attempt in range(1, ATTEMPTS + 1):
            gap = self.clock() - self.last
            if gap < PAUSE:
                self.sleep(PAUSE - gap)
            self.last = self.clock()
            try:
                with self.opener(request, timeout=TIMEOUT) as response:
                    return json.load(response)
            except HTTPError as error:
                if error.code in (401, 403):
                    raise DeezerError("Deezer refused the login (HTTP "
                                      f"{error.code}); the ARL may have "
                                      "expired") from error
                if error.code < 500 and error.code != 429:
                    raise DeezerError(f"Deezer error HTTP {error.code}") \
                        from error
                if attempt == ATTEMPTS:
                    raise ConnectionError(f"HTTP {error.code}") from error
            except (URLError, TimeoutError, OSError, ValueError) as error:
                if attempt == ATTEMPTS:
                    raise ConnectionError(str(error)) from error
            self.sleep(delay)
            delay *= 2
        return None  # pragma: no cover

    def login(self) -> None:
        """Trade the ARL for a short-lived lyrics token."""
        reply = self.send(Request(AUTH_URL, data=b"", method="POST", headers={
            "Cookie": f"arl={self.arl}", "User-Agent": USER_AGENT}))
        token = (reply or {}).get("jwt") if isinstance(reply, dict) else None
        if not token:
            raise DeezerError("Deezer refused the login; the ARL may have "
                              "expired")
        self.token, self.token_at = token, self.clock()

    def search(self, artist: str, title: str) -> list:
        """Deezer tracks for 'artist title' (public search, no login)."""
        request = Request(SEARCH_URL + urlencode({
            "q": f"{artist} {title}", "limit": SEARCH_RESULTS}),
            headers={"User-Agent": USER_AGENT})
        for _ in range(ATTEMPTS):
            reply = self.send(request)
            error = reply.get("error") if isinstance(reply, dict) else None
            if not error:
                return (reply or {}).get("data") or []
            if error.get("code") != 4:  # 4 = quota: wait and try again
                raise DeezerError("Deezer search: "
                                  + str(error.get("message", "failed")))
            self.sleep(5)
        raise DeezerError("Deezer search keeps hitting its rate limit")

    def find(self, info: dict):
        """The Deezer track matching a file by artist and length, or None."""
        title, artist = info.get("title"), info.get("artist")
        length = info.get("duration") or 0
        if not title or not artist or not length:
            return None
        for hit in self.search(artist, title):
            if abs(int(hit.get("duration") or 0) - length) \
                    <= LENGTH_TOLERANCE and \
                    same_artist(artist, (hit.get("artist") or {})
                                .get("name", "")):
                return hit
        return None

    def lyrics(self, track_id) -> dict:
        """{'synced': LRC text, 'plain': text} for a track (either '')."""
        for attempt in (1, 2):
            if not self.token or self.clock() - self.token_at \
                    > TOKEN_SECONDS:
                self.login()
            reply = self.send(Request(LYRICS_URL, data=json.dumps({
                "query": LYRICS_QUERY,
                "variables": {"id": str(track_id)}}).encode("utf-8"),
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json",
                         "User-Agent": USER_AGENT})) or {}
            errors = reply.get("errors") or []
            if any("Jwt" in str(item.get("type", "")) for item in errors):
                self.token = ""  # expired early: log in again once
                if attempt == 2:
                    raise DeezerError("Deezer keeps rejecting the lyrics "
                                      "token; the ARL may have expired")
                continue
            track = (reply.get("data") or {}).get("track") or {}
            found = track.get("lyrics") or {}
            return {"synced": to_lrc(found.get("synchronizedLines")),
                    "plain": (found.get("text") or "").strip()}
        return {"synced": "", "plain": ""}  # pragma: no cover
