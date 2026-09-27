"""Explanations must stay faithful to the existing permission and price model."""
import copy
import unittest
from dataclasses import replace

from radar.config import AppConfig
from radar.preflight import build_preflight_payload
from radar.preflight_clarity import explain_preflight
from tests.test_signal_position_policy import preflight, preflight_signal
from tests.legacy_preflight_cases import PreflightClient


class PreflightClarityTests(unittest.TestCase):
    def test_no_follow_through_is_suspended_without_ending_or_reopening_plan(self):
        signal = replace(preflight_signal(), signal_stage="NO_FOLLOW_THROUGH")
        before = copy.deepcopy(signal)
        result = preflight(signal, 100)
        self.assertFalse(result["verdict"]["new_entry_allowed"])
        self.assertIn("NO_FORMAL_TRIGGER", result["verdict"]["hard_blockers"])
        self.assertEqual(result["core_assessment"]["state"], "ENTRY_SUSPENDED")
        self.assertIn("未獲延續", result["core_assessment"]["reasons"][0]["detail"])
        self.assertFalse(result["signal_lifecycle"]["terminal"])
        self.assertEqual(signal, before)

    def test_missing_trigger_flag_does_not_claim_no_follow_through(self):
        signal = preflight_signal()
        signal = replace(signal, market_story={**signal.market_story, "trigger": {"triggered": False}})
        result = preflight(signal, 100)
        self.assertFalse(result["verdict"]["new_entry_allowed"])
        self.assertIn("觸發標記未成立", result["core_assessment"]["reasons"][0]["detail"])

    def test_core_missing_is_data_unavailable_not_a_dead_plan(self):
        signal = replace(preflight_signal(), data_quality={"core": "UNAVAILABLE"})
        result = preflight(signal, 100)
        self.assertEqual(result["core_assessment"]["state"], "DATA_UNAVAILABLE")
        self.assertFalse(result["verdict"]["new_entry_allowed"])
        self.assertFalse(result["signal_lifecycle"]["terminal"])

    def test_terminal_takes_priority_and_does_not_describe_zero_as_cost_change(self):
        signal = preflight_signal()
        result = preflight(signal, 89)
        self.assertEqual(result["core_assessment"]["state"], "PLAN_ENDED")
        self.assertEqual(result["quality_explanation"]["mode"], "TERMINAL")
        self.assertFalse(result["verdict"]["new_entry_allowed"])

    def test_missing_permission_never_gets_an_active_label(self):
        result = explain_preflight(preflight_signal(), {})
        self.assertEqual(result["core_assessment"]["state"], "DATA_UNAVAILABLE")

    def test_quote_execution_and_scan_timestamps_remain_distinct(self):
        signal = preflight_signal()
        client = PreflightClient(100)
        ticker = replace(client.get_ticker(signal.inst_id), ts=1_800_000_000_000)
        context = replace(client.get_execution_context(signal.inst_id), sampled_at=1_800_000_060_000)
        result = build_preflight_payload(signal, ticker, context, AppConfig(), report_generated_at="2026-09-20T03:00:00+00:00")
        times = result["data_times"]
        self.assertEqual(times["core_scan_at"], "2026-09-20T03:00:00+00:00")
        self.assertNotEqual(times["quote_at"], times["execution_at"])
        self.assertEqual(times["quote_at"], result["live"]["quote_sampled_at"])
        unknown = build_preflight_payload(signal, replace(ticker, ts=0), context, AppConfig(), report_generated_at=None)
        self.assertIsNone(unknown["data_times"]["quote_at"])

    def test_quality_components_reconcile_to_existing_score(self):
        for quote in (99, 100, 101):
            live = preflight(preflight_signal(), quote)["live"]
            self.assertAlmostEqual(sum(live["quality_components"].values()), live["quality_score"], delta=.051)

    def test_component_comparison_explains_actual_delta_without_mutation(self):
        signal = preflight_signal()
        payload = preflight(signal, 100)
        current = payload["live"]["quality_components"]
        old = {**current, "entry_location": current["entry_location"] - 4,
               "risk_reward": current["risk_reward"] + 2}
        signal = replace(signal, execution_quality={"score": round(sum(old.values()), 1),
                         "score_components_version": 1, "score_components": old})
        payload["original"]["quality_score"] = signal.execution_quality["score"]
        before = copy.deepcopy(payload)
        explanation = explain_preflight(signal, payload)["quality_explanation"]
        self.assertEqual(explanation["mode"], "COMPARISON")
        self.assertEqual(explanation["delta"], 2)
        self.assertIn("提高 4.0", explanation["reasons"][0])
        self.assertIn("降低 2.0", explanation["reasons"][1])
        self.assertEqual(payload, before)

    def test_old_or_inconsistent_components_never_get_invented_change_reasons(self):
        signal = preflight_signal()
        for old in ({}, {"score_components_version": 1, "score_components": {"entry_location": 99}}):
            result = preflight(replace(signal, execution_quality={**signal.execution_quality, **old}), 100)
            explanation = result["quality_explanation"]
            self.assertEqual(explanation["mode"], "CURRENT_ONLY")
            self.assertIn("無法精確歸因", explanation["note"])
            self.assertTrue(all("本次" in reason for reason in explanation["reasons"]))

    def test_missing_depth_uses_neutral_cost_and_discloses_it(self):
        signal = preflight_signal()
        client = PreflightClient(100)
        context = replace(client.get_execution_context(signal.inst_id), execution_notional_usdt=1000, bid_depth_usd=None)
        result = build_preflight_payload(signal, client.get_ticker(signal.inst_id), context, AppConfig(), report_generated_at=None)
        self.assertEqual(result["live"]["quality_components"]["execution_cost"], 5)
        self.assertIn("中性估值", result["quality_explanation"]["note"])
