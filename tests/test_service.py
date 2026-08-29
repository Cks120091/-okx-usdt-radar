import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from radar.config import AppConfig
from radar.models import MarketContext, MarketState, RadarReport, Signal, Ticker
from radar.reporting import load_latest_report, save_report
from radar.service import (
    PreflightError,
    RadarRuntime,
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

    def test_opposite_direction_requires_two_step_confirmation_before_invalidation(self):
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
        self.assertEqual(warning["status"], "OPPOSITE_WARNING")

        opposite_signal = signal()
        opposite_signal.direction = "SHORT"
        opposite_signal.market_story = {
            "trigger": {"event_index": 8, "confirmation_index": 9}
        }
        confirmed = _latest_confirmation(
            SimpleNamespace(signal=opposite_signal, market_state=state),
            "LONG",
        )
        self.assertEqual(confirmed["status"], "OPPOSITE_WARNING")
        self.assertFalse(confirmed["two_step_reversal_confirmed"])
        self.assertFalse(confirmed["new_entry_allowed"])

    def test_confirmation_merge_blocks_warning_but_does_not_close_existing_plan(self):
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
                "status": "OPPOSITE_WARNING",
                "message": "反向證據尚未二次確認",
            },
        )

        self.assertEqual(merged["verdict"]["status"], "WAIT_RETEST")
        self.assertFalse(merged["verdict"]["actionable"])
        self.assertTrue(merged["signal_lifecycle"]["active"])
        self.assertTrue(merged["plan_state"]["existing_position_plan_active"])
        self.assertFalse(merged["latest_confirmation"]["new_entry_allowed"])

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
                "WAIT_RETEST",
            )
            self.assertTrue(payload["short"]["preflight"]["safety"]["unified_single_scan"])

    def test_single_scan_failures_explain_connection_and_kline_states(self):
        timeout = RuntimeError("The read operation timed out")
        self.assertIn("不是幣種或訊號失效", _single_scan_failure_message(timeout))
        self.assertIn("官方主端點與備援端點", _single_scan_failure_message(timeout))

        candle = RuntimeError("15m K 線取得失敗")
        self.assertIn("不是訊號失效", _single_scan_failure_message(candle))

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
            runtime = RadarRuntime(ImmediateScanner(), AppConfig(data_dir=directory))
            runtime._latest = current

            full_size = len(json.dumps(current.to_dict(), ensure_ascii=False))
            payload = runtime.latest_dict()
            public_size = len(json.dumps(payload, ensure_ascii=False))

            self.assertNotIn("target_instruments", payload)
            self.assertNotIn("api_metrics", payload)
            self.assertNotIn("long_market_map", payload)
            self.assertNotIn("raw_indicators", payload["signals"][0]["market_metrics"])
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
            self.assertEqual(
                payload["signals"][0]["lifecycle"],
                {
                    "age_bars": 1,
                    "triggered_at": "2026-08-27T15:46:18+00:00",
                },
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
            self.assertEqual(len(payload["signals"]), 1)
            self.assertEqual(len(payload["long_signals"]), 1)
            self.assertEqual(payload["scan_unavailable_horizons"], ["SHORT"])
            self.assertFalse(payload["safety"]["horizon_actionable"]["SHORT"])
            self.assertTrue(payload["safety"]["horizon_actionable"]["LONG"])
            self.assertEqual(payload["horizon_read_only_reasons"]["SHORT"], "ERROR")
            self.assertIsNone(payload["horizon_read_only_reasons"]["LONG"])
            self.assertFalse(payload["signals"][0]["actionable"])
            self.assertFalse(
                payload["signals"][0]["decision_context"]["final"][
                    "new_entry_allowed"
                ]
            )
            self.assertEqual(
                payload["signals"][0]["decision_context"]["final"]["status"],
                "UPDATE_FAILED",
            )
            self.assertEqual(
                payload["signals"][0]["decision_context"]["final"][
                    "original_final_status"
                ],
                "ENTER",
            )
            self.assertFalse(
                payload["signals"][0]["entry_eligibility"]["new_entry_allowed"]
            )
            self.assertTrue(
                payload["long_signals"][0]["decision_context"]["final"][
                    "new_entry_allowed"
                ]
            )
            self.assertTrue(previous.signals[0].actionable)
            self.assertTrue(
                previous.signals[0].decision_context["final"]["new_entry_allowed"]
            )
            self.assertEqual(len(persisted.long_signals), 1)
            self.assertEqual(persisted.completed_at, previous.completed_at)

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

    def test_full_preview_keeps_previous_long_reference_but_disables_both_horizons(self):
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
            self.assertEqual(len(preview["long_signals"]), 1)
            self.assertFalse(preview["safety"]["horizon_actionable"]["SHORT"])
            self.assertFalse(preview["safety"]["horizon_actionable"]["LONG"])
            self.assertEqual(
                preview["horizon_read_only_reasons"],
                {"SHORT": "CORE_PREVIEW", "LONG": "CORE_PREVIEW"},
            )
            self.assertFalse(preview["signals"][0]["actionable"])
            self.assertFalse(preview["long_signals"][0]["actionable"])
            self.assertFalse(
                preview["long_signals"][0]["decision_context"]["final"][
                    "new_entry_allowed"
                ]
            )
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

    def test_partial_scanning_keeps_requested_horizon_visible_but_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RadarRuntime(ImmediateScanner(), AppConfig(data_dir=directory))
            previous = report()
            previous.signals[0] = allow_entry(previous.signals[0])
            previous_long = allow_entry(signal())
            previous_long.radar_horizon = "LONG"
            previous.long_signals = [previous_long]
            previous.short_completed_at = previous.completed_at
            previous.long_completed_at = previous.completed_at
            runtime._latest = previous
            runtime._running = True
            runtime._scan_mode = "SHORT"
            payload = runtime.latest_dict()
            self.assertEqual(payload["runtime_status"], "SCANNING")
            self.assertFalse(payload["actionable"])
            self.assertEqual(len(payload["signals"]), 1)
            self.assertEqual(len(payload["long_signals"]), 1)
            self.assertEqual(payload["historical_signal_count"], 1)
            self.assertFalse(payload["safety"]["horizon_actionable"]["SHORT"])
            self.assertTrue(payload["safety"]["horizon_actionable"]["LONG"])
            self.assertEqual(payload["scan_in_progress_horizons"], ["SHORT"])
            self.assertEqual(
                payload["horizon_read_only_reasons"],
                {"SHORT": "SCANNING", "LONG": None},
            )
            self.assertFalse(payload["signals"][0]["actionable"])
            self.assertFalse(
                payload["signals"][0]["decision_context"]["final"][
                    "new_entry_allowed"
                ]
            )
            self.assertEqual(
                payload["signals"][0]["decision_context"]["final"]["status"],
                "WAIT",
            )
            self.assertEqual(
                payload["signals"][0]["decision_context"]["final"][
                    "original_final_status"
                ],
                "ENTER",
            )
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
            self.assertEqual(len(payload["long_signals"]), 1)
            self.assertTrue(payload["safety"]["horizon_actionable"]["SHORT"])
            self.assertFalse(payload["safety"]["horizon_actionable"]["LONG"])
            self.assertEqual(
                payload["horizon_read_only_reasons"],
                {"SHORT": None, "LONG": "SCANNING"},
            )
            self.assertTrue(
                payload["signals"][0]["decision_context"]["final"][
                    "new_entry_allowed"
                ]
            )
            self.assertFalse(payload["long_signals"][0]["actionable"])
            self.assertFalse(
                payload["long_signals"][0]["decision_context"]["final"][
                    "new_entry_allowed"
                ]
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
