import math
import unittest

from radar.models import Candle, Instrument, MarketContext, Ticker
from radar.indicators import features
from radar.strategy import AdaptiveStrategyEngine, StrategyConfig, _format_price


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


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.instrument = Instrument("TEST-USDT-SWAP", "live", "USDT", "linear", 0.01)

    def test_clear_breakout_can_qualify(self):
        candles_4h = trend_candles(70, 0.4)
        candles_1h = trend_candles(100, 0.18, breakout=True)
        candles_15m = trend_candles(110, 0.09, accelerate=True)
        ticker = Ticker("TEST-USDT-SWAP", candles_15m[-1].close, candles_15m[-1].close - 0.03, candles_15m[-1].close + 0.03, 1)
        engine = AdaptiveStrategyEngine(StrategyConfig(min_quote_volume_24h=1_000_000))
        result = engine.analyze(self.instrument, ticker, candles_4h, candles_1h, candles_15m)
        self.assertIsNotNone(result.signal, result.reason)
        self.assertEqual(result.signal.direction, "LONG")
        self.assertGreaterEqual(result.signal.risk_reward, 1.8)
        self.assertGreaterEqual(len(result.signal.evidence), 2)
        self.assertIsNotNone(result.market_state)
        self.assertEqual(result.market_state.status, "EARLY_SIGNAL")
        self.assertLess(result.market_state.readiness_score, 100.0)
        self.assertEqual(
            set(result.signal.evidence_groups),
            {"position_structure", "trend_momentum", "participation_flow"},
        )
        self.assertIn(result.signal.trend_strength_label, ("偏弱", "中等", "強"))
        self.assertIn("tp1_action", result.signal.management_plan)
        metrics = result.market_state.market_metrics
        self.assertEqual(metrics["last_price"], ticker.last)
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

    def test_near_higher_timeframe_obstacle_blocks_late_breakout(self):
        candles_4h = trend_candles(80, 0.4)
        candles_1h = trend_candles(100, 0.18, breakout=True)
        candles_15m = trend_candles(110, 0.09, accelerate=True)
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
        self.assertIsNone(result.signal)
        self.assertEqual(result.reason, "no_trade_plan")

    def test_low_liquidity_is_rejected(self):
        data = trend_candles(100, 0.1, quote_volume=100)
        ticker = Ticker("TEST-USDT-SWAP", 110, 109.99, 110.01, 1)
        result = AdaptiveStrategyEngine().analyze(self.instrument, ticker, data, data, data)
        self.assertEqual(result.reason, "liquidity_too_low")
        self.assertIsNotNone(result.market_state)
        self.assertEqual(result.market_state.status, "FILTERED")
        self.assertTrue(result.market_state.missing_conditions)

    def test_live_market_context_can_confirm_or_downgrade_signal(self):
        candles_4h = trend_candles(70, 0.4)
        candles_1h = trend_candles(100, 0.18, breakout=True)
        candles_15m = trend_candles(110, 0.09, accelerate=True)
        candles_5m = trend_candles(118, 0.04, breakout=True)
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
        self.assertIsNone(opposed.signal)
        self.assertEqual(opposed.reason, "major_evidence_conflict")

    def test_quiet_micro_timeframe_does_not_veto_complete_setup(self):
        candles_4h = trend_candles(70, 0.4)
        candles_1h = trend_candles(100, 0.18, breakout=True)
        candles_15m = trend_candles(110, 0.09, accelerate=True)
        quiet_5m = trend_candles(118, 0.04)
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
        self.assertIn(
            evaluated.signal.timeframe_states["5m"]["label"],
            ("中性", "做多方加速"),
        )

    def test_execution_cost_can_reject_an_otherwise_valid_signal(self):
        candles_4h = trend_candles(70, 0.4)
        candles_1h = trend_candles(100, 0.18, breakout=True)
        candles_15m = trend_candles(110, 0.09, accelerate=True)
        candles_5m = trend_candles(118, 0.04, breakout=True)
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
        self.assertIsNone(filtered.signal)
        self.assertEqual(filtered.reason, "execution_cost_too_high")
        self.assertGreater(
            filtered.market_state.market_metrics["execution_cost_to_risk_pct"],
            12.0,
        )

    def test_low_open_interest_is_a_hard_filter(self):
        candles_4h = trend_candles(70, 0.4)
        candles_1h = trend_candles(100, 0.18, breakout=True)
        candles_15m = trend_candles(110, 0.09, accelerate=True)
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
        self.assertIsNone(filtered.signal)
        self.assertEqual(filtered.reason, "open_interest_too_low")
        self.assertEqual(filtered.market_state.status, "FILTERED")

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
