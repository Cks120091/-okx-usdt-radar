import json
import unittest
from datetime import datetime, timezone

from radar.context import (
    active_sessions,
    build_interpretation,
    build_market_context,
    classify_market_driver,
    detect_anomaly,
    summarize_flow_history,
)


class SessionContextTests(unittest.TestCase):
    def test_summer_dst_and_london_new_york_overlap(self):
        sessions = {
            item["key"]: item
            for item in active_sessions(
                datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc)
            )
        }

        self.assertEqual(sessions["ASIA"]["utc8_window"], "08:00–15:00")
        self.assertEqual(sessions["LONDON"]["utc8_window"], "15:00–00:00")
        self.assertEqual(sessions["NEW_YORK"]["utc8_window"], "20:00–05:00")
        self.assertTrue(sessions["LONDON"]["active"])
        self.assertTrue(sessions["NEW_YORK"]["active"])
        self.assertFalse(sessions["ASIA"]["active"])

    def test_winter_dst_is_calculated_from_each_market_timezone(self):
        sessions = {
            item["key"]: item
            for item in active_sessions(
                datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc)
            )
        }

        self.assertEqual(sessions["LONDON"]["utc8_window"], "16:00–01:00")
        self.assertEqual(sessions["NEW_YORK"]["utc8_window"], "21:00–06:00")
        self.assertTrue(sessions["LONDON"]["active"])
        self.assertTrue(sessions["NEW_YORK"]["active"])


class FlowHistoryTests(unittest.TestCase):
    @staticmethod
    def strengthening_samples():
        base = 1_780_000_000_000
        return [
            {
                "sampled_at": base,
                "window_ms": 300_000,
                "mid_price": 100.0,
                "open_interest_usd": 1_000.0,
                "taker_buy_ratio": 0.52,
                "funding_rate": 0.00010,
                "bid_depth_usd": 100.0,
                "ask_depth_usd": 100.0,
                "order_book_imbalance": 0.00,
            },
            {
                "sampled_at": base + 300_000,
                "window_ms": 300_000,
                "mid_price": 101.0,
                "open_interest_usd": 1_050.0,
                "taker_buy_ratio": 0.58,
                "funding_rate": 0.00015,
                "bid_depth_usd": 120.0,
                "ask_depth_usd": 95.0,
                "order_book_imbalance": 0.10,
            },
            {
                "sampled_at": base + 600_000,
                "window_ms": 300_000,
                "mid_price": 102.0,
                "open_interest_usd": 1_100.0,
                "taker_buy_ratio": 0.64,
                "funding_rate": 0.00020,
                "bid_depth_usd": 145.0,
                "ask_depth_usd": 90.0,
                "order_book_imbalance": 0.20,
            },
        ]

    def test_oi_and_taker_strengthen_on_actual_elapsed_time(self):
        summary = summarize_flow_history(self.strengthening_samples(), "LONG")

        self.assertEqual(summary["oi"]["state"], "STRENGTHENING")
        self.assertEqual(summary["oi"]["alignment"], "SAME_DIRECTION_BUILD")
        self.assertEqual(summary["oi"]["velocity_per_5m_pct"], 5.0)
        self.assertEqual(summary["taker"]["state"], "STRENGTHENING")
        self.assertEqual(summary["taker"]["velocity_per_5m_pp"], 6.0)
        self.assertEqual(summary["state"], "STRENGTHENING")
        self.assertFalse(summary["abnormal_speed"])

    def test_taker_buy_to_sell_marks_weakening_not_a_short_trigger(self):
        samples = self.strengthening_samples()
        for sample, ratio in zip(samples, (0.66, 0.50, 0.34)):
            sample["taker_buy_ratio"] = ratio

        summary = summarize_flow_history(samples, "LONG")

        self.assertEqual(summary["taker"]["state"], "WEAKENING")
        self.assertEqual(
            summary["permission"],
            "CONTEXT_ONLY_NEVER_CREATES_OR_CANCELS_TRIGGER",
        )

    def test_one_sample_remains_unknown_without_zero_placeholder(self):
        summary = summarize_flow_history(
            self.strengthening_samples()[:1],
            "LONG",
        )

        self.assertEqual(summary["state"], "UNKNOWN")
        self.assertEqual(summary["oi"]["state"], "UNKNOWN")
        self.assertIsNone(summary["oi"]["velocity_per_5m_pct"])
        self.assertIsNone(summary["abnormal_speed"])

    def test_mismatched_declared_windows_are_unknown(self):
        samples = self.strengthening_samples()
        samples[-1]["window_ms"] = 900_000

        summary = summarize_flow_history(samples, "LONG")

        self.assertFalse(summary["window"]["consistent"])
        self.assertEqual(summary["state"], "UNKNOWN")
        self.assertIsNone(summary["taker"]["velocity_per_5m_pp"])


