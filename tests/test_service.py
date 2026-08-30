import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from radar.api import OKXAPIError
from radar.config import AppConfig
from radar.models import MarketContext, MarketState, RadarReport, Signal, Ticker
from radar.reporting import load_latest_report, save_report
from radar.service import (
    PreflightError,
    RadarRuntime,
    _canonical_single_decision,
    _latest_confirmation,
    _merge_preflight_confirmation,
    _single_scan_failure_message,
)


def signal():
    return Signal(
        inst_id="AAA-USDT-SWAP",
        direction="LONG",
        strategy="fixture",
        score=75.0,
        evidence=["fixture"],
        entry_low="100",
        entry_high="101",
        stop_loss="98",
        take_profit_1="105",
        take_profit_2="108",
        risk_reward=2.0,
        invalidation="fixture",
        spread_pct=0.01,
        quote_volume_24h=10_000_000,
        closed_candle_ts=1,
        regime="TREND",
        signal_stage="EARLY_SIGNAL",
        readiness_score=75.0,
        market_metrics={"last_price": 100.5},
        market_story={"raw": {"core_atr": 2.0}, "trigger": {}},
        execution_quality={"score": 80.0, "label": "良好"},
    )


def allow_entry(item):
    item.actionable = True
    item.entry_eligibility = {
        "status": "ENTRY_READY",
        "actionable": True,
        "new_entry_allowed": True,
    }
    item.decision_context = {
        "final": {
            "status": "ENTER",
            "new_entry_allowed": True,
        }
    }
    return item


def report(completed_at=None):
    stamp = completed_at or datetime.now(timezone.utc).isoformat()
    return RadarReport(
        status="SIGNALS_FOUND",
        generated_at=stamp,
        scope="fixture",
        target_count=1,
        fetched_count=1,
        analyzable_count=1,
        coverage_pct=100.0,
        target_instruments=["AAA-USDT-SWAP"],
        failed_instruments={},
        signals=[signal()],
        exclusion_counts={},
        duration_seconds=0.1,
        message="fixture",
        completed_at=stamp,
    )


class ImmediateScanner:
    def scan_once(self, progress=None, scan_id=None):
        if progress:
            progress("ANALYSIS", 1, 1, "fixture")
        return report()


class BlockingScanner:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def scan_once(self, progress=None, scan_id=None):
        self.calls += 1
        self.started.set()
        self.release.wait(2)
        return report()


class PreviewScanner:
    def __init__(self):
        self.preview_ready = threading.Event()
        self.release = threading.Event()

    def scan_once(self, progress=None, scan_id=None, preview=None):
        core = report()
        core.scan_id = scan_id or ""
        core.runtime_status = "CORE_PREVIEW"
        core.message = "15m 核心結果已先發布"
        if preview:
            preview(core)
        self.preview_ready.set()
        self.release.wait(2)
        return report()


class ModeAwareScanner:
    def __init__(self):
        self.calls = []

    def scan_once(self, progress=None, scan_id=None, preview=None, scan_mode="FULL"):
        self.calls.append(scan_mode)
        current = report()
        current.scan_mode = scan_mode
        if scan_mode == "SHORT":
            current.long_signals = []
            current.short_completed_at = current.completed_at
            current.long_completed_at = ""
        elif scan_mode == "LONG":
            long_signal = signal()
            long_signal.radar_horizon = "LONG"
            current.signals = []
            current.long_signals = [long_signal]
            current.short_completed_at = ""
            current.long_completed_at = current.completed_at
        else:
            long_signal = signal()
            long_signal.radar_horizon = "LONG"
            current.long_signals = [long_signal]
            current.short_completed_at = current.completed_at
            current.long_completed_at = current.completed_at
        return current


class ReleasingScanner(ImmediateScanner):
    def __init__(self):
        self.release_calls = 0

    def release_transient_data(self):
        self.release_calls += 1
        return 7


class SingleInstrumentScanner(ImmediateScanner):
    def __init__(self):
        self.calls = []
        self.release_calls = 0

    def scan_instrument(
        self,
        inst_id,
        market_bias,
        long_market_bias=None,
        btc_bias="NEUTRAL",
        long_btc_bias="NEUTRAL",
        requested_horizon="BOTH",
    ):
        self.calls.append(
            {
                "inst_id": inst_id,
                "market_bias": market_bias,
                "long_market_bias": long_market_bias,
                "btc_bias": btc_bias,
                "long_btc_bias": long_btc_bias,
                "requested_horizon": requested_horizon,
            }
        )
        now_ms = int(time.time() * 1000)
        state = MarketState(
            inst_id=inst_id,
            regime="TREND",
            direction="LONG",
            preferred_strategy="等待突破",
            readiness_score=72.0,
            status="NEAR_TRIGGER",
            missing_conditions=["等待 15m Trigger"],
            spread_pct=0.01,
            quote_volume_24h=20_000_000,
            closed_candle_ts=1,
            summary="目前接近觸發，但還不能進場。",
        )
        return SimpleNamespace(
            inst_id=inst_id,
            ticker=Ticker(inst_id, 100.5, 100.49, 100.51, now_ms),
            context=MarketContext(
                inst_id=inst_id,
                open_interest_usd=None,
                funding_rate=None,
                order_book_imbalance=0.1,
                taker_buy_ratio=None,
                sampled_at=now_ms,
                bid_depth_usd=25_000,
                ask_depth_usd=24_000,
                buy_slippage_pct=0.01,
                sell_slippage_pct=0.01,
                execution_notional_usdt=1_000,
                best_bid=100.49,
                best_ask=100.51,
                source_timestamps={"order_book": now_ms},
            ),
            short_result=SimpleNamespace(
                signal=None,
                market_state=state,
                reason="near_trigger",
            ),
            long_result=None,
            analyzed_at=datetime.now(timezone.utc).isoformat(),
            errors=[],
        )

    def release_transient_data(self):
        self.release_calls += 1
        return 1


class OppositeSignalSingleScanner(SingleInstrumentScanner):
    def scan_instrument(
        self,
        inst_id,
        market_bias,
        long_market_bias=None,
        btc_bias="NEUTRAL",
        long_btc_bias="NEUTRAL",
        requested_horizon="BOTH",
    ):
        analysis = super().scan_instrument(
            inst_id,
            market_bias,
            long_market_bias,
            btc_bias,
            long_btc_bias,
            requested_horizon,
        )
        opposite = allow_entry(signal())
        opposite.direction = "SHORT"
        opposite.market_story = {
            "raw": {"core_atr": 2.0},
            "trigger": {"event_index": 8, "confirmation_index": 9},
        }
        analysis.short_result = SimpleNamespace(
            signal=opposite,
            market_state=analysis.short_result.market_state,
            reason="signal_found",
        )
        return analysis


class SameDirectionSignalSingleScanner(SingleInstrumentScanner):
    def scan_instrument(
        self,
        inst_id,
        market_bias,
        long_market_bias=None,
        btc_bias="NEUTRAL",
        long_btc_bias="NEUTRAL",
        requested_horizon="BOTH",
    ):
        analysis = super().scan_instrument(
            inst_id,
            market_bias,
            long_market_bias,
            btc_bias,
            long_btc_bias,
            requested_horizon,
        )
        current = allow_entry(signal())
        current.entry_low = "200"
        current.entry_high = "201"
        current.stop_loss = "195"
        current.take_profit_1 = "210"
        current.trigger_id = "fresh-trigger-must-not-replace-episode"
        current.summary = "NEW summary"
        current.supporting_evidence = ["NEW evidence"]
        current.safety_checks = [
            {
                "key": "new_context",
                "label": "NEW safety",
                "passed": True,
                "hard": False,
            }
        ]
        current.timeframe_states = {
            "15m": {"label": "NEW timeframe"},
        }
        current.lifecycle = {"age_bars": 1, "triggered_at": "NEW"}
        current.market_story = {
            "raw": {"core_atr": 2.0},
            "trigger": {"event_index": 99, "confirmation_index": 100},
        }
        analysis.short_result = SimpleNamespace(
            signal=current,
            market_state=analysis.short_result.market_state,
            reason="signal_found",
        )
        return analysis


class BlockingSingleInstrumentScanner(SingleInstrumentScanner):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def scan_instrument(
        self,
        inst_id,
        market_bias,
        long_market_bias=None,
        btc_bias="NEUTRAL",
        long_btc_bias="NEUTRAL",
        requested_horizon="BOTH",
    ):
        self.started.set()
        self.release.wait(2)
        return super().scan_instrument(
            inst_id,
            market_bias,
            long_market_bias,
            btc_bias,
            long_btc_bias,
            requested_horizon,
        )


class RejectingInvalidationRepository:
    def __init__(self, active_signal):
        self.active_signal = active_signal
        self.invalidate_calls = 0

    def load_active_signal(self, inst_id, horizon):
        if (
            self.active_signal is not None
            and self.active_signal.inst_id == inst_id
            and self.active_signal.radar_horizon == horizon
        ):
            return self.active_signal
        return None

    def invalidate_preflight_plan(self, signal_item, observed_at):
        self.invalidate_calls += 1
        return False


class StopCrossedSingleScanner(SingleInstrumentScanner):
    def __init__(self, active_signal):
        super().__init__()
        self.repository = RejectingInvalidationRepository(active_signal)

    def scan_instrument(
        self,
        inst_id,
        market_bias,
        long_market_bias=None,
        btc_bias="NEUTRAL",
        long_btc_bias="NEUTRAL",
        requested_horizon="BOTH",
    ):
        analysis = super().scan_instrument(
            inst_id,
            market_bias,
            long_market_bias,
            btc_bias,
            long_btc_bias,
            requested_horizon,
        )
        analysis.ticker = Ticker(
            analysis.inst_id,
            97.0,
            96.99,
            97.01,
            int(time.time() * 1000),
        )
        return analysis


class RecordingCompletionRepository:
    def __init__(self, active_signal):
        self.active_signal = active_signal
        self.complete_calls = 0
        self.terminal_kinds = {}

    def load_active_signal(self, inst_id, horizon):
        active = self.active_signal
        if (
            active is not None
            and active.inst_id == inst_id
            and active.radar_horizon == horizon
        ):
            return active
        return None

    def complete_preflight_plan(self, signal_item, observed_at):
        self.complete_calls += 1
        active = self.active_signal
        if (
            active is None
            or active.trigger_id != signal_item.trigger_id
            or active.direction != signal_item.direction
            or active.radar_horizon != signal_item.radar_horizon
        ):
            return False
        self.terminal_kinds[signal_item.trigger_id] = "COMPLETED"
        self.active_signal = None
        return True

    def preflight_terminal_kind(self, signal_item):
        return self.terminal_kinds.get(signal_item.trigger_id)


