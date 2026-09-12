from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class HistoryReturnNavigationTests(unittest.TestCase):
    def test_history_page_returns_to_originating_coin_view(self):
        html = (ROOT / "radar" / "static" / "history-scan.html").read_text(encoding="utf-8")

        self.assertIn('id="historyBack"', html)
        self.assertIn('href="/"', html)  # safe fallback for direct opens / notifications
        self.assertIn("← 回到 ${symbol}", html)
        self.assertIn("previous.origin === location.origin", html)
        self.assertIn("previous.pathname !== '/history-scan'", html)
        self.assertIn("history.back();", html)

    def test_history_return_does_not_hijack_modified_link_clicks(self):
        html = (ROOT / "radar" / "static" / "history-scan.html").read_text(encoding="utf-8")

        for guard in ("event.metaKey", "event.ctrlKey", "event.shiftKey", "event.altKey"):
            self.assertIn(guard, html)


if __name__ == "__main__":
    unittest.main()
