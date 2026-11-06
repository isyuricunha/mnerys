"""Isolated loopback-only static fixture for Mnerys Gateway browser tests.

Serves ONLY the repository's public /static/ assets and a minimal DOM shell.
No app initialization, databases, model providers, MCPs or user settings.
The test runner supplies all /api responses through Playwright interception.
"""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import argparse

STATIC = (Path(__file__).resolve().parents[3] / "static").resolve()
SHELL = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mnerys isolated UI test</title></head><body>
<div id="chat-history" role="log"></div>
<textarea id="message"></textarea><button class="send-btn" type="button">Send</button>
</body></html>"""


class FixtureHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/login"):
            payload = SHELL.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return
        if not path.startswith("/static/"):
            self.send_error(404)
            return
        super().do_GET()

    def translate_path(self, path):
        relative = urlsplit(path).path.removeprefix("/static/")
        candidate = (STATIC / relative).resolve()
        if not candidate.is_relative_to(STATIC) or not candidate.is_file():
            return "/nonexistent-mnerys-static-fixture"
        return str(candidate)

    def log_message(self, format, *args):
        # Do not log request headers, test payloads or client identifiers.
        return


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7014)
    args = parser.parse_args()
    if not (1024 <= args.port <= 65535):
        raise SystemExit("Unsupported isolated test port")
    with ThreadingHTTPServer(("127.0.0.1", args.port), FixtureHandler) as server:
        server.serve_forever()
