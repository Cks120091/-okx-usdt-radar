from __future__ import annotations

import json
import logging
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import AppConfig
from .models import RadarReport
from .reporting import report_markdown, save_report
from .scanner import MarketScanner


LOGGER = logging.getLogger("okx_radar")


class RadarRuntime:
    def __init__(self, scanner: MarketScanner, config: AppConfig):
        self.scanner = scanner
        self.config = config
        self._scan_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._latest: RadarReport | None = None
        self._running = False
        self._last_error: str | None = None
        self._next_scan_at: float | None = None

    def start_scheduler(self) -> None:
        thread = threading.Thread(target=self._schedule_loop, name="radar-scheduler", daemon=True)
        thread.start()

    def stop(self) -> None:
        self._stop.set()

    def trigger_scan(self) -> bool:
        with self._state_lock:
            if self._running:
                return False
            self._running = True
        thread = threading.Thread(target=self._scan_worker, name="radar-manual-scan", daemon=True)
        thread.start()
        return True

    def scan_blocking(self) -> RadarReport:
        with self._state_lock:
            self._running = True
        try:
            return self._perform_scan()
        finally:
            with self._state_lock:
                self._running = False

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "running": self._running,
                "last_error": self._last_error,
                "next_scan_at_unix": self._next_scan_at,
                "has_report": self._latest is not None,
                "latest_status": self._latest.status if self._latest else None,
                "latest_generated_at": self._latest.generated_at if self._latest else None,
                "analysis_only": True,
                "auto_ordering": False,
            }

    def latest_dict(self) -> dict[str, Any] | None:
        with self._state_lock:
            return self._latest.to_dict() if self._latest else None

    def latest_markdown(self) -> str | None:
        with self._state_lock:
            return report_markdown(self._latest) if self._latest else None

    def _schedule_loop(self) -> None:
        if self.config.scan_at_start:
            self.trigger_scan()
        while not self._stop.is_set():
            now = time.time()
            if self.config.align_to_hour:
                next_run = (int(now // 3600) + 1) * 3600
            else:
                next_run = now + self.config.interval_seconds
            with self._state_lock:
                self._next_scan_at = next_run
            if self._stop.wait(max(next_run - time.time(), 0.1)):
                break
            self.trigger_scan()

    def _scan_worker(self) -> None:
        try:
            self._perform_scan()
        finally:
            with self._state_lock:
                self._running = False

    def _perform_scan(self) -> RadarReport:
        with self._scan_lock:
            LOGGER.info("Starting complete OKX USDT perpetual scan")
            try:
                report = self.scanner.scan_once()
                save_report(report, self.config.data_dir)
                with self._state_lock:
                    self._latest = report
                    self._last_error = None
                LOGGER.info(
                    "Scan finished: status=%s coverage=%.2f signals=%d",
                    report.status,
                    report.coverage_pct,
                    len(report.signals),
                )
                return report
            except Exception as exc:
                LOGGER.exception("Unexpected scanner failure")
                with self._state_lock:
                    self._last_error = str(exc)
                raise


def serve(runtime: RadarRuntime, host: str, port: int) -> None:
    dashboard_path = Path(__file__).parent / "static" / "pages.html"
    dashboard = dashboard_path.read_text(encoding="utf-8").replace(
        "</head>",
        "<script>window.RADAR_DATA_URL='/api/report/latest';</script></head>",
        1,
    ).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "OKXRadar/0.1"

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/":
                self._send_bytes(HTTPStatus.OK, dashboard, "text/html; charset=utf-8")
            elif path == "/health":
                self._send_json(HTTPStatus.OK, {"ok": True, **runtime.status()})
            elif path == "/api/status":
                self._send_json(HTTPStatus.OK, runtime.status())
            elif path == "/api/report/latest":
                payload = runtime.latest_dict()
                if payload is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "尚未完成第一輪掃描"})
                else:
                    self._send_json(HTTPStatus.OK, payload)
            elif path == "/api/report/latest.md":
                markdown = runtime.latest_markdown()
                if markdown is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "尚未完成第一輪掃描"})
                else:
                    self._send_bytes(HTTPStatus.OK, markdown.encode("utf-8"), "text/markdown; charset=utf-8")
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path != "/api/scan":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            if runtime.trigger_scan():
                self._send_json(HTTPStatus.ACCEPTED, {"accepted": True, "message": "已開始完整掃描"})
            else:
                self._send_json(HTTPStatus.CONFLICT, {"accepted": False, "message": "掃描正在執行中"})

        def log_message(self, format_string: str, *args: Any) -> None:
            LOGGER.info("HTTP %s - %s", self.address_string(), format_string % args)

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send_bytes(status, body, "application/json; charset=utf-8")

        def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status.value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((host, port), Handler)
    runtime.start_scheduler()
    LOGGER.info("Radar dashboard listening on http://%s:%d", host, port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        runtime.stop()
        server.server_close()
