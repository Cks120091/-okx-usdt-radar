import unittest
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from radar.api import OKXAPIError, OKXPublicClient, SlidingWindowRateLimiter
from radar.models import Instrument


class FixtureClient(OKXPublicClient):
    def __init__(self, response):
        self.response = response

    def _get(self, path, params):
        return self.response


class RouteFixtureClient(OKXPublicClient):
    def __init__(self):
        pass

    def _get(self, path, params):
        if path.endswith("/ticker"):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "last": "100.5",
                    "bidPx": "100",
                    "askPx": "101",
                    "ts": "1004",
                }
            ]
        if path.endswith("instruments"):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "state": "live",
                    "settleCcy": "USDT",
                    "ctType": "linear",
                    "tickSz": "0.1",
                    "ctVal": "0.01",
                    "ctMult": "1",
                    "ctValCcy": "BTC",
                    "instCategory": "1",
                }
            ]
        if path.endswith("open-interest"):
            return [
                {"instId": "BTC-USDT-SWAP", "oiUsd": "25000000", "ts": "999"},
                {"instId": "BTC-USD-SWAP", "oiUsd": "100", "ts": "999"},
            ]
        if path.endswith("funding-rate"):
            return [{"fundingRate": "0.0001", "ts": "1000"}]
        if path.endswith("books"):
            return [{"bids": [["100", "12"], ["99", "8"]], "asks": [["101", "5"], ["102", "5"]], "ts": "1001"}]
        if path.endswith("trades"):
            return [
                {"side": "buy", "sz": "7", "ts": "1002"},
                {"side": "sell", "sz": "3", "ts": "1003"},
            ]
        raise AssertionError(path)


