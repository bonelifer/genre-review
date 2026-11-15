"""Offline tests for genre-review (no network; Last.fm and MPD are faked)."""

import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mutagen  # noqa: E402
from mutagen.easyid3 import EasyID3  # noqa: E402
from mutagen.flac import FLAC  # noqa: E402
from mutagen.id3 import ID3  # noqa: E402

import genre_review  # noqa: E402
import lastfm  # noqa: E402
import library  # noqa: E402
import mpd  # noqa: E402
import store  # noqa: E402
import suggest  # noqa: E402
import web  # noqa: E402


def make_mp3(path: Path, artist="Artist", album="Album", genre=None):
    """Write a tiny valid MP3 (three silent MPEG-1 Layer III frames)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = b"\xff\xfb\x90\x64" + bytes(413)
    path.write_bytes(frame * 3)
    tags = EasyID3()
    tags["artist"] = artist
    tags["albumartist"] = artist
    if album:
        tags["album"] = album
    if genre:
        tags["genre"] = genre
    tags.save(path, v2_version=3)


def make_flac(path: Path, artist="Artist", album="Album", genre=None):
    """Write a tiny FLAC file (STREAMINFO only) with Vorbis comments."""
    path.parent.mkdir(parents=True, exist_ok=True)
    info = struct.pack(">HH", 4096, 4096) + bytes(6)
    info += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big")
    info += bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80, 0, 0, 34]) + info)
    audio = FLAC(path)
    audio["artist"] = artist
    audio["albumartist"] = artist
    audio["album"] = album
    if genre:
        audio["genre"] = genre
    audio.save()


def genres_of(path: Path):
    """Genre values as stored in the file."""
    return list(mutagen.File(path, easy=True).tags.get("genre", []))


class SuggestTests(unittest.TestCase):
    """Curated-list matching and ranking."""

    def setUp(self):
        self.whitelist = suggest.load_whitelist()

    def test_names_are_matched_loosely_and_prettified(self):
        self.assertEqual(self.whitelist[suggest.lookup_key("HIP-HOP")],
                         "Hip Hop")
        self.assertEqual(self.whitelist[suggest.lookup_key("indie rock")],
                         "Indie Rock")
        self.assertEqual(self.whitelist[suggest.lookup_key("R&B")],
                         self.whitelist[suggest.lookup_key(
                             "rhythm and blues")])
        self.assertEqual(suggest.pretty_name("j-pop"), "J-Pop")
        self.assertEqual(suggest.pretty_name("rock and roll"),
                         "Rock and Roll")

    def test_junk_tags_are_dropped_and_sources_combined(self):
        found = suggest.build_suggestions(
            [("seen live", 100), ("Indie Rock", 80), ("favorites", 70)],
            [("indie rock", 50), ("Rock", 40)],
            ["Alternative/Rock pop"],
            self.whitelist)
        names = [item["name"] for item in found]
        self.assertNotIn("Seen Live", names)
        self.assertEqual(found[0]["name"], "Indie Rock")
        self.assertEqual(found[0]["sources"], ["album", "artist"])
        self.assertEqual(found[0]["weight"], 90)
        self.assertEqual(found[0]["album_weight"], 80)
        self.assertIn("Alternative", names)
        self.assertNotIn("Rock Pop", names)

    def test_split_genre_values(self):
        self.assertEqual(suggest.split_genre_values(
            ["Rock/Pop; Folk", "Pop"]), ["Rock", "Pop", "Folk"])

    def test_preselected(self):
        found = [{"name": "A", "weight": 90}, {"name": "B", "weight": 30},
                 {"name": "C", "weight": 26}, {"name": "D", "weight": 25}]
        self.assertEqual(suggest.preselected(found, 3), ["A", "B", "C"])
        self.assertEqual(suggest.preselected(
            [{"name": "Z", "weight": 10}], 3), ["Z"])
        self.assertEqual(suggest.preselected([], 3), [])


class LibraryTests(unittest.TestCase):
    """Scanning, writing and reverting against real (tiny) audio files."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "library"
        self.root.mkdir()
        self.db = store.connect(Path(self.temp.name) / "state" / "db.sqlite")
        self.messages = []

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def scan(self):
        return library.scan(self.db, self.root, log=self.messages.append)

    def album(self, name):
        return self.db.execute("SELECT * FROM albums WHERE album = ?",
                               (name,)).fetchone()

    def test_scan_groups_by_album_across_disc_folders(self):
        make_mp3(self.root / "A/Album/CD 1/01.mp3", genre=["Rock/Pop"])
        make_mp3(self.root / "A/Album/CD 2/01.mp3", genre=["Folk"])
        make_flac(self.root / "B/Other/01.flac", artist="B", album="Other")
        result = self.scan()
        self.assertEqual((result["files"], result["albums"],
                          result["new_albums"]), (3, 2, 2))
        album = self.album("Album")
        self.assertEqual(album["folder"], "A/Album")
        self.assertEqual(store.loads(album["current_genres"], []),
                         ["Rock/Pop", "Folk"])

    def test_files_without_album_tag_group_by_folder(self):
        make_mp3(self.root / "Loose/x.mp3", album="")
        make_mp3(self.root / "Loose/y.mp3", album="")
        self.scan()
        rows = self.db.execute("SELECT album_key, album FROM albums").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["album"], "Loose")

    def test_untagged_disc_folders_are_named_from_parent_folders(self):
        make_mp3(self.root / "Eagles/The Long Run (1979)/CD 1/01.mp3",
                 artist="", album="")
        make_mp3(self.root / "Eagles/The Long Run (1979)/CD 2/01.mp3",
                 artist="", album="")
        self.scan()
        rows = self.db.execute("SELECT albumartist, album, folder FROM "
                               "albums").fetchall()
        self.assertEqual([tuple(row) for row in rows],
                         [("Eagles", "The Long Run",
                           "Eagles/The Long Run (1979)")])

    def test_unchanged_files_are_not_read_again(self):
        make_mp3(self.root / "A/Album/01.mp3")
        self.scan()
        self.assertEqual(self.scan()["read"], 0)

    def test_unreadable_file_is_reported_not_fatal(self):
        make_mp3(self.root / "A/Album/01.mp3")
        (self.root / "A/Album/broken.mp3").write_bytes(b"not audio")
        result = self.scan()
        self.assertEqual(result["errors"], 1)
        self.assertTrue(self.messages)

    def test_apply_writes_separate_values_as_id3v24_and_revert(self):
        track = self.root / "A/Album/01.mp3"
        make_mp3(track, genre=["Rock/Pop"])
        self.scan()
        album_id = self.album("Album")["id"]
        result = library.apply_album(self.db, self.root, album_id,
                                     ["Indie Rock", "Alternative"])
        self.assertEqual(result, {"written": 1, "errors": []})
        tags = ID3(track)
        self.assertEqual(tags.version[:2], (2, 4))
        self.assertEqual(tags["TCON"].text, ["Indie Rock", "Alternative"])
        self.assertEqual(tags["TPE1"].text, ["Artist"])
        self.assertEqual(self.album("Album")["status"], "applied")
        # A second apply keeps the very first genres for Revert.
        library.apply_album(self.db, self.root, album_id, ["Rock"])
        library.revert_album(self.db, self.root, album_id)
        self.assertEqual(genres_of(track), ["Rock/Pop"])
        album = self.album("Album")
        self.assertEqual((album["status"], album["applied_genres"]),
                         ("pending", None))

    def test_flac_gets_one_field_per_genre(self):
        track = self.root / "B/Other/01.flac"
        make_flac(track, album="Other")
        self.scan()
        library.apply_album(self.db, self.root, self.album("Other")["id"],
                            ["Jazz", "Swing"])
        self.assertEqual(FLAC(track)["genre"], ["Jazz", "Swing"])

    def test_rescan_after_apply_stays_applied_until_files_change(self):
        track = self.root / "A/Album/01.mp3"
        make_mp3(track)
        self.scan()
        library.apply_album(self.db, self.root, self.album("Album")["id"],
                            ["Rock"])
        self.scan()
        self.assertEqual(self.album("Album")["status"], "applied")
        tags = EasyID3(track)
        tags["genre"] = ["Pop"]
        tags.save()
        os.utime(track, ns=(1, 1))
        self.scan()
        self.assertEqual(self.album("Album")["status"], "changed")

    def test_removed_album_becomes_missing_and_returns_pending(self):
        track = self.root / "A/Album/01.mp3"
        make_mp3(track)
        make_mp3(self.root / "B/Other/01.mp3", artist="B", album="Other")
        self.scan()
        backup = track.read_bytes()
        track.unlink()
        self.scan()
        self.assertEqual(self.album("Album")["status"], "missing")
        track.write_bytes(backup)
        self.scan()
        self.assertEqual(self.album("Album")["status"], "pending")

    def test_safe_path_refuses_escapes_and_symlinks(self):
        outside = Path(self.temp.name) / "outside.mp3"
        make_mp3(outside)
        (self.root / "link.mp3").symlink_to(outside)
        with self.assertRaises(library.TagError):
            library.safe_path(self.root, "../outside.mp3")
        with self.assertRaises(library.TagError):
            library.safe_path(self.root, "link.mp3")
        self.scan()
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM tracks").fetchone()[0], 0)

    def test_unreadable_track_keeps_its_revert_backup(self):
        track = self.root / "A/Album/01.mp3"
        make_mp3(track, genre=["Folk"])
        self.scan()
        album_id = self.album("Album")["id"]
        library.apply_album(self.db, self.root, album_id, ["Rock"])
        good = track.read_bytes()
        track.write_bytes(b"temporarily broken")
        self.scan()
        backup = self.db.execute("SELECT original_genres FROM tracks"
                                 ).fetchone()[0]
        self.assertEqual(store.loads(backup, None), ["Folk"])
        track.write_bytes(good)
        self.scan()
        library.revert_album(self.db, self.root, album_id)
        self.assertEqual(genres_of(track), ["Folk"])

    def test_file_that_cannot_be_checked_is_not_treated_as_deleted(self):
        keep = self.root / "A/Album/01.mp3"
        make_mp3(keep)
        make_mp3(self.root / "B/Other/01.mp3", artist="B", album="Other")
        self.scan()
        real_stat = Path.stat

        def failing_stat(path, *args, **kwargs):
            if path.name == "01.mp3" and path.parent.name == "Album":
                raise PermissionError("denied")
            return real_stat(path, *args, **kwargs)

        Path.stat = failing_stat
        try:
            self.scan()
        finally:
            Path.stat = real_stat
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM tracks").fetchone()[0], 2)

    def test_empty_library_does_not_wipe_tracks(self):
        track = self.root / "A/Album/01.mp3"
        make_mp3(track)
        self.scan()
        track.unlink()
        self.scan()
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM tracks").fetchone()[0], 1)

    def test_change_on_one_track_marks_album_changed(self):
        first = self.root / "A/Album/01.mp3"
        second = self.root / "A/Album/02.mp3"
        make_mp3(first)
        make_mp3(second)
        self.scan()
        library.apply_album(self.db, self.root, self.album("Album")["id"],
                            ["Rock", "Pop"])
        tags = EasyID3(second)
        tags["genre"] = ["Rock"]
        tags.save()
        os.utime(second, ns=(1, 1))
        self.scan()
        self.assertEqual(self.album("Album")["status"], "changed")

    def test_backup_comes_from_the_file_not_the_last_scan(self):
        track = self.root / "A/Album/01.mp3"
        make_mp3(track, genre=["Folk"])
        self.scan()
        tags = EasyID3(track)
        tags["genre"] = ["Edited Later"]
        tags.save()
        album_id = self.album("Album")["id"]
        library.apply_album(self.db, self.root, album_id, ["Rock"])
        library.revert_album(self.db, self.root, album_id)
        self.assertEqual(genres_of(track), ["Edited Later"])

    def test_failed_revert_stays_revertible(self):
        first = self.root / "A/Album/01.mp3"
        second = self.root / "A/Album/02.mp3"
        make_mp3(first, genre=["Folk"])
        make_mp3(second, genre=["Folk"])
        self.scan()
        album_id = self.album("Album")["id"]
        library.apply_album(self.db, self.root, album_id, ["Rock"])
        saved = second.read_bytes()
        second.unlink()
        result = library.revert_album(self.db, self.root, album_id)
        self.assertEqual(len(result["errors"]), 1)
        album = self.album("Album")
        self.assertEqual(album["status"], "changed")
        self.assertIsNotNone(album["applied_genres"])
        second.write_bytes(saved)
        self.assertEqual(library.revert_album(
            self.db, self.root, album_id)["errors"], [])
        self.assertEqual(genres_of(second), ["Folk"])

    def test_backup_is_saved_before_the_file_is_written(self):
        track = self.root / "A/Album/01.mp3"
        make_mp3(track, genre=["Folk"])
        self.scan()
        original_write = library.write_genres

        def write_then_crash(path, genres):
            original_write(path, genres)
            raise library.TagError("stopped after writing")

        library.write_genres = write_then_crash
        try:
            library.apply_album(self.db, self.root,
                                self.album("Album")["id"], ["Rock"])
        finally:
            library.write_genres = original_write
        backup = self.db.execute("SELECT original_genres FROM tracks"
                                 ).fetchone()[0]
        self.assertEqual(store.loads(backup, None), ["Folk"])

    def test_revert_leaves_never_written_tracks_alone(self):
        first = self.root / "A/Album/01.mp3"
        second = self.root / "A/Album/02.mp3"
        make_mp3(first, genre=["Folk"])
        make_mp3(second, genre=["Folk"])
        self.scan()
        album_id = self.album("Album")["id"]
        saved = second.read_bytes()
        second.unlink()
        library.apply_album(self.db, self.root, album_id, ["Rock"])
        second.write_bytes(saved)
        tags = EasyID3(second)
        tags["genre"] = ["Edited Elsewhere"]
        tags.save()
        result = library.revert_album(self.db, self.root, album_id)
        self.assertEqual(result, {"written": 1, "errors": []})
        self.assertEqual(genres_of(first), ["Folk"])
        self.assertEqual(genres_of(second), ["Edited Elsewhere"])

    def test_missing_file_is_reported_on_apply(self):
        track = self.root / "A/Album/01.mp3"
        make_mp3(track)
        self.scan()
        track.unlink()
        result = library.apply_album(self.db, self.root,
                                     self.album("Album")["id"], ["Rock"])
        self.assertEqual(result["written"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(self.album("Album")["status"], "changed")


class FakeResponse(io.BytesIO):
    """Context-manager response for the fake opener."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class LastfmTests(unittest.TestCase):
    """Retries, not-found handling, errors and the cache."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = store.connect(Path(self.temp.name) / "db.sqlite")
        self.calls = []
        self.sleeps = []

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def client(self, replies):
        replies = list(replies)

        def opener(request, timeout):
            self.calls.append(request.full_url)
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return FakeResponse(json.dumps(reply).encode())

        return lastfm.Lastfm("key", self.db, opener=opener,
                             sleep=self.sleeps.append)

    def test_tags_are_parsed_and_cached(self):
        client = self.client([{"toptags": {"tag": [
            {"name": "Indie Rock", "count": 100},
            {"name": "", "count": 5}, {"name": "pop", "count": "x"}]}}])
        expected = [("Indie Rock", 100), ("pop", 0)]
        self.assertEqual(client.album_tags("A", "B"), expected)
        self.assertEqual(client.album_tags("a ", "b"), expected)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("autocorrect=1", self.calls[0])

    def test_single_tag_object_and_not_found(self):
        client = self.client([
            {"toptags": {"tag": {"name": "Jazz", "count": 7}}},
            {"error": 6, "message": "not found"}])
        self.assertEqual(client.artist_tags("A"), [("Jazz", 7)])
        self.assertEqual(client.artist_tags("Nobody"), [])

    def test_transient_errors_retry_with_backoff(self):
        client = self.client([
            {"error": 29, "message": "rate limit"},
            OSError("reset"),
            {"toptags": {"tag": []}}])
        self.assertEqual(client.artist_tags("A"), [])
        self.assertEqual(len(self.calls), 3)
        self.assertIn(2, self.sleeps)
        self.assertIn(4, self.sleeps)

    def test_permanent_error_raises_and_is_not_cached(self):
        client = self.client([{"error": 10, "message": "Invalid API key"},
                              {"toptags": {"tag": []}}])
        with self.assertRaises(lastfm.LastfmError):
            client.artist_tags("A")
        self.assertEqual(client.artist_tags("A"), [])

    def test_http_4xx_without_body_is_permanent(self):
        error = HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b""))
        client = self.client([error])
        with self.assertRaises(lastfm.LastfmError):
            client.artist_tags("A")


