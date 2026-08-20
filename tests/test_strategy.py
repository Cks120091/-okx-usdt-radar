import math
import unittest

from radar.models import Candle, Instrument, MarketContext, Ticker
from radar.indicators import features
from radar.strategy import (
    AdaptiveStrategyEngine,
    StrategyConfig,
    _entry_eligibility,
    _format_price,
)


def trend_candles(start, step, count=100, quote_volume=120_000, breakout=False, accelerate=False):
    closes = [start + (step * index) for index in range(count)]
    if accelerate:
        for index in range(count - 6, count):
            closes[index] += ((index - (count - 7)) ** 2) * abs(step) * (1 if step > 0 else -1) * 0.25
    if breakout:
        closes[-1] += 1.10 if step > 0 else -1.10
    candles = []
    for index, close in enumerate(closes):
        padding = 1.0
        candles.append(
            Candle(
                ts=1_700_000_000_000 + index * 60_000,
                open=close - (step * 0.25),
                high=close + padding,
                low=close - padding,
                close=close,
                volume=1000,
                quote_volume=quote_volume * (3.0 if breakout and index == count - 1 else 1.0),
                confirmed=True,
            )
        )
    return candles


def early_breakout_candles():
    output = []
    for index in range(100):
        close = 110 + (0.02 * index) + (math.sin(index * 0.9) * 0.18)
        if index == 99:
            close += 0.50
        output.append(
            Candle(
                ts=1_700_000_000_000 + index * 60_000,
                open=close - 0.02,
                high=close + 0.50,
                low=close - 0.50,
                close=close,
                volume=1000,
                quote_volume=360_000 if index == 99 else 120_000,
                confirmed=True,
            )
        )
    return output


def story_candles(values, step_ms=900_000, quote_volume=200_000):
    """Closed candles with real price displacement and tight, useful ranges."""
    output = []
    for index, close in enumerate(values):
        previous = values[index - 1] if index else close
        output.append(
            Candle(
                ts=1_700_000_000_000 + index * step_ms,
                open=previous,
                high=max(previous, close) + 0.12,
                low=min(previous, close) - 0.12,
                close=close,
                volume=1000,
                quote_volume=quote_volume * (3.0 if index >= len(values) - 3 else 1.0),
                confirmed=True,
            )
        )
    return output


