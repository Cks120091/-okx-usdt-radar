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


def volume_policy(
    *,
    member: bool,
    volume: float | None,
    trusted: bool = True,
    status: str = "AVAILABLE",
) -> dict:
    return {
        "version": 1,
        "trusted": trusted,
        "member": member,
        "entry_usdt": 2_000_000.0,
        "exit_usdt": 1_500_000.0,
        "effective_min_usdt": 1_500_000.0 if member else 2_000_000.0,
        "volume_usdt": volume,
        "volume_status": status,
        "source": "PUBLICATION_TICKER",
    }


def fixed_continuation_summary(
    *,
    algorithm_version="CONTINUATION_LOOKBACK_V1",
    status="READY",
    as_of_close_ms=1_000,
    updated_at_ms=1_000,
    domains=None,
):
    if domains is None:
        domains = {
            key: {"state": "SUPPORT", "reason": f"{key} 多筆平均同向"}
            for key in ("OI", "TAKER_CVD", "VOLUME")
        }
    source_mode = (
        "HISTORICAL_CLOSED_BARS"
        if algorithm_version == "CONTINUATION_LOOKBACK_V1"
        else "POST_SIGNAL_OBSERVER"
    )
    primary = {
        "key": "10m",
        "ready": True,
        "state": "ALIGNED",
        "bucket_count": 2,
        "required_buckets": 2,
        "domains": domains,
    }
    return {
        "algorithm_version": algorithm_version,
        "source_mode": source_mode,
        "status": status,
        "bucket_count": 2,
        "target_buckets": 2,
        "early_window": "5m",
        "primary_window": "10m",
        "selected_window": "10m",
        "selected": primary,
        "windows": {
            "5m": {"key": "5m", "ready": True, "state": "ALIGNED"},
            "10m": primary,
        },
        "as_of_close_ms": as_of_close_ms,
        "updated_at_ms": updated_at_ms,
    }


