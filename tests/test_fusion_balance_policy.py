from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]


class FusionBalancePolicyTests(unittest.TestCase):
    def test_policy_is_downstream_and_keeps_risk_advisory(self):
        strategy = (ROOT / "radar/strategy.py").read_text(encoding="utf-8")
        decision = (ROOT / "radar/decision.py").read_text(encoding="utf-8")
        preflight = (ROOT / "radar/preflight.py").read_text(encoding="utf-8")
        preflight_core = (ROOT / "radar/_preflight_core.py").read_text(encoding="utf-8")
        story = (ROOT / "radar/market_story.py").read_text(encoding="utf-8")
        self.assertIn("FUSION_BALANCED_V1", strategy)
        self.assertIn("ADVISORY_RISK_V1", decision)
        self.assertIn("ADVISORY_RISK_V1", preflight)
        self.assertIn("risk_checks_advisory_only", decision)
        self.assertIn("reentry_confirmation_advisory", strategy)
        self.assertIn("RISK_REWARD", decision)
        self.assertIn("STOP_LOSS", decision)
        self.assertIn("EXECUTION_COST", decision)
        self.assertIn("RR_ADVISORY", preflight)
        self.assertIn("EXECUTION_COST_ADVISORY", preflight)
        for code in ("SPREAD_TOO_HIGH", "SLIPPAGE_TOO_HIGH", "LIQUIDITY_TOO_LOW"):
            self.assertIn(code, preflight_core)
            self.assertIn(code, decision)
            self.assertIn(code, preflight)
        self.assertIn("OPPOSITE_SIGNAL", preflight_core)
        self.assertNotIn('"OPPOSITE_SIGNAL",', decision)
        self.assertNotIn('"OPPOSITE_SIGNAL",', preflight)
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
