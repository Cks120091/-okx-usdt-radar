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

    def test_history_page_hides_methodology_behind_details(self):
        text = (ROOT / "radar/static/history-scan.html").read_text(encoding="utf-8")
        self.assertIn('<details class="history-help"><summary>執行說明</summary>', text)
        self.assertIn('<summary>勝率算法與限制</summary>', text)
        self.assertIn("資料管理", text)

    def test_history_cards_keep_numbers_but_shorten_explanations(self):
        text = (ROOT / "radar/static/history-replay.js").read_text(encoding="utf-8")
        self.assertIn("尚無歷史勝率", text)
        self.assertIn("不重跑", text)
        self.assertNotIn("可判定率 ${coverage.toFixed(1)}%", text)


if __name__ == "__main__":
    unittest.main()
