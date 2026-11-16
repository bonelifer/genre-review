"""Scan the music library into albums, and write or revert album genres.

Albums are grouped by album artist (falling back to the first track
artist) and album tag. Files without an album tag are grouped by folder.
Only files whose size or modification time changed since the last scan
are read again, so routine rescans are cheap.

Writing touches only the genre tag. MP3s are saved as ID3v2.4 so several
genres are stored as separate values; other formats store one genre
field per value.
"""

import os
import re
import sqlite3
from pathlib import Path

import mutagen

import store
import tags as tag_fields

AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a"}
SEPARATOR = "\x1f"
DISC_FOLDER = re.compile(r"(cd|disc|disk)\s*\d+", re.IGNORECASE)
TRAILING_YEAR = re.compile(r"\s*\(\d{4}\)$")


class TagError(Exception):
    """A file could not be read or written."""


def first(tags, name: str) -> str:
    """First non-empty value of an easy tag, or ''."""
    for value in tags.get(name, []) if tags else []:
        if str(value).strip():
            return str(value).strip()
    return ""


def read_track(path: Path) -> dict:
    """Read album artist, album and genres from one audio file."""
    try:
        audio = mutagen.File(path, easy=True)
    except Exception as error:  # mutagen raises many error types
        raise TagError(f"{path}: {error}") from error
    if audio is None:
        raise TagError(f"{path}: not a supported audio file")
    tags = audio.tags
    genres = []
    for value in tags.get("genre", []) if tags else []:
        value = str(value).strip()
        if value and value not in genres:
            genres.append(value)
    return {
        "albumartist": first(tags, "albumartist") or first(tags, "artist"),
        "album": first(tags, "album"),
        "genres": genres,
    }


def fill_from_folders(track: dict, folder: Path) -> None:
    """Name an untagged track's album and artist after its folders.

    'Artist/Album (1979)/CD 1/01.mp3' becomes album 'Album', artist
    'Artist' (an existing artist tag wins), grouped by 'Artist/Album (1979)'.
    """
    if DISC_FOLDER.fullmatch(folder.name):
        folder = folder.parent
    track["folder"] = folder.as_posix()
    track["album"] = TRAILING_YEAR.sub("", folder.name) or folder.name \
        or "(no album)"
    if not track["albumartist"]:
        track["albumartist"] = folder.parent.name


def album_key(albumartist: str, album: str, folder: str) -> str:
    """Grouping key: artist + album, or the folder when there is no album."""
    if not album:
        return "folder:" + folder
    return albumartist.casefold() + SEPARATOR + album.casefold()


def audio_files(root: Path, walk_errors=None):
    """Yield (relative path, stat) for every audio file under root.

    Folders that cannot be listed are appended to walk_errors (when given),
    so the caller knows the file list is incomplete.
    """
    def failed(error):
        if walk_errors is not None:
            walk_errors.append(error)

    for directory, folders, files in os.walk(root, onerror=failed):
        folders.sort()
        for name in sorted(files):
            if Path(name).suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            path = Path(directory) / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                info = path.stat()
            except OSError as error:
                # Unknown state, not a deletion: keep its row this scan.
                failed(error)
                continue
            yield path.relative_to(root).as_posix(), info


def cached_track(row) -> dict:
    """A scan entry built from a stored tracks row."""
    return {
        "album_key": row["album_key"], "albumartist": row["albumartist"],
        "album": row["album"], "genres": store.loads(row["genres"], []),
        "mtime_ns": row["mtime_ns"], "size": row["size"],
        "fields": store.loads(row["fields"], None),
    }


def common_folder(paths) -> str:
    """Deepest folder shared by all relative paths ('' for the root)."""
    parents = [Path(path).parent.parts for path in paths]
    shared = []
    for parts in zip(*parents):
        if len(set(parts)) != 1:
            break
        shared.append(parts[0])
    return "/".join(shared)


