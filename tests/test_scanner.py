from tests import legacy_scanner_cases as _legacy

for _name in dir(_legacy):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_legacy, _name)

ScannerMarketPulseTests = _legacy.ScannerMarketPulseTests


class ScannerTests(_legacy.ScannerTests):
    def test_execution_risks_warn_and_reopen_when_price_returns_to_entry(self):
        scanner = _legacy.MarketScanner(
            _legacy.FakeClient(),
            _legacy.ScannerConfig(
                min_quote_volume_24h=0,
                max_slippage_pct=0.15,
                max_execution_cost_to_risk_pct=15.0,
            ),
        )
        cases = {
            "high_slippage": ({"buy_slippage_pct": 0.30}, "SLIPPAGE_TOO_HIGH", False),
            "missing_order_book": ({"execution_quality_complete": False, "buy_slippage_pct": None}, "EXECUTION_DATA_UNAVAILABLE", True),
            "cost_too_high": ({"execution_cost_to_risk_pct": 20.0}, "EXECUTION_COST_TOO_HIGH", True),
        }
        for name, (metric_updates, expected_warning, expected_allowed) in cases.items():
            with self.subTest(case=name):
                signal = _legacy.replace(_legacy.qualified_signal(), take_profit_1="106")
                metrics = {**signal.market_metrics, **metric_updates, "last_price": 104.0}
                blocked_away = scanner._refresh_entry_eligibility(_legacy.replace(signal, market_metrics=metrics))
                returned = scanner._refresh_entry_eligibility(
                    _legacy.replace(blocked_away, market_metrics={**blocked_away.market_metrics, "last_price": 100.0})
                )
                self.assertEqual(returned.trigger_id, signal.trigger_id)
                self.assertEqual(returned.lifecycle, signal.lifecycle)
                self.assertIn(expected_warning, returned.entry_eligibility["risk_warnings"])
                self.assertEqual(returned.entry_eligibility["hard_blockers"], [])
                self.assertTrue(returned.actionable)
                self.assertTrue(returned.entry_eligibility["new_entry_allowed"])
                self.assertEqual(returned.entry_eligibility["status"], "ENTRY_READY")
                decided = scanner._attach_decision_context(returned)
                self.assertEqual(decided.actionable, expected_allowed)
                self.assertEqual(decided.decision_context["final"]["new_entry_allowed"], expected_allowed)
