from __future__ import annotations

import inspect
import json
import logging
import re
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import AppConfig
from .models import RadarReport
from .preflight import build_preflight_payload
from .public_payload import public_candidate_payload, public_report_payload
from .push import PushSubscriptionError, build_push_notifier
from .reporting import load_latest_report, report_markdown, save_report
from .scanner import MarketScanner


LOGGER = logging.getLogger("okx_radar")


def _normalize_scan_mode(value: Any) -> str:
    mode = str(value or "FULL").strip().upper()
    aliases = {
        "15M": "SHORT",
        "SHORT": "SHORT",
        "4H": "LONG",
        "LONG": "LONG",
        "ALL": "FULL",
        "FULL": "FULL",
    }
    normalized = aliases.get(mode)
    if normalized is None:
        raise ValueError("scan_mode must be SHORT, LONG, or FULL")
    return normalized


_SCAN_MODE_LABELS = {
    "SHORT": "15m 掃描",
    "LONG": "4H 掃描",
    "FULL": "全市場掃描（15m＋4H）",
}

_SCAN_MODE_HORIZONS = {
    "SHORT": frozenset({"SHORT"}),
    "LONG": frozenset({"LONG"}),
    "FULL": frozenset({"SHORT", "LONG"}),
}


def _single_scan_failure_message(exc: Exception) -> str:
    messages: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(messages) < 6:
        messages.append(str(current))
        current = current.__cause__
    detail = " ".join(messages).lower()
    if "timed out" in detail or "connection refused" in detail or "502" in detail:
        return (
            "OKX 公開行情目前連線失敗；系統已嘗試官方主端點與備援端點。"
            "這不是幣種或訊號失效，請稍後再試。"
        )
    if "429" in detail or "code=50011" in detail or "rate limit" in detail:
        return "OKX 暫時限制請求頻率；這不是幣種失效，請約 10 秒後再試。"
    if "k 線" in detail:
        return (
            "OKX 最新 K 線目前無法完整取得；不是訊號失效。"
            "系統沒有拿缺漏週期硬算，請稍後再試。"
        )
    return "OKX 最新單幣資料暫時無法完成分析；這不是幣種失效，請稍後再試。"


class PreflightError(RuntimeError):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


