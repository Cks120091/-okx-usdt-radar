from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]


class FusionBalancePolicyTests(unittest.TestCase):
    def test_policy_is_downstream_and_keeps_execution_safety(self):
        strategy = (ROOT / "radar/strategy.py").read_text(encoding="utf-8")
        decision = (ROOT / "radar/decision.py").read_text(encoding="utf-8")
        preflight = (ROOT / "radar/preflight.py").read_text(encoding="utf-8")
        story = (ROOT / "radar/market_story.py").read_text(encoding="utf-8")
        self.assertIn("FUSION_BALANCED_V1", strategy)
        self.assertIn("FUSION_BALANCED_V1", decision)
        self.assertIn("FUSION_BALANCED_V1", preflight)
        self.assertIn("reentry_confirmation_advisory", strategy)
        self.assertIn("RISK_REWARD", decision)
        self.assertIn("STOP_LOSS", decision)
        self.assertIn("EXECUTION_COST", decision)
        self.assertIn("SPREAD_TOO_HIGH", preflight)
        self.assertIn("SLIPPAGE_TOO_HIGH", preflight)
        self.assertIn("LIQUIDITY_TOO_LOW", preflight)
        self.assertIn("OPPOSITE_SIGNAL", preflight)
        self.assertNotIn("fusion_long_score", story)

    def test_fusion_replaces_duplicate_momentum_veto_only(self):
        decision = (ROOT / "radar/decision.py").read_text(encoding="utf-8")
        self.assertIn("CORE_CONSOLIDATION", decision)
        self.assertIn("TREND_MOMENTUM", decision)
        self.assertIn("POSITION_STRUCTURE", decision)
        self.assertIn("directional_score", decision)
        self.assertIn(">= 42.0", decision)


if __name__ == "__main__":
    unittest.main()