def scan(connection: sqlite3.Connection, root: Path, log=print) -> dict:
    """Bring the albums and tracks tables in line with the files on disk.

    Returns counts: files, read, errors, albums, new_albums.
    """
    known = {row["path"]: row for row in connection.execute(
        "SELECT path, album_key, albumartist, album, mtime_ns, size, genres, "
        "fields FROM tracks")}
    seen = {}
    read = errors = 0
    walk_errors = []
    for relative, info in audio_files(root, walk_errors):
        old = known.get(relative)
        # Folder-named (untagged) tracks are always re-read, so naming
        # changes reach them; they are rare.
        # Tracks without a tag inventory yet (fields NULL) are read again.
        if old and old["mtime_ns"] == info.st_mtime_ns \
                and old["size"] == info.st_size \
                and old["fields"] is not None \
                and not old["album_key"].startswith("folder:"):
            seen[relative] = cached_track(old)
            continue
        try:
            track = read_track(root / relative)
            track["fields"] = tag_fields.read_fields(root / relative)
        except (TagError, tag_fields.TagError) as error:
            errors += 1
            log(f"skipping {error}")
            if old:
                # Keep the known row (and its Revert backup); the old
                # mtime makes the next scan try to read it again.
                seen[relative] = cached_track(old)
            continue
        read += 1
        if not track["album"]:
            fill_from_folders(track, Path(relative).parent)
        track["album_key"] = album_key(track["albumartist"], track["album"],
                                       track.pop("folder", ""))
        track.update(mtime_ns=info.st_mtime_ns, size=info.st_size)
        seen[relative] = track

    groups = {}
    for relative, track in seen.items():
        groups.setdefault(track["album_key"], []).append(relative)

    new_albums = 0
    stamp = store.now_iso()
    with store.transaction(connection):
        albums = {row["album_key"]: row for row in connection.execute(
            "SELECT id, album_key, status, applied_genres FROM albums")}
        for key, paths in groups.items():
            paths.sort()
            sample = seen[paths[0]]
            current = []
            for relative in paths:
                for genre in seen[relative]["genres"]:
                    if genre not in current:
                        current.append(genre)
            row = albums.get(key)
            if row is None:
                album_id = connection.execute(
                    "INSERT INTO albums (album_key, albumartist, album, "
                    "folder, current_genres, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (key, sample["albumartist"], sample["album"],
                     common_folder(paths), store.dumps(current), stamp),
                ).lastrowid
                new_albums += 1
            else:
                album_id = row["id"]
                status = row["status"]
                applied = store.loads(row["applied_genres"], None)
                if status == "missing":
                    status = "pending"
                if status in ("applied", "changed") and applied is not None:
                    # Every track must carry exactly the approved genres.
                    matches = all(set(seen[path]["genres"]) == set(applied)
                                  for path in paths)
                    status = "applied" if matches else "changed"
                connection.execute(
                    "UPDATE albums SET albumartist = ?, album = ?, "
                    "folder = ?, current_genres = ?, status = ?, "
                    "updated_at = ? WHERE id = ?",
                    (sample["albumartist"], sample["album"],
                     common_folder(paths), store.dumps(current), status,
                     stamp, album_id),
                )
            for relative in paths:
                track = seen[relative]
                connection.execute(
                    "INSERT INTO tracks (path, album_id, album_key, "
                    "albumartist, album, mtime_ns, size, genres, fields) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (path) DO UPDATE SET album_id = excluded."
                    "album_id, album_key = excluded.album_key, albumartist = "
                    "excluded.albumartist, album = excluded.album, mtime_ns = "
                    "excluded.mtime_ns, size = excluded.size, genres = "
                    "excluded.genres, fields = excluded.fields",
                    (relative, album_id, key, track["albumartist"],
                     track["album"], track["mtime_ns"], track["size"],
                     store.dumps(track["genres"]),
                     None if track.get("fields") is None
                     else store.dumps(track["fields"])),
                )
        gone = set(known) - set(seen)
        if walk_errors or (known and not seen):
            # An unreadable folder or an empty (unmounted?) library must not
            # wipe tracks and their Revert backups.
            if gone:
                log(f"not removing {len(gone)} tracks: the library could not "
                    "be listed completely")
            gone = set()
        for relative in gone:
            connection.execute("DELETE FROM tracks WHERE path = ?",
                               (relative,))
        connection.execute(
            "UPDATE albums SET status = 'missing', updated_at = ? "
            "WHERE status != 'missing' AND id NOT IN "
            "(SELECT DISTINCT album_id FROM tracks)", (stamp,))
    return {"files": len(seen) + errors, "read": read, "errors": errors,
            "albums": len(groups), "new_albums": new_albums}


