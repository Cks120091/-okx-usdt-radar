import math
import unittest
from dataclasses import replace
from unittest.mock import patch

from radar.market_story import (
    DynamicZone,
    MarketStoryEngine,
    enrich_story_context,
    _momentum_confirmation,
    _prior_plan_invalidation,
    _price_acceptance,
    _trigger_candidate,
)
from radar.models import Candle, Instrument, MarketContext, Ticker
from radar.strategy import AdaptiveStrategyEngine, StrategyConfig
from tests.test_strategy import story_candles, trend_candles, valid_breakout_frames


class MarketStoryV34Tests(unittest.TestCase):
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

    def test_prior_stop_not_crossed_keeps_formal_opposite_as_warning(self):
        candles_4h, candles_1h, candles_15m = valid_breakout_frames()
        baseline = self.engine.analyze_short(
            candles_4h,
            candles_1h,
            candles_15m,
        )
        self.assertTrue(baseline.triggered)
        self.assertEqual(baseline.trigger_direction, "LONG")

        story = self.engine.analyze_short(
            candles_4h,
            candles_1h,
            candles_15m,
            previous_story={
                "active_trigger_direction": "SHORT",
                "active_stage": "CONFIRMED",
                "invalidation_price": max(
                    candle.high for candle in candles_15m
                )
                + 1.0,
                "last_evaluated_core_ts": candles_15m[-5].ts,
                "invalidated": False,
                "trigger": {
                    "direction": "SHORT",
                    "triggered": True,
                    "type": "BREAKOUT",
                    "stage": "CONFIRMED",
                    "freshness": "ACTIVE",
                    "event_ts": candles_15m[-10].ts,
                    "event_age_bars": 9,
                    "trigger_event_key": "SHORT:SHORT:BREAKOUT:prior:ZONE",
                    "zone_key": "ZONE",
                    "supporting": ["原做空計畫仍有效"],
                    "conflicts": [],
                    "neutral": [],
                    "momentum_confirmation": {},
                    "price_acceptance": {},
                    "control_transfer": {},
                },
            },
        )

        self.assertFalse(story.triggered)
        self.assertEqual(story.trigger_direction, "SHORT")
        self.assertEqual(story.direction, "SHORT")
        self.assertEqual(story.stage, "CONFIRMED")
        self.assertEqual(story.freshness, "ACTIVE")
        self.assertTrue(story.trigger["opposite_warning_only"])
        self.assertTrue(story.trigger["active_episode_preserved"])
        self.assertEqual(
            story.trigger["opposite_candidate"]["direction"],
            "LONG",
        )
        self.assertEqual(story.timeframe_states["15m"]["direction"], "SHORT")
        self.assertEqual(
            story.trigger["trigger_event_key"],
            "SHORT:SHORT:BREAKOUT:prior:ZONE",
        )
        self.assertFalse(story.trigger["previous_plan_invalidated"])
        self.assertEqual(story.trigger["previous_invalidation_ts"], 0)
        self.assertTrue(any("原方向尚未被價格失效" in item for item in story.conflicts))

    def test_stop_crossed_on_opposite_event_bar_cannot_flip_immediately(self):
        candles_4h, candles_1h, candles_15m = valid_breakout_frames()
        baseline = self.engine.analyze_short(
            candles_4h,
            candles_1h,
            candles_15m,
        )
        event_index = int(baseline.trigger["event_index"])
        event_candle = candles_15m[event_index]
        previous_candle = candles_15m[event_index - 1]
        stop = (previous_candle.high + event_candle.high) / 2.0
        self.assertLess(previous_candle.high, stop)
        self.assertGreaterEqual(event_candle.high, stop)

        story = self.engine.analyze_short(
            candles_4h,
            candles_1h,
            candles_15m,
            previous_story={
                "active_trigger_direction": "SHORT",
                "invalidation_price": stop,
                "last_evaluated_core_ts": previous_candle.ts,
                "invalidated": False,
            },
        )

        self.assertFalse(story.triggered)
        self.assertEqual(story.stage, "NEAR_TRIGGER")
        self.assertTrue(story.trigger["previous_plan_invalidated"])
        self.assertEqual(
            story.trigger["previous_invalidation_ts"],
            event_candle.ts,
        )
        self.assertEqual(story.event_ts, event_candle.ts)
        self.assertTrue(
            any("等待失效後的新價格事件" in item for item in story.conflicts)
        )

    def test_new_event_and_later_full_confirmation_can_reverse_after_stop(self):
        candles_4h, candles_1h, candles_15m = valid_breakout_frames()
        baseline = self.engine.analyze_short(
            candles_4h,
            candles_1h,
            candles_15m,
        )
        event_index = int(baseline.trigger["event_index"])
        invalidation_candle = candles_15m[event_index - 1]
        prior_candle = candles_15m[event_index - 2]
        stop = (prior_candle.high + invalidation_candle.high) / 2.0
        self.assertLess(prior_candle.high, stop)
        self.assertGreaterEqual(invalidation_candle.high, stop)

        story = self.engine.analyze_short(
            candles_4h,
            candles_1h,
            candles_15m,
            previous_story={
                "active_trigger_direction": "SHORT",
                "invalidation_price": stop,
                "last_evaluated_core_ts": prior_candle.ts,
                "invalidated": False,
            },
        )

        self.assertTrue(story.triggered)
        self.assertEqual(story.trigger_direction, "LONG")
        self.assertEqual(story.stage, "CONFIRMED")
        self.assertTrue(story.trigger["previous_plan_invalidated"])
        self.assertEqual(
            story.trigger["previous_invalidation_ts"],
            invalidation_candle.ts,
        )
        self.assertGreater(story.event_ts, invalidation_candle.ts)
        self.assertEqual(story.trigger["confirmation_level"], "FULL")
        self.assertGreater(
            story.trigger["confirmation_ts"],
            invalidation_candle.ts,
        )

    def test_out_of_order_and_unclosed_candles_cannot_invalidate_prior_plan(self):
        last_evaluated_ts = 200
        candles = [
            Candle(400, 99.0, 99.5, 98.5, 99.0, 1, 1, True),
            # This candle crosses the stop but is older than the last evaluated
            # core candle and appears out of order in the response.
            Candle(100, 99.0, 101.0, 98.5, 100.5, 1, 1, True),
            # A newer live candle cannot terminate the closed-candle plan.
            Candle(300, 99.0, 101.0, 98.5, 100.5, 1, 1, False),
        ]

        crossed, crossed_ts = _prior_plan_invalidation(
            "SHORT",
            100.0,
            candles,
            last_evaluated_ts,
        )

        self.assertFalse(crossed)
        self.assertEqual(crossed_ts, 0)

    def test_same_continuation_event_is_active_update_not_fake_reentry(self):
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
                    "active_stage": "CONFIRMED",
                    "last_evaluated_core_ts": candles_15m[-1].ts,
                    "invalidated": False,
                    "trigger": dict(first.trigger),
                },
            )

        self.assertEqual(first.trigger_type, "CONTINUATION")
        self.assertEqual(first.stage, "EARLY_SIGNAL")
        self.assertEqual(first.freshness, "NEW")
        self.assertNotEqual(later.stage, "REENTRY")
        self.assertEqual(later.freshness, "ACTIVE")
        self.assertTrue(later.trigger["same_episode_update"])
        self.assertEqual(
            later.trigger["trigger_event_key"],
            first.trigger["trigger_event_key"],
        )

    def test_reentry_requires_distinct_event_and_both_new_timestamps(self):
        candles_4h, candles_1h, candles_15m = valid_breakout_frames()
        watermark = candles_15m[-2].ts

        def new_continuation_fixture(*args, **kwargs):
            candidate = dict(_trigger_candidate(*args, **kwargs))
            if args[0] == "LONG":
                candidate.update(
                    {
                        "triggered": True,
                        "type": "CONTINUATION",
                        "stage": "EARLY_SIGNAL",
                        "freshness": "NEW",
                        "event_ts": candles_15m[-1].ts,
                        "confirmation_ts": candles_15m[-1].ts,
                        "event_index": len(candles_15m) - 1,
                        "event_age_bars": 0,
                    }
                )
            else:
                candidate["triggered"] = False
            return candidate

        with patch(
            "radar.market_story._trigger_candidate",
            side_effect=new_continuation_fixture,
        ):
            story = self.engine.analyze_short(
                candles_4h,
                candles_1h,
                candles_15m,
                previous_story={
                    "active_trigger_direction": "LONG",
                    "active_stage": "CONFIRMED",
                    "last_evaluated_core_ts": watermark,
                    "invalidated": False,
                    "trigger": {
                        "direction": "LONG",
                        "event_ts": candles_15m[-4].ts,
                        "confirmation_ts": watermark,
                        "trigger_event_key": "prior-continuation-event",
                    },
                },
            )

        self.assertEqual(story.stage, "REENTRY")
        self.assertEqual(story.freshness, "REACTIVATED")
        self.assertNotEqual(
            story.trigger["trigger_event_key"],
            "prior-continuation-event",
        )
        self.assertGreater(story.event_ts, watermark)
        self.assertGreater(story.trigger["confirmation_ts"], watermark)

    def test_reentry_is_blocked_when_confirmation_did_not_advance(self):
        candles_4h, candles_1h, candles_15m = valid_breakout_frames()
        watermark = candles_15m[-2].ts

        def unconfirmed_continuation_fixture(*args, **kwargs):
            candidate = dict(_trigger_candidate(*args, **kwargs))
            if args[0] == "LONG":
                candidate.update(
                    {
                        "triggered": True,
                        "type": "CONTINUATION",
                        "stage": "EARLY_SIGNAL",
                        "freshness": "NEW",
                        "event_ts": candles_15m[-1].ts,
                        "confirmation_ts": watermark,
                        "event_index": len(candles_15m) - 1,
                        "event_age_bars": 0,
                    }
                )
            else:
                candidate["triggered"] = False
            return candidate

        with patch(
            "radar.market_story._trigger_candidate",
            side_effect=unconfirmed_continuation_fixture,
        ):
            story = self.engine.analyze_short(
                candles_4h,
                candles_1h,
                candles_15m,
                previous_story={
                    "active_trigger_direction": "LONG",
                    "active_stage": "CONFIRMED",
                    "last_evaluated_core_ts": watermark,
                    "invalidated": False,
                    "trigger": {
                        "direction": "LONG",
                        "event_ts": candles_15m[-4].ts,
                        "confirmation_ts": watermark,
                        "trigger_event_key": "prior-continuation-event",
                    },
                },
            )

        self.assertNotEqual(story.stage, "REENTRY")
        self.assertEqual(story.freshness, "ACTIVE")
        self.assertTrue(story.trigger["same_episode_update"])

    def test_reentry_is_blocked_when_event_key_did_not_change(self):
        candles_4h, candles_1h, candles_15m = valid_breakout_frames()
        watermark = candles_15m[-2].ts

        def repeated_key_fixture(*args, **kwargs):
            candidate = dict(_trigger_candidate(*args, **kwargs))
            if args[0] == "LONG":
                candidate.update(
                    {
                        "triggered": True,
                        "type": "CONTINUATION",
                        "stage": "EARLY_SIGNAL",
                        "freshness": "NEW",
                        "event_ts": candles_15m[-1].ts,
                        "confirmation_ts": candles_15m[-1].ts,
                        "event_index": len(candles_15m) - 1,
                        "event_age_bars": 0,
                        "trigger_event_key": "prior-continuation-event",
                    }
                )
            else:
                candidate["triggered"] = False
            return candidate

        with patch(
            "radar.market_story._trigger_candidate",
            side_effect=repeated_key_fixture,
        ):
            story = self.engine.analyze_short(
                candles_4h,
                candles_1h,
                candles_15m,
                previous_story={
                    "active_trigger_direction": "LONG",
                    "active_stage": "CONFIRMED",
                    "last_evaluated_core_ts": watermark,
                    "invalidated": False,
                    "trigger": {
                        "direction": "LONG",
                        "event_ts": candles_15m[-4].ts,
                        "confirmation_ts": watermark,
                        "trigger_event_key": "prior-continuation-event",
                    },
                },
            )

        self.assertNotEqual(story.stage, "REENTRY")
        self.assertEqual(story.freshness, "ACTIVE")
        self.assertEqual(
            story.trigger["trigger_event_key"],
            "prior-continuation-event",
        )
        self.assertGreater(story.event_ts, watermark)
        self.assertGreater(story.trigger["confirmation_ts"], watermark)

    def test_terminal_flag_without_invalidation_watermark_fails_closed(self):
        candles_4h, candles_1h, candles_15m = valid_breakout_frames()

        story = self.engine.analyze_short(
            candles_4h,
            candles_1h,
            candles_15m,
            previous_story={
                "invalidated": True,
                "last_evaluated_core_ts": candles_15m[-20].ts,
            },
        )

        self.assertFalse(story.triggered)
        self.assertEqual(story.stage, "NEAR_TRIGGER")
        self.assertEqual(story.trigger["previous_invalidation_ts"], 0)
        self.assertTrue(
            any("缺少可信失效時間" in item for item in story.conflicts)
        )

    def test_explicit_terminal_watermark_allows_only_later_full_event(self):
        candles_4h, candles_1h, candles_15m = valid_breakout_frames()
        baseline = self.engine.analyze_short(
            candles_4h,
            candles_1h,
            candles_15m,
        )
        watermark = candles_15m[int(baseline.trigger["event_index"]) - 1].ts

        story = self.engine.analyze_short(
            candles_4h,
            candles_1h,
            candles_15m,
            previous_story={
                "active_trigger_direction": "SHORT",
                "invalidated": True,
                "invalidation_ts": watermark,
                "trigger": {
                    "direction": "SHORT",
                    "trigger_event_key": "old-terminal-event",
                },
            },
        )

        self.assertTrue(story.triggered)
        self.assertGreater(story.event_ts, watermark)
        self.assertGreater(story.trigger["confirmation_ts"], watermark)
        self.assertEqual(story.trigger["previous_invalidation_ts"], watermark)

    def test_missing_price_return_and_market_bias_remain_unknown(self):
        candles_4h, candles_1h, candles_15m = valid_breakout_frames()
        story = self.engine.analyze_short(
            candles_4h,
            candles_1h,
            candles_15m,
        )
        story = replace(
            story,
            raw={**story.raw, "core_return_pct": None},
        )
        context = MarketContext(
            "TEST-USDT-SWAP",
            20_000_000,
            0.0001,
            0.15,
            0.66,
            1,
            open_interest_change_pct=1.2,
            cvd=500.0,
        )

        enriched = enrich_story_context(
            story,
            context,
            timing=None,
            market_bias=None,
        )

        self.assertIsNone(enriched.market_participation["core_return_pct"])
        self.assertIsNone(enriched.market_participation["market_bias_score"])
        self.assertEqual(
            enriched.market_participation["market_bias_state"],
            "UNKNOWN",
        )
        self.assertIn(
            "core_return",
            enriched.market_participation["missing_sources"],
        )
        self.assertIn(
            "market_bias",
            enriched.market_participation["missing_sources"],
        )
        self.assertFalse(
            any("價格推不動" in item for item in enriched.conflicts)
        )

    def test_macro_countertrend_does_not_pollute_participation_flow(self):
        candles_4h, candles_1h, candles_15m = valid_breakout_frames()
        story = self.engine.analyze_short(
            candles_4h,
            candles_1h,
            candles_15m,
        )
        self.assertEqual(story.trigger_direction, "LONG")
        context = MarketContext(
            "TEST-USDT-SWAP",
            20_000_000,
            0.0001,
            0.15,
            0.66,
            1,
            open_interest_change_pct=1.2,
            cvd=500.0,
        )

        enriched = enrich_story_context(
            story,
            context,
            timing=None,
            market_bias={"score": 10.0, "label": "全市場偏空"},
        )

        self.assertTrue(
            any("全市場背景" in item for item in enriched.conflicts)
        )
        self.assertFalse(
            any(
                "全市場背景" in item
                for item in enriched.market_participation["conflicts"]
            )
        )
        self.assertEqual(enriched.market_participation["state"], "SUPPORT")
        self.assertEqual(
            enriched.groups["participation_flow"]["stance"],
            "SUPPORT",
        )

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
