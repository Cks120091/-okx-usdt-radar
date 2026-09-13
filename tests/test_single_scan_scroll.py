from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]


class SingleScanScrollTests(unittest.TestCase):
    def test_external_css_owns_single_scan_dialog_scroll(self):
        css = (ROOT / "radar/static/history-replay.css").read_text(encoding="utf-8")
        self.assertIn("#singleScanDialog.intraday-dialog[open]", css)
        self.assertIn("height:calc(100dvh - 24px", css)
        self.assertIn("#singleScanContent{min-height:0;max-height:none", css)
        self.assertIn("overflow-y:auto!important", css)
        self.assertIn("-webkit-overflow-scrolling:touch", css)
        self.assertIn("touch-action:pan-y", css)

    def test_mobile_dialog_uses_full_available_dynamic_viewport(self):
        css = (ROOT / "radar/static/history-replay.css").read_text(encoding="utf-8")
        self.assertIn("@media(max-width:600px)", css)
        self.assertIn("height:calc(100dvh - 16px", css)
        self.assertIn("max-height:none", css)
        self.assertIn("env(safe-area-inset-bottom)", css)

    def test_pages_loads_scroll_override_after_inline_dialog_css(self):
        pages = (ROOT / "radar/static/pages.html").read_text(encoding="utf-8")
        inline_dialog = pages.index(".intraday-dialog[open]")
        external_css = pages.index('<link rel="stylesheet" href="/history-replay.css">')
        self.assertLess(inline_dialog, external_css)
        self.assertIn('id="singleScanContent"', pages)


if __name__ == "__main__":
    unittest.main()
