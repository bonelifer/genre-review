"""Lyrics coverage and fill from LRCLIB (https://lrclib.net).

Tracks that already have lyrics (a sidecar file next to them, or lyrics
embedded in their tags) are left alone. For the rest, LRCLIB is asked for
the track, and the result is saved as a sidecar file with the track's base
name, the way myMPD and most players look for it:

    Song.mp3  ->  Song.lrc  (synced lyrics)
                  Song.txt  (plain lyrics, when no synced version exists)

Existing files are never overwritten. Every file written is recorded with
its SHA-256, so "remove written files" only deletes files that are still
exactly as music-librarian wrote them.

Requests go one at a time with a pause between them, identify this client
in the User-Agent, and honour Retry-After on HTTP 429, as LRCLIB asks for
batch work.
"""

import hashlib
import json
import os
import random
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import mutagen

import store

API = "https://lrclib.net/api/"
USER_AGENT = "music-librarian 1.0 (https://github.com/bonelifer/music-librarian)"
TIMEOUT = 20
PAUSE = 0.75          # seconds between requests (about 80 a minute)
ATTEMPTS = 3          # per request, for timeouts and server errors
NOT_FOUND_RETRY_DAYS = 90
ERROR_RETRY_DAYS = 1
SYNCED_EXT, PLAIN_EXT = ".lrc", ".txt"
DURATION_TOLERANCE = 2

# Words that mark a title or album suffix as an edition note, not a name.
EDITION_WORDS = re.compile(
    r"remaster|mono|stereo|version|edit\b|deluxe|bonus|explicit|clean|"
    r"anniversary|expanded|edition|single|feat\.|ft\.|featuring",
    re.IGNORECASE)


class LyricsError(Exception):
    """LRCLIB failed in a way that should stop the whole fill."""


class Lrclib:
    """Polite, sequential LRCLIB client."""

    def __init__(self, opener=urlopen, sleep=time.sleep):
        self.opener = opener
        self.sleep = sleep
        self.last = 0.0

    def request(self, endpoint: str, params: dict):
        """GET an endpoint; returns parsed JSON, or None for 404."""
        url = API + endpoint + "?" + urlencode(params)
        delay = 2
        for attempt in range(1, ATTEMPTS + 1):
            gap = time.monotonic() - self.last
            if gap < PAUSE:
                self.sleep(PAUSE - gap)
            self.last = time.monotonic()
            try:
                request = Request(url, headers={"User-Agent": USER_AGENT})
                with self.opener(request, timeout=TIMEOUT) as response:
                    return json.load(response)
            except HTTPError as error:
                if error.code == 404:
                    return None
                if error.code == 429:
                    wait = error.headers.get("Retry-After", "")
                    wait = min(int(wait), 300) if wait.isdigit() else 60
                    if attempt == ATTEMPTS:
                        raise LyricsError("LRCLIB keeps asking to slow down "
                                          "(HTTP 429); stopped") from error
                    self.sleep(wait * (1 + random.uniform(0, 0.3)))
                    continue
                if error.code < 500 or attempt == ATTEMPTS:
                    raise LyricsError(f"LRCLIB error HTTP {error.code}") \
                        from error
            except (URLError, TimeoutError, OSError, ValueError) as error:
                if attempt == ATTEMPTS:
                    raise ConnectionError(str(error)) from error
            self.sleep(delay)
            delay *= 2
        return None  # pragma: no cover

    def get(self, title, artist, album, duration):
        """Exact lookup (duration must match within 2 seconds)."""
        params = {"track_name": title, "artist_name": artist}
        if album:
            params["album_name"] = album
        if duration:
            params["duration"] = round(duration)
        return self.request("get", params)

    def search(self, title, artist):
        """Search by title and artist; a list of records."""
        return self.request("search", {"track_name": title,
                                       "artist_name": artist}) or []


def clean_name(name: str) -> str:
    """Drop edition notes: 'Song (2013 Remaster)' -> 'Song'."""
    cleaned = re.sub(r"\s*[\(\[]([^\)\]]*)[\)\]]",
                     lambda m: "" if EDITION_WORDS.search(m.group(1))
                     else m.group(0), name)
    cleaned = re.sub(r"\s+-\s+([^-]+)$",
                     lambda m: "" if EDITION_WORDS.search(m.group(1))
                     else m.group(0), cleaned)
    return cleaned.strip() or name


def primary_artist(artist: str) -> str:
    """First artist of a joined credit: 'A/B', 'A; B', 'A feat. B' -> 'A'."""
    first = re.split(r"\s*(?:/|;|,|&|\bfeat\.?|\bft\.?|\bfeaturing)\s*",
                     artist, maxsplit=1, flags=re.IGNORECASE)[0]
    return first.strip() or artist


