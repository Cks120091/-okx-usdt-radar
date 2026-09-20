from tests import legacy_decision_cases as _legacy
from radar.decision import build_decision_context

for _name in dir(_legacy):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_legacy, _name)


class DecisionContextTests(_legacy.DecisionContextTests):
    def test_trigger_without_a_trade_plan_has_unknown_confirmation(self):
        item = _legacy.complete_signal()
        for key in ("entry_low", "entry_high", "stop_loss", "take_profit_1"):
            item.pop(key)

        result = build_decision_context(item)

        self.assertEqual(result["continuation_confirmation"]["key"], "UNKNOWN")
        self.assertIn("trade_plan", result["hard_gate"]["blockers"])
        self.assertEqual(result["final"]["status"], "HARD_GATE_BLOCKED")
        self.assertFalse(result["final"]["new_entry_allowed"])

    def test_epsilon_beyond_each_hard_gate_limit_vetoes_entry(self):
        advisory_cases = {
            "liquidity": lambda item: item.update({"quote_volume_24h": 1_999_999.99}),
            "spread": lambda item: item.update({"spread_pct": 0.100001}),
            "slippage": lambda item: item["market_metrics"].update({"buy_slippage_pct": 0.150001}),
            "execution_cost": lambda item: (item["market_metrics"].update({"execution_cost_to_risk_pct": 15.0001}), item["execution_quality"].update({"execution_cost_to_risk_pct": 15.0001})),
            "risk_reward": lambda item: (item.update({"risk_reward": 1.7999}), item["entry_eligibility"].update({"remaining_rr": 1.7999})),
            "stop_loss": lambda item: item["market_metrics"].update({"technical_stop_pct": 5.0001}),
        }
        for key, mutate in advisory_cases.items():
            with self.subTest(advisory=key):
                item = _legacy.complete_signal(); mutate(item)
                result = build_decision_context(item)
                self.assertNotIn(key, result["hard_gate"]["blockers"])
                self.assertEqual(result["final"]["status"], "ENTER")
                self.assertTrue(result["final"]["new_entry_allowed"])
                self.assertTrue(result["hard_gate"]["warnings"])

        item = _legacy.complete_signal()
        item["entry_eligibility"].update({"chase_atr": 1.8001})
        result = build_decision_context(item)
        self.assertNotIn("chase", result["hard_gate"]["blockers"])
        self.assertTrue(result["final"]["new_entry_allowed"])

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
        self.assertEqual(result["hard_gate"]["status"], "PASSED")
        self.assertNotIn("SPREAD_TOO_HIGH", result["hard_gate"]["blockers"])
        self.assertNotIn("EXECUTION_COST_TOO_HIGH", result["hard_gate"]["blockers"])
        self.assertTrue(any("SPREAD_TOO_HIGH" in text for text in result["hard_gate"]["warnings"]))
        self.assertTrue(any("EXECUTION_COST_TOO_HIGH" in text for text in result["hard_gate"]["warnings"]))
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_volume_hysteresis_uses_exact_member_and_nonmember_boundaries(self):
        cases = (
            (False, 1_999_999.0, 2_000_000.0, True),
            (False, 2_000_000.0, 2_000_000.0, False),
            (True, 1_499_999.0, 1_500_000.0, True),
            (True, 1_500_000.0, 1_500_000.0, False),
        )
        for member, volume, effective_min, warned in cases:
            with self.subTest(member=member, volume=volume):
                item = _legacy.complete_signal()
                item["quote_volume_24h"] = volume
                item["data_quality"]["universe_volume_policy"] = _legacy.volume_policy(
                    member=member,
                    volume=volume,
                )
                result = build_decision_context(item)
                check = next(
                    row for row in result["hard_gate"]["checks"]
                    if row["key"] == "liquidity"
                )
                self.assertEqual(result["hard_gate"]["liquidity_policy"]["member"], member)
                self.assertEqual(result["hard_gate"]["thresholds"]["min_quote_volume_24h"], effective_min)
                self.assertEqual(check["status"] == "BLOCKED", warned)
                self.assertFalse(check["hard"])
                self.assertTrue(result["final"]["new_entry_allowed"])

    def test_untrusted_member_policy_cannot_lower_new_symbol_entry_line(self):
        item = _legacy.complete_signal()
        item["quote_volume_24h"] = 1_500_000.0
        item["data_quality"]["universe_volume_policy"] = _legacy.volume_policy(
            member=True,
            volume=1_500_000.0,
            trusted=False,
        )
        result = build_decision_context(item)
        self.assertFalse(result["hard_gate"]["liquidity_policy"]["member"])
        self.assertEqual(result["hard_gate"]["thresholds"]["min_quote_volume_24h"], 2_000_000.0)
        self.assertNotIn("liquidity", result["hard_gate"]["blockers"])
        self.assertTrue(any("24H 成交額低於" in warning for warning in result["hard_gate"]["warnings"]))
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_unavailable_publication_volume_never_uses_candidate_fallback(self):
        item = _legacy.complete_signal()
        item["quote_volume_24h"] = 99_000_000.0
        item["data_quality"]["universe_volume_policy"] = _legacy.volume_policy(
            member=True,
            volume=None,
            status="UNAVAILABLE",
        )
        result = build_decision_context(item)
        check = next(
            row for row in result["hard_gate"]["checks"]
            if row["key"] == "liquidity"
        )
        self.assertIsNone(result["hard_gate"]["liquidity_policy"]["volume_usdt"])
        self.assertEqual(check["status"], "UNKNOWN")
        self.assertFalse(check["hard"])
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_known_high_slippage_blocks_even_when_other_side_is_missing(self):
        item = _legacy.complete_signal()
        item["market_metrics"]["buy_slippage_pct"] = 0.20
        del item["market_metrics"]["sell_slippage_pct"]
        result = build_decision_context(item)
        check = next(
            row for row in result["hard_gate"]["checks"]
            if row["key"] == "slippage"
        )
        self.assertEqual(check["status"], "BLOCKED")
        self.assertFalse(check["hard"])
        self.assertEqual(result["hard_gate"]["status"], "PASSED")
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_slippage_uses_direct_threshold_not_quality_score(self):
        item = _legacy.complete_signal()
        item["market_metrics"]["buy_slippage_pct"] = 0.20
        item["execution_quality"]["score"] = 95
        result = build_decision_context(item)
        check = next(
            row for row in result["hard_gate"]["checks"]
            if row["key"] == "slippage"
        )
        self.assertEqual(check["status"], "BLOCKED")
        self.assertFalse(check["hard"])
        self.assertNotIn("slippage", result["hard_gate"]["blockers"])
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_thresholds_parameter_changes_limit_without_changing_priority(self):
        item = _legacy.complete_signal()
        item["spread_pct"] = 0.08
        normal = build_decision_context(item)
        strict = build_decision_context(item, {"max_spread_pct": 0.05})
        self.assertEqual(normal["final"]["status"], "ENTER")
        strict_check = next(
            row for row in strict["hard_gate"]["checks"]
            if row["key"] == "spread"
        )
        self.assertEqual(strict_check["status"], "BLOCKED")
        self.assertFalse(strict_check["hard"])
        self.assertEqual(strict["final"]["status"], "ENTER")
        self.assertTrue(strict["final"]["new_entry_allowed"])

    def test_missed_entry_position_keeps_priority_over_risk_warning(self):
        item = _legacy.complete_signal()
        item["entry_eligibility"].update({
            "status": "MISSED_ENTRY",
            "label": "已錯過｜禁止追價",
            "reason": "價格已離開最佳進場區。",
            "chase_atr": 1.27,
            "remaining_rr": 2.0,
            "actionable": False,
            "new_entry_allowed": False,
        })
        item["spread_pct"] = 0.2
        result = build_decision_context(item)
        self.assertNotIn("entry_permission", result["hard_gate"]["blockers"])
        self.assertNotIn("spread", result["hard_gate"]["blockers"])
        self.assertTrue(any("Spread" in warning for warning in result["hard_gate"]["warnings"]))
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_blocking_anomaly_vetoes_an_otherwise_valid_signal(self):
        item = _legacy.complete_signal()
        item["market_metrics"].update({
            "anomaly_state": "LIQUIDITY_WITHDRAWAL",
            "anomaly_label": "深度突然消失",
        })
        result = build_decision_context(item)
        check = next(
            row for row in result["hard_gate"]["checks"]
            if row["key"] == "anomaly"
        )
        self.assertEqual(check["status"], "BLOCKED")
        self.assertFalse(check["hard"])
        self.assertNotIn("anomaly", result["hard_gate"]["blockers"])
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_spread_hard_gate_takes_priority_over_context_conflict(self):
        item = _legacy.complete_signal()
        item["conflicts"] = ["4H 背景反向，屬逆勢 Trigger"]
        item["spread_pct"] = 0.2
        result = build_decision_context(item)
        self.assertFalse(result["conflict"]["blocks_entry"])
        self.assertNotIn("spread", result["hard_gate"]["blockers"])
        self.assertTrue(any("Spread" in warning for warning in result["hard_gate"]["warnings"]))
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_short_entry_requires_1h_and_15m_direction_alignment(self):
        item = _legacy.complete_signal()
        item["radar_horizon"] = "SHORT"
        item["market_metrics"]["raw_indicators"] = {
            "1H": {"fusion_long_score": 60.0},
            "15m": {"fusion_long_score": 68.0},
        }
        aligned = build_decision_context(item)
        self.assertEqual(aligned["final"]["status"], "ENTER")
        self.assertTrue(aligned["final"]["new_entry_allowed"])
        self.assertEqual(aligned["final"]["timeframe_alignment"]["state"], "ALIGNED")

        item["market_metrics"]["raw_indicators"]["1H"]["fusion_long_score"] = 40.0
        opposed = build_decision_context(item)
        self.assertEqual(opposed["final"]["status"], "WAIT")
        self.assertFalse(opposed["final"]["new_entry_allowed"])
        self.assertEqual(opposed["final"]["wait_reason"]["code"], "TIMEFRAME_DIRECTION_ALIGNMENT")

    def test_4h_does_not_veto_short_entry_when_1h_and_15m_align(self):
        item = _legacy.complete_signal()
        item["radar_horizon"] = "SHORT"
        item["market_metrics"]["raw_indicators"] = {
            "4H": {"fusion_long_score": 25.0},
            "1H": {"fusion_long_score": 61.0},
            "15m": {"fusion_long_score": 66.0},
        }
        result = build_decision_context(item)
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_oi_resonance_is_quality_confirmation_not_standalone_trigger(self):
        item = _legacy.complete_signal()
        item["radar_horizon"] = "SHORT"
        item["market_metrics"]["raw_indicators"] = {
            "1H": {"fusion_long_score": 60.0},
            "15m": {"fusion_long_score": 65.0},
        }
        item["market_metrics"]["continuation_lookback"] = {
            "capital_flow": _legacy.fixed_capital_flow_summary()
        }
        result = build_decision_context(item)
        self.assertEqual(result["oi_resonance"]["state"], "RESONANCE")
        self.assertFalse(result["oi_resonance"]["standalone_trigger"])
        self.assertTrue(result["final"]["new_entry_allowed"])



