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
        self.assertIn("長線已觸發・持續保留", html)
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
        self.assertNotIn("幣種掃描", html)
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
        self.assertIn("okx-radar-shell-v3.7-signal-terminal-1", service_worker)
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
        self.assertNotIn("function itemDecisionContext", html)
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
        self.assertIn("多空候選排行", html)
        self.assertIn("交易品質不會代替 Trigger（價格觸發）", html)
        self.assertIn("訊號準備度", html)
        self.assertIn("尚未觸發", html)
        self.assertIn("可進 · ${watchCount} 觀察", html)
        self.assertIn("itemEntryStatus(x)==='ENTRY_READY'", html)
        self.assertIn("function itemWasEntryReady(item)", html)
        self.assertIn("OI（未平倉量）異動雷達", html)
        self.assertIn("市場平均 RSI", html)
        self.assertIn("localStorage", html)
        self.assertIn("TradingView（技術圖表）", html)
        self.assertIn("輸入正式訊號幣種，例如 BTC", html)
        self.assertIn("renderOverviewUnavailable", html)
        self.assertNotIn("開發者資料（Raw Data）", html)
        self.assertNotIn("分組績效 JSON", html)
        self.assertNotIn("raw_indicators", html)
        self.assertIn("⚡ ${frame} 進場前更新", html)
        self.assertIn("重新取得最新成交資料", html)
        self.assertIn("進場檢查", html)
        self.assertIn("/api/preflight", html)
        self.assertIn("只更新現價、深度、滑價與成本，不重新分析多週期方向", html)
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
        self.assertIn("訊號觸發時間（台灣）", html)
        self.assertNotIn("status!=='ENTRY_READY'&&status!=='MISSED_ENTRY'", html)
        self.assertIn("okx-radar-shell-v3.7-signal-terminal-1", service_worker)
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
        self.assertIn("訊號觸發時間（台灣）", html)
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
        self.assertIn("HTTPStatus.GONE", retired_route)
        self.assertIn("幣種更新已停用", retired_route)
        self.assertNotIn("runtime.scan_instrument_dict", retired_route)
        self.assertNotIn("幣種掃描", html)
        self.assertNotIn("/api/instrument/scan", html)
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
        self.assertIn("目前進場資格", html)
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
        self.assertLess(
            html.index("const continuationDiff=", html.index("function signalSortComparator")),
            html.index("const qualityDiff=", html.index("function signalSortComparator")),
        )
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
        self.assertIn("itemEntryStatus(x)==='ENTRY_READY'", report)
        self.assertIn("itemWasEntryReady(item)", report)
        self.assertIn(
            "'目前沒有已觸發並持續保留的訊號。',shortReadOnlyReason",
            report,
        )
        self.assertIn("'目前沒有長線訊號。',longReadOnlyReason", report)
        self.assertIn(
            "renderContextCoverage(report,shortTransient||longTransient,preview)", report
        )

        comparator = html.split("function signalSortComparator(a,b){", 1)[1].split(
            "function renderContextCoverage", 1
        )[0]
        self.assertIn("item.data_timestamp", comparator)
        self.assertIn("item.closed_candle_ts", comparator)
        self.assertIn("const continuationDiff=continuationRank(b)-continuationRank(a)", comparator)
        self.assertLess(comparator.index("statusDiff"), comparator.index("continuationDiff"))
        self.assertLess(comparator.index("continuationDiff"), comparator.index("qualityDiff"))
        self.assertLess(comparator.index("qualityDiff"), comparator.index("dataTimeDiff"))
        self.assertLess(comparator.index("dataTimeDiff"), comparator.index("freshDiff"))
        self.assertLess(comparator.index("freshDiff"), comparator.index("rrDiff"))

        entry_status = html.split("function itemEntryStatus(item)", 1)[1].split(
            "function itemStoredEntryStatus", 1
        )[0]
        self.assertIn("eligibility.status", entry_status)
        self.assertIn("eligibility.wait_reason_code", entry_status)
        self.assertNotIn("eligibility.hard_blockers", entry_status)
        self.assertNotIn("check?.hard!==false&&check?.passed===false", entry_status)
        self.assertIn("eligibility.position_status||'ENTRY_READY'", entry_status)
        self.assertNotIn("new_entry_allowed", entry_status)
        self.assertNotIn("item?.actionable", entry_status)
        self.assertNotIn("decision_context", entry_status)
        self.assertNotIn("decisionContext", entry_status)
        decision_panel = html.split("function decisionPanel(item", 1)[1].split(
            "function timeframeGrid", 1
        )[0]
        self.assertIn("const entry=item.entry_eligibility||{}", decision_panel)
        self.assertIn("preflightActions(item.inst_id,horizon,item,false)", decision_panel)
        self.assertIn("signalTradeGrid(item", decision_panel)
        self.assertIn("preview:true", decision_panel)
        self.assertNotIn("finalDecisionPanel", decision_panel)
        self.assertNotIn("decisionContext", decision_panel)
        self.assertIn(
            "copyAction=item.entry_low&&item.stop_loss&&item.take_profit_1?",
            decision_panel,
        )
        self.assertNotIn("instrumentButton", decision_panel)
        self.assertNotIn("status==='HARD_GATE_BLOCKED'", decision_panel)
        entry_badge = html.split("function entryBadge(item)", 1)[1].split(
            "function entryCallout", 1
        )[0]
        self.assertNotIn("status==='HARD_GATE_BLOCKED'", entry_badge)
        self.assertNotIn("風控未通過｜暫停進場", entry_badge)
        self.assertIn("function reportRenderFingerprint(report)", html)
        fingerprint = html.split("function reportRenderFingerprint(report)", 1)[1].split(
            "function reportCardEntries", 1
        )[0]
        self.assertIn("item.entry_eligibility?.status", fingerprint)
        self.assertIn("item.entry_eligibility?.original_status", fingerprint)
        self.assertIn("item.lifecycle?.status", fingerprint)
        self.assertIn("item.decision_context?.continuation_confirmation||{}", fingerprint)
        self.assertIn("continuation.key", fingerprint)
        self.assertIn("votes.OI?.state", fingerprint)
        self.assertIn("votes.TAKER_CVD?.state", fingerprint)
        self.assertIn("votes.VOLUME?.state", fingerprint)

        continuation = html.split("function continuationConfirmation(item)", 1)[1].split(
            "function directionBadge", 1
        )[0]
        self.assertIn("item?.decision_context?.continuation_confirmation", continuation)
        self.assertIn("CONFIRMED:['continuation-confirmed-b','續走｜證據一致']", continuation)
        self.assertIn("FORMING:['continuation-forming-b','續走｜形成中']", continuation)
        self.assertIn("CONFLICT:['continuation-conflict-b','續走｜有反證']", continuation)
        self.assertIn("UNKNOWN:['continuation-unknown-b','續走｜資料不足']", continuation)
        self.assertIn("function continuationCoreVote(item,key)", html)
        self.assertIn("confirmation.core_votes?.[key]", continuation)
        self.assertIn("function continuationStrip(item)", html)
        self.assertIn("hasCoreVotes=domains.every", continuation)
        self.assertIn("這是更新前的保留資料", continuation)
        self.assertIn("readOnlyReason=itemReadOnlyReason(item)", continuation)
        self.assertIn("confirmation.supporting", continuation)
        self.assertIn("confirmation.conflicts", continuation)
        self.assertIn("confirmation.warnings", continuation)
        self.assertIn("confirmation.missing", continuation)
        self.assertIn("confirmation.meaning", continuation)
        self.assertIn("terminalSignalOutcome(item)", continuation)
        self.assertIn("OI（未平倉量）看是否有新增部位", continuation)
        self.assertIn("Taker Flow／CVD", continuation)
        self.assertIn("成交量看市場參與是否放大", continuation)
        self.assertIn("不是勝率", continuation)
        render_signals = html.split("function renderSignals(items", 1)[1].split(
            "function renderWatchlist", 1
        )[0]
        self.assertIn("${continuationBadge(item)}", render_signals)
        self.assertIn("${continuationStrip(item)}", render_signals)
        self.assertIn("signal-status-line", render_signals)
        self.assertIn("完整判定資料", render_signals)
        self.assertIn("details(item,true)", render_signals)
        self.assertIn("function signalTradeGrid(item,options={})", html)
        self.assertIn("signal-plan-grid", html)
        self.assertIn("R:R｜${esc(rrLabel)}", html)

        # 延續確認只能負責說明、重繪與同進場狀態內排序，不得變成進場硬門檻。
        self.assertNotIn("continuationConfirmation", entry_status)
        self.assertNotIn("continuationConfirmation", decision_panel)
        self.assertNotIn("continuationConfirmation", entry_badge)

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
        self.assertIn("本輪未取得 OI 數值", availability)
        self.assertIn("OI 數值已取得，但尚無上一輪比較基準", availability)

        anomaly = html.split("function oiAnomalyEmptyState(markets)", 1)[1].split(
            "function renderAnomalies", 1
        )[0]
        self.assertIn("目前沒有幣種達到異動門檻", anomaly)
        self.assertIn("但尚無上一輪比較基準", anomaly)
        self.assertIn("noBaselineCount", anomaly)
        self.assertIn("已取得 OI、仍待下一輪基準", anomaly)
        self.assertIn("OI API 或該幣資料可能暫時不可用", anomaly)
        self.assertIn("oiAnomalyEmptyState(markets)", anomaly)
        self.assertNotIn("至少需要連續兩輪", anomaly)
        self.assertNotIn("需要至少兩輪掃描才能比較 OI", html)

        continuation = html.split("function continuationCoreVote(item,key)", 1)[
            1
        ].split("function directionBadge", 1)[0]
        self.assertIn("key==='OI'&&safeState==='NEUTRAL'", continuation)
        self.assertIn("已取得・未達同向新增門檻", continuation)
        self.assertIn("key==='OI'&&safeState==='UNKNOWN'", continuation)
        self.assertIn("availability.key==='MISSING'", continuation)
        self.assertIn("availability.key==='NO_BASELINE'", continuation)
        self.assertIn("更新前保留資料未包含三項核心明細", continuation)
        self.assertIn("仍沒有足夠證據確認續走", continuation)
        self.assertNotIn(
            "continuationItems(confirmation.missing,'核心資料已取得')",
            continuation,
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
        self.assertIn('"name": "OKX Radar V3.4"', manifest)
        self.assertNotIn("V3.4 Context", manifest)

    def test_signal_episode_cards_are_sticky_terminal_and_independently_keyed(self):
        root = Path(__file__).parents[1] / "radar" / "static"
        html = (root / "pages.html").read_text(encoding="utf-8")
        worker = (root / "service-worker.js").read_text(encoding="utf-8")

        self.assertIn("function itemWasEntryReady(item)", html)
        self.assertIn(
            "if(itemWasEntryReady(item))return 'ENTRY_READY'",
            html,
        )
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
        self.assertIn("等待新的 Trigger 與全新交易計畫", html)
        self.assertIn("舊 Entry／SL／TP 不會復活", html)
        self.assertIn("舊交易計畫已結束", html)
        self.assertIn("signalTradeGrid(item,{prefix:'原始 ',original:true})", html)
        self.assertIn("okx-radar-shell-v3.7-signal-terminal-1", worker)

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
