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
        self.assertIn("長線目前可進", html)
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
        self.assertIn("`${displaySymbol(normalized)} · ${selected==='LONG'?'4H 長線'", html)
        self.assertIn("`${displaySymbol(instId)} · ${horizon==='LONG'?'4H 長線':'15m 短線'}`", html)
        self.assertIn("資料已過期，禁止依此進場", html)
        self.assertIn("上一輪快照會繼續顯示", html)
        self.assertIn("function isExpiredSnapshot(item)", html)
        self.assertIn("⏱ 資料已過期", html)
        self.assertIn("資料已過期｜原快照", html)
        self.assertIn("label='幣種掃描（更新判定）'", html)
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
        self.assertIn("grid-template-columns:repeat(4,minmax(0,1fr))", html)
        self.assertIn(".decision-actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))", html)
        self.assertIn("okx-radar-shell-v3.4-round-isolation-2", service_worker)
        self.assertIn("--primary-nav-safe-bottom", html)
        self.assertIn(".action-wrap{display:none;grid-row:4;position:relative", html)
        self.assertIn("@media(orientation:landscape) and (max-height:520px)", html)
        self.assertIn('class="preflight-action-bar"', html)
        self.assertLess(
            html.index('id="preflightContent"'), html.index('id="preflightRefresh"')
        )
        self.assertLess(
            html.index('id="instrumentContent"'), html.index('id="instrumentScan"')
        )
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
        self.assertIn("多空候選排行", html)
        self.assertIn("交易品質不會代替 Trigger（價格觸發）", html)
        self.assertIn("訊號準備度", html)
        self.assertIn("尚未觸發", html)
        self.assertIn("可進 · ${watchCount} 觀察", html)
        self.assertIn("itemDisplayEntryStatus(x)==='ENTRY_READY'", html)
        self.assertIn("OI（未平倉量）異動雷達", html)
        self.assertIn("市場平均 RSI", html)
        self.assertIn("localStorage", html)
        self.assertIn("TradingView（技術圖表）", html)
        self.assertIn("搜尋幣種，例如 BTC、SNDK", html)
        self.assertIn("renderOverviewUnavailable", html)
        self.assertNotIn("開發者資料（Raw Data）", html)
        self.assertNotIn("分組績效 JSON", html)
        self.assertNotIn("raw_indicators", html)
        self.assertIn("幣種掃描（更新判定）", html)
        self.assertIn("進場檢查", html)
        self.assertIn("/api/preflight", html)
        self.assertIn("只更新或重新分析這一個幣，不重新掃描全市場", html)
        self.assertIn("原始 Trigger（價格觸發）沒有被修改", html)
        self.assertIn("data-preflight-id", html)
        self.assertIn("function preflightPlainGuide(data)", html)
        self.assertIn("判定依據（簡要）", html)
        self.assertIn("現在怎麼做", html)
        self.assertIn("訊號含義", html)
        self.assertIn("失效與方向", html)
        self.assertIn("等待價格回到最佳進場點位", html)
        self.assertIn("CONTINUATION:'趨勢延續'", html)
        self.assertIn("status==='ENTRY_READY'", html)
        self.assertIn("status==='WAIT_RETEST'", html)
        self.assertIn("status==='PLAN_INVALIDATED'", html)
        self.assertIn("舊計畫失效；方向不會自動變成", html)
        self.assertIn("status==='MISSED_ENTRY'", html)
        self.assertIn("function signalTriggerTime(item,status)", html)
        self.assertIn("訊號觸發時間（台灣）", html)
        self.assertIn("status!=='ENTRY_READY'&&status!=='MISSED_ENTRY'", html)
        self.assertIn("okx-radar-shell-v3.4-round-isolation-2", service_worker)
        self.assertIn("$('#preflightRefresh').addEventListener('click',loadPreflight)", html)
        self.assertIn("'#waitRetestBox','#longWaitRetestBox'", html)
        self.assertIn("scanAction=includeInstrument", html)
        self.assertNotIn("/api/preflight/reanalyze", html)
        self.assertNotIn("reanalyzeMode", html)
        self.assertIn("ORIGINAL_DIRECTION_STABLE", html)
        self.assertIn("最新多週期確認", html)
        self.assertIn("同時核對舊訊號、最新收盤與成交條件", html)
        self.assertIn("訊號觸發時間（台灣）", html)
        self.assertIn("15m 短線歷史", html)
        self.assertIn("4H 長線歷史", html)
        self.assertIn("24 小時內", html)
        self.assertIn("7 天內", html)
        self.assertIn("不因 TP（止盈）／SL（止損）或走遠而提前消失", html)
        self.assertIn("function historyGroups(items)", html)
        self.assertIn("觸發 ${events.length} 次", html)
        self.assertIn("點開查看每次觸發時間與原始價位", html)
        self.assertIn("${shortCoins} 幣 / ${shortItems.length} 次", html)
        self.assertIn("/api/history?limit=60", html)
        self.assertIn("只按原始觸發時間輪替", (
            Path(__file__).parents[1] / "radar" / "service.py"
        ).read_text(encoding="utf-8"))
        self.assertIn("幣種掃描", html)
        self.assertIn("正在統一更新判定", html)
        self.assertIn("/api/instrument/scan", html)
        self.assertIn("data-instrument-id", html)
        self.assertIn("function openInstrument(instId,{scanNow=true,horizon='BOTH'}={})", html)
        self.assertIn("if(scanNow)scanInstrument()", html)
        self.assertIn("function instrumentScanLoading(instId,horizon='BOTH')", html)
        self.assertIn('class="instrument-scanning"', html)
        self.assertIn('class="scan-radar"', html)
        self.assertIn("@keyframes radarSweep", html)
        self.assertIn("每次按下都重新取得最新公開資料", html)
        self.assertNotIn("<canvas", html)
        self.assertIn(
            "function instrumentButton(instId,label='幣種掃描（更新判定）',horizon='BOTH')",
            html,
        )
        self.assertIn("data-instrument-horizon", html)
        self.assertIn("function instrumentPayloadState(payload)", html)
        self.assertIn("function instrumentOverallVerdict(shortPayload,longPayload)", html)
        self.assertIn("function instrumentPlainGuide(context)", html)
        self.assertIn("function instrumentStateDecision(item,payload={})", html)
        self.assertIn("現在能不能進場？", html)
        self.assertIn("判定依據（簡要）", html)
        self.assertIn("目前不可進場｜尚無正式 Trigger", html)
        self.assertIn("準備度不是進場許可", html)
        self.assertIn("只掃描這一個幣，不重新掃描全市場", html)
        self.assertIn("不加入全市場排名", html)
        self.assertIn("最佳進場點位", html)
        self.assertIn("已觸發・有效中", html)
        self.assertIn("目前進場資格", html)
        self.assertIn("尚未進場", html)
        self.assertIn("已經進場", html)
        self.assertIn("等待回踩」不是出場指令", html)
        self.assertIn("容許回測", html)
        self.assertIn("現在位置與進場資格", html)
        self.assertIn("原始交易計畫", html)
        self.assertIn("交易品質變化（不是勝率）", html)
        self.assertIn("Spread（買賣價差）", html)
        self.assertIn("R:R（風險報酬比）", html)
        self.assertIn("Order Book（委託簿）", html)
        self.assertIn("Order Book（訂單簿）", html)
        self.assertIn("(?:（(?:委託簿|訂單簿)）)*", html)
        self.assertIn("Trade Quality（交易品質）", html)
        self.assertIn("Execution Quality（執行品質，不是勝率）", html)
        self.assertIn("quality.combined_score", html)
        self.assertIn("'分層判讀'", html)
        self.assertIn("function technicalText(value)", html)
        self.assertIn('data-scan-mode="SHORT"', html)
        self.assertIn('data-scan-mode="LONG"', html)
        self.assertIn('data-scan-mode="FULL"', html)
        self.assertIn("function signalSortComparator(a,b)", html)
        self.assertLess(
            html.index("const qualityDiff=", html.index("function signalSortComparator")),
            html.index("const freshDiff=", html.index("function signalSortComparator")),
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
        self.assertIn("Trade Quality（交易品質）／Confidence（信心）", html)
        self.assertIn("禁止追價／交易計畫失效", html)
        self.assertIn("多空衝突／轉弱與翻向", html)
        self.assertIn("三大交易時段", html)
        self.assertIn("詳細數據", html)
        self.assertIn("歷史訊號", html)
        self.assertIn("COMPLETED:'交易計畫完成'", html)
        self.assertIn("stage==='EXTENDED'||stage==='TRENDING'||stage==='COMPLETED'", html)
        self.assertIn("該週期先不顯示上一輪卡片", html)
        self.assertIn("另一週期若仍是 Fresh（最新）就照常有效", html)
        self.assertIn("STALE（超過 30 分鐘）", html)
        self.assertIn("底層仍執行完整多週期驗證", html)
        self.assertIn('id="contextCountLabel">深度資料完整', html)
        self.assertIn('id="contextSourceCoverage">來源完整率 —', html)
        self.assertIn("function renderContextCoverage(report,transient=null,preview=false)", html)
        self.assertIn("quality.deep_complete_count", html)
        self.assertIn("quality.deep_source_completeness_pct", html)
        self.assertIn("五項來源完整率", html)
        self.assertIn(
            "horizon==='LONG'?null:isolatedInstrumentSide(data.short,'SHORT')",
            html,
        )
        self.assertIn(
            "horizon==='SHORT'?null:isolatedInstrumentSide(data.long,'LONG')",
            html,
        )
        self.assertIn("@media(prefers-reduced-motion:reduce)", html)
        self.assertNotIn("<canvas", html)

    def test_scan_round_hides_requested_horizons_but_stale_cards_are_retained(self):
        html = (Path(__file__).parents[1] / "radar" / "static" / "pages.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("horizon_read_only_reasons", html)
        self.assertIn("function horizonReadOnlyReason(report,horizon)", html)
        self.assertIn("function readOnlyReferenceBanner(reason,horizon)", html)
        self.assertIn("function itemDisplayEntryStatus(item)", html)
        stored_status = html.split("function itemStoredEntryStatus(item)", 1)[1].split(
            "function itemDisplayEntryStatus", 1
        )[0]
        self.assertIn("original_final_status", stored_status)
        self.assertIn("eligibility.original_status", stored_status)
        self.assertIn("status=originalFinal||", stored_status)
        self.assertNotIn("includes(originalEligibility)", stored_status)
        self.assertNotIn("eligibility.status==='WAIT_RETEST'", stored_status)
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
        self.assertIn("itemDisplayEntryStatus(x)==='ENTRY_READY'", report)
        self.assertIn("true,shortReadOnlyReason", report)
        self.assertIn("true,longReadOnlyReason", report)
        self.assertIn(
            "renderContextCoverage(report,shortTransient||longTransient,preview)", report
        )

        comparator = html.split("function signalSortComparator(a,b){", 1)[1].split(
            "function renderContextCoverage", 1
        )[0]
        self.assertIn("item.data_timestamp", comparator)
        self.assertIn("item.closed_candle_ts", comparator)
        self.assertLess(comparator.index("qualityDiff"), comparator.index("dataTimeDiff"))
        self.assertLess(comparator.index("dataTimeDiff"), comparator.index("freshDiff"))
        self.assertLess(comparator.index("freshDiff"), comparator.index("rrDiff"))

        decision = html.split("function decisionContextStatus(item,payload={})", 1)[1].split(
            "function itemEntryStatus", 1
        )[0]
        self.assertIn("if(itemReadOnlyReason(item))return 'READ_ONLY'", decision)
        final_panel = html.split("function finalDecisionPanel", 1)[1].split(
            "function entryBadge", 1
        )[0]
        self.assertIn("instrumentButton(item.inst_id,'幣種掃描（更新判定）'", final_panel)
        self.assertNotIn('data-preflight-id=', final_panel)
        self.assertIn(
            "copyAction=status==='ENTRY_READY'&&!expired&&!readOnlyReason", final_panel
        )
        self.assertIn("function reportRenderFingerprint(report)", html)
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
