"""Tag inventory and cleanup: list every tag field, remove chosen ones, undo.

A "field" is an ID3 frame ID (TPUB), or for frames that can repeat with a
description, the ID plus that description (TXXX:BARCODE, COMM:iTunNORM).
Other formats report their own key names (FLAC/Ogg comments, MP4 atoms)
but only MP3 files are ever changed.

Removed frames are kept byte for byte: they are written into a small,
self-contained ID3 block and stored in SQLite, so Restore puts back exactly
what was taken out.
"""

import io
import re
import sqlite3
import threading
from pathlib import Path

import mutagen
from mutagen.id3 import ID3, Frames, ID3NoHeaderError

import store

# Frame IDs whose description is part of the field name.
DESCRIBED = {"TXXX", "WXXX", "COMM", "USLT", "SYLT", "GEOB", "APIC"}
OWNED = {"PRIV", "UFID"}

# Fields that can never be removed: what identifies, plays or shows a
# track, plus everything music-librarian itself writes.
PROTECTED_IDS = {
    "TIT2", "TPE1", "TPE2", "TALB", "TRCK", "TPOS", "TDRC", "TYER", "TDAT",
    "TCON", "APIC",
}
PROTECTED_PATTERNS = (
    re.compile(r"^APIC(:|$)"),
    re.compile(r"^TXXX:replaygain_", re.IGNORECASE),
    re.compile(r"^TXXX:MusicBrainz ", re.IGNORECASE),
    re.compile(r"^UFID:http://musicbrainz\.org$"),
)
EXAMPLE_LENGTH = 60

# The inventory unpacks every track's field list, which takes seconds on a
# large library, so results are kept until the data behind them changes.
_cache = {}
_cache_lock = threading.Lock()


class TagError(Exception):
    """A file's tags could not be read or written."""


def field_key(frame) -> str:
    """Field name for one ID3 frame."""
    frame_id = frame.FrameID
    if frame_id in DESCRIBED:
        return f"{frame_id}:{frame.desc}"
    if frame_id in OWNED:
        return f"{frame_id}:{frame.owner}"
    return frame_id


def is_protected(field: str) -> bool:
    """True for fields the cleanup must never remove."""
    if field.split(":", 1)[0] in PROTECTED_IDS and ":" not in field:
        return True
    return any(pattern.search(field) for pattern in PROTECTED_PATTERNS)


def describe(field: str) -> str:
    """Plain-words name: 'Publisher (TPUB)', 'BARCODE (custom text)'."""
    frame_id, _, detail = field.partition(":")
    if frame_id == "TXXX":
        return f"{detail or '(no name)'} (custom text, TXXX)"
    if frame_id == "WXXX":
        return f"{detail or '(no name)'} (custom link, WXXX)"
    frame_class = Frames.get(frame_id)
    title = ""
    if frame_class is not None and frame_class.__doc__:
        title = frame_class.__doc__.strip().splitlines()[0].rstrip(".")
    if not title:
        return field
    if detail:
        return f"{title}: {detail} ({frame_id})"
    return f"{title} ({frame_id})"


def example(frame) -> str:
    """Short, printable example of a frame's value."""
    text = getattr(frame, "text", None)
    if text is not None and not isinstance(text, (bytes, bytearray)):
        value = " / ".join(str(item) for item in text) \
            if isinstance(text, list) else str(text)
    elif getattr(frame, "url", None):
        value = frame.url
    elif frame.FrameID in ("TIPL", "TMCL", "IPLS"):
        value = "; ".join(" ".join(pair) for pair in frame.people)
    else:
        value = "(binary data)"
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()
    if len(value) > EXAMPLE_LENGTH:
        value = value[:EXAMPLE_LENGTH - 1] + "…"
    return value