class MpdTests(unittest.TestCase):
    """The update command reaches MPD, quoted, with the password first."""

    def serve(self, replies):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        received = []

        def run():
            conn, _ = listener.accept()
            with conn:
                stream = conn.makefile("rwb")
                stream.write(b"OK MPD 0.24.0\n")
                stream.flush()
                for reply in replies:
                    received.append(stream.readline().decode().strip())
                    stream.write(reply)
                    stream.flush()
                received.append(stream.readline().decode().strip())
            listener.close()

        thread = threading.Thread(target=run)
        thread.start()
        return listener.getsockname()[1], received, thread

    def test_update_with_password(self):
        port, received, thread = self.serve([b"OK\n", b"updating_db: 1\nOK\n"])
        mpd.update("127.0.0.1", port, 'library/A "B"', "secret")
        thread.join(5)
        self.assertEqual(received, ['password "secret"',
                                    'update "library/A \\"B\\""', "close"])

    def test_ack_raises(self):
        port, _, thread = self.serve([b"ACK [50@0] {update} Malformed\n"])
        with self.assertRaises(mpd.MpdError):
            mpd.update("127.0.0.1", port, "x")
        thread.join(5)

    def test_unreachable_raises(self):
        with self.assertRaises(mpd.MpdError):
            mpd.update("127.0.0.1", 1, "x")


