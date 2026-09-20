from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]


class CompactUiTests(unittest.TestCase):
    def test_main_card_prioritizes_decision_and_removes_repeated_microcopy(self):
        text = (ROOT / "radar/static/pages.html").read_text(encoding="utf-8")
        self.assertIn("function decisionPanel(item){return decisionPanelBody(item)", text)
        self.assertNotIn("摘要，不新增判定", text)
        self.assertNotIn("（僅供輔助）", text)
        self.assertIn("Compact clarity pass:", text)
        self.assertIn("重新抓取按下進場前更新時的最新價格、進場距離、成交條件、續走力道與歷史 OI 持倉動向；不改寫方向或原 Entry／SL／TP", text)
        preflight = text.split("function renderPreflight(data){", 1)[1].split(
            "function preflightTerminalKind", 1
        )[0]
        self.assertNotIn("scanPriceLabel", preflight)
        self.assertNotIn("live.price_change_from_scan_pct", preflight)
        self.assertIn("本次進場前更新價格｜Ask（買入參考）", preflight)
        self.assertIn("本次進場前更新價格｜Bid（賣出參考）", preflight)
        self.assertIn('class="preflight-live-price"', preflight)
        self.assertIn("按下更新時重新向 OKX 取得", preflight)
        self.assertIn("price(live.price,original)", preflight)
        self.assertIn("executionPriceLabel", preflight)
        self.assertLess(
            preflight.index('class="preflight-live-price"'),
            preflight.index("原始進出場價格（固定，不被本次更新改寫）"),
        )

    def test_trigger_lifecycle_is_separate_from_snapshot_entry_state(self):
        text = (ROOT / "radar/static/pages.html").read_text(encoding="utf-8")
        report = text.split("function renderReport(report){", 1)[1].split(
            "function renderOverview(report)", 1
        )[0]

        self.assertIn('class="badge triggered-signal-b">⚡ 訊號已觸發', text)
        self.assertIn("EARLY_SIGNAL:'早期'", text)
        self.assertNotIn("EARLY_SIGNAL:'早期訊號'", text)
        self.assertNotIn("待進場確認", text)
        self.assertIn("${stageBadge(item)}", text)
        self.assertNotIn('id="earlySignals"', text)
        self.assertNotIn('id="signals"', text)
        self.assertNotIn('id="longEarlySignals"', text)
        self.assertNotIn('id="longReadySignals"', text)
        self.assertIn("等待回踩", text)
        self.assertIn("等待新訊號", text)
        self.assertNotIn("風控受阻", text)
        self.assertIn("風險建議｜不阻止進場", text)
        self.assertIn("必要條件未成立", text)
        self.assertIn("快照不是持續即時報價", text)
        self.assertIn("ENTRY｜可進位置（參考）", text)
        self.assertIn("可進位置僅供參考；Entry／SL／TP 固定", text)
        self.assertIn("String(activeShort.length)", report)
        self.assertIn("String(activeLong.length)", report)
        self.assertIn("renderOverview({...report,signals:activeShort})", report)
        self.assertNotIn(">目前可進<", text)
        self.assertNotIn(">已錯過<", text)
        self.assertNotIn("現在能否進場", text)

    def test_oi_and_taker_use_independent_segments_for_consistency(self):
        text = (ROOT / "radar/static/pages.html").read_text(encoding="utf-8")
        self.assertIn("非重疊區間交叉確認", text)
        self.assertIn("資料完整度：", text)
        self.assertIn("共同資料截至：", text)
        self.assertIn("OI 試驗門檻", text)
        self.assertIn("oi_persistence", text)
        self.assertIn("資金一致度：", text)
        self.assertIn("四段非重疊，不代表統計獨立", text)
        self.assertIn("不代表實際開倉方向、勝率或進場許可", text)
        self.assertIn("function flowConfirmationDetail(flow)", text)

    def test_history_page_groups_secondary_information_behind_one_details_block(self):
        text = (ROOT / "radar/static/history-scan.html").read_text(encoding="utf-8")
        self.assertIn('<details class="history-tools"><summary>說明、完整度與資料管理</summary>', text)
        self.assertIn("Trigger 第一次成立當下", text)
        self.assertIn("後續回踩", text)
        self.assertIn("資料完整度", text)
        self.assertIn("資料管理", text)
        self.assertIn('<option value="14">14 日</option>', text)
        self.assertNotIn('value="90"', text)

    def test_history_cards_show_trigger_samples_and_group_with_quick_look(self):
        text = (ROOT / "radar/static/history-replay.js").read_text(encoding="utf-8")
        css = (ROOT / "radar/static/history-replay.css").read_text(encoding="utf-8")
        self.assertIn("尚無歷史勝率", text)
        self.assertIn("Trigger 樣本", text)
        self.assertIn("TP1 / SL", text)
        self.assertIn("回踩、可進場與再進只算確認／執行狀態", text)
        self.assertNotIn("有效機會", text)
        self.assertNotIn("首進 / 再進", text)
        self.assertIn("organizeSignalCards", text)
        self.assertIn("decision-front-grid", text)
        self.assertNotIn("目前同類情境", text)
        self.assertIn(".history-stats-panel{display:none!important}", css)
        self.assertNotIn("可判定率 ${coverage.toFixed(1)}%", text)


if __name__ == "__main__":
    unittest.main()
