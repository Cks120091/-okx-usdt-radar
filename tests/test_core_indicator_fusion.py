from pathlib import Path

from tests import fusion_indicator_cases as _cases


class CoreIndicatorFusionTests(_cases.CoreIndicatorFusionTests):
    def test_shadow_fusion_cannot_reduce_or_create_formal_triggers(self):
        root = Path(__file__).resolve().parents[1]
        market_story = (root / "radar/market_story.py").read_text(encoding="utf-8")
        strategy = (root / "radar/strategy.py").read_text(encoding="utf-8")
        decision = (root / "radar/decision.py").read_text(encoding="utf-8")
        self.assertNotIn("fusion_long_score", market_story)
        self.assertIn("fusion_long_score", strategy)
        self.assertIn("fusion_long_score", decision)
        self.assertIn("CORE_CONSOLIDATION", decision)