def read_fields(path: Path) -> dict:
    """{field: example} for every tag field in one file."""
    try:
        if path.suffix.lower() == ".mp3":
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                return {}
            fields = {}
            for frame in tags.values():
                fields.setdefault(field_key(frame), example(frame))
            return fields
        audio = mutagen.File(path)
    except Exception as error:  # mutagen raises many error types
        raise TagError(f"{path}: {error}") from error
    if audio is None or audio.tags is None:
        return {}
    fields = {}
    for key in audio.tags.keys():
        value = audio.tags.get(key)
        if isinstance(value, list) and value:
            value = value[0]
        text = value if isinstance(value, str) else (
            "(binary data)" if isinstance(value, (bytes, bytearray))
            else str(value))
        fields.setdefault(str(key), re.sub(r"[\x00-\x1f\x7f]", " ", text)
                          [:EXAMPLE_LENGTH])
    return fields


def pack_frames(frames) -> bytes:
    """Store frames in a minimal ID3v2.4 block (bytes)."""
    holder = ID3()
    for frame in frames:
        holder.add(frame)
    buffer = io.BytesIO()
    holder.save(buffer, v2_version=4, padding=lambda info: 0)
    return buffer.getvalue()


def unpack_frames(data: bytes) -> list:
    """Frames from a block written by pack_frames."""
    return list(ID3(io.BytesIO(data)).values())


def save_preserving_version(tags: ID3, path: Path) -> None:
    """Save ID3 tags, keeping a v2.3 file at v2.3."""
    version = 3 if tags.version[:2] == (2, 3) else 4
    tags.save(path, v2_version=version)


def remove_from_file(path: Path, fields) -> dict:
    """Take the given fields out of one MP3.

    Returns {field: packed frames} for the fields that were present; the
    caller stores these before calling write_removal() to commit them.
    """
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        return {}
    except Exception as error:
        raise TagError(f"{path}: {error}") from error
    found = {}
    for frame in tags.values():
        key = field_key(frame)
        if key in fields and not is_protected(key):
            found.setdefault(key, []).append(frame)
    return {key: pack_frames(frames) for key, frames in found.items()}


def write_removal(path: Path, fields) -> None:
    """Delete the given fields from one MP3 and save it."""
    try:
        tags = ID3(path)
        for hash_key, frame in list(tags.items()):
            key = field_key(frame)
            if key in fields and not is_protected(key):
                del tags[hash_key]
        save_preserving_version(tags, path)
    except Exception as error:
        raise TagError(f"{path}: {error}") from error


def write_restore(path: Path, data_blocks) -> None:
    """Add stored frames back to one MP3, replacing same-named frames."""
    try:
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        for data in data_blocks:
            for frame in unpack_frames(data):
                tags.delall(frame.HashKey)
                tags.add(frame)
        save_preserving_version(tags, path)
    except Exception as error:
        raise TagError(f"{path}: {error}") from error


# --- library-wide operations ---------------------------------------------

def inventory(connection: sqlite3.Connection) -> list:
    """Every field in the library: name, files, albums, examples, flags."""
    rows = connection.execute(
        "SELECT field.key AS field, COUNT(*) AS files, "
        "COUNT(DISTINCT tracks.album_id) AS albums, "
        "SUM(tracks.path LIKE '%.mp3' OR tracks.path LIKE '%.MP3') AS mp3 "
        "FROM tracks, json_each(tracks.fields) AS field "
        "WHERE tracks.fields IS NOT NULL GROUP BY field.key "
        "ORDER BY files DESC, field.key").fetchall()
    examples = {}
    for row in connection.execute(
            "SELECT key, value FROM (SELECT key, value, ROW_NUMBER() OVER "
            "(PARTITION BY key ORDER BY value) AS rank FROM (SELECT DISTINCT "
            "field.key AS key, field.value AS value FROM tracks, "
            "json_each(tracks.fields) AS field WHERE tracks.fields IS NOT "
            "NULL AND field.value != '')) WHERE rank <= 3"):
        examples.setdefault(row["key"], []).append(row["value"])
    rules = {row[0] for row in connection.execute(
        "SELECT field FROM tag_rules")}
    removed = dict(connection.execute(
        "SELECT field, COUNT(*) FROM removed_tags WHERE restored_at IS NULL "
        "GROUP BY field").fetchall())
    result = []
    for row in rows:
        field = row["field"]
        result.append({
            "field": field, "name": describe(field), "files": row["files"],
            "albums": row["albums"], "mp3": row["mp3"] or 0,
            "examples": examples.get(field, []),
            "protected": is_protected(field), "rule": field in rules,
            "removed": removed.pop(field, 0),
        })
    # Fields already removed everywhere still need a Restore button.
    for field, count in sorted(removed.items()):
        result.append({
            "field": field, "name": describe(field), "files": 0, "albums": 0,
            "mp3": 0, "examples": [], "protected": False,
            "rule": field in rules, "removed": count,
        })
    return result


