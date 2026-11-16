#!/usr/bin/env python3
"""genre-review: review Last.fm genre suggestions per album, then write them.

Commands:
    serve    run the review web page, rescanning the library on a timer
    scan     scan the library once and print what changed
    suggest  fetch missing Last.fm suggestions once

Settings come from environment variables (see .env.example); command-line
options override them. With no command, the help text is shown.
"""

import argparse
import os
import sqlite3
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import lastfm
import library
import lyrics
import mpd
import store
import suggest
import tags


@dataclass
class Settings:
    """Runtime settings, from the environment and the command line."""

    library_dir: Path
    state_dir: Path
    lastfm_api_key: str
    listen: str
    port: int
    scan_interval_minutes: int
    max_genres: int
    bulk_min_weight: int
    mpd_host: str
    mpd_port: int
    mpd_password: str
    mpd_path_prefix: str
    lyrics_auto_fill: bool = False
    lyrics_auto_limit: int = 300

    @property
    def db_path(self) -> Path:
        """Location of the SQLite state database."""
        return self.state_dir / "genre-review.db"


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read an integer setting, exiting with a clear message if invalid."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a whole number, got {raw!r}")
    if not minimum <= value <= maximum:
        raise SystemExit(f"{name} must be between {minimum} and {maximum}")
    return value


