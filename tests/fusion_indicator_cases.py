import math
from pathlib import Path
import unittest

from radar.indicators import features
from radar.models import Candle


def trend_candles(count: int, step: float) -> list[Candle]:
    output: list[Candle] = []
    for index in range(count):
        close = 100.0 + (index * step)
        open_price = close - (step * 0.35)
        spread = max(abs(step) * 1.8, 0.8)
        output.append(
            Candle(
                index,
                open_price,
                max(open_price, close) + spread,
                min(open_price, close) - spread,
                close,
                10.0,
                1000.0,
                True,
            )
        )
    return output


class CoreIndicatorFusionTests(unittest.TestCase):
    def test_medium_tunnel_is_real_and_deep_tunnel_is_not_faked(self):
        values = features(trend_candles(200, 0.25))
        self.assertTrue(values.fusion_medium_tunnel_available)
        self.assertFalse(values.fusion_deep_tunnel_available)
        self.assertTrue(math.isfinite(values.ema144))
        self.assertTrue(math.isfinite(values.ema169))
        self.assertTrue(math.isnan(values.ema576))
        self.assertTrue(math.isnan(values.ema676))
        self.assertGreater(values.fusion_long_score, 55.0)

    def test_deep_tunnel_only_activates_with_enough_real_history(self):
        values = features(trend_candles(720, 0.08))
        self.assertTrue(values.fusion_medium_tunnel_available)
        self.assertTrue(values.fusion_deep_tunnel_available)
        self.assertTrue(all(math.isfinite(value) for value in (
            values.ema576,
            values.ema676,
            values.smoothed_rsi,
            values.rsi_dynamic_band,
        )))

    def test_fusion_is_directional_without_becoming_an_independent_vote(self):
        bullish = features(trend_candles(200, 0.25))
        bearish = features(trend_candles(200, -0.12))
        self.assertGreater(bullish.fusion_long_score, bearish.fusion_long_score)
        for values in (bullish, bearish):
            for score in (
                values.fusion_trend_score,
                values.fusion_retest_score,
                values.fusion_momentum_score,
                values.fusion_fast_score,
                values.fusion_long_score,
            ):
                self.assertGreaterEqual(score, 0.0)
                self.assertLessEqual(score, 100.0)

    def test_shadow_fusion_cannot_reduce_or_create_formal_triggers(self):
        root = Path(__file__).resolve().parents[1]
        # Live Trigger, entry permission and decision gates intentionally do not
        # consume the shadow score.  This static contract makes an accidental
        # future hard filter a deliberate reviewed change instead of drift.
        for relative in (
            "radar/market_story.py",
            "radar/strategy.py",
            "radar/decision.py",
        ):
            source = (root / relative).read_text(encoding="utf-8")
            self.assertNotIn("fusion_long_score", source, relative)
            self.assertNotIn("fusion_medium_tunnel_available", source, relative)
            self.assertNotIn("fusion_deep_tunnel_available", source, relative)


if __name__ == "__main__":
    unittest.main()
