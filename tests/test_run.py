import unittest

from run import _extract_previous_open_interest


class PreviousReportTests(unittest.TestCase):
    def test_extracts_open_interest_from_published_market_map(self):
        payload = {
            "market_map": [
                {
                    "inst_id": "AAA-USDT-SWAP",
                    "market_metrics": {"open_interest_usd": 5_000_000},
                },
                {
                    "inst_id": "BBB-USDT-SWAP",
                    "market_metrics": {"open_interest_usd": None},
                },
            ],
            "watchlist": [
                {
                    "inst_id": "CCC-USDT-SWAP",
                    "market_metrics": {"open_interest_usd": 8_000_000},
                }
            ],
        }
        self.assertEqual(
            _extract_previous_open_interest(payload),
            {
                "AAA-USDT-SWAP": 5_000_000.0,
                "CCC-USDT-SWAP": 8_000_000.0,
            },
        )


if __name__ == "__main__":
    unittest.main()
