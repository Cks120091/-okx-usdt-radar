from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]


class HistoryReferenceRateTests(unittest.TestCase):
    def test_ui_shows_base_and_similar_scenario_as_reference_only(self):
        text = (ROOT / "radar/static/history-replay.js").read_text(encoding="utf-8")
        self.assertIn("本幣基準", text)
        self.assertIn("相近情境", text)
        self.assertIn("歷史勝率只供參考，不影響 Trigger、進場資格、Entry、SL 或 TP。", text)
        self.assertIn("也不是本單預測機率", text)

    def test_similar_scenario_uses_four_context_dimensions_and_ignores_stage(self):
        text = (ROOT / "radar/static/history-replay.js").read_text(encoding="utf-8")
        self.assertIn("key[0] === scenario.horizon", text)
        self.assertIn("key[1] === scenario.direction", text)
        self.assertIn("key[2] === scenario.kind", text)
        self.assertIn("key[4] === scenario.relation", text)
        self.assertIn("key[5] === scenario.band", text)
        self.assertNotIn("key[3] === scenario", text)
        self.assertIn("相近情境合併不同 signal stage", text)

    def test_reference_rate_does_not_enter_strategy_or_decision_gates(self):
        history = (ROOT / "radar/static/history-replay.js").read_text(encoding="utf-8")
        strategy = (ROOT / "radar/strategy.py").read_text(encoding="utf-8")
        decision = (ROOT / "radar/decision.py").read_text(encoding="utf-8")
        entry_window = (ROOT / "radar/entry_window.py").read_text(encoding="utf-8")
        self.assertIn("similarScenario", history)
        for source in (strategy, decision, entry_window):
            self.assertNotIn("similarScenario", source)
            self.assertNotIn("相近情境", source)

    def test_preflight_reuses_current_card_scenario_without_changing_pages_contract(self):
        text = (ROOT / "radar/static/history-replay.js").read_text(encoding="utf-8")
        pages = (ROOT / "radar/static/pages.html").read_text(encoding="utf-8")
        self.assertIn("const scenarioByInst = new Map();", text)
        self.assertIn("scenarioByInst.set(instId, scenario)", text)
        self.assertIn("scenarioByInst.get(instId) || null", text)
        self.assertIn("window.HistoryReplay?.preflight?.(data.inst_id)", pages)


if __name__ == "__main__":
    unittest.main()
