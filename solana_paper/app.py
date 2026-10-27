"""Read-only web service for the independent paper-only Solana runtime."""
from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .engine import PaperRuntime, Settings


def make_handler(runtime: PaperRuntime):
    dashboard = Path(__file__).with_name("dashboard.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "SolanaPaper/1.0"

        def send_bytes(self, status: int, data: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
                             "img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            # HEAD advertises the same headers/length as GET, with no body.
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                self.send_bytes(200, dashboard, "text/html; charset=utf-8")
            elif path == "/api/status":
                body = json.dumps(runtime.status(), ensure_ascii=False, allow_nan=False).encode("utf-8")
                self.send_bytes(200, body, "application/json; charset=utf-8")
            elif path == "/health":
                # Health check reports server liveness even if upstream feed is temporarily down.
                body = json.dumps({"ok": True, "mode": "PAPER_ONLY", "product": "SOLANA_6_AGENTS",
                                   "upstream_error": bool(runtime.last_error)}).encode()
                self.send_bytes(200, body, "application/json")
            else:
                self.send_bytes(404, b'{"error":"Not found"}', "application/json")

        def do_POST(self) -> None:
            # An unauthenticated internet-facing dashboard must not expose money-mutating controls.
            self.send_bytes(405, b'{"error":"Read-only API; paper trading runs by policy"}', "application/json")

        def log_message(self, fmt, *args):
            logging.getLogger("solana_paper.http").info(fmt, *args)

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    runtime = PaperRuntime(Settings.load())
    if os.getenv("PAPER_AUTORUN", "1") == "1":
        runtime.start()
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), make_handler(runtime))
    server.daemon_threads = True
    logging.getLogger("solana_paper").info("Solana SIX Agent PAPER ONLY dashboard on %s:%s", host, port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
