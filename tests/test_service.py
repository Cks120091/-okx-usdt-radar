from tests import legacy_service_cases as _legacy


class RuntimeSafetyTests(_legacy.RuntimeSafetyTests):
    def test_single_scan_and_preflight_share_one_same_direction_decision(self):
        with _legacy.tempfile.TemporaryDirectory() as directory:
            scanner = _legacy.SingleInstrumentScanner()
            runtime = _legacy.RadarRuntime(scanner, _legacy.AppConfig(data_dir=directory))
            runtime._latest = _legacy.report()
            payload = runtime.scan_instrument_dict("AAA")
            short = payload["short"]
            self.assertEqual(short["latest_confirmation"]["status"], "ORIGINAL_DIRECTION_STABLE")
            self.assertEqual(short["preflight"]["verdict"]["status"], "WAIT_RETEST")
            self.assertEqual(short["preflight"]["verdict"]["hard_blockers"], [])
            self.assertIn("RR_ADVISORY", short["preflight"]["verdict"]["risk_warnings"])
            self.assertEqual(short["decision_context"]["final"]["status"], "WAIT")
            self.assertFalse(short["decision_context"]["final"]["new_entry_allowed"])
            self.assertTrue(short["preflight"]["safety"]["unified_single_scan"])
