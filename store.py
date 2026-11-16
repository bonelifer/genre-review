"""SQLite state for music-librarian.

Holds the album list built by the library scan, each track's genres (and
the genres it had before music-librarian first wrote it, for Revert), the
Last.fm suggestions, and a cache of Last.fm responses.

Album statuses:
    pending  waiting for review
    applied  reviewed; genres written to the files
    skipped  passed over for now (shown again under "Skipped")
    ignored  "never ask again"
    changed  applied earlier, but the files' genres changed since
    missing  no tracks left in the library
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 5
STATUSES = ("pending", "applied", "skipped", "ignored", "changed", "missing")

SCHEMA = """
CREATE TABLE albums (
    id INTEGER PRIMARY KEY,
    album_key TEXT NOT NULL UNIQUE,
    albumartist TEXT NOT NULL,
    album TEXT NOT NULL,
    folder TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    current_genres TEXT NOT NULL DEFAULT '[]',
    suggestions TEXT,
    suggestions_error TEXT,
    suggested_at TEXT,
    applied_genres TEXT,
    applied_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX albums_status ON albums (status);
CREATE TABLE tracks (
    path TEXT PRIMARY KEY,
    album_id INTEGER NOT NULL REFERENCES albums (id),
    album_key TEXT NOT NULL,
    albumartist TEXT NOT NULL,
    album TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL,
    genres TEXT NOT NULL,
    original_genres TEXT
);
CREATE INDEX tracks_album ON tracks (album_id);
CREATE TABLE lastfm_cache (
    request TEXT PRIMARY KEY,
    response TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""

# Schema 2: tag inventory and cleanup.
#   tracks.fields  JSON {field: example value} of every tag field in the
#                  file; NULL means "read the file again on the next scan".
#   tag_rules      fields marked for removal.
#   removed_tags   the exact tag data taken out of each file, for Restore.
MIGRATION_2 = """
ALTER TABLE tracks ADD COLUMN fields TEXT;
CREATE TABLE tag_rules (
    field TEXT PRIMARY KEY,
    updated_at TEXT NOT NULL
);
CREATE TABLE removed_tags (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    field TEXT NOT NULL,
    data BLOB NOT NULL,
    removed_at TEXT NOT NULL,
    restored_at TEXT
);
CREATE INDEX removed_tags_open ON removed_tags (field, restored_at);
CREATE INDEX removed_tags_path ON removed_tags (path);
"""

# Schema 3: lyrics fill.
#   lyrics   one row per track that was looked up on LRCLIB: what was
#            found, when, and the sidecar file written (with its SHA-256,
#            so "remove written files" never deletes a file edited since).
MIGRATION_3 = """
CREATE TABLE lyrics (
    path TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    lrclib_id INTEGER,
    written_file TEXT,
    written_sha256 TEXT,
    checked_at TEXT NOT NULL
);
CREATE INDEX lyrics_status ON lyrics (status);
"""

# Schema 4: automatic tag cleanup and the ID3v1 field.
#   settings  small key/value choices made on the pages (auto_clean).
#   Every track's field list is cleared so the next scan reads the files
#   again and records the new ID3v1 field.
MIGRATION_4 = """
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
UPDATE tracks SET fields = NULL
"""

# Schema 5: Deezer as a second lyrics source.
#   lyrics.source             where the written file came from: 'lrclib' or
#                             'deezer' (existing written files are LRCLIB's)
#   lyrics.deezer_status      the last Deezer answer: synced, plain (no
#                             synced version), none (no lyrics), nomatch
#                             (track not found) or error
#   lyrics.deezer_id          the matched Deezer track
#   lyrics.deezer_checked_at  when Deezer was last asked (UTC)
MIGRATION_5 = """
ALTER TABLE lyrics ADD COLUMN source TEXT;
ALTER TABLE lyrics ADD COLUMN deezer_status TEXT;
ALTER TABLE lyrics ADD COLUMN deezer_id INTEGER;
ALTER TABLE lyrics ADD COLUMN deezer_checked_at TEXT;
UPDATE lyrics SET source = 'lrclib' WHERE written_file IS NOT NULL
"""


def now_iso() -> str:
    """Current UTC time as an ISO 8601 string (storage format)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def display_time(moment: datetime) -> str:
    """A stored UTC time shown in local time: 'MM/DD/YYYY h:mm AM CST'.

    The local zone comes from the TZ environment variable (UTC when it is
    not set). Times without a zone are taken as UTC, the storage zone.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone()
    hour = local.hour % 12 or 12
    zone = local.tzname() or "UTC"
    return (f"{local:%m/%d/%Y} {hour}:{local:%M} "
            f"{'AM' if local.hour < 12 else 'PM'} {zone}")


def now_display() -> str:
    """Current time in the display format, for status lines."""
    return display_time(datetime.now(timezone.utc))


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating or migrating) the state database at path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise SystemExit(
            f"{path} was written by a newer music-librarian "
            f"(schema {version}, this version knows {SCHEMA_VERSION})."
        )
    for target, script in ((1, SCHEMA), (2, MIGRATION_2), (3, MIGRATION_3),
                           (4, MIGRATION_4), (5, MIGRATION_5)):
        if version < target:
            with transaction(connection):
                # Another process may have migrated while we waited.
                now = connection.execute("PRAGMA user_version").fetchone()[0]
                if now < target:
                    for statement in script.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(f"PRAGMA user_version={target}")
            version = target
    return connection


class transaction:
    """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def __enter__(self):
        self.connection.execute("BEGIN IMMEDIATE")
        return self.connection

    def __exit__(self, kind, value, traceback):
        self.connection.execute("ROLLBACK" if kind else "COMMIT")
        return False


def loads(text, default):
    """Decode a JSON column, returning default for NULL."""
    return default if text is None else json.loads(text)


def dumps(value) -> str:
    """Encode a JSON column."""
    return json.dumps(value, ensure_ascii=False)


def album_counts(connection: sqlite3.Connection) -> dict:
    """Number of albums per status (every status present, zero if none)."""
    counts = dict.fromkeys(STATUSES, 0)
    for row in connection.execute(
        "SELECT status, COUNT(*) FROM albums GROUP BY status"
    ):
        counts[row[0]] = row[1]
    return counts
