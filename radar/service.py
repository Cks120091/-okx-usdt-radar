from __future__ import annotations

import inspect
import json
import logging
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import AppConfig
from .models import RadarReport
from .reporting import load_latest_report, report_markdown, save_report
from .scanner import MarketScanner


LOGGER = logging.getLogger("okx_radar")


class RadarRuntime:
    """Single-scan runtime with persisted reports and an optional core preview."""

    def __init__(self, scanner: MarketScanner, config: AppConfig):
        self.scanner = scanner
        self.config = config
        self._scan_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._latest: RadarReport | None = load_latest_report(config.data_dir)
        self._preview: RadarReport | None = None
        self._running = False
        self._last_error: str | None = None
        self._last_attempt_status = "RESTORED" if self._latest is not None else "IDLE"
        self._scan_id: str | None = None
        self._scan_started_at: str | None = None
        self._progress: dict[str, Any] = self._idle_progress()

    def stop(self) -> None:
        return

    def trigger_scan(self) -> bool:
        with self._state_lock:
            if self._running:
                return False
            self._begin_scan_locked()
        thread = threading.Thread(
            target=self._scan_worker,
            name="radar-on-demand-scan",
            daemon=True,
        )
        thread.start()
        return True

    def scan_blocking(self) -> RadarReport:
        with self._state_lock:
            if self._running:
                raise RuntimeError("scan already running")
            self._begin_scan_locked()
        try:
            return self._perform_scan()
        finally:
            with self._state_lock:
                self._running = False

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            system_status, age_seconds, stale = self._system_status_locked()
            return {
                "running": self._running,
                "system_status": system_status,
                "runtime_status": system_status,
                "data_status": "STALE" if stale else "FRESH" if self._latest else "NONE",
                "actionable": system_status == "FRESH",
                "last_error": self._last_error,
                "last_attempt_status": self._last_attempt_status,
                "has_report": self._latest is not None,
                "has_preview": self._preview is not None and self._running,
                "latest_status": self._latest.status if self._latest else None,
                "latest_generated_at": self._latest.generated_at if self._latest else None,
                "latest_age_seconds": age_seconds,
                "stale_after_seconds": self.config.stale_after_seconds,
                "scan_id": self._scan_id,
                "scan_started_at": self._scan_started_at,
                "progress": dict(self._progress),
                "analysis_only": True,
                "auto_ordering": False,
            }

    def latest_dict(self) -> dict[str, Any] | None:
        with self._state_lock:
            if self._latest is None:
                return None
            payload = self._latest.to_dict()
            system_status, age_seconds, _ = self._system_status_locked()
            actionable = system_status == "FRESH" and self._latest.status != "DATA_INCOMPLETE"
            payload["runtime_status"] = system_status
            payload["actionable"] = actionable
            payload["latest_age_seconds"] = age_seconds
            payload["max_signals"] = self.config.max_signals
            if not actionable:
                payload["historical_signal_count"] = len(payload.get("signals", []))
                payload["historical_long_signal_count"] = len(
                    payload.get("long_signals", [])
                )
                payload["signals"] = []
                payload["long_signals"] = []
                payload["signals_suppressed_reason"] = system_status
                payload["safety"]["actionable"] = False
            else:
                payload["signals_suppressed_reason"] = None
            return payload

    def preview_dict(self) -> dict[str, Any] | None:
        with self._state_lock:
            if not self._running or self._preview is None:
                return None
            payload = self._preview.to_dict()
            payload["runtime_status"] = "CORE_PREVIEW"
            payload["actionable"] = True
            payload["preliminary"] = True
            payload["deep_data_pending"] = True
            payload["signals_suppressed_reason"] = None
            payload["safety"]["actionable"] = True
            return payload

    def statistics(self) -> dict[str, Any]:
        repository = getattr(self.scanner, "repository", None)
        if repository is None:
            return {
                "available": False,
                "note": "Signal Repository 尚未啟用；禁止顯示假勝率。",
            }
        return repository.performance()

    def latest_markdown(self) -> str | None:
        with self._state_lock:
            if self._latest is None:
                return None
            system_status, _, _ = self._system_status_locked()
            if system_status != "FRESH":
                labels = {
                    "SCANNING": "掃描中，舊正式訊號已暫停使用。",
                    "STALE": "資料已過期，禁止依此進場。",
                    "ERROR": "最新掃描失敗，舊正式訊號已暫停使用。",
                    "BOOTING": "雷達啟動中，尚無可用訊號。",
                }
                return "# OKX USDT 永續雷達\n\n" + labels.get(
                    system_status,
                    "目前沒有可使用的正式訊號。",
                )
            return report_markdown(self._latest)

    def _begin_scan_locked(self) -> None:
        self._running = True
        self._last_attempt_status = "SCANNING"
        self._scan_id = str(uuid.uuid4())
        self._scan_started_at = datetime.now(timezone.utc).isoformat()
        self._preview = None
        self._progress = {
            "phase": "STARTING",
            "completed": 0,
            "total": None,
            "percent": None,
            "message": "正在啟動雷達並取得最新市場資料",
        }

    def _scan_worker(self) -> None:
        try:
            self._perform_scan()
        finally:
            with self._state_lock:
                self._running = False

    def _perform_scan(self) -> RadarReport:
        with self._scan_lock:
            with self._state_lock:
                scan_id = self._scan_id or str(uuid.uuid4())
                started_at = self._scan_started_at or datetime.now(timezone.utc).isoformat()
            LOGGER.info("Starting on-demand OKX USDT perpetual scan id=%s", scan_id)
            try:
                scan_kwargs: dict[str, Any] = {
                    "progress": self._update_progress,
                    "scan_id": scan_id,
                }
                parameters = inspect.signature(self.scanner.scan_once).parameters
                if "preview" in parameters:
                    scan_kwargs["preview"] = self._publish_preview
                try:
                    report = self.scanner.scan_once(**scan_kwargs)
                finally:
                    self._release_scanner_transient_data()
                completed_at = datetime.now(timezone.utc).isoformat()
                report = replace(
                    report,
                    scan_id=scan_id,
                    scan_started_at=started_at,
                    generated_at=completed_at,
                    completed_at=completed_at,
                    runtime_status=(
                        "ERROR" if report.status == "DATA_INCOMPLETE" else "FRESH"
                    ),
                    actionable=report.status != "DATA_INCOMPLETE",
                    signals=([] if report.status == "DATA_INCOMPLETE" else report.signals),
                    long_signals=(
                        []
                        if report.status == "DATA_INCOMPLETE"
                        else report.long_signals
                    ),
                    max_signals=self.config.max_signals,
                )
                save_report(report, self.config.data_dir)
                with self._state_lock:
                    self._latest = report
                    self._preview = None
                    if report.status == "DATA_INCOMPLETE":
                        self._last_error = report.message
                        self._last_attempt_status = "ERROR"
                    else:
                        self._last_error = None
                        self._last_attempt_status = "SUCCESS"
                    self._progress = {
                        "phase": "COMPLETED",
                        "completed": 1,
                        "total": 1,
                        "percent": 100.0,
                        "message": (
                            "最新市場掃描完成"
                            if report.status != "DATA_INCOMPLETE"
                            else "最新市場掃描失敗"
                        ),
                    }
                LOGGER.info(
                    "Scan finished: id=%s status=%s coverage=%.2f signals=%d",
                    scan_id,
                    report.status,
                    report.coverage_pct,
                    len(report.signals),
                )
                return report
            except Exception as exc:
                LOGGER.exception("Unexpected scanner failure")
                with self._state_lock:
                    self._preview = None
                    self._last_error = str(exc)
                    self._last_attempt_status = "ERROR"
                    self._progress = {
                        "phase": "ERROR",
                        "completed": None,
                        "total": None,
                        "percent": None,
                        "message": "最新掃描失敗",
                    }
                raise

    def _release_scanner_transient_data(self) -> None:
        release = getattr(self.scanner, "release_transient_data", None)
        if not callable(release):
            return
        try:
            released = release()
            LOGGER.info("Released %s cached candle series", released)
        except Exception:
            LOGGER.exception("Unable to release transient scanner data")

    def _publish_preview(self, report: RadarReport) -> None:
        with self._state_lock:
            if not self._running or report.scan_id != self._scan_id:
                return
            self._preview = report
            self._progress = {
                "phase": "CORE_PREVIEW",
                "completed": 1,
                "total": 1,
                "percent": 100.0,
                "message": "15m 核心結果已發布，正在補深度資料與長線雷達",
            }

    def _update_progress(
        self,
        phase: str,
        completed: int | None,
        total: int | None,
        message: str,
    ) -> None:
        percent = (
            round(completed / total * 100.0, 1)
            if completed is not None and total not in (None, 0)
            else None
        )
        with self._state_lock:
            self._progress = {
                "phase": phase,
                "completed": completed,
                "total": total,
                "percent": percent,
                "message": message,
            }

    def _system_status_locked(self) -> tuple[str, float | None, bool]:
        age_seconds = self._report_age_seconds_locked()
        stale = age_seconds is not None and age_seconds > self.config.stale_after_seconds
        if self._running:
            return "SCANNING", age_seconds, stale
        # If a new attempt failed while the retained report is already old, STALE is
        # the primary safety state. ``last_error`` still explains the failed attempt.
        if self._latest is not None and stale:
            return "STALE", age_seconds, True
        if self._last_attempt_status == "ERROR" or self._last_error:
            return "ERROR", age_seconds, stale
        if self._latest is None:
            return "BOOTING", None, False
        if self._latest.status == "DATA_INCOMPLETE":
            return "ERROR", age_seconds, stale
        return "FRESH", age_seconds, False

    def _report_age_seconds_locked(self) -> float | None:
        if self._latest is None:
            return None
        try:
            completed = datetime.fromisoformat(
                self._latest.completed_at or self._latest.generated_at
            )
            if completed.tzinfo is None:
                completed = completed.replace(tzinfo=timezone.utc)
            return max(0.0, time.time() - completed.timestamp())
        except (TypeError, ValueError):
            return float("inf")

    @staticmethod
    def _idle_progress() -> dict[str, Any]:
        return {
            "phase": "IDLE",
            "completed": None,
            "total": None,
            "percent": None,
            "message": "等待排程或使用者要求最新市場掃描",
        }


