"""Visible preparation is not a second source of trading permission."""
import copy
import math
import unittest
from dataclasses import replace
from unittest.mock import patch

from radar.early_warning import short_scan_preparation
from radar.indicators import features
from radar.market_story import MarketStoryEngine
from radar.models import MarketState
from radar.public_payload import public_candidate_payload
from radar.scanner import MarketScanner, ScannerConfig
from radar.short_direction import hourly_direction
from tests.legacy_scanner_cases import FakeClient
from tests.test_market_story import story_candles, valid_breakout_frames
from tests.test_short_direction_contract import mirror


class ShortPreparationTests(unittest.TestCase):
    def test_real_opposite_price_setup_remains_preparation_in_both_directions(self):
        four, hourly, core = valid_breakout_frames()
        for side in ("LONG", "SHORT"):
            h, c = (hourly, mirror(core)) if side == "LONG" else (mirror(hourly), core)
            result = MarketStoryEngine().analyze_short(four, h, c)
            observation = result.raw["preparation"]
            self.assertFalse(result.triggered)
            self.assertEqual(result.trigger_direction, side)
            self.assertEqual(observation["code"], "DIRECTION_PENDING")
            self.assertNotEqual(observation["candidate_direction"], side)
            self.assertIn("尚不可進場", observation["reason"])
            with patch("radar.market_story.short_scan_preparation", return_value={}):
                baseline = MarketStoryEngine().analyze_short(four, h, c)
            self.assertEqual(result.trigger, baseline.trigger)
            self.assertEqual(result.timeframe_states, baseline.timeframe_states)
            self.assertEqual(result.invalidation_price, baseline.invalidation_price)

    def test_neutral_hourly_with_real_setup_is_visible_in_public_watchlist(self):
        four, hourly, core = valid_breakout_frames()
        def measured(rows):
            tf = features(rows)
            return replace(tf, fusion_long_score=50) if rows is hourly else tf
        with patch("radar.market_story.features", side_effect=measured):
            story = MarketStoryEngine().analyze_short(four, hourly, core)
        state = MarketState(
            inst_id="AAA-USDT-SWAP", regime=story.regime, direction=story.direction,
            preferred_strategy="fixture", readiness_score=story.readiness, status=story.stage,
            missing_conditions=story.neutral, spread_pct=.01, quote_volume_24h=20_000_000,
            closed_candle_ts=core[-1].ts, market_story=story.story_dict(),
            market_metrics={"raw_indicators": {"1H": {"fusion_long_score": 50}}},
            data_quality=story.data_quality,
        )
        scanner = MarketScanner(FakeClient(), ScannerConfig())
        before = copy.deepcopy(state)
        selected = scanner._watchlist([scanner._attach_decision_context(state)])
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].direction, "NEUTRAL")
        self.assertFalse(selected[0].actionable)
        payload = public_candidate_payload(selected[0], signal=False)
        self.assertEqual(payload["market_story"]["preparation"]["code"], "DIRECTION_PENDING")
        self.assertEqual(payload["market_story"]["preparation"]["candidate_direction"], "LONG")
        self.assertFalse(payload["decision_context"]["final"]["new_entry_allowed"])
        self.assertEqual(state, before)
        # Neutral with no supported setup must not flood the preparation list.
        self.assertEqual(scanner._watchlist([replace(state, market_story={})]), [])

    def test_same_direction_untriggered_setup_waits_for_price_confirmation(self):
        base = [100 + math.sin(i * .55) * .10 for i in range(98)]
        story = MarketStoryEngine().analyze_short(
            story_candles([90 + i * .09 for i in range(100)], 14_400_000),
            story_candles([95 + i * .05 for i in range(100)], 3_600_000),
            story_candles(base + [base[-1] + .06, base[-1] + .12]),
        )
        self.assertFalse(story.triggered)
        self.assertEqual(story.raw["preparation"]["code"], "TRIGGER_PENDING")
        self.assertIn("等待同向價格觸發", story.raw["preparation"]["reason"])

    def test_aligned_formal_trigger_is_not_downgraded_to_preparation(self):
        four, hourly, core = valid_breakout_frames()
        for h, c in ((hourly, core), (mirror(hourly), mirror(core))):
            story = MarketStoryEngine().analyze_short(four, h, c)
            self.assertTrue(story.triggered)
            self.assertEqual(story.raw["preparation"], {})

    def test_missing_unclosed_or_unsupported_observations_do_not_manufacture_preparation(self):
        candidate = {"direction": "LONG", "stage": "PRE_TRIGGER", "triggered": False}
        self.assertEqual(short_scan_preparation(hourly_direction(None), {"LONG": candidate}, closed=True), {})
        self.assertEqual(short_scan_preparation(hourly_direction(50), {"LONG": candidate}, closed=False), {})
        for change in ({"stage": "WATCH"}, {"noise": {"high": True}}, {"compression_block": True},
                       {"control_transfer": {"opponent_reclaimed": True}}):
            self.assertEqual(short_scan_preparation(hourly_direction(50), {"LONG": {**candidate, **change}}, closed=True), {})

    def test_long_horizon_does_not_use_short_preparation(self):
        four, hourly, core = valid_breakout_frames()
        with patch("radar.market_story.short_scan_preparation", side_effect=AssertionError("must stay short-only")):
            story = MarketStoryEngine().analyze_long(four, hourly, core)
        self.assertNotIn("preparation", story.raw)
