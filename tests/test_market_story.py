import math
import unittest
from unittest.mock import patch

from radar.market_story import (
    DynamicZone,
    MarketStoryEngine,
    _momentum_confirmation,
    _price_acceptance,
    _trigger_candidate,
)
from radar.models import Candle, Instrument, Ticker
from radar.strategy import AdaptiveStrategyEngine, StrategyConfig
from tests.test_strategy import story_candles, trend_candles, valid_breakout_frames


class MarketStoryV33Tests(unittest.TestCase):
    def setUp(self):
        self.engine = MarketStoryEngine()

    def test_breakout_chase_uses_entry_boundary_not_the_whole_approach(self):
        candles_4h, candles_1h, candles_15m = valid_breakout_frames()
        story = self.engine.analyze_short(candles_4h, candles_1h, candles_15m)

        self.assertTrue(story.triggered)
        self.assertEqual(story.trigger_type, "BREAKOUT")
        self.assertEqual(story.stage, "CONFIRMED")
        self.assertEqual(story.freshness, "ACTIVE")
        self.assertEqual(story.price_acceptance["state"], "ACCEPTED")
        self.assertTrue(story.control_transfer["transferred"])
        self.assertTrue(story.control_transfer["push_away"])
        self.assertTrue(story.trigger["momentum_confirmation"]["confirmed"])
        self.assertLessEqual(
            story.trigger["momentum_confirmation"]["window_bars"],
            6,
        )
        self.assertTrue(story.attack_waves["BULL"])
        self.assertTrue(story.attack_waves["BEAR"])
        self.assertTrue(any(value for value in story.zones.values()))
        self.assertTrue(story.data_quality["closed_candle"])
        self.assertLessEqual(story.trigger["entry_extension_atr"], 0.50)
        self.assertGreater(story.trigger["move_from_defense_atr"], 0.50)
        self.assertTrue(story.trigger["move_from_defense_warning"])
        self.assertEqual(
            story.trigger["entry_extension_atr"],
            story.trigger["structural_entry_extension_atr"],
        )
        self.assertGreater(story.event_age_bars, 0)

    def test_momentum_event_index_stays_at_onset_instead_of_latest_bar(self):
        values = [100.0] * 90 + [
            99.8,
            99.7,
            100.0,
            100.4,
            100.8,
            101.0,
            101.1,
            101.2,
            101.3,
            101.4,
        ]
        candles = story_candles(values)

        momentum = _momentum_confirmation(candles, "LONG", 6)

        self.assertTrue(momentum["confirmed"])
        self.assertEqual(momentum["event_index"], 94)
        self.assertLess(momentum["event_index"], len(candles) - 1)

    def test_breakout_that_really_left_the_entry_boundary_remains_extended(self):
        candles_4h, candles_1h, candles_15m = valid_breakout_frames()
        previous = candles_15m[-1]
        candles_15m[-1] = Candle(
            ts=previous.ts,
            open=previous.open,
            high=101.62,
            low=previous.low,
            close=101.50,
            volume=previous.volume,
            quote_volume=previous.quote_volume,
            confirmed=True,
        )

        story = self.engine.analyze_short(candles_4h, candles_1h, candles_15m)

        self.assertTrue(story.triggered)
        self.assertEqual(story.trigger_type, "BREAKOUT")
        self.assertEqual(story.stage, "EXTENDED")
        self.assertGreater(story.trigger["entry_extension_atr"], 0.50)

    def test_gentle_reactivation_can_still_be_an_early_signal(self):
        base = [100 + math.sin(index * 0.55) * 0.10 for index in range(98)]
        start = base[-1]
        story = self.engine.analyze_short(
            story_candles(
                [90 + index * 0.09 for index in range(100)],
                14_400_000,
            ),
            story_candles(
                [95 + index * 0.05 for index in range(100)],
                3_600_000,
            ),
            story_candles(base + [start + 0.06, start + 0.12]),
        )

        self.assertTrue(story.triggered)
        self.assertEqual(story.trigger_type, "CONTINUATION")
        self.assertEqual(story.stage, "EARLY_SIGNAL")
        self.assertLessEqual(story.trigger["entry_extension_atr"], 0.50)

    def test_role_reversal_retest_requires_a_later_closed_bar(self):
        zone = DynamicZone(
            side="RESISTANCE",
            tier="SECONDARY",
            lower=99.8,
            upper=100.2,
            center=100.0,
            tests=2,
            rejections=1,
            last_touch_bars=1,
            source="fixture",
        )
        values = [99.7, 99.9, 100.5, 100.4]
        candles = [
            Candle(
                ts=index,
                open=values[index - 1] if index else close,
                high=max(values[index - 1] if index else close, close) + 0.05,
                low=(100.15 if index == 3 else min(values[index - 1] if index else close, close) - 0.05),
                close=close,
                volume=1,
                quote_volume=1,
                confirmed=True,
            )
            for index, close in enumerate(values)
        ]

        accepted = _price_acceptance(candles[:3], zone, "LONG", 0.5)
        retested = _price_acceptance(candles, zone, "LONG", 0.5)
        self.assertEqual(accepted["state"], "ACCEPTED")
        self.assertEqual(retested["state"], "ROLE_REVERSAL_RETEST")

    def test_pressure_compression_is_warning_not_false_reversal(self):
        story = self.engine.analyze_short(
            trend_candles(70, 0.4),
            trend_candles(100, 0.18, breakout=True),
            trend_candles(110, 0.09, accelerate=True),
        )

        self.assertEqual(story.raw["compression"]["state"], "UPWARD")
        self.assertFalse(story.triggered)
        self.assertEqual(story.trigger_type, "NONE")
        self.assertEqual(story.stage, "NEAR_TRIGGER")

    def test_opposite_change_is_warning_until_previous_story_invalidates(self):
        candles_4h, candles_1h, candles_15m = valid_breakout_frames()
        story = self.engine.analyze_short(
            candles_4h,
            candles_1h,
            candles_15m,
            previous_story={
                "active_trigger_direction": "SHORT",
                "invalidated": False,
            },
        )

        self.assertFalse(story.triggered)
        self.assertEqual(story.stage, "NEAR_TRIGGER")
        self.assertEqual(story.freshness, "NONE")
        self.assertTrue(story.trigger["opposite_warning_only"])
        self.assertTrue(any("原方向尚未被價格失效" in item for item in story.conflicts))

    def test_first_continuation_is_early_and_only_active_event_becomes_reentry(self):
        candles_4h, candles_1h, candles_15m = valid_breakout_frames()

        def continuation_fixture(*args, **kwargs):
            candidate = dict(_trigger_candidate(*args, **kwargs))
            if args[0] == "LONG" and candidate.get("triggered"):
                candidate.update(
                    {
                        "type": "CONTINUATION",
                        "stage": "EARLY_SIGNAL",
                        "freshness": "NEW",
                        "event_age_bars": 0,
                    }
                )
            return candidate

        with patch(
            "radar.market_story._trigger_candidate",
            side_effect=continuation_fixture,
        ):
            first = self.engine.analyze_short(
                candles_4h,
                candles_1h,
                candles_15m,
            )
            later = self.engine.analyze_short(
                candles_4h,
                candles_1h,
                candles_15m,
                previous_story={
                    "active_trigger_direction": "LONG",
                    "invalidated": False,
                },
            )

        self.assertEqual(first.trigger_type, "CONTINUATION")
        self.assertEqual(first.stage, "EARLY_SIGNAL")
        self.assertEqual(first.freshness, "NEW")
        self.assertEqual(later.stage, "REENTRY")
        self.assertEqual(later.freshness, "REACTIVATED")

    def test_flat_noise_never_becomes_trigger_by_score_alone(self):
        flat = [100 + math.sin(index * 0.4) * 0.05 for index in range(100)]
        story = self.engine.analyze_short(
            story_candles(flat, 14_400_000),
            story_candles(flat, 3_600_000),
            story_candles(flat),
        )

        self.assertFalse(story.triggered)
        self.assertFalse(story.control_transfer["transferred"])
        self.assertEqual(story.trigger_type, "NONE")

    def test_long_radar_uses_1d_bias_4h_trigger_and_1h_timing(self):
        _, candles_1h, candles_4h_trigger = valid_breakout_frames()
        candles_1d = story_candles(
            [80 + index * 0.10 for index in range(100)],
            86_400_000,
        )
        instrument = Instrument(
            "TEST-USDT-SWAP",
            "live",
            "USDT",
            "linear",
            0.01,
        )
        close = candles_4h_trigger[-1].close
        ticker = Ticker(instrument.inst_id, close, close - 0.01, close + 0.01, 1)
        result = AdaptiveStrategyEngine(
            StrategyConfig(min_quote_volume_24h=1_000_000)
        ).analyze_long(
            instrument,
            ticker,
            candles_1d,
            candles_4h_trigger,
            candles_1h,
        )

        self.assertIsNotNone(result.signal, result.reason)
        self.assertEqual(result.signal.radar_horizon, "LONG")
        self.assertEqual(result.signal.trigger_type, "BREAKOUT")
        self.assertEqual(result.signal.timeframe_states["1D"]["role"], "大方向 Bias")
        self.assertTrue(result.signal.timeframe_states["4H"]["can_block_trigger"])
        self.assertFalse(result.signal.timeframe_states["1H"]["can_block_trigger"])


if __name__ == "__main__":
    unittest.main()
