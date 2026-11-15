"""Review web page for genre-review (plain HTML forms, no JavaScript).

Every page works with a keyboard and a screen reader: real headings,
labelled form controls, a skip link and visible focus. Forms that change
files carry a per-process token so other sites cannot submit them.
"""

import html
import re
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import store
import suggest

PAGE_SIZE = 100
BULK_LIMIT = 200
MAX_GENRE_LENGTH = 60
FILTERS = {
    "pending": "Waiting for review",
    "changed": "Changed since approval",
    "nosuggest": "No suggestions",
    "applied": "Applied",
    "skipped": "Skipped",
    "ignored": "Never ask again",
}
ORDER = "albumartist COLLATE NOCASE, album COLLATE NOCASE, id"
# Lists whose albums can be ticked and approved together.
APPROVABLE = ("pending", "changed", "skipped")

STYLE = """
:root { color-scheme: light dark; --bg: #fff; --fg: #1b1b1b; --muted: #555;
  --line: #ccc; --accent: #0b57d0; --warn: #a40000; --card: #f4f4f4; }
@media (prefers-color-scheme: dark) { :root { --bg: #161616; --fg: #ececec;
  --muted: #aaa; --line: #444; --accent: #8ab4f8; --warn: #ff8a80;
  --card: #222; } }
body { background: var(--bg); color: var(--fg); font: 16px/1.5 system-ui,
  sans-serif; margin: 0 auto; max-width: 60rem; padding: 0 1rem 3rem; }
a { color: var(--accent); }
:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
.skip { position: absolute; left: -999px; }
.skip:focus { left: 1rem; top: 1rem; background: var(--bg); padding: .5rem; }
nav ul { list-style: none; padding: 0; display: flex; flex-wrap: wrap;
  gap: .25rem 1rem; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .35rem .5rem;
  border-bottom: 1px solid var(--line); vertical-align: top; }
fieldset { border: 1px solid var(--line); border-radius: 6px;
  margin: 1rem 0; }
.muted { color: var(--muted); }
.error { color: var(--warn); }
.notice { background: var(--card); padding: .75rem 1rem; border-radius: 6px; }
button { font: inherit; padding: .4rem .9rem; margin: .25rem .5rem .25rem 0;
  cursor: pointer; }
input[type=text] { font: inherit; width: min(30rem, 100%); }
label { cursor: pointer; }
.genres li { margin: .2rem 0; }
"""


class ReviewServer(ThreadingHTTPServer):
    """HTTP server that carries the Reviewer and the form token."""

    daemon_threads = True

    def __init__(self, address, reviewer):
        super().__init__(address, Handler)
        self.reviewer = reviewer
        self.token = secrets.token_urlsafe(24)


def serve(reviewer) -> None:
    """Run the review page until interrupted."""
    settings = reviewer.settings
    server = ReviewServer((settings.listen, settings.port), reviewer)
    print(f"genre-review is listening on http://{settings.listen}:"
          f"{settings.port}/", flush=True)
    server.serve_forever()


def esc(value) -> str:
    """HTML-escape any value."""
    return html.escape(str(value), quote=True)


def genre_list(genres) -> str:
    """Comma-separated genres, or a muted 'none'."""
    return esc(", ".join(genres)) if genres else \
        '<span class="muted">none</span>'


def default_genres(status: str, suggestions, applied, limit: int) -> list:
    """Genres ticked by default for an album (album page and multi-approve).

    An album approved before keeps its approved genres; otherwise the best
    suggestions are used.
    """
    if status in ("applied", "changed") and applied:
        return list(applied)
    return suggest.preselected(suggestions or [], limit)


def clean_genres(values) -> list:
    """Validate submitted genre names; raises ValueError with a reason."""
    names = []
    for value in values:
        for part in value.split(","):
            name = " ".join(part.split())
            if not name:
                continue
            if len(name) > MAX_GENRE_LENGTH:
                raise ValueError(f"'{name[:20]}…' is longer than "
                                 f"{MAX_GENRE_LENGTH} characters")
            if re.search(r"[\x00-\x1f\x7f]", name):
                raise ValueError("Genre names cannot contain control "
                                 "characters")
            if name.casefold() not in (item.casefold() for item in names):
                names.append(name)
    if len(names) > 10:
        raise ValueError("Choose at most 10 genres")
    return names


