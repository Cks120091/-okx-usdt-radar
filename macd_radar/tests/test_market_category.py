"""Crypto plus explicitly named XAU gold perpetual; fail-closed stocks/other commodities."""
import unittest
from unittest.mock import patch

from macd_radar.app import (
    active_eligible_asset_usdt_swap_ids,
    eligible_asset_usdt_swap_ids,
    do_scan,
    ranked_usdt_swaps,
)


def inst(inst_id, category, state="live", inst_type="SWAP"):
    return {
        "instId": inst_id, "instType": inst_type,
        "instCategory": category, "state": state,
    }


def ticker(inst_id, vol=2_000_000):
    return {"instId": inst_id, "volCcy24h": str(vol), "last": "1"}


class TestCryptoGoldUniverse(unittest.TestCase):
    def setUp(self):
        self.metadata = [
            inst("BTC-USDT-SWAP", "1"),
            inst("ETH-USDT-SWAP", "1"),
            inst("AAPL-USDT-SWAP", "3"),
            inst("NVDA-USDT-SWAP", "3"),
            inst("TSLA-USDT-SWAP", "3"),
            inst("US100-USDT-SWAP", "3"),
            inst("US500-USDT-SWAP", "3"),
            inst("JP225-USDT-SWAP", "3"),
            inst("XAU-USDT-SWAP", "4"),
            inst("XAUUSDT-USDT-SWAP", "4"),  # invalid legacy-style identifier
            inst("XAG-USDT-SWAP", "4"),
            inst("CL-USDT-SWAP", "4"),
            inst("EURUSDT-USDT-SWAP", "5"),
            inst("US30Y-USDT-SWAP", "6"),
            inst("MYSTERY-USDT-SWAP", ""),
            inst("NO_CATEGORY-USDT-SWAP", None),
            inst("PAUSED-USDT-SWAP", "1", state="suspend"),
            inst("BTC-USD-SWAP", "1"),
            inst("BTC-USDT", "1", inst_type="SPOT"),
        ]

    def test_allow_crypto_and_only_live_xau_gold(self):
        ids = eligible_asset_usdt_swap_ids(self.metadata)
        self.assertEqual(ids, {"BTC-USDT-SWAP", "ETH-USDT-SWAP", "XAU-USDT-SWAP"})
        for rejected in ("AAPL-USDT-SWAP", "US100-USDT-SWAP",
                         "XAG-USDT-SWAP", "CL-USDT-SWAP",
                         "XAUUSDT-USDT-SWAP", "EURUSDT-USDT-SWAP"):
            self.assertNotIn(rejected, ids)

    def test_gold_must_be_category_four_and_live(self):
        mistaken = [
            inst("XAU-USDT-SWAP", "3"),
            inst("XAU-USDT-SWAP", "", "live"),
            inst("XAU-USDT-SWAP", "4", "suspend"),
            inst("XAU-USDT-SWAP", "4", "live", "SPOT"),
        ]
        self.assertEqual(eligible_asset_usdt_swap_ids(mistaken), set())

    def test_filter_first_before_top_n(self):
        rows = [ticker("AAPL-USDT-SWAP", 99_000_000),
                ticker("US500-USDT-SWAP", 89_000_000),
                ticker("ETH-USDT-SWAP", 3_000_000),
                ticker("BTC-USDT-SWAP", 5_000_000)]
        def exchange(endpoint, **kwargs):
            if endpoint == "/api/v5/public/instruments":
                return self.metadata
            if endpoint == "/api/v5/market/tickers":
                return rows
            self.fail("unexpected path " + endpoint)
        with patch("macd_radar.app.okx_get", side_effect=exchange), \
             patch("macd_radar.app._INSTRUMENT_CACHE", None):
            result = ranked_usdt_swaps(1)
        self.assertEqual([x["instId"] for x in result], ["BTC-USDT-SWAP"])

    def test_excluded_stock_never_reaches_technical_scan(self):
        rows = [ticker("AAPL-USDT-SWAP", 90_000_000),
                ticker("US100-USDT-SWAP", 40_000_000),
                ticker("BTC-USDT-SWAP", 4_000_000)]
        def exchange(endpoint, **kwargs):
            if endpoint == "/api/v5/public/instruments":
                return self.metadata
            if endpoint == "/api/v5/market/tickers":
                return rows
            self.fail("candles queried for non-crypto instrument: " + endpoint)
        def scan_stub(tick, timeframe):
            return {"instId": tick["instId"], "rank_priority": 1,
                    "normalized_histogram": .5}
        with patch("macd_radar.app.okx_get", side_effect=exchange), \
             patch("macd_radar.app._INSTRUMENT_CACHE", None), \
             patch("macd_radar.app.scan_one", side_effect=scan_stub) as scanner:
            result = do_scan("4H", 40)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["instId"], "BTC-USDT-SWAP")
        self.assertEqual(scanner.call_count, 1)
        self.assertIn("XAU-USDT-SWAP gold", result["asset_filter"])

    def test_gold_is_scanned_when_eligible_but_stocks_and_silver_are_not(self):
        rows = [ticker("AAPL-USDT-SWAP", 90_000_000),
                ticker("XAG-USDT-SWAP", 40_000_000),
                ticker("BTC-USDT-SWAP", 4_000_000),
                ticker("XAU-USDT-SWAP", 3_000_000)]
        def exchange(endpoint, **kwargs):
            if endpoint == "/api/v5/public/instruments":
                return self.metadata
            if endpoint == "/api/v5/market/tickers":
                return rows
            self.fail("unexpected API path " + endpoint)
        with patch("macd_radar.app.okx_get", side_effect=exchange), \
             patch("macd_radar.app._INSTRUMENT_CACHE", None):
            selected = ranked_usdt_swaps(20)
        self.assertEqual([r["instId"] for r in selected],
                         ["BTC-USDT-SWAP", "XAU-USDT-SWAP"])

    def test_gold_keeps_one_topn_slot_if_eligible(self):
        rows = [ticker("BTC-USDT-SWAP", 20_000_000),
                ticker("ETH-USDT-SWAP", 15_000_000),
                ticker("XAU-USDT-SWAP", 2_000_000)]
        def exchange(endpoint, **kwargs):
            if endpoint == "/api/v5/public/instruments":
                return self.metadata
            if endpoint == "/api/v5/market/tickers":
                return rows
            self.fail("unexpected API path " + endpoint)
        with patch("macd_radar.app.okx_get", side_effect=exchange), \
             patch("macd_radar.app._INSTRUMENT_CACHE", None):
            selected = ranked_usdt_swaps(2)
        self.assertEqual([r["instId"] for r in selected],
                         ["BTC-USDT-SWAP", "XAU-USDT-SWAP"])

    def test_gold_below_threshold_never_reserves_slot(self):
        rows = [ticker("BTC-USDT-SWAP", 20_000_000),
                ticker("ETH-USDT-SWAP", 15_000_000),
                ticker("XAU-USDT-SWAP", 1_400_000)]
        def exchange(endpoint, **kwargs):
            if endpoint == "/api/v5/public/instruments":
                return self.metadata
            if endpoint == "/api/v5/market/tickers":
                return rows
            self.fail("unexpected API path " + endpoint)
        with patch("macd_radar.app.okx_get", side_effect=exchange), \
             patch("macd_radar.app._INSTRUMENT_CACHE", None):
            selected = ranked_usdt_swaps(2)
        self.assertEqual([r["instId"] for r in selected],
                         ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])

    def test_missing_category_data_fails_closed(self):
        with patch("macd_radar.app.okx_get", return_value=self.metadata[2:3]), \
             patch("macd_radar.app._INSTRUMENT_CACHE", None):
            with self.assertRaisesRegex(RuntimeError, "已停止掃描"):
                active_eligible_asset_usdt_swap_ids()

    def test_metadata_outage_fails_closed(self):
        with patch("macd_radar.app.okx_get", side_effect=TimeoutError("timeout")), \
             patch("macd_radar.app._INSTRUMENT_CACHE", None):
            with self.assertRaises(TimeoutError):
                active_eligible_asset_usdt_swap_ids()

    def test_metadata_cache_skips_requery(self):
        with patch("macd_radar.app.okx_get", return_value=self.metadata) as api, \
             patch("macd_radar.app._INSTRUMENT_CACHE", None):
            first = active_eligible_asset_usdt_swap_ids()
            second = active_eligible_asset_usdt_swap_ids()
        self.assertEqual(first, second)
        api.assert_called_once_with("/api/v5/public/instruments", instType="SWAP")


if __name__ == "__main__":
    unittest.main()
