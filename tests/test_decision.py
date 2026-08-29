import copy
import unittest

from radar.decision import build_decision_context


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
        "market_story": {"trigger": {"triggered": True, "type": "BREAKOUT"}},
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

    def test_partial_data_is_unknown_and_never_enters(self):
        item = complete_signal()
        item["data_quality"] = {
            "core": "AVAILABLE",
            "deep": "PARTIAL",
            "missing_sources": ["order_book"],
        }

        result = build_decision_context(item)

        self.assertEqual(result["hard_gate"]["status"], "UNKNOWN")
        self.assertIn("data_quality", result["hard_gate"]["unknowns"])
        self.assertEqual(result["final"]["status"], "DATA_UNAVAILABLE")
        self.assertFalse(result["final"]["new_entry_allowed"])

    def test_missing_execution_numbers_fail_closed(self):
        item = complete_signal()
        del item["market_metrics"]["buy_slippage_pct"]
        del item["market_metrics"]["execution_cost_to_risk_pct"]
        item["execution_quality"].pop("execution_cost_to_risk_pct")

        result = build_decision_context(item)

        self.assertEqual(result["hard_gate"]["status"], "UNKNOWN")
        self.assertIn("slippage", result["hard_gate"]["unknowns"])
        self.assertIn("execution_cost", result["hard_gate"]["unknowns"])
        self.assertEqual(result["final"]["status"], "DATA_UNAVAILABLE")

    def test_failed_existing_hard_check_blocks_even_when_entry_is_ready(self):
        item = complete_signal()
        item["safety_checks"].append(
            {"key": "api_data", "passed": False, "hard": True, "label": "API 失敗"}
        )

        result = build_decision_context(item)

        self.assertEqual(result["hard_gate"]["status"], "BLOCKED")
        self.assertEqual(result["final"]["status"], "WAIT")
        self.assertFalse(result["final"]["new_entry_allowed"])

    def test_slippage_uses_direct_threshold_not_quality_score(self):
        item = complete_signal()
        item["market_metrics"]["buy_slippage_pct"] = 0.20
        item["execution_quality"]["score"] = 95

        result = build_decision_context(item)

        self.assertIn("slippage", result["hard_gate"]["blockers"])
        self.assertEqual(result["final"]["status"], "WAIT")
        self.assertFalse(result["final"]["new_entry_allowed"])

    def test_thresholds_parameter_changes_limit_without_changing_priority(self):
        item = complete_signal()
        item["spread_pct"] = 0.08

        normal = build_decision_context(item)
        strict = build_decision_context(item, {"max_spread_pct": 0.05})

        self.assertEqual(normal["final"]["status"], "ENTER")
        self.assertIn("spread", strict["hard_gate"]["blockers"])
        self.assertFalse(strict["final"]["new_entry_allowed"])

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

    def test_missed_entry_with_real_execution_blocker_keeps_gate_priority(self):
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
        self.assertEqual(result["final"]["status"], "WAIT")
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

    def test_true_live_severe_chase_reports_source_value_and_blocks(self):
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
        self.assertEqual(result["final"]["status"], "NO_CHASE")
        self.assertFalse(result["final"]["new_entry_allowed"])
        self.assertIn("2.10 ATR", result["final"]["reasons"][0])
        self.assertNotIn("仍在最佳進場", result["final"]["reasons"][0])

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
        self.assertEqual(result["final"]["status"], "NO_CHASE")

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
        self.assertEqual(result["final"]["status"], "DATA_UNAVAILABLE")
        self.assertFalse(result["final"]["new_entry_allowed"])

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

    def test_anomaly_blocks_an_otherwise_valid_signal(self):
        item = complete_signal()
        item["market_metrics"].update(
            {"anomaly_state": "LIQUIDITY_WITHDRAWAL", "anomaly_label": "深度突然消失"}
        )

        result = build_decision_context(item)

        self.assertEqual(result["final"]["status"], "ANOMALY")
        self.assertFalse(result["final"]["new_entry_allowed"])
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
        self.assertNotEqual(result["final"]["status"], "ENTER")
        self.assertFalse(result["final"]["new_entry_allowed"])

    def test_entry_ready_with_explicit_permission_false_fails_closed(self):
        item = complete_signal()
        item["entry_eligibility"]["new_entry_allowed"] = False

        result = build_decision_context(item)

        self.assertIn("entry_permission", result["hard_gate"]["blockers"])
        self.assertEqual(result["hard_gate"]["status"], "BLOCKED")
        self.assertEqual(result["final"]["status"], "WAIT")
        self.assertFalse(result["final"]["new_entry_allowed"])

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

    def test_low_rr_is_no_edge_instead_of_moving_stop(self):
        item = complete_signal()
        item["risk_reward"] = 1.1
        item["entry_eligibility"]["remaining_rr"] = 1.1

        result = build_decision_context(item)

        self.assertEqual(result["final"]["status"], "NO_EDGE")
        self.assertIn("risk_reward", result["hard_gate"]["blockers"])
        self.assertFalse(result["final"]["new_entry_allowed"])

    def test_high_conflict_waits_but_never_creates_opposite_direction(self):
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
        self.assertEqual(result["final"]["status"], "WAIT")
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