def safe_path(root: Path, relative: str) -> Path:
    """Resolve relative under root, refusing anything that escapes it."""
    path = (root / relative).resolve()
    if root.resolve() not in path.parents or path.is_symlink():
        raise TagError(f"{relative}: outside the library")
    return path


def write_genres(path: Path, genres: list) -> None:
    """Replace a file's genre tag with genres (empty list removes it)."""
    try:
        audio = mutagen.File(path, easy=True)
        if audio is None:
            raise TagError(f"{path}: not a supported audio file")
        if audio.tags is None:
            audio.add_tags()
        if genres:
            audio.tags["genre"] = list(genres)
        elif "genre" in audio.tags:
            del audio.tags["genre"]
        if path.suffix.lower() == ".mp3":
            audio.save(v2_version=4)
        else:
            audio.save()
    except TagError:
        raise
    except Exception as error:
        raise TagError(f"{path}: {error}") from error


def apply_album(connection: sqlite3.Connection, root: Path, album_id: int,
                genres: list) -> dict:
    """Write genres to every track of an album and record the result.

    Each track's genres from before its first write are kept in
    original_genres for Revert. Returns {"written", "errors": [...]}.
    """
    return _rewrite(connection, root, album_id, lambda track: genres,
                    record_original=True, applied=genres)


def revert_album(connection: sqlite3.Connection, root: Path,
                 album_id: int) -> dict:
    """Put back each track's genres from before music-librarian wrote them.

    Tracks without a backup (never written, or already restored) are left
    alone.
    """
    return _rewrite(
        connection, root, album_id,
        lambda track: store.loads(track["original_genres"], None),
        record_original=False, applied=None)


def _rewrite(connection, root, album_id, genres_for, record_original,
             applied):
    """Shared write loop for apply_album and revert_album."""
    tracks = connection.execute(
        "SELECT path, genres, original_genres FROM tracks WHERE album_id = ? "
        "ORDER BY path", (album_id,)).fetchall()
    written, errors = 0, []
    for track in tracks:
        genres = genres_for(track)
        if genres is None:
            continue
        genres = list(genres)
        try:
            path = safe_path(root, track["path"])
            if not path.is_file():
                raise TagError(f"{track['path']}: file is gone")
            original = track["original_genres"]
            if record_original and original is None:
                # Back up what is in the file now (not the last scan), and
                # save it before the file is touched, so an interrupted
                # write can still be reverted.
                original = store.dumps(read_track(path)["genres"])
                connection.execute(
                    "UPDATE tracks SET original_genres = ? WHERE path = ?",
                    (original, track["path"]))
            write_genres(path, genres)
            try:
                info = path.stat()
            except OSError as error:
                raise TagError(f"{track['path']}: {error}") from error
        except TagError as error:
            errors.append(str(error))
            continue
        written += 1
        if not record_original:
            original = None
        # fields = NULL: saving can rename frames (v2.3 -> v2.4), so the
        # next scan reads this file's tag inventory again.
        connection.execute(
            "UPDATE tracks SET genres = ?, original_genres = ?, mtime_ns = ?, "
            "size = ?, fields = NULL WHERE path = ?",
            (store.dumps(genres), original, info.st_mtime_ns, info.st_size,
             track["path"]))
    current = []
    for row in connection.execute(
            "SELECT genres FROM tracks WHERE album_id = ? ORDER BY path",
            (album_id,)):
        for genre in store.loads(row["genres"], []):
            if genre not in current:
                current.append(genre)
    if applied is not None:
        status = "applied" if not errors else "changed"
        connection.execute(
            "UPDATE albums SET status = ?, applied_genres = ?, applied_at = ?, "
            "current_genres = ?, updated_at = ? WHERE id = ?",
            (status, store.dumps(applied), store.now_iso(),
             store.dumps(current), store.now_iso(), album_id))
    elif errors:
        # Some tracks still carry the approved genres and their backups;
        # keep the album revertible so Revert can be tried again.
        connection.execute(
            "UPDATE albums SET status = 'changed', current_genres = ?, "
            "updated_at = ? WHERE id = ?",
            (store.dumps(current), store.now_iso(), album_id))
    else:
        connection.execute(
            "UPDATE albums SET status = 'pending', applied_genres = NULL, "
            "applied_at = NULL, current_genres = ?, updated_at = ? "
            "WHERE id = ?",
            (store.dumps(current), store.now_iso(), album_id))
    return {"written": written, "errors": errors}
