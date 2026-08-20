import tempfile
import unittest
from pathlib import Path

from radar.config import AppConfig


class V33ContractTests(unittest.TestCase):
    def test_v33_defaults_and_limits(self):
        config = AppConfig.load()
        self.assertEqual(config.max_signals, 20)
        self.assertEqual(config.context_candidates, 100)
        self.assertEqual(config.workers, 12)
        self.assertEqual(config.rate_limit_requests_per_2s, 30)
        self.assertEqual(config.candle_limit_1d, 200)
        self.assertEqual(config.universe_max_spread_pct, 1.0)
        self.assertEqual(config.stale_after_seconds, 1800)
        self.assertEqual(config.early_signal_max_age_bars, 2)
        self.assertEqual(config.entry_ready_max_chase_atr, 0.15)
        self.assertEqual(config.entry_missed_chase_atr, 0.50)
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
        self.assertIn("15m 早期", html)
        self.assertIn("目前可進", html)
        self.assertIn("等待回踩", html)
        self.assertIn("已錯過", html)
        self.assertIn("entry_eligibility", html)
        self.assertIn("長線訊號", html)
        self.assertIn("長線目前可進", html)
        self.assertIn("longEarlySignals", html)
        self.assertIn("longReadySignals", html)
        self.assertIn("longWaitRetest", html)
        self.assertIn("longMissedSignals", html)
        self.assertIn("補充中", html)
        self.assertIn("雷達服務剛啟動，正在建立第一份市場資料", html)
        self.assertIn("真實歷史績效", html)
        self.assertIn("manifest.webmanifest", html)
        self.assertIn("serviceWorker.register", html)
        self.assertIn("資料已過期，禁止依此進場", html)
        self.assertIn("overflow-x:hidden", html)
        self.assertIn("env(safe-area-inset-bottom)", html)
        self.assertIn("<details>", html)
        self.assertNotIn("重新載入結果", html)
        self.assertNotIn("setInterval", html)
        self.assertIn("status.latest_generated_at!==state.report.generated_at", html)
        self.assertIn("if(status.has_report)await loadReport()", html)
        self.assertIn("/api/report/preview", html)
        self.assertIn("多空候選排行", html)
        self.assertIn("OI 異動雷達", html)
        self.assertIn("市場平均 RSI", html)
        self.assertIn("localStorage", html)
        self.assertIn("TradingView 圖表", html)
        self.assertIn("搜尋幣種，例如 BTC、SNDK", html)
        self.assertIn("renderOverviewUnavailable", html)

    def test_pwa_never_caches_live_market_api(self):
        root = Path(__file__).parents[1] / "radar" / "static"
        worker = (root / "service-worker.js").read_text(encoding="utf-8")
        manifest = (root / "manifest.webmanifest").read_text(encoding="utf-8")
        self.assertIn("/api/", worker)
        self.assertIn('fetch(event.request, {cache: "no-store"})', worker)
        shell_assets = worker.split("SHELL_ASSETS", 1)[1].split("];", 1)[0]
        self.assertNotIn("/api/", shell_assets)
        self.assertIn('"display": "standalone"', manifest)

    def test_github_actions_triggers_scan_after_each_15m_close(self):
        root = Path(__file__).parents[1]
        workflows = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (root / ".github" / "workflows").glob("*.yml")
        )
        self.assertIn("schedule:", workflows)
        self.assertIn('cron: "1,16,31,46 * * * *"', workflows)
        self.assertIn("/api/scan", workflows)
        self.assertNotIn("run.py --once", workflows)


if __name__ == "__main__":
    unittest.main()