class ExistingTerminalRepository:
    def __init__(self, signal_item, terminal_kind):
        self.signal_id = signal_item.trigger_id
        self.terminal_kind = terminal_kind
        self.complete_calls = 0
        self.invalidate_calls = 0

    def load_active_signal(self, inst_id, horizon):
        return None

    def complete_preflight_plan(self, signal_item, observed_at):
        self.complete_calls += 1
        return False

    def invalidate_preflight_plan(self, signal_item, observed_at):
        self.invalidate_calls += 1
        return False

    def preflight_terminal_kind(self, signal_item):
        if signal_item.trigger_id == self.signal_id:
            return self.terminal_kind
        return None


class TargetReachedSingleScanner(SingleInstrumentScanner):
    def __init__(self, active_signal):
        super().__init__()
        self.repository = RecordingCompletionRepository(active_signal)
        self.price = 106.0

    def scan_instrument(
        self,
        inst_id,
        market_bias,
        long_market_bias=None,
        btc_bias="NEUTRAL",
        long_btc_bias="NEUTRAL",
        requested_horizon="BOTH",
    ):
        analysis = super().scan_instrument(
            inst_id,
            market_bias,
            long_market_bias,
            btc_bias,
            long_btc_bias,
            requested_horizon,
        )
        analysis.ticker = Ticker(
            analysis.inst_id,
            self.price,
            self.price - 0.01,
            self.price + 0.01,
            int(time.time() * 1000),
        )
        return analysis


class RolloverRepository:
    def __init__(self, old_signal, new_signal):
        self.active_signal = old_signal
        self.old_signal = old_signal
        self.new_signal = new_signal
        closed_at = datetime.now(timezone.utc)
        self.terminal_signal = Signal.from_dict(old_signal.to_dict())
        self.terminal_signal.signal_stage = "COMPLETED"
        self.terminal_signal.freshness = "COMPLETED"
        self.terminal_signal.actionable = False
        self.terminal_signal.lifecycle = {
            **self.terminal_signal.lifecycle,
            "status": "COMPLETED",
            "terminal_status": "COMPLETED",
            "terminal": True,
            "entry_ready_once": True,
            "closed_at": closed_at.isoformat(),
            "retention_until": (closed_at + timedelta(hours=5)).isoformat(),
        }
        self.terminal_signal.entry_eligibility = {
            "status": "COMPLETED",
            "label": "已達止盈｜本次交易計畫完成",
            "actionable": False,
            "new_entry_allowed": False,
        }

    def load_active_signal(self, inst_id, horizon):
        active = self.active_signal
        if (
            active is not None
            and active.inst_id == inst_id
            and active.radar_horizon == horizon
        ):
            return active
        return None

    def complete_preflight_plan(self, signal_item, observed_at):
        return False

    def preflight_terminal_kind(self, signal_item):
        if signal_item.trigger_id == self.old_signal.trigger_id:
            return "COMPLETED"
        return None

    def load_terminal_signal(self, signal_item):
        if signal_item.trigger_id == self.old_signal.trigger_id:
            return self.terminal_signal
        return None


class NewTriggerAfterTargetScanner(SingleInstrumentScanner):
    def __init__(self, old_signal, new_signal):
        super().__init__()
        self.new_signal = new_signal
        self.repository = RolloverRepository(old_signal, new_signal)

    def scan_instrument(
        self,
        inst_id,
        market_bias,
        long_market_bias=None,
        btc_bias="NEUTRAL",
        long_btc_bias="NEUTRAL",
        requested_horizon="BOTH",
    ):
        analysis = super().scan_instrument(
            inst_id,
            market_bias,
            long_market_bias,
            btc_bias,
            long_btc_bias,
            requested_horizon,
        )
        analysis.ticker = Ticker(
            analysis.inst_id,
            106.0,
            105.99,
            106.01,
            int(time.time() * 1000),
        )
        analysis.short_result = SimpleNamespace(
            signal=self.new_signal,
            market_state=analysis.short_result.market_state,
            reason="new_signal_found",
        )
        self.repository.active_signal = self.new_signal
        return analysis


class FailingScanner:
    def scan_once(self, progress=None, scan_id=None):
        raise RuntimeError("fixture scan failure")


class IncompleteModeScanner:
    def scan_once(self, progress=None, scan_id=None, scan_mode="FULL"):
        current = report()
        current.status = "DATA_INCOMPLETE"
        current.message = f"fixture {scan_mode} data unavailable"
        current.scan_mode = scan_mode
        return current


class FakePushNotifier:
    def __init__(self):
        self.sent = []
        self.sent_event = threading.Event()

    def public_config(self):
        return {
            "available": True,
            "public_key": "fixture-public-key",
            "key_id": "fixture-key-id",
            "temporary_key": True,
            "note": "fixture",
        }

    def normalize_subscription(self, payload):
        if not isinstance(payload, dict) or not payload.get("endpoint"):
            raise ValueError("invalid fixture subscription")
        return {"endpoint": payload["endpoint"], "keys": payload.get("keys", {})}

    def subscription_key(self, subscription):
        return subscription["endpoint"]

    def send(self, subscription, payload):
        self.sent.append((subscription, payload))
        self.sent_event.set()


class FailingPushNotifier(FakePushNotifier):
    def send(self, subscription, payload):
        self.sent_event.set()
        raise RuntimeError(f"delivery failed for {subscription['endpoint']}")


