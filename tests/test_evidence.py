import unittest
from dataclasses import replace

from radar.evidence import (
    EVIDENCE_OWNERSHIP,
    EvidenceGroup,
    _classify_stage,
    _structure_score,
    adx_quality,
    assess_evidence,
    infer_regime_direction,
)
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

    def test_raw_evidence_families_have_one_canonical_owner(self):
        families = [
            family
            for owned_families in EVIDENCE_OWNERSHIP.values()
            for family in owned_families
        ]
        self.assertEqual(len(families), len(set(families)))

    def test_15m_flow_only_changes_participation_group(self):
        tf4 = features(self.candles_4h)
        tf1 = features(self.candles_1h)
        tf15 = features(self.candles_15m)
        quiet = assess_evidence(
            tf4,
            tf1,
            replace(tf15, volume_ratio=0.60, directional_volume_ratio=0.50),
            "BREAKOUT_READY",
            "LONG",
        )
        active = assess_evidence(
            tf4,
            tf1,
            replace(tf15, volume_ratio=2.00, directional_volume_ratio=0.85),
            "BREAKOUT_READY",
            "LONG",
        )

        self.assertEqual(quiet.trigger_maturity, active.trigger_maturity)
        self.assertEqual(
            quiet.groups["trend_momentum"].score,
            active.groups["trend_momentum"].score,
        )
        self.assertGreater(
            active.groups["participation_flow"].score,
            quiet.groups["participation_flow"].score + 20.0,
        )

    def test_ema_state_does_not_change_position_structure_group(self):
        tf4 = features(self.candles_4h)
        tf1 = features(self.candles_1h)
        tf15 = features(self.candles_15m)
        bullish = assess_evidence(
            tf4,
            tf1,
            tf15,
            "BREAKOUT_READY",
            "LONG",
        )
        bearish_ema = assess_evidence(
            tf4,
            tf1,
            replace(
                tf15,
                ema21=tf15.close + tf15.atr14,
                ema55=tf15.close + tf15.atr14 * 2.0,
                ema21_slope_atr=-0.5,
                sma5=tf15.close - tf15.atr14 * 0.5,
                sma10=tf15.close + tf15.atr14 * 0.5,
                sma20=tf15.close + tf15.atr14,
            ),
            "BREAKOUT_READY",
            "LONG",
        )

        self.assertEqual(
            bullish.groups["position_structure"].score,
            bearish_ema.groups["position_structure"].score,
        )
        self.assertLess(
            bearish_ema.groups["trend_momentum"].score,
            bullish.groups["trend_momentum"].score,
        )

    def test_5m_volume_does_not_masquerade_as_micro_momentum(self):
        tf4 = features(self.candles_4h)
        tf1 = features(self.candles_1h)
        tf15 = features(self.candles_15m)
        tf5 = features(self.candles_5m)
        base = assess_evidence(
            tf4,
            tf1,
            tf15,
            "BREAKOUT_READY",
            "LONG",
        )
        context = MarketContext(self.instrument.inst_id, 20_000_000, 0.0, 0.0, 0.50, 2)
        quiet = base.with_live_context(
            context,
            replace(tf5, volume_ratio=0.60, directional_volume_ratio=0.50),
        )
        active = base.with_live_context(
            context,
            replace(tf5, volume_ratio=2.00, directional_volume_ratio=0.85),
        )

        self.assertEqual(quiet.micro_acceleration, active.micro_acceleration)
        self.assertEqual(
            quiet.groups["trend_momentum"].score,
            active.groups["trend_momentum"].score,
        )
        self.assertGreater(
            active.groups["participation_flow"].score,
            quiet.groups["participation_flow"].score,
        )

    def test_formal_signal_requires_two_independent_groups(self):
        def group(key: str, score: float) -> EvidenceGroup:
            return EvidenceGroup(key, score, "SUPPORT", 100.0)

        one_group = {
            "position_structure": group("position_structure", 82.0),
            "trend_momentum": group("trend_momentum", 58.0),
            "participation_flow": group("participation_flow", 58.0),
        }
        two_groups = {
            "position_structure": group("position_structure", 82.0),
            "trend_momentum": group("trend_momentum", 64.0),
            "participation_flow": group("participation_flow", 58.0),
        }

        self.assertEqual(
            _classify_stage(
                "TREND", one_group, 80.0, 80.0, 80.0, 0.0, "ACCEPTABLE"
            ),
            "NEAR_TRIGGER",
        )
        self.assertEqual(
            _classify_stage(
                "TREND", two_groups, 80.0, 80.0, 80.0, 0.0, "ACCEPTABLE"
            ),
            "EARLY_SIGNAL",
        )

    def test_strong_4h_opposition_blocks_ordinary_formal_signal(self):
        tf4 = features(trend_candles(120, -0.30))
        tf1 = features(trend_candles(100, 0.12))
        tf15 = features(trend_candles(110, 0.12, accelerate=True))
        assessment = assess_evidence(tf4, tf1, tf15, "TREND", "LONG")

        self.assertLess(assessment.bias_quality, 25.0)
        self.assertLess(assessment.groups["trend_momentum"].score, 35.0)
        self.assertNotIn(assessment.stage, {"EARLY_SIGNAL", "CONFIRMED"})
        self.assertIn("4H 大方向與候選方向強烈相反", assessment.conflicts)

    def test_neutral_4h_bias_is_not_treated_as_opposition(self):
        tf4 = features(trend_candles(120, 0.0))
        tf1 = features(trend_candles(100, 0.12))
        tf15 = features(trend_candles(110, 0.12, accelerate=True))
        assessment = assess_evidence(tf4, tf1, tf15, "TREND", "LONG")

        self.assertGreaterEqual(assessment.bias_quality, 25.0)
        self.assertGreaterEqual(assessment.groups["trend_momentum"].score, 60.0)
        self.assertNotIn("4H 大方向與候選方向強烈相反", assessment.conflicts)
        self.assertIn("4H 大方向中性／未支持", assessment.neutral)

    def test_missing_structure_support_is_neutral_not_conflict(self):
        tf = features(trend_candles(100, 0.10))
        atr = tf.atr14
        neutral = replace(
            tf,
            prior_high20=tf.close + atr * 5.0,
            prior_low20=tf.close - atr * 5.0,
            recent_high=tf.close + atr * 5.0,
            recent_low=tf.close - atr * 5.0,
        )
        opposed = replace(
            neutral,
            prior_low20=tf.close + atr * 0.30,
            recent_low=tf.close + atr * 0.30,
        )

        neutral_score = _structure_score(neutral, "LONG", "TREND")
        opposed_score = _structure_score(opposed, "LONG", "TREND")
        self.assertGreaterEqual(neutral_score, 40.0)
        self.assertLess(neutral_score, 60.0)
        self.assertLess(opposed_score, 35.0)

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

    def test_watch_summary_matches_stage_when_no_safe_entry_plan_exists(self):
        engine = AdaptiveStrategyEngine(StrategyConfig(min_quote_volume_24h=1_000_000))
        technical = engine.analyze(
            self.instrument,
            self.ticker,
            self.candles_4h,
            self.candles_1h,
            self.candles_15m,
        )
        technical = replace(technical, candidate_plan=None, candidate_signal=None)
        result = engine.apply_market_context(
            technical,
            MarketContext(self.instrument.inst_id, 20_000_000, 0.0, 0.0, 0.50, 2),
            "LONG",
            self.candles_5m,
            {"score": 50.0, "label": "中性"},
        )
        self.assertIsNone(result.signal)
        self.assertEqual(result.market_state.status, "WATCH")
        self.assertIn("目前列為觀望", result.market_state.summary)
        self.assertNotIn("目前列為早期", result.market_state.summary)


if __name__ == "__main__":
    unittest.main()
