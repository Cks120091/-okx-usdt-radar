"""Cryptocurrency-only perpetual universe: negative tests for stock/TradFi."""
import unittest
from unittest.mock import patch

from macd_radar.app import (
    active_crypto_usdt_swap_ids,
    crypto_usdt_swap_ids,
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


class TestCryptoOnlyUniverse(unittest.TestCase):
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
            inst("XAUUSDT-USDT-SWAP", "4"),
            inst("EURUSDT-USDT-SWAP", "5"),
            inst("US30Y-USDT-SWAP", "6"),
            inst("MYSTERY-USDT-SWAP", ""),
            inst("NO_CATEGORY-USDT-SWAP", None),
            inst("PAUSED-USDT-SWAP", "1", state="suspend"),
            inst("BTC-USD-SWAP", "1"),
            inst("BTC-USDT", "1", inst_type="SPOT"),
        ]

    def test_allow_only_active_crypto_usdt_perpetuals(self):
        ids = crypto_usdt_swap_ids(self.metadata)
        self.assertEqual(ids, {"BTC-USDT-SWAP", "ETH-USDT-SWAP"})
        self.assertNotIn("AAPL-USDT-SWAP", ids)
        self.assertNotIn("US100-USDT-SWAP", ids)

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
        self.assertIn("crypto only", result["asset_filter"])

    def test_missing_category_data_fails_closed(self):
        with patch("macd_radar.app.okx_get", return_value=self.metadata[2:3]), \
             patch("macd_radar.app._INSTRUMENT_CACHE", None):
            with self.assertRaisesRegex(RuntimeError, "已停止掃描"):
                active_crypto_usdt_swap_ids()

    def test_metadata_outage_fails_closed(self):
        with patch("macd_radar.app.okx_get", side_effect=TimeoutError("timeout")), \
             patch("macd_radar.app._INSTRUMENT_CACHE", None):
            with self.assertRaises(TimeoutError):
                active_crypto_usdt_swap_ids()

    def test_metadata_cache_skips_requery(self):
        with patch("macd_radar.app.okx_get", return_value=self.metadata) as api, \
             patch("macd_radar.app._INSTRUMENT_CACHE", None):
            first = active_crypto_usdt_swap_ids()
            second = active_crypto_usdt_swap_ids()
        self.assertEqual(first, second)
        api.assert_called_once_with("/api/v5/public/instruments", instType="SWAP")


if __name__ == "__main__":
    unittest.main()
