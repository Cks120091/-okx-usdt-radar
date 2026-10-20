"""Optional read-only all-market OKX ticker fanout.

Every LIVE linear USDT swap is subscribed, including low-turnover contracts;
volume gates apply only later to candidate *analysis*, never market coverage.
A periodic public REST instruments refresh follows listings/delistings.
No trading credentials, user positions, or private endpoints.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

LOG = logging.getLogger("okx_radar.full_market")


class FullMarketStream:
    def __init__(
        self, client: Any, websocket_url: str, on_frame: Callable[[str], None],
        *, ws_factory: Callable[..., Any] | None = None,
        shard_size: int = 80, subscribe_batch: int = 40,
        refresh_seconds: int = 300, clock: Callable[[], float] = time.time,
    ):
        if not 1 <= shard_size <= 120 or not 1 <= subscribe_batch <= 40:
            raise ValueError("Market stream shard/batch limit is invalid")
        if refresh_seconds < 60:
            raise ValueError("Universe refresh must be at least 60 seconds")
        self.client = client
        self.websocket_url = websocket_url
        self.on_frame = on_frame
        self.ws_factory = ws_factory
        self.shard_size = shard_size
        self.subscribe_batch = subscribe_batch
        self.refresh_seconds = refresh_seconds
        self.clock = clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._discovery: threading.Thread | None = None
        self._threads: list[threading.Thread] = []
        self._generation = 0
        self._symbols: tuple[str, ...] = ()
        self._volumes: dict[str, float] = {}
        self._sockets: dict[int, Any] = {}
        self._connected: set[int] = set()
        self._acknowledged: dict[int, set[str]] = {}
        self._last_universe_at: float | None = None
        self._last_error: str | None = None

    @property
    def symbols(self) -> tuple[str, ...]:
        with self._lock:
            return self._symbols

    def quote_volume(self, symbol: str) -> float | None:
        with self._lock:
            return self._volumes.get(symbol)

    def start(self) -> None:
        if self._discovery is not None and self._discovery.is_alive():
            return
        self._stop.clear()
        self._discovery = threading.Thread(
            target=self._discover_loop, name="okx-universe-refresh", daemon=True
        )
        self._discovery.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._generation += 1
            sockets = list(self._sockets.values())
        for sock in sockets:
            try:
                sock.close()
            except Exception:
                LOG.warning("Failed to close market WebSocket")
        if self._discovery and self._discovery.is_alive():
            self._discovery.join(timeout=3)
        for thread in list(self._threads):
            if thread.is_alive():
                thread.join(timeout=2)

    def refresh_universe(self) -> bool:
        """Public-only discovery. A discovery failure cannot erase good targets."""
        instruments = self.client.get_usdt_swap_instruments()
        symbols = tuple(sorted({
            item.inst_id for item in instruments
            if str(getattr(item, "inst_id", "")).endswith("-USDT-SWAP")
            and str(getattr(item, "state", "")).lower() == "live"
        }))
        if not symbols:
            raise RuntimeError("OKX live USDT swap universe is empty")
        # This snapshot is the normalized USDT quote turnover from the
        # existing public client, not a guess based on contract counts.
        tickers = self.client.get_swap_tickers()
        volumes: dict[str, float] = {}
        for symbol in symbols:
            ticker = tickers.get(symbol)
            volume = getattr(ticker, "quote_volume_24h", None)
            if volume is not None:
                try:
                    volume = float(volume)
                except (TypeError, ValueError, OverflowError):
                    continue
                if 0 <= volume < float("inf"):
                    volumes[symbol] = volume
        self.update_universe(symbols, volumes)
        return True

    def _discover_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_universe()
            except Exception as exc:
                # Report failure and retry; never silently claim full coverage.
                LOG.warning("Public universe refresh unavailable: %s", type(exc).__name__)
                with self._lock:
                    self._last_error = "OKX 合約清單更新失敗"
            self._stop.wait(self.refresh_seconds)

    def update_universe(self, symbols: tuple[str, ...] | list[str],
                        volumes: dict[str, float] | None = None) -> None:
        """Atomically replace listed contracts; reuse sockets if unchanged."""
        cleaned = tuple(sorted(set(symbols)))
        with self._lock:
            if self._stop.is_set():
                return
            self._volumes = {
                k: v for k, v in (volumes or {}).items() if k in cleaned
            }
            self._last_universe_at = self.clock()
            if cleaned == self._symbols:
                return
            old_sockets = list(self._sockets.values())
            self._generation += 1
            generation = self._generation
            self._symbols = cleaned
            self._connected.clear()
            self._acknowledged.clear()
            self._sockets = {}
            shards = [
                cleaned[i:i + self.shard_size]
                for i in range(0, len(cleaned), self.shard_size)
            ]
            self._threads = [
                threading.Thread(
                    target=self._socket_loop,
                    args=(index, group, generation),
                    name=f"okx-ticker-shard-{index}", daemon=True,
                )
                for index, group in enumerate(shards)
            ]
            threads = list(self._threads)
        for sock in old_sockets:
            try:
                sock.close()
            except Exception:
                LOG.warning("Old market socket close failed")
        for thread in threads:
            thread.start()

    def _current(self, generation: int) -> bool:
        with self._lock:
            return not self._stop.is_set() and generation == self._generation

    def _socket_loop(self, index: int, symbols: tuple[str, ...],
                     generation: int) -> None:
        delay = 2.0
        allowed = set(symbols)
        while self._current(generation):
            try:
                if self.ws_factory is None:
                    from websocket import WebSocketApp
                    factory = WebSocketApp
                else:
                    factory = self.ws_factory

                def on_open(sock: Any) -> None:
                    if not self._current(generation):
                        sock.close()
                        return
                    with self._lock:
                        self._connected.add(index)
                        self._acknowledged[index] = set()
                    for start in range(0, len(symbols), self.subscribe_batch):
                        if not self._current(generation):
                            return
                        chunk = symbols[start:start + self.subscribe_batch]
                        sock.send(json.dumps({
                            "op": "subscribe",
                            "args": [{"channel": "tickers", "instId": name}
                                     for name in chunk],
                        }, separators=(",", ":")))

                def on_message(_sock: Any, raw: str) -> None:
                    if not self._current(generation):
                        return
                    try:
                        packet = json.loads(raw)
                    except (ValueError, TypeError):
                        return
                    if isinstance(packet, dict):
                        event = packet.get("event")
                        arg = packet.get("arg") or {}
                        symbol = arg.get("instId") if isinstance(arg, dict) else None
                        if event == "subscribe" and symbol in allowed:
                            with self._lock:
                                self._acknowledged.setdefault(index, set()).add(symbol)
                        elif event == "error":
                            with self._lock:
                                self._last_error = "OKX 行情訂閱遭拒絕"
                    self.on_frame(raw)

                def on_close(_sock: Any, _status: Any, _reason: Any) -> None:
                    with self._lock:
                        if generation == self._generation:
                            self._connected.discard(index)
                            self._acknowledged.pop(index, None)

                def on_error(_sock: Any, err: Any) -> None:
                    with self._lock:
                        if generation == self._generation:
                            self._last_error = "OKX 行情連線異常"
                            self._connected.discard(index)
                            self._acknowledged.pop(index, None)
                    LOG.warning("OKX shard %d disconnected: %s", index, type(err).__name__)

                sock = factory(self.websocket_url, on_open=on_open,
                               on_message=on_message, on_close=on_close,
                               on_error=on_error)
                with self._lock:
                    if generation != self._generation:
                        return
                    self._sockets[index] = sock
                sock.run_forever(ping_interval=25, ping_timeout=10)
                delay = 2.0 if self._stop.is_set() else min(delay * 2, 60.0)
            except Exception as exc:
                LOG.warning("OKX shard %d failed: %s", index, type(exc).__name__)
                with self._lock:
                    self._last_error = "OKX 行情連線異常"
                delay = min(delay * 2, 60.0)
            finally:
                with self._lock:
                    if generation == self._generation:
                        self._connected.discard(index)
                        self._acknowledged.pop(index, None)
                        self._sockets.pop(index, None)
            if self._stop.wait(delay):
                break

    def status(self) -> dict[str, Any]:
        with self._lock:
            targets = len(self._symbols)
            shards = (targets + self.shard_size - 1) // self.shard_size
            ack = len(set().union(*self._acknowledged.values())) if self._acknowledged else 0
            return {
                "mode": "FULL_UNIVERSE",
                "listed_symbols": targets,
                "expected_shards": shards,
                "connected_shards": len(self._connected),
                "acknowledged_symbols": ack,
                "subscription_coverage_pct": round(100 * ack / targets, 1)
                if targets else 0.0,
                "coverage_complete": bool(targets and ack == targets
                                          and len(self._connected) == shards),
                "last_universe_refresh_at": (
                    datetime.fromtimestamp(self._last_universe_at, timezone.utc).isoformat()
                    if self._last_universe_at else None
                ),
                "last_error": self._last_error,
                "analysis_only": True,
                "auto_ordering": False,
            }
