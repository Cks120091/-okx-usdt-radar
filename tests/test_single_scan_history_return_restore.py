from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]


class SingleScanHistoryReturnRestoreTests(unittest.TestCase):
    def test_single_scan_history_link_records_explicit_return_context(self):
        text = (ROOT / "radar/static/history-replay.js").read_text(encoding="utf-8")
        self.assertIn("SINGLE_SCAN_RETURN_KEY", text)
        self.assertIn("link.closest('#singleScanContent')", text)
        self.assertIn("source:'single_scan'", text)
        self.assertIn("inst_id:instId", text)
        self.assertIn("title.includes('4H') ? 'LONG' : 'SHORT'", text)
        self.assertIn("scenarioByInst.get(instId)", text)
        self.assertIn("sessionStorage.setItem(SINGLE_SCAN_RETURN_KEY", text)

    def test_return_reopens_exact_coin_through_existing_single_scan_route(self):
        text = (ROOT / "radar/static/history-replay.js").read_text(encoding="utf-8")
        self.assertIn("function restoreSingleScanReturn()", text)
        self.assertIn("proxy.dataset.singleId = instId", text)
        self.assertIn("proxy.dataset.singleHorizon", text)
        self.assertIn("proxy.dataset.singleDirection", text)
        self.assertIn("proxy.click()", text)
        self.assertIn("window.addEventListener('pageshow'", text)
        self.assertIn("setTimeout(restoreSingleScanReturn, 0)", text)

    def test_restore_is_one_shot_and_expiring(self):
        text = (ROOT / "radar/static/history-replay.js").read_text(encoding="utf-8")
        self.assertIn("SINGLE_SCAN_RETURN_MAX_AGE_MS = 30 * 60 * 1000", text)
        self.assertIn("sessionStorage.removeItem(SINGLE_SCAN_RETURN_KEY)", text)
        self.assertIn("if (dialog.open)", text)

    def test_trading_core_is_not_touched_by_navigation_restore(self):
        for name in ("strategy.py", "market_story.py", "decision.py", "entry_window.py"):
            source = (ROOT / "radar" / name).read_text(encoding="utf-8")
            self.assertNotIn("SINGLE_SCAN_RETURN_KEY", source)
            self.assertNotIn("single-scan-return", source)


if __name__ == "__main__":
    unittest.main()
