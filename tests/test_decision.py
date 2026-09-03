import copy
import unittest

from radar.decision import build_decision_context
from radar.public_payload import public_candidate_payload


def complete_signal():
    return {
        "inst_id": "AAA-USDT-SWAP",
        "direction": "LONG",
        "regime": "TREND",
        "trigger_type": "BREAKOUT",
        "signal_stage": "CONFIRMED",
        "readiness_score": 82.0,
        "quote_volume_24h": 20_000_000,
        "spread_pct": 0.02,
        "entry_low": "99.8",
        "entry_high": "100.2",
        "stop_loss": "98",
        "take_profit_1": "104",
        "take_profit_2": "106",
        "risk_reward": 2.0,
        "invalidation": "15m 收盤跌破 $98，原計畫失效。",
        "trigger_id": "episode-1",
        "lifecycle": {
            "current_stage": "CONFIRMED",
            "transition": "UNCHANGED",
            "terminal": False,
        },
        "entry_eligibility": {
            "status": "ENTRY_READY",
            "label": "目前可進｜仍在合理區",
            "reason": "價格仍在最佳進場點位。",
            "actionable": True,
            "chase_atr": 0.05,
            "remaining_rr": 2.0,
            "remaining_rr_applicable": True,
        },
        "safety_checks": [
            {"key": "core_data", "passed": True, "hard": True},
            {"key": "universe_liquidity", "passed": True, "hard": True},
            {"key": "risk_reward", "passed": True, "hard": True},
            {"key": "stop_loss", "passed": True, "hard": True},
        ],
        "data_quality": {
            "core": "AVAILABLE",
            "deep": "AVAILABLE",
            "missing_sources": [],
        },
        "market_metrics": {
            "technical_stop_pct": 2.0,
            "buy_slippage_pct": 0.02,
            "sell_slippage_pct": 0.02,
            "execution_cost_to_risk_pct": 8.0,
            "market_driver": {"state": "INDEPENDENT", "label": "個幣獨立行情"},
            "relative_strength": {"state": "STRONG", "label": "相對強勢"},
            "market_resonance": {"state": "LOW", "label": "市場共振有限"},
            "market_sessions": [
                {"key": "LONDON", "label": "倫敦盤", "active": True}
            ],
        },
        "market_story": {
            "trigger": {"triggered": True, "type": "BREAKOUT"},
            "raw": {"core_return_pct": 0.3},
        },
        "evidence_groups": {
            "position_structure": {
                "label": "位置／價格行為",
                "score": 85,
                "stance": "SUPPORT",
                "confidence": 100,
            },
            "trend_momentum": {
                "label": "趨勢／動能",
                "score": 80,
                "stance": "SUPPORT",
                "confidence": 100,
            },
            "participation_flow": {
                "label": "市場參與",
                "score": 75,
                "stance": "SUPPORT",
                "confidence": 85,
            },
        },
        "market_participation": {"state": "SUPPORT", "label": "資金參與支持"},
        "execution_quality": {
            "score": 78,
            "label": "良好",
            "recommendation": "NORMAL",
            "execution_cost_to_risk_pct": 8.0,
        },
        "supporting_evidence": ["15m 結構轉多", "主動買盤增強", "OI 增加"],
        "conflicts": [],
    }


