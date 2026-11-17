"""Offline tests for install-mympd-tile.py against a fake myMPD API."""

import importlib.util
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "install-mympd-tile.py"
SPEC = importlib.util.spec_from_file_location("install_mympd_tile", SCRIPT)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class FakeMympd(BaseHTTPRequestHandler):
    """Answer the two Home tile methods and keep the tiles in memory."""

    tiles: list = []

    def do_POST(self):  # noqa: N802 - name required by BaseHTTPRequestHandler
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        params = request["params"]
        if request["method"] == "MYMPD_API_HOME_ICON_LIST":
            result = {"data": self.tiles}
        else:
            tile = {key: params[key] for key in
                    ("name", "ligature", "bgcolor", "color", "image",
                     "cmd", "options")}
            if params["replace"]:
                self.tiles[params["oldpos"]] = tile
            else:
                self.tiles.append(tile)
            result = {"message": "ok"}
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        """Keep test output quiet."""


class InstallTileTest(unittest.TestCase):
    """Check that the tile is added once and then updated in place."""

    def setUp(self):
        FakeMympd.tiles = [{"name": "Other", "cmd": "openExternalLink",
                            "options": ["http://example.test/", "true"]}]
        self.server = HTTPServer(("127.0.0.1", 0), FakeMympd)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/api/default"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_adds_then_updates_without_duplicating(self):
        self.assertFalse(installer.install(self.endpoint, "http://a.test:18771/"))
        self.assertTrue(installer.install(self.endpoint, "http://b.test:18771/"))
        ours = [tile for tile in FakeMympd.tiles if tile["name"] == "music-librarian"]
        self.assertEqual(len(ours), 1)
        self.assertEqual(ours[0]["options"], ["http://b.test:18771/", "true"])
        self.assertEqual(ours[0]["ligature"], "library_music")
        self.assertEqual(FakeMympd.tiles[0]["name"], "Other")

    def test_url_validation(self):
        self.assertTrue(installer.valid_http_url("http://127.0.0.1:18771/"))
        for bad in ("ftp://host/", "http://user@host/", "http://host/?a=1",
                    "javascript:alert(1)", "http://host/ x", "not a url"):
            self.assertFalse(installer.valid_http_url(bad), bad)


if __name__ == "__main__":
    unittest.main()
