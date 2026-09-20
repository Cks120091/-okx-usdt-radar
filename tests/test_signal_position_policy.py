"""Behavioral regressions: price location cannot grant or remove signal validity."""
import copy
import json
import math
import unittest
from dataclasses import replace

from radar.config import AppConfig
from radar.decision import build_decision_context
from radar.position_advisory import POLICY_VERSION, position_advisory
from radar.preflight import build_preflight_payload
from radar.public_payload import public_candidate_payload
from radar.service import _canonical_single_decision
from tests.legacy_decision_cases import complete_signal
from tests.legacy_preflight_cases import make_signal, PreflightClient


def signal_dict(direction="LONG", horizon="SHORT", quote=100.0):
    item = complete_signal()
    item.update(direction=direction, radar_horizon=horizon)
    if direction == "SHORT":
        item.update(stop_loss="102", take_profit_1="96", take_profit_2="94")
    item["market_metrics"].update(
        last_price=100.0, entry_execution_price=quote,
        raw_indicators={"1H": {"fusion_long_score": 65 if direction == "LONG" else 35},
                        "1D": {"fusion_long_score": 65 if direction == "LONG" else 35}},
    )
    item["data_quality"]["closed_candle"] = True
    item["entry_eligibility"].update(status="MISSED_ENTRY", chase_atr=20,
                                   actionable=False, new_entry_allowed=False,
                                   reentry_confirmation_required=True, closed_retest_confirmed=False)
    return item


def preflight_signal(direction="LONG", horizon="SHORT"):
    signal = make_signal()
    return replace(signal, direction=direction, radar_horizon=horizon,
                   stop_loss="90" if direction == "LONG" else "110",
                   take_profit_1="120" if direction == "LONG" else "80",
                   take_profit_2="130" if direction == "LONG" else "70",
                   signal_stage="CONFIRMED",
                   data_quality={"core": "AVAILABLE", "closed_candle": True},
                   market_metrics={**signal.market_metrics, "raw_indicators": {
                       "1H": {"fusion_long_score": 65 if direction == "LONG" else 35},
                       "1D": {"fusion_long_score": 65 if direction == "LONG" else 35}}},
                   market_story={**signal.market_story, "trigger": {
                       **signal.market_story["trigger"], "triggered": True}},
                   entry_eligibility={**signal.entry_eligibility, "status": "MISSED_ENTRY",
                                      "actionable": False, "new_entry_allowed": False,
                                      "reentry_confirmation_required": True})


def preflight(signal, quote):
    client = PreflightClient(quote)
    return build_preflight_payload(signal, client.get_ticker(signal.inst_id),
                                   client.get_execution_context(signal.inst_id),
                                   AppConfig(), report_generated_at="2026-09-20T03:00:00+00:00")


