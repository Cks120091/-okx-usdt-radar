"""Scan observations must stay separate from direction and entry permission."""
import copy
import math
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from radar.config import AppConfig
from radar.early_warning import preflight_early_warning, short_scan_observation
from radar.market_story import MarketStoryEngine
from radar.service import RadarRuntime
from radar.service_entry_policy import apply_service_entry_policy
import radar.service as service
from radar.short_direction import hourly_direction
from tests.legacy_service_cases import SingleInstrumentScanner, report
from tests.test_short_direction_contract import mirror
from tests.test_market_story import story_candles, valid_breakout_frames
from tests.test_signal_position_policy import preflight_signal, preflight


def signal_with_story(story):
    signal = preflight_signal(story.bias_direction)
    signal.market_metrics["raw_indicators"]["1H"]["fusion_long_score"] = story.timeframe_states["1H"]["score"]
    return replace(signal, market_story=story.story_dict(), signal_stage=story.stage)


class EarlyWarningTests(unittest.TestCase):
    def test_real_counter_direction_breakout_is_visible_but_not_an_entry(self):
        four, hourly, core = valid_breakout_frames()
        for side in ("LONG", "SHORT"):
            h, c = (hourly, mirror(core)) if side == "LONG" else (mirror(hourly), core)
            story = MarketStoryEngine().analyze_short(four, h, c)
            self.assertFalse(story.triggered)
            self.assertEqual(story.trigger_direction, side)
            signal = signal_with_story(story)
            before = copy.deepcopy(signal)
            result = preflight(signal, 100)
            self.assertEqual(result["early_warning"]["code"], "OPPOSITE_PRICE")
            self.assertIn("反向價格變化", result["early_warning"]["text"])
            self.assertIn("尚不能", result["early_warning"]["text"])
            self.assertFalse(result["verdict"]["new_entry_allowed"])
            self.assertEqual(signal, before)

    def test_real_pre_trigger_explains_setup_without_promising_only_a_close_is_missing(self):
        base = [100 + math.sin(i * .55) * .10 for i in range(98)]
        story = MarketStoryEngine().analyze_short(
            story_candles([90 + i * .09 for i in range(100)], 14_400_000),
            story_candles([95 + i * .05 for i in range(100)], 3_600_000),
            story_candles(base + [base[-1] + .06, base[-1] + .12]),
        )
        result = preflight(signal_with_story(story), 100)
        self.assertEqual(result["early_warning"]["code"], "SETUP_FORMING")
        self.assertIn("回踩續行", result["early_warning"]["text"])
        self.assertIn("同向價格條件", result["early_warning"]["text"])
        self.assertNotIn("等待收盤", result["early_warning"]["text"])
        self.assertFalse(result["verdict"]["new_entry_allowed"])

    def test_warning_neither_grants_nor_removes_entry_and_never_changes_plan(self):
        for side, score in (("LONG", 54), ("SHORT", 46)):
            for stage in ("CONFIRMED", "PRE_TRIGGER"):
                signal = replace(preflight_signal(side), signal_stage=stage)
                signal.market_metrics["raw_indicators"]["1H"]["fusion_long_score"] = score
                actual = preflight(signal, 100)
                with patch("radar.preflight_clarity.preflight_early_warning", return_value={}):
                    baseline = preflight(signal, 100)
                self.assertEqual(actual["verdict"], baseline["verdict"])
                self.assertEqual(actual["original"], baseline["original"])
                self.assertEqual(actual["live"]["quality_score"], baseline["live"]["quality_score"])
                self.assertEqual(actual["live"]["quality_components"], baseline["live"]["quality_components"])
                self.assertEqual(actual["direction"], baseline["direction"])
                self.assertEqual(actual["early_warning"]["code"], "DIRECTION_WEAKENING")
                self.assertIn("力道偏弱", actual["early_warning"]["text"])
                self.assertEqual(actual["verdict"]["new_entry_allowed"], stage == "CONFIRMED")

    def test_unknown_neutral_and_unclosed_data_do_not_claim_a_formed_setup(self):
        for score, code in ((None, "DATA_UNAVAILABLE"), (50, "DIRECTION_UNSET")):
            signal = preflight_signal()
            signal.market_metrics["raw_indicators"]["1H"]["fusion_long_score"] = score
            result = preflight(signal, 100)
            self.assertEqual(result["early_warning"]["code"], code)
            self.assertFalse(result["verdict"]["new_entry_allowed"])
        signal = replace(preflight_signal(), data_quality={"core": "AVAILABLE", "closed_candle": False})
        self.assertEqual(preflight(signal, 100)["early_warning"]["code"], "DATA_UNAVAILABLE")

    def test_finished_plans_and_long_radar_do_not_show_short_setup_warnings(self):
        signal = preflight_signal()
        signal.market_metrics["raw_indicators"]["1H"]["fusion_long_score"] = 54
        self.assertEqual(preflight(signal, 89)["early_warning"], {})
        self.assertEqual(preflight(replace(signal, radar_horizon="LONG"), 100)["early_warning"], {})

    def test_existing_signal_without_follow_through_gets_a_plain_explanation(self):
        result = preflight(replace(preflight_signal(), signal_stage="NO_FOLLOW_THROUGH"), 100)
        self.assertIn("觸發後未獲延續", result["early_warning"]["text"])
        self.assertFalse(result["verdict"]["new_entry_allowed"])

    def test_old_records_dont_fabricate_reverse_or_setup_facts(self):
        signal = preflight_signal()
        self.assertEqual(preflight(signal, 100)["early_warning"], {})
        four, hourly, core = valid_breakout_frames()
        story = MarketStoryEngine().analyze_short(four, hourly, mirror(core))
        signal.market_story = story.story_dict()
        # Hourly score does not belong to that scan: ignore its price observation.
        self.assertEqual(preflight(signal, 100)["early_warning"], {})

    def test_stale_noisy_reclaimed_and_unclosed_price_facts_do_not_raise_early_alert(self):
        base = {"direction": "SHORT", "triggered": True, "stage": "CONFIRMED", "event_age_bars": 2}
        for change in ({"stage": "TRENDING", "event_age_bars": 6}, {"noise": {"high": True}},
                       {"compression_block": True}, {"control_transfer": {"opponent_reclaimed": True}}):
            result = short_scan_observation(hourly_direction(65), {"SHORT": {**base, **change}}, closed=True, max_age_bars=2)
            self.assertEqual(result["code"], "NONE")
        result = short_scan_observation(hourly_direction(65), {"SHORT": base}, closed=False, max_age_bars=2)
        self.assertEqual(result["code"], "NONE")

    def test_fresh_price_observation_can_lead_formal_trigger(self):
        candidate = {"direction": "SHORT", "triggered": False, "stage": "PRE_TRIGGER", "event_age_bars": 1,
                     "control_transfer": {"push_away": True, "micro_defense_broken": True}}
        result = short_scan_observation(hourly_direction(65), {"SHORT": candidate}, closed=True, max_age_bars=2)
        self.assertEqual(result["code"], "OPPOSITE_PRICE")
        self.assertFalse(candidate["triggered"])

    def test_quote_refresh_preserves_scan_time_even_without_known_time(self):
        signal = preflight_signal()
        signal.market_metrics["raw_indicators"]["1H"]["fusion_long_score"] = 54
        result = preflight(signal, 100)
        warning = result["early_warning"]
        self.assertEqual(warning["source"], "STORED_SCAN")
        self.assertEqual(warning["scan_at"], result["original"]["report_generated_at"])
        self.assertNotEqual(warning["scan_at"], result["live"]["quote_sampled_at"])
        self.assertIsNone(preflight_early_warning(signal, {})["scan_at"])

    def test_full_single_scan_explains_new_facts_without_mutating_original(self):
        apply_service_entry_policy(service)
        class FreshScanner(SingleInstrumentScanner):
            def scan_instrument(self, inst_id, market_bias=None, **kwargs):
                result = super().scan_instrument(inst_id, market_bias, **kwargs)
                result.short_result.market_state.market_metrics["raw_indicators"]["1H"]["fusion_long_score"] = 54
                result.analyzed_at = "2026-09-27T10:00:00+00:00"
                return result
        with tempfile.TemporaryDirectory() as directory:
            runtime = RadarRuntime(FreshScanner(), AppConfig(data_dir=directory))
            runtime._latest = report()
            before = copy.deepcopy(runtime._latest.signals[0])
            result = runtime.scan_instrument_dict("AAA")["short"]["preflight"]
            self.assertEqual(result["early_warning"]["source"], "SINGLE_SCAN")
            self.assertEqual(result["early_warning"]["scan_at"], "2026-09-27T10:00:00+00:00")
            self.assertIn("多頭力道偏弱", result["early_warning"]["text"])
            self.assertEqual(runtime._latest.signals[0].market_metrics, before.market_metrics)
            self.assertEqual(result["original"]["stop_loss"], float(before.stop_loss))