class WebTests(unittest.TestCase):
    """The review page end to end, with Last.fm and MPD left out."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "library"
        self.track = self.root / "A/Album/01.mp3"
        make_mp3(self.track, genre=["Rock/Pop"])
        settings = genre_review.Settings(
            library_dir=self.root, state_dir=base / "state",
            lastfm_api_key="", listen="127.0.0.1", port=0,
            scan_interval_minutes=0, max_genres=3, bulk_min_weight=60,
            mpd_host="127.0.0.1", mpd_port=1, mpd_password="",
            mpd_path_prefix="library")
        self.reviewer = genre_review.Reviewer(settings, log=lambda _: None)
        self.reviewer.scan()
        db = self.reviewer.connect()
        db.execute("UPDATE albums SET suggestions = ?", (store.dumps([
            {"name": "Indie Rock", "weight": 80, "sources": ["album"],
             "album_weight": 80},
            {"name": "Rock", "weight": 30, "sources": ["artist"],
             "album_weight": 0}]),))
        db.close()
        self.server = web.ReviewServer(("127.0.0.1", 0), self.reviewer)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def get(self, path):
        with urlopen(self.base + path, timeout=10) as response:
            return response.status, response.read().decode()

    def post(self, path, fields):
        request = Request(self.base + path, urlencode(fields, doseq=True)
                          .encode(), method="POST")
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, response.url
        except HTTPError as error:
            return error.code, ""

    def test_overview_and_album_page(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("LASTFM_API_KEY is not set", body)
        status, body = self.get("/album/1")
        self.assertIn('value="Indie Rock" checked', body)
        self.assertIn('value="Rock" checked', body)
        self.assertIn("1 track in", body)

    def test_apply_needs_the_token(self):
        status, _ = self.post("/album/1/apply", {"token": "wrong",
                                                 "genre": "Jazz"})
        self.assertEqual(status, 403)
        self.assertEqual(genres_of(self.track), ["Rock/Pop"])

    def test_apply_writes_and_reports_mpd_failure(self):
        status, url = self.post("/album/1/apply", {
            "token": self.server.token, "genre": ["Indie Rock"],
            "extra": "Shoegaze, indie rock"})
        self.assertEqual(status, 200)
        self.assertIn("MPD+update+failed", url)
        self.assertEqual(genres_of(self.track), ["Indie Rock", "Shoegaze"])

    def test_empty_choice_writes_nothing(self):
        self.post("/album/1/apply", {"token": self.server.token})
        self.assertEqual(genres_of(self.track), ["Rock/Pop"])

    def test_bulk_skips_artist_only_suggestions(self):
        db = self.reviewer.connect()
        db.execute("UPDATE albums SET suggestions = ?", (store.dumps([
            {"name": "Blues", "weight": 90, "sources": ["artist"]}]),))
        db.close()
        _, body = self.get("/bulk")
        self.assertIn("No waiting album", body)

    def test_bulk_reports_mpd_failures(self):
        _, body = self.get("/bulk")
        self.assertIn('name="album" value="1"', body)
        status, url = self.post("/bulk", {"token": self.server.token,
                                          "album": "1"})
        self.assertEqual(status, 200)
        self.assertIn("Wrote+genres+for+1+albums", url)
        self.assertIn("MPD+was+not+told+about+1+albums", url)
        self.assertEqual(genres_of(self.track), ["Indie Rock", "Rock"])

    def test_bulk_needs_strong_album_tags_not_combined_score(self):
        db = self.reviewer.connect()
        db.execute("UPDATE albums SET suggestions = ?", (store.dumps([
            {"name": "Blues", "weight": 70, "sources": ["album", "artist"],
             "album_weight": 1}]),))
        db.close()
        _, body = self.get("/bulk")
        self.assertIn("No waiting album", body)

    def test_old_suggestions_are_looked_up_again(self):
        self.reviewer.settings.lastfm_api_key = "key"
        looked_up = []

        def fake(client, row):
            looked_up.append(row["id"])
            return []

        self.reviewer.suggestions_for = fake
        db = self.reviewer.connect()
        db.execute("UPDATE albums SET suggested_at = ?", (store.now_iso(),))
        db.close()
        self.assertEqual(self.reviewer.suggest_missing(), 0)
        db = self.reviewer.connect()
        db.execute("UPDATE albums SET suggested_at = '2000-01-01T00:00:00"
                   "+00:00'")
        db.close()
        self.assertEqual(self.reviewer.suggest_missing(), 1)
        self.assertEqual(looked_up, [1])

    def test_next_prefers_albums_that_were_looked_up(self):
        make_mp3(self.root / "0/First/01.mp3", artist="0", album="First")
        self.reviewer.scan()
        with urlopen(self.base + "/next", timeout=10) as response:
            self.assertTrue(response.url.endswith("/album/1"))

    def test_review_list_offers_checkboxes_with_default_genres(self):
        _, body = self.get("/albums?filter=pending")
        self.assertIn('name="album" value="1"', body)
        self.assertNotIn('value="1" id="a1" checked', body)
        self.assertIn("Indie Rock, Rock", body)
        _, body = self.get("/albums?filter=pending&tick=all")
        self.assertIn('value="1" id="a1" checked', body)
        _, body = self.get("/albums?filter=applied")
        self.assertNotIn('name="album"', body)

    def test_approve_ticked_writes_default_genres(self):
        status, url = self.post("/approve", {
            "token": self.server.token, "filter": "pending", "page": "1",
            "album": "1"})
        self.assertEqual(status, 200)
        self.assertIn("filter=pending", url)
        self.assertIn("Wrote+genres+for+1+albums", url)
        self.assertEqual(genres_of(self.track), ["Indie Rock", "Rock"])

    def test_approve_refuses_albums_no_longer_in_review(self):
        db = self.reviewer.connect()
        db.execute("UPDATE albums SET status = 'ignored'")
        db.close()
        _, url = self.post("/approve", {"token": self.server.token,
                                        "album": "1"})
        self.assertIn("Nothing+written", url)
        self.assertEqual(genres_of(self.track), ["Rock/Pop"])

    def test_approve_changed_album_keeps_its_approved_genres(self):
        db = self.reviewer.connect()
        db.execute("UPDATE albums SET status = 'changed', applied_genres = ?",
                   (store.dumps(["Shoegaze"]),))
        db.close()
        self.post("/approve", {"token": self.server.token,
                               "filter": "changed", "album": "1"})
        self.assertEqual(genres_of(self.track), ["Shoegaze"])

    def test_clean_genres(self):
        self.assertEqual(web.clean_genres(["Rock,  Pop ", "rock"]),
                         ["Rock", "Pop"])
        with self.assertRaises(ValueError):
            web.clean_genres(["x" * 61])
        with self.assertRaises(ValueError):
            web.clean_genres(["bad\x07name"])


class CliTests(unittest.TestCase):
    """No command prints help; settings are validated."""

    def test_no_arguments_prints_help(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(genre_review.main([]), 0)
        self.assertIn("usage:", output.getvalue())

    def test_missing_library_is_a_clear_error(self):
        os.environ.pop("LIBRARY_DIR", None)
        with self.assertRaises(SystemExit) as caught:
            genre_review.main(["scan"])
        self.assertIn("LIBRARY_DIR", str(caught.exception))

    def test_bad_number_setting(self):
        os.environ["MAX_GENRES"] = "lots"
        try:
            with tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(SystemExit) as caught:
                    genre_review.main(["-L", folder, "-S", folder, "scan"])
            self.assertIn("MAX_GENRES", str(caught.exception))
        finally:
            del os.environ["MAX_GENRES"]


if __name__ == "__main__":
    unittest.main()