class RuntimeSafetyTests(unittest.TestCase):
    def test_canonical_single_decision_separates_target_completion_from_invalidation(self):
        item = allow_entry(signal())
        completed = _canonical_single_decision(
            item,
            {
                "direction": "LONG",
                "verdict": {
                    "status": "MISSED_ENTRY",
                    "situation": "TARGET_REACHED",
                    "label": "已到達第一目標｜禁止追價",
                    "reason": "最新價格已到達原始 TP1。",
                    "actionable": False,
                },
                "signal_lifecycle": {
                    "status": "TARGET_REACHED",
                    "terminal": True,
                },
                "plan_state": {
                    "status": "TARGET_REACHED",
                    # Contradictory upstream permission must never revive a
                    # completed episode.
                    "old_plan_reusable_for_new_entry": True,
                },
                "original": {"stop_loss": 98.0},
            },
            None,
        )

        self.assertEqual(completed["final"]["status"], "COMPLETED")
        self.assertIn("目標已達", completed["final"]["label"])
        self.assertFalse(completed["final"]["new_entry_allowed"])
        self.assertTrue(completed["final"]["trigger_preserved"])
        self.assertEqual(
            completed["final"]["wait_reason"]["code"],
            "TARGET_REACHED",
        )
        self.assertTrue(completed["episode_plan_state"]["terminal"])
        self.assertTrue(completed["episode_plan_state"]["completed"])
        self.assertFalse(completed["episode_plan_state"]["invalidated"])
        self.assertFalse(
            completed["episode_plan_state"]["old_plan_reusable_for_new_entry"]
        )

        invalidated = _canonical_single_decision(
            item,
            {
                "direction": "LONG",
                "verdict": {
                    "status": "PLAN_INVALIDATED",
                    "situation": "INVALIDATED",
                    "label": "原交易計畫失效",
                    "actionable": False,
                },
                "signal_lifecycle": {
                    "status": "INVALIDATED",
                    "terminal": True,
                },
                "plan_state": {
                    "status": "INVALIDATED",
                    "old_plan_reusable_for_new_entry": True,
                },
                "original": {"stop_loss": 98.0},
            },
            None,
        )

        self.assertEqual(invalidated["final"]["status"], "INVALIDATED")
        self.assertFalse(invalidated["final"]["new_entry_allowed"])
        self.assertFalse(invalidated["final"]["trigger_preserved"])
        self.assertTrue(invalidated["episode_plan_state"]["terminal"])
        self.assertFalse(invalidated["episode_plan_state"]["completed"])
        self.assertTrue(invalidated["episode_plan_state"]["invalidated"])
        self.assertFalse(
            invalidated["episode_plan_state"]["old_plan_reusable_for_new_entry"]
        )

        explicit_completed = _canonical_single_decision(
            item,
            {
                "verdict": {"status": "COMPLETED", "actionable": False},
                "signal_lifecycle": {},
                "plan_state": {"old_plan_reusable_for_new_entry": True},
            },
            None,
        )
        self.assertEqual(explicit_completed["final"]["status"], "COMPLETED")
        self.assertTrue(explicit_completed["episode_plan_state"]["terminal"])
        self.assertFalse(
            explicit_completed["episode_plan_state"][
                "old_plan_reusable_for_new_entry"
            ]
        )

        explicit_invalidated = _canonical_single_decision(
            item,
            {
                "verdict": {
                    "status": "WAIT_RETEST",
                    "situation": "INVALIDATED",
                    "actionable": False,
                },
                "signal_lifecycle": {},
                "plan_state": {"old_plan_reusable_for_new_entry": True},
            },
            None,
        )
        self.assertEqual(explicit_invalidated["final"]["status"], "INVALIDATED")
        self.assertTrue(explicit_invalidated["episode_plan_state"]["terminal"])
        self.assertFalse(
            explicit_invalidated["episode_plan_state"][
                "old_plan_reusable_for_new_entry"
            ]
        )

    def test_canonical_entry_window_closed_is_wait_not_no_chase(self):
        decision = _canonical_single_decision(
            allow_entry(signal()),
            {
                "direction": "LONG",
                "verdict": {
                    "status": "MISSED_ENTRY",
                    "situation": "ENTRY_WINDOW_CLOSED",
                    "label": "進場窗口已關閉",
                    "actionable": False,
                },
                "signal_lifecycle": {
                    "status": "ACTIVE",
                    "terminal": False,
                },
                "plan_state": {
                    "status": "MISSED",
                    "old_plan_reusable_for_new_entry": False,
                },
            },
            None,
        )

        self.assertEqual(decision["final"]["status"], "WAIT")
        self.assertEqual(
            decision["final"]["wait_reason"]["code"],
            "ENTRY_WINDOW_CLOSED",
        )

    def test_latest_confirmation_keeps_same_direction_without_requiring_new_trigger(self):
        state = MarketState(
            inst_id="AAA-USDT-SWAP",
            regime="TREND",
            direction="LONG",
            preferred_strategy="等待突破",
            readiness_score=72.0,
            status="NEAR_TRIGGER",
            missing_conditions=["等待新事件"],
            spread_pct=0.01,
            quote_volume_24h=20_000_000,
            closed_candle_ts=123,
        )

        confirmation = _latest_confirmation(
            SimpleNamespace(signal=None, market_state=state),
            "LONG",
        )

        self.assertEqual(confirmation["status"], "ORIGINAL_DIRECTION_STABLE")
        self.assertFalse(confirmation["new_entry_allowed"])

    def test_latest_confirmation_treats_unspecified_safety_check_as_warning(self):
        current = allow_entry(signal())
        current.safety_checks = [
            {
                "key": "spread",
                "label": "Spread（買賣價差）超過上限",
                "passed": False,
            }
        ]

        confirmation = _latest_confirmation(
            SimpleNamespace(signal=current, market_state=None),
            "LONG",
        )

        self.assertEqual(confirmation["status"], "REVALIDATED")
        self.assertEqual(confirmation["hard_blockers"], [])
        self.assertIn("spread", confirmation["risk_warnings"])
        self.assertTrue(confirmation["new_entry_allowed"])

    def test_opposite_direction_is_only_original_direction_not_reconfirmed(self):
        state = MarketState(
            inst_id="AAA-USDT-SWAP",
            regime="TREND",
            direction="SHORT",
            preferred_strategy="反向觀察",
            readiness_score=80.0,
            status="NEAR_TRIGGER",
            missing_conditions=["等待回測確認"],
            spread_pct=0.01,
            quote_volume_24h=20_000_000,
            closed_candle_ts=123,
            trigger={},
        )
        warning = _latest_confirmation(
            SimpleNamespace(signal=None, market_state=state),
            "LONG",
        )
        self.assertEqual(
            warning["status"],
            "ORIGINAL_DIRECTION_NOT_RECONFIRMED",
        )
        self.assertIn("不建立反向判定", warning["message"])

        opposite_signal = signal()
        opposite_signal.direction = "SHORT"
        opposite_signal.market_story = {
            "trigger": {"event_index": 8, "confirmation_index": 9}
        }
        confirmed = _latest_confirmation(
            SimpleNamespace(signal=opposite_signal, market_state=state),
            "LONG",
        )
        self.assertEqual(
            confirmed["status"],
            "ORIGINAL_DIRECTION_NOT_RECONFIRMED",
        )
        self.assertFalse(confirmed["two_step_reversal_confirmed"])
        self.assertFalse(confirmed["new_entry_allowed"])

    def test_opposite_candidate_hard_failure_cannot_block_original_plan(self):
        opposite = allow_entry(signal())
        opposite.direction = "SHORT"
        opposite.safety_checks = [
            {
                "key": "rr",
                "label": "相反候選 R:R 不足",
                "passed": False,
            }
        ]
        opposite.decision_context = {
            "hard_gate": {
                "blocked": True,
                "unknown": False,
                "blockers": ["safety_checks", "risk_reward"],
            },
            "final": {"status": "NO_EDGE", "new_entry_allowed": False},
        }

        confirmation = _latest_confirmation(
            SimpleNamespace(signal=opposite, market_state=None),
            "LONG",
        )

        self.assertEqual(
            confirmation["status"],
            "ORIGINAL_DIRECTION_NOT_RECONFIRMED",
        )
        self.assertIn("只供參考", confirmation["label"])
        self.assertNotEqual(confirmation["status"], "HARD_GATE_BLOCKED")

    def test_opposite_candidate_shared_risks_remain_advisory(self):
        for key, label in (
            ("anomalous_market", "異常行情"),
            ("liquidity", "流動性不足"),
            ("future_hard_gate", "未來新增的硬性風控"),
        ):
            with self.subTest(key=key):
                opposite = allow_entry(signal())
                opposite.direction = "SHORT"
                opposite.safety_checks = [
                    {
                        "key": key,
                        "label": label,
                        "passed": False,
                        "hard": True,
                    }
                ]

                confirmation = _latest_confirmation(
                    SimpleNamespace(signal=opposite, market_state=None),
                    "LONG",
                )

                self.assertEqual(
                    confirmation["status"],
                    "ORIGINAL_DIRECTION_NOT_RECONFIRMED",
                )
                self.assertEqual(confirmation["hard_blockers"], [])
                self.assertIn(key, confirmation["risk_warnings"])
                self.assertFalse(confirmation["new_entry_allowed"])

        opposite = allow_entry(signal())
        opposite.direction = "SHORT"
        opposite.decision_context = {
            "hard_gate": {
                "blocked": False,
                "unknown": True,
                "unknowns": ["safety_integrity"],
            },
            "final": {"status": "DATA_UNAVAILABLE", "new_entry_allowed": False},
        }
        confirmation = _latest_confirmation(
            SimpleNamespace(signal=opposite, market_state=None),
            "LONG",
        )
        self.assertEqual(
            confirmation["status"],
            "ORIGINAL_DIRECTION_NOT_RECONFIRMED",
        )
        self.assertEqual(confirmation["hard_blockers"], [])
        self.assertIn("safety_integrity", confirmation["risk_warnings"])

    def test_confirmation_merge_ignores_direction_difference_without_closing_plan(self):
        payload = {
            "verdict": {"status": "ENTRY_READY", "actionable": True},
            "signal_lifecycle": {
                "status": "ACTIVE",
                "active": True,
                "terminal": False,
            },
            "plan_state": {
                "status": "ACTIVE",
                "existing_position_plan_active": True,
            },
        }
        merged = _merge_preflight_confirmation(
            payload,
            {
                "status": "ORIGINAL_DIRECTION_NOT_RECONFIRMED",
                "message": "最新未延續原方向",
            },
        )

        self.assertEqual(merged["verdict"]["status"], "ENTRY_READY")
        self.assertTrue(merged["verdict"]["actionable"])
        self.assertTrue(merged["signal_lifecycle"]["active"])
        self.assertTrue(merged["plan_state"]["existing_position_plan_active"])
        self.assertNotIn("direction_status", merged["plan_state"])
        self.assertTrue(merged["latest_confirmation"]["new_entry_allowed"])

    def test_confirmation_merge_turns_legacy_blocks_into_warnings(self):
        for status in ("HARD_GATE_BLOCKED", "DATA_UNAVAILABLE"):
            with self.subTest(status=status):
                merged = _merge_preflight_confirmation(
                    {
                        "verdict": {"status": "ENTRY_READY", "actionable": True},
                        "signal_lifecycle": {
                            "status": "ACTIVE",
                            "active": True,
                            "terminal": False,
                        },
                        "plan_state": {
                            "status": "ACTIVE",
                            "existing_position_plan_active": True,
                        },
                    },
                    {
                        "status": status,
                        "message": "fixture safety block",
                        "hard_blockers": ["fixture_hard_gate"],
                    },
                )

                self.assertEqual(merged["verdict"]["status"], "ENTRY_READY")
                self.assertTrue(merged["verdict"]["actionable"])
                self.assertIn(
                    "fixture_hard_gate",
                    merged["latest_confirmation"]["risk_warnings"],
                )
                self.assertEqual(
                    merged["latest_confirmation"]["hard_blockers"], []
                )
                self.assertTrue(merged["latest_confirmation"]["new_entry_allowed"])
                self.assertTrue(merged["signal_lifecycle"]["active"])

    def test_legacy_reverse_status_cannot_invalidate_episode(self):
        payload = {
            "verdict": {"status": "ENTRY_READY", "actionable": True},
            "signal_lifecycle": {
                "status": "ACTIVE",
                "active": True,
                "terminal": False,
            },
            "plan_state": {
                "status": "ACTIVE",
                "existing_position_plan_active": True,
            },
        }

        merged = _merge_preflight_confirmation(
            payload,
            {
                "status": "CONFIRMED_REVERSAL",
                "message": "舊版反向判定",
            },
        )

        self.assertEqual(merged["verdict"]["status"], "ENTRY_READY")
        self.assertTrue(merged["verdict"]["actionable"])
        self.assertFalse(merged["signal_lifecycle"]["terminal"])
        self.assertTrue(merged["signal_lifecycle"]["active"])
        self.assertEqual(merged["plan_state"]["status"], "ACTIVE")
        self.assertTrue(merged["plan_state"]["existing_position_plan_active"])
        self.assertEqual(
            merged["latest_confirmation"]["status"],
            "ORIGINAL_DIRECTION_NOT_RECONFIRMED",
        )

    def test_opposite_scan_does_not_remove_original_ready_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            scanner = OppositeSignalSingleScanner()
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))
            current = report()
            current.signals[0] = allow_entry(current.signals[0])
            runtime._latest = current

            payload = runtime.scan_instrument_dict("AAA", "SHORT")["short"]

            self.assertEqual(payload["kind"], "SIGNAL")
            self.assertEqual(payload["item"]["direction"], "LONG")
            self.assertEqual(
                payload["latest_confirmation"]["status"],
                "ORIGINAL_DIRECTION_NOT_RECONFIRMED",
            )
            self.assertEqual(payload["preflight"]["verdict"]["status"], "ENTRY_READY")
            self.assertTrue(payload["preflight"]["verdict"]["actionable"])
            self.assertEqual(payload["decision_context"]["final"]["status"], "ENTER")
            self.assertEqual(payload["preflight"]["direction"], "LONG")
            self.assertEqual(payload["decision_context"]["final"]["direction"], "LONG")
            self.assertEqual(payload["item"]["entry_low"], "100")
            self.assertEqual(payload["item"]["market_metrics"]["last_price"], 100.5)

    def test_same_direction_scan_updates_context_but_keeps_episode_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            scanner = SameDirectionSignalSingleScanner()
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))
            current = report()
            current.signals[0] = allow_entry(current.signals[0])
            current.signals[0].trigger_id = "stored-episode"
            current.signals[0].summary = "OLD summary"
            current.signals[0].supporting_evidence = ["OLD evidence"]
            current.signals[0].safety_checks = [
                {
                    "key": "old_context",
                    "label": "OLD safety",
                    "passed": True,
                    "hard": False,
                }
            ]
            current.signals[0].lifecycle = {
                "age_bars": 4,
                "triggered_at": "STORED",
            }
            runtime._latest = current

            payload = runtime.scan_instrument_dict("AAA", "SHORT")["short"]

            self.assertEqual(payload["item"]["direction"], "LONG")
            self.assertEqual(payload["item"]["summary"], "NEW summary")
            self.assertEqual(payload["item"]["supporting_evidence"], ["NEW evidence"])
            self.assertEqual(payload["item"]["safety_checks"][0]["key"], "new_context")
            self.assertEqual(
                payload["item"]["timeframe_states"]["15m"]["label"],
                "NEW timeframe",
            )
            self.assertEqual(payload["item"]["entry_low"], "100")
            self.assertEqual(payload["item"]["entry_high"], "101")
            self.assertEqual(payload["item"]["stop_loss"], "98")
            self.assertEqual(payload["item"]["take_profit_1"], "105")
            self.assertEqual(payload["item"]["lifecycle"]["age_bars"], 4)
            self.assertEqual(payload["item"]["lifecycle"]["triggered_at"], "STORED")

    def test_malformed_stored_plan_stays_data_unavailable_during_opposite_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            scanner = OppositeSignalSingleScanner()
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))
            current = report()
            current.signals[0] = allow_entry(current.signals[0])
            current.signals[0].entry_low = "invalid-price"
            runtime._latest = current

            payload = runtime.scan_instrument_dict("AAA", "SHORT")["short"]

            self.assertEqual(payload["item"]["direction"], "LONG")
            self.assertEqual(
                payload["preflight"]["verdict"]["status"],
                "DATA_UNAVAILABLE",
            )
            self.assertFalse(payload["preflight"]["verdict"]["actionable"])
            self.assertEqual(
                payload["decision_context"]["final"]["status"],
                "DATA_UNAVAILABLE",
            )
            self.assertFalse(
                payload["decision_context"]["final"]["new_entry_allowed"]
            )
            self.assertIsNone(payload["item"]["entry_low"])
            self.assertIsNone(payload["item"]["stop_loss"])

    def test_single_scan_and_preflight_share_one_same_direction_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            scanner = SingleInstrumentScanner()
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))
            runtime._latest = report()

            payload = runtime.scan_instrument_dict("AAA")

            self.assertEqual(
                payload["short"]["latest_confirmation"]["status"],
                "ORIGINAL_DIRECTION_STABLE",
            )
            self.assertEqual(
                payload["short"]["preflight"]["verdict"]["status"],
                "ENTRY_READY",
            )
            self.assertEqual(
                payload["short"]["decision_context"]["final"]["status"],
                "ENTER",
            )
            self.assertTrue(payload["short"]["preflight"]["safety"]["unified_single_scan"])

    def test_single_scan_failures_explain_connection_and_kline_states(self):
        timeout = RuntimeError("The read operation timed out")
        self.assertIn("不是幣種或訊號失效", _single_scan_failure_message(timeout))
        self.assertIn("官方主端點與備援端點", _single_scan_failure_message(timeout))

        candle = RuntimeError("15m K 線取得失敗")
        self.assertIn("不是訊號失效", _single_scan_failure_message(candle))

    def test_retryable_single_scan_failure_recovers_once(self):
        class TransientThenSuccessfulScanner(SingleInstrumentScanner):
            def __init__(self):
                super().__init__()
                self.attempts = 0

            def scan_instrument(
                self,
                inst_id,
                market_bias,
                long_market_bias=None,
                btc_bias="NEUTRAL",
                long_btc_bias="NEUTRAL",
                requested_horizon="BOTH",
            ):
                self.attempts += 1
                if self.attempts == 1:
                    raise OKXAPIError("HTTP 503 temporary upstream failure")
                return super().scan_instrument(
                    inst_id,
                    market_bias,
                    long_market_bias,
                    btc_bias,
                    long_btc_bias,
                    requested_horizon,
                )

        with tempfile.TemporaryDirectory() as directory:
            scanner = TransientThenSuccessfulScanner()
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))

            with patch("radar.service.time.sleep") as sleeper:
                payload = runtime.scan_instrument_dict("AAA", "SHORT")

            self.assertEqual(payload["inst_id"], "AAA-USDT-SWAP")
            self.assertEqual(scanner.attempts, 2)
            self.assertEqual(scanner.release_calls, 2)
            sleeper.assert_called_once_with(0.75)

    def test_non_retryable_single_scan_failure_is_not_hidden_by_retry(self):
        class BrokenScanner(SingleInstrumentScanner):
            def __init__(self):
                super().__init__()
                self.attempts = 0

            def scan_instrument(
                self,
                inst_id,
                market_bias,
                long_market_bias=None,
                btc_bias="NEUTRAL",
                long_btc_bias="NEUTRAL",
                requested_horizon="BOTH",
            ):
                self.attempts += 1
                raise RuntimeError("deterministic calculation bug")

        with tempfile.TemporaryDirectory() as directory:
            scanner = BrokenScanner()
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))

            with (
                patch("radar.service.time.sleep") as sleeper,
                patch("radar.service.LOGGER.exception"),
                self.assertRaises(PreflightError),
            ):
                runtime.scan_instrument_dict("AAA", "SHORT")

            self.assertEqual(scanner.attempts, 1)
            self.assertEqual(scanner.release_calls, 1)
            sleeper.assert_not_called()

    def test_single_instrument_scan_is_normalized_and_not_persisted_to_report(self):
        with tempfile.TemporaryDirectory() as directory:
            scanner = SingleInstrumentScanner()
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))

            payload = runtime.scan_instrument_dict("btc")

            self.assertEqual(payload["inst_id"], "BTC-USDT-SWAP")
            self.assertEqual(payload["short"]["kind"], "STATE")
            self.assertEqual(payload["short"]["item"]["status"], "NEAR_TRIGGER")
            self.assertEqual(payload["long"]["kind"], "UNAVAILABLE")
            self.assertFalse(payload["safety"]["full_market_scan"])
            self.assertFalse(payload["safety"]["persisted_to_report"])
            self.assertTrue(payload["safety"]["persisted_signal_episode"])
            self.assertFalse(payload["safety"]["persisted_to_market_report"])
            self.assertIsNone(runtime._latest)
            self.assertEqual(scanner.release_calls, 1)

    def test_single_scan_uses_the_requested_horizon_market_bias(self):
        with tempfile.TemporaryDirectory() as directory:
            scanner = SingleInstrumentScanner()
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))
            current = report()
            current.market_bias = {
                "label": "15m 偏多",
                "btc": {"direction": "LONG"},
            }
            current.long_market_bias = {
                "label": "4H 偏空",
                "btc": {"direction": "SHORT"},
            }
            runtime._latest = current

            runtime.scan_instrument_dict("AAA", "LONG")

            call = scanner.calls[-1]
            self.assertEqual(call["requested_horizon"], "LONG")
            self.assertEqual(call["market_bias"]["label"], "15m 偏多")
            self.assertEqual(call["long_market_bias"]["label"], "4H 偏空")
            self.assertEqual(call["btc_bias"], "LONG")
            self.assertEqual(call["long_btc_bias"], "SHORT")

    def test_duplicate_single_scan_and_full_scan_are_rejected_while_inflight(self):
        with tempfile.TemporaryDirectory() as directory:
            scanner = BlockingSingleInstrumentScanner()
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))
            errors = []

            def run_first():
                try:
                    runtime.scan_instrument_dict("AAA", "SHORT")
                except Exception as exc:  # pragma: no cover - diagnostic capture
                    errors.append(exc)

            worker = threading.Thread(target=run_first)
            worker.start()
            self.assertTrue(scanner.started.wait(1))

            with self.assertRaises(PreflightError):
                runtime.scan_instrument_dict("AAA", "SHORT")
            with self.assertRaises(PreflightError):
                runtime.trigger_scan(scan_mode="FULL")

            scanner.release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(scanner.calls), 1)

    def test_failed_invalidation_compare_and_set_never_deletes_current_card(self):
        with tempfile.TemporaryDirectory() as directory:
            current = report()
            active = current.signals[0]
            scanner = StopCrossedSingleScanner(active)
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))
            runtime._latest = current

            payload = runtime.scan_instrument_dict("AAA", "SHORT")

            self.assertEqual(
                payload["short"]["preflight"]["verdict"]["status"],
                "PLAN_INVALIDATED",
            )
            self.assertEqual(scanner.repository.invalidate_calls, 1)
            self.assertEqual(len(runtime._latest.signals), 1)
            self.assertEqual(
                runtime._latest.signals[0].trigger_id,
                active.trigger_id,
            )
            self.assertEqual(runtime._invalidated_preflight_signals, {})

    def test_target_preflight_durably_completes_and_never_revives_old_card(self):
        with tempfile.TemporaryDirectory() as directory:
            current = report()
            active = allow_entry(current.signals[0])
            active.trigger_id = "short-episode-old"
            long_signal = allow_entry(signal())
            long_signal.trigger_id = "long-episode-untouched"
            long_signal.radar_horizon = "LONG"
            current.long_signals = [long_signal]
            save_report(current, directory)

            scanner = TargetReachedSingleScanner(active)
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))

            # Simulate latest.json failing after SQLite's exact terminal CAS.
            with patch("radar.service.save_report", side_effect=OSError("disk full")):
                payload = runtime.scan_instrument_dict("AAA", "SHORT")

            preflight = payload["short"]["preflight"]
            self.assertEqual(preflight["verdict"]["status"], "COMPLETED")
            self.assertEqual(preflight["verdict"]["situation"], "TARGET_REACHED")
            self.assertEqual(
                payload["short"]["decision_context"]["final"]["status"],
                "COMPLETED",
            )
            self.assertEqual(scanner.repository.complete_calls, 1)
            self.assertEqual(runtime._latest.signals, [])
            self.assertEqual(
                [item.trigger_id for item in runtime._latest.closed_signals],
                ["short-episode-old"],
            )
            self.assertEqual(
                runtime._latest.closed_signals[0].entry_eligibility["label"],
                "已達止盈｜本次交易計畫完成",
            )
            self.assertTrue(
                runtime._latest.closed_signals[0].lifecycle["retention_until"]
            )
            self.assertEqual(
                runtime._latest.long_signals[0].trigger_id,
                "long-episode-untouched",
            )
            # The failed file write intentionally leaves the old card on disk.
            self.assertEqual(
                load_latest_report(directory).signals[0].trigger_id,
                "short-episode-old",
            )

            # Startup reconciles exact repository tombstones and repairs the
            # stale file without touching the other horizon.
            restored = RadarRuntime(scanner, AppConfig(data_dir=directory))
            self.assertEqual(restored._latest.signals, [])
            self.assertEqual(
                restored._latest.long_signals[0].trigger_id,
                "long-episode-untouched",
            )
            self.assertEqual(load_latest_report(directory).signals, [])

            scanner.price = 100.5
            refreshed = restored.scan_instrument_dict("AAA", "SHORT")
            self.assertIsNone(refreshed["short"]["preflight"])
            self.assertEqual(scanner.repository.complete_calls, 1)

    def test_terminal_old_episode_and_new_trigger_are_returned_as_two_cards(self):
        with tempfile.TemporaryDirectory() as directory:
            current = report()
            old_signal = allow_entry(current.signals[0])
            old_signal.trigger_id = "old-completed-episode"
            old_signal.lifecycle = {
                "status": "ACTIVE",
                "terminal": False,
                "entry_ready_once": True,
                "triggered_at": "2026-08-20T00:00:00+00:00",
            }
            new_signal = allow_entry(signal())
            new_signal.trigger_id = "new-independent-episode"
            new_signal.entry_low = "107"
            new_signal.entry_high = "108"
            new_signal.stop_loss = "103"
            new_signal.take_profit_1 = "115"
            new_signal.lifecycle = {
                "status": "ACTIVE",
                "terminal": False,
                "entry_ready_once": True,
                "triggered_at": "2026-08-20T00:15:00+00:00",
            }
            scanner = NewTriggerAfterTargetScanner(old_signal, new_signal)
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))
            runtime._latest = current

            payload = runtime.scan_instrument_dict("AAA", "SHORT")["short"]

            self.assertEqual(payload["kind"], "SIGNAL")
            self.assertIsNone(payload["preflight"])
            self.assertEqual(
                payload["item"]["trigger_id"],
                "new-independent-episode",
            )
            self.assertEqual(payload["item"]["entry_low"], "107")
            self.assertEqual(payload["item"]["stop_loss"], "103")
            self.assertEqual(
                payload["closed_item"]["trigger_id"],
                "old-completed-episode",
            )
            self.assertEqual(
                payload["closed_item"]["entry_eligibility"]["label"],
                "已達止盈｜本次交易計畫完成",
            )
            self.assertNotEqual(
                payload["item"]["trigger_id"],
                payload["closed_item"]["trigger_id"],
            )
            self.assertEqual(runtime._latest.signals, [])
            self.assertEqual(
                runtime._latest.closed_signals[0].trigger_id,
                "old-completed-episode",
            )

    def test_durable_invalidation_wins_over_concurrent_live_target(self):
        with tempfile.TemporaryDirectory() as directory:
            current = report()
            active = allow_entry(current.signals[0])
            active.trigger_id = "episode-race-invalidated"
            scanner = TargetReachedSingleScanner(active)
            scanner.repository = ExistingTerminalRepository(active, "INVALIDATED")
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))
            runtime._latest = current

            payload = runtime.scan_instrument_dict("AAA", "SHORT")
            preflight = payload["short"]["preflight"]

            self.assertEqual(preflight["verdict"]["status"], "PLAN_INVALIDATED")
            self.assertEqual(preflight["signal_lifecycle"]["status"], "INVALIDATED")
            self.assertEqual(
                payload["short"]["decision_context"]["final"]["status"],
                "INVALIDATED",
            )
            self.assertEqual(
                set(runtime._terminal_preflight_outcomes.values()),
                {"INVALIDATED"},
            )
            self.assertEqual(len(runtime._invalidated_preflight_signals), 1)

    def test_durable_completion_wins_over_concurrent_live_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            current = report()
            active = allow_entry(current.signals[0])
            active.trigger_id = "episode-race-completed"
            scanner = StopCrossedSingleScanner(active)
            scanner.repository = ExistingTerminalRepository(active, "COMPLETED")
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))
            runtime._latest = current

            payload = runtime.scan_instrument_dict("AAA", "SHORT")
            preflight = payload["short"]["preflight"]

            self.assertEqual(preflight["verdict"]["status"], "COMPLETED")
            self.assertEqual(preflight["verdict"]["situation"], "TARGET_REACHED")
            self.assertEqual(
                payload["short"]["decision_context"]["final"]["status"],
                "COMPLETED",
            )
            self.assertEqual(
                set(runtime._terminal_preflight_outcomes.values()),
                {"COMPLETED"},
            )
            self.assertEqual(runtime._invalidated_preflight_signals, {})

    def test_unknown_repository_closure_never_fabricates_live_tp_or_sl(self):
        with tempfile.TemporaryDirectory() as directory:
            current = report()
            active = allow_entry(current.signals[0])
            active.trigger_id = "episode-race-data-gap"
            scanner = TargetReachedSingleScanner(active)
            scanner.repository = ExistingTerminalRepository(
                active,
                "CLOSED_UNKNOWN",
            )
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))
            runtime._latest = current

            payload = runtime.scan_instrument_dict("AAA", "SHORT")
            preflight = payload["short"]["preflight"]
            decision = payload["short"]["decision_context"]

            self.assertEqual(preflight["verdict"]["status"], "DATA_UNAVAILABLE")
            self.assertEqual(preflight["verdict"]["situation"], "CLOSED_UNKNOWN")
            self.assertNotIn("SL／失效位已被突破", preflight["verdict"]["reason"])
            self.assertEqual(decision["final"]["status"], "DATA_UNAVAILABLE")
            self.assertEqual(
                decision["final"]["wait_reason"]["code"],
                "CLOSED_UNKNOWN",
            )
            self.assertTrue(decision["episode_plan_state"]["terminal"])
            self.assertTrue(decision["episode_plan_state"]["closed_unknown"])
            self.assertFalse(decision["episode_plan_state"]["invalidated"])
            self.assertFalse(decision["episode_plan_state"]["completed"])
            self.assertEqual(runtime._latest.signals, [])
            self.assertEqual(runtime._invalidated_preflight_signals, {})
            self.assertEqual(
                set(runtime._terminal_preflight_outcomes.values()),
                {"CLOSED_UNKNOWN"},
            )

    def test_closed_unknown_payload_never_calls_invalidation_mutator(self):
        with tempfile.TemporaryDirectory() as directory:
            active = allow_entry(signal())
            active.trigger_id = "episode-closed-unknown"
            current = report()
            current.signals = [active]
            scanner = ImmediateScanner()
            scanner.repository = ExistingTerminalRepository(
                active,
                "CLOSED_UNKNOWN",
            )
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))
            runtime._latest = current
            payload = {
                "verdict": {
                    "status": "DATA_UNAVAILABLE",
                    "situation": "CLOSED_UNKNOWN",
                    "actionable": False,
                },
                "signal_lifecycle": {
                    "status": "CLOSED_UNKNOWN",
                    "terminal": True,
                },
                "plan_state": {"status": "CLOSED_UNKNOWN"},
            }

            result = runtime._persist_preflight_terminal(
                repository=scanner.repository,
                signal=active,
                payload=payload,
                observed_at=datetime.now(timezone.utc).isoformat(),
                horizon="SHORT",
                cache_key=(current.completed_at, "SHORT", active.inst_id),
            )

            self.assertEqual(result, "CLOSED_UNKNOWN")
            self.assertEqual(scanner.repository.complete_calls, 0)
            self.assertEqual(scanner.repository.invalidate_calls, 0)
            self.assertEqual(runtime._latest.signals, [])

    def test_stale_terminal_cleanup_cannot_remove_newer_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            old_signal = allow_entry(signal())
            old_signal.trigger_id = "episode-old"
            new_signal = allow_entry(signal())
            new_signal.trigger_id = "episode-new"
            current = report()
            current.signals = [new_signal]
            scanner = ImmediateScanner()
            scanner.repository = ExistingTerminalRepository(
                old_signal,
                "COMPLETED",
            )
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))
            runtime._latest = current
            terminal_payload = {
                "verdict": {
                    "status": "MISSED_ENTRY",
                    "situation": "TARGET_REACHED",
                    "actionable": False,
                },
                "signal_lifecycle": {"status": "TARGET_REACHED", "terminal": True},
                "plan_state": {"status": "TARGET_REACHED"},
            }

            result = runtime._persist_preflight_terminal(
                repository=scanner.repository,
                signal=old_signal,
                payload=terminal_payload,
                observed_at=datetime.now(timezone.utc).isoformat(),
                horizon="SHORT",
                cache_key=(current.completed_at, "SHORT", old_signal.inst_id),
            )

            self.assertEqual(result, "COMPLETED")
            self.assertEqual(len(runtime._latest.signals), 1)
            self.assertEqual(runtime._latest.signals[0].trigger_id, "episode-new")
            self.assertEqual(
                [item.trigger_id for item in runtime._latest.closed_signals],
                ["episode-old"],
            )
            self.assertNotEqual(
                runtime._latest.closed_signals[0].trigger_id,
                runtime._latest.signals[0].trigger_id,
            )

    def test_push_config_is_exposed_without_a_private_key(self):
        with tempfile.TemporaryDirectory() as directory:
            notifier = FakePushNotifier()
            runtime = RadarRuntime(
                ImmediateScanner(), AppConfig(data_dir=directory), push_notifier=notifier
            )

            config = runtime.push_config()

            self.assertTrue(config["available"])
            self.assertEqual(config["public_key"], "fixture-public-key")
            self.assertNotIn("private_key", config)

    def test_successful_background_scan_sends_one_minimal_completion_notice(self):
        with tempfile.TemporaryDirectory() as directory:
            notifier = FakePushNotifier()
            runtime = RadarRuntime(
                ImmediateScanner(), AppConfig(data_dir=directory), push_notifier=notifier
            )
            device = {"endpoint": "fixture-device", "keys": {}}

            self.assertTrue(runtime.trigger_scan(device))
            self.assertTrue(notifier.sent_event.wait(2))

            self.assertEqual(len(notifier.sent), 1)
            sent_device, payload = notifier.sent[0]
            self.assertEqual(sent_device, device)
            self.assertEqual(payload["status"], "SUCCESS")
            self.assertEqual(payload["url"], "/")
            self.assertNotIn("AAA-USDT-SWAP", json.dumps(payload))
            self.assertFalse(runtime.status()["running"])

    def test_joining_the_same_scan_deduplicates_the_notification_device(self):
        with tempfile.TemporaryDirectory() as directory:
            scanner = BlockingScanner()
            notifier = FakePushNotifier()
            runtime = RadarRuntime(
                scanner, AppConfig(data_dir=directory), push_notifier=notifier
            )
            device = {"endpoint": "fixture-device", "keys": {}}

            self.assertTrue(runtime.trigger_scan(device))
            self.assertTrue(scanner.started.wait(1))
            self.assertFalse(runtime.trigger_scan(device))
            scanner.release.set()
            self.assertTrue(notifier.sent_event.wait(2))

            self.assertEqual(scanner.calls, 1)
            self.assertEqual(len(notifier.sent), 1)

    def test_failed_background_scan_sends_failure_notice_without_raising_to_user(self):
        with tempfile.TemporaryDirectory() as directory:
            notifier = FakePushNotifier()
            runtime = RadarRuntime(
                FailingScanner(), AppConfig(data_dir=directory), push_notifier=notifier
            )

            self.assertTrue(runtime.trigger_scan({"endpoint": "fixture-device", "keys": {}}))
            self.assertTrue(notifier.sent_event.wait(2))

            self.assertEqual(notifier.sent[0][1]["status"], "ERROR")
            self.assertEqual(runtime.status()["system_status"], "ERROR")

    def test_push_delivery_failure_does_not_fail_scan_or_log_capability_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            notifier = FailingPushNotifier()
            runtime = RadarRuntime(
                ImmediateScanner(), AppConfig(data_dir=directory), push_notifier=notifier
            )
            endpoint = "secret-browser-capability-endpoint"

            with self.assertLogs("okx_radar", level="WARNING") as logs:
                self.assertTrue(runtime.trigger_scan({"endpoint": endpoint, "keys": {}}))
                self.assertTrue(notifier.sent_event.wait(2))

            self.assertEqual(runtime.status()["system_status"], "FRESH")
            self.assertNotIn(endpoint, "\n".join(logs.output))

    def test_public_report_omits_developer_payloads_and_keeps_ui_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            current = report()
            item = current.signals[0]
            item.market_metrics = {
                "last_price": 100.0,
                "price_change_1h_pct": 1.5,
                "raw_indicators": {"oversized": "x" * 20_000},
                "order_book_sequence": {
                    "reason": "已累積時間序列",
                    "snapshots": ["x" * 10_000],
                },
            }
            item.market_story = {
                "where": {"label": "壓力上方", "distance_atr": 1.2},
                "trigger": {"type": "BREAKOUT", "event_age_bars": 1},
                "attack_waves": {"BULL": ["x" * 20_000]},
            }
            item.execution_quality = {"score": 87, "label": "良好", "raw": "x" * 5_000}
            item.management_plan = {
                "adaptive_market_plan": True,
                "frozen_at_trigger": True,
                "market_strength_score": 76.0,
                "market_strength_label": "強",
                "target_method": "有效市場結構＋波動／力度自動目標",
                "market_plan_sources": ["價格 Trigger", "價格＋OI", "Taker／CVD"],
                "structural_target_price": 101.0,
                "structural_target_rr": 0.9,
                "first_obstacle_action": "途中觀察",
                "private_debug": "not-public",
            }
            item.lifecycle = {
                "age_bars": 1,
                "triggered_at": "2026-08-27T15:46:18+00:00",
                "event_key": "internal-only",
            }
            item.entry_eligibility = {
                "status": "ENTRY_READY",
                "label": "可進",
                "reason": "仍在進場區",
                "chase_atr": 0.1,
                "remaining_rr": 2.2,
                "raw": "x" * 5_000,
            }
            item.safety_checks = [
                {
                    "key": "spread",
                    "status": "UNKNOWN",
                    "passed": False,
                    "label": "Spread（買賣價差）",
                    "hard": True,
                    "value": None,
                    "reason": "最新買賣價差未知",
                    "internal": "not-public",
                }
            ]
            item.decision_context = {
                "hard_gate": {
                    "status": "UNKNOWN",
                    "passed": False,
                    "blocked": False,
                    "unknown": True,
                    "blockers": [],
                    "unknowns": ["spread"],
                    "reasons": ["最新買賣價差未知"],
                    "thresholds": {"max_spread_pct": 0.1},
                    "checks": [
                        {
                            "key": "spread",
                            "label": "Spread（買賣價差）",
                            "status": "UNKNOWN",
                            "value": None,
                            "reason": "最新買賣價差未知",
                            "hard": True,
                            "internal": "not-public",
                        }
                    ],
                },
                "final": {
                    "status": "DATA_UNAVAILABLE",
                    "label": "資料不足",
                    "new_entry_allowed": False,
                },
            }
            closed = Signal.from_dict(item.to_dict())
            closed.trigger_id = "closed-episode-public-id"
            closed.signal_stage = "COMPLETED"
            closed.freshness = "COMPLETED"
            closed.lifecycle = {
                **closed.lifecycle,
                "status": "COMPLETED",
                "terminal_status": "COMPLETED",
                "terminal": True,
                "entry_ready_once": True,
                "closed_at": "2026-08-27T16:00:00+00:00",
                "retention_until": "2026-08-27T21:00:00+00:00",
            }
            closed.entry_eligibility = {
                "status": "COMPLETED",
                "label": "已達止盈｜本次交易計畫完成",
                "actionable": False,
            }
            current.closed_signals = [closed]
            market = MarketState(
                inst_id="AAA-USDT-SWAP",
                regime="TREND",
                direction="LONG",
                preferred_strategy="fixture",
                readiness_score=75.0,
                status="NEAR_TRIGGER",
                missing_conditions=[],
                spread_pct=0.01,
                quote_volume_24h=10_000_000,
                closed_candle_ts=1,
                market_metrics={
                    "last_price": 100.0,
                    "raw_indicators": {"x": "y" * 10_000},
                },
                market_story={"raw": "x" * 10_000},
            )
            current.market_map = [market]
            current.long_market_map = [market]
            current.context_target_count = 83
            current.context_enriched_count = 83
            current.data_quality = {
                "deep_target_count": 83,
                "deep_enriched_count": 83,
                "deep_complete_count": 77,
                "deep_completeness_pct": 92.77,
                "deep_source_completeness_pct": 96.14,
                "source_success": {
                    "funding": 83,
                    "order_book": 77,
                    "trades": 81,
                    "timing": 83,
                    "open_interest": 75,
                },
                "source_missing": {
                    "order_book": 6,
                    "trades": 2,
                    "open_interest": 8,
                },
                "context_failure_count": 8,
                "internal_failure_details": "not-public",
            }
            runtime = RadarRuntime(ImmediateScanner(), AppConfig(data_dir=directory))
            runtime._latest = current

            full_size = len(json.dumps(current.to_dict(), ensure_ascii=False))
            payload = runtime.latest_dict()
            public_size = len(json.dumps(payload, ensure_ascii=False))

            self.assertNotIn("target_instruments", payload)
            self.assertNotIn("api_metrics", payload)
            self.assertNotIn("long_market_map", payload)
            self.assertEqual(payload["context_enriched_count"], 83)
            self.assertEqual(payload["data_quality"]["deep_target_count"], 83)
            self.assertEqual(
                payload["data_quality"]["deep_complete_count"],
                77,
            )
            self.assertEqual(
                payload["data_quality"]["deep_source_completeness_pct"],
                96.14,
            )
            self.assertEqual(
                payload["data_quality"]["source_missing"]["order_book"],
                6,
            )
            self.assertNotIn("internal_failure_details", payload["data_quality"])
            self.assertNotIn("raw_indicators", payload["signals"][0]["market_metrics"])
            self.assertEqual(
                payload["signals"][0]["data_timestamp"],
                current.signals[0].data_timestamp,
            )
            self.assertEqual(
                payload["signals"][0]["closed_candle_ts"],
                current.signals[0].closed_candle_ts,
            )
            self.assertEqual(
                payload["signals"][0]["market_metrics"]["order_book_sequence"],
                {"reason": "已累積時間序列"},
            )
            self.assertEqual(
                payload["signals"][0]["market_story"]["where"],
                {"label": "壓力上方"},
            )
            self.assertNotIn("attack_waves", payload["signals"][0]["market_story"])
            self.assertEqual(
                payload["signals"][0]["entry_eligibility"]["status"],
                "ENTRY_READY",
            )
            self.assertTrue(
                payload["signals"][0]["management_plan"]["adaptive_market_plan"]
            )
            self.assertEqual(
                payload["signals"][0]["management_plan"]["market_plan_sources"],
                ["價格 Trigger", "價格＋OI", "Taker／CVD"],
            )
            self.assertNotIn(
                "private_debug",
                payload["signals"][0]["management_plan"],
            )
            self.assertEqual(
                payload["signals"][0]["lifecycle"],
                {
                    "age_bars": 1,
                    "triggered_at": "2026-08-27T15:46:18+00:00",
                },
            )
            self.assertEqual(
                payload["closed_signals"][0]["trigger_id"],
                "closed-episode-public-id",
            )
            self.assertEqual(
                payload["closed_signals"][0]["lifecycle"]["retention_until"],
                "2026-08-27T21:00:00+00:00",
            )
            self.assertTrue(
                payload["closed_signals"][0]["lifecycle"]["entry_ready_once"]
            )
            self.assertNotIn(
                "event_key",
                payload["closed_signals"][0]["lifecycle"],
            )
            self.assertEqual(
                payload["signals"][0]["decision_context"]["hard_gate"][
                    "checks"
                ][0]["status"],
                "UNKNOWN",
            )
            self.assertEqual(
                payload["signals"][0]["decision_context"]["hard_gate"][
                    "thresholds"
                ]["max_spread_pct"],
                0.1,
            )
            self.assertNotIn(
                "internal",
                payload["signals"][0]["decision_context"]["hard_gate"][
                    "checks"
                ][0],
            )
            self.assertEqual(
                payload["signals"][0]["safety_checks"][0]["reason"],
                "最新買賣價差未知",
            )
            self.assertEqual(
                set(payload["market_map"][0]),
                {
                    "inst_id",
                    "regime",
                    "direction",
                    "readiness_score",
                    "status",
                    "market_metrics",
                },
            )
            self.assertLess(public_size, full_size / 2)

    def test_successful_web_scan_releases_transient_scanner_data(self):
        with tempfile.TemporaryDirectory() as directory:
            scanner = ReleasingScanner()
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))

            runtime.scan_blocking()

            self.assertEqual(scanner.release_calls, 1)

    def test_partial_scan_passes_mode_and_preserves_unrequested_horizon(self):
        with tempfile.TemporaryDirectory() as directory:
            scanner = ModeAwareScanner()
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))
            previous = report()
            previous_long = signal()
            previous_long.radar_horizon = "LONG"
            previous.long_signals = [previous_long]
            previous.short_completed_at = previous.completed_at
            previous.long_completed_at = previous.completed_at
            runtime._latest = previous

            completed = runtime.scan_blocking("SHORT")

            self.assertEqual(scanner.calls, ["SHORT"])
            self.assertEqual(completed.scan_mode, "SHORT")
            self.assertEqual(len(completed.signals), 1)
            self.assertEqual(len(completed.long_signals), 1)
            self.assertEqual(
                completed.long_completed_at,
                previous.long_completed_at,
            )
            self.assertNotEqual(
                completed.short_completed_at,
                previous.short_completed_at,
            )
            self.assertEqual(runtime.status()["scan_mode"], "SHORT")

    def test_first_partial_scan_exposes_only_the_requested_horizon(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RadarRuntime(
                ModeAwareScanner(),
                AppConfig(data_dir=directory),
            )

            short_only = runtime.scan_blocking("SHORT")
            short_payload = runtime.latest_dict()

            self.assertEqual(short_only.long_signals, [])
            self.assertEqual(short_only.long_watchlist, [])
            self.assertEqual(short_only.long_market_map, [])
            self.assertEqual(short_only.long_completed_at, "")
            self.assertTrue(short_payload["horizon_freshness"]["SHORT"]["available"])
            self.assertFalse(short_payload["horizon_freshness"]["LONG"]["available"])

        with tempfile.TemporaryDirectory() as directory:
            runtime = RadarRuntime(
                ModeAwareScanner(),
                AppConfig(data_dir=directory),
            )

            long_only = runtime.scan_blocking("LONG")
            long_payload = runtime.latest_dict()

            self.assertEqual(long_only.signals, [])
            self.assertEqual(long_only.watchlist, [])
            self.assertEqual(long_only.market_map, [])
            self.assertEqual(long_only.market_regime_counts, {})
            self.assertEqual(long_only.market_bias, {})
            self.assertEqual(long_only.short_completed_at, "")
            self.assertFalse(long_payload["horizon_freshness"]["SHORT"]["available"])
            self.assertTrue(long_payload["horizon_freshness"]["LONG"]["available"])

    def test_alternating_partial_scans_keep_each_completed_horizon(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RadarRuntime(
                ModeAwareScanner(),
                AppConfig(data_dir=directory),
            )

            short_first = runtime.scan_blocking("SHORT")
            first_short_stamp = short_first.short_completed_at
            short_signal_ids = [item.inst_id for item in short_first.signals]

            long_second = runtime.scan_blocking("LONG")
            first_long_stamp = long_second.long_completed_at

            self.assertEqual(long_second.short_completed_at, first_short_stamp)
            self.assertEqual(
                [item.inst_id for item in long_second.signals],
                short_signal_ids,
            )
            self.assertEqual(len(long_second.long_signals), 1)
            after_long_payload = runtime.latest_dict()
            self.assertEqual(
                after_long_payload["horizon_read_only_reasons"],
                {"SHORT": None, "LONG": None},
            )
            self.assertTrue(
                after_long_payload["safety"]["horizon_actionable"]["SHORT"]
            )
            self.assertTrue(
                after_long_payload["safety"]["horizon_actionable"]["LONG"]
            )
            self.assertIsNone(after_long_payload["signals_read_only_reason"])
            self.assertIsNone(after_long_payload["signals_suppressed_reason"])
            self.assertIsNone(after_long_payload["long_signals_suppressed_reason"])

            short_third = runtime.scan_blocking("SHORT")

            self.assertEqual(short_third.long_completed_at, first_long_stamp)
            self.assertEqual(len(short_third.long_signals), 1)
            self.assertTrue(runtime.latest_dict()["horizon_freshness"]["SHORT"]["available"])
            self.assertTrue(runtime.latest_dict()["horizon_freshness"]["LONG"]["available"])

    def test_partial_merge_does_not_revive_an_unscanned_phantom_horizon(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RadarRuntime(
                ModeAwareScanner(),
                AppConfig(data_dir=directory),
            )
            previous = report()
            phantom = signal()
            phantom.radar_horizon = "LONG"
            previous.scan_mode = "SHORT"
            previous.short_completed_at = previous.completed_at
            previous.long_completed_at = ""
            previous.long_signals = [phantom]
            runtime._latest = previous

            completed = runtime.scan_blocking("SHORT")

            self.assertEqual(completed.long_signals, [])
            self.assertEqual(completed.long_completed_at, "")
            self.assertFalse(
                runtime.latest_dict()["horizon_freshness"]["LONG"]["available"]
            )

    def test_failed_partial_scan_keeps_unrequested_completed_horizon(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = report()
            previous.signals[0] = allow_entry(previous.signals[0])
            previous_long = allow_entry(signal())
            previous_long.radar_horizon = "LONG"
            previous.long_signals = [previous_long]
            previous.short_completed_at = previous.completed_at
            previous.long_completed_at = previous.completed_at
            save_report(previous, directory)
            runtime = RadarRuntime(
                IncompleteModeScanner(),
                AppConfig(data_dir=directory),
            )

            failed_attempt = runtime.scan_blocking("SHORT")
            payload = runtime.latest_dict()
            persisted = load_latest_report(directory)

            self.assertEqual(failed_attempt.status, "DATA_INCOMPLETE")
            self.assertEqual(payload["runtime_status"], "ERROR")
            self.assertEqual(payload["signals"], [])
            self.assertEqual(payload["watchlist"], [])
            self.assertEqual(payload["market_map"], [])
            self.assertEqual(payload["market_regime_counts"], {})
            self.assertEqual(payload["market_bias"], {})
            self.assertEqual(len(payload["long_signals"]), 1)
            self.assertEqual(payload["scan_unavailable_horizons"], ["SHORT"])
            self.assertFalse(payload["safety"]["horizon_actionable"]["SHORT"])
            self.assertTrue(payload["safety"]["horizon_actionable"]["LONG"])
            self.assertEqual(payload["horizon_read_only_reasons"]["SHORT"], "ERROR")
            self.assertIsNone(payload["horizon_read_only_reasons"]["LONG"])
            self.assertEqual(
                payload["horizon_suppressed_reasons"],
                {"SHORT": "ERROR", "LONG": None},
            )
            self.assertEqual(payload["signals_suppressed_reason"], "ERROR")
            self.assertIsNone(payload["long_signals_suppressed_reason"])
            self.assertTrue(
                payload["long_signals"][0]["decision_context"]["final"][
                    "new_entry_allowed"
                ]
            )
            self.assertTrue(previous.signals[0].actionable)
            self.assertTrue(
                previous.signals[0].decision_context["final"]["new_entry_allowed"]
            )
            self.assertEqual(len(persisted.signals), 1)
            self.assertEqual(len(persisted.long_signals), 1)
            self.assertEqual(persisted.completed_at, previous.completed_at)

    def test_failed_full_scan_hides_both_previous_horizons_without_deleting_them(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = report()
            previous.signals[0] = allow_entry(previous.signals[0])
            previous_long = allow_entry(signal())
            previous_long.radar_horizon = "LONG"
            previous.long_signals = [previous_long]
            previous.short_completed_at = previous.completed_at
            previous.long_completed_at = previous.completed_at
            save_report(previous, directory)
            runtime = RadarRuntime(
                IncompleteModeScanner(),
                AppConfig(data_dir=directory),
            )

            runtime.scan_blocking("FULL")
            payload = runtime.latest_dict()
            persisted = load_latest_report(directory)

            self.assertEqual(payload["runtime_status"], "ERROR")
            self.assertEqual(payload["signals"], [])
            self.assertEqual(payload["watchlist"], [])
            self.assertEqual(payload["market_map"], [])
            self.assertEqual(payload["market_bias"], {})
            self.assertEqual(payload["long_signals"], [])
            self.assertEqual(payload["long_watchlist"], [])
            self.assertEqual(
                payload["horizon_suppressed_reasons"],
                {"SHORT": "ERROR", "LONG": "ERROR"},
            )
            self.assertEqual(payload["scan_unavailable_horizons"], ["LONG", "SHORT"])
            self.assertEqual(len(persisted.signals), 1)
            self.assertEqual(len(persisted.long_signals), 1)

    def test_per_horizon_freshness_marks_only_old_preserved_radar_expired(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RadarRuntime(
                ModeAwareScanner(),
                AppConfig(data_dir=directory, stale_after_seconds=1800),
            )
            current = report()
            current.signals[0] = allow_entry(current.signals[0])
            long_signal = allow_entry(signal())
            long_signal.radar_horizon = "LONG"
            current.long_signals = [long_signal]
            current.scan_mode = "SHORT"
            current.short_completed_at = current.completed_at
            current.long_completed_at = (
                datetime.now(timezone.utc) - timedelta(minutes=31)
            ).isoformat()
            runtime._latest = current

            payload = runtime.latest_dict()

            self.assertTrue(payload["actionable"])
            self.assertFalse(payload["horizon_freshness"]["SHORT"]["expired"])
            self.assertTrue(payload["horizon_freshness"]["LONG"]["expired"])
            self.assertTrue(payload["safety"]["horizon_actionable"]["SHORT"])
            self.assertFalse(payload["safety"]["horizon_actionable"]["LONG"])
            self.assertEqual(len(payload["long_signals"]), 1)
            self.assertEqual(payload["horizon_read_only_reasons"]["LONG"], "STALE")
            self.assertFalse(payload["long_signals"][0]["actionable"])
            self.assertFalse(
                payload["long_signals"][0]["decision_context"]["final"][
                    "new_entry_allowed"
                ]
            )
            self.assertTrue(
                current.long_signals[0].decision_context["final"]["new_entry_allowed"]
            )

    def test_preflight_rejects_expired_preserved_horizon_after_partial_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RadarRuntime(
                ModeAwareScanner(),
                AppConfig(data_dir=directory, stale_after_seconds=1800),
            )
            current = report()
            long_signal = signal()
            long_signal.radar_horizon = "LONG"
            current.long_signals = [long_signal]
            current.scan_mode = "SHORT"
            current.short_completed_at = current.completed_at
            current.long_completed_at = (
                datetime.now(timezone.utc) - timedelta(minutes=31)
            ).isoformat()
            runtime._latest = current

            with self.assertRaises(PreflightError) as raised:
                runtime.preflight_dict("AAA-USDT-SWAP", "LONG")

            self.assertIn("4H 資料已過期", str(raised.exception))

    def test_runtime_restores_latest_report_without_forced_rescan(self):
        with tempfile.TemporaryDirectory() as directory:
            saved = report()
            save_report(saved, directory)
            runtime = RadarRuntime(ImmediateScanner(), AppConfig(data_dir=directory))

            self.assertEqual(runtime.status()["system_status"], "FRESH")
            self.assertEqual(runtime.status()["last_attempt_status"], "RESTORED")
            self.assertEqual(len(runtime.latest_dict()["signals"]), 1)

    def test_core_preview_is_available_while_deep_scan_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            scanner = PreviewScanner()
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))
            self.assertTrue(runtime.trigger_scan())
            self.assertTrue(scanner.preview_ready.wait(1))

            preview = runtime.preview_dict()
            self.assertIsNotNone(preview)
            self.assertEqual(preview["runtime_status"], "CORE_PREVIEW")
            self.assertFalse(preview["actionable"])
            self.assertFalse(preview["safety"]["horizon_actionable"]["SHORT"])
            self.assertTrue(runtime.status()["has_preview"])

            scanner.release.set()
            deadline = time.time() + 2
            while runtime.status()["running"] and time.time() < deadline:
                time.sleep(0.01)
            self.assertIsNone(runtime.preview_dict())
            self.assertEqual(runtime.status()["system_status"], "FRESH")

    def test_short_preview_keeps_the_existing_long_horizon_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            scanner = PreviewScanner()
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))
            previous = report()
            previous_long = signal()
            previous_long.radar_horizon = "LONG"
            previous.long_signals = [previous_long]
            previous.short_completed_at = previous.completed_at
            previous.long_completed_at = previous.completed_at
            runtime._latest = previous

            self.assertTrue(runtime.trigger_scan(scan_mode="SHORT"))
            self.assertTrue(scanner.preview_ready.wait(1))
            preview = runtime.preview_dict()

            self.assertEqual(preview["scan_request_mode"], "SHORT")
            self.assertEqual(len(preview["long_signals"]), 1)
            self.assertEqual(
                preview["horizon_freshness"]["LONG"]["completed_at"],
                previous.long_completed_at,
            )
            self.assertTrue(preview["safety"]["horizon_actionable"]["LONG"])

            scanner.release.set()
            deadline = time.time() + 2
            while runtime.status()["running"] and time.time() < deadline:
                time.sleep(0.01)

    def test_full_preview_shows_current_short_core_without_previous_long_cards(self):
        with tempfile.TemporaryDirectory() as directory:
            scanner = PreviewScanner()
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))
            previous = report()
            previous.signals[0] = allow_entry(previous.signals[0])
            previous_long = allow_entry(signal())
            previous_long.radar_horizon = "LONG"
            previous.long_signals = [previous_long]
            previous.short_completed_at = previous.completed_at
            previous.long_completed_at = previous.completed_at
            runtime._latest = previous

            self.assertTrue(runtime.trigger_scan(scan_mode="FULL"))
            self.assertTrue(scanner.preview_ready.wait(1))
            preview = runtime.preview_dict()

            self.assertEqual(len(preview["signals"]), 1)
            self.assertEqual(preview["long_signals"], [])
            self.assertEqual(preview["long_watchlist"], [])
            self.assertFalse(preview["horizon_freshness"]["LONG"]["available"])
            self.assertFalse(preview["safety"]["horizon_actionable"]["SHORT"])
            self.assertFalse(preview["safety"]["horizon_actionable"]["LONG"])
            self.assertEqual(
                preview["horizon_read_only_reasons"],
                {"SHORT": "CORE_PREVIEW", "LONG": None},
            )
            self.assertEqual(
                preview["horizon_suppressed_reasons"],
                {"SHORT": None, "LONG": "CORE_PREVIEW"},
            )
            self.assertFalse(preview["signals"][0]["actionable"])
            self.assertTrue(
                previous_long.decision_context["final"]["new_entry_allowed"]
            )

            scanner.release.set()
            deadline = time.time() + 2
            while runtime.status()["running"] and time.time() < deadline:
                time.sleep(0.01)

    def test_scan_lock_joins_existing_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            scanner = BlockingScanner()
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))
            self.assertTrue(runtime.trigger_scan())
            self.assertTrue(scanner.started.wait(1))
            self.assertFalse(runtime.trigger_scan())
            self.assertEqual(runtime.status()["system_status"], "SCANNING")
            scanner.release.set()
            deadline = time.time() + 2
            while runtime.status()["running"] and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(scanner.calls, 1)
            self.assertEqual(runtime.status()["system_status"], "FRESH")

    def test_scanning_hides_only_the_requested_previous_horizon(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RadarRuntime(ImmediateScanner(), AppConfig(data_dir=directory))
            previous = report()
            previous.signals[0] = allow_entry(previous.signals[0])
            previous_long = allow_entry(signal())
            previous_long.radar_horizon = "LONG"
            previous.long_signals = [previous_long]
            previous_state = MarketState(
                inst_id="BBB-USDT-SWAP",
                regime="TREND",
                direction="LONG",
                preferred_strategy="fixture",
                readiness_score=70.0,
                status="NEAR_TRIGGER",
                missing_conditions=[],
                spread_pct=0.01,
                quote_volume_24h=10_000_000,
                closed_candle_ts=1,
            )
            previous.watchlist = [previous_state]
            previous.long_watchlist = [previous_state]
            previous.market_map = [previous_state]
            previous.market_regime_counts = {"TREND": 1}
            previous.market_bias = {"label": "上一輪偏多"}
            previous.short_completed_at = previous.completed_at
            previous.long_completed_at = previous.completed_at
            runtime._latest = previous
            runtime._running = True
            runtime._scan_mode = "SHORT"
            payload = runtime.latest_dict()
            self.assertEqual(payload["runtime_status"], "SCANNING")
            self.assertFalse(payload["actionable"])
            self.assertEqual(payload["signals"], [])
            self.assertEqual(payload["watchlist"], [])
            self.assertEqual(payload["market_map"], [])
            self.assertEqual(payload["market_regime_counts"], {})
            self.assertEqual(payload["market_bias"], {})
            self.assertEqual(len(payload["long_signals"]), 1)
            self.assertEqual(len(payload["long_watchlist"]), 1)
            self.assertEqual(payload["historical_signal_count"], 0)
            self.assertFalse(payload["safety"]["horizon_actionable"]["SHORT"])
            self.assertTrue(payload["safety"]["horizon_actionable"]["LONG"])
            self.assertEqual(payload["scan_in_progress_horizons"], ["SHORT"])
            self.assertEqual(
                payload["horizon_read_only_reasons"],
                {"SHORT": "SCANNING", "LONG": None},
            )
            self.assertEqual(
                payload["horizon_suppressed_reasons"],
                {"SHORT": "SCANNING", "LONG": None},
            )
            self.assertEqual(payload["signals_suppressed_reason"], "SCANNING")
            self.assertTrue(
                payload["long_signals"][0]["decision_context"]["final"][
                    "new_entry_allowed"
                ]
            )
            self.assertTrue(
                previous.signals[0].decision_context["final"]["new_entry_allowed"]
            )

            runtime._scan_mode = "LONG"
            payload = runtime.latest_dict()
            self.assertEqual(len(payload["signals"]), 1)
            self.assertEqual(payload["long_signals"], [])
            self.assertEqual(payload["long_watchlist"], [])
            self.assertTrue(payload["safety"]["horizon_actionable"]["SHORT"])
            self.assertFalse(payload["safety"]["horizon_actionable"]["LONG"])
            self.assertEqual(
                payload["horizon_read_only_reasons"],
                {"SHORT": None, "LONG": "SCANNING"},
            )
            self.assertEqual(
                payload["horizon_suppressed_reasons"],
                {"SHORT": None, "LONG": "SCANNING"},
            )
            self.assertTrue(
                payload["signals"][0]["decision_context"]["final"][
                    "new_entry_allowed"
                ]
            )

            runtime._scan_mode = "FULL"
            payload = runtime.latest_dict()
            self.assertEqual(payload["signals"], [])
            self.assertEqual(payload["watchlist"], [])
            self.assertEqual(payload["market_map"], [])
            self.assertEqual(payload["long_signals"], [])
            self.assertEqual(payload["long_watchlist"], [])
            self.assertEqual(
                payload["horizon_suppressed_reasons"],
                {"SHORT": "SCANNING", "LONG": "SCANNING"},
            )
            self.assertEqual(payload["scan_in_progress_horizons"], ["LONG", "SHORT"])
            self.assertTrue(
                previous.signals[0].decision_context["final"]["new_entry_allowed"]
            )
            self.assertTrue(
                previous_long.decision_context["final"]["new_entry_allowed"]
            )

    def test_report_older_than_thirty_minutes_is_retained_as_expired_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RadarRuntime(
                ImmediateScanner(),
                AppConfig(data_dir=directory, stale_after_seconds=1800),
            )
            old = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
            runtime._latest = report(old)
            runtime._latest.signals[0] = allow_entry(runtime._latest.signals[0])
            self.assertEqual(runtime.status()["system_status"], "STALE")
            payload = runtime.latest_dict()
            self.assertFalse(payload["actionable"])
            self.assertTrue(payload["snapshot_expired"])
            self.assertEqual(len(payload["signals"]), 1)
            self.assertEqual(payload["signals"][0]["inst_id"], "AAA-USDT-SWAP")
            self.assertIsNone(payload["signals_suppressed_reason"])
            self.assertEqual(payload["signals_read_only_reason"], "STALE")
            self.assertEqual(
                payload["horizon_suppressed_reasons"],
                {"SHORT": None, "LONG": None},
            )
            self.assertFalse(payload["safety"]["actionable"])
            self.assertFalse(payload["signals"][0]["actionable"])
            self.assertFalse(
                payload["signals"][0]["decision_context"]["final"][
                    "new_entry_allowed"
                ]
            )
            self.assertEqual(
                payload["signals"][0]["decision_context"]["final"]["status"],
                "EXPIRED",
            )
            self.assertEqual(
                payload["signals"][0]["decision_context"]["final"][
                    "original_final_status"
                ],
                "ENTER",
            )
            self.assertTrue(
                runtime._latest.signals[0].decision_context["final"][
                    "new_entry_allowed"
                ]
            )
            markdown = runtime.latest_markdown()
            self.assertIn("資料已過期", markdown)
            self.assertIn("AAA-USDT-SWAP", markdown)

    def test_failed_attempt_keeps_stale_as_primary_safety_state(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RadarRuntime(
                ImmediateScanner(),
                AppConfig(data_dir=directory, stale_after_seconds=1800),
            )
            old = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
            runtime._latest = report(old)
            runtime._last_attempt_status = "ERROR"
            runtime._last_error = "fixture failure"
            status = runtime.status()
            self.assertEqual(status["system_status"], "STALE")
            self.assertEqual(status["last_error"], "fixture failure")
            payload = runtime.latest_dict()
            self.assertTrue(payload["snapshot_expired"])
            self.assertFalse(payload["actionable"])
            self.assertEqual(len(payload["signals"]), 1)

    def test_successful_scan_uses_completion_time_and_is_actionable(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RadarRuntime(ImmediateScanner(), AppConfig(data_dir=directory))
            completed = runtime.scan_blocking()
            self.assertEqual(completed.generated_at, completed.completed_at)
            self.assertEqual(runtime.status()["system_status"], "FRESH")
            self.assertEqual(len(runtime.latest_dict()["signals"]), 1)


if __name__ == "__main__":
    unittest.main()