def fingerprint(connection: sqlite3.Connection) -> tuple:
    """Cheap summary that changes whenever the inventory could change."""
    tracks = tuple(connection.execute(
        "SELECT COUNT(*), TOTAL(mtime_ns), TOTAL(size), "
        "SUM(fields IS NULL) FROM tracks").fetchone())
    removed = connection.execute(
        "SELECT COUNT(*), MAX(id), MAX(restored_at) FROM removed_tags"
    ).fetchone()
    rules = connection.execute(
        "SELECT group_concat(field, char(31)) FROM (SELECT field FROM "
        "tag_rules ORDER BY field)").fetchone()[0]
    return tracks + tuple(removed) + (rules,)


def _cached(name: str, key: tuple, compute):
    """Return compute() for key, reusing the last result while key holds."""
    with _cache_lock:
        entry = _cache.get(name)
        if entry is not None and entry[0] == key:
            return entry[1]
    value = compute()
    with _cache_lock:
        _cache[name] = (key, value)
    return value


def cached_inventory(connection: sqlite3.Connection) -> list:
    """inventory(), reused until the tracks, rules or removals change."""
    return _cached("inventory", fingerprint(connection),
                   lambda: inventory(connection))


def cached_affected(connection: sqlite3.Connection, fields) -> list:
    """affected(), reused until the data or the field set changes."""
    key = fingerprint(connection) + (tuple(sorted(fields)),)
    return _cached("affected", key, lambda: affected(connection, fields))


def warm_cache(connection: sqlite3.Connection) -> None:
    """Compute the inventory ahead of the next page view."""
    cached_inventory(connection)
    cached_affected(connection, rule_fields(connection))


def rule_fields(connection: sqlite3.Connection) -> set:
    """Fields currently marked for removal (protected ones never count)."""
    return {row[0] for row in connection.execute(
        "SELECT field FROM tag_rules") if not is_protected(row[0])}


def save_rules(connection: sqlite3.Connection, fields) -> None:
    """Replace the removal rules with fields (protected ones are refused)."""
    stamp = store.now_iso()
    with store.transaction(connection):
        connection.execute("DELETE FROM tag_rules")
        for field in sorted(set(fields)):
            if not is_protected(field):
                connection.execute(
                    "INSERT INTO tag_rules (field, updated_at) VALUES (?, ?)",
                    (field, stamp))


def affected(connection: sqlite3.Connection, fields) -> list:
    """MP3 tracks carrying any of fields: [(path, album_id)]."""
    if not fields:
        return []
    marks = ",".join("?" * len(fields))
    return [tuple(row) for row in connection.execute(
        "SELECT DISTINCT tracks.path, tracks.album_id FROM tracks, "
        "json_each(tracks.fields) AS field WHERE tracks.fields IS NOT NULL "
        f"AND field.key IN ({marks}) AND lower(tracks.path) LIKE '%.mp3' "
        "ORDER BY tracks.path", sorted(fields))]


def _safe_path(root: Path, relative: str) -> Path:
    """Resolve relative under root, refusing escapes and symlinks."""
    path = (root / relative).resolve()
    if root.resolve() not in path.parents or (root / relative).is_symlink():
        raise TagError(f"{relative}: outside the library")
    if not path.is_file():
        raise TagError(f"{relative}: file is gone")
    return path


def _refresh_track(connection, root: Path, relative: str) -> None:
    """Record a file's new size, time and fields after a write."""
    path = root / relative
    info = path.stat()
    connection.execute(
        "UPDATE tracks SET mtime_ns = ?, size = ?, fields = ? WHERE path = ?",
        (info.st_mtime_ns, info.st_size,
         store.dumps(read_fields(path)), relative))


