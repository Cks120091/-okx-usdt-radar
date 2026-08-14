import tempfile
import unittest
from pathlib import Path

from radar.config import AppConfig


class V2ContractTests(unittest.TestCase):
    def test_v2_defaults_and_limits(self):
        config = AppConfig.load()
        self.assertEqual(config.max_signals, 20)
        self.assertEqual(config.context_candidates, 100)
        self.assertEqual(config.stale_after_seconds, 1800)
        self.assertEqual(config.candle_rate_limit_requests_per_2s, 14)
        self.assertEqual(config.auto_scan_cooldown_seconds, 120)
        self.assertEqual(config.manual_scan_cooldown_seconds, 0)
        self.assertEqual(config.core_recovery_attempts, 1)
        self.assertEqual(config.context_recovery_attempts, 1)
        self.assertEqual(config.recovery_workers, 2)
        self.assertFalse(config.require_micro_volume_anomaly)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text('{"max_signals": 21}', encoding="utf-8")
            with self.assertRaises(ValueError):
                AppConfig.load(str(path))

    def test_mobile_ui_triggers_real_scan_and_uses_chinese_lifecycle(self):
        html = (Path(__file__).parents[1] / "radar" / "static" / "pages.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("/api/scan", html)
        self.assertIn("method:'POST'", html)
        self.assertIn("bootstrap()", html)
        self.assertIn("立即掃描現在市場", html)
        self.assertIn("早期訊號", html)
        self.assertIn("完整確認", html)
        self.assertIn("資料已過期，禁止依此進場", html)
        self.assertIn("overflow-x:hidden", html)
        self.assertIn("env(safe-area-inset-bottom)", html)
        self.assertIn("<details>", html)
        self.assertNotIn("重新載入結果", html)
        self.assertNotIn("每 15 分鐘", html)
        self.assertNotIn("setInterval", html)
        self.assertIn("status.scan_id!==state.report.scan_id", html)
        self.assertIn("多空候選排行", html)
        self.assertIn("OI 異動雷達", html)
        self.assertIn("市場平均 RSI", html)
        self.assertIn("localStorage", html)
        self.assertIn("TradingView 圖表", html)
        self.assertIn("搜尋幣種，例如 BTC、SNDK", html)
        self.assertIn("renderOverviewUnavailable", html)
        self.assertIn("JSON.stringify({reason})", html)
        self.assertIn("startScan('auto')", html)
        self.assertIn("startScan('manual')", html)
        self.assertIn("PARTIAL_CONTEXT", html)
        self.assertIn("部分深度資料缺漏", html)

    def test_github_actions_contains_no_market_schedule_or_scan(self):
        root = Path(__file__).parents[1]
        workflows = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (root / ".github" / "workflows").glob("*.yml")
        )
        self.assertNotIn("schedule:", workflows)
        self.assertNotIn("run.py --once", workflows)
        self.assertNotIn("cron:", workflows)


if __name__ == "__main__":
    unittest.main()
