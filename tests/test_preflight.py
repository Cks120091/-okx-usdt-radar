from tests import legacy_preflight_cases as _legacy
from radar.preflight import build_preflight_payload

for _name in dir(_legacy):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_legacy, _name)


class PreflightTests(_legacy.PreflightTests):
    def test_adverse_side_hides_artificial_live_rr_without_mutating_trigger(self):
        with _legacy.tempfile.TemporaryDirectory() as directory:
            item = _legacy.make_signal()
            original_entry = dict(item.entry_eligibility)
            runtime = _legacy.RadarRuntime(
                _legacy.PreflightScanner(_legacy.PreflightClient(price=98.1)),
                _legacy.AppConfig(data_dir=directory),
            )
            runtime._latest = _legacy.make_report(item)
            payload = runtime.preflight_dict(item.inst_id, "SHORT")
            self.assertEqual(payload["verdict"]["status"], "WAIT_RETEST")
            self.assertFalse(payload["verdict"]["actionable"])
            self.assertIn("接近失效", payload["verdict"]["label"])
            self.assertEqual(payload["verdict"]["situation"], "NEAR_INVALIDATION")
            self.assertIn("EXECUTION_COST_ADVISORY", payload["verdict"]["risk_warnings"])
            self.assertNotIn("EXECUTION_COST_TOO_HIGH", payload["verdict"]["hard_blockers"])
            self.assertFalse(payload["plan_state"]["old_plan_reusable_for_new_entry"])
            self.assertEqual(payload["signal_lifecycle"]["status"], "ACTIVE")
            self.assertIsNone(payload["live"]["remaining_rr"])
            self.assertFalse(payload["live"]["remaining_rr_applicable"])
            self.assertEqual(item.entry_eligibility, original_entry)
            self.assertTrue(payload["safety"]["stored_trigger_unchanged"])

    def test_execution_cost_warning_band_only_allows_values_below_hard_limit(self):
        signal = _legacy.make_signal()
        client = _legacy.PreflightClient(price=100.0)
        ticker = client.get_ticker(signal.inst_id)
        base_context = client.get_execution_context(signal.inst_id)
        config = _legacy.AppConfig(max_execution_cost_to_risk_pct=15.0)
        payload = build_preflight_payload(
            signal, ticker,
            _legacy.replace(base_context, buy_slippage_pct=0.11, sell_slippage_pct=0.11),
            config,
            report_generated_at=_legacy.datetime.now(_legacy.timezone.utc).isoformat(),
        )
        self.assertGreater(payload["execution"]["execution_cost_to_risk_pct"], 15.0)
        self.assertEqual(payload["verdict"]["status"], "ENTRY_READY")
        self.assertTrue(payload["verdict"]["actionable"])
        self.assertEqual(payload["verdict"]["hard_blockers"], [])
        self.assertIn("EXECUTION_COST_TOO_HIGH", payload["verdict"]["advisory_blockers"])
        self.assertIn("EXECUTION_COST_ADVISORY", payload["verdict"]["risk_warnings"])
        self.assertTrue(payload["plan_state"]["new_entry_allowed"])

    def test_preflight_uses_raw_cost_and_rr_at_hard_boundaries(self):
        signal = _legacy.make_signal()
        client = _legacy.PreflightClient(price=99.99)
        ticker = client.get_ticker(signal.inst_id)
        context = client.get_execution_context(signal.inst_id)
        config = _legacy.AppConfig(max_execution_cost_to_risk_pct=15.0, minimum_rr=1.8)
        cost = build_preflight_payload(
            signal, ticker,
            _legacy.replace(context, buy_slippage_pct=0.0901, sell_slippage_pct=0.0901),
            config,
            report_generated_at=_legacy.datetime.now(_legacy.timezone.utc).isoformat(),
        )
        self.assertEqual(cost["execution"]["execution_cost_to_risk_pct"], 15.0)
        self.assertEqual(cost["verdict"]["status"], "ENTRY_READY")
        self.assertIn("EXECUTION_COST_ADVISORY", cost["verdict"]["risk_warnings"])
        self.assertNotIn("EXECUTION_COST_TOO_HIGH", cost["verdict"]["hard_blockers"])

        rr_price = (104.2 + 1.7999 * 98.0) / (1.0 + 1.7999)
        rr_client = _legacy.PreflightClient(price=rr_price - 0.01)
        rr = build_preflight_payload(
            signal,
            rr_client.get_ticker(signal.inst_id),
            rr_client.get_execution_context(signal.inst_id),
            config,
            report_generated_at=_legacy.datetime.now(_legacy.timezone.utc).isoformat(),
        )
        self.assertEqual(rr["live"]["remaining_rr"], 1.8)
        self.assertEqual(rr["verdict"]["status"], "ENTRY_READY")
        self.assertIn("RR_ADVISORY", rr["verdict"]["risk_warnings"])
        self.assertNotIn("RR_INSUFFICIENT", rr["verdict"]["hard_blockers"])
