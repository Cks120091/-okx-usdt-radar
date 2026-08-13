from __future__ import annotations

import json
import random
import threading
import time
from collections import deque
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import Candle, Instrument, MarketContext, Ticker


class OKXAPIError(RuntimeError):
    pass


class SlidingWindowRateLimiter:
    """A conservative process-wide limiter for OKX public REST calls."""

    def __init__(self, max_requests: int = 18, window_seconds: float = 2.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            wait_for = 0.0
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.window_seconds:
                    self._calls.popleft()
                if len(self._calls) < self.max_requests:
                    self._calls.append(now)
                    return
                wait_for = self.window_seconds - (now - self._calls[0]) + 0.01
            time.sleep(max(wait_for, 0.01))


class OKXPublicClient:
    """Public market-data client. It never accepts or sends private API keys."""

    def __init__(
        self,
        base_url: str = "https://www.okx.com",
        timeout_seconds: float = 12.0,
        retries: int = 3,
        rate_limit_requests: int = 18,
        execution_notional_usdt: float = 1_000.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.rate_limiter = SlidingWindowRateLimiter(rate_limit_requests, 2.0)
        self.execution_notional_usdt = max(0.0, execution_notional_usdt)
        self._instrument_meta: dict[str, Instrument] = {}
        self._metrics_lock = threading.Lock()
        self._metrics: dict[str, Any] = {}
        self.reset_metrics()

    def reset_metrics(self) -> None:
        with self._metrics_lock:
            self._metrics = {
                "requests": 0,
                "successful_requests": 0,
                "retries": 0,
                "errors": 0,
                "rate_limit_errors": 0,
                "endpoint_requests": {},
                "request_seconds": 0.0,
            }

    def metrics_snapshot(self) -> dict[str, Any]:
        with self._metrics_lock:
            payload = dict(self._metrics)
            payload["endpoint_requests"] = dict(self._metrics["endpoint_requests"])
        payload["request_seconds"] = round(float(payload["request_seconds"]), 3)
        return payload

    def _metric(self, key: str, amount: float = 1.0, endpoint: str | None = None) -> None:
        with self._metrics_lock:
            self._metrics[key] = self._metrics.get(key, 0) + amount
            if endpoint is not None:
                counts = self._metrics["endpoint_requests"]
                counts[endpoint] = counts.get(endpoint, 0) + 1

    def _get(self, path: str, params: dict[str, Any]) -> list[Any]:
        query = urlencode({key: str(value) for key, value in params.items()})
        url = f"{self.base_url}{path}?{query}"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self.rate_limiter.acquire()
            request_started = time.monotonic()
            self._metric("requests", endpoint=path)
            request = Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "okx-usdt-perp-radar/0.1 (public-data-only)",
                },
            )
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if str(payload.get("code")) != "0":
                    raise OKXAPIError(
                        f"OKX code={payload.get('code')}: {payload.get('msg', 'unknown error')}"
                    )
                data = payload.get("data")
                if not isinstance(data, list):
                    raise OKXAPIError("OKX response did not contain a data list")
                self._metric("successful_requests")
                self._metric("request_seconds", time.monotonic() - request_started)
                return data
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OKXAPIError) as exc:
                self._metric("request_seconds", time.monotonic() - request_started)
                self._metric("errors")
                if (
                    isinstance(exc, HTTPError)
                    and exc.code == 429
                    or "code=50011" in str(exc)
                    or "rate limit" in str(exc).lower()
                ):
                    self._metric("rate_limit_errors")
                last_error = exc
                if attempt >= self.retries:
                    break
                self._metric("retries")
                delay = (0.45 * (2**attempt)) + random.uniform(0.0, 0.15)
                time.sleep(delay)
        raise OKXAPIError(f"GET {path} failed after retries: {last_error}")

    def get_usdt_swap_instruments(self) -> list[Instrument]:
        data = self._get("/api/v5/public/instruments", {"instType": "SWAP"})
        instruments: list[Instrument] = []
        for row in data:
            inst_id = str(row.get("instId", ""))
            settle_ccy = str(row.get("settleCcy", ""))
            state = str(row.get("state", ""))
            ct_type = str(row.get("ctType", ""))
            if (
                state == "live"
                and settle_ccy == "USDT"
                and inst_id.endswith("-USDT-SWAP")
                and ct_type in {"linear", ""}
            ):
                instrument = Instrument(
                    inst_id=inst_id,
                    state=state,
                    settle_ccy=settle_ccy,
                    ct_type=ct_type,
                    tick_size=_float(row.get("tickSz")),
                    list_time=_int(row.get("listTime")),
                    contract_value=max(_float(row.get("ctVal"), 1.0), 0.0),
                    contract_multiplier=max(_float(row.get("ctMult"), 1.0), 0.0),
                    contract_value_ccy=str(row.get("ctValCcy", "")),
                )
                instruments.append(instrument)
        ordered = sorted(instruments, key=lambda item: item.inst_id)
        self._instrument_meta = {item.inst_id: item for item in ordered}
        return ordered

    def get_swap_tickers(self) -> dict[str, Ticker]:
        data = self._get("/api/v5/market/tickers", {"instType": "SWAP"})
        tickers: dict[str, Ticker] = {}
        for row in data:
            inst_id = str(row.get("instId", ""))
            if not inst_id.endswith("-USDT-SWAP"):
                continue
            ticker = Ticker(
                inst_id=inst_id,
                last=_float(row.get("last")),
                bid=_float(row.get("bidPx")),
                ask=_float(row.get("askPx")),
                ts=_int(row.get("ts")),
            )
            tickers[inst_id] = ticker
        return tickers

    def get_candles(self, inst_id: str, bar: str, limit: int = 100) -> list[Candle]:
        data = self._get(
            "/api/v5/market/candles",
            {"instId": inst_id, "bar": bar, "limit": min(max(limit, 1), 300)},
        )
        candles: list[Candle] = []
        for row in data:
            if not isinstance(row, list) or len(row) < 9:
                continue
            candle = Candle(
                ts=_int(row[0]),
                open=_float(row[1]),
                high=_float(row[2]),
                low=_float(row[3]),
                close=_float(row[4]),
                volume=_float(row[5]),
                quote_volume=_float(row[7]),
                confirmed=str(row[8]) == "1",
            )
            if candle.confirmed:
                candles.append(candle)
        candles.sort(key=lambda item: item.ts)
        return candles

    def get_open_interest_usd(self) -> dict[str, float]:
        data = self._get("/api/v5/public/open-interest", {"instType": "SWAP"})
        return {
            str(row.get("instId")): _float(row.get("oiUsd"))
            for row in data
            if str(row.get("instId", "")).endswith("-USDT-SWAP")
        }

    def get_market_context(
        self,
        inst_id: str,
        open_interest_usd: float | None = None,
    ) -> MarketContext:
        failures: list[str] = []
        sampled_at = 0
        funding_rate: float | None = None
        order_book_imbalance: float | None = None
        taker_buy_ratio: float | None = None
        bid_depth_usd: float | None = None
        ask_depth_usd: float | None = None
        buy_slippage_pct: float | None = None
        sell_slippage_pct: float | None = None
        execution_notional = max(0.0, getattr(self, "execution_notional_usdt", 0.0))

        try:
            rows = self._get("/api/v5/public/funding-rate", {"instId": inst_id})
            if not rows:
                raise OKXAPIError("empty funding-rate response")
            funding_rate = _float_or_none(rows[0].get("fundingRate"))
            sampled_at = max(sampled_at, _int(rows[0].get("ts")))
            if funding_rate is None:
                raise OKXAPIError("missing fundingRate")
        except Exception as exc:
            failures.append(f"funding_rate: {exc}")

        try:
            rows = self._get("/api/v5/market/books", {"instId": inst_id, "sz": 20})
            if not rows:
                raise OKXAPIError("empty order-book response")
            bids = rows[0].get("bids", [])
            asks = rows[0].get("asks", [])
            bid_depth = _weighted_depth(bids)
            ask_depth = _weighted_depth(asks)
            total_depth = bid_depth + ask_depth
            if total_depth <= 0:
                raise OKXAPIError("order-book depth is zero")
            order_book_imbalance = (bid_depth - ask_depth) / total_depth
            instrument = getattr(self, "_instrument_meta", {}).get(inst_id)
            bid_depth_usd = _book_depth_usd(bids, instrument)
            ask_depth_usd = _book_depth_usd(asks, instrument)
            if execution_notional > 0:
                buy_slippage_pct = _estimated_slippage_pct(
                    asks,
                    execution_notional,
                    instrument,
                    is_buy=True,
                )
                sell_slippage_pct = _estimated_slippage_pct(
                    bids,
                    execution_notional,
                    instrument,
                    is_buy=False,
                )
                if buy_slippage_pct is None or sell_slippage_pct is None:
                    raise OKXAPIError(
                        f"top-20 depth cannot fill {execution_notional:,.0f} USDT"
                    )
            sampled_at = max(sampled_at, _int(rows[0].get("ts")))
        except Exception as exc:
            failures.append(f"order_book: {exc}")

        try:
            rows = self._get("/api/v5/market/trades", {"instId": inst_id, "limit": 100})
            buy_size = sum(_float(row.get("sz")) for row in rows if row.get("side") == "buy")
            sell_size = sum(_float(row.get("sz")) for row in rows if row.get("side") == "sell")
            total_size = buy_size + sell_size
            if total_size <= 0:
                raise OKXAPIError("recent taker volume is zero")
            taker_buy_ratio = buy_size / total_size
            sampled_at = max(sampled_at, max((_int(row.get("ts")) for row in rows), default=0))
        except Exception as exc:
            failures.append(f"taker_trades: {exc}")

        return MarketContext(
            inst_id=inst_id,
            open_interest_usd=open_interest_usd,
            funding_rate=funding_rate,
            order_book_imbalance=order_book_imbalance,
            taker_buy_ratio=taker_buy_ratio,
            sampled_at=sampled_at,
            failures=failures,
            bid_depth_usd=bid_depth_usd,
            ask_depth_usd=ask_depth_usd,
            buy_slippage_pct=buy_slippage_pct,
            sell_slippage_pct=sell_slippage_pct,
            execution_notional_usdt=execution_notional,
        )


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _weighted_depth(levels: list[Any]) -> float:
    total = 0.0
    for index, level in enumerate(levels):
        if not isinstance(level, list) or len(level) < 2:
            continue
        total += _float(level[1]) / (1.0 + (index * 0.12))
    return total