def apply_rules(connection: sqlite3.Connection, root: Path,
                progress=lambda done, total: None) -> dict:
    """Remove every rule field from every MP3 that has it.

    The removed frames are stored before each file is written, so an
    interrupted run can still be restored. Returns {"files", "albums",
    "errors": [...]}.
    """
    fields = rule_fields(connection)
    targets = affected(connection, fields)
    files, albums, errors = 0, set(), []
    for done, (relative, album_id) in enumerate(targets, 1):
        progress(done, len(targets))
        try:
            path = _safe_path(root, relative)
            packed = remove_from_file(path, fields)
            if not packed:
                _refresh_track(connection, root, relative)
                continue
            stamp = store.now_iso()
            with store.transaction(connection):
                for field, data in packed.items():
                    connection.execute(
                        "INSERT INTO removed_tags (path, field, data, "
                        "removed_at) VALUES (?, ?, ?, ?)",
                        (relative, field, data, stamp))
            write_removal(path, set(packed))
            _refresh_track(connection, root, relative)
        except (TagError, OSError) as error:
            errors.append(str(error))
            continue
        files += 1
        albums.add(album_id)
    return {"files": files, "albums": sorted(albums), "errors": errors}


def restore(connection: sqlite3.Connection, root: Path, field=None,
            album_id=None, progress=lambda done, total: None) -> dict:
    """Put removed frames back, for one field or one album (or both)."""
    query = ("SELECT removed_tags.id, removed_tags.path, removed_tags.data, "
             "tracks.album_id FROM removed_tags LEFT JOIN tracks ON "
             "tracks.path = removed_tags.path WHERE restored_at IS NULL")
    params = []
    if field is not None:
        query += " AND removed_tags.field = ?"
        params.append(field)
    if album_id is not None:
        query += " AND tracks.album_id = ?"
        params.append(album_id)
    rows = connection.execute(query + " ORDER BY removed_tags.path, "
                              "removed_tags.id", params).fetchall()
    by_path = {}
    for row in rows:
        by_path.setdefault(row["path"], []).append(row)
    files, albums, errors = 0, set(), []
    for done, (relative, items) in enumerate(by_path.items(), 1):
        progress(done, len(by_path))
        try:
            path = _safe_path(root, relative)
            write_restore(path, [item["data"] for item in items])
            stamp = store.now_iso()
            with store.transaction(connection):
                for item in items:
                    connection.execute(
                        "UPDATE removed_tags SET restored_at = ? WHERE id = ?",
                        (stamp, item["id"]))
            _refresh_track(connection, root, relative)
        except (TagError, OSError) as error:
            errors.append(str(error))
            continue
        files += 1
        if items[0]["album_id"] is not None:
            albums.add(items[0]["album_id"])
    return {"files": files, "albums": sorted(albums), "errors": errors}


def albums_with(connection: sqlite3.Connection, field: str) -> list:
    """Albums carrying field: rows of id, albumartist, album, tracks."""
    return connection.execute(
        "SELECT albums.id, albums.albumartist, albums.album, "
        "COUNT(DISTINCT tracks.path) AS tracks FROM tracks JOIN albums ON "
        "albums.id = tracks.album_id, json_each(tracks.fields) AS item "
        "WHERE tracks.fields IS NOT NULL AND item.key = ? GROUP BY albums.id "
        "ORDER BY albums.albumartist COLLATE NOCASE, albums.album COLLATE "
        "NOCASE", (field,)).fetchall()


def removed_for_album(connection: sqlite3.Connection, album_id: int) -> list:
    """[(field, files)] removed from an album and not yet restored."""
    return [tuple(row) for row in connection.execute(
        "SELECT removed_tags.field, COUNT(DISTINCT removed_tags.path) FROM "
        "removed_tags JOIN tracks ON tracks.path = removed_tags.path WHERE "
        "tracks.album_id = ? AND restored_at IS NULL GROUP BY "
        "removed_tags.field ORDER BY removed_tags.field", (album_id,))]
