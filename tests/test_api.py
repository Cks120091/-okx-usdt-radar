import unittest

from radar.api import OKXPublicClient


class FixtureClient(OKXPublicClient):
    def __init__(self, response):
        self.response = response

    def _get(self, path, params):
        return self.response


class RouteFixtureClient(OKXPublicClient):
    def __init__(self):
        pass

    def _get(self, path, params):
        if path.endswith("open-interest"):
            return [
                {"instId": "BTC-USDT-SWAP", "oiUsd": "25000000"},
                {"instId": "BTC-USD-SWAP", "oiUsd": "100"},
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


if __name__ == "__main__":
    unittest.main()
