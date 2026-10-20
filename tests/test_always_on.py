"""Offline contract tests for the opt-in always-on public market watcher."""
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
import unittest

from radar.always_on import AlwaysOnMonitor, MonitorConfig


@dataclass
class FakeState:
    inst_id: str
    direction: str = "LONG"
    quote_volume_24h: float = 3_000_000


@dataclass
class FakeReport:
    status: str = "NO_QUALIFIED_SIGNAL"
    target_instruments: list[str] = field(default_factory=lambda: [
        "SOL-USDT-SWAP", "BTC-USDT-SWAP", "ETH-USDT-SWAP"])
    watchlist: list[FakeState] = field(default_factory=lambda: [FakeState("SOL-USDT-SWAP")])
    long_watchlist: list[FakeState] = field(default_factory=list)
    signals: list[FakeState] = field(default_factory=list)
    long_signals: list[FakeState] = field(default_factory=list)
    market_map: list[FakeState] = field(default_factory=lambda: [
        FakeState("BTC-USDT-SWAP", quote_volume_24h=10_000_000)])
    long_market_map: list[FakeState] = field(default_factory=list)


class FakeRuntime:
    def __init__(self, path):
        self.config = SimpleNamespace(data_dir=str(path))
        self.scanned = []

    def scan_blocking(self, mode):
        self.scanned.append(mode)
        return FakeReport()


def ticker(symbol, ts, price):
    return json.dumps({
        "arg": {"channel": "tickers"},
        "data": [{"instId": symbol, "ts": str(ts), "last": str(price)}],
    })


class AlwaysOnTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = [1_800_000_000.0]
        self.runtime = FakeRuntime(self.temp.name)
        self.monitor = AlwaysOnMonitor(self.runtime, clock=lambda: self.now[0])
        self.monitor.refresh_targets(FakeReport())

    def test_select_eligible_only(self):
        report = FakeReport()
        report.market_map.append(FakeState("DOGE-USDT-SWAP", quote_volume_24h=1e12))
        self.monitor = AlwaysOnMonitor(self.runtime, MonitorConfig(max_symbols=2))
        self.monitor.refresh_targets(report)
        self.assertEqual(self.monitor._targets, ("SOL-USDT-SWAP", "BTC-USDT-SWAP"))
        self.assertNotIn("DOGE-USDT-SWAP", self.monitor._targets)

    def test_warning_is_advisory_and_deduplicated(self):
        self.monitor.handle_frame(ticker("SOL-USDT-SWAP", int(self.now[0]*1000), 100))
        self.now[0] += 61
        self.monitor.handle_frame(ticker("SOL-USDT-SWAP", int(self.now[0]*1000), 100.6))
        result = self.monitor.status()["recent_warnings"]
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["advisory_only"])
        self.assertFalse(result[0]["entry_permitted"])
        self.now[0] += 61
        self.monitor.handle_frame(ticker("SOL-USDT-SWAP", int(self.now[0]*1000), 101))
        self.assertEqual(len(self.monitor.status()["recent_warnings"]), 1)
        path = Path(self.temp.name) / "monitor_warnings.jsonl"
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)

    def test_invalid_stale_future_and_duplicate_are_ignored(self):
        frames = [
            "{invalid", "pong", '{"event":"subscribe","arg":{"channel":"tickers"}}',
            '{"arg":null,"data":[]}',
            ticker("DOGE-USDT-SWAP", int(self.now[0]*1000), 100),
            ticker("SOL-USDT-SWAP", int((self.now[0]-40)*1000), 100),
            ticker("SOL-USDT-SWAP", int((self.now[0]+5)*1000), 100),
            ticker("SOL-USDT-SWAP", int(self.now[0]*1000), 0),
        ]
        for frame in frames:
            self.monitor.handle_frame(frame)
        self.assertIsNone(self.monitor.status()["last_tick_at"])
        self.monitor.handle_frame(ticker("SOL-USDT-SWAP", int(self.now[0]*1000), 100))
        self.monitor.handle_frame(ticker("SOL-USDT-SWAP", int(self.now[0]*1000), 120))
        self.assertEqual(len(self.monitor._samples["SOL-USDT-SWAP"]), 1)

    def test_opposite_watchlist_does_not_warn(self):
        self.monitor.refresh_targets(FakeReport(watchlist=[FakeState("SOL-USDT-SWAP", "SHORT")]))
        self.monitor.handle_frame(ticker("SOL-USDT-SWAP", int(self.now[0]*1000), 100))
        self.now[0] += 90
        self.monitor.handle_frame(ticker("SOL-USDT-SWAP", int(self.now[0]*1000), 101))
        self.assertFalse(self.monitor.status()["recent_warnings"])

    def test_config_rejects_unbounded_or_external_feed(self):
        for config in (
            MonitorConfig(max_symbols=10000),
            MonitorConfig(websocket_url="wss://evil.example/ws/v5/public"),
            MonitorConfig(websocket_url="wss://ws.okx.com:8443/ws/v5/public"),
            MonitorConfig(advisory_move_pct=-1),
        ):
            with self.assertRaises(ValueError):
                AlwaysOnMonitor(self.runtime, config)

    def test_swing_scan_runs_on_hourly_trigger_close(self):
        self.assertEqual(self.monitor.config.long_scan_seconds, 3600)
        self.monitor._last_success["SHORT"] = self.now[0]
        self.monitor._last_success["LONG"] = self.now[0] - 3601
        class StopOnScan:
            def is_set(inner): return len(self.runtime.scanned) > 0
            def wait(inner, _seconds): return None
        self.monitor._stop = StopOnScan()
        self.monitor._scan_loop()
        self.assertEqual(self.runtime.scanned, ["LONG"])

    def test_full_universe_candidate_is_observation_only_and_liquidity_gated(self):
        from types import SimpleNamespace
        class PublicClient:
            def get_usdt_swap_instruments(inner):
                return [SimpleNamespace(inst_id="LIQUID-USDT-SWAP", state="live"),
                        SimpleNamespace(inst_id="THIN-USDT-SWAP", state="live")]
            def get_swap_tickers(inner):
                return {
                    "LIQUID-USDT-SWAP": SimpleNamespace(quote_volume_24h=3_000_000),
                    "THIN-USDT-SWAP": SimpleNamespace(quote_volume_24h=100_000),
                }
        self.runtime.scanner = SimpleNamespace(client=PublicClient())
        monitor = AlwaysOnMonitor(
            self.runtime, MonitorConfig(full_universe=True),
            clock=lambda: self.now[0],
        )
        # Use isolated sample frames; no websocket socket is opened here.
        stream = monitor._full_stream
        stream._symbols = ("LIQUID-USDT-SWAP", "THIN-USDT-SWAP")
        stream._volumes = {"LIQUID-USDT-SWAP": 3_000_000,
                           "THIN-USDT-SWAP": 100_000}
        for symbol in stream.symbols:
            monitor.handle_frame(ticker(symbol, int(self.now[0] * 1000), 100))
        self.now[0] += 61
        monitor.handle_frame(ticker("LIQUID-USDT-SWAP", int(self.now[0] * 1000), 100.8))
        monitor.handle_frame(ticker("THIN-USDT-SWAP", int(self.now[0] * 1000), 110))
        candidates = monitor.status()["recent_candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["symbol"], "LIQUID-USDT-SWAP")
        self.assertTrue(candidates[0]["advisory_only"])
        self.assertFalse(candidates[0]["entry_permitted"])
        self.assertEqual(candidates[0]["type"], "MARKET_PRICE_ACCELERATION_CANDIDATE")

    def test_scan_uses_existing_runtime_without_order(self):
        class StopOnScan:
            def is_set(inner): return len(self.runtime.scanned) > 0
            def wait(inner, _seconds): return None
        self.monitor._stop = StopOnScan()
        self.monitor._scan_loop()
        self.assertEqual(self.runtime.scanned, ["FULL"])
        self.assertIn("SOL-USDT-SWAP", self.monitor._targets)


if __name__ == "__main__":
    unittest.main()