class SignalPositionSeparationTests(unittest.TestCase):
    def test_position_matrix_preserves_signal_and_immutable_plan(self):
        for horizon in ("SHORT", "LONG"):
            for direction in ("LONG", "SHORT"):
                for quote, state in ((99.0, "BELOW"), (99.8, "WITHIN"), (100, "WITHIN"),
                                     (100.2, "WITHIN"), (101.0, "ABOVE")):
                    for legacy in ("ENTRY_READY", "WAIT_RETEST", "MISSED_ENTRY", "NO_CHASE"):
                        with self.subTest(horizon=horizon, direction=direction, price=quote, legacy=legacy):
                            item = signal_dict(direction, horizon, quote)
                            item["entry_eligibility"]["status"] = legacy
                            before = copy.deepcopy(item)
                            result = build_decision_context(item)
                            self.assertEqual(result["final"]["status"], "ENTER")
                            self.assertTrue(result["final"]["new_entry_allowed"])
                            self.assertTrue(result["hard_gate"]["passed"])
                            self.assertEqual(result["final"]["position_advisory"]["state"], state)
                            self.assertFalse(result["final"]["position_advisory"]["affects_signal"])
                            self.assertEqual(item, before)

    def test_mtf_mismatch_always_wins_over_position(self):
        for horizon, timeframe in (("SHORT", "1H"), ("LONG", "1D")):
            for direction in ("LONG", "SHORT"):
                item = signal_dict(direction, horizon, 101)
                item["market_metrics"]["raw_indicators"][timeframe]["fusion_long_score"] = 35 if direction == "LONG" else 65
                result = build_decision_context(item)["final"]
                self.assertEqual(result["status"], "WAIT")
                self.assertFalse(result["new_entry_allowed"])
                self.assertEqual(result["wait_reason"]["code"], "TIMEFRAME_DIRECTION_ALIGNMENT")

    def test_present_but_corrupt_direction_data_does_not_grant_permission(self):
        for bad in (None, "bad", float("nan"), 101, -1):
            for timeframe, horizon in (("1H", "SHORT"), ("1D", "LONG")):
                item = signal_dict(horizon=horizon)
                item["market_metrics"]["raw_indicators"][timeframe]["fusion_long_score"] = bad
                self.assertFalse(build_decision_context(item)["final"]["new_entry_allowed"])

    def test_neutral_mtf_is_not_directional_confirmation(self):
        for timeframe, horizon in (("1H", "SHORT"), ("1D", "LONG")):
            item = signal_dict(horizon=horizon)
            item["market_metrics"]["raw_indicators"][timeframe]["fusion_long_score"] = 50
            self.assertFalse(build_decision_context(item)["final"]["new_entry_allowed"])

    def test_location_does_not_bypass_explicit_missing_or_unconfirmed_core(self):
        for delta in ({"core": "UNAVAILABLE"}, {"closed_candle": False},
                      {"required_missing_sources": ["candles_15m"]}):
            item = signal_dict(quote=101)
            item["data_quality"].update(delta)
            result = build_decision_context(item)
            self.assertFalse(result["final"]["new_entry_allowed"])
            self.assertEqual(result["final"]["status"], "DATA_UNAVAILABLE")

    def test_malformed_authoritative_quote_is_not_replaced_with_old_price(self):
        for value in (0, -1, "bad", math.nan, math.inf, True):
            with self.subTest(value=value):
                item = signal_dict(quote=value)
                result = build_decision_context(item)
                self.assertFalse(result["final"]["new_entry_allowed"])
                self.assertIsNone(result["final"]["position_advisory"]["current_price"])

    def test_bad_geometry_and_missing_plan_remain_blocked(self):
        for field, value in (("entry_low", "105"), ("stop_loss", None),
                             ("stop_loss", "103"), ("take_profit_1", "99"),
                             ("entry_high", "-1")):
            item = signal_dict(quote=101)
            item[field] = value
            self.assertFalse(build_decision_context(item)["final"]["new_entry_allowed"])

    def test_stop_and_target_exact_touch_are_terminal_at_any_distance(self):
        for side in ("LONG", "SHORT"):
            template = signal_dict(side)
            for field in ("stop_loss", "take_profit_1"):
                item = signal_dict(side, quote=float(template[field]))
                result = build_decision_context(item)["final"]
                self.assertFalse(result["new_entry_allowed"])
                self.assertEqual(result["status"], "INVALIDATED" if field == "stop_loss" else "NO_EDGE")

    def test_false_or_forming_trigger_never_grants_permission(self):
        for stage in ("NEAR_TRIGGER", "WATCH", "CONFIRMED"):
            item = signal_dict(quote=101)
            item["market_story"]["trigger"]["triggered"] = False
            item["signal_stage"] = stage
            self.assertFalse(build_decision_context(item)["final"]["new_entry_allowed"])

    def test_opposite_formal_trigger_is_not_cleared_by_chase(self):
        for key in ("new_entry_suspended", "opposite_warning_only"):
            item = signal_dict(quote=101)
            item["market_story"]["trigger"][key] = True
            result = build_decision_context(item)
            self.assertFalse(result["final"]["new_entry_allowed"])

    def test_unknown_hard_blocker_not_treated_as_price_position(self):
        item = signal_dict(quote=101)
        item["entry_eligibility"]["hard_blockers"] = ["ENTRY_WINDOW_CHANGED"]
        result = build_decision_context(item)
        self.assertFalse(result["final"]["new_entry_allowed"])
        self.assertIn("ENTRY_WINDOW_CHANGED", result["hard_gate"]["blockers"])

    def test_optional_oi_and_cvd_missing_never_create_a_gate(self):
        item = signal_dict(quote=101)
        item["data_quality"].update(deep="UNAVAILABLE", optional_missing_sources=["open_interest", "taker"])
        result = build_decision_context(item)
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_maturity_warning_is_advisory_and_registered_with_unittest(self):
        item = signal_dict(quote=101)
        item["market_story"]["raw"]["core_atr"] = .2
        item["market_metrics"]["_core_path"] = [[i * 900_000, 100, 99, 99.5] for i in range(20)]
        result = build_decision_context(item)["final"]
        self.assertEqual(result["swing_maturity"]["state"], "MATURE")
        self.assertTrue(result["new_entry_allowed"])
        self.assertIn("不影響訊號", result["swing_maturity"]["reason"])

    def test_public_projection_preserves_position_but_no_internal_paths(self):
        item = signal_dict(quote=101)
        item["market_metrics"]["_core_path"] = [[1, 100, 99, 100]]
        item["decision_context"] = build_decision_context(item)
        item["decision_context"]["final"]["position_advisory"]["private_array"] = [123]
        public = public_candidate_payload(item, signal=True)
        final = public["decision_context"]["final"]
        self.assertEqual(final["position_advisory"]["state"], "ABOVE")
        self.assertEqual(final["timeframe_alignment"]["state"], "ALIGNED")
        self.assertNotIn("_core_path", json.dumps(public))
        self.assertNotIn("private_array", final["position_advisory"])