def env_flag(name: str) -> bool:
    """Read a yes/no setting (1/0, true/false, yes/no, on/off)."""
    raw = os.environ.get(name, "").strip().lower()
    if raw in ("", "0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    raise SystemExit(f"{name} must be 1 or 0, got {raw!r}")


def load_settings(args) -> Settings:
    """Combine environment variables and command-line options."""
    library_dir = args.library or os.environ.get("LIBRARY_DIR", "")
    if not library_dir:
        raise SystemExit("Set LIBRARY_DIR (or pass --library): the music "
                         "library folder to review.")
    library_path = Path(library_dir).expanduser()
    if not library_path.is_dir():
        raise SystemExit(f"LIBRARY_DIR is not a folder: {library_path}")
    return Settings(
        library_dir=library_path,
        state_dir=Path(args.state or os.environ.get("STATE_DIR", "state"))
        .expanduser(),
        lastfm_api_key=os.environ.get("LASTFM_API_KEY", "").strip(),
        listen=args.listen or os.environ.get("LISTEN", "127.0.0.1"),
        port=args.port or env_int("PORT", 18771, 1, 65535),
        scan_interval_minutes=env_int("SCAN_INTERVAL_MINUTES", 60, 0, 10080),
        max_genres=env_int("MAX_GENRES", 3, 1, 10),
        bulk_min_weight=env_int("BULK_MIN_WEIGHT", 60, 1, 100),
        mpd_host=os.environ.get("MPD_HOST", "127.0.0.1"),
        mpd_port=env_int("MPD_PORT", 6600, 1, 65535),
        mpd_password=os.environ.get("MPD_PASSWORD", ""),
        mpd_path_prefix=os.environ.get("MPD_PATH_PREFIX", "").strip("/"),
        lyrics_auto_fill=env_flag("LYRICS_AUTO_FILL"),
        lyrics_auto_limit=env_int("LYRICS_AUTO_LIMIT", 300, 1, 100000),
    )


class Reviewer:
    """Shared operations for the web page and the one-shot commands."""

    def __init__(self, settings: Settings, log=print):
        self.settings = settings
        self.log = log
        self.whitelist = suggest.load_whitelist()
        # Scans and tag writes never run at the same time.
        self.library_lock = threading.Lock()
        self.status = {"scan": "not run yet", "suggest": "not run yet",
                       "tags": "nothing removed or restored yet",
                       "lyrics": "not run yet"}
        self.tag_job = None
        self.lyrics_job = None
        self.lyrics_stop = threading.Event()
        # One fill at a time, whether started from the page or the timer.
        self.lyrics_lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        """New connection to the state database (one per thread)."""
        return store.connect(self.settings.db_path)

    def scan(self) -> dict:
        """Scan the library under the library lock."""
        with self.library_lock:
            self.status["scan"] = "running"
            connection = self.connect()
            try:
                result = library.scan(connection, self.settings.library_dir,
                                      log=self.log)
            finally:
                connection.close()
        self.status["scan"] = (
            f"{store.now_display()}: {result['files']} files, "
            f"{result['albums']} albums, {result['new_albums']} new, "
            f"{result['errors']} unreadable")
        return result

    def suggest_missing(self, limit: int = 0) -> int:
        """Fetch suggestions for albums that have none; returns how many."""
        if not self.settings.lastfm_api_key:
            self.status["suggest"] = "LASTFM_API_KEY is not set"
            return 0
        connection = self.connect()
        done = 0
        try:
            client = lastfm.Lastfm(self.settings.lastfm_api_key, connection)
            # Missing suggestions, and ones older than the Last.fm cache
            # lifetime (so albums Last.fm knew nothing about get retried).
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(days=lastfm.CACHE_DAYS)).isoformat()
            query = ("SELECT id, albumartist, album, current_genres FROM "
                     "albums WHERE status IN ('pending', 'changed') AND "
                     "(suggestions IS NULL OR suggested_at < ?) ORDER BY id")
            rows = connection.execute(query, (cutoff,)).fetchall()
            for row in rows[:limit or None]:
                self.status["suggest"] = (
                    f"running: {done} of {len(rows)} albums")
                try:
                    found = self.suggestions_for(client, row)
                    error = None
                except lastfm.LastfmError as failure:
                    message = str(failure)
                    if "error 10" in message or "error 26" in message:
                        self.status["suggest"] = f"stopped: {message}"
                        return done
                    found, error = None, message
                connection.execute(
                    "UPDATE albums SET suggestions = ?, suggestions_error = ?, "
                    "suggested_at = ? WHERE id = ?",
                    (None if found is None else store.dumps(found), error,
                     store.now_iso(), row["id"]))
                done += 1
        finally:
            connection.close()
        self.status["suggest"] = f"{store.now_display()}: {done} albums looked up"
        return done

    def suggestions_for(self, client, row) -> list:
        """Album tags first; artist tags fill in when the album has few."""
        artist, album = row["albumartist"], row["album"]
        album_tags = client.album_tags(artist, album) if artist else []
        whitelisted = [name for name, _ in album_tags
                       if suggest.lookup_key(name) in self.whitelist]
        artist_tags = client.artist_tags(artist) \
            if artist and len(whitelisted) < 2 else []
        return suggest.build_suggestions(
            album_tags, artist_tags, store.loads(row["current_genres"], []),
            self.whitelist)

    def apply(self, album_id: int, genres: list) -> dict:
        """Write genres to an album's files, then ask MPD to rescan it."""
        return self._write(album_id, lambda connection: library.apply_album(
            connection, self.settings.library_dir, album_id, genres))

    def revert(self, album_id: int) -> dict:
        """Restore an album's genres from before genre-review wrote them."""
        return self._write(album_id, lambda connection: library.revert_album(
            connection, self.settings.library_dir, album_id))

    def _write(self, album_id: int, action) -> dict:
        """Run a tag-writing action under the library lock, then MPD update."""
        if not self.library_lock.acquire(timeout=300):
            return {"written": 0, "errors": ["A library scan is still "
                                             "running; try again shortly."]}
        try:
            connection = self.connect()
            try:
                result = action(connection)
                folder = connection.execute(
                    "SELECT folder FROM albums WHERE id = ?",
                    (album_id,)).fetchone()["folder"]
            finally:
                connection.close()
        finally:
            self.library_lock.release()
        result["mpd"] = self.update_mpd(folder) if result["written"] else ""
        return result

    def update_mpd(self, folder: str) -> str:
        """Ask MPD to rescan folder; returns '' or a short error message."""
        target = "/".join(part for part in (self.settings.mpd_path_prefix,
                                            folder) if part)
        try:
            mpd.update(self.settings.mpd_host, self.settings.mpd_port,
                       target, self.settings.mpd_password)
        except mpd.MpdError as error:
            return f"MPD update failed: {error}"
        return ""

    # --- tag cleanup -------------------------------------------------------

    def start_tag_job(self, kind: str, field=None) -> str:
        """Start removing rule fields ("apply") or restoring ("restore").

        Runs in a background thread under the library lock; returns a
        message for the page. Only one tag job runs at a time.
        """
        if self.tag_job is not None and self.tag_job.is_alive():
            return "A tag job is already running; see its progress below."
        self.tag_job = threading.Thread(
            target=self._run_tag_job, args=(kind, field), daemon=True)
        self.tag_job.start()
        return ("Removing tags in the background." if kind == "apply"
                else "Restoring tags in the background.")

    def _run_tag_job(self, kind: str, field) -> None:
        """Body of start_tag_job's thread."""
        verb = "Removing" if kind == "apply" else "Restoring"

        def progress(done, total):
            self.status["tags"] = f"{verb}: file {done} of {total}"

        self.status["tags"] = f"{verb}: waiting for the library scan"
        try:
            with self.library_lock:
                connection = self.connect()
                try:
                    if kind == "apply":
                        result = tags.apply_rules(
                            connection, self.settings.library_dir, progress)
                    else:
                        result = tags.restore(
                            connection, self.settings.library_dir,
                            field=field, progress=progress)
                    folders = [row[0] for row in connection.execute(
                        "SELECT folder FROM albums WHERE id IN (%s)"
                        % ",".join("?" * len(result["albums"])),
                        result["albums"])] if result["albums"] else []
                finally:
                    connection.close()
        except Exception as error:  # shown on the Tags page
            self.status["tags"] = f"{verb} failed: {error}"
            return
        mpd_note = self.update_mpd_folders(folders)
        self.warm_tag_cache()
        done = "Removed tags from" if kind == "apply" else "Restored tags in"
        message = (f"{store.now_display()}: {done} {result['files']} files in "
                   f"{len(result['albums'])} albums.")
        if result["errors"]:
            message += (f" {len(result['errors'])} files failed: "
                        + "; ".join(result["errors"][:3]))
        if mpd_note:
            message += " " + mpd_note
        self.status["tags"] = message

    def warm_tag_cache(self) -> None:
        """Prepare the Tags page's inventory in the background thread."""
        connection = self.connect()
        try:
            tags.warm_cache(connection)
        except Exception as error:  # the page computes it on demand
            self.log(f"could not prepare the tag inventory: {error}")
        finally:
            connection.close()

    def restore_album_tags(self, album_id: int) -> dict:
        """Restore every removed tag of one album (runs in the request)."""
        if not self.library_lock.acquire(timeout=300):
            return {"files": 0, "albums": [], "errors": [
                "A library scan or tag job is running; try again shortly."]}
        try:
            connection = self.connect()
            try:
                result = tags.restore(connection, self.settings.library_dir,
                                      album_id=album_id)
                row = connection.execute(
                    "SELECT folder FROM albums WHERE id = ?",
                    (album_id,)).fetchone()
            finally:
                connection.close()
        finally:
            self.library_lock.release()
        result["mpd"] = self.update_mpd_folders([row[0]] if row and
                                                result["files"] else [])
        return result

    def update_mpd_folders(self, folders) -> str:
        """Ask MPD to rescan folders (the whole library if there are many)."""
        if not folders:
            return ""
        if len(folders) > 20:
            return self.update_mpd("")
        for folder in folders:
            note = self.update_mpd(folder)
            if note:
                return note
        return ""

    # --- lyrics fill -------------------------------------------------------

    def start_lyrics_job(self, limit: int = 0, only_new: bool = False) -> str:
        """Start filling missing lyrics from LRCLIB in the background."""
        if self.lyrics_lock.locked():
            return "The lyrics fill is already running."
        self.lyrics_stop.clear()
        self.lyrics_job = threading.Thread(
            target=self.fill_lyrics, args=(limit, only_new), daemon=True)
        self.lyrics_job.start()
        return "Filling lyrics in the background."

    def stop_lyrics_job(self) -> str:
        """Ask a running lyrics fill to stop after the current track."""
        if not self.lyrics_lock.locked():
            return "The lyrics fill is not running."
        self.lyrics_stop.set()
        return "Stopping the lyrics fill after the current track."

    def fill_lyrics(self, limit: int = 0, only_new: bool = False) -> dict:
        """Look up missing lyrics (at most limit tracks; 0 = all due)."""
        if not self.lyrics_lock.acquire(blocking=False):
            return {}
        connection = self.connect()
        try:
            paths = lyrics.due(connection, self.settings.library_dir,
                               only_new=only_new)
            if limit:
                paths = paths[:limit]

            def progress(done, total, found):
                self.status["lyrics"] = (f"running: track {done} of {total},"
                                         f" {found} lyrics files written")

            result = lyrics.fill(connection, self.settings.library_dir,
                                 paths, stop=self.lyrics_stop,
                                 progress=progress)
        except Exception as error:  # shown on the Lyrics page
            self.status["lyrics"] = f"failed: {error}"
            return {}
        finally:
            connection.close()
            self.lyrics_lock.release()
        message = (f"{store.now_display()}: {result['synced']} synced and "
                   f"{result['plain']} plain lyrics files written, "
                   f"{result['instrumental']} instrumental, "
                   f"{result['notfound']} not on LRCLIB, "
                   f"{result['error']} errors")
        if result["reason"]:
            message += f" ({result['reason']})"
        self.status["lyrics"] = message
        return result

    def remove_lyrics_files(self) -> dict:
        """Delete lyrics files genre-review wrote (unchanged ones only)."""
        if self.lyrics_lock.locked():
            return {"removed": 0, "kept": 0,
                    "error": "Stop the lyrics fill first."}
        connection = self.connect()
        try:
            return lyrics.remove_written(connection,
                                         self.settings.library_dir)
        finally:
            connection.close()

    def background_loop(self, stop: threading.Event) -> None:
        """Scan, then suggest, every scan_interval_minutes (0 = once)."""
        while not stop.is_set():
            try:
                self.scan()
                self.suggest_missing()
                self.warm_tag_cache()
                if self.settings.lyrics_auto_fill:
                    self.fill_lyrics(self.settings.lyrics_auto_limit,
                                     only_new=True)
            except Exception as error:  # keep the web page alive
                self.log(f"background pass failed: {error}")
                self.status["scan"] = f"failed: {error}"
            minutes = self.settings.scan_interval_minutes
            if not minutes:
                return
            stop.wait(minutes * 60)


