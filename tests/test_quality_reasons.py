import copy
import unittest
from dataclasses import replace

from radar.preflight_clarity import explain_preflight, _quality_reason
from tests.test_signal_position_policy import preflight_signal, preflight


class QualityReasonTests(unittest.TestCase):
    def test_stop_distance_reason_explains_the_low_score_with_actual_distance(self):
        result = preflight(preflight_signal(), 100)
        detail = _quality_reason("stop_distance", result)
        self.assertIn(f'{result["live"]["risk_pct"]:.2f}%', detail)
        self.assertIn("超過 5%", detail)
        self.assertEqual(result["live"]["quality_components"]["stop_distance"], 3)

    def test_price_departure_explains_change_and_does_not_mutate_the_plan(self):
        base = preflight_signal()
        original = preflight(base, 100)["live"]
        quality = {**original["quality_inputs"], "score": original["quality_score"],
                   "score_components": original["quality_components"],
                   "score_components_version": original["quality_components_version"],
                   "execution_cost_estimated": original["quality_cost_estimated"]}
        signal = replace(base, execution_quality=quality)
        before = copy.deepcopy(signal)
        result = preflight(signal, 101)
        reasons = result["quality_explanation"]["reasons"]
        self.assertIn("降低", reasons[0])
        self.assertIn("高於原進場區", reasons[0])
        self.assertIn("順向偏離", reasons[0])
        self.assertEqual(signal, before)

    def test_rr_and_cost_reasons_use_scoring_inputs_and_thresholds(self):
        result = preflight(preflight_signal(), 100)
        rr = _quality_reason("risk_reward", result)
        self.assertIn("剩餘風報", rr)
        self.assertIn("評分基準 1.80R", rr)
        spread = _quality_reason("spread", result)
        self.assertIn("買賣價差", spread)
        self.assertIn("扣分門檻", spread)
        result["live"].update(quality_cost_estimated=True, quality_inputs={
            "execution_cost_to_risk_pct": 20, "quality_thresholds": {"max_cost_to_risk_pct": 15}})
        self.assertIn("20.0%，超過 15.0%", _quality_reason("execution_cost", result))

    def test_missing_depth_does_not_claim_cost_improvement(self):
        result = preflight(preflight_signal(), 100)
        result["live"]["quality_cost_estimated"] = False
        self.assertIn("不代表實際成交成本改善", _quality_reason("execution_cost", result))

    def test_in_zone_reason_does_not_invent_weakening_or_retest_failure(self):
        result = preflight(preflight_signal(), 100)
        self.assertIn("不因訊號階段或等待重新確認而扣分", _quality_reason("entry_location", result))
        before = copy.deepcopy(result)
        explain_preflight(preflight_signal(), result)
        self.assertEqual(result, before)
