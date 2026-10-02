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
from radar.short_direction import LONG_POLICY, POLICY, hourly_direction, swing_direction
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

    def test_one_family_cross_waits_for_same_side_resonance(self):
        frame = {
            "close": 104.0,
            "ma5": 105.0,
            "ma10": 103.0,
            "ma20": 100.0,
            "ma90": 95.0,
            "macd_line": -0.4,
            "macd_signal": 0.1,
            "ma_last_cross_direction": "BULL",
            "ma_last_cross_bars_ago": 0,
            "macd_last_cross_direction": "BEAR",
            "macd_last_cross_bars_ago": 8,
        }
        result = hourly_direction(frame)
        self.assertEqual(result["direction"], "NEUTRAL")
        self.assertEqual(result["pending_direction"], "LONG")
        self.assertEqual(result["state"], "LONG_FORMING")
        self.assertEqual(result["leading"], "MA")
        self.assertFalse(result["resonance"])

    def test_same_side_crosses_resonate_before_ma20_strength_confirmation(self):
        frame = {
            "close": 99.0,
            "ma5": 99.0,
            "ma10": 98.0,
            "ma20": 105.0,
            "ma90": 120.0,
            "macd_line": 0.5,
            "macd_signal": 0.1,
            "ma_last_cross_direction": "BULL",
            "ma_last_cross_bars_ago": 6,
            "macd_last_cross_direction": "BULL",
            "macd_last_cross_bars_ago": 2,
        }
        result = hourly_direction(frame)
        self.assertEqual(result["direction"], "LONG")
        self.assertTrue(result["resonance"])
        self.assertEqual(result["strength"], "WEAK")
        self.assertEqual(result["state"], "LONG_WEAK")
        self.assertEqual(result["ma20_state"], "MIXED")

    def test_ma20_only_grades_strength_and_sequence_does_not_matter(self):
        # MA5/10 may already be above MA20 before the second crossover arrives,
        # or they may cross MA20 later; the current completed arrangement grades
        # strength while MACD x MA5/10 resonance owns direction.
        frame = {
            "close": 106.0,
            "ma5": 105.0,
            "ma10": 103.0,
            "ma20": 100.0,
            "ma90": 200.0,
            "macd_line": 0.8,
            "macd_signal": 0.2,
            "ma_last_cross_direction": "BULL",
            "ma_last_cross_bars_ago": 9,
            "macd_last_cross_direction": "BULL",
            "macd_last_cross_bars_ago": 1,
        }
        result = hourly_direction(frame)
        self.assertEqual(result["direction"], "LONG")
        self.assertEqual(result["strength"], "STRONG")
        self.assertEqual(result["state"], "LONG_STRONG")
        self.assertEqual(result["ma20_state"], "BULL_STACK")
        # MA90 is reference only and must not veto the formal resonance.
        self.assertEqual(result["ma90_state"], "BELOW")
        self.assertTrue(result["resonance"])

    def test_death_cross_resonance_and_ma20_bear_stack_is_strong_short(self):
        frame = {
            "close": 94.0,
            "ma5": 95.0,
            "ma10": 97.0,
            "ma20": 100.0,
            "ma90": 90.0,
            "macd_line": -0.8,
            "macd_signal": -0.2,
            "ma_last_cross_direction": "BEAR",
            "ma_last_cross_bars_ago": 3,
            "macd_last_cross_direction": "BEAR",
            "macd_last_cross_bars_ago": 7,
        }
        result = hourly_direction(frame)
        self.assertEqual(result["direction"], "SHORT")
        self.assertEqual(result["strength"], "STRONG")
        self.assertEqual(result["state"], "SHORT_STRONG")
        self.assertEqual(result["ma20_state"], "BEAR_STACK")
        self.assertTrue(result["resonance"])

    def test_swing_4h_uses_exact_same_cross_resonance_engine(self):
        frame = {
            "close": 106.0,
            "ma5": 105.0,
            "ma10": 103.0,
            "ma20": 100.0,
            "ma90": 98.0,
            "macd_line": 0.8,
            "macd_signal": 0.2,
            "ma_last_cross_direction": "BULL",
            "ma_last_cross_bars_ago": 4,
            "macd_last_cross_direction": "BULL",
            "macd_last_cross_bars_ago": 1,
        }
        short_direction = hourly_direction(frame)
        long_direction = swing_direction(frame)
        self.assertEqual(long_direction["direction"], short_direction["direction"])
        self.assertEqual(long_direction["strength"], short_direction["strength"])
        self.assertEqual(long_direction["resonance"], short_direction["resonance"])
        self.assertEqual(long_direction["policy"], LONG_POLICY)

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
            if rows is hourly:
                return replace(
                    tf,
                    sma5=tf.sma20,
                    sma10=tf.sma20,
                    macd_line=tf.macd_signal,
                )
            return tf
        with patch("radar.market_story.features", side_effect=measured):
            result = MarketStoryEngine().analyze_short(four, hourly, core)
        self.assertFalse(result.triggered)
        self.assertEqual(result.timeframe_states["1H"]["direction"], "NEUTRAL")

    def test_hourly_display_and_entry_use_same_macd_ma_direction_even_if_fusion_differs(self):
        four, hourly, core = valid_breakout_frames()
        for side in ("LONG", "SHORT"):
            h = hourly if side == "LONG" else mirror(hourly)
            tf = features(h)
            conflicting_fusion = 10 if side == "LONG" else 90
            def measured(rows):
                current = features(rows)
                return replace(current, fusion_long_score=conflicting_fusion) if rows is h else current
            with patch("radar.market_story.features", side_effect=measured):
                story = MarketStoryEngine().analyze_short(four, h, core if side == "LONG" else mirror(core))
            item = signal_dict(side)
            item["market_metrics"]["raw_indicators"]["1H"] = {
                "ma5": tf.sma5,
                "ma10": tf.sma10,
                "ma20": tf.sma20,
                "macd_line": tf.macd_line,
                "macd_signal": tf.macd_signal,
                "macd_hist": tf.macd_hist,
                "macd_prev_hist": tf.macd_prev_hist,
                "fusion_long_score": conflicting_fusion,
            }
            alignment = build_decision_context(item)["final"]["timeframe_alignment"]
            self.assertEqual(story.timeframe_states["1H"]["direction"], side)
            self.assertEqual(alignment["timeframe_direction"], side)
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