def track_info(path: Path) -> dict:
    """Title, artist, album and duration of one file (empty on failure)."""
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:  # mutagen raises many error types
        return {}
    if audio is None:
        return {}
    tags = audio.tags

    def first(key):
        values = tags.get(key) or [] if tags is not None else []
        return str(values[0]).strip() if values else ""

    info = {"title": first("title"), "artist": first("artist"),
            "album": first("album"),
            "duration": getattr(audio.info, "length", 0) or 0}
    if not info["title"]:
        # Untagged files: "1.01 Artist - Title.mp3" from the file name.
        stem = re.sub(r"^[\d.\s_-]*\d[\s._-]+", "", path.stem)
        artist, sep, title = stem.partition(" - ")
        if sep:
            info["title"] = title.strip()
            info["artist"] = info["artist"] or artist.strip()
    return info


def bare_name(name: str) -> str:
    """Drop every bracketed part: 'Song (Live at X, 1975)' -> 'Song'."""
    return re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", name).strip() or name


def plain_from_synced(text: str) -> str:
    """Strip LRC timestamps and tags, leaving the words."""
    lines = [re.sub(r"^(\[[^\]]*\])+\s*", "", line)
             for line in text.splitlines()]
    return "\n".join(line for line in lines if line.strip())


def choose(records, duration):
    """Best search result.

    A record within the duration tolerance wins, synced preferred. When no
    record has the right length (live takes, edits), the best other match
    is returned as plain lyrics only, since its timings would not fit.
    """
    def ranked(items):
        return sorted(items, key=lambda record: (
            not record.get("syncedLyrics"), not record.get("plainLyrics")))

    fitting = [record for record in records
               if not duration or abs((record.get("duration") or 0)
                                      - duration) <= DURATION_TOLERANCE]
    if fitting:
        return ranked(fitting)[0]
    for record in ranked(records):
        if record.get("instrumental"):
            continue
        words = record.get("plainLyrics") or plain_from_synced(
            record.get("syncedLyrics") or "")
        if words.strip():
            return {**record, "syncedLyrics": None, "plainLyrics": words}
    return None


def lookup(client: Lrclib, info: dict):
    """Find a record for a track: exact, then cleaned names, then search."""
    title, artist = info.get("title"), info.get("artist")
    if not title or not artist:
        return None
    album, duration = info.get("album", ""), info.get("duration", 0)
    record = client.get(title, artist, album, duration)
    if record:
        return record
    title2, artist2 = clean_name(title), primary_artist(artist)
    album2 = clean_name(album) if album else ""
    if (title2, artist2, album2) != (title, artist, album):
        record = client.get(title2, artist2, album2, duration)
        if record:
            return record
    record = choose(client.search(title2, artist2), duration)
    if record:
        return record
    title3 = bare_name(title2)
    if title3 != title2:
        return choose(client.search(title3, artist2), duration)
    return None


def sidecars(path: Path):
    """The synced and plain sidecar paths for a track."""
    return path.with_suffix(SYNCED_EXT), path.with_suffix(PLAIN_EXT)


def has_embedded(fields) -> bool:
    """True when a track's tag inventory includes lyrics frames/comments."""
    return any(key.split(":", 1)[0] in ("USLT", "SYLT")
               or key.upper() in ("LYRICS", "SYNCEDLYRICS", "UNSYNCEDLYRICS",
                                  "\xa9LYR")
               for key in fields or {})


def write_new(target: Path, text: str) -> str:
    """Create target with text (never replacing a file); returns SHA-256."""
    data = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"
    encoded = data.encode("utf-8")
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".lyrics-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(encoded)
        os.chmod(temporary, 0o644)
        os.link(temporary, target)  # fails if target exists: no overwrite
    finally:
        os.unlink(temporary)
    return hashlib.sha256(encoded).hexdigest()


# Text patterns for a quick SQL pre-filter of tracks with embedded lyrics;
# has_embedded() then confirms on the few rows that match.
EMBEDDED_LIKE = ('%"USLT:%', '%"SYLT:%', '%"LYRICS"%', '%"SYNCEDLYRICS"%',
                 '%"UNSYNCEDLYRICS"%', '%"\u00a9lyr"%', '%"©lyr"%')


def coverage(connection: sqlite3.Connection, root: Path) -> dict:
    """Per-track lyrics state and library totals.

    Returns {"tracks": {path: state}, "totals": {state: count}} where state
    is synced-file, plain-file, embedded, instrumental, not-found, error or
    unchecked. Each album folder is listed once instead of checking every
    possible sidecar file separately.
    """
    looked_up = {row["path"]: row["status"] for row in connection.execute(
        "SELECT path, status FROM lyrics")}
    likes = " OR ".join("lower(fields) LIKE lower(?)" for _ in EMBEDDED_LIKE)
    embedded = {row["path"] for row in connection.execute(
        f"SELECT path, fields FROM tracks WHERE fields IS NOT NULL AND "
        f"({likes})", EMBEDDED_LIKE)
        if has_embedded(store.loads(row["fields"], {}))}
    folders = {}
    states = {}
    for (relative,) in connection.execute("SELECT path FROM tracks"):
        folder, _, name = relative.rpartition("/")
        if folder not in folders:
            try:
                with os.scandir(root / folder) as entries:
                    folders[folder] = {entry.name for entry in entries}
            except OSError:
                folders[folder] = set()
        names = folders[folder]
        stem = name.rsplit(".", 1)[0] if "." in name else name
        if stem + SYNCED_EXT in names:
            state = "synced-file"
        elif stem + PLAIN_EXT in names:
            state = "plain-file"
        elif relative in embedded:
            state = "embedded"
        else:
            state = {"instrumental": "instrumental",
                     "notfound": "not-found",
                     "error": "error"}.get(looked_up.get(relative),
                                           "unchecked")
        states[relative] = state
    totals = {}
    for state in states.values():
        totals[state] = totals.get(state, 0) + 1
    return {"tracks": states, "totals": totals}


