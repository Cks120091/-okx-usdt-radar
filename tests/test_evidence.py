import unittest
from dataclasses import replace

from radar.evidence import adx_quality, assess_evidence, infer_regime_direction
from radar.indicators import features
from radar.models import Instrument, MarketContext, Ticker
from radar.strategy import AdaptiveStrategyEngine, StrategyConfig
from tests.test_strategy import trend_candles


class EvidenceV2Tests(unittest.TestCase):
    def setUp(self):
        self.candles_4h = trend_candles(70, 0.4)
        self.candles_1h = trend_candles(100, 0.18, breakout=True)
        self.candles_15m = trend_candles(110, 0.09, accelerate=True)
        self.candles_5m = trend_candles(118, 0.04)
        self.instrument = Instrument(
            "TEST-USDT-SWAP",
            "live",
            "USDT",
            "linear",
            0.01,
        )
        close = self.candles_15m[-1].close
        self.ticker = Ticker(self.instrument.inst_id, close, close - 0.03, close + 0.03, 1)

    def test_adx_is_continuous_around_old_threshold(self):
        self.assertLess(abs(adx_quality(21.0) - adx_quality(20.9)), 1.0)
        self.assertGreater(adx_quality(30.0), adx_quality(20.0))

    def test_three_groups_are_scored_from_zero_to_one_hundred(self):
        tf4 = features(self.candles_4h)
        tf1 = features(self.candles_1h)
        tf15 = features(self.candles_15m)
        regime, direction = infer_regime_direction(tf4, tf1, tf15)
        assessment = assess_evidence(tf4, tf1, tf15, regime, direction)
        self.assertEqual(
            set(assessment.groups),
            {"position_structure", "trend_momentum", "participation_flow"},
        )
        self.assertTrue(all(0 <= item.score <= 100 for item in assessment.groups.values()))
        self.assertIn(assessment.stage, {"WATCH", "NEAR_TRIGGER", "EARLY_SIGNAL", "CONFIRMED"})

    def test_configured_severe_chase_threshold_is_used(self):
        tf4 = features(self.candles_4h)
        tf1 = features(self.candles_1h)
        tf15 = replace(features(self.candles_15m), extension_atr=1.60)
        regime, direction = infer_regime_direction(tf4, tf1, tf15)
        default = assess_evidence(tf4, tf1, tf15, regime, direction)
        stricter = assess_evidence(
            tf4,
            tf1,
            tf15,
            regime,
            direction,
            severe_entry_extension_atr=1.50,
        )
        self.assertNotEqual(default.entry_quality["key"], "SEVERE_CHASE")
        self.assertEqual(stricter.entry_quality["key"], "SEVERE_CHASE")

    def test_neutral_live_flow_is_not_a_conflict_or_veto(self):
        engine = AdaptiveStrategyEngine(StrategyConfig(min_quote_volume_24h=1_000_000))
        technical = engine.analyze(
            self.instrument,
            self.ticker,
            self.candles_4h,
            self.candles_1h,
            self.candles_15m,
        )
        result = engine.apply_market_context(
            technical,
            MarketContext(self.instrument.inst_id, 20_000_000, 0.0, 0.0, 0.50, 2),
            "LONG",
            self.candles_5m,
            {"score": 50.0, "label": "中性"},
        )
        self.assertIsNotNone(result.signal)
        self.assertEqual(result.reason, "qualified")
        self.assertNotIn("主動成交明顯反向", result.assessment.conflicts)

    def test_two_strong_live_flow_conflicts_block_signal(self):
        engine = AdaptiveStrategyEngine(StrategyConfig(min_quote_volume_24h=1_000_000))
        technical = engine.analyze(
            self.instrument,
            self.ticker,
            self.candles_4h,
            self.candles_1h,
            self.candles_15m,
        )
        result = engine.apply_market_context(
            technical,
            MarketContext(self.instrument.inst_id, 20_000_000, 0.0001, -0.30, 0.30, 2),
            "LONG",
            self.candles_5m,
            {"score": 72.0, "label": "偏多"},
        )
        self.assertIsNone(result.signal)
        self.assertEqual(result.reason, "major_evidence_conflict")
        self.assertGreaterEqual(result.assessment.conflict_severity, 55.0)


if __name__ == "__main__":
    unittest.main()