class Handler(BaseHTTPRequestHandler):
    """Routes for the review page."""

    server_version = "genre-review"

    def log_message(self, format, *args):  # noqa: A002 (stdlib signature)
        """Keep request logging quiet; errors are shown on the page."""

    # --- helpers ---------------------------------------------------------

    @property
    def reviewer(self):
        """The shared Reviewer."""
        return self.server.reviewer

    def send_page(self, title: str, body: str,
                  status: HTTPStatus = HTTPStatus.OK) -> None:
        """Wrap body in the page layout and send it."""
        page = (
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, "
            f"initial-scale=1\"><title>{esc(title)} - Genre review</title>"
            f"<style>{STYLE}</style></head><body>"
            "<a class=\"skip\" href=\"#main\">Skip to content</a>"
            "<header><nav aria-label=\"Main\"><ul>"
            "<li><a href=\"/\">Overview</a></li>"
            "<li><a href=\"/next\">Next album</a></li>"
            "<li><a href=\"/bulk\">Bulk accept</a></li>"
            "</ul></nav></header>"
            f"<main id=\"main\">{body}</main></body></html>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; "
                         "form-action 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(page)

    def redirect(self, location: str) -> None:
        """303 redirect after a form post."""
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def not_found(self) -> None:
        """404 page."""
        self.send_page("Not found", "<h1>Not found</h1>",
                       HTTPStatus.NOT_FOUND)

    def token_field(self) -> str:
        """Hidden form token."""
        return (f"<input type=\"hidden\" name=\"token\" "
                f"value=\"{esc(self.server.token)}\">")

    def read_form(self):
        """Parse a POSTed form; None if it is too big or the token is wrong."""
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            return None
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"),
                        keep_blank_values=True)
        token = form.get("token", [""])[0]
        if not secrets.compare_digest(token, self.server.token):
            return None
        return form

    def db(self):
        """A short-lived DB connection for this request."""
        return self.reviewer.connect()

    # --- routing ---------------------------------------------------------

    def do_GET(self):  # noqa: N802 (stdlib name)
        """Read-only pages."""
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if url.path == "/":
            return self.overview(query.get("msg", [""])[0])
        if url.path == "/albums":
            return self.album_list(query.get("filter", ["pending"])[0],
                                   query.get("page", ["1"])[0],
                                   query.get("tick", [""])[0] == "all",
                                   query.get("msg", [""])[0])
        if url.path == "/next":
            return self.go_next(None)
        if url.path == "/bulk":
            return self.bulk_preview()
        match = re.fullmatch(r"/album/(\d+)", url.path)
        if match:
            return self.album_page(int(match.group(1)),
                                   query.get("msg", [""])[0])
        return self.not_found()

    def do_POST(self):  # noqa: N802 (stdlib name)
        """Actions that change state or files."""
        form = self.read_form()
        if form is None:
            return self.send_page(
                "Expired form", "<h1>That form has expired</h1><p>Reload the "
                "page and try again.</p>", HTTPStatus.FORBIDDEN)
        path = urlsplit(self.path).path
        if path == "/scan":
            return self.start_scan()
        if path == "/bulk":
            return self.bulk_apply(form)
        if path == "/approve":
            return self.approve_ticked(form)
        match = re.fullmatch(r"/album/(\d+)/(apply|skip|ignore|revert|"
                             r"unignore)", path)
        if not match:
            return self.not_found()
        album_id, action = int(match.group(1)), match.group(2)
        return self.album_action(album_id, action, form)

    # --- pages -----------------------------------------------------------

    def overview(self, message: str) -> None:
        """Counts per status, background status and actions."""
        connection = self.db()
        try:
            counts = store.album_counts(connection)
            nosuggest = connection.execute(
                "SELECT COUNT(*) FROM albums WHERE status IN ('pending', "
                "'changed') AND suggestions IS NOT NULL AND suggestions = "
                "'[]'").fetchone()[0]
            waiting = connection.execute(
                "SELECT COUNT(*) FROM albums WHERE status IN ('pending', "
                "'changed') AND suggestions IS NULL").fetchone()[0]
        finally:
            connection.close()
        counts["nosuggest"] = nosuggest
        rows = "".join(
            f"<tr><td><a href=\"/albums?filter={key}\">{esc(label)}</a></td>"
            f"<td>{counts.get(key, 0)}</td></tr>"
            for key, label in FILTERS.items())
        status = self.reviewer.status
        notice = f"<p class=\"notice\" role=\"status\">{esc(message)}</p>" \
            if message else ""
        key_note = "" if self.reviewer.settings.lastfm_api_key else (
            "<p class=\"error\">LASTFM_API_KEY is not set, so no "
            "suggestions can be fetched.</p>")
        self.send_page("Overview", (
            f"<h1>Genre review</h1>{notice}{key_note}"
            "<table><caption class=\"muted\">Albums by status</caption>"
            f"<thead><tr><th scope=\"col\">Status</th><th scope=\"col\">"
            f"Albums</th></tr></thead><tbody>{rows}</tbody></table>"
            f"<h2>Background work</h2><ul><li>Last scan: "
            f"{esc(status['scan'])}</li><li>Last.fm lookups: "
            f"{esc(status['suggest'])}</li><li>Albums still waiting for a "
            f"Last.fm lookup: {waiting}</li></ul>"
            f"<form method=\"post\" action=\"/scan\">{self.token_field()}"
            "<button type=\"submit\">Scan the library now</button></form>"
            "<p><a href=\"/next\">Review the next album</a></p>"))

    def album_list(self, name: str, page_text: str, tick_all: bool = False,
                   message: str = "") -> None:
        """Paged list of albums in one filter.

        In the review lists every album with suggestions gets a checkbox,
        so several can be approved at once with their default genres.
        """
        if name not in FILTERS:
            return self.not_found()
        page = max(1, int(page_text)) if page_text.isdigit() else 1
        where, params = self.filter_sql(name)
        connection = self.db()
        try:
            total = connection.execute(
                f"SELECT COUNT(*) FROM albums WHERE {where}",
                params).fetchone()[0]
            rows = connection.execute(
                f"SELECT id, albumartist, album, status, current_genres, "
                f"suggestions, applied_genres FROM albums WHERE {where} "
                f"ORDER BY {ORDER} LIMIT ? OFFSET ?",
                (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()
        finally:
            connection.close()
        approvable = name in APPROVABLE
        limit = self.reviewer.settings.max_genres
        items = []
        for row in rows:
            title = (f"{esc(row['albumartist'] or 'Unknown artist')} - "
                     f"{esc(row['album'])}")
            cells = (f"<td><a href=\"/album/{row['id']}\">{title}</a></td>"
                     f"<td>{genre_list(store.loads(row['current_genres'], []))}"
                     "</td>")
            if approvable:
                genres = default_genres(
                    row["status"], store.loads(row["suggestions"], None),
                    store.loads(row["applied_genres"], None), limit)
                if genres:
                    box = (f"<input type=\"checkbox\" name=\"album\" "
                           f"value=\"{row['id']}\" id=\"a{row['id']}\""
                           f"{' checked' if tick_all else ''} aria-label=\""
                           f"Approve {title}\">")
                else:
                    box = '<span class="muted">-</span>'
                cells = (f"<td>{box}</td>{cells}<td>{genre_list(genres)}"
                         "</td>")
            items.append(f"<tr>{cells}</tr>")
        columns = 4 if approvable else 2
        body_rows = "".join(items) or (
            f"<tr><td colspan=\"{columns}\">No albums here.</td></tr>")
        pages = ""
        if page > 1:
            pages += (f"<a href=\"/albums?{urlencode({'filter': name, 'page': page - 1})}\">"
                      "Previous page</a> ")
        if page * PAGE_SIZE < total:
            pages += (f"<a href=\"/albums?{urlencode({'filter': name, 'page': page + 1})}\">"
                      "Next page</a>")
        notice = f"<p class=\"notice\" role=\"status\">{esc(message)}</p>" \
            if message else ""
        head = ("<th scope=\"col\">Album</th><th scope=\"col\">Genres in "
                "the files</th>")
        if approvable:
            toggle = {"filter": name, "page": page}
            if not tick_all:
                toggle["tick"] = "all"
            head = ("<th scope=\"col\">Approve</th>" + head
                    + "<th scope=\"col\">Will become</th>")
        table = (f"<table><thead><tr>{head}</tr></thead>"
                 f"<tbody>{body_rows}</tbody></table>")
        if approvable:
            content = (
                "<p>Tick albums to approve them with the genres shown under "
                "<em>Will become</em> (the ones their album page would "
                "tick). Albums without suggestions need their own page.</p>"
                f"<p><a href=\"/albums?{urlencode(toggle)}\">"
                f"{'Untick all' if tick_all else 'Tick all on this page'}"
                "</a></p>"
                f"<form method=\"post\" action=\"/approve\">"
                f"{self.token_field()}"
                f"<input type=\"hidden\" name=\"filter\" value=\"{esc(name)}\">"
                f"<input type=\"hidden\" name=\"page\" value=\"{page}\">"
                + table
                + "<button type=\"submit\">Write genres for the ticked "
                "albums</button></form>")
        else:
            content = table
        self.send_page(FILTERS[name], (
            f"<h1>{esc(FILTERS[name])}</h1>{notice}<p>{total} albums.</p>"
            f"{content}<p>{pages}</p>"))

    @staticmethod
    def filter_sql(name: str):
        """WHERE clause and parameters for a list filter."""
        if name == "nosuggest":
            return ("status IN ('pending', 'changed') AND suggestions = '[]'",
                    ())
        return ("status = ?", (name,))

    def go_next(self, after_id, message: str = ""):
        """Redirect to the next album needing review, in list order.

        message, when given, is shown on the page it lands on.
        """
        connection = self.db()
        try:
            row = None
            # Albums whose Last.fm lookup is done come first.
            for ready in ("AND suggestions IS NOT NULL", ""):
                row = self.next_row(connection, after_id, ready)
                if row is not None:
                    break
        finally:
            connection.close()
        if row is None:
            return self.redirect("/?" + urlencode(
                {"msg": (message + " " if message else "")
                 + "Nothing is waiting for review."}))
        if message:
            return self.redirect(f"/album/{row['id']}?"
                                 + urlencode({"msg": message}))
        return self.redirect(f"/album/{row['id']}")

    @staticmethod
    def next_row(connection, after_id, ready: str):
        """Next reviewable album after after_id (wrapping), or None.

        ready is an extra SQL condition (a constant, never user input).
        """
        base = f"status IN ('pending', 'changed') {ready} AND id != ?"
        if after_id is not None:
            current = connection.execute(
                "SELECT albumartist, album FROM albums WHERE id = ?",
                (after_id,)).fetchone()
            if current is not None:
                row = connection.execute(
                    f"SELECT id FROM albums WHERE {base} AND (albumartist "
                    "COLLATE NOCASE, album COLLATE NOCASE, id) > (?, ?, ?) "
                    f"ORDER BY {ORDER} LIMIT 1",
                    (after_id, current["albumartist"], current["album"],
                     after_id)).fetchone()
                if row is not None:
                    return row
        return connection.execute(
            f"SELECT id FROM albums WHERE {base} ORDER BY {ORDER} LIMIT 1",
            (after_id or 0,)).fetchone()

    def album_page(self, album_id: int, message: str) -> None:
        """One album: current genres, suggestions, and the review form."""
        connection = self.db()
        try:
            album = connection.execute("SELECT * FROM albums WHERE id = ?",
                                       (album_id,)).fetchone()
            tracks = connection.execute(
                "SELECT path, genres FROM tracks WHERE album_id = ? "
                "ORDER BY path", (album_id,)).fetchall()
        finally:
            connection.close()
        if album is None:
            return self.not_found()
        settings = self.reviewer.settings
        current = store.loads(album["current_genres"], [])
        suggestions = store.loads(album["suggestions"], None)
        applied = store.loads(album["applied_genres"], None)
        ticked = default_genres(album["status"], suggestions, applied,
                                settings.max_genres)
        choices = "".join(
            f"<li><label><input type=\"checkbox\" name=\"genre\" "
            f"value=\"{esc(item['name'])}\""
            f"{' checked' if item['name'] in ticked else ''}> "
            f"{esc(item['name'])}</label> <span class=\"muted\">"
            f"(score {item['weight']}, from "
            f"{esc(', '.join(item['sources']))})</span></li>"
            for item in suggestions or [])
        listed = {item["name"] for item in suggestions or []}
        extra = ", ".join(name for name in ticked if name not in listed)
        if suggestions is None:
            lookup = ("<p class=\"error\">Last.fm lookup failed: "
                      f"{esc(album['suggestions_error'])}. It is retried on "
                      "the next scan.</p>" if album["suggestions_error"]
                      else "<p class=\"muted\">Not looked up on Last.fm "
                      "yet.</p>")
        elif not suggestions:
            lookup = ("<p class=\"muted\">Last.fm has no usable genres for "
                      "this album or artist. Type your own below.</p>")
        else:
            lookup = ""
        notice = f"<p class=\"notice\" role=\"status\">{esc(message)}</p>" \
            if message else ""
        track_rows = "".join(
            f"<tr><td>{esc(row['path'].rsplit('/', 1)[-1])}</td>"
            f"<td>{genre_list(store.loads(row['genres'], []))}</td></tr>"
            for row in tracks)
        token = self.token_field()
        status_text = {
            "pending": "Waiting for review",
            "applied": "Applied " + esc(album["applied_at"] or ""),
            "skipped": "Skipped", "ignored": "Never ask again",
            "changed": "Changed since approval (or some files could not "
                       "be written)",
            "missing": "No tracks left in the library",
        }[album["status"]]
        revert = (f"<form method=\"post\" action=\"/album/{album_id}/revert\">"
                  f"{token}<button type=\"submit\">Revert to the genres from "
                  "before</button></form>") \
            if album["status"] in ("applied", "changed") else ""
        ignore_action = "unignore" if album["status"] == "ignored" \
            else "ignore"
        ignore_label = "Ask about this album again" \
            if album["status"] == "ignored" else "Never ask again"
        self.send_page(f"{album['albumartist']} - {album['album']}", (
            f"<h1>{esc(album['albumartist'] or 'Unknown artist')} - "
            f"{esc(album['album'])}</h1>{notice}"
            f"<p>Status: {status_text}. Folder: <code>"
            f"{esc(album['folder'] or '(library root)')}</code>. "
            f"{len(tracks)} {'track' if len(tracks) == 1 else 'tracks'}.</p>"
            f"<p>Genres in the files now: {genre_list(current)}</p>"
            f"<form method=\"post\" action=\"/album/{album_id}/apply\">"
            f"{token}<fieldset><legend>Genres to write (up to "
            f"{settings.max_genres} are ticked by default)</legend>{lookup}"
            f"<ul class=\"genres\">{choices}</ul>"
            "<p><label for=\"extra\">Other genres, separated by commas:"
            "</label><br><input type=\"text\" id=\"extra\" name=\"extra\" "
            f"value=\"{esc(extra)}\" autocomplete=\"off\"></p></fieldset>"
            "<button type=\"submit\">Write these genres</button></form>"
            f"<form method=\"post\" action=\"/album/{album_id}/skip\">{token}"
            "<button type=\"submit\">Skip for now</button></form>"
            f"<form method=\"post\" action=\"/album/{album_id}/"
            f"{ignore_action}\">{token}<button type=\"submit\">"
            f"{ignore_label}</button></form>{revert}"
            "<details><summary>Tracks</summary><table><thead><tr>"
            "<th scope=\"col\">File</th><th scope=\"col\">Genres</th></tr>"
            f"</thead><tbody>{track_rows}</tbody></table></details>"))

    # --- actions ---------------------------------------------------------

    def album_action(self, album_id: int, action: str, form) -> None:
        """Apply, skip, ignore, unignore or revert one album."""
        connection = self.db()
        try:
            album = connection.execute(
                "SELECT id, status FROM albums WHERE id = ?",
                (album_id,)).fetchone()
            if album is None:
                return self.not_found()
            if action in ("skip", "ignore", "unignore"):
                status = {"skip": "skipped", "ignore": "ignored",
                          "unignore": "pending"}[action]
                connection.execute(
                    "UPDATE albums SET status = ?, updated_at = ? "
                    "WHERE id = ?", (status, store.now_iso(), album_id))
                return self.go_next(album_id) if action != "unignore" \
                    else self.redirect(f"/album/{album_id}")
        finally:
            connection.close()
        if action == "revert":
            result = self.reviewer.revert(album_id)
            return self.redirect(f"/album/{album_id}?" + urlencode(
                {"msg": self.describe(result, "Reverted")}))
        try:
            genres = clean_genres(form.get("genre", []) +
                                  form.get("extra", []))
        except ValueError as error:
            return self.redirect(f"/album/{album_id}?" + urlencode(
                {"msg": f"Nothing written: {error}."}))
        if not genres:
            return self.redirect(f"/album/{album_id}?" + urlencode(
                {"msg": "Nothing written: choose or type at least one "
                        "genre."}))
        result = self.reviewer.apply(album_id, genres)
        if result["errors"]:
            return self.redirect(f"/album/{album_id}?" + urlencode(
                {"msg": self.describe(result, "Wrote")}))
        return self.go_next(album_id, result.get("mpd", ""))

    @staticmethod
    def describe(result: dict, verb: str) -> str:
        """One-line summary of a write result."""
        count = result["written"]
        text = f"{verb} {count} {'track' if count == 1 else 'tracks'}."
        if result["errors"]:
            text += (f" {len(result['errors'])} failed: "
                     + "; ".join(result["errors"][:3]))
        if result.get("mpd"):
            text += " " + result["mpd"]
        return text

    def start_scan(self) -> None:
        """Run a scan and a suggestion pass in the background."""
        import threading

        def work():
            try:
                self.reviewer.scan()
                self.reviewer.suggest_missing()
            except Exception as error:  # shown on the overview
                self.reviewer.status["scan"] = f"failed: {error}"

        threading.Thread(target=work, daemon=True).start()
        self.redirect("/?" + urlencode({"msg": "Scan started."}))

    def bulk_candidates(self):
        """Pending albums whose best suggestion scores at least the minimum."""
        settings = self.reviewer.settings
        connection = self.db()
        try:
            rows = connection.execute(
                "SELECT id, albumartist, album, current_genres, suggestions "
                "FROM albums WHERE status = 'pending' AND suggestions IS NOT "
                f"NULL AND suggestions != '[]' ORDER BY {ORDER}").fetchall()
        finally:
            connection.close()
        candidates = []
        for row in rows:
            found = store.loads(row["suggestions"], [])
            # Only the album's own tags are trusted for bulk writes;
            # artist-level tags often miss what a single album is.
            if found and found[0].get("album_weight", 0) \
                    >= settings.bulk_min_weight:
                candidates.append((row, suggest.preselected(
                    found, settings.max_genres)))
            if len(candidates) >= BULK_LIMIT:
                break
        return candidates

    def bulk_preview(self) -> None:
        """List what bulk accept would write; nothing changes yet."""
        settings = self.reviewer.settings
        candidates = self.bulk_candidates()
        rows = "".join(
            f"<tr><td><input type=\"checkbox\" name=\"album\" "
            f"value=\"{row['id']}\" id=\"a{row['id']}\" checked></td>"
            f"<td><label for=\"a{row['id']}\">"
            f"{esc(row['albumartist'])} - {esc(row['album'])}</label></td>"
            f"<td>{genre_list(store.loads(row['current_genres'], []))}</td>"
            f"<td>{genre_list(genres)}</td></tr>"
            for row, genres in candidates)
        if not candidates:
            body = ("<h1>Bulk accept</h1><p>No waiting album has a "
                    "suggestion from its own Last.fm tags scoring "
                    f"{settings.bulk_min_weight} or more.</p>")
        else:
            body = (
                "<h1>Bulk accept</h1><p>These albums have a suggestion "
                "from the album's own Last.fm tags scoring at least "
                f"{settings.bulk_min_weight}. "
                "Untick any you want to review by hand. Nothing is written "
                f"until you press the button. At most {BULK_LIMIT} albums "
                "are shown at a time.</p>"
                f"<form method=\"post\" action=\"/bulk\">{self.token_field()}"
                "<table><thead><tr><th scope=\"col\">Write</th>"
                "<th scope=\"col\">Album</th><th scope=\"col\">Now</th>"
                "<th scope=\"col\">Will become</th></tr></thead>"
                f"<tbody>{rows}</tbody></table><button type=\"submit\">"
                "Write genres for the ticked albums</button></form>")
        self.send_page("Bulk accept", body)

    def bulk_apply(self, form) -> None:
        """Write the preselected genres for each ticked album."""
        chosen = {int(value) for value in form.get("album", [])
                  if value.isdigit()}
        pairs = [(row["id"], genres) for row, genres in self.bulk_candidates()
                 if row["id"] in chosen and genres]
        self.redirect("/?" + urlencode({"msg": self.apply_many(pairs)}))

    def approve_ticked(self, form) -> None:
        """Approve the albums ticked on a review list with default genres.

        Each album is re-checked here: it must still be in a review list
        and have genres to write, so a stale page cannot write anything
        unexpected.
        """
        name = form.get("filter", ["pending"])[0]
        name = name if name in APPROVABLE else "pending"
        page = form.get("page", ["1"])[0]
        page = page if page.isdigit() else "1"
        chosen = sorted({int(value) for value in form.get("album", [])
                         if value.isdigit()})
        limit = self.reviewer.settings.max_genres
        pairs = []
        connection = self.db()
        try:
            for album_id in chosen:
                row = connection.execute(
                    "SELECT status, suggestions, applied_genres FROM albums "
                    "WHERE id = ?", (album_id,)).fetchone()
                if row is None or row["status"] not in APPROVABLE:
                    continue
                genres = default_genres(
                    row["status"], store.loads(row["suggestions"], None),
                    store.loads(row["applied_genres"], None), limit)
                if genres:
                    pairs.append((album_id, genres))
        finally:
            connection.close()
        message = self.apply_many(pairs) if pairs else \
            "Nothing written: no albums with genres were ticked."
        self.redirect("/albums?" + urlencode(
            {"filter": name, "page": page, "msg": message}))

    def apply_many(self, pairs) -> str:
        """Apply (album id, genres) pairs; returns a one-line summary."""
        written = failed = mpd_failed = 0
        mpd_message = ""
        for album_id, genres in pairs:
            result = self.reviewer.apply(album_id, genres)
            if result["errors"]:
                failed += 1
            else:
                written += 1
            if result.get("mpd"):
                mpd_failed += 1
                mpd_message = result["mpd"]
        message = f"Wrote genres for {written} albums."
        if failed:
            message += (f" {failed} albums had files that could not be "
                        "written; see 'Changed since approval'.")
        if mpd_failed:
            message += (f" MPD was not told about {mpd_failed} albums "
                        f"({mpd_message}); run an MPD library update.")
        return message
