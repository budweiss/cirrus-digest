#!/usr/bin/env python3
"""halftime_serve.py — serve the halftime dashboard, and nothing else.

S79. Binds to 127.0.0.1 only; the outside world reaches it through the CUMULUS
tunnel with Cloudflare Access in front.

DELIBERATELY NOT A STATIC FILE SERVER. It serves ONE hard-coded path and 404s
everything else. There is no directory root, no path parameter, no
`send_from_directory`, no `safe_join`. That is a security property this tree
already relies on: the S79 credential-exposure review concluded that plaintext
credential copies on these boxes are not reachable from the internet partly
BECAUSE no app anywhere can serve a file chosen by the requester. A general
static server here would quietly retire that guarantee, and the next person to
audit it would have to rediscover why it no longer holds.

If this ever needs to serve a second file, add a second explicit route. Do not
add a directory.

S271 — ONE WRITE ROUTE (R29, the history log). `POST /history`, exact match,
and nothing else accepts a body. It is the first place a client can write on
this box, so every gate is here and each refuses on its own:

  * ORIGIN must be the dashboard's own host -- a form on any other site cannot
    post into the log with the viewer's Access session (CSRF);
  * the IDENTITY Cloudflare Access attaches (Cf-Access-Authenticated-User-Email)
    must be present -- it is recorded on the entry, and a request that did not
    come through Access has none;
  * the BODY is form-encoded, has a declared length, and is small;
  * the FIELDS are validated in halftime_dashboard.history_entry: a played game
    on this season's slate, bounded printable text, a real dollar figure.

It writes to ONE file, data/halftime/history.jsonl, append-only; the unit's
sandbox grants write access to that directory and nothing else.

GET / now renders the page from the snapshot plus the log on each request, so
a saved entry shows at once without this process ever writing the page file.
If that render fails for any reason it serves the built index.html instead --
the page a client sees must never be a 500 because the log is new.
"""
import http.server
import json
import re
import socketserver
import sys
import urllib.parse
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
PAGE = PROJECT_DIR / "out" / "halftime" / "index.html"
SNAPSHOT = PROJECT_DIR / "out" / "halftime" / "snapshot.json"
HISTORY = None          # None = halftime_dashboard.HISTORY_PATH; tests override
TODAY = None            # None = the real date; tests pin it
HOST = "127.0.0.1"
PORT = 5003
ALLOWED_ORIGIN = "https://halftime.cirrustask.com"
IDENTITY_HEADER = "Cf-Access-Authenticated-User-Email"
MAX_BODY = 4096
_GAME_ID = re.compile(r"^wk\d{2}-[a-z]{1,20}$")


def _dashboard():
    import halftime_dashboard
    return halftime_dashboard


def _history_path():
    return HISTORY if HISTORY is not None else _dashboard().HISTORY_PATH


