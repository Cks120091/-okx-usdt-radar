from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]


class CompactPreflightAndHistoryNotifyTests(unittest.TestCase):
    def test_history_completion_notification_is_separate_and_opt_in(self):
        text = (ROOT / "radar/static/history-scan.js").read_text(encoding="utf-8")
        css = (ROOT / "radar/static/history-replay.css").read_text(encoding="utf-8")
        self.assertIn("okx-radar-history-push-enabled", text)
        self.assertIn("勝率更新完成通知", text)
        self.assertIn("historyNotifyButton", text)
        self.assertIn("historyNotificationPayload", text)
        self.assertIn("showHistoryCompletionNotification", text)
        self.assertIn("COMPLETE','PARTIAL_COMPLETE','ERROR", text)
        self.assertIn("Trigger ${triggers}", text)
        self.assertIn("TP1 ${wins} / SL ${losses}", text)
        self.assertIn(".history-notify-row", css)

    def test_preflight_keeps_core_decision_open_and_folds_secondary_data(self):
        text = (ROOT / "radar/static/history-replay.js").read_text(encoding="utf-8")
        css = (ROOT / "radar/static/history-replay.css").read_text(encoding="utf-8")
        self.assertIn("organizePreflightPage", text)
        self.assertIn("現在位置與進場資格", text)
        self.assertIn("preflight-plan-block", text)
        self.assertIn("#preflightHistoryRate", text)
        self.assertIn("更多確認與資料細節", text)
        self.assertIn("成交品質・OI/CVD・續走・資料來源", text)
        self.assertIn(".preflight-secondary-stack", css)
        self.assertIn(".preflight-priority-position", css)
        self.assertIn(".preflight-priority-plan", css)

    def test_single_coin_scan_keeps_decision_first_and_removes_recursive_scan_button(self):
        text = (ROOT / "radar/static/history-replay.js").read_text(encoding="utf-8")
        css = (ROOT / "radar/static/history-replay.css").read_text(encoding="utf-8")
        self.assertIn("organizeSingleScanDialog", text)
        self.assertIn("single-scan-core", text)
        self.assertIn("更多市場與持倉資料", text)
        self.assertIn("資金流・OI/CVD・完整數據", text)
        self.assertIn("button.hidden = true", text)
        self.assertIn(".single-scan-details", css)
        self.assertIn("#singleScanContent .single-scan-button[hidden]", css)


if __name__ == "__main__":
    unittest.main()
