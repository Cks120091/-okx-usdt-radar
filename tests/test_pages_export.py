import json
import tempfile
import unittest
from pathlib import Path

from scripts.export_pages import export_site


class PagesExportTests(unittest.TestCase):
    def test_exports_static_site_and_latest_report(self):
        payload = {
            "status": "NO_QUALIFIED_SIGNAL",
            "generated_at": "2026-08-10T10:00:00+00:00",
            "target_count": 200,
            "fetched_count": 200,
            "analyzable_count": 198,
            "coverage_pct": 100.0,
            "failed_instruments": {},
            "signals": [],
            "market_regime_counts": {"TREND": 80, "RANGE": 60},
            "watchlist": [{"inst_id": "BTC-USDT-SWAP", "readiness_score": 85.7}],
            "market_map": [{"inst_id": "BTC-USDT-SWAP", "regime": "TREND"}],
            "message": "完整掃描完成，本輪沒有合格訊號。",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "latest.json"
            template = root / "pages.html"
            output = root / "site"
            report.write_text(json.dumps(payload), encoding="utf-8")
            template.write_text("<html>radar</html>", encoding="utf-8")

            index_path, latest_path = export_site(report, template, output)

            self.assertEqual(index_path.read_text(encoding="utf-8"), "<html>radar</html>")
            exported = json.loads(latest_path.read_text(encoding="utf-8"))
            self.assertEqual(exported["coverage_pct"], 100.0)
            self.assertEqual(exported["watchlist"][0]["inst_id"], "BTC-USDT-SWAP")
            self.assertEqual(exported["market_map"][0]["regime"], "TREND")
            self.assertTrue((output / ".nojekyll").exists())

    def test_rejects_report_with_missing_required_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "latest.json"
            template = root / "pages.html"
            report.write_text(json.dumps({"status": "BROKEN"}), encoding="utf-8")
            template.write_text("<html></html>", encoding="utf-8")
            with self.assertRaises(ValueError):
                export_site(report, template, root / "site")
