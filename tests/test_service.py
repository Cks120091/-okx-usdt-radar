import radar.service as _service
from radar.service_entry_policy import POLICY_VERSION, apply_service_entry_policy


apply_service_entry_policy(_service)

from tests import legacy_service_cases as _legacy


for _name in dir(_legacy):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_legacy, _name)


class RuntimeSafetyTests(_legacy.RuntimeSafetyTests):
    def test_single_scan_and_preflight_share_one_same_direction_decision(self):
        with _legacy.tempfile.TemporaryDirectory() as directory:
            scanner = _legacy.SingleInstrumentScanner()
            runtime = _legacy.RadarRuntime(scanner, _legacy.AppConfig(data_dir=directory))
            runtime._latest = _legacy.report()
            payload = runtime.scan_instrument_dict("AAA")
            short = payload["short"]

            confirmation = short["latest_confirmation"]
            self.assertEqual(confirmation["status"], "REVALIDATED")
            self.assertEqual(
                confirmation["source_status"],
                "ORIGINAL_DIRECTION_STABLE",
            )
            self.assertFalse(confirmation["formal_trigger"])
            self.assertTrue(confirmation["stable_episode_entry_reuse"])
            self.assertEqual(
                confirmation["entry_confirmation_policy"],
                POLICY_VERSION,
            )

            self.assertEqual(short["preflight"]["verdict"]["status"], "ENTRY_READY")
            self.assertTrue(short["preflight"]["verdict"]["actionable"])
            self.assertEqual(short["preflight"]["verdict"]["hard_blockers"], [])
            self.assertIn(
                "RR_ADVISORY",
                short["preflight"]["verdict"]["risk_warnings"],
            )
            self.assertTrue(short["preflight"]["plan_state"]["new_entry_allowed"])

            self.assertEqual(short["decision_context"]["final"]["status"], "ENTER")
            self.assertTrue(
                short["decision_context"]["final"]["new_entry_allowed"]
            )
            self.assertTrue(short["preflight"]["safety"]["unified_single_scan"])
