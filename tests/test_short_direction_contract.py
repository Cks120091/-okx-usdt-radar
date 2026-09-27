"""The completed 1H direction must own every 15m entry decision."""
import copy
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from radar.config import AppConfig
from radar.decision import build_decision_context
from radar.indicators import features
from radar.market_story import MarketStoryEngine
from radar.public_payload import public_candidate_payload
from radar.scanner import MarketScanner, ScannerConfig
from radar.service import RadarRuntime, _canonical_single_decision
import radar.service as service
from radar.service_entry_policy import apply_service_entry_policy
from radar.short_direction import POLICY, hourly_direction
from tests.legacy_scanner_cases import FakeClient
from tests.legacy_service_cases import SingleInstrumentScanner, report
from tests.test_market_story import valid_breakout_frames
from tests.test_signal_position_policy import signal_dict, preflight_signal, preflight


def mirror(candles):
    return [replace(c, open=300-c.open, high=300-c.low, low=300-c.high, close=300-c.close) for c in candles]


class ShortDirectionContractTests(unittest.TestCase):
    def test_all_direction_boundaries_match_scan_preflight_and_single_decision(self):
        scanner = MarketScanner(FakeClient(), ScannerConfig(min_quote_volume_24h=0))
        for score, expected_direction in ((0, "SHORT"), (40, "SHORT"), (40.01, "SHORT"),
                                         (48, "SHORT"), (48.01, "NEUTRAL"), (50, "NEUTRAL"),
                                         (51.99, "NEUTRAL"), (52, "LONG"), (59.99, "LONG"),
                                         (60, "LONG"), (100, "LONG")):
            for direction in ("LONG", "SHORT"):
                with self.subTest(score=score, direction=direction):
                    signal = preflight_signal(direction)
                    signal.market_metrics["raw_indicators"]["1H"]["fusion_long_score"] = score
                    before = copy.deepcopy(signal)
                    scanned = scanner._attach_decision_context(signal)
                    refreshed = preflight(signal, 100)
                    canonical = _canonical_single_decision(signal, refreshed, None)
                    expected = direction == expected_direction
                    self.assertEqual(scanned.actionable, expected)
                    self.assertEqual(refreshed["verdict"]["new_entry_allowed"], expected)
                    self.assertEqual(canonical["final"]["new_entry_allowed"], expected)
                    self.assertEqual(refreshed["timeframe_alignment"]["timeframe_direction"], expected_direction)
                    self.assertEqual(signal, before)

    def test_missing_or_invalid_hourly_data_never_grants_entry(self):
        for raw in ({}, {"1H": {}}, *({"1H": {"fusion_long_score": x}} for x in
                   (None, "bad", True, False, float("nan"), float("inf"), -1, 101))):
            for direction in ("LONG", "SHORT"):
                with self.subTest(raw=raw, direction=direction):
                    signal = preflight_signal(direction)
                    signal.market_metrics["raw_indicators"] = raw
                    self.assertFalse(build_decision_context(signal)["final"]["new_entry_allowed"])
                    result = preflight(signal, 100)
                    self.assertFalse(result["verdict"]["new_entry_allowed"])
                    self.assertEqual(result["timeframe_alignment"]["state"], "UNKNOWN")

    def test_rounding_matches_saved_direction_precision(self):
        for score in (51.994, 51.996, 48.004, 48.006):
            self.assertEqual(hourly_direction(score), hourly_direction(round(score, 2)))

    def test_real_breakout_cannot_override_opposing_hourly_direction(self):
        four, hourly, core = valid_breakout_frames()
        engine = MarketStoryEngine()
        for side in ("LONG", "SHORT"):
            h, c = (hourly, core) if side == "LONG" else (mirror(hourly), mirror(core))
            aligned = engine.analyze_short(four, h, c)
            opposed = engine.analyze_short(four, mirror(h), c)
            self.assertTrue(aligned.triggered)
            self.assertEqual(aligned.trigger_direction, side)
            self.assertEqual(aligned.timeframe_states["1H"]["direction"], side)
            self.assertFalse(opposed.triggered)

    def test_neutral_hourly_frame_does_not_create_a_formal_price_trigger(self):
        four, hourly, core = valid_breakout_frames()
        def measured(rows):
            tf = features(rows)
            return replace(tf, fusion_long_score=50) if rows is hourly else tf
        with patch("radar.market_story.features", side_effect=measured):
            result = MarketStoryEngine().analyze_short(four, hourly, core)
        self.assertFalse(result.triggered)
        self.assertEqual(result.timeframe_states["1H"]["direction"], "NEUTRAL")

    def test_hourly_display_and_entry_use_same_score_even_if_other_metrics_differ(self):
        four, hourly, core = valid_breakout_frames()
        for score, side in ((54, "LONG"), (46, "SHORT")):
            def measured(rows):
                tf = features(rows)
                return replace(tf, fusion_long_score=score) if rows is hourly else tf
            with patch("radar.market_story.features", side_effect=measured):
                story = MarketStoryEngine().analyze_short(four, hourly, core)
            item = signal_dict(side)
            item["market_metrics"]["raw_indicators"]["1H"]["fusion_long_score"] = score
            alignment = build_decision_context(item)["final"]["timeframe_alignment"]
            self.assertEqual(story.timeframe_states["1H"]["direction"], alignment["timeframe_direction"])
            self.assertEqual(story.timeframe_states["1H"]["score"], alignment["long_score"])
            self.assertTrue(alignment["passed"])

    def test_public_card_retains_direction_policy_and_reason(self):
        item = preflight_signal("SHORT")
        item.market_metrics["raw_indicators"]["1H"]["fusion_long_score"] = 54
        item.decision_context = build_decision_context(item)
        public = public_candidate_payload(item, signal=True)
        alignment = public["decision_context"]["final"]["timeframe_alignment"]
        self.assertFalse(alignment["passed"])
        self.assertEqual(alignment["policy"], POLICY)
        self.assertEqual(alignment["bias_state"], "LONG_WEAKENING")
        self.assertIn("只允許做多", alignment["reason"])

    def test_single_scan_uses_new_hourly_evidence_not_stored_permission(self):
        apply_service_entry_policy(service)
        for raw, expected in (({"1H": {"fusion_long_score": 54}}, True),
                              ({"1H": {"fusion_long_score": 46}}, False),
                              ({"1H": {"fusion_long_score": 50}}, False), ({}, False)):
            class FreshHourlyScanner(SingleInstrumentScanner):
                def scan_instrument(self, inst_id, market_bias=None, **kwargs):
                    result = super().scan_instrument(inst_id, market_bias, **kwargs)
                    result.short_result.market_state.market_metrics["raw_indicators"] = raw
                    side = hourly_direction(raw.get("1H", {}).get("fusion_long_score"))["direction"]
                    result.short_result.market_state.direction = side if side != "UNKNOWN" else "LONG"
                    return result
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as directory:
                runtime = RadarRuntime(FreshHourlyScanner(), AppConfig(data_dir=directory))
                runtime._latest = report()
                original = copy.deepcopy(runtime._latest.signals[0])
                result = runtime.scan_instrument_dict("AAA")["short"]
                self.assertEqual(result["preflight"]["verdict"]["new_entry_allowed"], expected)
                self.assertEqual(result["decision_context"]["final"]["new_entry_allowed"], expected)
                self.assertEqual(result["decision_context"]["final"]["timeframe_alignment"], result["preflight"]["timeframe_alignment"])
                self.assertEqual(result["item"]["timeframe_states"]["1H"]["label"], hourly_direction(result["preflight"]["timeframe_alignment"]["long_score"])["label"])
                self.assertEqual(result["preflight"]["direction"], original.direction)
                self.assertEqual(result["preflight"]["original"]["stop_loss"], float(original.stop_loss))
                self.assertEqual(runtime._latest.signals[0].market_metrics, original.market_metrics)

    def test_terminal_plan_cannot_be_reopened_by_aligned_hourly_direction(self):
        result = preflight(preflight_signal(), 89)
        self.assertTrue(result["signal_lifecycle"]["terminal"])
        self.assertFalse(result["verdict"]["new_entry_allowed"])