class DecisionContextTests(unittest.TestCase):
    def test_complete_signal_produces_one_enter_decision(self):
        result = build_decision_context(complete_signal())

        self.assertEqual(result["hard_gate"]["status"], "PASSED")
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])
        self.assertEqual(result["conflict"]["main_direction"], "LONG")
        self.assertEqual(result["quality"]["combined_score"], None)
        self.assertEqual(result["confidence"]["key"], "HIGH")
        self.assertEqual(result["market_context"]["driver"]["state"], "INDEPENDENT")
        self.assertEqual(result["market_context"]["sessions"][0]["label"], "倫敦盤")

    def test_function_does_not_mutate_input(self):
        item = complete_signal()
        before = copy.deepcopy(item)

        build_decision_context(item)

        self.assertEqual(item, before)

    def test_partial_deep_data_warns_but_keeps_formal_entry(self):
        item = complete_signal()
        item["data_quality"] = {
            "core": "AVAILABLE",
            "deep": "PARTIAL",
            "missing_sources": ["order_book"],
        }

        result = build_decision_context(item)

        self.assertEqual(result["hard_gate"]["status"], "UNKNOWN")
        self.assertIn("data_quality", result["hard_gate"]["unknowns"])
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])
        self.assertTrue(result["hard_gate"]["advisory_only"])

    def test_missing_execution_numbers_are_advisory(self):
        item = complete_signal()
        del item["market_metrics"]["buy_slippage_pct"]
        del item["market_metrics"]["execution_cost_to_risk_pct"]
        item["execution_quality"].pop("execution_cost_to_risk_pct")

        result = build_decision_context(item)

        self.assertEqual(result["hard_gate"]["status"], "UNKNOWN")
        self.assertIn("slippage", result["hard_gate"]["unknowns"])
        self.assertIn("execution_cost", result["hard_gate"]["unknowns"])
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_failed_legacy_hard_check_is_advisory_when_entry_is_ready(self):
        item = complete_signal()
        item["safety_checks"].append(
            {"key": "api_data", "passed": False, "hard": True, "label": "API 失敗"}
        )

        result = build_decision_context(item)

        self.assertEqual(result["hard_gate"]["status"], "BLOCKED")
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_slippage_uses_direct_threshold_not_quality_score(self):
        item = complete_signal()
        item["market_metrics"]["buy_slippage_pct"] = 0.20
        item["execution_quality"]["score"] = 95

        result = build_decision_context(item)

        self.assertIn("slippage", result["hard_gate"]["blockers"])
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_thresholds_parameter_changes_limit_without_changing_priority(self):
        item = complete_signal()
        item["spread_pct"] = 0.08

        normal = build_decision_context(item)
        strict = build_decision_context(item, {"max_spread_pct": 0.05})

        self.assertEqual(normal["final"]["status"], "ENTER")
        self.assertIn("spread", strict["hard_gate"]["blockers"])
        self.assertTrue(strict["final"]["new_entry_allowed"])

    def test_no_chase_preserves_trigger_and_is_not_invalidation(self):
        item = complete_signal()
        item["entry_eligibility"].update(
            {
                "status": "MISSED_ENTRY",
                "label": "已錯過｜禁止追價",
                "reason": "價格已順向離開最佳進場區。",
                "chase_atr": 2.1,
            }
        )

        result = build_decision_context(item)

        self.assertEqual(result["final"]["status"], "NO_CHASE")
        self.assertTrue(result["final"]["trigger_preserved"])
        self.assertFalse(result["final"]["new_entry_allowed"])
        self.assertNotEqual(result["episode"]["status"], "INVALIDATED")

    def test_missed_entry_below_severe_threshold_is_no_chase_not_severe_gate(self):
        item = complete_signal()
        item["entry_eligibility"].update(
            {
                "status": "MISSED_ENTRY",
                "label": "已錯過｜禁止追價",
                "reason": "價格已離開最佳進場區。",
                "chase_atr": 1.27,
                "remaining_rr": 2.0,
                "actionable": False,
                "new_entry_allowed": False,
            }
        )

        result = build_decision_context(item)
        chase = next(
            check
            for check in result["hard_gate"]["checks"]
            if check["key"] == "chase"
        )

        self.assertEqual(chase["status"], "PASSED")
        self.assertEqual(chase["value"]["chase_atr"], 1.27)
        self.assertNotIn("超過嚴重追價門檻", chase["reason"])
        self.assertEqual(result["hard_gate"]["blockers"], ["entry_permission"])
        self.assertEqual(result["final"]["status"], "NO_CHASE")
        self.assertFalse(result["final"]["new_entry_allowed"])

    def test_missed_entry_position_keeps_priority_over_risk_warning(self):
        item = complete_signal()
        item["entry_eligibility"].update(
            {
                "status": "MISSED_ENTRY",
                "label": "已錯過｜禁止追價",
                "reason": "價格已離開最佳進場區。",
                "chase_atr": 1.27,
                "remaining_rr": 2.0,
                "actionable": False,
                "new_entry_allowed": False,
            }
        )
        item["spread_pct"] = 0.2

        result = build_decision_context(item)

        self.assertIn("entry_permission", result["hard_gate"]["blockers"])
        self.assertIn("spread", result["hard_gate"]["blockers"])
        self.assertEqual(result["final"]["status"], "NO_CHASE")
        self.assertFalse(result["final"]["new_entry_allowed"])

    def test_live_entry_distance_overrides_stale_hidden_entry_quality(self):
        item = complete_signal()
        item["entry_eligibility"].update(
            {
                "status": "ENTRY_READY",
                "label": "目前可進｜仍在合理區",
                "chase_atr": 0.0,
            }
        )
        item["market_metrics"]["entry_chase_atr"] = 2.4
        item["entry_quality"] = {
            "key": "SEVERE_CHASE",
            "label": "嚴重追價",
            "extension_atr": 2.4,
        }

        result = build_decision_context(item)
        chase = next(
            check
            for check in result["hard_gate"]["checks"]
            if check["key"] == "chase"
        )

        self.assertEqual(chase["status"], "PASSED")
        self.assertEqual(chase["value"]["source"], "entry_eligibility.chase_atr")
        self.assertEqual(chase["value"]["chase_atr"], 0.0)
        self.assertEqual(chase["value"]["entry_quality_key"], "SEVERE_CHASE")
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_legacy_ready_status_is_not_overridden_by_risk_review(self):
        item = complete_signal()
        item["entry_eligibility"].update(
            {
                "status": "ENTRY_READY",
                "label": "目前可進｜仍在合理區",
                "chase_atr": 2.1,
            }
        )

        result = build_decision_context(item)
        chase = next(
            check
            for check in result["hard_gate"]["checks"]
            if check["key"] == "chase"
        )

        self.assertEqual(chase["status"], "BLOCKED")
        self.assertEqual(chase["value"]["source"], "entry_eligibility.chase_atr")
        self.assertEqual(chase["value"]["chase_atr"], 2.1)
        self.assertEqual(chase["value"]["threshold_atr"], 1.8)
        self.assertIn("2.10 ATR", chase["reason"])
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])
        self.assertTrue(result["hard_gate"]["advisory_only"])

    def test_legacy_episode_uses_numeric_quality_extension_as_chase_fallback(self):
        item = complete_signal()
        item["entry_eligibility"].pop("chase_atr")
        item["entry_quality"] = {
            "key": "SEVERE_CHASE",
            "label": "嚴重追價",
            "extension_atr": 2.2,
        }

        result = build_decision_context(item)
        chase = next(
            check
            for check in result["hard_gate"]["checks"]
            if check["key"] == "chase"
        )

        self.assertEqual(chase["status"], "BLOCKED")
        self.assertEqual(chase["value"]["source"], "entry_quality.extension_atr")
        self.assertEqual(chase["value"]["chase_atr"], 2.2)
        self.assertEqual(result["final"]["status"], "ENTER")

    def test_missing_live_chase_with_nonsevere_legacy_value_is_unknown(self):
        item = complete_signal()
        item["entry_eligibility"].pop("chase_atr")
        item["entry_quality"] = {
            "key": "ACCEPTABLE",
            "label": "可以接受",
            "extension_atr": 0.4,
        }

        result = build_decision_context(item)
        chase = next(
            check
            for check in result["hard_gate"]["checks"]
            if check["key"] == "chase"
        )

        self.assertEqual(chase["status"], "UNKNOWN")
        self.assertEqual(chase["value"]["source"], "live_chase_unavailable")
        self.assertIsNone(chase["value"]["chase_atr"])
        self.assertEqual(chase["value"]["entry_quality_extension_atr"], 0.4)
        self.assertIn("chase", result["hard_gate"]["unknowns"])
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_terminal_invalidation_has_highest_priority_and_cannot_revive(self):
        item = complete_signal()
        item["lifecycle"].update(
            {
                "current_stage": "INVALIDATED",
                "status": "INVALIDATED",
                "terminal": True,
                "outcome": "SL_FIRST",
            }
        )
        item["data_quality"] = {"core": "MISSING", "deep": "MISSING"}
        item["market_metrics"]["anomaly_state"] = "FLASH_CRASH"

        result = build_decision_context(item)

        self.assertEqual(result["final"]["status"], "INVALIDATED")
        self.assertFalse(result["final"]["trigger_preserved"])
        self.assertEqual(result["episode"]["status"], "INVALIDATED")

    def test_anomaly_is_warning_for_an_otherwise_valid_signal(self):
        item = complete_signal()
        item["market_metrics"].update(
            {"anomaly_state": "LIQUIDITY_WITHDRAWAL", "anomaly_label": "深度突然消失"}
        )

        result = build_decision_context(item)

        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])
        self.assertIn("anomaly", result["hard_gate"]["blockers"])

    def test_anomaly_watch_warns_without_becoming_a_hard_gate(self):
        item = complete_signal()
        item["market_metrics"].update(
            {
                "anomaly_state": "WATCH",
                "anomalies": ["Funding（資金費率）偏擁擠"],
            }
        )
        item["market_story"]["context"] = {
            "anomaly": {
                "status": "WATCH",
                "label": "異常風險觀察",
                "reasons": [
                    {
                        "code": "FUNDING_CROWDED",
                        "label": "Funding（資金費率）偏擁擠",
                        "severity": "WATCH",
                    }
                ],
            }
        }

        result = build_decision_context(item)

        self.assertNotIn("anomaly", result["hard_gate"]["blockers"])
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])
        self.assertEqual(result["confidence"]["key"], "MEDIUM")
        self.assertIn(
            "Funding（資金費率）偏擁擠",
            result["market_context"]["anomaly_warnings"],
        )
        self.assertEqual(
            result["final"]["warnings"],
            ["Funding（資金費率）偏擁擠"],
        )

    def test_unknown_terminal_flag_fails_closed_even_if_stage_looks_ready(self):
        item = complete_signal()
        item["lifecycle"].update(
            {
                "current_stage": "CONFIRMED",
                "status": "ARCHIVED_BY_UPSTREAM",
                "terminal": True,
            }
        )

        result = build_decision_context(item)

        self.assertEqual(result["final"]["status"], "INVALIDATED")
        self.assertFalse(result["final"]["new_entry_allowed"])
        self.assertFalse(result["final"]["trigger_preserved"])
        self.assertTrue(result["episode"]["terminal"])

    def test_watch_without_reason_still_surfaces_and_lowers_confidence(self):
        item = complete_signal()
        item["market_story"]["context"] = {
            "anomaly": {"status": "WATCH"}
        }

        result = build_decision_context(item)

        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertEqual(result["confidence"]["key"], "MEDIUM")
        self.assertEqual(
            result["market_context"]["anomaly_warnings"],
            ["異常行情風險觀察"],
        )

    def test_stop_and_closed_aliases_are_terminal_invalidation(self):
        for terminal_status in ("STOP_HIT", "SL_HIT", "CLOSED"):
            with self.subTest(terminal_status=terminal_status):
                item = complete_signal()
                item["lifecycle"].update(
                    {
                        "status": terminal_status,
                        "terminal": False,
                    }
                )

                result = build_decision_context(item)

                self.assertEqual(result["final"]["status"], "INVALIDATED")
                self.assertFalse(result["final"]["new_entry_allowed"])

    def test_terminal_target_completion_never_reenters(self):
        item = complete_signal()
        item["lifecycle"].update(
            {
                "status": "CLOSED",
                "outcome": "TP1_FIRST",
                "terminal": True,
            }
        )

        result = build_decision_context(item)

        self.assertEqual(result["final"]["status"], "NO_EDGE")
        self.assertFalse(result["final"]["new_entry_allowed"])
        self.assertTrue(result["episode"]["terminal"])

    def test_upstream_entry_hard_blockers_are_merged_into_hard_gate(self):
        item = complete_signal()
        item["entry_eligibility"]["hard_blockers"] = [
            "SPREAD_TOO_HIGH",
            "EXECUTION_COST_TOO_HIGH",
        ]

        result = build_decision_context(item)

        self.assertEqual(result["hard_gate"]["status"], "BLOCKED")
        self.assertIn("SPREAD_TOO_HIGH", result["hard_gate"]["blockers"])
        self.assertIn("EXECUTION_COST_TOO_HIGH", result["hard_gate"]["blockers"])
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_legacy_permission_false_is_advisory_when_position_is_ready(self):
        item = complete_signal()
        item["entry_eligibility"]["new_entry_allowed"] = False

        result = build_decision_context(item)

        self.assertIn("entry_permission", result["hard_gate"]["blockers"])
        self.assertEqual(result["hard_gate"]["status"], "BLOCKED")
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_wait_retest_and_missed_entry_are_not_terminal_states(self):
        cases = (
            ("WAIT_RETEST", "WAIT", "ENTRY_RETEST"),
            ("MISSED_ENTRY", "WAIT", "ENTRY_WINDOW_CLOSED"),
        )
        for entry_status, expected_final, expected_wait_code in cases:
            with self.subTest(entry_status=entry_status):
                item = complete_signal()
                item["entry_eligibility"].update(
                    {
                        "status": entry_status,
                        "label": "等待更新判定",
                        "actionable": False,
                        "chase_atr": 0.2,
                    }
                )

                result = build_decision_context(item)

                self.assertEqual(result["final"]["status"], expected_final)
                self.assertEqual(
                    result["final"]["wait_reason"]["code"],
                    expected_wait_code,
                )
                self.assertTrue(result["final"]["trigger_preserved"])
                self.assertFalse(result["episode"]["terminal"])

    def test_inactive_lifecycle_window_is_not_mislabeled_as_price_chase(self):
        item = complete_signal()
        item["lifecycle"].update(
            {
                "current_stage": "TRENDING",
                "transition": "UNCHANGED",
                "terminal": False,
            }
        )
        item["entry_eligibility"].update(
            {
                "status": "MISSED_ENTRY",
                "label": "已錯過｜生命週期已離開進場階段",
                "reason": "訊號仍保留作追蹤，但目前階段不再提供新進場。",
                "chase_atr": 0.0,
                "missed_chase_atr": 0.5,
                "actionable": False,
                "new_entry_allowed": False,
            }
        )

        result = build_decision_context(item)

        self.assertEqual(result["hard_gate"]["blockers"], ["entry_permission"])
        self.assertEqual(result["final"]["status"], "WAIT")
        self.assertEqual(
            result["final"]["wait_reason"]["code"],
            "ENTRY_WINDOW_CLOSED",
        )
        self.assertNotIn("追價", result["final"]["label"])
        self.assertTrue(
            any(
                "不是價格追價判定" in reason
                for reason in result["final"]["reasons"]
            )
        )
        self.assertFalse(result["final"]["new_entry_allowed"])
        self.assertTrue(result["final"]["trigger_preserved"])

    def test_low_rr_is_a_warning_without_moving_stop(self):
        item = complete_signal()
        item["risk_reward"] = 1.1
        item["entry_eligibility"]["remaining_rr"] = 1.1

        result = build_decision_context(item)

        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertIn("risk_reward", result["hard_gate"]["blockers"])
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_execution_cost_uses_warning_band_before_hard_limit(self):
        for cost in (13.5, 15.0):
            with self.subTest(cost=cost):
                item = complete_signal()
                item["market_metrics"]["execution_cost_to_risk_pct"] = cost
                item["execution_quality"]["execution_cost_to_risk_pct"] = cost

                result = build_decision_context(item)

                self.assertNotIn("execution_cost", result["hard_gate"]["blockers"])
                self.assertEqual(result["final"]["status"], "ENTER")
                self.assertTrue(result["final"]["new_entry_allowed"])
                self.assertTrue(
                    any("高於 10.0% 建議線" in value for value in result["hard_gate"]["warnings"])
                )
                self.assertFalse(
                    any(value == "Execution Cost（交易成本）10% 建議線" for value in result["hard_gate"]["warnings"])
                )

        blocked = complete_signal()
        blocked["market_metrics"]["execution_cost_to_risk_pct"] = 15.1
        blocked["execution_quality"]["execution_cost_to_risk_pct"] = 15.1

        result = build_decision_context(blocked)

        self.assertIn("execution_cost", result["hard_gate"]["blockers"])
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_correlated_countertrend_context_does_not_cancel_formal_trigger(self):
        item = complete_signal()
        item["conflicts"] = [
            "1H 背景反向，屬逆勢 Trigger",
            "更高週期背景明顯反向；只列 Conflict，不取消核心 Trigger",
            "全市場背景與 Trigger 方向相反（逆勢）",
        ]
        # Compatibility case for an episode produced before macro context was
        # separated from Participation（市場參與）.
        item["evidence_groups"]["participation_flow"]["stance"] = "CONFLICT"

        result = build_decision_context(item)

        self.assertEqual(result["conflict"]["level"], "MEDIUM")
        self.assertTrue(result["conflict"]["countertrend"])
        self.assertFalse(result["conflict"]["blocks_entry"])
        self.assertEqual(
            [row["key"] for row in result["conflict"]["domains"]],
            ["CONTEXT_COUNTERTREND"],
        )
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])
        self.assertEqual(result["confidence"]["key"], "MEDIUM")
        self.assertFalse(result["conflict"]["opposite_signal_created"])

    def test_boundary_cost_and_correlated_context_preserve_formal_entry(self):
        """Regression for the DASH-like case that disappeared from 可進."""
        item = complete_signal()
        item["market_metrics"]["execution_cost_to_risk_pct"] = 13.5
        item["execution_quality"]["execution_cost_to_risk_pct"] = 13.5
        item["conflicts"] = [
            "1H 背景反向，屬逆勢 Trigger",
            "更高週期背景明顯反向；只列 Conflict，不取消核心 Trigger",
            "全市場背景與 Trigger 方向相反（逆勢）",
        ]
        item["evidence_groups"]["participation_flow"]["stance"] = "CONFLICT"

        result = build_decision_context(item)

        self.assertEqual(result["hard_gate"]["status"], "PASSED")
        self.assertTrue(result["hard_gate"]["warnings"])
        self.assertEqual(result["conflict"]["level"], "MEDIUM")
        self.assertFalse(result["conflict"]["blocks_entry"])
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])
        self.assertTrue(result["final"]["warnings"])

    def test_duplicate_flow_conflict_counts_as_one_domain(self):
        item = complete_signal()
        reason = "主動成交與價格反應明顯反向"
        item["conflicts"] = [reason]
        item["evidence_groups"]["participation_flow"].update(
            {"stance": "CONFLICT", "conflicts": [reason]}
        )
        item["market_participation"] = {
            "state": "CONFLICT",
            "label": "存在反向證據",
            "conflicts": [reason],
        }

        result = build_decision_context(item)

        self.assertEqual(result["conflict"]["items"], [reason])
        self.assertEqual(
            [row["key"] for row in result["conflict"]["domains"]],
            ["TAKER_FLOW"],
        )
        self.assertFalse(result["conflict"]["blocks_entry"])
        self.assertEqual(result["final"]["status"], "ENTER")

    def test_two_conflicting_evidence_groups_do_not_cancel_formal_trigger(self):
        item = complete_signal()
        item["conflicts"] = ["價格仍在壓縮中段", "攻擊效率未改善"]
        item["evidence_groups"]["position_structure"].update(
            {
                "stance": "CONFLICT",
                "score": 30,
                "conflicts": ["價格仍在壓縮中段"],
            }
        )
        item["evidence_groups"]["trend_momentum"].update(
            {
                "stance": "CONFLICT",
                "score": 35,
                "conflicts": ["攻擊效率未改善"],
            }
        )

        result = build_decision_context(item)

        self.assertEqual(
            [row["key"] for row in result["conflict"]["domains"]],
            ["POSITION_STRUCTURE", "TREND_MOMENTUM"],
        )
        self.assertFalse(result["conflict"]["blocks_entry"])
        self.assertEqual(result["conflict"]["blocking_domains"], [])
        self.assertEqual(result["conflict"]["level"], "HIGH")
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_countertrend_plus_live_flow_is_advisory_not_entry_veto(self):
        item = complete_signal()
        item["conflicts"] = [
            "4H 背景反向，屬逆勢 Trigger",
            "主動成交與價格反應明顯反向",
        ]

        result = build_decision_context(item)

        self.assertEqual(result["conflict"]["level"], "HIGH")
        self.assertFalse(result["conflict"]["blocks_entry"])
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_spread_and_context_conflict_are_both_advisory(self):
        item = complete_signal()
        item["conflicts"] = ["4H 背景反向，屬逆勢 Trigger"]
        item["spread_pct"] = 0.2

        result = build_decision_context(item)

        self.assertFalse(result["conflict"]["blocks_entry"])
        self.assertIn("spread", result["hard_gate"]["blockers"])
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_high_conflict_lowers_confidence_but_never_changes_entry_or_direction(self):
        item = complete_signal()
        item["conflicts"] = [
            "4H 背景反向，屬逆勢 Trigger",
            "主動成交與價格反應明顯反向",
            "OI 持續衰退",
            "Timing 週期反向",
        ]

        result = build_decision_context(item)

        self.assertEqual(result["conflict"]["level"], "HIGH")
        self.assertTrue(result["conflict"]["countertrend"])
        self.assertEqual(result["conflict"]["main_direction"], "LONG")
        self.assertFalse(result["conflict"]["opposite_signal_created"])
        self.assertFalse(result["conflict"]["blocks_entry"])
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])
        self.assertEqual(result["confidence"]["key"], "LOW")

    def test_episode_transition_maps_to_strengthening_and_weakening(self):
        strengthening = complete_signal()
        strengthening["lifecycle"]["transition"] = "UPGRADED"
        weakening = complete_signal()
        weakening["signal_stage"] = "NO_FOLLOW_THROUGH"
        weakening["lifecycle"] = {
            "current_stage": "NO_FOLLOW_THROUGH",
            "transition": "DOWNGRADED",
        }

        stronger = build_decision_context(strengthening)
        weaker = build_decision_context(weakening)

        self.assertEqual(stronger["episode"]["status"], "STRENGTHENING")
        self.assertEqual(stronger["episode"]["arrow"], "↑")
        self.assertEqual(weaker["episode"]["status"], "WEAKENING")
        self.assertEqual(weaker["episode"]["arrow"], "↓")
        self.assertFalse(weaker["final"]["new_entry_allowed"])

    def test_extended_stage_is_weakening_even_if_legacy_rank_called_it_upgraded(self):
        item = complete_signal()
        item["signal_stage"] = "EXTENDED"
        item["lifecycle"] = {
            "current_stage": "EXTENDED",
            "transition": "UPGRADED",
        }

        result = build_decision_context(item)

        self.assertEqual(result["episode"]["status"], "WEAKENING")
        self.assertEqual(result["episode"]["arrow"], "↓")
        self.assertFalse(result["final"]["new_entry_allowed"])

    def test_unknown_context_is_explicit_and_not_fabricated(self):
        item = complete_signal()
        for key in ("market_driver", "relative_strength", "market_resonance"):
            item["market_metrics"].pop(key)

        result = build_decision_context(item)

        self.assertEqual(result["market_context"]["driver"]["state"], "UNKNOWN")
        self.assertEqual(result["market_context"]["relative_strength"]["state"], "UNKNOWN")
        self.assertEqual(result["market_context"]["resonance"]["state"], "UNKNOWN")

    def test_two_of_three_directional_votes_confirm_continuation(self):
        item = complete_signal()
        item["market_metrics"].update(
            {
                "flow_oi_alignment": "SAME_DIRECTION_BUILD",
                "flow_taker_state": "STRENGTHENING",
                "flow_participation_state": "STRENGTHENING",
                "flow_valid_sample_count": 3,
                "price_change_core_pct": 0.42,
                "taker_buy_pct": 65.0,
                "volume_ratio_core": 0.95,
            }
        )
        item["market_story"].update(
            {
                "price_acceptance": {
                    "state": "ACCEPTED",
                    "label": "Zone 外新價格獲得接受",
                },
                "trigger": {
                    "triggered": True,
                    "type": "BREAKOUT",
                    "momentum_confirmation": {
                        "confirmed": True,
                        "partial": True,
                        "label": "MA5/10 與 MACD 已同向呼應",
                    },
                },
            }
        )

        result = build_decision_context(item)
        continuation = result["continuation_confirmation"]

        self.assertEqual(continuation["key"], "CONFIRMED")
        self.assertEqual(continuation["score"], 83.3)
        self.assertTrue(any("OI" in text for text in continuation["supporting"]))
        self.assertTrue(any("Taker" in text for text in continuation["supporting"]))
        self.assertEqual(continuation["conflicts"], [])
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_taker_and_cvd_share_one_vote_instead_of_double_counting(self):
        item = complete_signal()
        item["supporting_evidence"] = ["15m 結構轉多"]
        item["market_participation"] = {
            "state": "SUPPORT",
            "supporting": [
                "主動成交與價格成果同向",
                "近期 CVD 與價格成果同向",
            ],
            "trend": {"state": "STRENGTHENING"},
        }
        item["market_metrics"].update(
            {
                "flow_oi_alignment": "STABLE",
                "flow_taker_state": "STRENGTHENING",
                "flow_participation_state": "STRENGTHENING",
                "flow_valid_sample_count": 3,
                "price_change_core_pct": 0.35,
                "volume_ratio_core": 0.8,
                "cvd": 500.0,
            }
        )

        result = build_decision_context(item)
        continuation = result["continuation_confirmation"]

        self.assertEqual(continuation["key"], "FORMING")
        self.assertEqual(continuation["score"], 66.7)
        self.assertEqual(result["final"]["status"], "ENTER")

    def test_insufficient_flow_history_is_forming_not_fake_confirmation(self):
        item = complete_signal()
        item["supporting_evidence"] = ["15m 結構轉多"]
        item["market_participation"] = {
            "state": "DATA_MISSING",
            "supporting": [],
            "conflicts": [],
            "trend": {"state": "UNKNOWN", "label": "資料不足"},
        }
        item["market_metrics"].update(
            {
                "flow_oi_alignment": "UNKNOWN",
                "flow_taker_state": "UNKNOWN",
                "flow_participation_state": "UNKNOWN",
                "flow_valid_sample_count": 2,
                "price_change_core_pct": 0.3,
                "volume_ratio_core": 1.4,
            }
        )

        result = build_decision_context(item)
        continuation = result["continuation_confirmation"]

        self.assertEqual(continuation["key"], "FORMING")
        self.assertIn("連續資金流歷史（至少 3 筆）", continuation["missing"])
        self.assertEqual(result["final"]["status"], "ENTER")

    def test_absorption_is_low_conflict_but_never_cancels_entry(self):
        item = complete_signal()
        item["supporting_evidence"] = ["15m 結構轉多"]
        item["market_participation"] = {"state": "NEUTRAL"}
        item["market_story"]["raw"]["core_return_pct"] = -0.02
        item["market_metrics"].update(
            {
                "flow_oi_alignment": "SAME_DIRECTION_BUILD",
                "flow_taker_state": "STRENGTHENING",
                "flow_participation_state": "MIXED",
                "flow_valid_sample_count": 3,
                "price_change_core_pct": -0.02,
                "taker_buy_pct": 68.0,
                "volume_ratio_core": 0.9,
            }
        )

        result = build_decision_context(item)
        continuation = result["continuation_confirmation"]

        self.assertEqual(continuation["key"], "CONFLICT")
        self.assertTrue(any("吸收" in text for text in continuation["conflicts"]))
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])
        self.assertEqual(result["final"]["direction"], "LONG")

    def test_opposite_oi_build_overrides_two_support_votes_as_information_only(self):
        item = complete_signal()
        item["supporting_evidence"] = ["15m 結構轉多"]
        item["market_participation"] = {"state": "NEUTRAL"}
        item["market_metrics"].update(
            {
                "flow_oi_alignment": "OPPOSITE_BUILD",
                "flow_taker_state": "STRENGTHENING",
                "flow_participation_state": "MIXED",
                "flow_valid_sample_count": 3,
                "price_change_core_pct": 0.4,
                "volume_ratio_core": 1.5,
            }
        )

        result = build_decision_context(item)
        continuation = result["continuation_confirmation"]

        self.assertEqual(continuation["key"], "CONFLICT")
        self.assertLessEqual(continuation["score"], 35.0)
        self.assertTrue(any("opposite build" in text for text in continuation["conflicts"]))
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["trigger_preserved"])

    def test_funding_btc_and_higher_timeframe_are_warnings_not_votes(self):
        item = complete_signal()
        item["conflicts"] = [
            "同方向 Funding 極端擁擠",
            "BTC 大盤轉弱",
            "4H 高週期背景反向（逆勢）",
        ]
        item["market_participation"] = {"state": "NEUTRAL"}
        item["supporting_evidence"] = ["15m 結構轉多"]
        item["market_metrics"].update(
            {
                "flow_oi_alignment": "SAME_DIRECTION_BUILD",
                "flow_taker_state": "STRENGTHENING",
                "flow_participation_state": "STRENGTHENING",
                "flow_valid_sample_count": 3,
                "price_change_core_pct": 0.35,
                "taker_buy_pct": 65.0,
                "volume_ratio_core": 0.9,
            }
        )

        result = build_decision_context(item)
        continuation = result["continuation_confirmation"]

        self.assertEqual(continuation["key"], "CONFIRMED")
        self.assertEqual(continuation["conflicts"], [])
        self.assertTrue(
            all(
                warning in continuation["warnings"]
                for warning in item["conflicts"]
            )
        )
        self.assertEqual(result["final"]["status"], "ENTER")

    def test_multiple_counterevidence_domains_are_low_conflict(self):
        item = complete_signal()
        item["supporting_evidence"] = ["15m 結構轉多"]
        item["market_participation"] = {"state": "NEUTRAL"}
        item["market_story"]["raw"]["core_return_pct"] = -0.3
        item["market_metrics"].update(
            {
                "flow_oi_alignment": "SAME_DIRECTION_BUILD",
                "flow_taker_state": "WEAKENING",
                "flow_participation_state": "MIXED",
                "flow_valid_sample_count": 3,
                "price_change_core_pct": -0.3,
                "volume_ratio_core": 1.6,
            }
        )

        result = build_decision_context(item)
        continuation = result["continuation_confirmation"]

        self.assertEqual(continuation["key"], "CONFLICT")
        self.assertTrue(any("Taker" in text for text in continuation["conflicts"]))
        self.assertTrue(any("量能" in text for text in continuation["conflicts"]))
        self.assertEqual(result["final"]["status"], "ENTER")

    def test_absent_participation_sources_are_explicitly_unknown(self):
        item = complete_signal()
        item["supporting_evidence"] = ["15m 結構轉多"]
        item["market_participation"] = {}
        for key in (
            "flow_oi_alignment",
            "flow_taker_state",
            "flow_participation_state",
            "flow_trend",
            "oi_flow_state",
            "open_interest_change_pct",
            "taker_buy_pct",
            "cvd",
            "volume_ratio_core",
            "volume_ratio_15m",
            "price_change_core_pct",
            "price_change_15m_pct",
        ):
            item["market_metrics"].pop(key, None)

        result = build_decision_context(item)
        continuation = result["continuation_confirmation"]

        self.assertEqual(continuation["key"], "UNKNOWN")
        self.assertIsNone(continuation["score"])
        self.assertIn("OI 方向資料", continuation["missing"])
        self.assertIn("Taker／CVD 方向資料", continuation["missing"])
        self.assertIn("K 線量能資料", continuation["missing"])
        self.assertEqual(result["final"]["status"], "ENTER")

    def test_short_direction_uses_directional_volume_and_taker(self):
        item = complete_signal()
        item["direction"] = "SHORT"
        item["supporting_evidence"] = ["15m 結構轉空"]
        item["market_participation"] = {"state": "NEUTRAL"}
        item["market_story"]["raw"]["core_return_pct"] = -0.4
        item["market_metrics"].update(
            {
                "flow_oi_alignment": "SAME_DIRECTION_BUILD",
                "flow_taker_state": "STRENGTHENING",
                "flow_participation_state": "STRENGTHENING",
                "flow_valid_sample_count": 3,
                "price_change_core_pct": -0.4,
                "volume_ratio_core": 1.5,
                "taker_buy_pct": 30.0,
            }
        )

        result = build_decision_context(item)

        self.assertEqual(result["continuation_confirmation"]["key"], "CONFIRMED")
        self.assertEqual(result["final"]["direction"], "SHORT")
        self.assertEqual(result["final"]["status"], "ENTER")

    def test_aggregate_flow_cannot_clear_missing_directional_history(self):
        item = complete_signal()
        item["supporting_evidence"] = ["15m 結構轉多"]
        item["market_participation"] = {"state": "NEUTRAL"}
        item["market_metrics"].update(
            {
                "flow_oi_alignment": "UNKNOWN",
                "flow_taker_state": "UNKNOWN",
                # This can be driven by Funding / depth / book and is not a vote.
                "flow_participation_state": "STRENGTHENING",
                "flow_valid_sample_count": 3,
                "price_change_core_pct": 0.4,
                "taker_buy_pct": 68.0,
                "volume_ratio_core": 1.5,
            }
        )

        result = build_decision_context(item)
        continuation = result["continuation_confirmation"]

        self.assertEqual(continuation["key"], "FORMING")
        self.assertIn("連續資金流歷史（至少 3 筆）", continuation["missing"])
        self.assertEqual(result["final"]["status"], "ENTER")

    def test_no_price_trigger_can_never_have_confirmed_follow_through(self):
        item = complete_signal()
        item["signal_stage"] = "NEAR_TRIGGER"
        item["lifecycle"]["current_stage"] = "NEAR_TRIGGER"
        item["market_story"]["trigger"] = {
            "triggered": False,
            "type": "NONE",
        }
        item["market_metrics"].update(
            {
                "flow_oi_alignment": "SAME_DIRECTION_BUILD",
                "flow_taker_state": "STRENGTHENING",
                "flow_participation_state": "STRENGTHENING",
                "flow_valid_sample_count": 3,
                "price_change_core_pct": 0.4,
                "volume_ratio_core": 1.5,
            }
        )

        result = build_decision_context(item)

        self.assertEqual(result["continuation_confirmation"]["key"], "UNKNOWN")
        self.assertIn(
            "有效中的正式價格 Trigger",
            result["continuation_confirmation"]["missing"],
        )
        self.assertEqual(result["final"]["status"], "WAIT")

    def test_preserved_active_episode_can_still_confirm_follow_through(self):
        item = complete_signal()
        item["market_story"].update(
            {
                "raw": {"core_return_pct": 0.4},
                "trigger": {
                    "triggered": False,
                    "type": "ACTIVE_EPISODE",
                    "active_episode_preserved": True,
                },
            }
        )
        item["market_metrics"].update(
            {
                "flow_oi_alignment": "SAME_DIRECTION_BUILD",
                "flow_taker_state": "STRENGTHENING",
                "flow_participation_state": "STRENGTHENING",
                "flow_valid_sample_count": 3,
                "taker_buy_pct": 65.0,
                "volume_ratio_core": 0.9,
            }
        )

        result = build_decision_context(item)

        self.assertEqual(result["continuation_confirmation"]["key"], "CONFIRMED")
        self.assertEqual(result["final"]["status"], "ENTER")

    def test_closed_candle_return_wins_over_live_price_for_volume_vote(self):
        item = complete_signal()
        item["supporting_evidence"] = ["15m 結構轉多"]
        item["market_participation"] = {"state": "NEUTRAL"}
        item["market_story"]["raw"] = {"core_return_pct": -0.3}
        item["market_metrics"].update(
            {
                "flow_oi_alignment": "STABLE",
                "flow_taker_state": "STABLE",
                "flow_participation_state": "STABLE",
                "flow_valid_sample_count": 3,
                "price_change_core_pct": 0.4,
                "volume_ratio_core": 1.5,
            }
        )

        result = build_decision_context(item)
        continuation = result["continuation_confirmation"]

        self.assertNotEqual(continuation["key"], "CONFIRMED")
        self.assertTrue(any("價格反向" in text for text in continuation["conflicts"]))

    def test_taker_buy_pct_is_always_percentage_not_ambiguous_ratio(self):
        item = complete_signal()
        item["supporting_evidence"] = ["15m 結構轉多"]
        item["market_participation"] = {"state": "NEUTRAL"}
        item["market_metrics"].update(
            {
                "flow_oi_alignment": "STABLE",
                "flow_taker_state": "STABLE",
                "flow_participation_state": "STABLE",
                "flow_valid_sample_count": 3,
                "price_change_core_pct": 0.4,
                "taker_buy_pct": 1.0,
                "volume_ratio_core": 0.8,
            }
        )

        continuation = build_decision_context(item)["continuation_confirmation"]

        self.assertFalse(any("Taker" in text for text in continuation["supporting"]))
        self.assertEqual(continuation["key"], "UNKNOWN")

    def test_explicit_unknown_flow_does_not_trust_stale_prose(self):
        item = complete_signal()
        item["supporting_evidence"] = ["15m 結構轉多"]
        item["market_participation"] = {
            "state": "SUPPORT",
            "supporting": [
                "價格與持倉量同向增加，顯示新增部位參與",
                "主動成交與價格成果同向",
            ],
        }
        item["market_metrics"].update(
            {
                "flow_oi_alignment": "UNKNOWN",
                "flow_taker_state": "UNKNOWN",
                "flow_participation_state": "UNKNOWN",
                "flow_valid_sample_count": 2,
                "price_change_core_pct": 0.3,
                "volume_ratio_core": 0.8,
            }
        )

        continuation = build_decision_context(item)["continuation_confirmation"]

        self.assertFalse(any("持倉量同向" in text for text in continuation["supporting"]))
        self.assertFalse(any("主動成交" in text for text in continuation["supporting"]))
        self.assertEqual(continuation["key"], "UNKNOWN")

    def test_strengthening_taker_history_needs_current_directional_dominance(self):
        item = complete_signal()
        item["market_metrics"].update(
            {
                "flow_oi_alignment": "SAME_DIRECTION_BUILD",
                "flow_taker_state": "STRENGTHENING",
                "flow_participation_state": "STRENGTHENING",
                "flow_valid_sample_count": 3,
                # LONG taker share may have risen from 20% to 30%, but sellers
                # still dominate.  STRENGTHENING alone is not a direction vote.
                "taker_buy_pct": 30.0,
                "volume_ratio_core": 0.9,
            }
        )

        continuation = build_decision_context(item)["continuation_confirmation"]

        self.assertEqual(continuation["key"], "FORMING")
        self.assertFalse(any("Taker 主動成交與價格成果同向" in text for text in continuation["supporting"]))

    def test_price_rejection_prevents_high_confirmation(self):
        item = complete_signal()
        item["market_story"]["price_acceptance"] = {
            "state": "REJECTED",
            "label": "突破區外價格遭拒絕",
        }
        item["market_metrics"].update(
            {
                "flow_oi_alignment": "SAME_DIRECTION_BUILD",
                "flow_taker_state": "STRENGTHENING",
                "flow_participation_state": "STRENGTHENING",
                "flow_valid_sample_count": 3,
                "taker_buy_pct": 65.0,
                "volume_ratio_core": 1.5,
            }
        )

        result = build_decision_context(item)

        self.assertEqual(result["continuation_confirmation"]["key"], "FORMING")
        self.assertEqual(result["final"]["status"], "ENTER")

    def test_ma_macd_counterevidence_prevents_high_confirmation(self):
        item = complete_signal()
        item["market_story"]["trigger"]["momentum_confirmation"] = {
            "confirmed": False,
            "partial": False,
            "label": "MA／MACD 尚未同向呼應",
        }
        item["market_metrics"].update(
            {
                "flow_oi_alignment": "SAME_DIRECTION_BUILD",
                "flow_taker_state": "STRENGTHENING",
                "flow_participation_state": "STRENGTHENING",
                "flow_valid_sample_count": 3,
                "taker_buy_pct": 65.0,
                "volume_ratio_core": 1.5,
            }
        )

        result = build_decision_context(item)

        self.assertEqual(result["continuation_confirmation"]["key"], "FORMING")
        self.assertEqual(result["final"]["status"], "ENTER")

    def test_trigger_without_a_trade_plan_has_unknown_confirmation(self):
        item = complete_signal()
        for key in ("entry_low", "entry_high", "stop_loss", "take_profit_1"):
            item.pop(key)
        item["market_metrics"].update(
            {
                "flow_oi_alignment": "SAME_DIRECTION_BUILD",
                "flow_taker_state": "STRENGTHENING",
                "flow_participation_state": "STRENGTHENING",
                "flow_valid_sample_count": 3,
                "taker_buy_pct": 65.0,
                "volume_ratio_core": 1.5,
            }
        )

        continuation = build_decision_context(item)["continuation_confirmation"]

        self.assertEqual(continuation["key"], "UNKNOWN")
        self.assertIn("有效中的正式價格 Trigger", continuation["missing"])

    def test_long_horizon_core_4h_volume_conflict_is_not_context_warning(self):
        item = complete_signal()
        item["radar_horizon"] = "LONG"
        item["supporting_evidence"] = ["4H 結構轉多"]
        item["conflicts"] = []
        item["market_participation"] = {"state": "NEUTRAL"}
        item["market_story"]["raw"] = {"core_return_pct": -0.4}
        item["market_metrics"].update(
            {
                "flow_oi_alignment": "STABLE",
                "flow_taker_state": "STABLE",
                "flow_participation_state": "STABLE",
                "flow_valid_sample_count": 3,
                "volume_ratio_core": 1.5,
            }
        )

        continuation = build_decision_context(item)["continuation_confirmation"]

        self.assertTrue(any("K 線量能" in text for text in continuation["conflicts"]))
        self.assertFalse(any("K 線量能" in text for text in continuation["warnings"]))

    def test_short_horizon_does_not_count_noncore_volume_text_as_core_vote(self):
        for warning in (
            "4H 成交量放大但價格反向",
            "5m 成交量放大但價格反向",
        ):
            with self.subTest(warning=warning):
                item = complete_signal()
                item["conflicts"] = [warning]
                item["market_participation"] = {"state": "NEUTRAL"}
                item["market_metrics"].update(
                    {
                        "flow_oi_alignment": "STABLE",
                        "flow_taker_state": "STABLE",
                        "flow_participation_state": "STABLE",
                        "flow_valid_sample_count": 3,
                    }
                )
                item["market_metrics"].pop("volume_ratio_core", None)
                item["market_metrics"].pop("volume_ratio_15m", None)

                continuation = build_decision_context(item)[
                    "continuation_confirmation"
                ]

                self.assertNotIn(warning, continuation["conflicts"])
                self.assertIn(warning, continuation["warnings"])

        item = complete_signal()
        item["supporting_evidence"] = ["5m 成交量放大且價格同向"]
        item["market_participation"] = {"state": "NEUTRAL"}
        item["market_metrics"].update(
            {
                "flow_oi_alignment": "SAME_DIRECTION_BUILD",
                "flow_taker_state": "STABLE",
                "flow_participation_state": "STABLE",
                "flow_valid_sample_count": 3,
            }
        )
        item["market_metrics"].pop("volume_ratio_core", None)
        item["market_metrics"].pop("volume_ratio_15m", None)

        continuation = build_decision_context(item)["continuation_confirmation"]

        self.assertEqual(continuation["key"], "FORMING")
        self.assertNotIn(
            "5m 成交量放大且價格同向",
            continuation["supporting"],
        )

    def test_short_horizon_keeps_noncore_oi_and_cvd_prose_warning_only(self):
        for warning in (
            "4H OI opposite build",
            "5m CVD 同向但價格未跟進",
        ):
            with self.subTest(warning=warning):
                item = complete_signal()
                item["supporting_evidence"] = ["15m 結構轉多"]
                item["conflicts"] = [warning]
                item["market_participation"] = {"state": "NEUTRAL"}
                for key in (
                    "flow_oi_alignment",
                    "flow_taker_state",
                    "open_interest_change_pct",
                    "taker_buy_pct",
                    "cvd",
                    "volume_ratio_core",
                    "volume_ratio_15m",
                ):
                    item["market_metrics"].pop(key, None)

                continuation = build_decision_context(item)[
                    "continuation_confirmation"
                ]

                self.assertNotIn(warning, continuation["conflicts"])
                self.assertIn(warning, continuation["warnings"])

    def test_public_projection_exposes_only_continuation_summary(self):
        item = complete_signal()
        item["market_metrics"].update(
            {
                "flow_oi_alignment": "SAME_DIRECTION_BUILD",
                "flow_taker_state": "STRENGTHENING",
                "flow_participation_state": "STRENGTHENING",
                "flow_valid_sample_count": 3,
                "price_change_core_pct": 0.4,
                "taker_buy_pct": 65.0,
                "volume_ratio_core": 0.9,
            }
        )
        item["decision_context"] = build_decision_context(item)
        self.assertEqual(
            item["decision_context"]["continuation_confirmation"]["key"],
            "CONFIRMED",
        )

        public = public_candidate_payload(item, signal=True)
        continuation = public["decision_context"]["continuation_confirmation"]

        self.assertEqual(continuation["key"], "UNKNOWN")
        self.assertIn("score", continuation)
        self.assertIn("supporting", continuation)
        self.assertIn("conflicts", continuation)
        self.assertIn("missing", continuation)
        self.assertIn("meaning", continuation)
        self.assertEqual(
            set(continuation["core_votes"]),
            {"OI", "TAKER_CVD", "VOLUME"},
        )
        self.assertEqual(
            continuation["core_votes"]["OI"]["state"],
            "UNKNOWN",
        )
        self.assertEqual(
            set(continuation["core_votes"]["OI"]),
            {"state", "label", "detail"},
        )
        self.assertNotIn("severe", continuation["core_votes"]["OI"])
        self.assertNotIn("flow_oi_alignment", public["market_metrics"])

    def test_fixed_average_observer_replaces_legacy_snapshot_votes(self):
        item = complete_signal()
        item["market_metrics"].update(
            {
                # These old scan-snapshot fields would otherwise confirm all
                # three domains. Once the fixed observer exists, they must not
                # masquerade as the requested 5/10m average.
                "flow_oi_alignment": "SAME_DIRECTION_BUILD",
                "flow_taker_state": "STRENGTHENING",
                "flow_valid_sample_count": 3,
                "taker_buy_pct": 70.0,
                "volume_ratio_core": 1.5,
                "continuation_observer": {
                    "algorithm_version": "CONTINUATION_AVG_V2",
                    "status": "COLLECTING",
                    "bucket_count": 2,
                    "target_buckets": 10,
                    "early_window": "5m",
                    "primary_window": "10m",
                    "selected_window": "5m",
                    "selected": {
                        "ready": False,
                        "domains": {},
                    },
                    "windows": {
                        "5m": {"ready": False},
                        "10m": {"ready": False},
                    },
                },
            }
        )

        result = build_decision_context(item)
        continuation = result["continuation_confirmation"]

        self.assertEqual(continuation["key"], "UNKNOWN")
        self.assertTrue(
            all(
                vote["state"] == "UNKNOWN"
                for vote in continuation["core_votes"].values()
            )
        )
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_fast_average_can_only_form_until_base_window_is_ready(self):
        item = complete_signal()
        support_domains = {
            key: {
                "state": "SUPPORT",
                "reason": f"{key} 多筆平均同向",
                "severe": False,
            }
            for key in ("OI", "TAKER_CVD", "VOLUME")
        }
        observer = {
            "algorithm_version": "CONTINUATION_AVG_V2",
            "status": "EARLY_READY",
            "bucket_count": 5,
            "target_buckets": 10,
            "early_window": "5m",
            "primary_window": "10m",
            "selected_window": "5m",
            "selected": {"ready": True, "domains": support_domains},
            "windows": {
                "5m": {"ready": True},
                "10m": {"ready": False},
            },
        }
        item["market_metrics"]["continuation_observer"] = observer

        early = build_decision_context(item)
        self.assertEqual(early["continuation_confirmation"]["key"], "FORMING")
        self.assertEqual(early["final"]["status"], "ENTER")

        observer["status"] = "READY"
        observer["bucket_count"] = 10
        observer["selected_window"] = "10m"
        observer["selected"] = {"ready": True, "domains": support_domains}
        observer["windows"]["10m"] = {"ready": True}
        completed = build_decision_context(item)

        self.assertEqual(
            completed["continuation_confirmation"]["key"],
            "CONFIRMED",
        )
        self.assertEqual(completed["final"]["status"], "ENTER")
        item["decision_context"] = completed
        self.assertEqual(
            public_candidate_payload(item, signal=True)["decision_context"]
            ["continuation_confirmation"]["key"],
            "CONFIRMED",
        )

    def test_base_window_requires_oi_support_for_top_confirmation(self):
        item = complete_signal()
        domains = {
            "OI": {"state": "UNKNOWN", "missing": "OI 平均樣本"},
            "TAKER_CVD": {"state": "SUPPORT", "reason": "主動成交平均同向"},
            "VOLUME": {"state": "SUPPORT", "reason": "平均量價同向"},
        }
        item["market_metrics"]["continuation_observer"] = {
            "algorithm_version": "CONTINUATION_AVG_V2",
            "status": "PARTIAL",
            "bucket_count": 10,
            "target_buckets": 10,
            "early_window": "5m",
            "primary_window": "10m",
            "selected_window": "10m",
            "selected": {"ready": True, "domains": domains},
            "windows": {"5m": {"ready": True}, "10m": {"ready": True}},
        }

        result = build_decision_context(item)

        self.assertEqual(result["continuation_confirmation"]["key"], "FORMING")
        self.assertEqual(result["final"]["status"], "ENTER")

    def test_recent_fast_window_conflict_downgrades_aligned_base_window(self):
        item = complete_signal()
        support_domains = {
            key: {"state": "SUPPORT", "reason": f"{key} 基準窗同向"}
            for key in ("OI", "TAKER_CVD", "VOLUME")
        }
        item["market_metrics"]["continuation_observer"] = {
            "algorithm_version": "CONTINUATION_AVG_V2",
            "status": "READY",
            "bucket_count": 10,
            "target_buckets": 10,
            "early_window": "5m",
            "primary_window": "10m",
            "selected_window": "10m",
            "selected": {"ready": True, "domains": support_domains},
            "windows": {
                "5m": {"ready": True, "state": "CONFLICT"},
                "10m": {"ready": True, "state": "ALIGNED"},
            },
        }

        result = build_decision_context(item)

        continuation = result["continuation_confirmation"]
        self.assertEqual(continuation["key"], "FORMING")
        self.assertTrue(any("快速平均窗" in item for item in continuation["warnings"]))
        self.assertEqual(result["final"]["status"], "ENTER")

    def test_public_observer_projection_never_exposes_raw_samples(self):
        item = complete_signal()
        item["market_metrics"]["continuation_observer"] = {
            "algorithm_version": "CONTINUATION_AVG_V2",
            "status": "COLLECTING",
            "cadence_label": "每 1 分鐘採樣",
            "bucket_count": 3,
            "target_buckets": 10,
            "early_window": "5m",
            "primary_window": "10m",
            "selected_window": "5m",
            "selected": {"ready": False, "domains": {}},
            "windows": {
                "5m": {
                    "key": "5m",
                    "ready": False,
                    "bucket_count": 3,
                    "required_buckets": 5,
                    "state": "COLLECTING",
                }
            },
            "samples": [{"tradeId": "must-not-leak"}],
        }
        item["decision_context"] = build_decision_context(item)

        public = public_candidate_payload(item, signal=True)
        observer = public["decision_context"]["continuation_confirmation"]["observer"]

        self.assertEqual(observer["bucket_count"], 3)
        self.assertNotIn("samples", observer)
        self.assertNotIn("selected", observer)
        self.assertNotIn("continuation_observer", public["market_metrics"])

    def test_public_projection_hides_stale_observer_algorithm(self):
        item = complete_signal()
        item["market_metrics"]["continuation_observer"] = {
            "algorithm_version": "CONTINUATION_AVG_V1",
            "status": "READY",
            "selected_window": "10m",
            "selected": {
                "ready": True,
                "domains": {
                    key: {"state": "SUPPORT"}
                    for key in ("OI", "TAKER_CVD", "VOLUME")
                },
            },
        }
        item["decision_context"] = build_decision_context(item)

        public = public_candidate_payload(item, signal=True)
        continuation = public["decision_context"]["continuation_confirmation"]

        self.assertEqual(continuation["key"], "UNKNOWN")
        self.assertEqual(continuation["observer"], {})

    def test_market_state_without_plan_is_no_edge_not_a_fake_entry(self):
        item = complete_signal()
        for key in (
            "entry_low",
            "entry_high",
            "stop_loss",
            "take_profit_1",
            "take_profit_2",
            "risk_reward",
            "entry_eligibility",
        ):
            item.pop(key, None)
        item["market_story"] = {"trigger": {"triggered": False, "type": "NONE"}}
        item["signal_stage"] = "NEAR_TRIGGER"
        item["lifecycle"] = {"current_stage": "NEAR_TRIGGER"}

        result = build_decision_context(item)

        self.assertEqual(result["final"]["status"], "WAIT")
        self.assertFalse(result["final"]["new_entry_allowed"])
        self.assertIn("trade_plan", result["hard_gate"]["blockers"])

    def test_watch_state_without_plan_is_no_edge(self):
        item = complete_signal()
        for key in (
            "entry_low",
            "entry_high",
            "stop_loss",
            "take_profit_1",
            "take_profit_2",
            "risk_reward",
            "entry_eligibility",
        ):
            item.pop(key, None)
        item["market_story"] = {"trigger": {"triggered": False, "type": "NONE"}}
        item["signal_stage"] = "WATCH"
        item["lifecycle"] = {"current_stage": "WATCH"}

        result = build_decision_context(item)

        self.assertEqual(result["final"]["status"], "NO_EDGE")
        self.assertFalse(result["final"]["new_entry_allowed"])


if __name__ == "__main__":
    unittest.main()
