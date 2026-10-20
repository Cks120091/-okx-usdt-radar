"""Opt-in, public-data-only OKX watcher; all warnings are advisory only.

Shares the existing RadarRuntime to preserve closed-candle trigger decisions and
Signal Episodes. No trading credentials, AI calls, paper or live orders.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .formal_alerts import FormalSignalTracker
from .full_market_stream import FullMarketStream

LOG = logging.getLogger("okx_radar.always_on")
PUBLIC_WS = "wss://ws.okx.com/ws/v5/public"


@dataclass(frozen=True)
class MonitorConfig:
    websocket_url: str = PUBLIC_WS
    max_symbols: int = 80
    full_universe: bool = False
    ws_shard_size: int = 80
    universe_refresh_seconds: int = 300
    candidate_min_volume_usdt: float = 2_000_000.0
    candidate_deep_scan: bool = True
    candidate_scan_interval_seconds: int = 90
    momentum_window_seconds: int = 180
    min_observation_seconds: int = 60
    advisory_move_pct: float = 0.5
    warning_cooldown_seconds: int = 1800
    stale_tick_seconds: int = 20
    short_scan_seconds: int = 900
    # LONG uses 1H closed-price Trigger; 4H is direction filter, not scan clock.
    long_scan_seconds: int = 3600
    scan_settle_seconds: int = 10
    retry_seconds: int = 60

    @classmethod
    def from_env(cls) -> "MonitorConfig":
        return cls(
            websocket_url=os.getenv("RADAR_PUBLIC_WS_URL", PUBLIC_WS),
            max_symbols=int(os.getenv("RADAR_MONITOR_MAX_SYMBOLS", "80")),
            full_universe=os.getenv("RADAR_MONITOR_ALL_SYMBOLS", "1").strip().lower() in {"1", "true", "yes", "on"},
            ws_shard_size=int(os.getenv("RADAR_MONITOR_WS_SHARD_SIZE", "80")),
            universe_refresh_seconds=int(os.getenv("RADAR_MONITOR_UNIVERSE_REFRESH_SECONDS", "300")),
            candidate_min_volume_usdt=float(os.getenv("RADAR_MONITOR_CANDIDATE_MIN_VOLUME", "2000000")),
            candidate_deep_scan=os.getenv("RADAR_MONITOR_CANDIDATE_DEEP_SCAN", "1").strip().lower() in {"1", "true", "yes", "on"},
            candidate_scan_interval_seconds=int(os.getenv("RADAR_MONITOR_CANDIDATE_SCAN_INTERVAL", "90")),
            advisory_move_pct=float(os.getenv("RADAR_MONITOR_MOVE_PCT", "0.5")),
        )

    def validate(self) -> None:
        parsed = urlparse(self.websocket_url)
        if (parsed.scheme != "wss" or
                parsed.hostname not in {"ws.okx.com", "wseea.okx.com", "wsus.okx.com"} or
                parsed.path != "/ws/v5/public" or parsed.port not in (None, 443) or
                parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Official public OKX WebSocket on port 443 required")
        if not 1 <= self.max_symbols <= 150:
            raise ValueError("max_symbols out of range")
        if not 1 <= self.ws_shard_size <= 120 or self.universe_refresh_seconds < 60:
            raise ValueError("invalid market subscription limits")
        if self.candidate_scan_interval_seconds < 30:
            raise ValueError("candidate deep-scan interval must be at least 30 seconds")
        if not 0 <= self.candidate_min_volume_usdt <= 1_000_000_000_000:
            raise ValueError("invalid candidate turnover threshold")
        if not 0 < self.advisory_move_pct <= 20:
            raise ValueError("advisory_move_pct out of range")
        if not 0 < self.min_observation_seconds <= self.momentum_window_seconds:
            raise ValueError("invalid observation window")
        if (self.stale_tick_seconds <= 0 or self.retry_seconds <= 0 or
                self.short_scan_seconds <= 0 or self.long_scan_seconds <= 0):
            raise ValueError("invalid timeout or scan cadence")


class AlwaysOnMonitor:
    """WebSocket observation + scheduled *existing* closed-candle scanner."""

    def __init__(
        self,
        runtime: Any,
        config: MonitorConfig | None = None,
        *,
        ws_factory: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.time,
        on_warning: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.runtime = runtime
        self.config = config or MonitorConfig.from_env()
        self.config.validate()
        self._clock = clock
        self._ws_factory = ws_factory
        self._on_warning = on_warning
        self._formal_alerts = FormalSignalTracker(runtime.config.data_dir, clock=clock)
        self._full_stream = None
        if self.config.full_universe:
            client = getattr(getattr(runtime, "scanner", None), "client", None)
            if client is None:
                raise ValueError("Full-universe monitoring requires a public OKX client")
            self._full_stream = FullMarketStream(
                client, self.config.websocket_url, self.handle_frame,
                ws_factory=ws_factory, shard_size=self.config.ws_shard_size,
                refresh_seconds=self.config.universe_refresh_seconds, clock=clock,
            )
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._scan_thread: threading.Thread | None = None
        self._ws_thread: threading.Thread | None = None
        self._candidate_thread: threading.Thread | None = None
        self._candidate_queue: deque[str] = deque(maxlen=24)
        self._candidate_queued: set[str] = set()
        self._deep_scans_done = 0
        self._deep_scan_last_at: float | None = None
        self._deep_scan_last_error: str | None = None
        self._socket: Any = None
        self._targets: tuple[str, ...] = ()
        self._watch_sides: dict[str, set[tuple[str, str]]] = {}
        self._samples: dict[str, deque[tuple[int, float]]] = {}
        self._last_warning: dict[tuple[str, str, str], float] = {}
        self._recent_warnings: deque[dict[str, Any]] = deque(maxlen=30)
        self._recent_candidates: deque[dict[str, Any]] = deque(maxlen=40)
        self._last_tick_at: float | None = None
        self._last_success: dict[str, float] = {"SHORT": 0.0, "LONG": 0.0}
        self._last_attempt: dict[str, float] = {"SHORT": 0.0, "LONG": 0.0}
        self._last_success_bucket: dict[str, int] = {"SHORT": -1, "LONG": -1}
        self._connected = False
        self._last_error: str | None = None

    def start(self) -> None:
        with self._lock:
            if self._scan_thread is not None and self._scan_thread.is_alive():
                return
            self._stop.clear()
            self._scan_thread = threading.Thread(
                target=self._scan_loop, daemon=True, name="radar-continuous-scan")
            if self._full_stream is None:
                self._ws_thread = threading.Thread(
                    target=self._ws_loop, daemon=True, name="radar-public-tickers")
            self._scan_thread.start()
            if self._full_stream is not None and self.config.candidate_deep_scan:
                self._candidate_thread = threading.Thread(
                    target=self._candidate_loop, daemon=True,
                    name="radar-candidate-deep-analyzer")
                self._candidate_thread.start()
            if self._full_stream is not None:
                self._full_stream.start()
            elif self._ws_thread is not None:
                self._ws_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._full_stream is not None:
            self._full_stream.stop()
        with self._lock:
            sock = self._socket
        if sock is not None:
            try:
                sock.close()
            except Exception:
                LOG.exception("WebSocket close failed")
        for thread in (self._ws_thread, self._scan_thread, self._candidate_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=3)

    def status(self) -> dict[str, Any]:
        with self._lock:
            now, last = self._clock(), self._last_tick_at
            market = self._full_stream.status() if self._full_stream is not None else None
            return {
                "enabled": True,
                "running": bool(self._scan_thread and self._scan_thread.is_alive()),
                "websocket_connected": market["connected_shards"] > 0 if market else self._connected,
                "market_stream": market,
                "websocket_fresh": bool(
                    (market["coverage_complete"] if market else self._connected)
                    and last is not None and
                    now - last <= self.config.stale_tick_seconds),
                "last_tick_at": datetime.fromtimestamp(last, timezone.utc).isoformat()
                if last else None,
                "selected_symbols": market["listed_symbols"] if market else len(self._targets),
                "recent_candidates": list(self._recent_candidates)[-12:],
                "candidate_analysis": {
                    "enabled": bool(self._full_stream is not None and self.config.candidate_deep_scan),
                    "queued": len(self._candidate_queue),
                    "completed": self._deep_scans_done,
                    "last_completed_at": (
                        datetime.fromtimestamp(self._deep_scan_last_at, timezone.utc).isoformat()
                        if self._deep_scan_last_at else None),
                    "last_error": self._deep_scan_last_error,
                },
                "latest_scan_at": {
                    h: datetime.fromtimestamp(t, timezone.utc).isoformat() if t else None
                    for h, t in self._last_success.items()
                },
                "recent_warnings": list(self._recent_warnings)[-10:],
                "formal_alert_notifications": self._formal_alerts.status(),
                "last_error": self._last_error,
                "analysis_only": True,
                "auto_ordering": False,
            }

    def refresh_targets(self, report: Any) -> None:
        """Cache formal-scan watch sides; never narrow a full-market subscription."""
        eligible = set(getattr(report, "target_instruments", []) or [])
        prioritized: list[str] = []
        watch: dict[str, set[tuple[str, str]]] = {}
        for horizon, items in (
            ("SHORT", getattr(report, "watchlist", [])),
            ("LONG", getattr(report, "long_watchlist", [])),
        ):
            for item in items:
                symbol = getattr(item, "inst_id", "")
                side = str(getattr(item, "direction", "")).upper()
                if symbol in eligible and side in {"LONG", "SHORT"}:
                    watch.setdefault(symbol, set()).add((horizon, side))
                    prioritized.append(symbol)
        for items in (getattr(report, "signals", []), getattr(report, "long_signals", [])):
            prioritized.extend(getattr(item, "inst_id", "") for item in items)
        markets = list(getattr(report, "market_map", [])) + list(
            getattr(report, "long_market_map", []))
        markets.sort(
            key=lambda m: float(getattr(m, "quote_volume_24h", 0) or 0),
            reverse=True)
        prioritized.extend(getattr(item, "inst_id", "") for item in markets)
        prioritized.extend(["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
        chosen = tuple(dict.fromkeys(
            symbol for symbol in prioritized if symbol in eligible
        ))[:self.config.max_symbols]
        with self._lock:
            changed = chosen != self._targets
            self._targets = chosen
            self._watch_sides = {
                key: val for key, val in watch.items()
                if (self._full_stream is not None or key in chosen)}
            if self._full_stream is None:
                self._samples = {
                    key: val for key, val in self._samples.items() if key in chosen}
            sock = self._socket if changed and self._full_stream is None else None
        if sock is not None:
            sock.close()

    def handle_frame(self, raw: str) -> None:
        """Validate tick timestamps; never create or modify formal triggers."""
        if raw == "pong":
            return
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(message, dict):
            return
        if message.get("event") == "error":
            with self._lock:
                self._last_error = str(
                    message.get("msg") or message.get("code") or
                    "subscription rejected")
            return
        arg = message.get("arg")
        if (message.get("event") or not isinstance(arg, dict) or
                arg.get("channel") != "tickers"):
            return
        data = message.get("data")
        if not isinstance(data, list):
            return
        now = self._clock()
        now_ms = int(now * 1000)
        for row in data:
            if not isinstance(row, dict):
                continue
            symbol = row.get("instId")
            try:
                last, ts = float(row["last"]), int(row["ts"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if (not math.isfinite(last) or last <= 0 or ts > now_ms + 2000
                    or now_ms - ts > self.config.stale_tick_seconds * 1000):
                continue
            warnings = []
            with self._lock:
                if symbol not in (self._full_stream.symbols if self._full_stream else self._targets):
                    continue
                self._last_tick_at = now
                samples = self._samples.setdefault(symbol, deque())
                if samples and ts <= samples[-1][0]:
                    continue
                samples.append((ts, last))
                cutoff = ts - self.config.momentum_window_seconds * 1000
                while samples and samples[0][0] < cutoff:
                    samples.popleft()
                if (len(samples) < 2 or
                        ts - samples[0][0] <
                        self.config.min_observation_seconds * 1000):
                    continue
                reference = samples[0][1]
                change_pct = (last / reference - 1) * 100
                # Observe every subscribed contract, but only promote sufficiently
                # liquid ones to candidate analysis. This never creates a Trigger.
                if self._full_stream is not None:
                    turnover = self._full_stream.quote_volume(symbol)
                    if (turnover is not None
                            and turnover >= self.config.candidate_min_volume_usdt
                            and abs(change_pct) >= self.config.advisory_move_pct):
                        side = "LONG" if change_pct > 0 else "SHORT"
                        key = (symbol, "CANDIDATE", side)
                        if now - self._last_warning.get(key, float("-inf")) >= self.config.warning_cooldown_seconds:
                            self._last_warning[key] = now
                            candidate = {
                                "type": "MARKET_PRICE_ACCELERATION_CANDIDATE",
                                "advisory_only": True,
                                "entry_permitted": False,
                                "symbol": symbol,
                                "direction": side,
                                "horizon": "CANDIDATE",
                                "price": last,
                                "change_pct": round(change_pct, 3),
                                "quote_volume_24h": round(turnover, 2),
                                "exchange_ts": ts,
                                "observed_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                                "message": "全市場價格加速候選；尚未通過 MA/MACD/收線正式觸發。",
                            }
                            self._recent_candidates.append(candidate)
                            if (self.config.candidate_deep_scan and
                                    symbol not in self._candidate_queued and
                                    len(self._candidate_queue) < self._candidate_queue.maxlen):
                                self._candidate_queue.append(symbol)
                                self._candidate_queued.add(symbol)
                            warnings.append(candidate)
                for horizon, side in self._watch_sides.get(symbol, ()):
                    if ((side == "LONG" and
                            change_pct < self.config.advisory_move_pct) or
                        (side == "SHORT" and
                            change_pct > -self.config.advisory_move_pct)):
                        continue
                    key = (symbol, horizon, side)
                    if (now - self._last_warning.get(key, float("-inf")) <
                            self.config.warning_cooldown_seconds):
                        continue
                    self._last_warning[key] = now
                    warning = {
                        "type": "WATCHLIST_PRICE_ACCELERATION",
                        "advisory_only": True,
                        "entry_permitted": False,
                        "symbol": symbol,
                        "horizon": horizon,
                        "direction": side,
                        "price": last,
                        "change_pct": round(change_pct, 3),
                        "exchange_ts": ts,
                        "observed_at": datetime.fromtimestamp(
                            now, timezone.utc).isoformat(),
                        "message": "觀察清單價格加速，尚未構成正式收線 Trigger。",
                    }
                    self._recent_warnings.append(warning)
                    warnings.append(warning)
            for warning in warnings:
                self._record_warning(warning)

    def _record_warning(self, warning: dict[str, Any]) -> None:
        try:
            path = Path(self.runtime.config.data_dir) / "monitor_warnings.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(
                    warning, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:
            LOG.exception("Could not persist advisory warning")
        LOG.info("Live advisory: %s", warning)
        if self._on_warning is not None:
            try:
                self._on_warning(warning)
            except Exception:
                LOG.exception("Warning callback failed")

    def _candidate_loop(self) -> None:
        """Rate-limited single-symbol deep analysis, never live-tick Triggering.

        The existing scanner reuses CLOSED K lines and the same Signal Episode
        repository. Manual scan and full-market scan take precedence.
        """
        while not self._stop.is_set():
            with self._lock:
                symbol = self._candidate_queue.popleft() if self._candidate_queue else None
                if symbol:
                    self._candidate_queued.discard(symbol)
            if not symbol:
                self._stop.wait(2.0)
                continue
            analyzer = getattr(self.runtime, "scan_instrument_dict", None)
            if not callable(analyzer):
                with self._lock:
                    self._deep_scan_last_error = "單幣分析器尚未啟用"
                self._stop.wait(self.config.candidate_scan_interval_seconds)
                continue
            if bool(getattr(self.runtime, "_running", False)):
                with self._lock:
                    if symbol not in self._candidate_queued and len(self._candidate_queue) < self._candidate_queue.maxlen:
                        self._candidate_queue.append(symbol)
                        self._candidate_queued.add(symbol)
                self._stop.wait(5.0)
                continue
            try:
                result = analyzer(symbol, "BOTH")
                with self._lock:
                    self._deep_scans_done += 1
                    self._deep_scan_last_at = self._clock()
                    self._deep_scan_last_error = None
                # A single-symbol confirmation can promote an Episode into
                # the saved report. Route it through the same deduplicated notifier.
                if result and getattr(self.runtime, "_latest", None) is not None:
                    with self.runtime._state_lock:
                        latest = self.runtime._latest
                    if latest is not None:
                        self._formal_alerts.process(latest)
            except Exception as exc:
                LOG.info("Candidate analysis skipped: %s", type(exc).__name__)
                with self._lock:
                    self._deep_scan_last_error = "候選幣分析暫時失敗或掃描佔用"
            self._stop.wait(self.config.candidate_scan_interval_seconds)

    def _scan_loop(self) -> None:
        while not self._stop.is_set():
            now = self._clock()
            due: list[str] = []
            for horizon, seconds in (
                ("SHORT", self.config.short_scan_seconds),
                ("LONG", self.config.long_scan_seconds),
            ):
                with self._lock:
                    previous = self._last_success[horizon]
                    attempted = self._last_attempt[horizon]
                bucket = int((now - self.config.scan_settle_seconds) // seconds)
                last_bucket = self._last_success_bucket[horizon] if previous else -1
                if (bucket > last_bucket and
                        now - attempted >= self.config.retry_seconds):
                    due.append(horizon)
            mode = "FULL" if len(due) == 2 else due[0] if due else None
            if mode:
                with self._lock:
                    for horizon in due:
                        self._last_attempt[horizon] = now
                try:
                    report = self.runtime.scan_blocking(mode)
                    if report.status == "DATA_INCOMPLETE":
                        raise RuntimeError("Core market data incomplete")
                    completed = self._clock()
                    with self._lock:
                        for horizon in due:
                            self._last_success[horizon] = completed
                            # Use the bucket that was scanned, NOT completion time:
                            # a 3-minute scan can straddle the next bar open.
                            self._last_success_bucket[horizon] = int(
                                (now - self.config.scan_settle_seconds) //
                                (self.config.short_scan_seconds if horizon == "SHORT"
                                 else self.config.long_scan_seconds))
                        self._last_error = None
                    self.refresh_targets(report)
                    # Notify only about existing closed-candle formal triggers.
                    # Price-acceleration advisories must never reach this path.
                    self._formal_alerts.process(report)
                except RuntimeError as exc:
                    if "already running" not in str(exc):
                        LOG.warning("Background scan unavailable: %s", exc)
                        with self._lock:
                            self._last_error = str(exc)
                except Exception as exc:
                    LOG.exception("Background scan failed")
                    with self._lock:
                        self._last_error = str(exc)
            self._stop.wait(2.0)

    def _ws_loop(self) -> None:
        delay = 2.0
        while not self._stop.is_set():
            with self._lock:
                symbols = self._targets
            if not symbols:
                self._stop.wait(2.0)
                continue
            try:
                if self._ws_factory is None:
                    from websocket import WebSocketApp
                    factory = WebSocketApp
                else:
                    factory = self._ws_factory

                def on_open(sock: Any) -> None:
                    with self._lock:
                        self._connected = True
                    for start in range(0, len(symbols), 40):
                        args = [
                            {"channel": "tickers", "instId": name}
                            for name in symbols[start:start + 40]
                        ]
                        sock.send(json.dumps({"op": "subscribe", "args": args}))

                def on_close(_sock: Any, _status: Any, _reason: Any) -> None:
                    with self._lock:
                        self._connected = False

                def on_error(_sock: Any, error: Any) -> None:
                    LOG.warning("OKX ticker socket error: %s", error)
                    with self._lock:
                        self._last_error = str(error)
                        self._connected = False

                sock = factory(
                    self.config.websocket_url,
                    on_open=on_open,
                    on_message=lambda _sock, msg: self.handle_frame(msg),
                    on_error=on_error,
                    on_close=on_close,
                )
                with self._lock:
                    self._socket = sock
                sock.run_forever(ping_interval=25, ping_timeout=10)
                delay = 2 if self._stop.is_set() else min(delay * 2, 60)
            except Exception as exc:
                LOG.exception("Ticker stream disconnected")
                with self._lock:
                    self._last_error = str(exc)
                delay = min(delay * 2, 60)
            finally:
                with self._lock:
                    self._socket = None
                    self._connected = False
            self._stop.wait(delay)
