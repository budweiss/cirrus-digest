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
"""
import http.server
import socketserver
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
PAGE = PROJECT_DIR / "out" / "halftime" / "index.html"
HOST = "127.0.0.1"
PORT = 5003


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
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                body = PAGE.read_bytes()
            except FileNotFoundError:
                self._send(503, b"<h1>Not built yet</h1><p>The dashboard has "
                                b"not been generated on this box.</p>")
                return
            self._send(200, body)
            return
        if path == "/healthz":
            self._send(200, b"ok", "text/plain; charset=utf-8")
            return
        self._send(404, b"<h1>404</h1>")

    def do_HEAD(self):
        self.do_GET()

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
    global PAGE
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
