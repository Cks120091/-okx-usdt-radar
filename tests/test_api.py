import unittest

from radar.api import OKXPublicClient


class FixtureClient(OKXPublicClient):
    def __init__(self, response):
        self.response = response

    def _get(self, path, params):
        return self.response


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


if __name__ == "__main__":
    unittest.main()