class RadarRuntime:
    """Single-scan runtime with persisted reports and an optional core preview."""

    def __init__(
        self,
        scanner: MarketScanner,
        config: AppConfig,
        *,
        push_notifier: Any | None = None,
    ):
        self.scanner = scanner
        self.config = config
        self.push_notifier = push_notifier or build_push_notifier()
        self._scan_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._preflight_lock = threading.Lock()
        self._preflight_cache: dict[
            tuple[str, str, str], tuple[float, dict[str, Any]]
        ] = {}
        self._invalidated_preflight_signals: dict[
            tuple[str, str, str], Any
        ] = {}
        self._preflight_cache_ttl_seconds = 12.0
        self._latest: RadarReport | None = load_latest_report(config.data_dir)
        self._preview: RadarReport | None = None
        self._running = False
        self._last_error: str | None = None
        self._last_attempt_status = "RESTORED" if self._latest is not None else "IDLE"
        self._scan_id: str | None = None
        self._scan_started_at: str | None = None
        self._scan_mode = "FULL"
        self._scan_push_subscriptions: dict[str, dict[str, Any]] = {}
        self._max_scan_push_subscriptions = 8
        self._progress: dict[str, Any] = self._idle_progress()

    def stop(self) -> None:
        return

    def trigger_scan(
        self,
        push_subscription: Any | None = None,
        scan_mode: str = "FULL",
    ) -> bool:
        normalized_mode = _normalize_scan_mode(scan_mode)
        normalized_subscription = (
            self.push_notifier.normalize_subscription(push_subscription)
            if push_subscription is not None
            else None
        )
        with self._state_lock:
            if self._running:
                if normalized_subscription is not None:
                    self._register_scan_push_locked(normalized_subscription)
                return False
            self._begin_scan_locked(normalized_mode)
            if normalized_subscription is not None:
                self._register_scan_push_locked(normalized_subscription)
        thread = threading.Thread(
            target=self._scan_worker,
            name="radar-on-demand-scan",
            daemon=True,
        )
        thread.start()
        return True

    def push_config(self) -> dict[str, Any]:
        return self.push_notifier.public_config()

    def scan_blocking(self, scan_mode: str = "FULL") -> RadarReport:
        normalized_mode = _normalize_scan_mode(scan_mode)
        with self._state_lock:
            if self._running:
                raise RuntimeError("scan already running")
            self._begin_scan_locked(normalized_mode)
        try:
            return self._perform_scan()
        finally:
            with self._state_lock:
                self._running = False

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            system_status, age_seconds, stale = self._system_status_locked()
            horizon_freshness = {
                horizon: self._horizon_freshness_locked(horizon)
                for horizon in ("SHORT", "LONG")
            }
            return {
                "running": self._running,
                "system_status": system_status,
                "runtime_status": system_status,
                "data_status": "STALE" if stale else "FRESH" if self._latest else "NONE",
                "actionable": system_status == "FRESH",
                "snapshot_expired": system_status == "STALE",
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
                "scan_mode": self._scan_mode,
                "scan_mode_label": _SCAN_MODE_LABELS[self._scan_mode],
                "horizon_freshness": horizon_freshness,
                "progress": dict(self._progress),
                "analysis_only": True,
                "auto_ordering": False,
            }

    def latest_dict(self) -> dict[str, Any] | None:
        with self._state_lock:
            if self._latest is None:
                return None
            payload = public_report_payload(self._latest)
            system_status, age_seconds, _ = self._system_status_locked()
            horizon_freshness = {
                horizon: self._horizon_freshness_locked(horizon)
                for horizon in ("SHORT", "LONG")
            }
            actionable = system_status == "FRESH" and self._latest.status != "DATA_INCOMPLETE"
            snapshot_expired = system_status == "STALE"
            unavailable_horizons = (
                _SCAN_MODE_HORIZONS[self._scan_mode]
                if system_status == "SCANNING"
                or (
                    system_status == "ERROR"
                    and self._last_attempt_status == "ERROR"
                )
                else frozenset()
            )
            horizon_actionable = {
                horizon: (
                    self._latest.status != "DATA_INCOMPLETE"
                    and item["available"]
                    and not item["expired"]
                    and (
                        system_status == "FRESH"
                        or (
                            system_status in {"SCANNING", "ERROR"}
                            and horizon not in unavailable_horizons
                        )
                    )
                )
                for horizon, item in horizon_freshness.items()
            }
            payload["runtime_status"] = system_status
            payload["actionable"] = actionable
            payload["snapshot_expired"] = snapshot_expired
            payload["latest_age_seconds"] = age_seconds
            payload["horizon_freshness"] = horizon_freshness
            payload["max_signals"] = self.config.max_signals
            payload["safety"]["horizon_actionable"] = horizon_actionable
            payload["scan_in_progress_horizons"] = (
                sorted(unavailable_horizons) if system_status == "SCANNING" else []
            )
            payload["scan_unavailable_horizons"] = (
                sorted(unavailable_horizons) if system_status == "ERROR" else []
            )
            # Availability is the hard boundary. Even a malformed legacy partial
            # report cannot leak an array from a horizon that has no completion
            # timestamp.
            if not horizon_freshness["SHORT"]["available"]:
                payload["signals"] = []
                payload["watchlist"] = []
                payload["market_map"] = []
                payload["market_regime_counts"] = {}
                payload["market_bias"] = {}
            if not horizon_freshness["LONG"]["available"]:
                payload["long_signals"] = []
                payload["long_watchlist"] = []
            if not actionable:
                payload["historical_signal_count"] = len(payload.get("signals", []))
                payload["historical_long_signal_count"] = len(
                    payload.get("long_signals", [])
                )
                payload["safety"]["actionable"] = False
                if snapshot_expired:
                    # Keep the last completed snapshot visible for reference, but
                    # expose an explicit read-only state so no client can mistake it
                    # for a current entry opportunity.
                    payload["signals_suppressed_reason"] = None
                    payload["signals_read_only_reason"] = "STALE"
                elif system_status == "SCANNING":
                    # A partial scan invalidates only the horizon currently being
                    # recalculated. The other radar remains visible and usable at
                    # its own timestamp instead of disappearing with the request.
                    if "SHORT" in unavailable_horizons:
                        payload["signals"] = []
                        payload["watchlist"] = []
                        payload["market_map"] = []
                    if "LONG" in unavailable_horizons:
                        payload["long_signals"] = []
                        payload["long_watchlist"] = []
                    payload["signals_suppressed_reason"] = "SCANNING"
                    payload["signals_read_only_reason"] = None
                elif system_status == "ERROR" and unavailable_horizons:
                    if "SHORT" in unavailable_horizons:
                        payload["signals"] = []
                        payload["watchlist"] = []
                        payload["market_map"] = []
                    if "LONG" in unavailable_horizons:
                        payload["long_signals"] = []
                        payload["long_watchlist"] = []
                    payload["signals_suppressed_reason"] = "ERROR"
                    payload["signals_read_only_reason"] = None
                else:
                    payload["signals"] = []
                    payload["watchlist"] = []
                    payload["market_map"] = []
                    payload["long_signals"] = []
                    payload["long_watchlist"] = []
                    payload["signals_suppressed_reason"] = system_status
                    payload["signals_read_only_reason"] = None
            else:
                payload["signals_suppressed_reason"] = None
                payload["signals_read_only_reason"] = None
            return payload

    def preview_dict(self) -> dict[str, Any] | None:
        with self._state_lock:
            if not self._running or self._preview is None:
                return None
            payload = public_report_payload(self._preview)
            horizon_freshness = {
                horizon: self._report_horizon_freshness(self._preview, horizon)
                for horizon in ("SHORT", "LONG")
            }
            payload["runtime_status"] = "CORE_PREVIEW"
            payload["actionable"] = True
            payload["preliminary"] = True
            payload["deep_data_pending"] = True
            payload["scan_request_mode"] = self._scan_mode
            payload["horizon_freshness"] = horizon_freshness
            payload["signals_suppressed_reason"] = None
            payload["safety"]["actionable"] = True
            payload["safety"]["horizon_actionable"] = {
                horizon: item["available"] and not item["expired"]
                for horizon, item in horizon_freshness.items()
            }
            return payload

    def statistics(self) -> dict[str, Any]:
        repository = getattr(self.scanner, "repository", None)
        if repository is None:
            return {
                "available": False,
                "note": "Signal Repository 尚未啟用；禁止顯示假勝率。",
            }
        return repository.performance()

    def signal_history(self, limit: int = 60) -> dict[str, Any]:
        repository = getattr(self.scanner, "repository", None)
        if repository is None or not hasattr(repository, "recent_history"):
            return {
                "available": False,
                "items": [],
                "note": "Signal Repository 尚未啟用。",
            }
        short_items = repository.recent_history(
            limit,
            horizon="SHORT",
            max_age_hours=24,
        )
        long_items = repository.recent_history(
            limit,
            horizon="LONG",
            max_age_hours=24 * 7,
        )
        items = short_items + long_items
        return {
            "available": bool(items),
            "items": items,
            "short_items": short_items,
            "long_items": long_items,
            "retention": {
                "SHORT": {"hours": 24, "limit": min(max(1, int(limit)), 100)},
                "LONG": {"hours": 24 * 7, "limit": min(max(1, int(limit)), 100)},
            },
            "note": (
                "15m 保留 24 小時、4H 保留 7 天；各自最多 60 筆，只按原始觸發時間輪替。"
                if items
                else "尚無訊號生命週期紀錄。"
            ),
        }

    def scan_instrument_dict(self, inst_id: str) -> dict[str, Any]:
        """Run an isolated, non-persisted analysis for one requested symbol."""

        normalized_id = _normalize_usdt_swap_id(inst_id)
        analyzer = getattr(self.scanner, "scan_instrument", None)
        if not callable(analyzer):
            raise PreflightError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "單幣掃描服務尚未啟用",
            )

        with self._scan_lock:
            with self._state_lock:
                if self._running:
                    raise PreflightError(
                        HTTPStatus.CONFLICT,
                        "全市場掃描正在執行，完成後才能掃描單一幣種",
                    )
                market_bias = deepcopy(
                    self._latest.market_bias if self._latest is not None else {}
                )
                btc_bias = "NEUTRAL"
                if self._latest is not None:
                    btc_state = next(
                        (
                            item
                            for item in self._latest.market_map
                            if item.inst_id == "BTC-USDT-SWAP"
                        ),
                        None,
                    )
                    if btc_state is not None:
                        btc_bias = btc_state.direction
            try:
                analysis = analyzer(normalized_id, market_bias, btc_bias)
            except ValueError as exc:
                raise PreflightError(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc
            except Exception as exc:
                LOGGER.exception("Single-instrument scan failed for %s", normalized_id)
                raise PreflightError(
                    HTTPStatus.BAD_GATEWAY,
                    _single_scan_failure_message(exc),
                ) from exc
            finally:
                self._release_scanner_transient_data()

        def horizon_payload(result: Any, horizon: str) -> dict[str, Any]:
            if result is None:
                return {
                    "horizon": horizon,
                    "horizon_label": "4H 長線" if horizon == "LONG" else "15m 短線",
                    "kind": "UNAVAILABLE",
                    "reason_code": "data_unavailable",
                    "message": "這個週期的資料目前不足，沒有使用替代值硬算。",
                    "item": None,
                }
            signal = result.signal
            item = signal or result.market_state
            if signal is not None:
                message = "已使用最新資料形成正式 Trigger 與交易計畫。"
                kind = "SIGNAL"
            elif item is not None:
                message = (
                    "目前尚未形成正式 Trigger；下方仍顯示最新方向、階段、"
                    "多週期證據與缺少條件。"
                )
                kind = "STATE"
            else:
                message = "核心資料無法形成可判讀的 Market Story，沒有使用假資料。"
                kind = "UNAVAILABLE"
            return {
                "horizon": horizon,
                "horizon_label": "4H 長線" if horizon == "LONG" else "15m 短線",
                "kind": kind,
                "reason_code": result.reason,
                "message": message,
                "item": (
                    public_candidate_payload(item, signal=signal is not None)
                    if item is not None
                    else None
                ),
            }

        return {
            "inst_id": analysis.inst_id,
            "analyzed_at": analysis.analyzed_at,
            "source": "ON_DEMAND_SINGLE_INSTRUMENT",
            "current_price": analysis.ticker.last,
            "short": horizon_payload(analysis.short_result, "SHORT"),
            "long": horizon_payload(analysis.long_result, "LONG"),
            "warnings": list(analysis.errors),
            "safety": {
                "analysis_only": True,
                "auto_ordering": False,
                "full_market_scan": False,
                "persisted_to_report": False,
                "note": (
                    "只掃描這一個幣；結果只回傳目前頁面，不加入全市場報告，"
                    "也不會在伺服器記憶體保留完整單幣分析。"
                ),
            },
        }

    def preflight_dict(self, inst_id: str, horizon: str) -> dict[str, Any]:
        """Refresh one stored signal's execution conditions without rescanning."""

        normalized_id = str(inst_id or "").strip().upper()
        normalized_horizon = {
            "15M": "SHORT",
            "SHORT": "SHORT",
            "4H": "LONG",
            "LONG": "LONG",
        }.get(str(horizon or "").strip().upper())
        if not normalized_id.endswith("-USDT-SWAP") or normalized_horizon is None:
            raise PreflightError(HTTPStatus.BAD_REQUEST, "幣種或長短線參數不正確")

        with self._state_lock:
            system_status, _, _ = self._system_status_locked()
            if self._running:
                raise PreflightError(
                    HTTPStatus.CONFLICT,
                    "全市場掃描正在執行，完成後才能進行單幣進場檢查",
                )
            if self._latest is None:
                raise PreflightError(HTTPStatus.NOT_FOUND, "尚未完成第一輪市場掃描")
            if system_status != "FRESH":
                raise PreflightError(
                    HTTPStatus.CONFLICT,
                    "市場報告已過期或異常，請先重新掃描全市場",
                )
            horizon_freshness = self._horizon_freshness_locked(normalized_horizon)
            if not horizon_freshness["available"] or horizon_freshness["expired"]:
                label = "4H" if normalized_horizon == "LONG" else "15m"
                raise PreflightError(
                    HTTPStatus.CONFLICT,
                    f"{label} 資料已過期，請先執行對應週期掃描或全市場掃描",
                )
            report_generated_at = str(horizon_freshness["completed_at"])
            collection = (
                self._latest.long_signals
                if normalized_horizon == "LONG"
                else self._latest.signals
            )
            signal = next(
                (item for item in collection if item.inst_id == normalized_id),
                None,
            )
            if signal is None:
                raise PreflightError(
                    HTTPStatus.NOT_FOUND,
                    "最新報告中沒有這個週期的正式 Trigger；候選尚不能進行進場檢查",
                )
            cache_key = (report_generated_at, normalized_horizon, normalized_id)
            cached = self._cached_preflight_locked(cache_key)
            if cached is not None:
                return cached

        client = getattr(self.scanner, "client", None)
        if client is None:
            raise PreflightError(HTTPStatus.SERVICE_UNAVAILABLE, "即時公開資料服務尚未啟用")

        with self._preflight_lock:
            with self._state_lock:
                cached = self._cached_preflight_locked(cache_key)
                if cached is not None:
                    return cached
            try:
                ticker = client.get_ticker(normalized_id)
                context = client.get_execution_context(normalized_id)
                payload = build_preflight_payload(
                    signal,
                    ticker,
                    context,
                    self.config,
                    report_generated_at=report_generated_at,
                )
            except PreflightError:
                raise
            except ValueError as exc:
                raise PreflightError(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc
            except Exception as exc:
                LOGGER.exception("Preflight market-data refresh failed for %s", normalized_id)
                raise PreflightError(
                    HTTPStatus.BAD_GATEWAY,
                    "OKX 即時公開資料暫時無法取得，請稍後再按一次",
                ) from exc

            with self._state_lock:
                if (
                    self._latest is None
                    or self._horizon_completed_at_locked(normalized_horizon)
                    != report_generated_at
                ):
                    raise PreflightError(
                        HTTPStatus.CONFLICT,
                        "市場報告已更新，請回到訊號頁重新選擇",
                    )
                cached_payload = deepcopy(payload)
                cached_payload["cached"] = False
                cached_payload["cache_age_seconds"] = 0.0
                cached_payload["cache_ttl_seconds"] = self._preflight_cache_ttl_seconds
                self._preflight_cache[cache_key] = (
                    time.monotonic(),
                    cached_payload,
                )
                if payload.get("verdict", {}).get("status") == "PLAN_INVALIDATED":
                    self._invalidated_preflight_signals[cache_key] = deepcopy(signal)
                return deepcopy(cached_payload)

    def reanalyze_preflight_dict(self, inst_id: str, horizon: str) -> dict[str, Any]:
        """Re-run one invalidated symbol through the unchanged V3.3 pipeline."""

        normalized_id = str(inst_id or "").strip().upper()
        normalized_horizon = {
            "15M": "SHORT",
            "SHORT": "SHORT",
            "4H": "LONG",
            "LONG": "LONG",
        }.get(str(horizon or "").strip().upper())
        if not normalized_id.endswith("-USDT-SWAP") or normalized_horizon is None:
            raise PreflightError(HTTPStatus.BAD_REQUEST, "幣種或長短線參數不正確")

        analyzer = getattr(self.scanner, "reanalyze_instrument", None)
        committer = getattr(self.scanner, "commit_single_reanalysis", None)
        if not callable(analyzer) or not callable(committer):
            raise PreflightError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "單幣重新分析服務尚未啟用",
            )

        with self._scan_lock:
            with self._state_lock:
                system_status, _, _ = self._system_status_locked()
                if self._running:
                    raise PreflightError(
                        HTTPStatus.CONFLICT,
                        "全市場掃描正在執行，完成後才能重新分析這一個幣",
                    )
                if self._latest is None:
                    raise PreflightError(HTTPStatus.NOT_FOUND, "尚未完成第一輪市場掃描")
                if system_status != "FRESH":
                    raise PreflightError(
                        HTTPStatus.CONFLICT,
                        "市場報告已過期或異常，請先重新掃描全市場",
                    )
                horizon_freshness = self._horizon_freshness_locked(normalized_horizon)
                if not horizon_freshness["available"] or horizon_freshness["expired"]:
                    label = "4H" if normalized_horizon == "LONG" else "15m"
                    raise PreflightError(
                        HTTPStatus.CONFLICT,
                        f"{label} 資料已過期，請先執行對應週期掃描或全市場掃描",
                    )
                report_generated_at = str(horizon_freshness["completed_at"])
                cache_key = (
                    report_generated_at,
                    normalized_horizon,
                    normalized_id,
                )
                previous_signal = self._invalidated_preflight_signals.get(cache_key)
                if previous_signal is None:
                    raise PreflightError(
                        HTTPStatus.CONFLICT,
                        "必須先由進場檢查確認原交易計畫失效，才能重新分析這一個幣",
                    )
                market_bias = deepcopy(self._latest.market_bias)

            try:
                analysis = analyzer(previous_signal, market_bias)
                new_signal = committer(analysis)
            except PreflightError:
                raise
            except ValueError as exc:
                raise PreflightError(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc
            except Exception as exc:
                LOGGER.exception(
                    "Single-instrument reanalysis failed for %s",
                    normalized_id,
                )
                raise PreflightError(
                    HTTPStatus.BAD_GATEWAY,
                    _single_scan_failure_message(exc),
                ) from exc
            finally:
                self._release_scanner_transient_data()

            with self._state_lock:
                if (
                    self._running
                    or self._latest is None
                    or self._horizon_completed_at_locked(normalized_horizon)
                    != report_generated_at
                ):
                    raise PreflightError(
                        HTTPStatus.CONFLICT,
                        "市場報告已更新，請回到訊號頁重新選擇",
                    )
                current_report = self._latest
                collection = (
                    current_report.long_signals
                    if normalized_horizon == "LONG"
                    else current_report.signals
                )
                updated_collection = [
                    item
                    for item in collection
                    if item.inst_id != normalized_id
                ]
                if new_signal is not None:
                    updated_collection.append(new_signal)
                    sorter = getattr(self.scanner, "_signal_sort_key", None)
                    if callable(sorter):
                        updated_collection.sort(key=sorter, reverse=True)
                    updated_collection = updated_collection[: self.config.max_signals]
                repository = getattr(self.scanner, "repository", None)
                historical = (
                    repository.performance()
                    if repository is not None and hasattr(repository, "performance")
                    else current_report.historical_performance
                )
                updated_report = replace(
                    current_report,
                    signals=(
                        updated_collection
                        if normalized_horizon == "SHORT"
                        else current_report.signals
                    ),
                    long_signals=(
                        updated_collection
                        if normalized_horizon == "LONG"
                        else current_report.long_signals
                    ),
                    historical_performance=historical,
                )
                self._latest = updated_report
                self._preflight_cache.pop(cache_key, None)
                if new_signal is not None:
                    self._invalidated_preflight_signals.pop(cache_key, None)

            save_report(updated_report, self.config.data_dir)

            if new_signal is None:
                return {
                    "inst_id": normalized_id,
                    "horizon": normalized_horizon,
                    "horizon_label": (
                        "4H 長線" if normalized_horizon == "LONG" else "15m 短線"
                    ),
                    "reanalysis": {
                        "performed": True,
                        "status": "NO_NEW_ENTRY_OPPORTUNITY",
                        "message": (
                            "已用最新多週期資料重新分析這一個幣，"
                            "目前沒有新的正式 Trigger。"
                        ),
                        "old_plan_closed": True,
                        "reason_code": analysis.reason,
                        "analyzed_at": analysis.analyzed_at,
                    },
                    "safety": {
                        "analysis_only": True,
                        "auto_ordering": False,
                        "old_plan_reused": False,
                        "core_strategy_unchanged": True,
                    },
                }

            payload = build_preflight_payload(
                new_signal,
                analysis.ticker,
                analysis.context,
                self.config,
                report_generated_at=analysis.analyzed_at,
            )
            payload["reanalysis"] = {
                "performed": True,
                "status": "NEW_ENTRY_OPPORTUNITY",
                "message": "已產生全新的正式 Trigger 與交易計畫。",
                "old_plan_closed": True,
                "old_trigger_id": previous_signal.trigger_id,
                "new_trigger_id": new_signal.trigger_id,
                "analyzed_at": analysis.analyzed_at,
            }
            payload["cached"] = False
            payload["cache_age_seconds"] = 0.0
            payload["cache_ttl_seconds"] = self._preflight_cache_ttl_seconds
            return payload

    def latest_markdown(self) -> str | None:
        with self._state_lock:
            if self._latest is None:
                return None
            system_status, _, _ = self._system_status_locked()
            if system_status == "STALE":
                markdown = report_markdown(self._latest)
                heading = "# OKX USDT 永續雷達\n\n"
                warning = (
                    "> ⚠️ 資料已過期：這是超過 30 分鐘的保留快照，"
                    "禁止依此進場。請重新掃描全市場或只更新單一幣種。\n\n"
                )
                if markdown.startswith(heading):
                    return heading + warning + markdown[len(heading) :]
                return warning + markdown
            if system_status != "FRESH":
                labels = {
                    "SCANNING": "掃描中，舊正式訊號已暫停使用。",
                    "ERROR": "最新掃描失敗，舊正式訊號已暫停使用。",
                    "BOOTING": "雷達啟動中，尚無可用訊號。",
                }
                return "# OKX USDT 永續雷達\n\n" + labels.get(
                    system_status,
                    "目前沒有可使用的正式訊號。",
                )
            return report_markdown(self._latest)

    def _begin_scan_locked(self, scan_mode: str = "FULL") -> None:
        self._scan_mode = _normalize_scan_mode(scan_mode)
        self._running = True
        self._last_attempt_status = "SCANNING"
        self._scan_id = str(uuid.uuid4())
        self._scan_started_at = datetime.now(timezone.utc).isoformat()
        self._preview = None
        self._preflight_cache.clear()
        self._invalidated_preflight_signals.clear()
        self._scan_push_subscriptions.clear()
        self._progress = {
            "phase": "STARTING",
            "completed": 0,
            "total": None,
            "percent": None,
            "message": f"正在啟動{_SCAN_MODE_LABELS[self._scan_mode]}",
        }

    def _scan_worker(self) -> None:
        report: RadarReport | None = None
        error: Exception | None = None
        try:
            report = self._perform_scan()
        except Exception as exc:
            error = exc
        finally:
            with self._state_lock:
                self._running = False
                scan_id = self._scan_id or "unknown"
                subscriptions = list(self._scan_push_subscriptions.values())
                self._scan_push_subscriptions.clear()
        if subscriptions:
            self._deliver_scan_notifications(subscriptions, scan_id, report, error)

    def _perform_scan(self) -> RadarReport:
        with self._scan_lock:
            with self._state_lock:
                scan_id = self._scan_id or str(uuid.uuid4())
                started_at = self._scan_started_at or datetime.now(timezone.utc).isoformat()
                scan_mode = self._scan_mode
                previous_report = self._latest
            LOGGER.info(
                "Starting on-demand OKX USDT perpetual scan id=%s mode=%s",
                scan_id,
                scan_mode,
            )
            try:
                scan_kwargs: dict[str, Any] = {
                    "progress": self._update_progress,
                    "scan_id": scan_id,
                }
                parameters = inspect.signature(self.scanner.scan_once).parameters
                if "preview" in parameters:
                    scan_kwargs["preview"] = self._publish_preview
                if "scan_mode" in parameters:
                    scan_kwargs["scan_mode"] = scan_mode
                try:
                    report = self.scanner.scan_once(**scan_kwargs)
                finally:
                    self._release_scanner_transient_data()
                completed_at = datetime.now(timezone.utc).isoformat()
                if report.status != "DATA_INCOMPLETE":
                    report = self._merge_partial_report(
                        report,
                        previous_report,
                        scan_mode,
                        completed_at,
                    )
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
                    scan_mode=scan_mode,
                )
                if report.status == "DATA_INCOMPLETE" and previous_report is not None:
                    # A failed refresh is an attempt state, not a new market
                    # snapshot. Keep the last completed horizon slots on disk and
                    # in memory; the requested slot is disabled by latest_dict(),
                    # while an untouched slot remains available at its own age.
                    with self._state_lock:
                        self._preview = None
                        self._last_error = report.message
                        self._last_attempt_status = "ERROR"
                        self._progress = {
                            "phase": "COMPLETED",
                            "completed": 1,
                            "total": 1,
                            "percent": 100.0,
                            "message": f"{_SCAN_MODE_LABELS[scan_mode]}失敗",
                        }
                    LOGGER.info(
                        "Scan attempt failed without replacing completed snapshot: "
                        "id=%s mode=%s status=%s",
                        scan_id,
                        scan_mode,
                        report.status,
                    )
                    return report
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
                            f"{_SCAN_MODE_LABELS[scan_mode]}完成"
                            if report.status != "DATA_INCOMPLETE"
                            else f"{_SCAN_MODE_LABELS[scan_mode]}失敗"
                        ),
                    }
                LOGGER.info(
                    "Scan finished: id=%s mode=%s status=%s coverage=%.2f signals=%d",
                    scan_id,
                    scan_mode,
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

    @staticmethod
    def _previous_horizon_timestamp(
        report: RadarReport,
        horizon: str,
    ) -> str:
        field_name = "long_completed_at" if horizon == "LONG" else "short_completed_at"
        timestamp = str(getattr(report, field_name, "") or "").strip()
        if timestamp:
            return timestamp
        mode = str(getattr(report, "scan_mode", "FULL") or "FULL").upper()
        if mode == "FULL" or mode == horizon:
            return report.completed_at or report.generated_at
        return ""

    def _merge_partial_report(
        self,
        report: RadarReport,
        previous: RadarReport | None,
        scan_mode: str,
        completed_at: str,
    ) -> RadarReport:
        """Keep the unrequested radar intact after a successful partial scan."""

        if scan_mode == "FULL":
            return replace(
                report,
                short_completed_at=completed_at,
                long_completed_at=completed_at,
            )
        quality = deepcopy(report.data_quality)
        if scan_mode == "SHORT":
            previous_long_completed_at = (
                self._previous_horizon_timestamp(previous, "LONG")
                if previous is not None
                else ""
            )
            if not previous_long_completed_at:
                return replace(
                    report,
                    long_signals=[],
                    long_watchlist=[],
                    long_market_map=[],
                    short_completed_at=completed_at,
                    long_completed_at="",
                )
            previous_quality = previous.data_quality or {}
            for key, value in previous_quality.items():
                if key.startswith("long_"):
                    quality[key] = deepcopy(value)
            return replace(
                report,
                long_signals=previous.long_signals,
                long_watchlist=previous.long_watchlist,
                long_market_map=previous.long_market_map,
                long_completed_at=previous_long_completed_at,
                short_completed_at=completed_at,
                data_quality=quality,
                message=f"{report.message} 4H 沿用上一輪資料。",
            )
        previous_short_completed_at = (
            self._previous_horizon_timestamp(previous, "SHORT")
            if previous is not None
            else ""
        )
        if not previous_short_completed_at:
            return replace(
                report,
                signals=[],
                watchlist=[],
                market_map=[],
                market_regime_counts={},
                market_bias={},
                short_completed_at="",
                long_completed_at=completed_at,
            )
        previous_quality = previous.data_quality or {}
        for key, value in previous_quality.items():
            if key.startswith("core_"):
                quality[key] = deepcopy(value)
        return replace(
            report,
            signals=previous.signals,
            watchlist=previous.watchlist,
            market_map=previous.market_map,
            market_regime_counts=previous.market_regime_counts,
            market_bias=previous.market_bias,
            short_completed_at=previous_short_completed_at,
            long_completed_at=completed_at,
            data_quality=quality,
            message=f"{report.message} 15m 沿用上一輪資料。",
        )

    def _release_scanner_transient_data(self) -> None:
        release = getattr(self.scanner, "release_transient_data", None)
        if not callable(release):
            return
        try:
            released = release()
            LOGGER.info("Released %s cached candle series", released)
        except Exception:
            LOGGER.exception("Unable to release transient scanner data")

    def _register_scan_push_locked(self, subscription: dict[str, Any]) -> None:
        key = self.push_notifier.subscription_key(subscription)
        if not key or key in self._scan_push_subscriptions:
            return
        if len(self._scan_push_subscriptions) >= self._max_scan_push_subscriptions:
            raise PushSubscriptionError("本輪掃描通知裝置已達安全上限")
        self._scan_push_subscriptions[key] = subscription

    def _deliver_scan_notifications(
        self,
        subscriptions: list[dict[str, Any]],
        scan_id: str,
        report: RadarReport | None,
        error: Exception | None,
    ) -> None:
        success = (
            error is None
            and report is not None
            and report.status != "DATA_INCOMPLETE"
        )
        payload = {
            "title": "OKX 雷達掃描完成" if success else "OKX 雷達掃描未完成",
            "body": (
                "最新市場報告已完成，點擊查看結果。"
                if success
                else "本輪掃描未能完成，點擊查看目前狀態。"
            ),
            "url": "/",
            "tag": f"okx-radar-scan-{scan_id}",
            "status": "SUCCESS" if success else "ERROR",
            "scan_id": scan_id,
        }
        for subscription in subscriptions:
            try:
                self.push_notifier.send(subscription, payload)
            except Exception as exc:
                # Browser push endpoints are capability URLs. Never log them.
                LOGGER.warning(
                    "Unable to deliver one scan completion notification error=%s",
                    type(exc).__name__,
                )

    def _publish_preview(self, report: RadarReport) -> None:
        with self._state_lock:
            if not self._running or report.scan_id != self._scan_id:
                return
            if self._scan_mode == "SHORT":
                report = self._merge_partial_report(
                    report,
                    self._latest,
                    "SHORT",
                    report.short_completed_at or report.generated_at,
                )
            self._preview = report
            self._progress = {
                "phase": "CORE_PREVIEW",
                "completed": 1,
                "total": 1,
                "percent": 100.0,
                "message": (
                    "15m 核心結果已發布；既有 4H 結果保持不變，正在補深度資料"
                    if self._scan_mode == "SHORT"
                    else "15m 核心結果已發布，正在補深度資料與 4H 長線雷達"
                ),
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

    def _horizon_completed_at_locked(self, horizon: str) -> str | None:
        if self._latest is None:
            return None
        return self._previous_horizon_timestamp(self._latest, horizon) or None

    def _horizon_freshness_locked(self, horizon: str) -> dict[str, Any]:
        if self._latest is None:
            return {
                "available": False,
                "completed_at": None,
                "age_seconds": None,
                "expired": False,
            }
        return self._report_horizon_freshness(self._latest, horizon)

    def _report_horizon_freshness(
        self,
        report: RadarReport,
        horizon: str,
    ) -> dict[str, Any]:
        completed_at = self._previous_horizon_timestamp(report, horizon)
        if not completed_at:
            return {
                "available": False,
                "completed_at": None,
                "age_seconds": None,
                "expired": False,
            }
        try:
            completed = datetime.fromisoformat(completed_at)
            if completed.tzinfo is None:
                completed = completed.replace(tzinfo=timezone.utc)
            age_seconds = max(0.0, time.time() - completed.timestamp())
        except (TypeError, ValueError):
            age_seconds = float("inf")
        return {
            "available": True,
            "completed_at": completed_at,
            "age_seconds": age_seconds,
            "expired": age_seconds > self.config.stale_after_seconds,
        }

    def _cached_preflight_locked(
        self,
        key: tuple[str, str, str],
    ) -> dict[str, Any] | None:
        cached = self._preflight_cache.get(key)
        if cached is None:
            return None
        created_at, payload = cached
        age = max(0.0, time.monotonic() - created_at)
        if age >= self._preflight_cache_ttl_seconds:
            self._preflight_cache.pop(key, None)
            return None
        result = deepcopy(payload)
        result["cached"] = True
        result["cache_age_seconds"] = round(age, 3)
        return result

    @staticmethod
    def _idle_progress() -> dict[str, Any]:
        return {
            "phase": "IDLE",
            "completed": None,
            "total": None,
            "percent": None,
            "message": "等待排程或使用者要求最新市場掃描",
        }


def _normalize_usdt_swap_id(value: str) -> str:
    raw = str(value or "").strip().upper().replace(" ", "")
    if raw.endswith("-USDT-SWAP"):
        base = raw[: -len("-USDT-SWAP")]
    else:
        base = raw
        for suffix in ("/USDT", "-USDT", "USDT", "-SWAP"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
    if not re.fullmatch(r"[A-Z0-9]{1,24}", base):
        raise PreflightError(
            HTTPStatus.BAD_REQUEST,
            "請輸入正確幣種，例如 BTC 或 BTC-USDT-SWAP",
        )
    return f"{base}-USDT-SWAP"


def serve(runtime: RadarRuntime, host: str, port: int) -> None:
    static_dir = Path(__file__).parent / "static"
    dashboard_path = static_dir / "pages.html"
    dashboard = dashboard_path.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "OKXRadar/3.3"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
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
            elif path == "/api/push/config":
                self._send_json(HTTPStatus.OK, runtime.push_config())
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
            elif path == "/api/history":
                query = parse_qs(parsed.query)
                try:
                    limit = int(query.get("limit", ["60"])[0])
                except (TypeError, ValueError):
                    limit = 60
                self._send_json(HTTPStatus.OK, runtime.signal_history(limit))
            elif path == "/api/preflight":
                query = parse_qs(parsed.query)
                try:
                    payload = runtime.preflight_dict(
                        query.get("inst_id", [""])[0],
                        query.get("horizon", [""])[0],
                    )
                except PreflightError as exc:
                    self._send_json(exc.status, {"error": str(exc)})
                else:
                    self._send_json(HTTPStatus.OK, payload)
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/instrument/scan":
                try:
                    payload = self._read_json_body()
                    result = runtime.scan_instrument_dict(payload.get("inst_id", ""))
                except PreflightError as exc:
                    self._send_json(exc.status, {"error": str(exc)})
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                else:
                    self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/preflight/reanalyze":
                try:
                    payload = self._read_json_body()
                    result = runtime.reanalyze_preflight_dict(
                        payload.get("inst_id", ""),
                        payload.get("horizon", ""),
                    )
                except PreflightError as exc:
                    self._send_json(exc.status, {"error": str(exc)})
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                else:
                    self._send_json(HTTPStatus.OK, result)
                return
            if path != "/api/scan":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                payload = self._read_json_body()
                push_subscription = payload.get("push_subscription")
                scan_mode = _normalize_scan_mode(payload.get("scan_mode", "FULL"))
                started = runtime.trigger_scan(
                    push_subscription,
                    scan_mode=scan_mode,
                )
            except PushSubscriptionError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            status = runtime.status()
            self._send_json(
                HTTPStatus.ACCEPTED,
                {
                    "accepted": started,
                    "joined_existing_scan": not started,
                    "scan_id": status["scan_id"],
                    "scan_mode": status["scan_mode"],
                    "scan_mode_label": status["scan_mode_label"],
                    "runtime_status": "SCANNING",
                    "notification_registered": push_subscription is not None,
                    "message": (
                        f"已開始{_SCAN_MODE_LABELS[scan_mode]}"
                        if started
                        else f"{status['scan_mode_label']}正在執行，已加入目前進度"
                    ),
                },
            )

        def _read_json_body(self) -> dict[str, Any]:
            raw_length = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_length)
            except (TypeError, ValueError) as exc:
                raise ValueError("請求長度不正確") from exc
            if length < 0 or length > 16_384:
                raise ValueError("通知請求內容過大")
            if length == 0:
                return {}
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("請求內容必須是正確的 JSON") from exc
            if not isinstance(payload, dict):
                raise ValueError("請求內容格式不正確")
            return payload

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
