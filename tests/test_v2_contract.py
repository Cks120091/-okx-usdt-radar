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
        self.assertIn("尚無市場報告，請按「立即掃描現在市場」", html)
        self.assertIn("真實歷史績效", html)
        self.assertIn("manifest.webmanifest", html)
        self.assertIn("serviceWorker.register", html)
        self.assertIn("/api/push/config", html)
        self.assertIn("pushManager.subscribe", html)
        self.assertIn("Notification.requestPermission", html)
        self.assertIn("push_subscription", html)
        self.assertIn("notification_registered", html)
        self.assertIn("$('#pushButton').addEventListener('click',togglePushNotifications)", html)
        self.assertIn("分享 → 加入主畫面", html)
        self.assertIn("資料已過期，禁止依此進場", html)
        self.assertIn("overflow-x:hidden", html)
        self.assertIn("env(safe-area-inset-bottom)", html)
        self.assertIn("height:100dvh", html)
        self.assertIn("grid-template-rows:auto auto minmax(0,1fr) auto auto", html)
        self.assertIn(".top{grid-row:1;position:relative", html)
        self.assertIn(".filter-shell{grid-row:2;position:relative", html)
        self.assertIn(".shell{grid-row:3;min-height:0", html)
        self.assertIn(".primary-nav{grid-row:5;position:relative", html)
        self.assertIn("--primary-nav-safe-bottom", html)
        self.assertIn(".action-wrap{display:none;grid-row:4;position:relative", html)
        self.assertIn("$('.shell').scrollTo({top:0", html)
        self.assertLess(html.index('<header class="top">'), html.index('<main class="shell">'))
        self.assertLess(html.index('id="filterShell"'), html.index('<main class="shell">'))
        self.assertNotIn(".top{position:sticky", html)
        self.assertNotIn(".filter-shell{position:sticky", html)
        self.assertNotIn(".primary-nav{position:fixed", html)
        self.assertNotIn("window.scrollTo", html)
        self.assertIn("<details>", html)
        self.assertNotIn("重新載入結果", html)
        self.assertNotIn("setInterval", html)
        self.assertIn("status.latest_generated_at!==state.report.generated_at", html)
        self.assertIn("if(status.has_report)await loadReport()", html)
        bootstrap = html.split("async function bootstrap(){", 1)[1].split(
            "async function refreshFreshness", 1
        )[0]
        self.assertNotIn("startScan", bootstrap)
        self.assertNotIn("requestPermission", bootstrap)
        freshness_poll = html.split("async function refreshFreshness(){", 1)[1].split(
            "const tabGroups", 1
        )[0]
        self.assertNotIn("startScan", freshness_poll)
        self.assertNotIn("autoStarted", html)
        self.assertIn("$('#scanButton').addEventListener('click',startScan)", html)
        self.assertIn("$('#refreshButton').addEventListener('click',startScan)", html)
        self.assertIn("/api/report/preview", html)
        self.assertIn("多空候選排行", html)
        self.assertIn("交易品質不會代替價格 Trigger", html)
        self.assertIn("訊號準備度", html)
        self.assertIn("尚未觸發", html)
        self.assertIn("可進 · ${watchCount} 觀察", html)
        self.assertIn("item.entry_eligibility?.status==='ENTRY_READY'", html)
        self.assertIn("OI 異動雷達", html)
        self.assertIn("市場平均 RSI", html)
        self.assertIn("localStorage", html)
        self.assertIn("TradingView 圖表", html)
        self.assertIn("搜尋幣種，例如 BTC、SNDK", html)
        self.assertIn("renderOverviewUnavailable", html)
        self.assertNotIn("開發者資料（Raw Data）", html)
        self.assertNotIn("分組績效 JSON", html)
        self.assertNotIn("raw_indicators", html)
        self.assertIn("進場前更新", html)
        self.assertIn("進場檢查", html)
        self.assertIn("/api/preflight", html)
        self.assertIn("只更新這一個訊號，不重新掃描全市場", html)
        self.assertIn("原始 Trigger 沒有被修改", html)
        self.assertIn("data-preflight-id", html)
        self.assertIn("$('#preflightRefresh').addEventListener('click',loadPreflight)", html)
        self.assertIn("selector==='#signalsBox'||selector==='#longReadySignalsBox'", html)
        self.assertIn("15m 短線歷史", html)
        self.assertIn("4H 長線歷史", html)
        self.assertIn("24 小時內", html)
        self.assertIn("7 天內", html)
        self.assertIn("不因 TP／SL 或走遠而提前消失", html)
        self.assertIn("function historyGroups(items)", html)
        self.assertIn("觸發 ${events.length} 次", html)
        self.assertIn("點開查看每次觸發時間與原始價位", html)
        self.assertIn("${shortCoins} 幣 / ${shortItems.length} 次", html)
        self.assertIn("/api/history?limit=60", html)
        self.assertIn("只按原始觸發時間輪替", (
            Path(__file__).parents[1] / "radar" / "service.py"
        ).read_text(encoding="utf-8"))

    def test_pwa_never_caches_live_market_api(self):
        root = Path(__file__).parents[1] / "radar" / "static"
        worker = (root / "service-worker.js").read_text(encoding="utf-8")
        manifest = (root / "manifest.webmanifest").read_text(encoding="utf-8")
        self.assertIn("/api/", worker)
        self.assertIn('fetch(event.request, {cache: "no-store"})', worker)
        self.assertIn('self.addEventListener("push"', worker)
        self.assertIn('self.addEventListener("notificationclick"', worker)
        self.assertIn("showNotification", worker)
        self.assertIn("openWindow", worker)
        shell_assets = worker.split("SHELL_ASSETS", 1)[1].split("];", 1)[0]
        self.assertNotIn("/api/", shell_assets)
        self.assertIn('"display": "standalone"', manifest)

    def test_market_scan_has_no_github_schedule(self):
        root = Path(__file__).parents[1]
        workflows = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (root / ".github" / "workflows").glob("*.yml")
        )
        self.assertNotIn("schedule:", workflows)
        self.assertNotIn("cron:", workflows)
        self.assertNotIn("/api/scan", workflows)


if __name__ == "__main__":
    unittest.main()