def serve(runtime: RadarRuntime, host: str, port: int) -> None:
    static_dir = Path(__file__).parent / "static"
    dashboard_path = static_dir / "pages.html"
    dashboard = dashboard_path.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "OKXRadar/3.3"

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/":
                self._send_bytes(HTTPStatus.OK, dashboard, "text/html; charset=utf-8")
            elif path == "/manifest.webmanifest":
                self._send_bytes(
                    HTTPStatus.OK,
                    (static_dir / "manifest.webmanifest").read_bytes(),
                    "application/manifest+json; charset=utf-8",
                )
            elif path == "/service-worker.js":
                self._send_bytes(
                    HTTPStatus.OK,
                    (static_dir / "service-worker.js").read_bytes(),
                    "application/javascript; charset=utf-8",
                )
            elif path == "/radar-icon.svg":
                self._send_bytes(
                    HTTPStatus.OK,
                    (static_dir / "radar-icon.svg").read_bytes(),
                    "image/svg+xml; charset=utf-8",
                )
            elif path == "/health":
                self._send_json(HTTPStatus.OK, {"ok": True, **runtime.status()})
            elif path == "/api/status":
                self._send_json(HTTPStatus.OK, runtime.status())
            elif path == "/api/report/latest":
                payload = runtime.latest_dict()
                if payload is None:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {
                            "error": "尚未完成第一輪掃描",
                            "runtime_status": runtime.status()["system_status"],
                        },
                    )
                else:
                    self._send_json(HTTPStatus.OK, payload)
            elif path == "/api/report/preview":
                payload = runtime.preview_dict()
                if payload is None:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "本輪 15m 核心結果尚未完成"},
                    )
                else:
                    self._send_json(HTTPStatus.OK, payload)
            elif path == "/api/report/latest.md":
                markdown = runtime.latest_markdown()
                if markdown is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "尚未完成第一輪掃描"})
                else:
                    self._send_bytes(
                        HTTPStatus.OK,
                        markdown.encode("utf-8"),
                        "text/markdown; charset=utf-8",
                    )
            elif path == "/api/stats":
                self._send_json(HTTPStatus.OK, runtime.statistics())
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path != "/api/scan":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            started = runtime.trigger_scan()
            status = runtime.status()
            self._send_json(
                HTTPStatus.ACCEPTED,
                {
                    "accepted": started,
                    "joined_existing_scan": not started,
                    "scan_id": status["scan_id"],
                    "runtime_status": "SCANNING",
                    "message": (
                        "已開始完整掃描"
                        if started
                        else "掃描正在執行，已加入目前進度"
                    ),
                },
            )

        def log_message(self, format_string: str, *args: Any) -> None:
            LOGGER.info("HTTP %s - %s", self.address_string(), format_string % args)

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self._send_bytes(status, body, "application/json; charset=utf-8")

        def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status.value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((host, port), Handler)
    LOGGER.info("Radar dashboard listening on http://%s:%d", host, port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        runtime.stop()
        server.server_close()