class AnomalyAndDriverTests(unittest.TestCase):
    def test_empty_anomaly_inputs_are_unknown_not_normal(self):
        anomaly = detect_anomaly({}, {"state": "UNKNOWN"})

        self.assertEqual(anomaly["status"], "UNKNOWN")
        self.assertEqual(anomaly["coverage"]["status"], "UNKNOWN")
        self.assertEqual(anomaly["coverage"]["available"], [])
        self.assertFalse(anomaly["entry_block"])

    def test_partial_healthy_anomaly_data_keeps_visible_coverage(self):
        anomaly = detect_anomaly({"spread_pct": 0.05}, {})

        self.assertEqual(anomaly["status"], "NORMAL")
        self.assertEqual(anomaly["coverage"]["status"], "PARTIAL")
        self.assertIn("spread", anomaly["coverage"]["available"])
        self.assertIn("slippage", anomaly["coverage"]["unknown"])

    def test_anomaly_blocks_entry_only_and_never_changes_trigger(self):
        flow = summarize_flow_history(
            [
                {
                    "sampled_at": 1_780_000_000_000 + index * 300_000,
                    "mid_price": 100.0 + index,
                    "open_interest_usd": value,
                    "taker_buy_ratio": 0.55,
                    "bid_depth_usd": 100.0,
                    "ask_depth_usd": 100.0,
                    "order_book_imbalance": 0.0,
                }
                for index, value in enumerate((1_000.0, 1_120.0, 1_240.0))
            ],
            "LONG",
        )
        anomaly = detect_anomaly(
            {
                "wick_atr": 3.2,
                "volume_ratio_core": 6.5,
                "spread_pct": 0.35,
            },
            flow,
        )

        self.assertEqual(anomaly["status"], "BLOCK")
        self.assertTrue(anomaly["entry_block"])
        self.assertFalse(anomaly["may_create_trigger"])
        self.assertFalse(anomaly["may_cancel_trigger"])
        self.assertIn("OI_VELOCITY", {item["code"] for item in anomaly["reasons"]})

    def test_missing_required_data_blocks_entry_without_cancelling_trigger(self):
        anomaly = detect_anomaly(
            {"missing_sources": ["order_book"]},
            {"state": "UNKNOWN"},
        )

        self.assertEqual(anomaly["status"], "BLOCK")
        self.assertTrue(anomaly["entry_block"])
        self.assertFalse(anomaly["may_cancel_trigger"])
        self.assertIn(
            "REQUIRED_DATA_MISSING",
            {item["code"] for item in anomaly["reasons"]},
        )

    def test_market_driver_distinguishes_btc_independent_and_resonance(self):
        btc_driven = classify_market_driver(
            0.2,
            3.0,
            {"state": "NEUTRAL"},
            70.0,
            False,
        )
        independent = classify_market_driver(
            -0.4,
            -3.0,
            {"state": "SUPPORT"},
            35.0,
            False,
        )
        resonance = classify_market_driver(
            2.0,
            1.0,
            {"state": "SUPPORT"},
            75.0,
            {"active": True, "ratio": 0.72},
        )

        self.assertEqual(btc_driven["key"], "BTC_DRIVEN")
        self.assertEqual(btc_driven["relative_strength_pct"], -2.8)
        self.assertEqual(independent["key"], "INDEPENDENT")
        self.assertEqual(independent["relative_strength"], "STRONGER")
        self.assertEqual(resonance["key"], "MARKET_RESONANCE")

    def test_builders_are_compact_serializable_and_trigger_neutral(self):
        sessions = active_sessions(
            datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc)
        )
        anomaly = detect_anomaly({"spread_pct": 0.31}, {})
        driver = classify_market_driver(
            0.2,
            3.0,
            {"state": "NEUTRAL"},
            70.0,
            False,
        )
        context = build_market_context(
            regime="TREND",
            phase="RETEST",
            volatility="ANOMALOUS",
            anomaly=anomaly,
            driver=driver,
            sessions=sessions,
        )
        interpretation = build_interpretation(
            evidence_groups={
                "position_structure": {
                    "label": "位置／價格行為",
                    "score": 82,
                    "stance": "SUPPORT",
                    "conflicts": [],
                },
                "trend_momentum": {
                    "label": "趨勢／動能",
                    "score": 76,
                    "stance": "SUPPORT",
                    "conflicts": [],
                },
            },
            flow_summary={"state": "STRENGTHENING"},
            anomaly=anomaly,
            main_conflicts=["Spread 偏高"],
            change_conditions={
                "weaken": ["OI 持續衰退"],
                "invalidate": ["跌破原 SL"],
            },
            data_quality={"missing_sources": []},
        )

        json.dumps({"context": context, "interpretation": interpretation})
        self.assertEqual(context["regime"]["key"], "TREND")
        self.assertCountEqual(context["sessions"]["active"], ["LONDON", "NEW_YORK"])
        self.assertTrue(context["anomaly"]["entry_block"])
        self.assertEqual(context["anomaly"]["coverage"]["status"], "PARTIAL")
        self.assertEqual(
            interpretation["trigger_permission"],
            "NEVER_CREATES_OR_CANCELS_TRIGGER",
        )
        self.assertEqual(interpretation["direction_quality"]["score"], 79)

    def test_interpretation_excludes_unavailable_and_zero_confidence_groups(self):
        interpretation = build_interpretation(
            evidence_groups={
                "position_structure": {
                    "label": "位置／價格行為",
                    "score": 72,
                    "stance": "SUPPORT",
                    "confidence": 80,
                    "conflicts": [],
                },
                "trend_momentum": {
                    "label": "趨勢／動能",
                    "score": 99,
                    "stance": "SUPPORT",
                    "confidence": 0,
                    "conflicts": ["這是零信心 Placeholder，不應採用"],
                },
                "participation_flow": {
                    "label": "市場參與",
                    "score": 100,
                    "stance": "CONFLICT",
                    "confidence": 100,
                    "availability": "UNAVAILABLE",
                    "conflicts": ["缺失資料不應形成反證"],
                },
            },
            flow_summary={"state": "UNKNOWN"},
            anomaly={"status": "NORMAL", "reasons": []},
            data_quality={"missing_sources": []},
        )

        self.assertEqual(interpretation["direction_quality"]["score"], 72)
        self.assertEqual(interpretation["confidence"]["key"], "MEDIUM")
        self.assertEqual(interpretation["evidence_coverage"]["status"], "PARTIAL")
        self.assertEqual(
            interpretation["evidence_coverage"]["available_groups"],
            ["position_structure"],
        )
        self.assertEqual(interpretation["main_conflicts"], [])

    def test_interpretation_with_only_placeholders_is_unknown_never_high(self):
        interpretation = build_interpretation(
            evidence_groups={
                "position_structure": {
                    "score": 100,
                    "stance": "SUPPORT",
                    "confidence": 0,
                },
                "trend_momentum": {
                    "score": 100,
                    "stance": "SUPPORT",
                    "data_status": "DATA_MISSING",
                },
            },
            flow_summary={"state": "UNKNOWN"},
            anomaly={"status": "UNKNOWN", "reasons": []},
            data_quality={"missing_sources": []},
        )

        self.assertIsNone(interpretation["direction_quality"]["score"])
        self.assertEqual(interpretation["confidence"]["key"], "UNKNOWN")
        self.assertEqual(interpretation["evidence_coverage"]["status"], "UNKNOWN")

    def test_pending_neutral_placeholder_does_not_raise_evidence_coverage(self):
        interpretation = build_interpretation(
            evidence_groups={
                "participation_flow": {
                    "score": 50,
                    "stance": "NEUTRAL",
                    "confidence": 25,
                    "supporting": [],
                    "conflicts": [],
                    "neutral": ["深度資料待取得；中性不取消 Trigger"],
                },
            },
            flow_summary={"state": "UNKNOWN"},
            anomaly={"status": "UNKNOWN", "reasons": []},
            data_quality={"missing_sources": ["order_book"]},
        )

        self.assertEqual(interpretation["confidence"]["key"], "UNKNOWN")
        self.assertEqual(interpretation["evidence_coverage"]["status"], "UNKNOWN")
        self.assertEqual(interpretation["evidence_coverage"]["available_count"], 0)


if __name__ == "__main__":
    unittest.main()