def render_live(saved=None):
    """The page with the log as it stands now, or None to fall back."""
    try:
        hd = _dashboard()
        snap = json.loads(SNAPSHOT.read_text())
        ids = {g.get("game_id") for g in snap.get("games", [])}
        return hd.render_html(snap, hd.load_history(_history_path()),
                              saved=saved if saved in ids else None).encode()
    except Exception as e:
        sys.stderr.write("live render failed, serving the built page: %s\n"
                         % type(e).__name__)
        return None


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "halftime/1.0"

    def _send(self, code, body: bytes, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # The page carries client research. Never let a proxy or browser keep
        # a copy that outlives the Access session.
        self.send_header("Cache-Control", "no-store, private")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        # Exact match only. No prefix matching, no normalisation, no traversal
        # surface — "/" is the whole API.
        path, _, query = self.path.partition("?")
        if path in ("/", "/index.html"):
            saved = (urllib.parse.parse_qs(query).get("saved") or [None])[0]
            body = render_live(saved if saved and _GAME_ID.match(saved)
                               else None)
            if body is None:
                try:
                    body = PAGE.read_bytes()
                except FileNotFoundError:
                    self._send(503, b"<h1>Not built yet</h1><p>The dashboard "
                                    b"has not been generated on this box.</p>")
                    return
            self._send(200, body)
            return
        if path == "/healthz":
            self._send(200, b"ok", "text/plain; charset=utf-8")
            return
        self._send(404, b"<h1>404</h1>")

    def do_HEAD(self):
        self.do_GET()

    def _refuse(self, code, why):
        # S273: every refusal is logged with its reason. Until now a client
        # whose save failed was invisible to us -- the journal showed only
        # "POST /history". Reasons are fixed strings; no field a client typed
        # and no email address is ever written here.
        sys.stderr.write("history: refused %d -- %s\n" % (code, why))
        import html
        self._send(code, ("<h1>Not saved</h1><p>{}</p><p><a href='/'>Back to "
                          "the dashboard</a></p>".format(html.escape(why))
                          ).encode())

    def do_POST(self):
        """The history log, and nothing else. Each gate refuses on its own."""
        if self.path != "/history":
            self._send(404, b"<h1>404</h1>")
            return
        if self.headers.get("Origin") != ALLOWED_ORIGIN:
            self._refuse(403, "This form only accepts entries from the "
                              "dashboard page itself.")
            return
        who = (self.headers.get(IDENTITY_HEADER) or "").strip()
        if not who:
            self._refuse(403, "No signed-in identity came with this request.")
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype != "application/x-www-form-urlencoded":
            self._refuse(415, "Unexpected form encoding.")
            return
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            self._refuse(411, "The request did not say how long it was.")
            return
        if not 0 < length <= MAX_BODY:
            self._refuse(413, "That entry is too long to save.")
            return
        try:
            raw = self.rfile.read(length).decode("utf-8")
            fields = urllib.parse.parse_qs(raw, keep_blank_values=True,
                                           max_num_fields=8)
        except (UnicodeDecodeError, ValueError):
            self._refuse(400, "The form could not be read.")
            return
        form = {k: v[0] for k, v in fields.items()}
        try:
            hd = _dashboard()
            snap = json.loads(SNAPSHOT.read_text())
            entry, why = hd.history_entry(form, snap, who, today=TODAY)
            if entry is None:
                self._refuse(400, why)
                return
            hd.append_history(entry, _history_path())
        except Exception as e:
            sys.stderr.write("history save failed: %s\n" % type(e).__name__)
            self._refuse(500, "The entry could not be saved. Nothing was "
                              "written; please try again.")
            return
        gid = entry["game_id"]
        sys.stderr.write("history: saved %s\n" % gid)
        self.send_response(303)
        self.send_header("Location", "/?saved={0}#log-{0}".format(gid))
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store, private")
        self.end_headers()

    def log_message(self, fmt, *args):
        # Default logging writes the request line to stderr, which lands in the
        # journal. Keep it, but never echo the query string.
        sys.stderr.write("%s - %s\n" % (self.address_string(),
                                        (args[0] if args else "").split("?")[0]))


def selftest() -> int:
    import tempfile
    import threading
    import urllib.request
    import urllib.error
    global PAGE, SNAPSHOT, HISTORY, TODAY
    failures = []

    def check(label, ok):
        print(("  PASS  " if ok else "  FAIL  ") + label)
        if not ok:
            failures.append(label)

    with tempfile.TemporaryDirectory() as td:
        secret = Path(td) / "credentials.json"
        secret.write_text('{"nope": "should never be reachable"}')
        page = Path(td) / "index.html"
        page.write_text("<h1>dashboard</h1>")
        PAGE = page
        # T32: live render reads SNAPSHOT and the log. Point both at the temp
        # dir BEFORE the first request, or these GETs would render the box's
        # real snapshot. A path never created = "no snapshot" = fallback.
        SNAPSHOT = Path(td) / "no-snapshot.json"
        HISTORY = Path(td) / "data" / "history.jsonl"

        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv:
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            base = "http://127.0.0.1:%d" % port

            def get(p):
                try:
                    with urllib.request.urlopen(base + p, timeout=5) as r:
                        return r.status, r.read(), dict(r.headers)
                except urllib.error.HTTPError as e:
                    return e.code, e.read(), dict(e.headers)

            code, body, hdrs = get("/")
            check("the dashboard is served at /", code == 200
                  and b"dashboard" in body)
            check("it is marked no-store, so no proxy keeps client research",
                  "no-store" in hdrs.get("Cache-Control", ""))
            check("healthz answers for monitoring", get("/healthz")[0] == 200)
            check("an unknown path is 404, not a directory listing",
                  get("/anything")[0] == 404)

            # The property this file exists to protect.
            for probe in ("/../credentials.json",
                          "/%2e%2e/credentials.json",
                          "/config/credentials.json",
                          "/out/halftime/../../config/credentials.json",
                          "//etc/passwd"):
                code, body, _ = get(probe)
                check("traversal %s is refused" % probe,
                      code == 404 and b"should never be reachable" not in body)

            check("a query string cannot select a different file",
                  get("/?file=credentials.json")[1] == b"<h1>dashboard</h1>")
            srv.shutdown()

        PAGE = Path(td) / "missing.html"
        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv2:
            port = srv2.server_address[1]
            threading.Thread(target=srv2.serve_forever, daemon=True).start()
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=5)
                got = 200
            except urllib.error.HTTPError as e:
                got = e.code
            check("an unbuilt page says SO (503), it does not 200 an empty page",
                  got == 503)
            srv2.shutdown()

        # --- R29: the history log ---------------------------------------
        import halftime_dashboard as hd
        PAGE = page
        SNAPSHOT = Path(td) / "snapshot.json"
        TODAY = "2026-09-20"            # Week 1 played, Week 15 not
        snap = hd.build_snapshot(today=TODAY, db_path=str(Path(td) / "kb.db"),
                                 routing_path=Path(td) / "never.json",
                                 itinerary_path=Path(td) / "never-itin.json")
        SNAPSHOT.write_text(json.dumps(snap))
        check("the log is NOT under out/ -- build output is rewritten nightly",
              "out" not in hd.HISTORY_PATH.relative_to(
                  hd.PROJECT_DIR).parts)

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(_NoRedirect)

        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv3:
            port = srv3.server_address[1]
            threading.Thread(target=srv3.serve_forever, daemon=True).start()
            base = "http://127.0.0.1:%d" % port
            ok_hdrs = {"Origin": ALLOWED_ORIGIN,
                       IDENTITY_HEADER: "justin@example.com",
                       "Content-Type": "application/x-www-form-urlencoded"}

            def post(path, fields=None, headers=None, raw=None):
                data = raw if raw is not None else urllib.parse.urlencode(
                    fields or {}).encode()
                req = urllib.request.Request(base + path, data=data,
                                             headers=headers or {},
                                             method="POST")
                try:
                    with opener.open(req, timeout=5) as r:
                        return r.status, r.read(), dict(r.headers)
                except urllib.error.HTTPError as e:
                    return e.code, e.read(), dict(e.headers)

            def lines():
                try:
                    return HISTORY.read_text().splitlines()
                except FileNotFoundError:
                    return []

            good = {"game_id": "wk01-falcons", "what": "Drumline <b>+</b> flyover",
                    "cost": "$12,500", "vof": "8.1 / 10"}
            code, _, hdrs = post("/history", good, ok_hdrs)
            check("log: a valid entry saves and redirects back to it",
                  code == 303 and hdrs.get("Location")
                  == "/?saved=wk01-falcons#log-wk01-falcons")
            row = json.loads(lines()[0]) if lines() else {}
            check("log: the entry records WHO, from the Access identity",
                  row.get("entered_by") == "justin@example.com"
                  and row.get("cost") == 12500)
            code, body, _ = get("/?saved=wk01-falcons")
            check("log: the page shows it at once, opened, marked saved",
                  code == 200 and b"Saved." in body and b"8.1 / 10" in body
                  and b"<details open>" in body)
            check("log: what a client typed is escaped on the page",
                  b"<b>+</b>" not in body and b"&lt;b&gt;" in body)

            n = len(lines())
            # Each gate is asserted by its OWN status code: the validator
            # behind them refuses most of these too (400), so "any 4xx" could
            # not tell a missing gate from a working one.
            for label, want, args in (
                    ("a cross-site POST (CSRF)", 403,
                     dict(fields=good, headers=dict(ok_hdrs,
                          Origin="https://evil.example"))),
                    ("a POST with no Origin", 403,
                     dict(fields=good, headers={k: v for k, v in ok_hdrs.items()
                                                if k != "Origin"})),
                    ("a POST with no Access identity", 403,
                     dict(fields=good, headers={k: v for k, v in ok_hdrs.items()
                                                if k != IDENTITY_HEADER})),
                    ("a JSON body", 415,
                     dict(raw=b'{"game_id":"wk01-falcons"}',
                          headers=dict(ok_hdrs, **{"Content-Type":
                                                   "application/json"}))),
                    ("an oversized body", 413,
                     dict(raw=b"what=" + b"x" * (MAX_BODY + 10),
                          headers=ok_hdrs)),
                    ("a game not played yet", 400,
                     dict(fields=dict(good, game_id="wk15-ravens"),
                          headers=ok_hdrs)),
                    ("an unknown game", 400,
                     dict(fields=dict(good, game_id="wk99-nobody"),
                          headers=ok_hdrs))):
                code = post("/history", **args)[0]
                check("log: %s is refused (%d), nothing written" % (label, code),
                      code == want and len(lines()) == n)
            check("log: POST anywhere else is 404",
                  post("/", good, ok_hdrs)[0] == 404
                  and post("/history/../x", good, ok_hdrs)[0] == 404
                  and len(lines()) == n)
            check("log: a 'saved' value that is not a game id is not reflected",
                  b"<script>" not in get("/?saved=<script>")[1])

            SNAPSHOT.write_text("{not json")
            code, body, _ = get("/")
            check("log: if the live render breaks, the built page is served",
                  code == 200 and body == b"<h1>dashboard</h1>")
            srv3.shutdown()
        TODAY = None

    print()
    if failures:
        print("FAILURES: %d" % len(failures))
        return 1
    print("ALL PASS")
    return 0


def main() -> int:
    if "selftest" in sys.argv[1:]:
        return selftest()
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((HOST, PORT), Handler) as srv:
        print("halftime_serve on %s:%d serving %s" % (HOST, PORT, PAGE),
              flush=True)
        srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