def due(connection: sqlite3.Connection, root: Path, only_new=False,
        cover=None) -> list:
    """Tracks to look up: no lyrics yet, and not tried recently.

    cover is a coverage() result to reuse, when the caller has one.
    """
    now = datetime.now(timezone.utc)
    retry = {
        "notfound": (now - timedelta(days=NOT_FOUND_RETRY_DAYS)).isoformat(),
        "error": (now - timedelta(days=ERROR_RETRY_DAYS)).isoformat(),
    }
    checked = {row["path"]: (row["status"], row["checked_at"])
               for row in connection.execute(
                   "SELECT path, status, checked_at FROM lyrics")}
    cover = cover or coverage(connection, root)
    result = []
    for path, state in sorted(cover["tracks"].items()):
        if state not in ("unchecked", "not-found", "error"):
            continue
        if path in checked:
            if only_new:
                continue
            status, when = checked[path]
            # A written file that was deleted since: a manual fill
            # fetches it again (the automatic fill above leaves it).
            if status in ("synced", "plain"):
                result.append(path)
                continue
            if status not in retry or when >= retry[status]:
                continue
        result.append(path)
    return result


def fill(connection: sqlite3.Connection, root: Path, paths, client=None,
         stop=None, progress=lambda done, total, found: None) -> dict:
    """Look up each path on LRCLIB and write sidecar files.

    Stops early when stop (a threading.Event) is set, or on an LRCLIB error
    that retrying will not fix. Returns counts and the stop reason.
    """
    client = client or Lrclib()
    counts = {"synced": 0, "plain": 0, "instrumental": 0, "notfound": 0,
              "error": 0, "skipped": 0}
    reason = ""
    failures_in_a_row = 0
    for done, relative in enumerate(paths, 1):
        if stop is not None and stop.is_set():
            reason = "stopped"
            break
        progress(done, len(paths), counts["synced"] + counts["plain"])
        path = root / relative
        synced_path, plain_path = sidecars(path)
        if not path.is_file() or synced_path.exists() or plain_path.exists():
            counts["skipped"] += 1
            continue
        status, written, digest, record_id = "notfound", None, None, None
        try:
            record = lookup(client, track_info(path))
            failures_in_a_row = 0
        except ConnectionError as error:
            failures_in_a_row += 1
            status = "error"
            record = None
            if failures_in_a_row >= 5:
                reason = f"LRCLIB unreachable: {error}"
        except LyricsError as error:
            reason = str(error)
            break
        if record:
            record_id = record.get("id")
            try:
                if record.get("instrumental"):
                    status = "instrumental"
                elif record.get("syncedLyrics"):
                    digest = write_new(synced_path, record["syncedLyrics"])
                    status, written = "synced", synced_path
                elif record.get("plainLyrics"):
                    digest = write_new(plain_path, record["plainLyrics"])
                    status, written = "plain", plain_path
            except FileExistsError:
                counts["skipped"] += 1
                continue
            except OSError:
                status = "error"
        counts[status] += 1
        connection.execute(
            "INSERT OR REPLACE INTO lyrics (path, status, lrclib_id, "
            "written_file, written_sha256, checked_at) VALUES "
            "(?, ?, ?, ?, ?, ?)",
            (relative, status, record_id,
             written.relative_to(root).as_posix() if written else None,
             digest, store.now_iso()))
        if reason:
            break
    counts["reason"] = reason
    return counts


def remove_written(connection: sqlite3.Connection, root: Path) -> dict:
    """Delete sidecar files music-librarian wrote, if still unchanged."""
    removed = kept = 0
    rows = connection.execute(
        "SELECT path, written_file, written_sha256 FROM lyrics WHERE "
        "written_file IS NOT NULL").fetchall()
    for row in rows:
        target = root / row["written_file"]
        try:
            resolved = target.resolve()
            inside = root.resolve() in resolved.parents
            same = inside and target.is_file() and not target.is_symlink() \
                and hashlib.sha256(target.read_bytes()).hexdigest() \
                == row["written_sha256"]
        except OSError:
            same = False
        if same:
            target.unlink()
            removed += 1
            connection.execute("DELETE FROM lyrics WHERE path = ?",
                               (row["path"],))
        elif target.exists():
            kept += 1
        else:
            connection.execute("DELETE FROM lyrics WHERE path = ?",
                               (row["path"],))
    return {"removed": removed, "kept": kept}