def fixed_capital_flow_summary(
    *,
    algorithm_version="CAPITAL_FLOW_LOOKBACK_V1",
):
    windows = {}
    required_samples = {"1h": 8, "2h": 15, "4h": 29}
    for key, hours, change_pct, ratio in (
        ("1h", 1, 3.2, 2.4),
        ("2h", 2, 5.1, 2.1),
        ("4h", 4, 8.6, 1.8),
    ):
        windows[key] = {
            "key": key,
            "hours": hours,
            "ready": True,
            "sample_count": required_samples[key],
            "required_sample_count": required_samples[key],
            "baseline_window_count": 6,
            "state": "LARGE_LONG",
            "label": f"{key} 推定大量偏多資金流入",
            "as_of_close_ms": 1_234_000,
            "latest_value": 108_600_000.0,
            "window_start_value": 100_000_000.0,
            "change_amount": 8_600_000.0,
            "change_pct": change_pct,
            "baseline_average_change_pct": 2.0,
            "change_vs_average_ratio": ratio,
            "above_average": True,
            "large_inflow": True,
            "persistence_pct": 83.3,
            "unit": "CONTRACTS",
            "directional_bias": "LONG",
            "directional_bias_label": "推定偏多新增參與",
            "price_return_pct": 2.8,
            "price_consistency_pct": 75.0,
            "raw_points": [{"oi": "must-not-leak"}],
            "samples": [{"oi": "must-not-leak"}],
            "reason": "internal classification detail",
        }
    return {
        "algorithm_version": algorithm_version,
        "source_mode": "HISTORICAL_CLOSED_1H_AT_SCAN",
        "status": "READY",
        "sample_count": 29,
        "required_sample_count": 29,
        "baseline_window_count": 6,
        "as_of_close_ms": 1_234_000,
        "continuity_reset": False,
        "detected": True,
        "strongest_window": "4h",
        "headline_state": "LARGE_LONG",
        "headline_direction": "LONG",
        "headline_label": "發現相對異常增倉，價格推定偏多主導",
        "direction_basis": "SAME_WINDOW_PRICE_ACTION_INFERENCE",
        "long_short_split_available": False,
        "minimum_change_pct": 0.5,
        "large_ratio_threshold": 1.5,
        "persistence_threshold_pct": 60.0,
        "meaning": "比較最近 1h/2h/4h OI 變化與歷史平均。",
        "permission": "ADVISORY_ONLY_NEVER_CHANGES_TRIGGER_OR_PLAN",
        "windows": windows,
        "samples": [{"oi": "must-not-leak"}],
        "raw_points": [{"oi": "must-not-leak"}],
        "reason": "internal headline reason",
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

    def test_exact_hard_gate_numeric_limits_still_allow_entry(self):
        item = complete_signal()
        item["quote_volume_24h"] = 2_000_000.0
        item["spread_pct"] = 0.10
        item["market_metrics"].update(
            {
                "buy_slippage_pct": 0.15,
                "sell_slippage_pct": 0.15,
                "execution_cost_to_risk_pct": 15.0,
                "technical_stop_pct": 5.0,
            }
        )
        item["execution_quality"]["execution_cost_to_risk_pct"] = 15.0
        item["risk_reward"] = 1.8
        item["entry_eligibility"].update(
            {"remaining_rr": 1.8, "chase_atr": 1.8}
        )

        result = build_decision_context(item)

        self.assertEqual(result["hard_gate"]["status"], "PASSED")
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_volume_hysteresis_uses_exact_member_and_nonmember_boundaries(self):
        cases = (
            (False, 1_999_999.0, False, 2_000_000.0),
            (False, 2_000_000.0, True, 2_000_000.0),
            (True, 1_499_999.0, False, 1_500_000.0),
            (True, 1_500_000.0, True, 1_500_000.0),
        )
        for member, volume, allowed, effective_min in cases:
            with self.subTest(member=member, volume=volume):
                item = complete_signal()
                item["quote_volume_24h"] = volume
                item["data_quality"]["universe_volume_policy"] = volume_policy(
                    member=member,
                    volume=volume,
                )

                result = build_decision_context(item)

                self.assertEqual(
                    result["hard_gate"]["liquidity_policy"]["member"],
                    member,
                )
                self.assertEqual(
                    result["hard_gate"]["thresholds"]["min_quote_volume_24h"],
                    effective_min,
                )
                self.assertEqual(result["final"]["new_entry_allowed"], allowed)
                self.assertEqual(
                    "liquidity" in result["hard_gate"]["blockers"],
                    not allowed,
                )

    def test_untrusted_member_policy_cannot_lower_new_symbol_entry_line(self):
        item = complete_signal()
        item["quote_volume_24h"] = 1_500_000.0
        item["data_quality"]["universe_volume_policy"] = volume_policy(
            member=True,
            volume=1_500_000.0,
            trusted=False,
        )

        result = build_decision_context(item)

        self.assertFalse(result["hard_gate"]["liquidity_policy"]["member"])
        self.assertEqual(
            result["hard_gate"]["thresholds"]["min_quote_volume_24h"],
            2_000_000.0,
        )
        self.assertIn("liquidity", result["hard_gate"]["blockers"])
        self.assertFalse(result["final"]["new_entry_allowed"])

    def test_unavailable_publication_volume_never_uses_candidate_fallback(self):
        item = complete_signal()
        item["quote_volume_24h"] = 99_000_000.0
        item["data_quality"]["universe_volume_policy"] = volume_policy(
            member=True,
            volume=None,
            status="UNAVAILABLE",
        )

        result = build_decision_context(item)

        self.assertIn("liquidity", result["hard_gate"]["unknowns"])
        self.assertEqual(result["final"]["status"], "DATA_UNAVAILABLE")
        self.assertFalse(result["final"]["new_entry_allowed"])

    def test_epsilon_beyond_each_hard_gate_limit_vetoes_entry(self):
        cases = {
            "liquidity": lambda item: item.update(
                {"quote_volume_24h": 1_999_999.99}
            ),
            "spread": lambda item: item.update({"spread_pct": 0.100001}),
            "slippage": lambda item: item["market_metrics"].update(
                {"buy_slippage_pct": 0.150001}
            ),
            "execution_cost": lambda item: (
                item["market_metrics"].update(
                    {"execution_cost_to_risk_pct": 15.0001}
                ),
                item["execution_quality"].update(
                    {"execution_cost_to_risk_pct": 15.0001}
                ),
            ),
            "risk_reward": lambda item: (
                item.update({"risk_reward": 1.7999}),
                item["entry_eligibility"].update({"remaining_rr": 1.7999}),
            ),
            "stop_loss": lambda item: item["market_metrics"].update(
                {"technical_stop_pct": 5.0001}
            ),
            "chase": lambda item: item["entry_eligibility"].update(
                {"chase_atr": 1.8001}
            ),
        }

        for blocker, mutate in cases.items():
            with self.subTest(blocker=blocker):
                item = complete_signal()
                mutate(item)

                result = build_decision_context(item)

                self.assertIn(blocker, result["hard_gate"]["blockers"])
                self.assertNotEqual(result["final"]["status"], "ENTER")
                self.assertFalse(result["final"]["new_entry_allowed"])

    def test_advisory_upstream_checks_do_not_create_synthetic_unknown_gate(self):
        item = complete_signal()
        item["safety_checks"] = [
            {"key": "core_data", "passed": True, "hard": False},
            {"key": "execution_note", "passed": False, "hard": False},
        ]

        result = build_decision_context(item)

        self.assertEqual(result["hard_gate"]["status"], "PASSED")
        self.assertNotIn("safety_checks", result["hard_gate"]["unknowns"])
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_active_episode_with_formal_opposite_signal_blocks_new_entry(self):
        item = complete_signal()
        item["market_story"]["trigger"].update(
            {
                "triggered": False,
                "direction": "LONG",
                "opposite_warning_only": True,
                "opposite_candidate": {
                    "direction": "SHORT",
                    "type": "BREAKOUT",
                    "confirmation_level": "FULL",
                },
            }
        )

        result = build_decision_context(item)
        opposite = next(
            check
            for check in result["hard_gate"]["checks"]
            if check["key"] == "opposite_signal"
        )

        self.assertEqual(opposite["status"], "BLOCKED")
        self.assertEqual(opposite["value"]["direction"], "SHORT")
        self.assertIn("opposite_signal", result["hard_gate"]["blockers"])
        self.assertEqual(result["final"]["status"], "HARD_GATE_BLOCKED")
        self.assertFalse(result["final"]["new_entry_allowed"])
        self.assertTrue(result["final"]["trigger_preserved"])
        self.assertTrue(any("正式反向訊號" in reason for reason in result["final"]["reasons"]))

    def test_explicit_new_entry_suspension_blocks_without_legacy_warning_flag(self):
        item = complete_signal()
        item["market_story"]["trigger"].update(
            {
                "opposite_warning_only": False,
                "new_entry_suspended": True,
                "opposite_candidate": {
                    "direction": "SHORT",
                    "type": "REVERSAL",
                },
            }
        )

        result = build_decision_context(item)

        self.assertIn("opposite_signal", result["hard_gate"]["blockers"])
        self.assertEqual(result["final"]["status"], "HARD_GATE_BLOCKED")
        self.assertFalse(result["final"]["new_entry_allowed"])

    def test_partial_auxiliary_deep_data_does_not_veto_entry(self):
        item = complete_signal()
        item["data_quality"] = {
            "core": "AVAILABLE",
            "deep": "PARTIAL",
            "missing_sources": ["open_interest", "funding"],
        }

        result = build_decision_context(item)

        self.assertEqual(result["hard_gate"]["status"], "PASSED")
        self.assertNotIn("data_quality", result["hard_gate"]["unknowns"])
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])
        self.assertTrue(result["final"]["trigger_preserved"])
        self.assertFalse(result["hard_gate"]["advisory_only"])
        self.assertTrue(result["hard_gate"]["entry_veto_enabled"])
        self.assertTrue(
            any("輔助資料不完整" in value for value in result["hard_gate"]["warnings"])
        )

    def test_missing_core_or_explicit_required_data_fails_closed(self):
        cases = (
            {"core": "MISSING", "deep": "AVAILABLE", "missing_sources": []},
            {
                "core": "AVAILABLE",
                "deep": "PARTIAL",
                "missing_sources": ["publication_ticker"],
                "required_missing_sources": ["publication_ticker"],
                "publication_ticker_status": "UNAVAILABLE",
            },
        )

        for data_quality in cases:
            with self.subTest(data_quality=data_quality):
                item = complete_signal()
                item["data_quality"] = data_quality

                result = build_decision_context(item)

                self.assertEqual(result["hard_gate"]["status"], "UNKNOWN")
                self.assertIn("data_quality", result["hard_gate"]["unknowns"])
                self.assertEqual(result["final"]["status"], "DATA_UNAVAILABLE")
                self.assertFalse(result["final"]["new_entry_allowed"])

    def test_missing_execution_estimates_are_advisory_only(self):
        item = complete_signal()
        del item["market_metrics"]["buy_slippage_pct"]
        del item["market_metrics"]["execution_cost_to_risk_pct"]
        item["execution_quality"].pop("execution_cost_to_risk_pct")

        result = build_decision_context(item)

        checks = {
            check["key"]: check
            for check in result["hard_gate"]["checks"]
        }
        self.assertEqual(result["hard_gate"]["status"], "PASSED")
        self.assertNotIn("slippage", result["hard_gate"]["unknowns"])
        self.assertNotIn("execution_cost", result["hard_gate"]["unknowns"])
        self.assertEqual(checks["slippage"]["status"], "UNKNOWN")
        self.assertFalse(checks["slippage"]["hard"])
        self.assertEqual(checks["execution_cost"]["status"], "UNKNOWN")
        self.assertFalse(checks["execution_cost"]["hard"])
        self.assertTrue(
            any("不禁止進場" in warning for warning in result["hard_gate"]["warnings"])
        )
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])
        self.assertIsNone(result["final"]["wait_reason"])

    def test_known_high_slippage_blocks_even_when_other_side_is_missing(self):
        item = complete_signal()
        item["market_metrics"]["buy_slippage_pct"] = 0.20
        del item["market_metrics"]["sell_slippage_pct"]

        result = build_decision_context(item)

        self.assertEqual(result["hard_gate"]["status"], "BLOCKED")
        self.assertIn("slippage", result["hard_gate"]["blockers"])
        self.assertEqual(result["final"]["status"], "HARD_GATE_BLOCKED")
        self.assertFalse(result["final"]["new_entry_allowed"])

    def test_failed_legacy_hard_check_vetoes_ready_entry(self):
        item = complete_signal()
        item["safety_checks"].append(
            {"key": "api_data", "passed": False, "hard": True, "label": "API 失敗"}
        )

        result = build_decision_context(item)

        self.assertEqual(result["hard_gate"]["status"], "BLOCKED")
        self.assertEqual(result["final"]["status"], "HARD_GATE_BLOCKED")
        self.assertFalse(result["final"]["new_entry_allowed"])

    def test_slippage_uses_direct_threshold_not_quality_score(self):
        item = complete_signal()
        item["market_metrics"]["buy_slippage_pct"] = 0.20
        item["execution_quality"]["score"] = 95

        result = build_decision_context(item)

        self.assertIn("slippage", result["hard_gate"]["blockers"])
        self.assertEqual(result["final"]["status"], "HARD_GATE_BLOCKED")
        self.assertFalse(result["final"]["new_entry_allowed"])

    def test_thresholds_parameter_changes_limit_without_changing_priority(self):
        item = complete_signal()
        item["spread_pct"] = 0.08

        normal = build_decision_context(item)
        strict = build_decision_context(item, {"max_spread_pct": 0.05})

        self.assertEqual(normal["final"]["status"], "ENTER")
        self.assertIn("spread", strict["hard_gate"]["blockers"])
        self.assertEqual(strict["final"]["status"], "HARD_GATE_BLOCKED")
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

    def test_legacy_ready_status_is_vetoed_by_severe_live_chase(self):
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
        self.assertFalse(result["hard_gate"]["advisory_only"])

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
        self.assertFalse(result["final"]["new_entry_allowed"])

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

    def test_blocking_anomaly_vetoes_an_otherwise_valid_signal(self):
        item = complete_signal()
        item["market_metrics"].update(
            {"anomaly_state": "LIQUIDITY_WITHDRAWAL", "anomaly_label": "深度突然消失"}
        )

        result = build_decision_context(item)

        self.assertEqual(result["final"]["status"], "ANOMALY")
        self.assertFalse(result["final"]["new_entry_allowed"])
        self.assertEqual(result["final"]["wait_reason"]["code"], "MARKET_ANOMALY")
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
        self.assertEqual(result["final"]["status"], "HARD_GATE_BLOCKED")
        self.assertFalse(result["final"]["new_entry_allowed"])

    def test_legacy_permission_false_vetoes_when_position_looks_ready(self):
        item = complete_signal()
        item["entry_eligibility"]["new_entry_allowed"] = False

        result = build_decision_context(item)

        self.assertIn("entry_permission", result["hard_gate"]["blockers"])
        self.assertEqual(result["hard_gate"]["status"], "BLOCKED")
        self.assertEqual(result["final"]["status"], "HARD_GATE_BLOCKED")
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

    def test_low_rr_vetoes_entry_without_moving_stop(self):
        item = complete_signal()
        item["risk_reward"] = 1.1
        item["entry_eligibility"]["remaining_rr"] = 1.1

        result = build_decision_context(item)

        self.assertEqual(result["final"]["status"], "NO_EDGE")
        self.assertIn("risk_reward", result["hard_gate"]["blockers"])
        self.assertFalse(result["final"]["new_entry_allowed"])
        self.assertEqual(result["final"]["wait_reason"]["code"], "RISK_REWARD")

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
        self.assertEqual(result["final"]["status"], "HARD_GATE_BLOCKED")
        self.assertFalse(result["final"]["new_entry_allowed"])

    def test_explicit_strong_higher_timeframe_countertrend_suspends_entry(self):
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
        self.assertTrue(result["conflict"]["blocks_entry"])
        self.assertEqual(
            [row["key"] for row in result["conflict"]["domains"]],
            ["CONTEXT_COUNTERTREND"],
        )
        self.assertEqual(result["final"]["status"], "WAIT")
        self.assertFalse(result["final"]["new_entry_allowed"])
        self.assertEqual(result["confidence"]["key"], "MEDIUM")
        self.assertFalse(result["conflict"]["opposite_signal_created"])

    def test_boundary_cost_does_not_override_strong_countertrend_block(self):
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
        self.assertTrue(result["conflict"]["blocks_entry"])
        self.assertEqual(result["final"]["status"], "WAIT")
        self.assertFalse(result["final"]["new_entry_allowed"])
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

    def test_two_conflicting_evidence_groups_suspend_new_entry(self):
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
        self.assertTrue(result["conflict"]["blocks_entry"])
        self.assertEqual(
            result["conflict"]["blocking_domains"],
            ["POSITION_STRUCTURE", "TREND_MOMENTUM"],
        )
        self.assertEqual(result["conflict"]["level"], "HIGH")
        self.assertEqual(result["final"]["status"], "WAIT")
        self.assertFalse(result["final"]["new_entry_allowed"])
        self.assertEqual(result["final"]["wait_reason"]["code"], "EVIDENCE_CONFLICT")

    def test_countertrend_plus_auxiliary_flow_does_not_create_hidden_veto(self):
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

    def test_spread_hard_gate_takes_priority_over_context_conflict(self):
        item = complete_signal()
        item["conflicts"] = ["4H 背景反向，屬逆勢 Trigger"]
        item["spread_pct"] = 0.2

        result = build_decision_context(item)

        self.assertFalse(result["conflict"]["blocks_entry"])
        self.assertIn("spread", result["hard_gate"]["blockers"])
        self.assertEqual(result["final"]["status"], "HARD_GATE_BLOCKED")
        self.assertFalse(result["final"]["new_entry_allowed"])

    def test_high_auxiliary_conflict_lowers_confidence_without_veto(self):
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
        self.assertEqual(continuation["label"], "續走力道強")
        self.assertNotIn("score", continuation)
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
        self.assertEqual(continuation["label"], "續走力道中等")
        self.assertNotIn("score", continuation)
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
        self.assertEqual(continuation["label"], "續走力道偏弱")
        self.assertNotIn("score", continuation)
        self.assertTrue(any("opposite build" in text for text in continuation["conflicts"]))
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["trigger_preserved"])

    def test_funding_btc_and_ordinary_higher_timeframe_context_stay_auxiliary(self):
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
        self.assertEqual(result["conflict"]["level"], "HIGH")
        self.assertFalse(result["conflict"]["blocks_entry"])
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

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
        self.assertNotIn("score", continuation)
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
        self.assertNotIn("score", continuation)
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

    def test_ready_closed_bar_lookback_beats_collecting_observer(self):
        item = complete_signal()
        item["market_metrics"]["continuation_lookback"] = (
            fixed_continuation_summary(as_of_close_ms=1_000)
        )
        item["market_metrics"]["continuation_observer"] = {
            "algorithm_version": "CONTINUATION_AVG_V2",
            "status": "COLLECTING",
            "bucket_count": 1,
            "target_buckets": 10,
            "early_window": "5m",
            "primary_window": "10m",
            "selected_window": "5m",
            "selected": {"ready": False, "domains": {}},
            "windows": {"5m": {"ready": False}, "10m": {"ready": False}},
            "updated_at_ms": 2_000,
        }

        result = build_decision_context(item)
        continuation = result["continuation_confirmation"]

        self.assertEqual(continuation["key"], "CONFIRMED")
        self.assertEqual(
            continuation["observer"]["algorithm_version"],
            "CONTINUATION_LOOKBACK_V1",
        )
        self.assertEqual(
            continuation["observer"]["source_mode"],
            "HISTORICAL_CLOSED_BARS",
        )
        self.assertEqual(result["final"]["status"], "ENTER")
        self.assertTrue(result["final"]["new_entry_allowed"])

    def test_public_fixed_oi_vote_exposes_delta_without_raw_samples(self):
        item = complete_signal()
        domains = {
            "OI": {
                "state": "SUPPORT",
                "reason": "OI 數量增加並與價格同向",
                "change_amount": 200_000_000.0,
                "change_pct": 200.0,
                "prior_average": 100_000_000.0,
                "latest_value": 300_000_000.0,
                "comparison_points": 2,
                "unit": "CONTRACTS",
                "capital_state": "INCREASING",
                "material_increase": True,
                "persistent_increase": True,
                "directional_bias": "LONG",
                "directional_bias_label": "推定偏多新增參與",
            },
            "TAKER_CVD": {"state": "NEUTRAL", "reason": "主動成交中性"},
            "VOLUME": {"state": "NEUTRAL", "reason": "成交量中性"},
        }
        item["market_metrics"]["continuation_lookback"] = (
            fixed_continuation_summary(domains=domains)
        )
        item["decision_context"] = build_decision_context(item)

        public = public_candidate_payload(item, signal=True)
        oi = public["decision_context"]["continuation_confirmation"]["core_votes"]["OI"]

        self.assertEqual(oi["change_amount"], 200_000_000.0)
        self.assertEqual(oi["change_pct"], 200.0)
        self.assertEqual(oi["directional_bias"], "LONG")
        self.assertNotIn("samples", public["decision_context"]["continuation_confirmation"])

    def test_ready_legacy_observer_cannot_fill_missing_historical_lookback(self):
        item = complete_signal()
        item["market_metrics"]["continuation_observer"] = (
            fixed_continuation_summary(
                algorithm_version="CONTINUATION_AVG_V2",
                as_of_close_ms=2_000,
                updated_at_ms=2_000,
            )
        )

        continuation = build_decision_context(item)["continuation_confirmation"]

        self.assertEqual(continuation["key"], "UNKNOWN")
        self.assertEqual(continuation["label"], "續走力道資料不足")
        self.assertEqual(continuation["observer"], {})

    def test_post_signal_observer_never_replaces_closed_bar_lookback(self):
        conflict_domains = {
            "OI": {
                "state": "CONFLICT",
                "reason": "OI 平均與原方向相反",
                "severe": True,
            },
            "TAKER_CVD": {"state": "CONFLICT", "reason": "主動成交轉向"},
            "VOLUME": {"state": "NEUTRAL", "reason": "量能中性"},
        }
        item = complete_signal()
        item["market_metrics"]["continuation_lookback"] = (
            fixed_continuation_summary(as_of_close_ms=1_000)
        )
        item["market_metrics"]["continuation_observer"] = (
            fixed_continuation_summary(
                algorithm_version="CONTINUATION_AVG_V2",
                as_of_close_ms=2_000,
                updated_at_ms=2_000,
                domains=conflict_domains,
            )
        )

        newer = build_decision_context(item)["continuation_confirmation"]

        self.assertEqual(newer["key"], "CONFIRMED")
        self.assertEqual(
            newer["observer"]["algorithm_version"],
            "CONTINUATION_LOOKBACK_V1",
        )

        item["market_metrics"]["continuation_observer"]["updated_at_ms"] = 900
        older = build_decision_context(item)["continuation_confirmation"]

        self.assertEqual(older["key"], "CONFIRMED")
        self.assertEqual(
            older["observer"]["algorithm_version"],
            "CONTINUATION_LOOKBACK_V1",
        )

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
            "algorithm_version": "CONTINUATION_LOOKBACK_V1",
            "source_mode": "HISTORICAL_CLOSED_BARS",
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
        item["market_metrics"]["continuation_lookback"] = observer

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
        item["market_metrics"]["continuation_lookback"] = {
            "algorithm_version": "CONTINUATION_LOOKBACK_V1",
            "source_mode": "HISTORICAL_CLOSED_BARS",
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

    def test_ready_neutral_history_is_weak_not_data_insufficient(self):
        item = complete_signal()
        domains = {
            "OI": {
                "state": "NEUTRAL",
                "reason": "最新完整 OI 相較前段均值變化不明顯",
            },
            "TAKER_CVD": {
                "state": "UNKNOWN",
                "missing": "Taker／CVD 完整區間樣本",
            },
            "VOLUME": {
                "state": "NEUTRAL",
                "reason": "平均成交量尚未形成持續同向放量",
            },
        }
        item["market_metrics"]["continuation_lookback"] = (
            fixed_continuation_summary(status="PARTIAL", domains=domains)
        )

        result = build_decision_context(item)
        continuation = result["continuation_confirmation"]

        self.assertEqual(continuation["key"], "WEAK")
        self.assertEqual(continuation["label"], "續走力道偏弱")
        self.assertNotIn("score", continuation)
        self.assertNotIn("至少一項同向資金證據", continuation["missing"])
        self.assertTrue(
            any("尚未形成同向支持" in value for value in continuation["warnings"])
        )
        self.assertEqual(result["final"]["status"], "ENTER")

        item["decision_context"] = result
        public = public_candidate_payload(item, signal=True)
        self.assertEqual(
            public["decision_context"]["continuation_confirmation"]["key"],
            "WEAK",
        )

    def test_recent_fast_window_conflict_downgrades_aligned_base_window(self):
        item = complete_signal()
        support_domains = {
            key: {"state": "SUPPORT", "reason": f"{key} 基準窗同向"}
            for key in ("OI", "TAKER_CVD", "VOLUME")
        }
        item["market_metrics"]["continuation_lookback"] = {
            "algorithm_version": "CONTINUATION_LOOKBACK_V1",
            "source_mode": "HISTORICAL_CLOSED_BARS",
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

    def test_public_projection_rejects_legacy_observer_and_raw_samples(self):
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

        self.assertEqual(observer, {})
        self.assertNotIn("samples", observer)
        self.assertNotIn("selected", observer)
        self.assertNotIn("continuation_observer", public["market_metrics"])

    def test_public_projection_accepts_closed_bar_lookback_without_raw_rows(self):
        item = complete_signal()
        lookback = fixed_continuation_summary(as_of_close_ms=1_234_000)
        lookback["samples"] = [{"open_interest_contracts": 99_999}]
        lookback["windows"]["10m"]["raw_points"] = [{"oi": "secret"}]
        item["market_metrics"]["continuation_lookback"] = lookback
        item["decision_context"] = build_decision_context(item)

        public = public_candidate_payload(item, signal=True)
        continuation = public["decision_context"]["continuation_confirmation"]
        projected = continuation["observer"]

        self.assertEqual(continuation["key"], "CONFIRMED")
        self.assertEqual(
            projected["algorithm_version"],
            "CONTINUATION_LOOKBACK_V1",
        )
        self.assertEqual(projected["source_mode"], "HISTORICAL_CLOSED_BARS")
        self.assertEqual(projected["as_of_close_ms"], 1_234_000)
        self.assertNotIn("samples", projected)
        self.assertNotIn("selected", projected)
        self.assertNotIn("raw_points", projected["windows"]["10m"])
        self.assertNotIn("continuation_lookback", public["market_metrics"])

    def test_public_projection_exposes_strict_capital_flow_windows(self):
        item = complete_signal()
        lookback = fixed_continuation_summary(as_of_close_ms=1_234_000)
        lookback["capital_flow"] = fixed_capital_flow_summary()
        # Even valid capital-flow data must expose only the three contractual
        # horizons and never forward arbitrary internal windows.
        lookback["capital_flow"]["windows"]["8h"] = {
            "key": "8h",
            "large_inflow": True,
            "samples": [{"oi": "must-not-leak"}],
        }
        item["market_metrics"]["continuation_lookback"] = lookback
        item["decision_context"] = build_decision_context(item)

        public = public_candidate_payload(item, signal=True)
        capital_flow = public["decision_context"]["continuation_confirmation"][
            "observer"
        ]["capital_flow"]

        self.assertEqual(
            set(capital_flow),
            {
                "algorithm_version",
                "source_mode",
                "status",
                "sample_count",
                "required_sample_count",
                "baseline_window_count",
                "as_of_close_ms",
                "continuity_reset",
                "detected",
                "strongest_window",
                "headline_state",
                "headline_direction",
                "headline_label",
                "direction_basis",
                "long_short_split_available",
                "minimum_change_pct",
                "large_ratio_threshold",
                "persistence_threshold_pct",
                "meaning",
                "permission",
                "windows",
            },
        )
        self.assertEqual(set(capital_flow["windows"]), {"1h", "2h", "4h"})
        expected_window_fields = {
            "key",
            "hours",
            "ready",
            "sample_count",
            "required_sample_count",
            "baseline_window_count",
            "state",
            "label",
            "as_of_close_ms",
            "latest_value",
            "window_start_value",
            "change_amount",
            "change_pct",
            "baseline_average_change_pct",
            "change_vs_average_ratio",
            "above_average",
            "large_inflow",
            "persistence_pct",
            "unit",
            "directional_bias",
            "directional_bias_label",
            "price_return_pct",
            "price_consistency_pct",
        }
        for key in ("1h", "2h", "4h"):
            self.assertEqual(
                set(capital_flow["windows"][key]),
                expected_window_fields,
            )
            self.assertNotIn("samples", capital_flow["windows"][key])
            self.assertNotIn("raw_points", capital_flow["windows"][key])
            self.assertNotIn("reason", capital_flow["windows"][key])
        self.assertEqual(capital_flow["strongest_window"], "4h")
        self.assertTrue(capital_flow["windows"]["4h"]["above_average"])
        self.assertTrue(capital_flow["windows"]["4h"]["large_inflow"])
        self.assertNotIn("samples", capital_flow)
        self.assertNotIn("raw_points", capital_flow)
        self.assertNotIn("reason", capital_flow)

    def test_public_projection_rejects_stale_capital_flow_algorithm(self):
        item = complete_signal()
        lookback = fixed_continuation_summary()
        lookback["capital_flow"] = fixed_capital_flow_summary(
            algorithm_version="CAPITAL_FLOW_LOOKBACK_V0"
        )
        item["market_metrics"]["continuation_lookback"] = lookback
        item["decision_context"] = build_decision_context(item)

        public = public_candidate_payload(item, signal=True)

        self.assertEqual(
            public["decision_context"]["continuation_confirmation"]["observer"]
            ["capital_flow"],
            {},
        )

    def test_public_projection_missing_capital_flow_never_uses_snapshot_oi(self):
        for missing_value in (None, "invalid", {"status": "READY"}):
            with self.subTest(missing_value=missing_value):
                item = complete_signal()
                lookback = fixed_continuation_summary()
                lookback["capital_flow"] = missing_value
                item["market_metrics"].update(
                    {
                        "continuation_lookback": lookback,
                        "open_interest_change_pct": 999.0,
                    }
                )
                item["decision_context"] = build_decision_context(item)

                public = public_candidate_payload(item, signal=True)

                self.assertEqual(
                    public["decision_context"]["continuation_confirmation"]
                    ["observer"]["capital_flow"],
                    {},
                )

    def test_capital_flow_observer_does_not_change_existing_decision(self):
        baseline_item = complete_signal()
        baseline_item["market_metrics"]["continuation_lookback"] = (
            fixed_continuation_summary()
        )
        enriched_item = copy.deepcopy(baseline_item)
        enriched_item["market_metrics"]["continuation_lookback"][
            "capital_flow"
        ] = fixed_capital_flow_summary()

        baseline = build_decision_context(baseline_item)
        enriched = build_decision_context(enriched_item)

        self.assertEqual(enriched["hard_gate"], baseline["hard_gate"])
        self.assertEqual(enriched["final"], baseline["final"])
        self.assertEqual(enriched["confidence"], baseline["confidence"])
        self.assertEqual(
            enriched["continuation_confirmation"]["key"],
            baseline["continuation_confirmation"]["key"],
        )
        self.assertEqual(
            enriched["continuation_confirmation"]["core_votes"],
            baseline["continuation_confirmation"]["core_votes"],
        )

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
