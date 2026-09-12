from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]


class CompactUiTests(unittest.TestCase):
    def test_main_card_prioritizes_decision_and_removes_repeated_microcopy(self):
        text = (ROOT / "radar/static/pages.html").read_text(encoding="utf-8")
        self.assertIn("function decisionPanel(item){return decisionPanelBody(item)+quickLookPanel(item)", text)
        self.assertNotIn("摘要，不新增判定", text)
        self.assertNotIn("（僅供輔助）", text)
        self.assertIn("Compact clarity pass:", text)
        self.assertIn("更新現價、進場距離、成交條件、續走力道與歷史 OI 持倉動向；不改寫方向或原 Entry／SL／TP", text)

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