# Long radar mirrors the short direction/trigger contract.
def test_long_entry_requires_1d_and_4h_direction_alignment():
    item = _legacy.complete_signal()
    item["radar_horizon"] = "LONG"
    item["market_metrics"]["raw_indicators"] = {
        "1D": {"fusion_long_score": 62.0},
        "4H_TRIGGER": {"fusion_long_score": 65.0},
        "1H_TIMING": {"fusion_long_score": 30.0},
    }
    aligned = build_decision_context(item)
    assert aligned["final"]["status"] == "ENTER"
    assert aligned["final"]["new_entry_allowed"] is True
    assert aligned["final"]["timeframe_alignment"]["timeframe"] == "1D"

    item["market_metrics"]["raw_indicators"]["1D"]["fusion_long_score"] = 40.0
    opposed = build_decision_context(item)
    assert opposed["final"]["status"] == "WAIT"
    assert opposed["final"]["new_entry_allowed"] is False
    assert opposed["final"]["wait_reason"]["code"] == "TIMEFRAME_DIRECTION_ALIGNMENT"


def test_mature_short_leg_waits_for_new_retest_trigger():
    item = _legacy.complete_signal()
    item["radar_horizon"] = "SHORT"
    item["direction"] = "LONG"
    item["market_metrics"]["raw_indicators"] = {
        "1H": {"fusion_long_score": 62.0},
        "15m": {"fusion_long_score": 66.0},
    }
    item["market_story"]["raw"]["core_atr"] = 1.0
    item["market_metrics"]["last_price"] = 105.0
    item["market_metrics"]["entry_execution_price"] = 105.0
    item["market_metrics"]["_core_path"] = [
        [i * 900000, 101.0 + i * 0.1, 100.0 + i * 0.1, 100.5 + i * 0.1]
        for i in range(12)
    ]
    item["trigger_type"] = "BREAKOUT"
    result = build_decision_context(item)
    assert result["final"]["status"] == "ENTER"
    assert result["final"]["new_entry_allowed"] is True
    assert result["final"]["swing_maturity"]["state"] == "MATURE"
    assert any("行情已走一段" in row for row in result["final"]["risk_warnings"])


def test_fresh_continuation_has_wider_maturity_allowance():
    item = _legacy.complete_signal()
    item["radar_horizon"] = "SHORT"
    item["direction"] = "LONG"
    item["market_metrics"]["raw_indicators"] = {
        "1H": {"fusion_long_score": 62.0},
        "15m": {"fusion_long_score": 66.0},
    }
    item["market_story"]["raw"]["core_atr"] = 1.0
    item["market_metrics"]["last_price"] = 103.5
    item["market_metrics"]["entry_execution_price"] = 103.5
    item["market_metrics"]["_core_path"] = [
        [i * 900000, 101.0 + i * 0.1, 100.0 + i * 0.1, 100.5 + i * 0.1]
        for i in range(12)
    ]
    item["trigger_type"] = "CONTINUATION"
    result = build_decision_context(item)
    assert result["final"]["swing_maturity"]["passed"] is True
