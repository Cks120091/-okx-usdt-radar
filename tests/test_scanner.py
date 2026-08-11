import unittest

from radar.models import Candle, Instrument, MarketContext, MarketState, Signal, Ticker
from radar.scanner import MarketScanner, ScannerConfig
from radar.strategy import AnalysisResult


def candles(count=100, micro_anomaly=False):
    return [
        Candle(
            index,
            100 + index * 0.1 - (0.05 if micro_anomaly and index >= count - 12 else 0),
            101 + index * 0.1,
            99 + index * 0.1,
            100 + index * 0.1,
            10,
            1_000_000 * (2 if micro_anomaly and index == count - 1 else 1),
            True,
        )
        for index in range(count)
    ]


class FakeClient:
    def __init__(self, fail_id=None):
        self.fail_id = fail_id
        self.instruments = [
            Instrument("AAA-USDT-SWAP", "live", "USDT", "linear", 0.01),
            Instrument("BBB-USDT-SWAP", "live", "USDT", "linear", 0.01),
        ]

    def get_usdt_swap_instruments(self):
        return self.instruments

    def get_swap_tickers(self):
        return {
            item.inst_id: Ticker(item.inst_id, 110, 109.99, 110.01, 1)
            for item in self.instruments
        }

    def get_candles(self, inst_id, bar, limit=100):
        if inst_id == self.fail_id and bar == "1H":
            raise RuntimeError("fixture failure")
        return candles(limit)


class ManyFakeClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.instruments = [
            Instrument(f"T{index:02d}-USDT-SWAP", "live", "USDT", "linear", 0.01)
            for index in range(12)
        ]


class ContextFakeClient(FakeClient):
    def get_open_interest_usd(self):
        return {item.inst_id: 5_000_000 for item in self.instruments}

    def get_market_context(self, inst_id, open_interest_usd=None):
        return MarketContext(inst_id, open_interest_usd, 0.0001, 0.12, 0.56, 1)

    def get_candles(self, inst_id, bar, limit=100):
        return candles(limit, micro_anomaly=bar == "5m")


class LowOpenInterestClient(ContextFakeClient):
    def get_open_interest_usd(self):
        return {item.inst_id: 500_000 for item in self.instruments}


class AlwaysSignalEngine:
    def analyze(self, instrument, ticker, candles_4h, candles_1h, candles_15m):
        score = float(instrument.inst_id[1:3])
        return AnalysisResult(
            Signal(
                inst_id=instrument.inst_id,
                direction="LONG",
                strategy="fixture",
                score=score,
                evidence=["a", "b"],
                entry_low="1",
                entry_high="1",
                stop_loss="0.9",
                take_profit_1="1.2",
                take_profit_2="1.3",
                risk_reward=2.0,
                invalidation="fixture",
                spread_pct=0.01,
                quote_volume_24h=1_000_000,
                closed_candle_ts=1,
                regime="fixture",
            ),
            "qualified",
        )


class ScannerTests(unittest.TestCase):
    def test_any_request_failure_clears_all_signals(self):
        report = MarketScanner(FakeClient("BBB-USDT-SWAP"), ScannerConfig(workers=2)).scan_once()
        self.assertEqual(report.status, "DATA_INCOMPLETE")
        self.assertLess(report.coverage_pct, 100)
        self.assertEqual(report.signals, [])
        self.assertIn("BBB-USDT-SWAP", report.failed_instruments)

    def test_full_fetch_reports_one_hundred_percent_coverage(self):
        report = MarketScanner(
            FakeClient(),
            ScannerConfig(
                workers=2,
                min_open_interest_usd=0,
                require_micro_volume_anomaly=False,
            ),
        ).scan_once()
        self.assertEqual(report.coverage_pct, 100)
        self.assertEqual(report.target_count, 2)
        self.assertEqual(report.fetched_count, 2)
        self.assertNotEqual(report.status, "DATA_INCOMPLETE")
        self.assertEqual(len(report.market_map), 2)
        self.assertEqual(sum(report.market_regime_counts.values()), 2)
        self.assertTrue(report.watchlist)
        self.assertTrue(report.watchlist[0].missing_conditions)

    def test_output_has_hard_limit_of_ten_and_is_quality_sorted(self):
        scanner = MarketScanner(
            ManyFakeClient(),
            ScannerConfig(
                workers=3,
                max_signals=99,
                min_quote_volume_24h=0,
                max_spread_pct=1,
                min_open_interest_usd=0,
                require_micro_volume_anomaly=False,
            ),
        )
        scanner.engine = AlwaysSignalEngine()
        report = scanner.scan_once()
        self.assertEqual(len(report.signals), 10)
        self.assertEqual(report.signals[0].inst_id, "T11-USDT-SWAP")
        self.assertEqual(report.signals[-1].inst_id, "T02-USDT-SWAP")

    def test_top_candidates_receive_public_market_context(self):
        report = MarketScanner(ContextFakeClient(), ScannerConfig(workers=2)).scan_once()
        self.assertEqual(report.context_target_count, 2)
        self.assertEqual(report.context_enriched_count, 2)
        self.assertEqual(report.context_failures, {})
        self.assertTrue(report.watchlist[0].market_metrics["context_complete"])
        self.assertTrue(report.watchlist[0].market_metrics["micro_volume_anomaly"])
        self.assertIn(report.market_bias["label"], ("偏多", "中性", "偏空"))

    def test_low_open_interest_is_excluded_from_watchlist(self):
        report = MarketScanner(LowOpenInterestClient(), ScannerConfig(workers=2)).scan_once()
        self.assertEqual(report.context_target_count, 2)
        self.assertEqual(report.watchlist, [])
        self.assertTrue(
            all(item.status == "FILTERED" for item in report.market_map),
        )

    def test_market_bias_turns_bullish_when_breadth_and_anchors_align(self):
        scanner = MarketScanner(FakeClient())

        def bullish(inst_id):
            return AnalysisResult(
                None,
                "fixture",
                MarketState(
                    inst_id=inst_id,
                    regime="TREND",
                    direction="LONG",
                    preferred_strategy="趨勢回踩續行",
                    readiness_score=80.0,
                    status="WATCH",
                    missing_conditions=[],
                    spread_pct=0.01,
                    quote_volume_24h=20_000_000,
                    closed_candle_ts=1,
                ),
            )

        bias = scanner._calculate_market_bias(
            {
                "BTC-USDT-SWAP": bullish("BTC-USDT-SWAP"),
                "ETH-USDT-SWAP": bullish("ETH-USDT-SWAP"),
                "AAA-USDT-SWAP": bullish("AAA-USDT-SWAP"),
            }
        )
        self.assertEqual(bias["label"], "偏多")
        self.assertGreaterEqual(bias["score"], 65.0)
        self.assertEqual(bias["market_breadth_long_pct"], 100.0)


if __name__ == "__main__":
    unittest.main()
