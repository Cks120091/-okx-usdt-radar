import tempfile
import unittest
from pathlib import Path

from radar.config import AppConfig
from radar.decision import DEFAULT_THRESHOLDS
from radar.scanner import ScannerConfig
from radar.strategy import StrategyConfig


class V33ContractTests(unittest.TestCase):
    def test_v33_defaults_and_limits(self):
        config = AppConfig.load()
        self.assertEqual(config.max_signals, 20)
        self.assertEqual(config.context_candidates, 100)
        self.assertEqual(config.workers, 12)
        self.assertEqual(config.rate_limit_requests_per_2s, 30)
        self.assertEqual(config.candle_limit_1d, 200)
        self.assertEqual(config.min_quote_volume_24h, 2_000_000.0)
        self.assertEqual(config.quote_volume_buffer_24h, 500_000.0)
        self.assertEqual(ScannerConfig().min_quote_volume_24h, 2_000_000.0)
        self.assertEqual(ScannerConfig().quote_volume_buffer_24h, 500_000.0)
        self.assertEqual(StrategyConfig().min_quote_volume_24h, 2_000_000.0)
        self.assertEqual(
            DEFAULT_THRESHOLDS["min_quote_volume_24h"],
            2_000_000.0,
        )
        self.assertEqual(config.universe_max_spread_pct, 1.0)
        self.assertEqual(config.stale_after_seconds, 1800)
        self.assertEqual(config.early_signal_max_age_bars, 2)
        self.assertEqual(config.entry_ready_max_chase_atr, 0.15)
        self.assertEqual(config.entry_missed_chase_atr, 0.50)
        self.assertEqual(config.max_execution_cost_to_risk_pct, 15.0)
        self.assertEqual(ScannerConfig().max_execution_cost_to_risk_pct, 15.0)
        self.assertEqual(StrategyConfig().max_execution_cost_to_risk_pct, 15.0)
        self.assertEqual(
            DEFAULT_THRESHOLDS["max_execution_cost_to_risk_pct"],
            15.0,
        )
        self.assertFalse(config.require_micro_volume_anomaly)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text('{"max_signals": 21}', encoding="utf-8")
            with self.assertRaises(ValueError):
                AppConfig.load(str(path))
            path.write_text(
                '{"min_quote_volume_24h": 0}',
                encoding="utf-8",
            )
            disabled = AppConfig.load(str(path))
            self.assertEqual(disabled.min_quote_volume_24h, 0)
            self.assertEqual(disabled.quote_volume_buffer_24h, 500_000.0)
            for invalid_volume in ("NaN", "Infinity"):
                with self.subTest(invalid_volume=invalid_volume):
                    path.write_text(
                        f'{{"min_quote_volume_24h": {invalid_volume}}}',
                        encoding="utf-8",
                    )
                    with self.assertRaises(ValueError):
                        AppConfig.load(str(path))
            path.write_text(
                '{"min_quote_volume_24h": 2000000, '
                '"quote_volume_buffer_24h": 2500000}',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                AppConfig.load(str(path))

    def test_mobile_ui_triggers_real_scan_and_uses_chinese_lifecycle(self):
        html = (Path(__file__).parents[1] / "radar" / "static" / "pages.html").read_text(
            encoding="utf-8"
        )
        service_worker = (
            Path(__file__).parents[1] / "radar" / "static" / "service-worker.js"
        ).read_text(encoding="utf-8")
        self.assertIn("/api/scan", html)
        self.assertIn("method:'POST'", html)
        self.assertIn("bootstrap()", html)
        self.assertIn("全市場掃描（15m＋4H）", html)
        self.assertIn("早期訊號", html)
        self.assertIn("完整確認", html)
        self.assertIn("15m 早期", html)
        self.assertIn("目前可進", html)
        self.assertIn("等待回踩", html)
        self.assertIn("已錯過", html)
        self.assertIn("entry_eligibility", html)
        self.assertIn("長線訊號", html)
        self.assertIn("4H 長線目前可進", html)
        self.assertIn("longEarlySignals", html)
        self.assertIn("longReadySignals", html)
        self.assertIn("longWaitRetest", html)
        self.assertIn("longMissedSignals", html)
        self.assertIn("補充中", html)
        self.assertIn("尚無市場報告，請選擇上方掃描範圍", html)
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
        self.assertIn("USDT PERPETUAL（永續合約） · MARKET INTELLIGENCE（市場情報）", html)
        self.assertIn("brand-mark-shell", html)
        self.assertIn("data-runtime-status", html)
        self.assertIn("content-visibility:auto", html)
        self.assertIn("CSS/SVG only, no heavy media assets", html)
        self.assertIn("Professional market command surface", html)
        self.assertIn('class="command-deck"', html)
        self.assertIn('aria-label="市場即時指揮台"', html)
        self.assertIn("expired-snapshot", html)
        self.assertIn("@media(prefers-reduced-motion:reduce)", html)
        self.assertNotIn("<video", html)
        self.assertIn("function displaySymbol(instId)", html)
        self.assertIn("replace(/-USDT-SWAP$/i,'')", html)
        self.assertIn("esc(displaySymbol(item.inst_id))", html)
        self.assertIn("`${displaySymbol(instId)} · ${horizon==='LONG'?'4H 長線':'15m 短線'}`", html)
        self.assertIn("資料已過期，禁止依此進場", html)
        self.assertIn("上一輪快照會繼續顯示", html)
        self.assertIn("function isExpiredSnapshot(item)", html)
        self.assertIn("⏱ 資料已過期", html)
        self.assertIn("資料已過期｜原快照", html)
        self.assertIn("幣種掃描", html)
        self.assertIn("/api/instrument/scan", html)
        self.assertIn("價格・OI・CVD 多週期判讀", html)
        self.assertIn("reportBecameStale", html)
        self.assertIn("overflow-x:hidden", html)
        self.assertIn("env(safe-area-inset-bottom)", html)
        self.assertIn("env(safe-area-inset-left)", html)
        self.assertIn("env(safe-area-inset-right)", html)
        self.assertIn("height:100dvh", html)
        self.assertIn("max-width:100vw", html)
        self.assertIn("grid-template-columns:minmax(0,1fr)", html)
        self.assertIn("grid-template-rows:auto auto minmax(0,1fr) auto auto", html)
        self.assertIn(".top{grid-row:1;position:relative", html)
        self.assertIn(".filter-shell{grid-row:2;position:relative", html)
        self.assertIn(".shell{grid-row:3;min-width:0;min-height:0;width:100%;max-width:980px", html)
        self.assertIn(".brand{display:flex;align-items:center;justify-content:space-between;gap:10px;min-width:0;width:100%;max-width:936px", html)
        self.assertIn(".filter-tabs{display:flex;gap:8px;min-width:0;width:100%;max-width:936px", html)
        self.assertIn(".filter-tab{flex:0 0 auto", html)
        self.assertIn(".primary-nav{grid-row:5;position:relative", html)
        self.assertIn("grid-template-columns:repeat(5,minmax(0,1fr))", html)
        self.assertIn(".decision-actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))", html)
        self.assertIn('<body data-active-group="home">', html)
        self.assertIn('body:not([data-active-group="home"]) .command-deck', html)
        self.assertIn("document.body.dataset.activeGroup=group", html)
        self.assertIn("okx-radar-shell-v4.5-history-replay-1", service_worker)
        self.assertIn("市場方向 · 24H 全市場平均 RSI", html)
        self.assertIn("bias.market_average_rsi", html)
        self.assertIn("rsi24.market_rsi_24h_label", html)
        self.assertIn("rsi24Value", html)
        self.assertIn("report.short_completed_at", html)
        self.assertIn("report.long_completed_at", html)
        self.assertIn("24H", html)
        self.assertIn("完整收線 OI 數量方向輔助（不計分）", html)
        self.assertIn("function preflightContinuation(data)", html)
        self.assertIn("原方向續走力道", html)
        self.assertIn("純輔助 · 不影響進場資格", html)
        self.assertNotIn("continuation.score", html)
        self.assertIn("市場自動計畫", html)
        self.assertIn("plan.adaptive_market_plan", html)
        self.assertIn("plan.market_plan_sources", html)
        self.assertIn("Trigger 後固定原始計畫", html)
        self.assertIn("<title>OKX 雷達 V3.4</title>", html)
        self.assertIn("OKX 雷達 <span>V3.4</span>", html)
        self.assertNotIn('data-tab="pendingSignals"', html)
        self.assertNotIn('data-tab="longPendingSignals"', html)
        self.assertNotIn('id="pendingSignalsBox"', html)
        self.assertNotIn('id="longPendingSignalsBox"', html)
        self.assertNotIn("function isPendingConfirmationSignal(item)", html)
        self.assertIn("function itemDecisionContext", html)
        self.assertNotIn("function decisionContextStatus", html)
        self.assertNotIn("function finalDecisionPanel", html)
        self.assertNotIn("唯一 Final Decision", html)
        self.assertNotIn("Conflict（反向證據）", html)
        self.assertNotIn("Confidence（信心）", html)
        self.assertNotIn("二次反轉確認", html)
        self.assertIn("--primary-nav-safe-bottom", html)
        self.assertIn(".action-wrap{display:none;grid-row:4;position:relative", html)
        self.assertIn("@media(orientation:landscape) and (max-height:520px)", html)
        self.assertIn('class="preflight-action-bar"', html)
        self.assertLess(
            html.index('id="preflightContent"'), html.index('id="preflightRefresh"')
        )
        self.assertNotIn('id="instrumentContent"', html)
        self.assertNotIn('id="instrumentScan"', html)
        self.assertIn("$('.shell').scrollTo({top:0", html)
        self.assertLess(html.index('<header class="top">'), html.index('<main id="appContent" class="shell"'))
        self.assertLess(html.index('id="filterShell"'), html.index('<main id="appContent" class="shell"'))
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
        self.assertIn("horizonFreshnessChanged", freshness_poll)
        self.assertIn("status.horizon_freshness?.[horizon]", freshness_poll)
        self.assertIn("reportBecameStale||horizonFreshnessChanged", freshness_poll)
        self.assertNotIn("autoStarted", html)
        self.assertIn("$$('[data-scan-mode]').forEach", html)
        self.assertIn("scan_mode:normalizedMode", html)
        self.assertIn("/api/report/preview", html)
        self.assertIn("function horizonSnapshot(report,horizon)", html)
        self.assertIn("function renderHorizonUnavailable(horizon,message)", html)
        self.assertIn("function renderScanPending(mode,message)", html)
        self.assertIn("只更新所選週期，另一週期保持不變", html)
        self.assertIn("只更新所選週期，另一週期保持不變", html)
        self.assertIn("尚未執行 15m 掃描", html)
        self.assertIn("尚未執行 4H 掃描", html)
        start_scan = html.split("async function startScan(mode='FULL'){", 1)[1].split(
            "async function pollUntilComplete", 1
        )[0]
        self.assertNotIn("state.report=null", start_scan)
        self.assertIn("目前最值得看", html)
        self.assertIn("依可進狀態與交易品質排列", html)
        self.assertIn("訊號準備度", html)
        self.assertIn("尚未觸發", html)
        self.assertIn("可進 · ${watchCount} 觀察", html)
        self.assertIn("function itemCurrentEntryReady(item)", html)
        self.assertIn("function itemWasEntryReady(item)", html)
        self.assertIn("OI（未平倉量）異動雷達", html)
        self.assertIn("市場方向分布", html)
        self.assertIn("市場平均 RSI", html)
        self.assertIn("localStorage", html)
        self.assertIn("TradingView（技術圖表）", html)
        self.assertIn("輸入正式訊號幣種，例如 BTC", html)
        self.assertIn("renderOverviewUnavailable", html)
        overview_unavailable = html.split(
            "function renderOverviewUnavailable(message)", 1
        )[1].split("function isRecord", 1)[0]
        self.assertIn("$('#marketHeat').innerHTML", overview_unavailable)
        self.assertNotIn("開發者資料（Raw Data）", html)
        self.assertNotIn("分組績效 JSON", html)
        self.assertNotIn("raw_indicators", html)
        self.assertIn("⚡ ${frame} 進場前更新", html)
        self.assertIn("更新進場判定與持倉動向", html)
        self.assertIn("進場檢查", html)
        self.assertIn("/api/preflight", html)
        self.assertIn("更新現價、進場距離、成交條件、續走力道與歷史 OI 持倉動向；不改寫方向或原 Entry／SL／TP", html)
        self.assertIn("原始 Trigger（價格觸發）沒有被修改", html)
        self.assertIn("data-preflight-id", html)
        self.assertIn("data-preflight-trigger-id", html)
        self.assertIn("expected_trigger_id:triggerId", html)
        self.assertIn("state.preflight?.triggerId===triggerId", html)
        self.assertIn("String(data?.trigger_id||'')!==triggerId", html)
        self.assertIn("function preflightPlainGuide(data)", html)
        self.assertIn("判定依據（簡要）", html)
        self.assertIn("現在怎麼做", html)
        self.assertIn("訊號含義", html)
        self.assertIn("失效條件", html)
        self.assertNotIn("失效與方向", html)
        self.assertIn("等待價格回到最佳進場點位", html)
        self.assertIn("CONTINUATION:'趨勢延續'", html)
        self.assertIn("status==='ENTRY_READY'", html)
        self.assertIn("status==='WAIT_RETEST'", html)
        self.assertIn("status==='PLAN_INVALIDATED'", html)
        self.assertIn("舊計畫失效。", html)
        self.assertNotIn("舊計畫失效；方向不會自動變成", html)
        self.assertIn("status==='MISSED_ENTRY'", html)
        self.assertIn("function signalTriggerTime(item)", html)
        self.assertIn("訊號觸發時間（台灣 UTC+8）", html)
        self.assertNotIn("status!=='ENTRY_READY'&&status!=='MISSED_ENTRY'", html)
        self.assertIn("okx-radar-shell-v4.5-history-replay-1", service_worker)
        self.assertIn("$('#preflightRefresh').addEventListener('click',loadPreflight)", html)
        self.assertIn("${decisionPanel(item)}", html)
        self.assertNotIn("showPreflight", html)
        self.assertIn("function preflightActions(instId,horizon='BOTH'", html)
        self.assertNotIn("/api/preflight/reanalyze", html)
        self.assertNotIn("reanalyzeMode", html)
        self.assertNotIn("ORIGINAL_DIRECTION_STABLE", html)
        self.assertNotIn("OPPOSITE_WARNING", html)
        self.assertNotIn("CONFIRMED_REVERSAL", html)
        self.assertNotIn("二次反轉確認", html)
        self.assertNotIn("重新分析最新多週期資料", html)
        self.assertIn("不重新掃描多週期 K 線", html)
        self.assertIn("訊號觸發時間（台灣 UTC+8）", html)
        self.assertIn("15m 短線歷史", html)
        self.assertIn("4H 長線歷史", html)
        self.assertIn("24 小時內", html)
        self.assertIn("7 天內", html)
        self.assertIn("不因 TP（止盈）／SL（止損）或走遠而提前消失", html)
        self.assertIn("function historyGroups(items)", html)
        self.assertIn("目前有效新訊號 · 不會更新下方舊紀錄", html)
        self.assertIn("觸發 ${events.length} 次", html)
        self.assertIn("點開查看每次觸發時間與原始進出場價位", html)
        self.assertIn("${shortCoins} 幣 / ${shortItems.length} 次", html)
        self.assertIn("/api/history?limit=60", html)
        service_source = (
            Path(__file__).parents[1] / "radar" / "service.py"
        ).read_text(encoding="utf-8")
        self.assertIn("只按原始觸發時間輪替", service_source)
        retired_route = service_source.split(
            'if path == "/api/instrument/scan":', 1
        )[1].split('if path == "/api/preflight/reanalyze":', 1)[0]
        self.assertIn("HTTPStatus.OK", retired_route)
        self.assertIn("direction_lock", retired_route)
        self.assertIn("runtime.scan_instrument_dict", retired_route)
        self.assertIn("幣種掃描", html)
        self.assertIn("/api/instrument/scan", html)
        self.assertIn("價格・OI・CVD 多週期判讀", html)
        self.assertIn("/api/instrument/scan", html)
        self.assertNotIn("data-instrument-id", html)
        self.assertNotIn("function openInstrument", html)
        self.assertNotIn("function scanInstrument", html)
        self.assertNotIn("instrumentSideCache", html)
        self.assertNotIn("single_scan_analyzed_at", html)
        self.assertIn("function activePreflightSignal(instId,horizon)", html)
        self.assertIn("function preferredPreflightSignal(instId)", html)
        self.assertIn("function preflightButton(instId,horizon,item=null,compact=true)", html)
        self.assertIn("function preflightActions(instId,horizon='BOTH'", html)
        self.assertIn("preflightActions(item.inst_id", html)
        self.assertIn("尚無正式交易計畫", html)
        self.assertIn("舊交易計畫已結束", html)
        self.assertIn("等待正式掃描結果", html)
        self.assertIn("更新市場後可檢查", html)
        self.assertIn("目前沒有可執行進場前更新的正式交易計畫", html)
        self.assertIn("function planTargetR(item,targetValue,fallback=null)", html)
        self.assertIn("function pricePrecision(context)", html)
        self.assertIn("const authoritative=metricNumber(fallback)", html)
        self.assertIn("item?.tp1_r??item?.risk_reward", html)
        self.assertIn("item?.tp2_r??item?.management_plan?.tp2_rr_model", html)
        self.assertIn("function tradeRoute(item,options={})", html)
        self.assertIn("ENTRY 進場", html)
        self.assertIn("SL 止損", html)
        self.assertIn("TP1 止盈", html)
        self.assertIn("TP2 止盈", html)
        self.assertIn("2～4R", html)
        self.assertIn("7R", html)
        self.assertIn("最高 8R", html)
        self.assertNotIn("<canvas", html)
        self.assertIn("最佳進場點位", html)
        self.assertIn("已觸發・有效中", html)
        self.assertIn("現在能否進場", html)
        self.assertIn("尚未進場", html)
        self.assertIn("已經進場", html)
        self.assertIn("等待回踩」不是出場指令", html)
        self.assertIn("容許回測", html)
        self.assertIn("現在位置與進場資格", html)
        self.assertIn("原始進出場價格（固定，不被本次更新改寫）", html)
        self.assertIn("交易品質變化（不是勝率）", html)
        self.assertIn("Spread（買賣價差）", html)
        self.assertIn("R:R（風險報酬比）", html)
        self.assertIn("Order Book（委託簿）", html)
        self.assertIn("Order Book（訂單簿）", html)
        self.assertIn("(?:（(?:委託簿|訂單簿)）)*", html)
        self.assertNotIn("Trade Quality（交易品質）", html)
        self.assertIn("Execution Quality（執行品質，不是勝率）", html)
        self.assertNotIn("quality.combined_score", html)
        self.assertNotIn("'分層判讀'", html)
        self.assertIn("function technicalText(value)", html)
        self.assertIn('data-scan-mode="SHORT"', html)
        self.assertIn('data-scan-mode="LONG"', html)
        self.assertIn('data-scan-mode="FULL"', html)
        self.assertIn("function signalSortComparator(a,b)", html)
        comparator_start = html.index("function signalSortComparator")
        comparator_end = html.index("function terminalSortComparator", comparator_start)
        comparator = html[comparator_start:comparator_end]
        self.assertNotIn("continuationDiff", comparator)
        self.assertLess(
            comparator.index("readyDiff="),
            comparator.index("qualityDiff="),
        )
        self.assertLess(
            comparator.index("qualityDiff="),
            comparator.index("statusDiff="),
        )
        self.assertLess(
            comparator.index("statusDiff="),
            comparator.index("freshDiff="),
        )
        self.assertIn("Europe/London", html)
        self.assertIn("America/New_York", html)
        self.assertIn("Asia/Taipei", html)
        self.assertIn("function wallTimeInstant", html)
        self.assertIn("function refreshSessions", html)
        self.assertIn('data-tab="manual"', html)
        self.assertIn('id="manual"', html)
        self.assertIn("使用手冊", html)
        self.assertIn('<details class="manual-card">', html)
        self.assertIn("Signal Episode（訊號生命週期）", html)
        self.assertIn("交易品質／安全檢查", html)
        self.assertNotIn("Trade Quality（交易品質）／Confidence（信心）", html)
        self.assertIn("禁止追價／交易計畫失效", html)
        self.assertNotIn("多空衝突／轉弱與翻向", html)
        self.assertIn("三大交易時段", html)
        self.assertIn("詳細數據", html)
        self.assertIn("歷史訊號", html)
        self.assertIn("COMPLETED:'交易計畫完成'", html)
        self.assertIn("stage==='EXTENDED'||stage==='TRENDING'||stage==='COMPLETED'", html)
        self.assertIn("該週期先不顯示上一輪卡片", html)
        self.assertIn("另一週期若仍是 Fresh（最新）就照常有效", html)
        self.assertIn("STALE（超過 30 分鐘）", html)
        self.assertIn("4H 判斷方向", html)
        self.assertIn("1H 判斷背景／形態", html)
        self.assertIn("15m 作為 Trigger 與入場時間", html)
        self.assertIn('id="contextCountLabel">深度資料完整', html)
        self.assertIn('id="contextSourceCoverage">來源完整率 —', html)
        self.assertIn("function renderContextCoverage(report,transient=null,preview=false)", html)
        self.assertIn("quality.deep_complete_count", html)
        self.assertIn("quality.deep_source_completeness_pct", html)
        self.assertIn("五項來源完整率", html)
        self.assertNotIn("isolatedInstrumentSide", html)
        self.assertIn("@media(prefers-reduced-motion:reduce)", html)
        self.assertNotIn("<canvas", html)

    def test_complete_interface_system_is_clear_accessible_and_data_preserving(self):
        html = (Path(__file__).parents[1] / "radar" / "static" / "pages.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("Complete interface system", html)
        self.assertIn('class="home-data-health"', html)
        self.assertIn('id="nearCount"', html)
        self.assertIn('id="contextCount"', html)
        self.assertIn('class="manual-section"', html)
        self.assertIn("開始使用", html)
        self.assertIn("讀懂交易計畫", html)
        self.assertIn("市場與紀錄", html)
        self.assertIn("觀察中｜尚未觸發", html)
        self.assertIn("terminal-profit", html)
        self.assertIn("terminal-loss", html)
        self.assertIn("terminal-unknown", html)
        self.assertIn('role="dialog" aria-modal="true"', html)
        self.assertIn('id="preflightAnnouncer"', html)
        self.assertIn('aria-label="搜尋市場地圖幣種"', html)
        self.assertIn('aria-label="15m 短線"', html)
        self.assertIn('aria-label="4H 長線"', html)
        self.assertIn("function setAppInert(enabled)", html)
        self.assertIn("function setScanResultsBusy(scanning,mode='FULL')", html)
        self.assertIn("event.key==='Escape'", html)
        self.assertIn("aria-current", html)
        self.assertIn("document.body.dataset.activeTab=id", html)
        self.assertIn('id="homeEmptyState"', html)
        self.assertIn("function setHomeReportEmpty", html)
        self.assertIn('body.home-report-empty[data-active-tab="overview"] #overview', html)
        self.assertIn('body[data-active-tab="manual"] .search-tools', html)
        self.assertIn("目前可進優先 · 同狀態品質高 → 低", html)
        self.assertIn("NEW:'新訊號週期'", html)
        self.assertIn("calc((100vw - var(--layout-max) + 28px)/2)", html)
        self.assertIn("function preflightPositionMetric(data)", html)
        self.assertIn("function preflightRiskMetric(data)", html)
        self.assertIn("位於 Entry 不利側", html)
        self.assertIn("已越過原始 SL", html)
        self.assertIn("只分析、不下單", html)

        terminal = html.split("function terminalSignalOutcome(item)", 1)[1].split(
            "function isTerminalSignal", 1
        )[0]
        self.assertIn("terminalStates.includes('CLOSED_UNKNOWN')", terminal)
        self.assertLess(
            terminal.index("terminalStates.includes('CLOSED_UNKNOWN')"),
            terminal.index("'INVALIDATED'"),
        )
        render_signals = html.split("function renderSignals(items", 1)[1].split(
            "function renderWatchlist", 1
        )[0]
        self.assertIn("terminal?'terminal-unknown':''", render_signals)
        self.assertIn(
            "terminal==='CLOSED_UNKNOWN'?'結果未知':'交易結果'", render_signals
        )
        decision = html.split("function decisionPanel(item)", 1)[1].split(
            "function timeframeGrid", 1
        )[0]
        self.assertIn("isLoss?'stopped':'closed'", decision)
        self.assertIn("交易計畫已關閉｜結果未知", decision)
        self.assertIn("複製原始紀錄（不可沿用）", decision)

        rankings = html.split("function renderRankings(signals,watchlist)", 1)[1].split(
            "function renderFavorites", 1
        )[0]
        self.assertIn("itemCurrentEntryReady(item)", rankings)
        self.assertIn("&&!isExpiredSnapshot(item)&&!itemReadOnlyReason(item)", rankings)
        self.assertIn("readyCount=rows.filter(isReady).length", rankings)
        self.assertIn("entryStatus=itemEntryStatus(item)", rankings)
        self.assertIn("?'先不要進'", rankings)
        self.assertIn("?'等回踩／確認'", rankings)
        self.assertIn("?'已錯過｜勿追價'", rankings)

        continuation = html.split("function preflightContinuation(data)", 1)[1].split(
            "function preflightCapitalFlow", 1
        )[0]
        self.assertIn("rawWindow=String(current.primary_window||'')", continuation)
        self.assertNotIn("horizon==='LONG'?'60m':'10m'", continuation)
        self.assertIn("本次輔助資料未取得", continuation)
        self.assertIn("基準視窗未建立", continuation)
        preflight_capital = html.split("function preflightCapitalFlow(data)", 1)[
            1
        ].split("function preflightPositionMetric", 1)[0]
        self.assertIn("continuation.current", preflight_capital)
        self.assertIn("current.capital_flow", preflight_capital)
        self.assertIn("continuation.refresh_failed===true?null", preflight_capital)
        self.assertNotIn("continuation.original", preflight_capital)
        self.assertIn("source:'PREFLIGHT'", preflight_capital)
        self.assertIn("referenceTime:data?.live?.sampled_at", preflight_capital)
        preflight_auxiliary = html.split("function preflightAuxiliary(data)", 1)[
            1
        ].split("function preflightPositionMetric", 1)[0]
        self.assertIn("${preflightContinuation(data)}", preflight_auxiliary)
        self.assertIn("${preflightCapitalFlow(data)}", preflight_auxiliary)
        self.assertIn("dataPageButton", preflight_auxiliary)
        self.assertIn("intradayFlowPanel(flow)", preflight_auxiliary)
        self.assertIn("exitReviewPanel(data.exit_review", preflight_auxiliary)
        self.assertIn('id="dataDetailDialog"', html)
        self.assertIn("function preflightTerminalKind(data)", html)
        self.assertIn("statuses.includes('CLOSED_UNKNOWN')", html)
        self.assertIn("return Boolean(preflightTerminalKind(data))", html)
        self.assertIn("OI／成交流只作續走力道輔助", html)
        self.assertIn("此頁僅供輔助，不改變正式方向", html)
        self.assertIn("不能繞過成交額門檻", html)
        self.assertNotIn("不受全市場成交額緩衝帶限制", html)

        preflight = html.split("function renderPreflight(data){", 1)[1].split(
            "function preflightResponseTerminal", 1
        )[0]
        self.assertLess(
            preflight.index("原始進出場價格（固定，不被本次更新改寫）"),
            preflight.index("現在位置與進場資格"),
        )
        self.assertLess(
            preflight.index("現在位置與進場資格"),
            preflight.index("${preflightAuxiliary(data)}"),
        )
        self.assertLess(
            preflight.index("${preflightAuxiliary(data)}"),
            preflight.index('class="preflight-disclosure"'),
        )

    def test_scan_round_hides_requested_horizons_but_stale_cards_are_retained(self):
        html = (Path(__file__).parents[1] / "radar" / "static" / "pages.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("horizon_read_only_reasons", html)
        self.assertIn("function horizonReadOnlyReason(report,horizon)", html)
        self.assertIn("function horizonAttempt(status,horizon)", html)
        self.assertIn("function readOnlyReferenceBanner(reason,horizon)", html)
        self.assertIn("function itemDisplayEntryStatus(item)", html)
        stored_status = html.split("function itemStoredEntryStatus(item)", 1)[1].split(
            "function itemDisplayEntryStatus", 1
        )[0]
        self.assertIn("eligibility.original_status", stored_status)
        self.assertIn("eligibility.original_status||eligibility.status", stored_status)
        self.assertNotIn("original_final_status", stored_status)
        self.assertNotIn("decision_context", stored_status)
        self.assertNotIn("掃描中｜上一輪結果只供參考", html)
        self.assertNotIn("更新失敗｜上一輪結果只供參考", html)
        self.assertNotIn("下方保留上一輪", html)
        self.assertIn("正在掃描本輪資料；完成後會直接顯示最新結果", html)
        self.assertIn("掃描失敗；此週期目前不顯示訊號", html)
        self.assertIn("下方快照只供回看，更新確認前不可進場", html)
        self.assertIn("class=\"read-only-reference", html)

        pending = html.split("function renderScanPending(mode,message){", 1)[1].split(
            "function renderUnavailable", 1
        )[0]
        self.assertIn(
            "if(state.report&&(shortAvailable||longAvailable)){renderReport(state.report);return}",
            pending,
        )

        readonly_reason = html.split(
            "function horizonReadOnlyReason(report,horizon){", 1
        )[1].split("function itemReadOnlyReason", 1)[0]
        self.assertIn("const attempt=horizonAttempt(state.status,horizon)", readonly_reason)
        self.assertIn(
            "attempt.available&&['SCANNING','ERROR'].includes(attempt.status)",
            readonly_reason,
        )
        self.assertGreaterEqual(readonly_reason.count("if(!attempt.available&&"), 2)
        self.assertIn(
            "!attempt.available||!['SCANNING','ERROR','CORE_PREVIEW'].includes(normalizedExplicit)",
            readonly_reason,
        )
        self.assertLess(
            readonly_reason.index("const currentRuntime="),
            readonly_reason.index("const explicit="),
        )
        self.assertIn("scanModeIncludesHorizon(currentMode,horizon)", readonly_reason)
        self.assertIn("reportHasCurrentPreview", readonly_reason)
        self.assertIn("if(snapshot.available&&snapshot.expired)return 'STALE'", readonly_reason)
        self.assertIn("return null", readonly_reason)

        transient = html.split("function horizonTransientState(report,horizon){", 1)[
            1
        ].split("function horizonTransientMessage", 1)[0]
        self.assertIn("attempt=horizonAttempt(state.status,horizon)", transient)
        self.assertIn("if(attempt.status==='ERROR')return 'ERROR'", transient)
        self.assertIn("if(attempt.status==='SCANNING')", transient)
        self.assertIn("return null}const reportRuntime", transient)
        self.assertIn("['SCANNING','ERROR'].includes(runtime)", transient)
        self.assertIn("scanModeIncludesHorizon(mode,horizon)", transient)
        self.assertIn("if(runtime==='ERROR')return 'ERROR'", transient)
        self.assertIn("reportHasCurrentPreview(report,horizon,mode)", transient)
        preview_scope = html.split(
            "function reportHasCurrentPreview(report,horizon", 1
        )[1].split("function horizonTransientState", 1)[0]
        self.assertIn("mode==='SHORT'||mode==='FULL'", preview_scope)
        self.assertIn("return mode==='LONG'", preview_scope)
        self.assertIn("state.currentPreviewGeneratedAt!==report.generated_at", preview_scope)
        render_status = html.split("function renderStatus(status){", 1)[1].split(
            "function horizonSnapshot", 1
        )[0]
        self.assertIn("horizonAttempt(status,horizon)", render_status)
        self.assertIn("item.status==='ERROR'", render_status)
        self.assertIn("horizon_attempt_errors", html)
        self.assertIn("只停用失敗週期，其他已完成週期保持可用", render_status)

        report = html.split("function renderReport(report){", 1)[1].split(
            "function renderOverview(report)", 1
        )[0]
        self.assertIn("shortAvailable=shortState.available", report)
        self.assertIn("longAvailable=longState.available", report)
        self.assertIn("shortReadOnlyReason=horizonReadOnlyReason(report,'SHORT')", report)
        self.assertIn("longReadOnlyReason=horizonReadOnlyReason(report,'LONG')", report)
        self.assertIn("shortTransient=horizonTransientState(report,'SHORT')", report)
        self.assertIn("longTransient=horizonTransientState(report,'LONG')", report)
        self.assertIn(
            "shortTransient?(shortTransient==='ERROR'?'更新失敗':'掃描中')",
            report,
        )
        self.assertIn(
            "longTransient?(longTransient==='ERROR'?'更新失敗':'掃描中')",
            report,
        )
        self.assertIn("if(shortTransient)", report)
        self.assertIn("if(longTransient)", report)
        coverage = html.split(
            "function renderContextCoverage(report,transient=null,preview=false){", 1
        )[1].split("function reportRenderFingerprint", 1)[0]
        self.assertIn("transient==='ERROR'?'更新失敗':'掃描中'", coverage)
        self.assertIn("liveReady=activeShort.filter(itemCurrentEntryReady)", report)
        self.assertNotIn("itemWasEntryReady", report)
        self.assertIn(
            "'目前沒有允許新進場的 15m 訊號。',shortReadOnlyReason",
            report,
        )
        self.assertIn("'目前沒有長線訊號。',longReadOnlyReason", report)
        self.assertIn(
            "renderContextCoverage(report,shortTransient||longTransient,preview)", report
        )
        self.assertIn(
            "early=liveReady.filter(x=>x.signal_stage==='EARLY_SIGNAL').sort(signalSortComparator)",
            report,
        )
        self.assertIn("allShort=[...activeShort].sort(signalSortComparator)", report)
        self.assertIn("ready=[...liveReady].sort(signalSortComparator)", report)
        self.assertIn(
            "waiting=activeShort.filter(x=>!isPreviewItem(x)&&itemWaitingForEntry(x)).sort(signalSortComparator)",
            report,
        )
        self.assertIn(
            "missed=activeShort.filter(x=>!isPreviewItem(x)&&itemEntryStatus(x)==='MISSED_ENTRY').sort(signalSortComparator)",
            report,
        )
        self.assertIn("allLong=[...activeLong].sort(signalSortComparator)", report)
        self.assertIn("liveLongReady=activeLong.filter(itemCurrentEntryReady)", report)
        self.assertIn("longReady=[...liveLongReady].sort(signalSortComparator)", report)
        self.assertIn(
            "longEarly=liveLongReady.filter(x=>x.signal_stage==='EARLY_SIGNAL').sort(signalSortComparator)",
            report,
        )
        self.assertIn(
            "longWaiting=activeLong.filter(x=>!isPreviewItem(x)&&itemWaitingForEntry(x)).sort(signalSortComparator)",
            report,
        )
        self.assertIn(
            "longMissed=activeLong.filter(x=>!isPreviewItem(x)&&itemEntryStatus(x)==='MISSED_ENTRY').sort(signalSortComparator)",
            report,
        )
        self.assertGreaterEqual(html.count("品質高 → 低"), 10)

        comparator = html.split("function signalSortComparator(a,b){", 1)[1].split(
            "function renderContextCoverage", 1
        )[0]
        self.assertIn("item.data_timestamp", comparator)
        self.assertIn("item.closed_candle_ts", comparator)
        self.assertIn(
            "quality=item=>metricNumber(item.execution_quality?.score)??Number.NEGATIVE_INFINITY",
            comparator,
        )
        self.assertNotIn("continuationRank", comparator)
        self.assertIn("readyDiff=Number(itemCurrentEntryReady(b))", comparator)
        self.assertLess(comparator.index("readyDiff"), comparator.index("qualityDiff"))
        self.assertLess(comparator.index("qualityDiff"), comparator.index("statusDiff"))
        self.assertLess(comparator.index("statusDiff"), comparator.index("dataTimeDiff"))
        self.assertLess(comparator.index("dataTimeDiff"), comparator.index("freshDiff"))
        self.assertLess(comparator.index("freshDiff"), comparator.index("rrDiff"))

        entry_status = html.split("function itemEntryStatus(item)", 1)[1].split(
            "function itemStoredEntryStatus", 1
        )[0]
        self.assertIn("eligibility.status", entry_status)
        self.assertIn("eligibility.wait_reason_code", entry_status)
        self.assertIn("itemHardGateBlocked(item)", entry_status)
        self.assertIn("final.new_entry_allowed===true", entry_status)
        self.assertIn("gateStatus!=='PASSED'", entry_status)
        self.assertIn("gate.passed!==true", entry_status)
        self.assertIn("eligibility.new_entry_allowed===true", entry_status)
        self.assertIn("eligibility.actionable===true", entry_status)
        self.assertIn("eligibility.new_entry_allowed===false", entry_status)
        self.assertIn("item?.actionable===false", entry_status)
        self.assertIn("itemFinalDecision(item)", entry_status)
        self.assertNotIn("eligibility.position_status||'ENTRY_READY'", entry_status)
        decision_panel = html.split("function decisionPanel(item", 1)[1].split(
            "function timeframeGrid", 1
        )[0]
        self.assertIn("const entry=item.entry_eligibility||{}", decision_panel)
        self.assertIn("decisionContext=itemDecisionContext(item)", decision_panel)
        self.assertIn("finalDecision=isRecord(decisionContext.final)", decision_panel)
        self.assertIn("hardGate=isRecord(decisionContext.hard_gate)", decision_panel)
        self.assertIn("decisionAlertHtml(item,status)", decision_panel)
        self.assertIn("先不要進場｜風險條件未通過", decision_panel)
        self.assertIn("preflightActions(item.inst_id,horizon,item,false)", decision_panel)
        self.assertIn("signalTradeGrid(item", decision_panel)
        self.assertIn("preview:true", decision_panel)
        self.assertNotIn("finalDecisionPanel", decision_panel)
        self.assertIn(
            "copyAction=item.entry_low&&item.stop_loss&&item.take_profit_1?",
            decision_panel,
        )
        self.assertNotIn("instrumentButton", decision_panel)
        self.assertIn("status==='HARD_GATE_BLOCKED'", decision_panel)
        self.assertIn("function decisionAlertItems(item)", html)
        self.assertIn("decisionExecutionNoticeHtml(item)", decision_panel)
        self.assertIn("function decisionExecutionNoticeHtml(item)", html)
        self.assertIn("decisionDirectionNoticeHtml(item)", decision_panel)
        self.assertIn("function decisionDirectionNoticeHtml(item)", html)
        self.assertIn("執行資料提醒｜不作為禁止條件", html)
        self.assertIn("滑價／成本暫時無法估算", html)
        alert_items = html.split("function decisionAlertItems(item)", 1)[1].split(
            "function decisionAlertHtml", 1
        )[0]
        self.assertIn("status||'').toUpperCase()==='BLOCKED'", alert_items)
        self.assertIn("check?.hard!==false", alert_items)
        self.assertIn("gate.reasons.slice(0,blockerCount)", alert_items)
        self.assertNotIn("conflict.items", alert_items)
        execution_notice = html.split(
            "function decisionExecutionNoticeHtml(item)", 1
        )[1].split("function decisionPanel", 1)[0]
        self.assertIn("check?.hard===false", execution_notice)
        self.assertIn("toUpperCase()==='UNKNOWN'", execution_notice)
        self.assertIn("['slippage','execution_cost']", execution_notice)
        self.assertNotIn("gate.unknowns", execution_notice)
        direction_notice = html.split(
            "function decisionDirectionNoticeHtml(item)", 1
        )[1].split("function decisionExecutionNoticeHtml", 1)[0]
        self.assertIn("方向衝突提醒｜不單獨禁止進場", direction_notice)
        self.assertIn("高週期方向與本卡相反，這是逆勢訊號", html)
        self.assertNotIn("function entryBadge(item)", html)
        self.assertIn("function reportRenderFingerprint(report)", html)
        fingerprint = html.split("function reportRenderFingerprint(report)", 1)[1].split(
            "function reportCardEntries", 1
        )[0]
        self.assertIn("item.entry_eligibility?.status", fingerprint)
        self.assertIn("item.entry_eligibility?.original_status", fingerprint)
        self.assertIn("item.lifecycle?.status", fingerprint)
        self.assertIn("decision.continuation_confirmation||{}", fingerprint)
        self.assertIn("final.new_entry_allowed", fingerprint)
        self.assertIn("hardGate.blocked", fingerprint)
        self.assertIn("conflict.countertrend", fingerprint)
        self.assertIn("continuation.key", fingerprint)
        self.assertIn("votes.OI?.state", fingerprint)
        self.assertIn("votes.TAKER_CVD?.state", fingerprint)
        self.assertIn("votes.VOLUME?.state", fingerprint)

        continuation = html.split("function continuationConfirmation(item)", 1)[1].split(
            "function directionBadge", 1
        )[0]
        self.assertIn("item?.decision_context?.continuation_confirmation", continuation)
        self.assertNotIn("function continuationBadge", continuation)
        self.assertNotIn("function continuationRank", continuation)
        self.assertNotIn("function continuationCoreVote(item,key)", html)
        self.assertIn("function continuationStrip(item)", html)
        self.assertIn("readOnlyReason=itemReadOnlyReason(item)", continuation)
        self.assertIn("terminalSignalOutcome(item)", continuation)
        self.assertIn("function continuationObserver(item)", continuation)
        self.assertIn("strength='強'", continuation)
        self.assertIn("strength='中等'", continuation)
        self.assertIn("strength='偏弱'", continuation)
        self.assertIn("strength='資料不足'", continuation)
        self.assertIn("status!=='INTERRUPTED'&&primaryReady", continuation)
        self.assertNotIn("else{strength='偏弱';cls='conflict'}", continuation)
        self.assertIn("key==='FORMING'", continuation)
        self.assertIn("key==='WEAK'", continuation)
        self.assertIn("knownCount>0", continuation)
        self.assertIn("${direction}續走力道", continuation)
        self.assertNotIn("continuation-window-note", continuation)
        self.assertNotIn("5／10m", continuation)
        self.assertNotIn("30／60m", continuation)
        self.assertNotIn("confirmation.score", continuation)
        continuation_markup = continuation.split("return `<section", 1)[1]
        self.assertNotIn("OI", continuation_markup)
        self.assertNotIn("Taker", continuation_markup)
        self.assertNotIn("成交量", continuation_markup)
        self.assertNotIn("加成", continuation_markup)
        self.assertIn("observer_updated_at", fingerprint)
        self.assertIn("observer.algorithm_version", fingerprint)
        self.assertIn("observer.source_mode", fingerprint)
        self.assertIn("observer.as_of_close_ms", fingerprint)
        self.assertIn("windows['5m']?.bucket_count", fingerprint)
        self.assertIn("windows['60m']?.bucket_count", fingerprint)
        self.assertIn("observer.capital_flow||{}", fingerprint)
        self.assertIn("capital.algorithm_version", fingerprint)
        self.assertIn("capitalWindows['1h']?.change_pct", fingerprint)
        self.assertIn("capitalWindows['2h']?.change_vs_average_ratio", fingerprint)
        self.assertIn("capitalWindows['4h']?.state", fingerprint)
        render_signals = html.split("function renderSignals(items", 1)[1].split(
            "function renderWatchlist", 1
        )[0]
        self.assertNotIn("${continuationBadge(item)}", render_signals)
        self.assertNotIn("${entryBadge(item)}", render_signals)
        self.assertNotIn("${continuationStrip(item)}", render_signals)
        self.assertIn("${currentEntryBadge(item)}", render_signals)
        self.assertIn("${decisionAuxiliary(item)}", decision_panel)
        decision_auxiliary = html.split("function decisionAuxiliary(item)", 1)[1].split(
            "function horizonBadge", 1
        )[0]
        self.assertNotIn("${continuationStrip(item)}", decision_auxiliary)
        self.assertIn('class="compact-context"', decision_auxiliary)
        self.assertIn("OI 只看增減倉，不單獨判多空", decision_auxiliary)
        full_auxiliary = html.split("function decisionAuxiliaryFull(item)", 1)[1].split("function intradayFlowPanel", 1)[0]
        self.assertIn("${continuationStrip(item)}", full_auxiliary)
        self.assertIn("${capitalFlowStrip(item)}", full_auxiliary)
        active_decision = decision_panel.split("const entry=item.entry_eligibility||{}", 1)[1]
        self.assertLess(
            active_decision.index("${signalTradeGrid(item)}"),
            active_decision.index("${decisionAuxiliary(item)}"),
        )
        self.assertLess(
            active_decision.index("${decisionAlertHtml(item,status)}")
            if "${decisionAlertHtml(item,status)}" in active_decision
            else active_decision.index("alertHtml=decisionAlertHtml(item,status)"),
            active_decision.index("${signalTradeGrid(item)}"),
        )
        self.assertIn("signal-status-line", render_signals)
        self.assertIn("現在能否進場", decision_panel)
        self.assertIn("目前可買價（Ask）", decision_panel)
        self.assertIn("目前可賣價（Bid）", decision_panel)
        self.assertIn("續走力道", continuation)
        self.assertNotIn("最高等級門檻", continuation)
        self.assertNotIn("加成", continuation)
        self.assertNotIn('role="progressbar"', continuation)
        self.assertIn("itemDataPage(item", render_signals)
        self.assertIn("details(item,true)", html.split("function itemDataPage(item",1)[1].split("function decisionAuxiliaryFull",1)[0])
        self.assertIn("function signalTradeGrid(item,options={})", html)
        self.assertIn("signal-plan-grid", html)
        self.assertIn("R:R｜${esc(rrLabel)}", html)
        self.assertIn("function signalMore(item,horizon,reason", html)
        self.assertIn("原因、時間與其他操作", html)
        self.assertNotIn("<small>", html.split("function signalTradeGrid(item,options={})", 1)[1].split("function signalTriggerTime", 1)[0])

        # 延續確認只負責說明與重繪，不參與排序，也不得變成進場硬門檻。
        self.assertNotIn("continuationConfirmation", entry_status)
        self.assertNotIn("continuationConfirmation", decision_panel)

        preflight_button = html.split("function preflightButton(instId,horizon", 1)[
            1
        ].split("function preflightActions", 1)[0]
        self.assertIn("activePreflightSignal(instId,normalized)", preflight_button)
        self.assertIn("preflightSignalUsable(signal)", preflight_button)
        self.assertIn("data-preflight-id", preflight_button)
        self.assertIn("data-preflight-trigger-id", preflight_button)
        self.assertIn("hasExplicitEpisode", preflight_button)
        self.assertIn("disabled title=", preflight_button)
        usable = html.split("function preflightSignalUsable(signal)", 1)[1].split(
            "function preferredPreflightSignal", 1
        )[0]
        self.assertIn("!isTerminalSignal(signal)", usable)
        self.assertIn("!isPreviewItem(signal)", usable)
        self.assertIn("!isExpiredSnapshot(signal)", usable)
        self.assertIn("!itemReadOnlyReason(signal)", usable)
        self.assertIn("!signalDataUnavailable(signal)", usable)
        preflight_actions = html.split("function preflightActions(instId", 1)[
            1
        ].split("function preflightClass", 1)[0]
        self.assertIn("activePreflightSignal(instId,'SHORT')", preflight_actions)
        self.assertIn("activePreflightSignal(instId,'LONG')", preflight_actions)
        self.assertIn("buttons.join('')", preflight_actions)
        load_preflight = html.split("async function loadPreflight(){", 1)[1].split(
            "function openPreflight", 1
        )[0]
        self.assertIn("expected_trigger_id:triggerId", load_preflight)
        self.assertIn("preflightResponseTerminal(data)", load_preflight)
        self.assertIn("state.preflight.terminal=true", load_preflight)
        self.assertIn("try{await loadReport()}", load_preflight)
        self.assertIn("const locked=state.preflight.terminal", load_preflight)
        open_preflight = html.split("function openPreflight", 1)[1].split(
            "function hidePreflight", 1
        )[0]
        self.assertIn("state.preflight={instId,horizon,triggerId", open_preflight)
        self.assertIn("$('#preflightPage .preflight-shell')", open_preflight)
        self.assertIn("function captureReportUiState()", html)
        self.assertIn("function captureReportUiState()", html)
        self.assertIn("function restoreReportUiState(saved)", html)
        self.assertIn("if(state.reportRenderKey===renderKey)return", report)
        preview_tail = report.split("if(preview){", 1)[1]
        self.assertLess(
            preview_tail.index("}"), preview_tail.index("state.reportRenderKey=renderKey")
        )
        self.assertLess(
            preview_tail.index("}"), preview_tail.index("restoreReportUiState(savedUi)")
        )
        poll = html.split("async function pollUntilComplete()", 1)[1].split(
            "function showConnectionError", 1
        )[0]
        self.assertIn(
            "state.scanStarting=false;state.currentPreviewGeneratedAt=null;await loadReport()",
            poll,
        )
        self.assertNotIn("longAwaitingFullPreview", report)
        self.assertIn("if(mode==='FULL')return horizon==='SHORT'", html)
        start_scan = html.split("async function startScan(mode='FULL'){", 1)[1].split(
            "async function pollUntilComplete", 1
        )[0]
        self.assertLess(
            start_scan.index("state.currentPreviewGeneratedAt=null"),
            start_scan.index("renderScanPending(normalizedMode,pendingMessage)"),
        )
        load_preview = html.split("async function loadPreview(){", 1)[1].split(
            "async function refreshStatus", 1
        )[0]
        self.assertLess(
            load_preview.index("state.currentPreviewGeneratedAt=report.generated_at"),
            load_preview.index("renderReport(report)"),
        )
        connection_error = html.split("function showConnectionError(error){", 1)[1].split(
            "async function bootstrap", 1
        )[0]
        self.assertIn(
            "scan_mode:state.scanRequestedMode||state.status?.scan_mode||'FULL'",
            connection_error,
        )

    def test_oi_ui_distinguishes_missing_baseline_and_below_threshold(self):
        html = (
            Path(__file__).parents[1] / "radar" / "static" / "pages.html"
        ).read_text(encoding="utf-8")

        availability = html.split("function oiAvailability(item)", 1)[1].split(
            "function oiInterpretation", 1
        )[0]
        self.assertIn("metricNumber(m.open_interest_usd)", availability)
        self.assertIn("metricNumber(m.open_interest_change_pct)", availability)
        self.assertIn("key:'MISSING'", availability)
        self.assertIn("key:'NO_BASELINE'", availability)
        self.assertIn("key:'COMPARED'", availability)
        self.assertIn("本輪未取得 OI USD 名目值", availability)
        self.assertIn("OI USD 名目值已取得，但尚無上一輪掃描基準", availability)
        self.assertIn("此數值含幣價影響", availability)

        anomaly = html.split("function oiAnomalyEmptyState(markets)", 1)[1].split(
            "function renderAnomalies", 1
        )[0]
        self.assertIn("目前沒有幣種達到異動門檻", anomaly)
        self.assertIn("但尚無上一輪掃描基準", anomaly)
        self.assertIn("noBaselineCount", anomaly)
        self.assertIn("已取得名目值、仍待下一輪基準", anomaly)
        self.assertIn("OI API 或該幣資料可能暫時不可用", anomaly)
        self.assertIn("oiAnomalyEmptyState(markets)", anomaly)
        self.assertNotIn("至少需要連續兩輪", anomaly)
        self.assertNotIn("需要至少兩輪掃描才能比較 OI", html)
        self.assertIn("OI 沒有「多單 OI」和「空單 OI」兩個獨立數字", html)
        self.assertIn("每張未平倉合約同時有一方做多、一方做空", html)
        self.assertIn("OI USD 名目值較上輪 ${signed", html)
        self.assertIn("Math.abs(move)<.05", html)
        self.assertNotIn("新增買方部位", html)
        self.assertNotIn("新增賣方部位", html)
        self.assertIn(".capital-flow-row{grid-template-columns:minmax(0,1fr)", html)

        continuation = html.split("function continuationStrip(item)", 1)[1].split(
            "function directionBadge", 1
        )[0]
        self.assertIn("續走力道", continuation)
        continuation_markup = continuation.split("return `<section", 1)[1]
        self.assertNotIn("OI", continuation_markup)
        self.assertNotIn("Taker", continuation_markup)
        self.assertNotIn("成交量", continuation_markup)
        self.assertNotIn("加成", continuation_markup)

    def test_signal_card_capital_flow_uses_scan_time_history_not_snapshot(self):
        html = (
            Path(__file__).parents[1] / "radar" / "static" / "pages.html"
        ).read_text(encoding="utf-8")

        capital = html.split("function capitalFlowData(item)", 1)[1].split(
            "function horizonBadge", 1
        )[0]
        self.assertIn(
            "decision_context?.continuation_confirmation?.observer?.capital_flow",
            capital,
        )
        self.assertIn("CAPITAL_FLOW_LOOKBACK_V1", capital)
        self.assertIn("function capitalFlowProfile(horizon)", capital)
        self.assertIn("{key:'1h',role:'近端持倉',period:'最近 1 小時'}", capital)
        self.assertIn("{key:'4h',role:'波段持倉',period:'最近 4 小時'}", capital)
        self.assertLess(
            capital.index("{key:'4h',role:'波段持倉'"),
            capital.index("{key:'1h',role:'短線脈衝'"),
        )
        self.assertLess(
            capital.index("{key:'1h',role:'近端持倉'"),
            capital.index("{key:'4h',role:'大級別背景'"),
        )
        self.assertIn("primaryMeta=profile[0]", capital)
        self.assertIn("function capitalFlowBias(row)", capital)
        self.assertIn("價格推定偏多主導", capital)
        self.assertIn("價格推定偏空主導", capital)
        self.assertIn("支持${cardLabel}", capital)
        self.assertIn("與${cardLabel}衝突", capital)
        self.assertIn("相對異常增倉", capital)
        self.assertIn("高於平常的增倉", capital)
        self.assertIn("無法單靠 OI 確認哪一方", capital)
        self.assertIn("時窗方向分歧", capital)
        self.assertIn("largeDirections.size>1", capital)
        self.assertIn("${primaryMeta.role}：${relation.label}", capital)
        self.assertIn("function capitalFlowBehavior(row)", capital)
        self.assertIn("oiChange>0&&bias==='LONG'", capital)
        self.assertIn("label:'新多資金進場'", capital)
        self.assertIn("oiChange>0&&bias==='SHORT'", capital)
        self.assertIn("label:'新空資金進場'", capital)
        self.assertIn("oiChange<0&&bias==='LONG'", capital)
        self.assertIn("label:'偏向空單回補'", capital)
        self.assertIn("oiChange<0&&bias==='SHORT'", capital)
        self.assertIn("label:'偏向多單平倉或遭強制減倉'", capital)
        self.assertIn("目前背景：主動買入較強", capital)
        self.assertIn("目前背景：主動賣出較強", capital)
        self.assertIn("marketMetrics?.taker_buy_pct", capital)
        self.assertIn("marketMetrics?.funding_rate_pct", capital)
        self.assertIn("目前背景：Funding", capital)
        self.assertIn("僅供輔助｜不影響進場判定", capital)
        self.assertIn("<span>推測</span>", capital)
        self.assertIn("<span>依據</span>", capital)
        self.assertIn("<span>判斷信心</span>", capital)
        self.assertIn("白話結論：", capital)
        self.assertIn("（不是勝率）", capital)
        self.assertIn("約 ${minutes} 分鐘前", capital)
        self.assertIn("確認出現持倉異常增加", capital)
        self.assertIn("OI 本身不能拆成多單量與空單量", capital)
        self.assertIn("不會改變做多／做空、可進／等待、Entry、SL 或 TP", capital)
        self.assertIn("屬於掃描當下背景，不是同一歷史時窗", capital)
        self.assertIn("capitalFlowDetectionLabel(row,asOf,referenceTime)", capital)
        self.assertIn("capitalFlowRange(row)", capital)
        self.assertIn("flow.as_of_close_ms", capital)
        self.assertIn("function capitalFlowScanReference(item)", capital)
        self.assertIn("report.long_completed_at", capital)
        self.assertIn("report.short_completed_at", capital)
        self.assertIn("referenceTime:capitalFlowScanReference(item)", capital)
        self.assertIn("marketMetrics:item.market_metrics", capital)
        self.assertIn("elapsed < -120000", capital)
        self.assertIn("資料時間異常", capital)
        self.assertIn("台灣 UTC+8", capital)
        self.assertIn("完整 1 小時收線", capital)
        self.assertIn("前 6 個同長區間平均 1.5 倍", capital)
        self.assertIn("OI 變化 ${signed(row?.change_pct)}", capital)
        self.assertIn("row?.baseline_average_change_pct", capital)
        self.assertIn("row?.change_vs_average_ratio", capital)
        self.assertIn("不使用上一輪掃描快照代替", capital)
        self.assertIn("不等於錢包入金或單一大戶", capital)
        self.assertIn("OI 本身不能拆成多單量與空單量", capital)
        self.assertIn("<details class=\"capital-flow-details\">", capital)
        self.assertIn("terminalSignalOutcome(item)||isPreviewItem(item)", capital)
        self.assertNotIn("open_interest_change_pct", capital)
        self.assertNotIn("oi_flow_state", capital)
        self.assertNotIn("疑似大量新增持倉 · 配合", capital)
        self.assertNotIn("疑似大量新增持倉 · 與", capital)

        entry_status = html.split("function itemEntryStatus(item)", 1)[1].split(
            "function itemStoredEntryStatus", 1
        )[0]
        comparator = html.split("function signalSortComparator(a,b){", 1)[1].split(
            "function renderContextCoverage", 1
        )[0]
        self.assertNotIn("capitalFlow", entry_status)
        self.assertNotIn("capital_flow", entry_status)
        self.assertNotIn("capitalFlow", comparator)
        self.assertNotIn("capital_flow", comparator)

    def test_market_times_are_explicit_taiwan_h23_and_separate_from_oi_close(self):
        html = (
            Path(__file__).parents[1] / "radar" / "static" / "pages.html"
        ).read_text(encoding="utf-8")

        time_helpers = html.split("function timestampDate(value)", 1)[1].split(
            "async function api", 1
        )[0]
        self.assertIn("timeZone:'Asia/Taipei'", time_helpers)
        self.assertIn("hourCycle:'h23'", time_helpers)
        self.assertNotIn("hour12:false", time_helpers)
        self.assertIn("normalized=`${normalized}Z`", time_helpers)
        self.assertIn("year:'numeric'", time_helpers)
        self.assertIn("最新行情時間（台灣 UTC+8）", html)
        self.assertIn("持倉資料截止：${esc(taiwanMinute(asOf))}（台灣 UTC+8）", html)
        self.assertNotIn("<br>取得時間：", html)

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
        self.assertIn('"name": "OKX Radar V3.4"', manifest)
        self.assertNotIn("V3.4 Context", manifest)

    def test_signal_episode_cards_use_current_entry_state_and_are_independently_keyed(self):
        root = Path(__file__).parents[1] / "radar" / "static"
        html = (root / "pages.html").read_text(encoding="utf-8")
        worker = (root / "service-worker.js").read_text(encoding="utf-8")

        self.assertIn("function itemWasEntryReady(item)", html)
        display_status = html.split("function itemDisplayEntryStatus(item)", 1)[1].split(
            "function isCorePreview", 1
        )[0]
        self.assertNotIn("itemWasEntryReady", display_status)
        self.assertIn("itemEntryStatus(item)", display_status)
        self.assertIn("liveReady=activeShort.filter(itemCurrentEntryReady)", html)
        self.assertIn("liveLongReady=activeLong.filter(itemCurrentEntryReady)", html)
        self.assertIn("closed_signals", html)
        self.assertIn("long_closed_signals", html)
        self.assertIn('data-group="closed"', html)
        self.assertIn('id="closedSignals"', html)
        self.assertIn('id="closedShortSignals"', html)
        self.assertIn('id="closedLongSignals"', html)
        self.assertIn("allShort=[...activeShort].sort", html)
        self.assertNotIn("allShort=[...activeShort,...shortClosed]", html)
        self.assertIn("function terminalSortComparator(a,b)", html)
        self.assertIn("instCategory＝1", html)
        self.assertIn("股票型永續合約", html)
        self.assertIn("function pruneExpiredTerminalCards(report)", html)
        self.assertIn("item?.radar_horizon==='LONG'?24:5", html)
        self.assertIn("終局結果保留 5 小時", html)
        self.assertIn("終局結果保留 24 小時", html)
        self.assertIn("已達止盈｜本次交易計畫完成", html)
        self.assertIn("已達止損｜本次交易計畫結束", html)
        self.assertIn('data-trigger-id="${esc(item.trigger_id||\'\')}"', html)
        self.assertIn("等待新的 Trigger；舊 Entry／SL／TP 不會復活", html)
        self.assertIn("舊 Entry／SL／TP 不會復活", html)
        self.assertIn("舊交易計畫已結束", html)
        self.assertIn("signalTradeGrid(item,{prefix:'原始 ',original:true})", html)
        self.assertIn("okx-radar-shell-v4.5-history-replay-1", worker)

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