def build_parser() -> argparse.ArgumentParser:
    """Command-line interface."""
    parser = argparse.ArgumentParser(
        description="Review Last.fm genre suggestions per album and write "
                    "the ones you approve into the files.")
    parser.add_argument("-L", "--library", help="music library folder "
                        "(default: LIBRARY_DIR)")
    parser.add_argument("-S", "--state", help="folder for the SQLite state "
                        "database (default: STATE_DIR or ./state)")
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="run the review web page")
    serve.add_argument("-l", "--listen", help="address to listen on "
                       "(default: LISTEN or 127.0.0.1)")
    serve.add_argument("-p", "--port", type=int, help="port to listen on "
                       "(default: PORT or 18771)")
    commands.add_parser("scan", help="scan the library once")
    suggest_command = commands.add_parser(
        "suggest", help="fetch missing Last.fm suggestions once")
    suggest_command.add_argument("-n", "--limit", type=int, default=0,
                                 help="look up at most this many albums")
    return parser


def main(argv=None) -> int:
    """Entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    for name in ("listen", "port"):
        if not hasattr(args, name):
            setattr(args, name, None)
    reviewer = Reviewer(load_settings(args))
    if args.command == "scan":
        result = reviewer.scan()
        print(f"{result['files']} files in {result['albums']} albums "
              f"({result['new_albums']} new, {result['read']} read, "
              f"{result['errors']} unreadable).")
        return 1 if result["errors"] else 0
    if args.command == "suggest":
        count = reviewer.suggest_missing(args.limit)
        print(f"Looked up {count} albums. {reviewer.status['suggest']}")
        return 0
    import web
    stop = threading.Event()
    worker = threading.Thread(target=reviewer.background_loop, args=(stop,),
                              daemon=True)
    worker.start()
    try:
        web.serve(reviewer)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
