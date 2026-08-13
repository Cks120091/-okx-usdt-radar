import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from radar.config import AppConfig
from radar.models import RadarReport, Signal
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


class RuntimeSafetyTests(unittest.TestCase):
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

    def test_report_older_than_thirty_minutes_is_stale_and_masked(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RadarRuntime(
                ImmediateScanner(),
                AppConfig(data_dir=directory, stale_after_seconds=1800),
            )
            old = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
            runtime._latest = report(old)
            self.assertEqual(runtime.status()["system_status"], "STALE")
            self.assertEqual(runtime.latest_dict()["signals"], [])

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
            self.assertEqual(runtime.latest_dict()["signals"], [])

    def test_successful_scan_uses_completion_time_and_is_actionable(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = RadarRuntime(ImmediateScanner(), AppConfig(data_dir=directory))
            completed = runtime.scan_blocking()
            self.assertEqual(completed.generated_at, completed.completed_at)
            self.assertEqual(runtime.status()["system_status"], "FRESH")
            self.assertEqual(len(runtime.latest_dict()["signals"]), 1)


if __name__ == "__main__":
    unittest.main()
