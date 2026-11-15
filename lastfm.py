"""Minimal Last.fm client for album and artist top tags.

Requests are spaced out (MIN_INTERVAL seconds apart), transient failures
are retried a bounded number of times with backoff, and successful
responses (including "not found") are cached in SQLite so a rescan does
not hit Last.fm again until the cache entry is CACHE_DAYS old.
"""

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import store

API_URL = "https://ws.audioscrobbler.com/2.0/"
USER_AGENT = "genre-review/1.0 (+https://github.com/bonelifer/genre-review)"
TIMEOUT = 15
MIN_INTERVAL = 0.25
ATTEMPTS = 4
CACHE_DAYS = 90

# Last.fm error codes: 6 = not found; 11/16 = temporary; 29 = rate limit.
NOT_FOUND = {6}
TRANSIENT = {8, 11, 16, 29}


class LastfmError(Exception):
    """Last.fm refused the request in a way retrying will not fix."""


class Lastfm:
    """Fetch top tags, using the lastfm_cache table of the state DB."""

    def __init__(self, api_key: str, connection: sqlite3.Connection,
                 opener=urlopen, sleep=time.sleep):
        self.api_key = api_key
        self.connection = connection
        self.opener = opener
        self.sleep = sleep
        self.lock = threading.Lock()
        self.last_request = 0.0

    def album_tags(self, artist: str, album: str) -> list:
        """Top tags for an album as [(name, count)]; [] when unknown."""
        return self.top_tags("album.getTopTags",
                             {"artist": artist, "album": album})

    def artist_tags(self, artist: str) -> list:
        """Top tags for an artist as [(name, count)]; [] when unknown."""
        return self.top_tags("artist.getTopTags", {"artist": artist})

    def top_tags(self, method: str, params: dict) -> list:
        """Cached top-tags lookup for method with params."""
        key = method + "|" + "|".join(
            f"{name}={params[name].strip().lower()}" for name in sorted(params)
        )
        cached = self.connection.execute(
            "SELECT response, fetched_at FROM lastfm_cache WHERE request = ?",
            (key,),
        ).fetchone()
        if cached is not None:
            fetched = datetime.fromisoformat(cached["fetched_at"])
            if datetime.now(timezone.utc) - fetched < timedelta(
                    days=CACHE_DAYS):
                return [tuple(item) for item in json.loads(cached["response"])]
        tags = self.fetch(method, params)
        self.connection.execute(
            "INSERT OR REPLACE INTO lastfm_cache (request, response, "
            "fetched_at) VALUES (?, ?, ?)",
            (key, store.dumps(tags), store.now_iso()),
        )
        return tags

    def fetch(self, method: str, params: dict) -> list:
        """Call Last.fm, retrying transient failures; parse the tag list."""
        query = urlencode({**params, "method": method, "api_key": self.api_key,
                           "autocorrect": "1", "format": "json"})
        delay = 2
        for attempt in range(1, ATTEMPTS + 1):
            self.wait_turn()
            retry_after = None
            try:
                request = Request(API_URL + "?" + query,
                                  headers={"User-Agent": USER_AGENT})
                with self.opener(request, timeout=TIMEOUT) as response:
                    body = json.load(response)
            except HTTPError as error:
                body = self.error_body(error)
                if body is None:
                    if error.code < 500 and error.code != 429:
                        raise LastfmError(f"HTTP {error.code}") from error
                    retry_after = error.headers.get("Retry-After")
                    body = {"error": 29 if error.code == 429 else 16}
            except (URLError, TimeoutError, OSError, ValueError) as error:
                body = {"error": 16, "message": str(error)}
            code = body.get("error")
            if code is None:
                return self.parse(body)
            if code in NOT_FOUND:
                return []
            if code not in TRANSIENT or attempt == ATTEMPTS:
                raise LastfmError(
                    f"Last.fm error {code}: {body.get('message', 'unknown')}"
                )
            wait = delay
            if retry_after and str(retry_after).isdigit():
                wait = min(int(retry_after), 60)
            self.sleep(wait)
            delay *= 2
        raise LastfmError("Last.fm did not answer")  # pragma: no cover

    def wait_turn(self):
        """Keep at least MIN_INTERVAL seconds between requests."""
        with self.lock:
            gap = time.monotonic() - self.last_request
            if gap < MIN_INTERVAL:
                self.sleep(MIN_INTERVAL - gap)
            self.last_request = time.monotonic()

    @staticmethod
    def error_body(error: HTTPError):
        """Last.fm puts its JSON error in some 4xx bodies; None otherwise."""
        try:
            body = json.loads(error.read() or b"null")
        except (ValueError, OSError):
            return None
        return body if isinstance(body, dict) and "error" in body else None

    @staticmethod
    def parse(body: dict) -> list:
        """Extract [(name, count)] from a getTopTags response."""
        tags = body.get("toptags", {}).get("tag", [])
        if isinstance(tags, dict):
            tags = [tags]
        result = []
        for tag in tags:
            name = str(tag.get("name", "")).strip()
            try:
                count = int(tag.get("count", 0))
            except (TypeError, ValueError):
                count = 0
            if name:
                result.append((name, count))
        return result
