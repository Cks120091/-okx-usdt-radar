"""Offline 24-hour USDT notional filter tests; no exchange calls."""
import unittest
from unittest.mock import patch

from macd_radar.app import (
    MIN_24H_TURNOVER_USDT_EST,
    do_scan,
    eligible_usdt_swaps,
    estimated_24h_turnover_usdt,
    ranked_usdt_swaps,
)


class Test24hTurnoverGate(unittest.TestCase):
    def ticker(self, inst, qty, price):
        return {"instId": inst, "volCcy24h": str(qty), "last": str(price)}

    def test_threshold_in_usdt_not_base_units(self):
        self.assertEqual(MIN_24H_TURNOVER_USDT_EST, 1_500_000)
        just_enough = self.ticker("BTC-USDT-SWAP", 15, 100000)
        self.assertEqual(estimated_24h_turnover_usdt(just_enough), 1_500_000)
        self.assertEqual(len(eligible_usdt_swaps([just_enough])), 1)

    def test_under_limit_strictly_excluded(self):
        low = self.ticker("ETH-USDT-SWAP", 749.99, 2000)
        self.assertLess(estimated_24h_turnover_usdt(low), 1_500_000)
        self.assertEqual(eligible_usdt_swaps([low]), [])

    def test_exclude_spot_and_coin_margined(self):
        rows = [self.ticker("BTC-USDT", 30, 100000),
                self.ticker("BTC-USD-SWAP", 30, 100000),
                self.ticker("BTC-USDT-SWAP", 30, 100000)]
        self.assertEqual([x["instId"] for x in eligible_usdt_swaps(rows)],
                         ["BTC-USDT-SWAP"])

    def test_missing_invalid_or_nonfinite_turnover(self):
        rows = [
            self.ticker("ETH-USDT-SWAP", "NaN", 10000),
            self.ticker("BTC-USDT-SWAP", "inf", 100000),
            self.ticker("DOT-USDT-SWAP", -50, 1_000_000),
            {"instId": "SOL-USDT-SWAP", "last": "180"},
            {"instId": "XRP-USDT-SWAP", "last": "abc", "volCcy24h": "100"},
            self.ticker("ADA-USDT-SWAP", 1e308, 1e308),
        ]
        self.assertEqual(eligible_usdt_swaps(rows), [])

    def test_sort_after_filter_before_limit(self):
        rows = [
            self.ticker("A-USDT-SWAP", 1_600_000, 1),
            self.ticker("B-USDT-SWAP", 1_499_999, 1),
            self.ticker("C-USDT-SWAP", 2_000_000, 1),
        ]
        with patch("macd_radar.app.okx_get", return_value=rows), \
             patch("macd_radar.app.active_eligible_asset_usdt_swap_ids", return_value={"A-USDT-SWAP", "B-USDT-SWAP", "C-USDT-SWAP"}):
            top = ranked_usdt_swaps(1)
            self.assertEqual(len(top), 1)
            self.assertEqual(top[0]["instId"], "C-USDT-SWAP")
            self.assertEqual(top[0]["turnover_24h_usdt_est"], 2_000_000)

    def test_scan_never_opens_candles_for_below_threshold(self):
        tickers = [
            self.ticker("BTC-USDT-SWAP", 100, 20_000), # 2m USDT
            self.ticker("SOL-USDT-SWAP", 100, 180),    # 18k USDT
        ]
        def scan_stub(ticker, timeframe):
            return {
                "instId": ticker["instId"],
                "normalized_histogram": 0.3,
                "rank_priority": 0,
                "turnover_24h_usdt_est": ticker["turnover_24h_usdt_est"],
            }
        with patch("macd_radar.app.okx_get", return_value=tickers), \
             patch("macd_radar.app.active_eligible_asset_usdt_swap_ids", return_value={"BTC-USDT-SWAP", "SOL-USDT-SWAP"}), \
             patch("macd_radar.app.scan_one", side_effect=scan_stub) as scanner:
            result = do_scan("15m", 40)
        self.assertEqual(result["requested"], 1)
        self.assertEqual(result["count"], 1)
        self.assertEqual(scanner.call_count, 1)
        self.assertEqual(result["items"][0]["instId"], "BTC-USDT-SWAP")


if __name__ == "__main__":
    unittest.main()