class PreflightPositionSeparationTests(unittest.TestCase):
    def test_preflight_and_single_decision_agree_everywhere_inside_stop_target(self):
        for horizon in ("SHORT", "LONG"):
            for side in ("LONG", "SHORT"):
                for quote in (95, 99, 100, 101, 105):
                    with self.subTest(horizon=horizon, side=side, price=quote):
                        item = preflight_signal(side, horizon)
                        before = copy.deepcopy(item)
                        payload = preflight(item, quote)
                        self.assertTrue(payload["verdict"]["new_entry_allowed"])
                        self.assertTrue(payload["plan_state"]["new_entry_allowed"])
                        self.assertFalse(payload["plan_state"]["new_trigger_required"])
                        self.assertEqual(payload["entry_policy_version"], POLICY_VERSION)
                        self.assertAlmostEqual(payload["live"]["price"], quote + (.01 if side == "LONG" else -.01))
                        final = _canonical_single_decision(item, payload, None)["final"]
                        self.assertTrue(final["new_entry_allowed"])
                        self.assertEqual(final["position_advisory"], payload["position_advisory"])
                        self.assertEqual(item, before)

    def test_core_direction_block_remains_in_both_preflight_and_single_scan(self):
        for side in ("LONG", "SHORT"):
            item = preflight_signal(side)
            item.market_metrics["raw_indicators"]["1H"]["fusion_long_score"] = 35 if side == "LONG" else 65
            payload = preflight(item, 105)
            self.assertIn("TIMEFRAME_DIRECTION_ALIGNMENT", payload["verdict"]["hard_blockers"])
            self.assertFalse(payload["verdict"]["new_entry_allowed"])
            self.assertFalse(_canonical_single_decision(item, payload, None)["final"]["new_entry_allowed"])

    def test_preflight_missing_core_outside_entry_cannot_be_hidden(self):
        item = preflight_signal()
        item.data_quality["core"] = "MISSING"
        payload = preflight(item, 105)
        self.assertEqual(payload["verdict"]["status"], "DATA_UNAVAILABLE")
        self.assertFalse(payload["verdict"]["new_entry_allowed"])

    def test_closed_trigger_remains_closed_after_quote_returns(self):
        for terminal in ("INVALIDATED", "COMPLETED", "CLOSED_UNKNOWN"):
            item = preflight_signal()
            item.lifecycle.update(status=terminal, terminal=True)
            payload = preflight(item, 100)
            self.assertFalse(payload["verdict"]["new_entry_allowed"])
            self.assertTrue(payload["plan_state"]["new_trigger_required"])

    def test_fresh_live_stop_target_crossing_never_reopens(self):
        for side in ("LONG", "SHORT"):
            item = preflight_signal(side)
            for raw in (float(item.stop_loss), float(item.take_profit_1)):
                quote = raw - .01 if side == "LONG" else raw + .01
                payload = preflight(item, quote)
                self.assertFalse(payload["verdict"]["new_entry_allowed"])
                self.assertTrue(payload["signal_lifecycle"]["terminal"])

    def test_opposite_evidence_with_far_quote_still_blocks(self):
        item = preflight_signal()
        item.market_story["trigger"]["new_entry_suspended"] = True
        payload = preflight(item, 105)
        self.assertFalse(payload["verdict"]["new_entry_allowed"])
        self.assertIn("OPPOSITE_SIGNAL", payload["verdict"]["hard_blockers"])

    def test_closed_candle_confirmation_never_replaced_with_location(self):
        item = preflight_signal()
        item.data_quality["closed_candle"] = False
        self.assertFalse(preflight(item, 105)["verdict"]["new_entry_allowed"])

    def test_plan_geometry_is_checked_before_any_advisory(self):
        item = replace(preflight_signal(), entry_low="130")
        with self.assertRaises(ValueError):
            preflight(item, 105)

    def test_missing_flow_does_not_invent_zero_or_prevent_signal(self):
        item = preflight_signal()
        payload = preflight(item, 105)
        self.assertTrue(payload["verdict"]["new_entry_allowed"])
        self.assertFalse(payload["position_advisory"]["affects_signal"])
        self.assertTrue(payload["safety"]["stored_trigger_unchanged"])


if __name__ == "__main__":
    unittest.main()
