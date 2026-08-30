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
            {"instId": "BTC-USDT-SWAP", "state": "live", "settleCcy": "USDT", "ctType": "linear", "tickSz": "0.1"},
            {"instId": "ETH-USDC-SWAP", "state": "live", "settleCcy": "USDC", "ctType": "linear", "tickSz": "0.01"},
            {"instId": "OLD-USDT-SWAP", "state": "suspend", "settleCcy": "USDT", "ctType": "linear", "tickSz": "0.001"},
            {"instId": "BTC-USD-SWAP", "state": "live", "settleCcy": "BTC", "ctType": "inverse", "tickSz": "0.1"},
        ]
        instruments = FixtureClient(rows).get_usdt_swap_instruments()
        self.assertEqual([item.inst_id for item in instruments], ["BTC-USDT-SWAP"])

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
