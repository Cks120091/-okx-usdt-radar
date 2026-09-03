from __future__ import annotations

import json
import math
import random
import threading
import time
from copy import deepcopy
from collections import deque
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .models import Candle, Instrument, MarketContext, Ticker


def _request_overrides(
    request_retries: int | None,
    request_timeout_seconds: float | None,
) -> dict[str, int | float]:
    """Forward per-request limits only when a caller explicitly supplies them."""

    options: dict[str, int | float] = {}
    if request_retries is not None:
        options["request_retries"] = request_retries
    if request_timeout_seconds is not None:
        options["request_timeout_seconds"] = request_timeout_seconds
    return options


class OKXAPIError(RuntimeError):
    """An OKX transport or application error with an optional exact code.

    ``code`` is intentionally absent for mixed/uncertain failures.  Callers
    must never infer a semantic result (for example, "instrument not found")
    merely because one host's text appears somewhere inside an aggregate
    network error.
    """

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = str(code) if code is not None else None


class SlidingWindowRateLimiter:
    """A conservative process-wide limiter for OKX public REST calls."""

    def __init__(self, max_requests: int = 18, window_seconds: float = 2.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()
        self._blocked_until = 0.0

    def penalize(self, seconds: float = 2.05) -> None:
        """Apply one shared cooldown after OKX reports a rate-limit error."""

        with self._lock:
            self._blocked_until = max(
                self._blocked_until,
                time.monotonic() + max(float(seconds), 0.0),
            )

    def acquire(self) -> None:
        while True:
            wait_for = 0.0
            with self._lock:
                now = time.monotonic()
                if now < self._blocked_until:
                    wait_for = self._blocked_until - now
                else:
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
        base_url: str = "https://openapi.okx.com",
        timeout_seconds: float = 12.0,
        retries: int = 3,
        rate_limit_requests: int = 30,
        execution_notional_usdt: float = 1_000.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.base_urls = self._request_base_urls(self.base_url)
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.rate_limiter = SlidingWindowRateLimiter(
            min(rate_limit_requests, 18),
            2.0,
        )
        self.candle_rate_limiter = SlidingWindowRateLimiter(
            rate_limit_requests,
            2.0,
        )
        # OKX publishes a separate 10 requests / 2 seconds quota for the
        # contract open-interest history endpoint.  Keeping its budget
        # separate prevents a historical lookback from consuming the quota
        # used by current market-data requests (and vice versa).
        self.open_interest_history_rate_limiter = SlidingWindowRateLimiter(
            10,
            2.0,
        )
        self.execution_notional_usdt = max(0.0, execution_notional_usdt)
        self._instrument_meta: dict[str, Instrument] = {}
        self._instrument_meta_expires_at: dict[str, float] = {}
        self._open_interest_timestamps: dict[str, int] = {}
        self._cache: dict[str, tuple[float, list[Any]]] = {}
        self._cache_lock = threading.Lock()
        self._metrics_lock = threading.Lock()
        self._metrics: dict[str, Any] = {}
        self.reset_metrics()

    @staticmethod
    def _request_base_urls(base_url: str) -> tuple[str, ...]:
        """Use both current official OKX REST hosts, but never alter custom hosts."""

        normalized = str(base_url or "").rstrip("/")
        host = (urlparse(normalized).hostname or "").lower()
        if host not in {"openapi.okx.com", "www.okx.com"}:
            return (normalized,)
        candidates = (normalized, "https://openapi.okx.com", "https://www.okx.com")
        return tuple(dict.fromkeys(candidates))

    def reset_metrics(self) -> None:
        with self._metrics_lock:
            self._metrics = {
                "requests": 0,
                "successful_requests": 0,
                "retries": 0,
                "errors": 0,
                "rate_limit_errors": 0,
                "timeouts": 0,
                "cache_hits": 0,
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

    def _get(
        self,
        path: str,
        params: dict[str, Any],
        cache_ttl_seconds: float | None = None,
        *,
        request_retries: int | None = None,
        request_timeout_seconds: float | None = None,
    ) -> list[Any]:
        if cache_ttl_seconds is None:
            cache_ttl_seconds = {
                "/api/v5/public/instruments": 300.0,
                "/api/v5/market/tickers": 2.0,
                "/api/v5/market/candles": 3.0,
                "/api/v5/public/open-interest": 3.0,
            }.get(path, 0.0)
        query = urlencode({key: str(value) for key, value in params.items()})
        cache_key = f"{path}?{query}"
        if cache_ttl_seconds > 0:
            with self._cache_lock:
                cached = self._cache.get(cache_key)
                if cached and cached[0] > time.monotonic():
                    self._metric("cache_hits")
                    return deepcopy(cached[1])
        last_error: Exception | None = None
        attempt_errors: list[Exception] = []
        host_errors: dict[str, str] = {}
        retry_limit = (
            self.retries
            if request_retries is None
            else max(0, min(int(request_retries), self.retries))
        )
        timeout_seconds = (
            self.timeout_seconds
            if request_timeout_seconds is None
            else max(1.0, min(float(request_timeout_seconds), self.timeout_seconds))
        )
        for attempt in range(retry_limit + 1):
            request_base_url = self.base_urls[attempt % len(self.base_urls)]
            url = f"{request_base_url}{path}?{query}"
            if path == "/api/v5/market/candles":
                limiter = self.candle_rate_limiter
            elif path == "/api/v5/rubik/stat/contracts/open-interest-history":
                limiter = self.open_interest_history_rate_limiter
            else:
                limiter = self.rate_limiter
            limiter.acquire()
            request_started = time.monotonic()
            self._metric("requests", endpoint=path)
            request = Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "okx-usdt-perp-radar/3.4 (public-data-only)",
                },
            )
            try:
                with urlopen(request, timeout=timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if str(payload.get("code")) != "0":
                    response_code = str(payload.get("code"))
                    raise OKXAPIError(
                        f"OKX code={response_code}: {payload.get('msg', 'unknown error')}",
                        code=response_code,
                    )
                data = payload.get("data")
                if not isinstance(data, list):
                    raise OKXAPIError("OKX response did not contain a data list")
                self._metric("successful_requests")
                self._metric("request_seconds", time.monotonic() - request_started)
                if cache_ttl_seconds > 0:
                    with self._cache_lock:
                        self._cache[cache_key] = (
                            time.monotonic() + cache_ttl_seconds,
                            deepcopy(data),
                        )
                return data
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OKXAPIError) as exc:
                self._metric("request_seconds", time.monotonic() - request_started)
                self._metric("errors")
                if isinstance(exc, (TimeoutError, URLError)) and "timed out" in str(exc).lower():
                    self._metric("timeouts")
                rate_limited = (
                    (isinstance(exc, HTTPError) and exc.code == 429)
                    or "code=50011" in str(exc)
                    or "rate limit" in str(exc).lower()
                )
                if rate_limited:
                    self._metric("rate_limit_errors")
                    limiter.penalize(2.05)
                last_error = exc
                attempt_errors.append(exc)
                host_errors[urlparse(request_base_url).hostname or request_base_url] = str(exc)
                if attempt >= retry_limit:
                    break
                self._metric("retries")
                delay = (0.45 * (2**attempt)) + random.uniform(0.0, 0.15)
                time.sleep(delay)
        attempted = "; ".join(f"{host}: {error}" for host, error in host_errors.items())
        # Only expose a semantic OKX code when every attempt independently
        # returned that exact application error.  A 51001 from one official
        # host mixed with a timeout/502 from the other remains an uncertain API
        # failure and must not be relabelled as a nonexistent symbol.
        exact_code = (
            "51001"
            if attempt_errors
            and all(
                isinstance(error, OKXAPIError) and error.code == "51001"
                for error in attempt_errors
            )
            else None
        )
        raise OKXAPIError(
            f"GET {path} failed after retries across official endpoints: "
            f"{attempted or last_error}",
            code=exact_code,
        )

    def get_usdt_swap_instruments(self) -> list[Instrument]:
        data = self._get("/api/v5/public/instruments", {"instType": "SWAP"})
        instruments: list[Instrument] = []
        for row in data:
            instrument = _instrument_from_row(row)
            if instrument is not None:
                instruments.append(instrument)
        ordered = sorted(instruments, key=lambda item: item.inst_id)
        self._instrument_meta = {item.inst_id: item for item in ordered}
        meta_expiry = time.monotonic() + 300.0
        self._instrument_meta_expires_at = {
            item.inst_id: meta_expiry for item in ordered
        }
        return ordered

    def get_usdt_swap_instrument(
        self,
        inst_id: str,
        *,
        request_retries: int | None = None,
        request_timeout_seconds: float | None = None,
    ) -> Instrument | None:
        """Fetch one live linear USDT perpetual selected by the user."""

        instrument_meta = getattr(self, "_instrument_meta", {})
        instrument_meta_expiry = getattr(self, "_instrument_meta_expires_at", {})
        cached = instrument_meta.get(inst_id)
        if (
            cached is not None
            and instrument_meta_expiry.get(inst_id, 0.0) > time.monotonic()
        ):
            metric = getattr(self, "_metric", None)
            if callable(metric):
                metric("cache_hits")
            return cached

        try:
            data = self._get(
                "/api/v5/public/instruments",
                {"instType": "SWAP", "instId": inst_id},
                **_request_overrides(
                    request_retries,
                    request_timeout_seconds,
                ),
            )
        except OKXAPIError as exc:
            # OKX answers an unknown ``instId`` with application error 51001
            # instead of an empty successful list.  This is a user/input
            # result, not a temporary market-data outage.  Returning ``None``
            # lets the single-symbol scanner produce its existing explicit
            # "live contract not found" validation error (HTTP 422), rather
            # than incorrectly presenting a retryable HTTP 502.
            if exc.code == "51001":
                return None
            cached = self._instrument_meta.get(inst_id)
            if cached is not None:
                return cached
            raise
        instrument = next(
            (
                parsed
                for row in data
                if (parsed := _instrument_from_row(row)) is not None
                and parsed.inst_id == inst_id
            ),
            None,
        )
        if instrument is not None:
            instrument_meta = getattr(self, "_instrument_meta", {})
            instrument_meta[instrument.inst_id] = instrument
            self._instrument_meta = instrument_meta
            instrument_meta_expiry = getattr(self, "_instrument_meta_expires_at", {})
            instrument_meta_expiry[instrument.inst_id] = (
                time.monotonic() + 300.0
            )
            self._instrument_meta_expires_at = instrument_meta_expiry
        return instrument

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

    def get_ticker(
        self,
        inst_id: str,
        *,
        request_retries: int | None = None,
        request_timeout_seconds: float | None = None,
    ) -> Ticker:
        """Fetch one live ticker for an explicit user-requested preflight check."""

        rows = self._get(
            "/api/v5/market/ticker",
            {"instId": inst_id},
            **_request_overrides(
                request_retries,
                request_timeout_seconds,
            ),
        )
        row = next(
            (item for item in rows if str(item.get("instId", "")) == inst_id),
            None,
        )
        if row is None:
            raise OKXAPIError(f"ticker unavailable for {inst_id}")
        ticker = Ticker(
            inst_id=inst_id,
            last=_float(row.get("last")),
            bid=_float(row.get("bidPx")),
            ask=_float(row.get("askPx")),
            ts=_int(row.get("ts")),
        )
        if ticker.last <= 0 or ticker.bid <= 0 or ticker.ask <= 0:
            raise OKXAPIError(f"ticker contains invalid prices for {inst_id}")
        return ticker

    def get_execution_context(self, inst_id: str) -> MarketContext:
        """Fetch only the live order-book inputs needed for a preflight check.

        A preflight is deliberately smaller than a full market scan: it does not
        fetch funding, trades, candles or the complete universe.  It only needs
        current depth and slippage for one signal selected by the user.
        """

        failures: list[str] = []
        instrument = self._instrument_meta.get(inst_id)
        if instrument is None:
            try:
                rows = self._get(
                    "/api/v5/public/instruments",
                    {"instType": "SWAP", "instId": inst_id},
                )
                instrument = next(
                    (
                        parsed
                        for row in rows
                        if (parsed := _instrument_from_row(row)) is not None
                        and parsed.inst_id == inst_id
                    ),
                    None,
                )
                if instrument is None:
                    raise OKXAPIError("live linear USDT contract metadata unavailable")
                self._instrument_meta[inst_id] = instrument
            except Exception as exc:
                failures.append(f"instrument_meta: {exc}")

        order_book_imbalance: float | None = None
        bid_depth_usd: float | None = None
        ask_depth_usd: float | None = None
        buy_slippage_pct: float | None = None
        sell_slippage_pct: float | None = None
        best_bid: float | None = None
        best_ask: float | None = None
        sampled_at = 0
        source_timestamps: dict[str, int] = {}
        execution_notional = max(0.0, self.execution_notional_usdt)

        try:
            rows = self._get("/api/v5/market/books", {"instId": inst_id, "sz": 20})
            if not rows:
                raise OKXAPIError("empty order-book response")
            bids = rows[0].get("bids", [])
            asks = rows[0].get("asks", [])
            best_bid = _float_or_none(bids[0][0]) if bids and isinstance(bids[0], list) else None
            best_ask = _float_or_none(asks[0][0]) if asks and isinstance(asks[0], list) else None
            bid_depth = _weighted_depth(bids)
            ask_depth = _weighted_depth(asks)
            total_depth = bid_depth + ask_depth
            if total_depth <= 0 or best_bid is None or best_ask is None:
                raise OKXAPIError("order-book depth is zero")
            order_book_imbalance = (bid_depth - ask_depth) / total_depth
            if instrument is not None:
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
            sampled_at = _int(rows[0].get("ts"))
            if sampled_at:
                source_timestamps["order_book"] = sampled_at
        except Exception as exc:
            failures.append(f"order_book: {exc}")

        return MarketContext(
            inst_id=inst_id,
            open_interest_usd=None,
            funding_rate=None,
            order_book_imbalance=order_book_imbalance,
            taker_buy_ratio=None,
            sampled_at=sampled_at,
            failures=failures,
            bid_depth_usd=bid_depth_usd,
            ask_depth_usd=ask_depth_usd,
            buy_slippage_pct=buy_slippage_pct,
            sell_slippage_pct=sell_slippage_pct,
            execution_notional_usdt=execution_notional,
            best_bid=best_bid,
            best_ask=best_ask,
            source_timestamps=source_timestamps,
            data_quality={
                "available": sorted(source_timestamps),
                "failures": list(failures),
                "sampled_at": sampled_at,
            },
        )

    def get_candles(
        self,
        inst_id: str,
        bar: str,
        limit: int = 100,
        *,
        request_retries: int | None = None,
        request_timeout_seconds: float | None = None,
    ) -> list[Candle]:
        data = self._get(
            "/api/v5/market/candles",
            {"instId": inst_id, "bar": bar, "limit": min(max(limit, 1), 300)},
            **_request_overrides(
                request_retries,
                request_timeout_seconds,
            ),
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
        output = {
            str(row.get("instId")): _float(row.get("oiUsd"))
            for row in data
            if str(row.get("instId", "")).endswith("-USDT-SWAP")
        }
        self._open_interest_timestamps = {
            str(row.get("instId")): _int(row.get("ts"))
            for row in data
            if str(row.get("instId", "")).endswith("-USDT-SWAP")
            and _int(row.get("ts")) > 0
        }
        return output

    def get_open_interest_for(
        self,
        inst_id: str,
        *,
        request_retries: int | None = None,
        request_timeout_seconds: float | None = None,
    ) -> float | None:
        """Fetch open interest for one instrument without loading the universe."""

        data = self._get(
            "/api/v5/public/open-interest",
            {"instType": "SWAP", "instId": inst_id},
            **_request_overrides(
                request_retries,
                request_timeout_seconds,
            ),
        )
        row = next(
            (item for item in data if str(item.get("instId", "")) == inst_id),
            None,
        )
        if row is None:
            return None
        timestamp = _int(row.get("ts"))
        if timestamp > 0:
            timestamps = getattr(self, "_open_interest_timestamps", {})
            timestamps[inst_id] = timestamp
            self._open_interest_timestamps = timestamps
        return _float_or_none(row.get("oiUsd"))

    def get_open_interest_history(
        self,
        inst_id: str,
        period: str = "5m",
        limit: int = 20,
        end_ms: int | None = None,
        *,
        request_retries: int | None = None,
        request_timeout_seconds: float | None = None,
    ) -> list[dict[str, int | float]]:
        """Return validated raw OI levels for one linear USDT contract.

        ``oi`` (contracts) and ``oiCcy`` are retained as the stable levels for
        later trend calculations.  ``oiUsd`` is optional display/liquidity
        context only: its price component means callers must not use it as the
        OI trend input.  Candle-close filtering deliberately belongs to the
        scanner, which owns the confirmed 5-minute close watermark.
        """

        normalized_inst_id = _usdt_contract_inst_id(inst_id)
        normalized_period = str(period or "").strip()
        if not normalized_period:
            raise ValueError("open-interest history period must not be empty")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("open-interest history limit must be between 1 and 100")

        params: dict[str, Any] = {
            "instId": normalized_inst_id,
            "period": normalized_period,
            "limit": limit,
        }
        if end_ms is not None:
            if isinstance(end_ms, bool):
                raise ValueError(
                    "open-interest history end must be a positive timestamp"
                )
            try:
                normalized_end_ms = int(end_ms)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(
                    "open-interest history end must be a positive timestamp"
                ) from exc
            if normalized_end_ms <= 0:
                raise ValueError(
                    "open-interest history end must be a positive timestamp"
                )
            params["end"] = normalized_end_ms

        data = self._get(
            "/api/v5/rubik/stat/contracts/open-interest-history",
            params,
            **_request_overrides(
                request_retries,
                request_timeout_seconds,
            ),
        )
        by_timestamp: dict[int, dict[str, int | float]] = {}
        conflicted_timestamps: set[int] = set()
        for row in data:
            # The documented Rubik response is positional
            # [ts, oi, oiCcy, oiUsd].  Accept named rows too because some OKX
            # SDK adapters normalize the same response into mappings.
            if isinstance(row, (list, tuple)):
                timestamp_raw = row[0] if len(row) > 0 else None
                open_interest_raw = row[1] if len(row) > 1 else None
                open_interest_ccy_raw = row[2] if len(row) > 2 else None
                open_interest_usd_raw = row[3] if len(row) > 3 else None
            elif isinstance(row, dict):
                timestamp_raw = row.get("ts")
                open_interest_raw = row.get("oi")
                open_interest_ccy_raw = row.get("oiCcy")
                open_interest_usd_raw = row.get("oiUsd")
            else:
                continue
            timestamp = _positive_int_or_none(timestamp_raw)
            open_interest = _positive_finite_float_or_none(open_interest_raw)
            open_interest_ccy = _positive_finite_float_or_none(
                open_interest_ccy_raw
            )
            if timestamp is None or (
                open_interest is None and open_interest_ccy is None
            ):
                continue
            parsed: dict[str, int | float] = {"ts": timestamp}
            if open_interest is not None:
                parsed["oi"] = open_interest
            if open_interest_ccy is not None:
                parsed["oiCcy"] = open_interest_ccy
            open_interest_usd = _positive_finite_float_or_none(
                open_interest_usd_raw
            )
            if open_interest_usd is not None:
                parsed["oiUsd"] = open_interest_usd
            if timestamp in conflicted_timestamps:
                continue
            existing = by_timestamp.get(timestamp)
            if existing is not None:
                # Duplicate rows are harmless only when every overlapping
                # raw OI field agrees.  A conflicting endpoint must be
                # discarded completely; choosing whichever row happened to
                # arrive last would manufacture a trend.
                conflicts = any(
                    key in existing
                    and key in parsed
                    and existing[key] != parsed[key]
                    for key in ("oi", "oiCcy")
                )
                if conflicts:
                    by_timestamp.pop(timestamp, None)
                    conflicted_timestamps.add(timestamp)
                    continue
                merged = dict(existing)
                merged.update(parsed)
                by_timestamp[timestamp] = merged
                continue
            by_timestamp[timestamp] = parsed
        return [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]

    def get_continuation_snapshot(
        self,
        inst_id: str,
        horizon: str,
        *,
        since_ms: int | None = None,
        bucket_end_ms: int | None = None,
        request_retries: int | None = 0,
        request_timeout_seconds: float | None = 5.0,
    ) -> dict[str, Any]:
        """Fetch one lightweight, post-signal continuation sample.

        The observer deliberately avoids ``get_market_context``: that method's
        latest-N trade snapshot overlaps between scans and cannot represent a
        fixed one/five-minute average.  Here each trade bucket begins strictly
        after ``since_ms`` and advertises incomplete coverage instead of
        silently treating the latest 500 trades as the whole interval.
        """

        normalized_horizon = str(horizon or "").strip().upper()
        if normalized_horizon not in {"SHORT", "LONG"}:
            raise ValueError("continuation horizon must be SHORT or LONG")
        interval_ms = 60_000 if normalized_horizon == "SHORT" else 300_000
        observed_at_ms = int(time.time() * 1000)
        # Taker flow and K-line price/volume must describe the same exchange-
        # aligned closed bucket.  ``since_ms`` is the prior poll clock and can
        # be almost one full candle out of phase (for example every minute at
        # :59), so it is retained only as observation context and never used
        # as the trade-window boundary.
        if bucket_end_ms is None:
            bucket_end_ms = observed_at_ms - (observed_at_ms % interval_ms)
        else:
            bucket_end_ms = int(bucket_end_ms)
            if (
                bucket_end_ms <= 0
                or bucket_end_ms % interval_ms != 0
                or bucket_end_ms > observed_at_ms
            ):
                raise ValueError("continuation bucket end must be a closed bar boundary")
        bucket_start_ms = bucket_end_ms - interval_ms
        options = _request_overrides(
            request_retries,
            request_timeout_seconds,
        )
        sample: dict[str, Any] = {
            "observed_at_ms": observed_at_ms,
            "bucket_start_ms": bucket_start_ms,
            "bucket_end_ms": bucket_end_ms,
            "previous_observed_at_ms": (
                int(since_ms)
                if isinstance(since_ms, (int, float)) and int(since_ms) > 0
                else None
            ),
            "horizon": normalized_horizon,
            "failures": [],
            "source_timestamps": {},
        }

        try:
            rows = self._get(
                "/api/v5/public/open-interest",
                {"instType": "SWAP", "instId": inst_id},
                **options,
            )
            row = next(
                (item for item in rows if str(item.get("instId", "")) == inst_id),
                None,
            )
            if row is None:
                raise OKXAPIError("empty open-interest response")
            oi_timestamp = _int(row.get("ts"))
            oi_checked_at_ms = int(time.time() * 1000)
            if (
                oi_timestamp <= 0
                or abs(oi_timestamp - bucket_end_ms) > 15_000
                or oi_timestamp > oi_checked_at_ms + 5_000
            ):
                raise OKXAPIError("stale or invalid open-interest timestamp")
            sample.update(
                {
                    # Contracts are the primary level for trend calculations.
                    # oiUsd remains display/liquidity context only because it
                    # can rise mechanically when the contract price rises.
                    "open_interest_contracts": _float_or_none(row.get("oi")),
                    "open_interest_ccy": _float_or_none(row.get("oiCcy")),
                    "open_interest_usd": _float_or_none(row.get("oiUsd")),
                }
            )
            sample["source_timestamps"]["open_interest"] = oi_timestamp
        except Exception as exc:
            sample["failures"].append(f"open_interest: {exc}")

        trade_limit = 500
        try:
            rows = self._get(
                "/api/v5/market/trades",
                {"instId": inst_id, "limit": trade_limit},
                **options,
            )
            deduplicated: dict[str, dict[str, Any]] = {}
            malformed_rows = 0
            duplicate_rows = 0
            for index, row in enumerate(rows):
                timestamp = _int(row.get("ts"))
                trade_id = str(row.get("tradeId") or "").strip()
                side = str(row.get("side") or "").strip().lower()
                size = _float_or_none(row.get("sz"))
                if (
                    timestamp <= 0
                    or not trade_id
                    or side not in {"buy", "sell"}
                    or size is None
                    or size < 0
                ):
                    malformed_rows += 1
                    continue
                if trade_id in deduplicated:
                    duplicate_rows += 1
                deduplicated[trade_id] = {
                    "tradeId": trade_id,
                    "side": side,
                    "sz": size,
                    "px": _float_or_none(row.get("px")),
                    "ts": timestamp,
                }
            ordered_trades = sorted(
                deduplicated.values(),
                key=lambda row: (_int(row.get("ts")), str(row.get("tradeId") or "")),
            )
            interval_trades = [
                row
                for row in ordered_trades
                if bucket_start_ms < _int(row.get("ts")) <= bucket_end_ms
            ]
            oldest_timestamp = min(
                (_int(row.get("ts")) for row in ordered_trades),
                default=0,
            )
            coverage = "PARTIAL" if malformed_rows or duplicate_rows else (
                "COMPLETE"
                if len(rows) < trade_limit or oldest_timestamp <= bucket_start_ms
                else "PARTIAL"
            )
            buy_size = sum(
                _float(row.get("sz"))
                for row in interval_trades
                if str(row.get("side")) == "buy"
            )
            sell_size = sum(
                _float(row.get("sz"))
                for row in interval_trades
                if str(row.get("side")) == "sell"
            )
            sample.update(
                {
                    "taker_buy_volume": buy_size,
                    "taker_sell_volume": sell_size,
                    "cvd": buy_size - sell_size,
                    "trade_count": len(interval_trades),
                    "trades_coverage": coverage,
                }
            )
            latest_bucket_trade = interval_trades[-1] if interval_trades else None
            if latest_bucket_trade is not None:
                sample["source_timestamps"]["trades"] = _int(
                    latest_bucket_trade.get("ts")
                )
            observation_trades = [
                row
                for row in ordered_trades
                if _int(row.get("ts")) <= observed_at_ms
            ]
            latest_trade = observation_trades[-1] if observation_trades else None
            if latest_trade is not None:
                trade_timestamp = _int(latest_trade.get("ts"))
                latest_price = _float_or_none(latest_trade.get("px"))
                source_lag_limit_ms = max(10_000, int(interval_ms * 0.10))
                trade_checked_at_ms = int(time.time() * 1000)
                if (
                    latest_price is not None
                    and trade_timestamp <= trade_checked_at_ms + 5_000
                    and trade_checked_at_ms - trade_timestamp <= source_lag_limit_ms
                ):
                    # OI is sampled near the REST observation clock rather
                    # than at the prior candle close.  Pair it with this fresh
                    # observation price; Taker and Volume continue to use the
                    # exchange-aligned closed-candle price below.
                    sample["latest_trade_price"] = latest_price
                    sample["observation_price"] = latest_price
                    sample["source_timestamps"][
                        "observation_price"
                    ] = trade_timestamp
        except Exception as exc:
            sample["trades_coverage"] = "UNKNOWN"
            sample["failures"].append(f"taker_trades: {exc}")

        bar = "1m" if normalized_horizon == "SHORT" else "5m"
        try:
            candles = self.get_candles(
                inst_id,
                bar,
                limit=30,
                **options,
            )
            if not candles:
                raise OKXAPIError(f"empty {bar} candle response")
            eligible = [
                item
                for item in candles
                if item.ts + interval_ms <= observed_at_ms
            ]
            if not eligible:
                raise OKXAPIError(f"no completed {bar} candle at observation time")
            latest = eligible[-1]
            candle_close_ts = latest.ts + interval_ms
            if candle_close_ts != bucket_end_ms:
                raise OKXAPIError(
                    f"latest completed {bar} candle does not match trade bucket"
                )
            previous = eligible[:-1][-20:]
            baseline = (
                sum(item.quote_volume for item in previous) / len(previous)
                if len(previous) == 20
                else None
            )
            sample.update(
                {
                    "candle_ts": latest.ts,
                    "candle_close_ts": candle_close_ts,
                    "candle_bar": bar,
                    "candle_open": latest.open,
                    "candle_close": latest.close,
                    # Price response must share the same completed micro-candle
                    # clock as volume.  A live last trade is retained only as
                    # source context and never drives the average-window vote.
                    "price": latest.close,
                    "quote_volume": latest.quote_volume,
                    "volume_baseline": baseline,
                }
            )
            sample["source_timestamps"]["candle"] = latest.ts
        except Exception as exc:
            sample["failures"].append(f"{bar}_candles: {exc}")

        # Network/rate-limit waits are not part of the canonical market
        # bucket.  Keep their actual completion clock only as source context.
        sample["observed_at_ms"] = int(time.time() * 1000)
        return sample

    def get_market_context(
        self,
        inst_id: str,
        open_interest_usd: float | None = None,
        *,
        request_retries: int | None = None,
        request_timeout_seconds: float | None = None,
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
        taker_buy_volume: float | None = None
        taker_sell_volume: float | None = None
        cvd: float | None = None
        best_bid: float | None = None
        best_ask: float | None = None
        oi_timestamp = getattr(self, "_open_interest_timestamps", {}).get(inst_id, 0)
        source_timestamps: dict[str, int] = (
            {"open_interest": oi_timestamp} if oi_timestamp else {}
        )
        execution_notional = max(0.0, getattr(self, "execution_notional_usdt", 0.0))

        try:
            rows = self._get(
                "/api/v5/public/funding-rate",
                {"instId": inst_id},
                **_request_overrides(
                    request_retries,
                    request_timeout_seconds,
                ),
            )
            if not rows:
                raise OKXAPIError("empty funding-rate response")
            funding_rate = _float_or_none(rows[0].get("fundingRate"))
            sampled_at = max(sampled_at, _int(rows[0].get("ts")))
            source_timestamps["funding"] = _int(rows[0].get("ts"))
            if funding_rate is None:
                raise OKXAPIError("missing fundingRate")
        except Exception as exc:
            failures.append(f"funding_rate: {exc}")

        try:
            rows = self._get(
                "/api/v5/market/books",
                {"instId": inst_id, "sz": 20},
                **_request_overrides(
                    request_retries,
                    request_timeout_seconds,
                ),
            )
            if not rows:
                raise OKXAPIError("empty order-book response")
            bids = rows[0].get("bids", [])
            asks = rows[0].get("asks", [])
            best_bid = _float_or_none(bids[0][0]) if bids and isinstance(bids[0], list) else None
            best_ask = _float_or_none(asks[0][0]) if asks and isinstance(asks[0], list) else None
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
            source_timestamps["order_book"] = _int(rows[0].get("ts"))
        except Exception as exc:
            failures.append(f"order_book: {exc}")

        try:
            rows = self._get(
                "/api/v5/market/trades",
                {"instId": inst_id, "limit": 100},
                **_request_overrides(
                    request_retries,
                    request_timeout_seconds,
                ),
            )
            buy_size = sum(_float(row.get("sz")) for row in rows if row.get("side") == "buy")
            sell_size = sum(_float(row.get("sz")) for row in rows if row.get("side") == "sell")
            total_size = buy_size + sell_size
            if total_size <= 0:
                raise OKXAPIError("recent taker volume is zero")
            taker_buy_ratio = buy_size / total_size
            taker_buy_volume = buy_size
            taker_sell_volume = sell_size
            cvd = buy_size - sell_size
            sampled_at = max(sampled_at, max((_int(row.get("ts")) for row in rows), default=0))
            source_timestamps["trades"] = max((_int(row.get("ts")) for row in rows), default=0)
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
            taker_buy_volume=taker_buy_volume,
            taker_sell_volume=taker_sell_volume,
            cvd=cvd,
            best_bid=best_bid,
            best_ask=best_ask,
            source_timestamps=source_timestamps,
            data_quality={
                "available": sorted(source_timestamps),
                "failures": list(failures),
                "sampled_at": sampled_at,
            },
        )


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _usdt_contract_inst_id(value: Any) -> str:
    """Validate one OKX linear USDT perpetual or dated futures identifier."""

    inst_id = str(value or "").strip()
    parts = inst_id.split("-")
    is_perpetual = len(parts) == 3 and parts[2] == "SWAP"
    is_dated_future = len(parts) == 3 and len(parts[2]) == 6 and parts[2].isdigit()
    if not (
        len(parts) == 3
        and bool(parts[0])
        and parts[0].isalnum()
        and parts[1] == "USDT"
        and (is_perpetual or is_dated_future)
    ):
        raise ValueError(
            "open-interest history requires one USDT SWAP or FUTURES instId"
        )
    return inst_id


def _positive_int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _positive_finite_float_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 and math.isfinite(parsed) else None


def _instrument_from_row(row: dict[str, Any]) -> Instrument | None:
    inst_id = str(row.get("instId", ""))
    settle_ccy = str(row.get("settleCcy", ""))
    state = str(row.get("state", ""))
    ct_type = str(row.get("ctType", ""))
    inst_category = str(row.get("instCategory", "")).strip()
    if not (
        state == "live"
        and settle_ccy == "USDT"
        and inst_id.endswith("-USDT-SWAP")
        and ct_type in {"linear", ""}
        # OKX classifies crypto contracts as category 1 and stock
        # perpetuals as category 3.  Reject every explicitly non-crypto
        # category at the universe boundary so excluded products never reach
        # ticker, candle, OI or order-book requests.  Missing category data is
        # also rejected: fail closed instead of risking a stock scan.
        and inst_category == "1"
    ):
        return None
    return Instrument(
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
