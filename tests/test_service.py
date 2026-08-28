import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from radar.config import AppConfig
from radar.models import MarketState, RadarReport, Signal
from radar.reporting import save_report
from radar.service import RadarRuntime


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
    )


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

    def scan_instrument(self, inst_id, market_bias, btc_bias):
        self.calls.append((inst_id, market_bias, btc_bias))
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
            ticker=SimpleNamespace(last=100.5),
            context=SimpleNamespace(),
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


class FailingScanner:
    def scan_once(self, progress=None, scan_id=None):
        raise RuntimeError("fixture scan failure")


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
            self.assertIsNone(runtime._latest)
            self.assertEqual(scanner.release_calls, 1)

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
            self.assertTrue(preview["actionable"])
            self.assertTrue(runtime.status()["has_preview"])

            scanner.release.set()
            deadline = time.time() + 2
            while runtime.status()["running"] and time.time() < deadline:
                time.sleep(0.01)
            self.assertIsNone(runtime.preview_dict())
            self.assertEqual(runtime.status()["system_status"], "FRESH")

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

    def test_scanning_masks_previous_formal_signals(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RadarRuntime(ImmediateScanner(), AppConfig(data_dir=directory))
            runtime._latest = report()
            runtime._running = True
            payload = runtime.latest_dict()
            self.assertEqual(payload["runtime_status"], "SCANNING")
            self.assertFalse(payload["actionable"])
            self.assertEqual(payload["signals"], [])
            self.assertEqual(payload["historical_signal_count"], 1)

    def test_report_older_than_thirty_minutes_is_retained_as_expired_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RadarRuntime(
                ImmediateScanner(),
                AppConfig(data_dir=directory, stale_after_seconds=1800),
            )
            old = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
            runtime._latest = report(old)
            self.assertEqual(runtime.status()["system_status"], "STALE")
            payload = runtime.latest_dict()
            self.assertFalse(payload["actionable"])
            self.assertTrue(payload["snapshot_expired"])
            self.assertEqual(len(payload["signals"]), 1)
            self.assertEqual(payload["signals"][0]["inst_id"], "AAA-USDT-SWAP")
            self.assertIsNone(payload["signals_suppressed_reason"])
            self.assertEqual(payload["signals_read_only_reason"], "STALE")
            self.assertFalse(payload["safety"]["actionable"])
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
