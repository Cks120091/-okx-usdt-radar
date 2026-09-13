from tests import legacy_decision_cases as _legacy
from radar.decision import build_decision_context

for _name in dir(_legacy):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_legacy, _name)


class DecisionContextTests(_legacy.DecisionContextTests):
    def test_epsilon_beyond_each_hard_gate_limit_vetoes_entry(self):
        hard_cases = {
            "liquidity": lambda item: item.update({"quote_volume_24h": 1_999_999.99}),
            "spread": lambda item: item.update({"spread_pct": 0.100001}),
            "slippage": lambda item: item["market_metrics"].update({"buy_slippage_pct": 0.150001}),
            "chase": lambda item: item["entry_eligibility"].update({"chase_atr": 1.8001}),
        }
        for blocker, mutate in hard_cases.items():
            with self.subTest(hard=blocker):
                item = _legacy.complete_signal(); mutate(item)
                result = build_decision_context(item)
                self.assertIn(blocker, result["hard_gate"]["blockers"])
                self.assertFalse(result["final"]["new_entry_allowed"])

        soft_cases = {
            "execution_cost": lambda item: (item["market_metrics"].update({"execution_cost_to_risk_pct": 15.0001}), item["execution_quality"].update({"execution_cost_to_risk_pct": 15.0001})),
            "risk_reward": lambda item: (item.update({"risk_reward": 1.7999}), item["entry_eligibility"].update({"remaining_rr": 1.7999})),
            "stop_loss": lambda item: item["market_metrics"].update({"technical_stop_pct": 5.0001}),
        }
        for key, mutate in soft_cases.items():
            with self.subTest(advisory=key):
                item = _legacy.complete_signal(); mutate(item)
                result = build_decision_context(item)
                self.assertNotIn(key, result["hard_gate"]["blockers"])
                self.assertEqual(result["final"]["status"], "ENTER")
                self.assertTrue(result["final"]["new_entry_allowed"])
                self.assertTrue(result["hard_gate"]["warnings"])

    def test_execution_cost_uses_warning_band_before_hard_limit(self):
        item = _legacy.complete_signal()
        item["market_metrics"]["execution_cost_to_risk_pct"] = 20.0
        item["execution_quality"]["execution_cost_to_risk_pct"] = 20.0
        result = build_decision_context(item)
        self.assertNotIn("execution_cost", result["hard_gate"]["blockers"])
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])
        self.assertTrue(any("交易成本" in text for text in result["hard_gate"]["warnings"]))

    def test_low_rr_vetoes_entry_without_moving_stop(self):
        item = _legacy.complete_signal()
        original_stop = item["stop_loss"]
        item["risk_reward"] = 0.8
        item["entry_eligibility"]["remaining_rr"] = 0.8
        result = build_decision_context(item)
        self.assertEqual(item["stop_loss"], original_stop)
        self.assertNotIn("risk_reward", result["hard_gate"]["blockers"])
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_upstream_entry_hard_blockers_are_merged_into_hard_gate(self):
        item = _legacy.complete_signal()
        item["entry_eligibility"]["hard_blockers"] = ["SPREAD_TOO_HIGH", "EXECUTION_COST_TOO_HIGH"]
        result = build_decision_context(item)
        self.assertEqual(result["hard_gate"]["status"], "BLOCKED")
        self.assertIn("SPREAD_TOO_HIGH", result["hard_gate"]["blockers"])
        self.assertNotIn("EXECUTION_COST_TOO_HIGH", result["hard_gate"]["blockers"])
        self.assertTrue(any("EXECUTION_COST_TOO_HIGH" in text for text in result["hard_gate"]["warnings"]))
        self.assertFalse(result["final"]["new_entry_allowed"])