def valid_breakout_frames(opposed_context=False):
    base = [100 + math.sin(index * 0.55) * 0.35 for index in range(92)]
    candles_15m = story_candles(
        base
        + [99.95, 100.05, 100.12, 100.20, 100.28, 100.42, 100.72, 101.05]
    )
    if opposed_context:
        candles_1h = story_candles(
            [110 - index * 0.05 for index in range(100)],
            3_600_000,
        )
        candles_4h = story_candles(
            [120 - index * 0.09 for index in range(100)],
            14_400_000,
        )
    else:
        candles_1h = story_candles(
            [95 + index * 0.05 for index in range(100)],
            3_600_000,
        )
        candles_4h = story_candles(
            [90 + index * 0.09 for index in range(100)],
            14_400_000,
        )
    return candles_4h, candles_1h, candles_15m


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.instrument = Instrument("TEST-USDT-SWAP", "live", "USDT", "linear", 0.01)

    def test_entry_eligibility_separates_trigger_from_chase_state(self):
        base = {
            "direction": "LONG",
            "entry_low": 100.0,
            "entry_high": 101.0,
            "stop": 98.0,
            "target": 110.0,
            "atr": 2.0,
            "stage": "EARLY_SIGNAL",
            "minimum_rr": 1.8,
            "ready_max_chase_atr": 0.15,
            "missed_chase_atr": 0.50,
        }
        ready = _entry_eligibility(current_price=100.5, **base)
        waiting = _entry_eligibility(current_price=101.6, **base)
        missed = _entry_eligibility(current_price=102.2, **base)

        self.assertEqual(ready["status"], "ENTRY_READY")
        self.assertTrue(ready["actionable"])
        self.assertEqual(waiting["status"], "WAIT_RETEST")
        self.assertFalse(waiting["actionable"])
        self.assertEqual(missed["status"], "MISSED_ENTRY")
        self.assertFalse(missed["actionable"])

    def test_low_remaining_rr_and_inactive_stage_are_missed(self):
        low_rr = _entry_eligibility(
            direction="LONG",
            current_price=101.2,
            entry_low=100.0,
            entry_high=101.0,
            stop=98.0,
            target=106.0,
            atr=2.0,
            stage="EARLY_SIGNAL",
            minimum_rr=1.8,
            ready_max_chase_atr=0.15,
            missed_chase_atr=0.50,
        )
        inactive = _entry_eligibility(
            direction="LONG",
            current_price=100.5,
            entry_low=100.0,
            entry_high=101.0,
            stop=98.0,
            target=110.0,
            atr=2.0,
            stage="TRENDING",
            minimum_rr=1.8,
            ready_max_chase_atr=0.15,
            missed_chase_atr=0.50,
        )

        self.assertEqual(low_rr["status"], "MISSED_ENTRY")
        self.assertLess(low_rr["remaining_rr"], 1.8)
        self.assertEqual(inactive["status"], "MISSED_ENTRY")

    def test_far_breakout_is_kept_for_tracking_but_not_actionable(self):
        candles_4h, candles_1h, candles_15m = valid_breakout_frames()
        ticker = Ticker("TEST-USDT-SWAP", candles_15m[-1].close, candles_15m[-1].close - 0.03, candles_15m[-1].close + 0.03, 1)
        engine = AdaptiveStrategyEngine(StrategyConfig(min_quote_volume_24h=1_000_000))
        result = engine.analyze(self.instrument, ticker, candles_4h, candles_1h, candles_15m)
        self.assertIsNotNone(result.signal, result.reason)
        self.assertEqual(result.signal.direction, "LONG")
        self.assertGreaterEqual(result.signal.risk_reward, 1.8)
        self.assertGreaterEqual(len(result.signal.evidence), 2)
        self.assertIsNotNone(result.market_state)
        self.assertEqual(result.market_state.status, "EXTENDED")
        self.assertEqual(result.signal.trigger_type, "BREAKOUT")
        self.assertEqual(result.signal.freshness, "EXTENDED")
        self.assertEqual(result.signal.radar_horizon, "SHORT")
        self.assertLess(result.market_state.readiness_score, 100.0)
        self.assertEqual(
            set(result.signal.evidence_groups),
            {"position_structure", "trend_momentum", "participation_flow"},
        )
        self.assertIn(result.signal.trend_strength_label, ("偏弱", "中等", "強"))
        self.assertIn("tp1_action", result.signal.management_plan)
        self.assertEqual(result.signal.entry_eligibility["status"], "MISSED_ENTRY")
        self.assertFalse(result.signal.actionable)
        self.assertAlmostEqual(
            result.candidate_plan.entry,
            result.assessment.trigger["entry_reference_price"],
        )
        metrics = result.market_state.market_metrics
        self.assertEqual(metrics["last_price"], ticker.last)
        self.assertEqual(metrics["core_timestamp"], candles_15m[-1].ts)
        self.assertIsInstance(metrics["price_change_15m_pct"], float)
        self.assertIsInstance(metrics["price_change_1h_pct"], float)
        self.assertIsInstance(metrics["price_change_24h_pct"], float)

    def test_early_expansion_does_not_wait_for_one_hour_breakout(self):
        engine = AdaptiveStrategyEngine(StrategyConfig(max_entry_extension_atr=1.0))
        tf4 = features(trend_candles(80, 0.4))
        tf1 = features(trend_candles(100, 0.08))
        tf15 = features(early_breakout_candles())
        self.assertLessEqual(tf1.close, tf1.prior_high20)
        plan = engine._early_expansion_plan("LONG", tf4, tf1, tf15)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.signal_stage, "EARLY_SIGNAL")
        self.assertEqual(plan.strategy, "早期動能擴張")

    def test_higher_timeframe_opposition_is_conflict_not_trigger_veto(self):
        candles_4h, candles_1h, candles_15m = valid_breakout_frames(
            opposed_context=True
        )
        ticker = Ticker(
            "TEST-USDT-SWAP",
            candles_15m[-1].close,
            candles_15m[-1].close - 0.03,
            candles_15m[-1].close + 0.03,
            1,
        )
        result = AdaptiveStrategyEngine(
            StrategyConfig(min_quote_volume_24h=1_000_000)
        ).analyze(
            self.instrument,
            ticker,
            candles_4h,
            candles_1h,
            candles_15m,
        )
        self.assertIsNotNone(result.signal, result.reason)
        self.assertEqual(result.signal.direction, "LONG")
        self.assertEqual(result.assessment.direction, "SHORT")
        self.assertTrue(
            any("Conflict" in item or "反向" in item for item in result.signal.conflicts)
        )

    def test_low_liquidity_is_rejected(self):
        data = trend_candles(100, 0.1, quote_volume=100)
        ticker = Ticker("TEST-USDT-SWAP", 110, 109.99, 110.01, 1)
        result = AdaptiveStrategyEngine().analyze(self.instrument, ticker, data, data, data)
        self.assertEqual(result.reason, "liquidity_too_low")
        self.assertIsNotNone(result.market_state)
        self.assertEqual(result.market_state.status, "FILTERED")
        self.assertTrue(result.market_state.missing_conditions)

    def test_live_market_context_can_confirm_or_downgrade_signal(self):
        candles_4h, candles_1h, candles_15m = valid_breakout_frames()
        candles_5m = story_candles(
            [98 + index * 0.03 for index in range(100)],
            300_000,
        )
        ticker = Ticker("TEST-USDT-SWAP", candles_15m[-1].close, candles_15m[-1].close - 0.03, candles_15m[-1].close + 0.03, 1)
        engine = AdaptiveStrategyEngine(StrategyConfig(min_quote_volume_24h=1_000_000))
        technical = engine.analyze(self.instrument, ticker, candles_4h, candles_1h, candles_15m)
        confirmed = engine.apply_market_context(
            technical,
            MarketContext("TEST-USDT-SWAP", 20_000_000, 0.0001, 0.20, 0.62, 2),
            "LONG",
            candles_5m,
            {"score": 72.0, "label": "偏多"},
        )
        self.assertIsNotNone(confirmed.signal)
        self.assertIn("participation_flow", confirmed.signal.factor_scores)
        opposed = engine.apply_market_context(
            technical,
            MarketContext("TEST-USDT-SWAP", 20_000_000, 0.0001, -0.30, 0.30, 2),
            "LONG",
            candles_5m,
            {"score": 72.0, "label": "偏多"},
        )
        self.assertIsNotNone(opposed.signal)
        self.assertEqual(opposed.reason, "qualified")
        self.assertEqual(opposed.signal.market_participation["state"], "CONFLICT")
        self.assertTrue(opposed.signal.conflicts)

    def test_quiet_micro_timeframe_does_not_veto_complete_setup(self):
        candles_4h, candles_1h, candles_15m = valid_breakout_frames()
        quiet_5m = story_candles(
            [100 + math.sin(index * 0.5) * 0.03 for index in range(100)],
            300_000,
        )
        ticker = Ticker("TEST-USDT-SWAP", candles_15m[-1].close, candles_15m[-1].close - 0.03, candles_15m[-1].close + 0.03, 1)
        engine = AdaptiveStrategyEngine(StrategyConfig(min_quote_volume_24h=1_000_000))
        technical = engine.analyze(self.instrument, ticker, candles_4h, candles_1h, candles_15m)
        evaluated = engine.apply_market_context(
            technical,
            MarketContext("TEST-USDT-SWAP", 20_000_000, 0.0001, 0.20, 0.62, 2),
            "LONG",
            quiet_5m,
            {"score": 72.0, "label": "偏多"},
        )
        self.assertIsNotNone(evaluated.signal)
        self.assertEqual(evaluated.reason, "qualified")
        self.assertFalse(evaluated.signal.timeframe_states["5m"]["can_block_trigger"])
        self.assertIn("micro_acceleration_5m", evaluated.signal.market_metrics)

    def test_execution_cost_warns_but_does_not_cancel_price_trigger(self):
        candles_4h, candles_1h, candles_15m = valid_breakout_frames()
        candles_5m = story_candles(
            [98 + index * 0.03 for index in range(100)],
            300_000,
        )
        ticker = Ticker(
            "TEST-USDT-SWAP",
            candles_15m[-1].close,
            candles_15m[-1].close - 0.03,
            candles_15m[-1].close + 0.03,
            1,
        )
        engine = AdaptiveStrategyEngine(
            StrategyConfig(
                min_quote_volume_24h=1_000_000,
                max_execution_cost_to_risk_pct=12.0,
            )
        )
        technical = engine.analyze(
            self.instrument,
            ticker,
            candles_4h,
            candles_1h,
            candles_15m,
        )
        filtered = engine.apply_market_context(
            technical,
            MarketContext(
                "TEST-USDT-SWAP",
                20_000_000,
                0.0001,
                0.20,
                0.62,
                2,
                bid_depth_usd=50_000,
                ask_depth_usd=50_000,
                buy_slippage_pct=0.08,
                sell_slippage_pct=0.08,
                execution_notional_usdt=1_000,
            ),
            "LONG",
            candles_5m,
            {"score": 72.0, "label": "偏多"},
        )
        self.assertIsNotNone(filtered.signal)
        self.assertEqual(filtered.reason, "qualified")
        self.assertGreater(
            filtered.market_state.market_metrics["execution_cost_to_risk_pct"],
            12.0,
        )
        self.assertIn(
            filtered.signal.execution_quality["recommendation"],
            ("CAUTION", "AVOID_EXECUTION"),
        )
        execution_check = next(
            item
            for item in filtered.signal.safety_checks
            if item["key"] == "execution_cost"
        )
        self.assertFalse(execution_check["hard"])

    def test_low_open_interest_is_context_not_a_hard_filter(self):
        candles_4h, candles_1h, candles_15m = valid_breakout_frames()
        ticker = Ticker("TEST-USDT-SWAP", candles_15m[-1].close, candles_15m[-1].close - 0.03, candles_15m[-1].close + 0.03, 1)
        engine = AdaptiveStrategyEngine(
            StrategyConfig(
                min_quote_volume_24h=1_000_000,
                min_open_interest_usd=3_000_000,
            )
        )
        technical = engine.analyze(self.instrument, ticker, candles_4h, candles_1h, candles_15m)
        filtered = engine.apply_market_context(
            technical,
            MarketContext("TEST-USDT-SWAP", 500_000, 0.0001, 0.20, 0.62, 2),
            "LONG",
        )
        self.assertIsNotNone(filtered.signal)
        self.assertEqual(filtered.reason, "qualified")
        self.assertNotEqual(filtered.market_state.status, "FILTERED")
        self.assertEqual(
            filtered.signal.market_metrics["open_interest_usd"],
            500_000,
        )

    def test_unconfirmed_or_short_history_is_rejected(self):
        data = trend_candles(100, 0.1, count=59)
        ticker = Ticker("TEST-USDT-SWAP", 106, 105.99, 106.01, 1)
        result = AdaptiveStrategyEngine().analyze(self.instrument, ticker, data, data, data)
        self.assertEqual(result.reason, "insufficient_history")

    def test_price_is_rounded_to_instrument_tick_size(self):
        self.assertEqual(_format_price(123.456, 0.01), "123.46")
        self.assertEqual(_format_price(1.024, 0.05), "1.00")
        self.assertEqual(_format_price(1.026, 0.05), "1.05")


if __name__ == "__main__":
    unittest.main()