class APITests(unittest.TestCase):
    @staticmethod
    def _json_response(payload: bytes):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = payload
        return response

    def test_official_rest_hosts_use_current_primary_and_fallback(self):
        client = OKXPublicClient(retries=0)
        self.assertEqual(client.base_url, "https://openapi.okx.com")
        self.assertEqual(
            client.base_urls,
            ("https://openapi.okx.com", "https://www.okx.com"),
        )

        legacy = OKXPublicClient(base_url="https://www.okx.com", retries=0)
        self.assertEqual(
            legacy.base_urls,
            ("https://www.okx.com", "https://openapi.okx.com"),
        )

        custom = OKXPublicClient(base_url="http://127.0.0.1:8001", retries=0)
        self.assertEqual(custom.base_urls, ("http://127.0.0.1:8001",))

    def test_rate_limit_penalty_pauses_all_following_requests(self):
        clock = [10.0]

        def advance(seconds):
            clock[0] += seconds

        with patch("radar.api.time.monotonic", side_effect=lambda: clock[0]), patch(
            "radar.api.time.sleep",
            side_effect=advance,
        ):
            limiter = SlidingWindowRateLimiter(30, 2.0)
            limiter.penalize(2.05)
            limiter.acquire()

        self.assertGreaterEqual(clock[0], 12.05)

    def test_filters_only_live_linear_usdt_swaps(self):
        rows = [
            {"instId": "BTC-USDT-SWAP", "state": "live", "settleCcy": "USDT", "ctType": "linear", "tickSz": "0.1", "instCategory": "1"},
            {"instId": "UNH-USDT-SWAP", "state": "live", "settleCcy": "USDT", "ctType": "linear", "tickSz": "0.01", "instCategory": "3"},
            {"instId": "ETH-USDC-SWAP", "state": "live", "settleCcy": "USDC", "ctType": "linear", "tickSz": "0.01"},
            {"instId": "OLD-USDT-SWAP", "state": "suspend", "settleCcy": "USDT", "ctType": "linear", "tickSz": "0.001"},
            {"instId": "BTC-USD-SWAP", "state": "live", "settleCcy": "BTC", "ctType": "inverse", "tickSz": "0.1"},
        ]
        instruments = FixtureClient(rows).get_usdt_swap_instruments()
        self.assertEqual([item.inst_id for item in instruments], ["BTC-USDT-SWAP"])

    def test_rejects_every_explicit_non_crypto_contract_category(self):
        rows = [
            {"instId": "TSLA-USDT-SWAP", "state": "live", "settleCcy": "USDT", "ctType": "linear", "tickSz": "0.01", "instCategory": "3"},
            {"instId": "XAU-USDT-SWAP", "state": "live", "settleCcy": "USDT", "ctType": "linear", "tickSz": "0.01", "instCategory": "4"},
            {"instId": "UNKNOWN-USDT-SWAP", "state": "live", "settleCcy": "USDT", "ctType": "linear", "tickSz": "0.01"},
        ]

        self.assertEqual(FixtureClient(rows).get_usdt_swap_instruments(), [])

    def test_single_instrument_lookup_cannot_scan_stock_perpetual(self):
        rows = [
            {"instId": "TSLA-USDT-SWAP", "state": "live", "settleCcy": "USDT", "ctType": "linear", "tickSz": "0.01", "instCategory": "3"},
        ]

        self.assertIsNone(
            FixtureClient(rows).get_usdt_swap_instrument("TSLA-USDT-SWAP")
        )

    def test_candles_drop_unclosed_bar_and_sort(self):
        rows = [
            ["2000", "1", "2", "0.5", "1.5", "10", "10", "15", "1"],
            ["3000", "1.5", "2", "1", "1.8", "10", "10", "18", "0"],
            ["1000", "0.8", "1.2", "0.7", "1", "10", "10", "10", "1"],
        ]
        candles = FixtureClient(rows).get_candles("BTC-USDT-SWAP", "1H")
        self.assertEqual([item.ts for item in candles], [1000, 2000])

    def test_public_market_context_is_normalized(self):
        client = RouteFixtureClient()
        client.execution_notional_usdt = 1_000
        client._instrument_meta = {}
        oi = client.get_open_interest_usd()
        self.assertEqual(oi, {"BTC-USDT-SWAP": 25_000_000.0})
        context = client.get_market_context("BTC-USDT-SWAP", oi["BTC-USDT-SWAP"])
        self.assertTrue(context.complete)
        self.assertEqual(context.funding_rate, 0.0001)
        self.assertAlmostEqual(context.taker_buy_ratio, 0.7)
        self.assertGreater(context.order_book_imbalance, 0)
        self.assertTrue(context.execution_quality_complete)
        self.assertGreater(context.bid_depth_usd, 1_000)
        self.assertGreaterEqual(context.buy_slippage_pct, 0)
        self.assertEqual(context.source_timestamps["open_interest"], 999)

    def test_single_instrument_execution_context_uses_live_book(self):
        client = RouteFixtureClient()
        client.execution_notional_usdt = 5
        client._instrument_meta = {}

        ticker = client.get_ticker("BTC-USDT-SWAP")
        context = client.get_execution_context("BTC-USDT-SWAP")

        self.assertEqual(ticker.last, 100.5)
        self.assertEqual(context.best_bid, 100.0)
        self.assertEqual(context.best_ask, 101.0)
        self.assertTrue(context.execution_quality_complete)
        self.assertGreater(context.bid_depth_usd, 0)
        self.assertGreater(context.ask_depth_usd, 0)
        self.assertEqual(context.source_timestamps["order_book"], 1001)
        self.assertNotIn("funding", context.source_timestamps)

    def test_targeted_instrument_metadata_and_open_interest(self):
        client = RouteFixtureClient()
        instrument = client.get_usdt_swap_instrument("BTC-USDT-SWAP")
        open_interest = client.get_open_interest_for("BTC-USDT-SWAP")

        self.assertIsNotNone(instrument)
        self.assertEqual(instrument.inst_id, "BTC-USDT-SWAP")
        self.assertEqual(open_interest, 25_000_000.0)
        self.assertEqual(client._open_interest_timestamps["BTC-USDT-SWAP"], 999)

    def test_open_interest_history_normalizes_sorts_and_deduplicates_raw_oi(self):
        class HistoryClient(OKXPublicClient):
            def __init__(self):
                self.calls = []

            def _get(self, path, params, **kwargs):
                self.calls.append((path, params, kwargs))
                return [
                    {"ts": "3000", "oi": "30", "oiCcy": "3", "oiUsd": "300"},
                    {"ts": "1000", "oi": "10", "oiCcy": "1", "oiUsd": "100"},
                    # Conflicting raw OI at one timestamp must invalidate the
                    # endpoint instead of letting response order pick a trend.
                    {"ts": "3000", "oi": "31", "oiCcy": "3.1", "oiUsd": "nan"},
                    {"ts": "0", "oi": "20", "oiCcy": "2"},
                    {"ts": "4000", "oi": "0", "oiCcy": "4"},
                    {"ts": "5000", "oi": "nan", "oiCcy": "5"},
                    {"ts": "6000", "oi": "60", "oiCcy": "inf"},
                    ["7000", "70", "7", "700"],
                    ["8000", "80"],
                ]

        client = HistoryClient()
        history = client.get_open_interest_history(
            "BTC-USDT-SWAP",
            period="5m",
            limit=7,
            end_ms=9_000,
            request_retries=0,
            request_timeout_seconds=2.5,
        )

        self.assertEqual(
            client.calls,
            [
                (
                    "/api/v5/rubik/stat/contracts/open-interest-history",
                    {
                        "instId": "BTC-USDT-SWAP",
                        "period": "5m",
                        "limit": 7,
                        "end": 9_000,
                    },
                    {"request_retries": 0, "request_timeout_seconds": 2.5},
                )
            ],
        )
        self.assertEqual(
            history,
            [
                {"ts": 1000, "oi": 10.0, "oiCcy": 1.0, "oiUsd": 100.0},
                {"ts": 4000, "oiCcy": 4.0},
                {"ts": 5000, "oiCcy": 5.0},
                {"ts": 6000, "oi": 60.0},
                {"ts": 7000, "oi": 70.0, "oiCcy": 7.0, "oiUsd": 700.0},
                {"ts": 8000, "oi": 80.0},
            ],
        )

    def test_open_interest_history_validates_contract_limit_and_end(self):
        client = OKXPublicClient(retries=0)
        with patch.object(client, "_get", return_value=[]) as getter:
            client.get_open_interest_history("BTC-USDT-260925")
            self.assertEqual(
                getter.call_args.args,
                (
                    "/api/v5/rubik/stat/contracts/open-interest-history",
                    {"instId": "BTC-USDT-260925", "period": "5m", "limit": 20},
                ),
            )

            for inst_id in (
                "BTC-USDC-SWAP",
                "BTC-USDT-SWAP,ETH-USDT-SWAP",
                "BTC-USDT",
                "btc-usdt-swap",
            ):
                with self.subTest(inst_id=inst_id), self.assertRaises(ValueError):
                    client.get_open_interest_history(inst_id)

            for invalid_limit in (0, 101, True, 2.5):
                with self.subTest(limit=invalid_limit), self.assertRaises(ValueError):
                    client.get_open_interest_history(
                        "BTC-USDT-SWAP",
                        limit=invalid_limit,
                    )

            for invalid_end in (0, -1, True, "not-a-timestamp"):
                with self.subTest(end=invalid_end), self.assertRaises(ValueError):
                    client.get_open_interest_history(
                        "BTC-USDT-SWAP",
                        end_ms=invalid_end,
                    )

        self.assertEqual(getter.call_count, 1)

    def test_open_interest_history_uses_dedicated_rate_limiter(self):
        client = OKXPublicClient(retries=0)
        self.assertEqual(client.open_interest_history_rate_limiter.max_requests, 10)
        self.assertEqual(
            client.open_interest_history_rate_limiter.window_seconds,
            2.0,
        )
        history_limiter = MagicMock()
        client.open_interest_history_rate_limiter = history_limiter
        client.rate_limiter = MagicMock()
        client.candle_rate_limiter = MagicMock()
        payload = (
            b'{"code":"0","msg":"","data":'
            b'[{"ts":"1000","oi":"10","oiCcy":"1"}]}'
        )

        with patch("radar.api.urlopen", return_value=self._json_response(payload)):
            history = client.get_open_interest_history(
                "BTC-USDT-SWAP",
                request_retries=0,
            )

        self.assertEqual(history, [{"ts": 1000, "oi": 10.0, "oiCcy": 1.0}])
        history_limiter.acquire.assert_called_once_with()
        client.rate_limiter.acquire.assert_not_called()
        client.candle_rate_limiter.acquire.assert_not_called()

    def test_continuation_snapshot_keeps_raw_oi_and_fixed_trade_bucket(self):
        now_ms = 1_800_000_000_000

        class ObserverClient(OKXPublicClient):
            def __init__(self):
                pass

            def _get(self, path, params, **kwargs):
                if path.endswith("open-interest"):
                    return [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "oi": "12345",
                            "oiCcy": "123.45",
                            "oiUsd": "25000000",
                            "ts": str(now_ms - 500),
                        }
                    ]
                if path.endswith("trades"):
                    return [
                        {
                            "tradeId": "2",
                            "side": " BUY ",
                            "sz": "7",
                            "px": "103",
                            "ts": str(now_ms - 1_000),
                        },
                        {
                            "tradeId": "1",
                            "side": "sell",
                            "sz": "3",
                            "px": "100",
                            "ts": str(now_ms - 30_000),
                        },
                    ]
                if path.endswith("candles"):
                    return [
                        [
                            str(now_ms - index * 60_000),
                            "100",
                            "102",
                            "99",
                            "101",
                            "10",
                            "10",
                            str(100 + index),
                            "1",
                        ]
                        for index in range(22)
                    ]
                raise AssertionError(path)

        with patch("radar.api.time.time", return_value=now_ms / 1000):
            sample = ObserverClient().get_continuation_snapshot(
                "BTC-USDT-SWAP",
                "SHORT",
                since_ms=now_ms - 60_000,
            )

        self.assertEqual(sample["open_interest_contracts"], 12_345.0)
        self.assertEqual(sample["open_interest_ccy"], 123.45)
        self.assertEqual(sample["open_interest_usd"], 25_000_000.0)
        self.assertEqual(sample["trades_coverage"], "COMPLETE")
        self.assertEqual(sample["taker_buy_volume"], 7.0)
        self.assertEqual(sample["taker_sell_volume"], 3.0)
        self.assertEqual(sample["cvd"], 4.0)
        self.assertEqual(sample["latest_trade_price"], 103.0)
        # Continuation price must use the same confirmed candle clock as
        # volume, not whichever live trade happened to arrive last.
        self.assertEqual(sample["price"], 101.0)
        self.assertEqual(sample["candle_bar"], "1m")
        self.assertEqual(sample["bucket_start_ms"], now_ms - 60_000)
        self.assertEqual(sample["bucket_end_ms"], now_ms)
        self.assertEqual(sample["candle_ts"], now_ms - 60_000)
        self.assertEqual(sample["candle_close_ts"], now_ms)
        self.assertIsNotNone(sample["volume_baseline"])

    def test_truncated_continuation_trade_bucket_is_partial_not_neutral(self):
        now_ms = 1_800_000_000_000

        class TruncatedObserverClient(OKXPublicClient):
            def __init__(self):
                pass

            def _get(self, path, params, **kwargs):
                if path.endswith("open-interest"):
                    return [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "oi": "12345",
                            "oiCcy": "123.45",
                            "oiUsd": "25000000",
                            "ts": str(now_ms),
                        }
                    ]
                if path.endswith("trades"):
                    return [
                        {
                            "tradeId": str(index),
                            "side": "buy" if index % 2 else "sell",
                            "sz": "1",
                            "px": "101",
                            # All 500 rows are newer than the requested bucket
                            # start, so older in-window trades may be missing.
                            "ts": str(now_ms - 10_000 + index),
                        }
                        for index in range(500)
                    ]
                if path.endswith("candles"):
                    return [
                        [str(now_ms), "100", "101", "99", "100", "1", "1", "1", "1"]
                    ]
                raise AssertionError(path)

        with patch("radar.api.time.time", return_value=now_ms / 1000):
            sample = TruncatedObserverClient().get_continuation_snapshot(
                "BTC-USDT-SWAP",
                "SHORT",
                since_ms=now_ms - 60_000,
            )

        self.assertEqual(sample["trades_coverage"], "PARTIAL")

    def test_continuation_trade_bucket_ignores_second_fifty_nine_poll_phase(self):
        bucket_end_ms = 1_800_000_000_000
        now_ms = bucket_end_ms + 59_000

        class PhaseShiftObserverClient(OKXPublicClient):
            def __init__(self):
                pass

            def _get(self, path, params, **kwargs):
                if path.endswith("open-interest"):
                    return [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "oi": "12345",
                            # Fresh to the arbitrary poll clock, but too far
                            # from the canonical candle boundary.
                            "ts": str(now_ms - 500),
                        }
                    ]
                if path.endswith("trades"):
                    return [
                        {
                            "tradeId": "start",
                            "side": "buy",
                            "sz": "50",
                            "px": "99",
                            "ts": str(bucket_end_ms - 60_000),
                        },
                        {
                            "tradeId": "inside-sell",
                            "side": "sell",
                            "sz": "2",
                            "px": "100",
                            "ts": str(bucket_end_ms - 30_000),
                        },
                        {
                            "tradeId": "end",
                            "side": "buy",
                            "sz": "3",
                            "px": "101",
                            "ts": str(bucket_end_ms),
                        },
                        {
                            "tradeId": "after-end",
                            "side": "buy",
                            "sz": "100",
                            "px": "150",
                            "ts": str(bucket_end_ms + 55_000),
                        },
                    ]
                if path.endswith("candles"):
                    return [
                        [
                            str(bucket_end_ms - (index + 1) * 60_000),
                            "100",
                            "102",
                            "99",
                            "101",
                            "10",
                            "10",
                            str(100 + index),
                            "1",
                        ]
                        for index in range(22)
                    ]
                raise AssertionError(path)

        with patch("radar.api.time.time", return_value=now_ms / 1000):
            sample = PhaseShiftObserverClient().get_continuation_snapshot(
                "BTC-USDT-SWAP",
                "SHORT",
                since_ms=now_ms - 60_000,
            )

        self.assertEqual(sample["bucket_end_ms"], bucket_end_ms)
        self.assertEqual(sample["bucket_start_ms"], bucket_end_ms - 60_000)
        self.assertEqual(sample["taker_buy_volume"], 3.0)
        self.assertEqual(sample["taker_sell_volume"], 2.0)
        self.assertEqual(sample["latest_trade_price"], 150.0)
        self.assertNotIn("open_interest_contracts", sample)
        self.assertTrue(
            any("open_interest" in failure for failure in sample["failures"])
        )

    def test_malformed_continuation_trade_row_downgrades_coverage(self):
        now_ms = 1_800_000_000_000

        class MalformedObserverClient(OKXPublicClient):
            def __init__(self):
                pass

            def _get(self, path, params, **kwargs):
                if path.endswith("open-interest"):
                    return [{"instId": "BTC-USDT-SWAP", "oi": "100", "ts": str(now_ms)}]
                if path.endswith("trades"):
                    return [
                        {
                            "tradeId": "valid",
                            "side": "buy",
                            "sz": "2",
                            "px": "101",
                            "ts": str(now_ms - 1_000),
                        },
                        {
                            "tradeId": "broken",
                            "side": "unknown",
                            "sz": "not-a-number",
                            "px": "101",
                            "ts": str(now_ms - 2_000),
                        },
                    ]
                if path.endswith("candles"):
                    return [
                        [str(now_ms), "100", "101", "99", "100", "1", "1", "1", "1"]
                    ]
                raise AssertionError(path)

        with patch("radar.api.time.time", return_value=now_ms / 1000):
            sample = MalformedObserverClient().get_continuation_snapshot(
                "BTC-USDT-SWAP",
                "SHORT",
                since_ms=now_ms - 60_000,
            )

        self.assertEqual(sample["trades_coverage"], "PARTIAL")

    def test_recent_full_scan_metadata_skips_targeted_network_request(self):
        client = OKXPublicClient(retries=0)
        instrument = Instrument(
            "BTC-USDT-SWAP",
            "live",
            "USDT",
            "linear",
            0.1,
        )
        client._instrument_meta[instrument.inst_id] = instrument
        client._instrument_meta_expires_at[instrument.inst_id] = float("inf")

        with patch.object(client, "_get") as getter:
            loaded = client.get_usdt_swap_instrument(instrument.inst_id)

        self.assertEqual(loaded, instrument)
        getter.assert_not_called()

    def test_unknown_targeted_instrument_is_not_misreported_as_api_outage(self):
        class UnknownInstrumentClient(OKXPublicClient):
            def __init__(self):
                self._instrument_meta = {}

            def _get(self, path, params):
                raise OKXAPIError(
                    "OKX code=51001: Instrument ID does not exist.",
                    code="51001",
                )

        client = UnknownInstrumentClient()

        self.assertIsNone(
            client.get_usdt_swap_instrument("GRESS-USDT-SWAP")
        )

    def test_other_targeted_instrument_errors_remain_retryable_failures(self):
        class UnavailableClient(OKXPublicClient):
            def __init__(self):
                self._instrument_meta = {}

            def _get(self, path, params):
                raise OKXAPIError("GET failed after retries: timed out")

        client = UnavailableClient()

        with self.assertRaisesRegex(OKXAPIError, "timed out"):
            client.get_usdt_swap_instrument("BTC-USDT-SWAP")

    def test_mixed_unknown_instrument_and_transport_error_is_not_not_found(self):
        class MixedFailureClient(OKXPublicClient):
            def __init__(self):
                self._instrument_meta = {}

            def _get(self, path, params):
                raise OKXAPIError(
                    "openapi.okx.com: OKX code=51001; www.okx.com: timed out"
                )

        with self.assertRaisesRegex(OKXAPIError, "timed out"):
            MixedFailureClient().get_usdt_swap_instrument("BTC-USDT-SWAP")

    def test_similar_but_different_okx_code_is_not_not_found(self):
        class DifferentCodeClient(OKXPublicClient):
            def __init__(self):
                self._instrument_meta = {}

            def _get(self, path, params):
                raise OKXAPIError(
                    "OKX code=510010: different application error",
                    code="510010",
                )

        with self.assertRaisesRegex(OKXAPIError, "510010"):
            DifferentCodeClient().get_usdt_swap_instrument("BTC-USDT-SWAP")

    def test_get_aggregates_mixed_host_failures_without_semantic_51001(self):
        unknown = self._json_response(
            b'{"code":"51001","msg":"Instrument ID does not exist","data":[]}'
        )
        client = OKXPublicClient(retries=1)
        with patch("radar.api.urlopen", side_effect=[unknown, URLError("timed out")]), patch(
            "radar.api.time.sleep",
            return_value=None,
        ):
            with self.assertRaises(OKXAPIError) as raised:
                client.get_usdt_swap_instrument("BTC-USDT-SWAP")

        self.assertIsNone(raised.exception.code)
        self.assertIn("timed out", str(raised.exception))

    def test_get_only_marks_51001_when_every_host_attempt_agrees(self):
        first = self._json_response(
            b'{"code":"51001","msg":"Instrument ID does not exist","data":[]}'
        )
        second = self._json_response(
            b'{"code":"51001","msg":"Instrument ID does not exist","data":[]}'
        )
        client = OKXPublicClient(retries=1)
        with patch("radar.api.urlopen", side_effect=[first, second]), patch(
            "radar.api.time.sleep",
            return_value=None,
        ):
            self.assertIsNone(
                client.get_usdt_swap_instrument("GRESS-USDT-SWAP")
            )

    def test_explicit_single_request_budget_bounds_retry_and_timeout(self):
        client = OKXPublicClient(retries=3, timeout_seconds=12.0)
        with patch(
            "radar.api.urlopen",
            side_effect=URLError("timed out"),
        ) as mocked_open, patch("radar.api.time.sleep") as mocked_sleep:
            with self.assertRaises(OKXAPIError):
                client.get_ticker(
                    "BTC-USDT-SWAP",
                    request_retries=0,
                    request_timeout_seconds=3.0,
                )

        self.assertEqual(mocked_open.call_count, 1)
        self.assertEqual(mocked_open.call_args.kwargs["timeout"], 3.0)
        mocked_sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