def _level_quote_notional(
    level: list[Any],
    instrument: Instrument | None,
) -> tuple[float, float]:
    if not isinstance(level, list) or len(level) < 2:
        return 0.0, 0.0
    price = _float(level[0])
    contracts = _float(level[1])
    if price <= 0 or contracts <= 0:
        return 0.0, 0.0
    if instrument is None:
        base_amount = contracts
        return price, base_amount * price
    contract_size = instrument.contract_value * instrument.contract_multiplier
    if contract_size <= 0:
        contract_size = 1.0
    if instrument.contract_value_ccy in {instrument.settle_ccy, "USDT", "USDC"}:
        quote_notional = contracts * contract_size
    else:
        quote_notional = contracts * contract_size * price
    return price, quote_notional


def _book_depth_usd(levels: list[Any], instrument: Instrument | None) -> float:
    return round(
        sum(_level_quote_notional(level, instrument)[1] for level in levels),
        2,
    )


def _estimated_slippage_pct(
    levels: list[Any],
    target_notional: float,
    instrument: Instrument | None,
    *,
    is_buy: bool,
) -> float | None:
    if target_notional <= 0 or not levels:
        return 0.0
    best_price, _ = _level_quote_notional(levels[0], instrument)
    if best_price <= 0:
        return None
    remaining = target_notional
    filled_quote = 0.0
    filled_base = 0.0
    for level in levels:
        price, available_quote = _level_quote_notional(level, instrument)
        if price <= 0 or available_quote <= 0:
            continue
        take_quote = min(remaining, available_quote)
        filled_quote += take_quote
        filled_base += take_quote / price
        remaining -= take_quote
        if remaining <= max(target_notional * 1e-9, 1e-9):
            break
    if remaining > max(target_notional * 1e-6, 0.01) or filled_base <= 0:
        return None
    average_price = filled_quote / filled_base
    impact = (
        (average_price / best_price - 1.0)
        if is_buy
        else (1.0 - average_price / best_price)
    ) * 100.0
    return round(max(impact, 0.0), 5)
