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

    def test_latest_confirmation_blocks_unspecified_failed_safety_check(self):
        for key in (
            "spread",
            "slippage",
            "execution_cost",
            "risk_reward",
            "liquidity",
            "anomalous_market",
            "depth",
        ):
            with self.subTest(advisory_key=key):
                current = _legacy.allow_entry(_legacy.signal())
                current.safety_checks = [
                    {"key": key, "passed": False, "hard": True}
                ]
                confirmation = _legacy._latest_confirmation(
                    _legacy.SimpleNamespace(signal=current, market_state=None),
                    "LONG",
                )
                self.assertEqual(confirmation["status"], "REVALIDATED")
                self.assertEqual(confirmation["hard_blockers"], [])
                self.assertIn(key, confirmation["risk_warnings"])
                self.assertTrue(confirmation["new_entry_allowed"])

        required = _legacy.allow_entry(_legacy.signal())
        required.safety_checks = [
            {"key": "future_required_check", "passed": False, "hard": True}
        ]
        confirmation = _legacy._latest_confirmation(
            _legacy.SimpleNamespace(signal=required, market_state=None),
            "LONG",
        )
        self.assertEqual(confirmation["status"], "HARD_GATE_BLOCKED")
        self.assertEqual(
            confirmation["hard_blockers"], ["future_required_check"]
        )
        self.assertFalse(confirmation["new_entry_allowed"])

    def test_latest_confirmation_uses_gate_status_lists_and_unknown_hard_checks(self):
        advisory = _legacy.allow_entry(_legacy.signal())
        advisory.decision_context = {
            "hard_gate": {
                "status": "BLOCKED",
                "blocked": True,
                "blockers": ["spread"],
            },
            "final": {"status": "HARD_GATE_BLOCKED", "new_entry_allowed": False},
        }
        confirmation = _legacy._latest_confirmation(
            _legacy.SimpleNamespace(signal=advisory, market_state=None),
            "LONG",
        )
        self.assertEqual(confirmation["status"], "REVALIDATED")
        self.assertEqual(confirmation["hard_blockers"], [])
        self.assertIn("spread", confirmation["risk_warnings"])

        optional_unknown = _legacy.allow_entry(_legacy.signal())
        optional_unknown.safety_checks = [
            {"key": "depth", "passed": None, "hard": True}
        ]
        confirmation = _legacy._latest_confirmation(
            _legacy.SimpleNamespace(signal=optional_unknown, market_state=None),
            "LONG",
        )
        self.assertEqual(confirmation["status"], "REVALIDATED")
        self.assertTrue(confirmation["new_entry_allowed"])

        required_unknown = _legacy.allow_entry(_legacy.signal())
        required_unknown.safety_checks = [
            {"key": "entry_inputs_available", "passed": None, "hard": True}
        ]
        confirmation = _legacy._latest_confirmation(
            _legacy.SimpleNamespace(signal=required_unknown, market_state=None),
            "LONG",
        )
        self.assertEqual(confirmation["status"], "DATA_UNAVAILABLE")
        self.assertFalse(confirmation["new_entry_allowed"])

        for gate_status, expected, allowed in (
            ("HARD_GATE_BLOCKED", "HARD_GATE_BLOCKED", False),
            ("ANOMALY", "REVALIDATED", True),
            ("DATA_UNAVAILABLE", "DATA_UNAVAILABLE", False),
        ):
            with self.subTest(gate_status=gate_status):
                current = _legacy.allow_entry(_legacy.signal())
                current.decision_context["hard_gate"] = {"status": gate_status}
                confirmation = _legacy._latest_confirmation(
                    _legacy.SimpleNamespace(signal=current, market_state=None),
                    "LONG",
                )
                self.assertEqual(confirmation["status"], expected)
                self.assertEqual(confirmation["new_entry_allowed"], allowed)

    def test_merge_and_canonical_require_all_entry_permissions_to_agree(self):
        ready = {
            "direction": "LONG",
            "verdict": {
                "status": "ENTRY_READY",
                "situation": "IN_ENTRY_AREA",
                "actionable": True,
                "new_entry_allowed": True,
                "hard_blockers": [],
            },
            "signal_lifecycle": {
                "status": "ACTIVE",
                "active": True,
                "triggered": True,
                "terminal": False,
            },
            "plan_state": {
                "status": "ACTIVE",
                "new_entry_status": "READY",
                "new_entry_allowed": True,
                "old_plan_reusable": True,
                "old_plan_reusable_for_new_entry": True,
                "direction_still_valid": True,
            },
            "data_quality": {"status": "AVAILABLE", "missing_sources": []},
        }
        cases = (
            (
                {"status": "OPPOSITE_SIGNAL", "hard_blockers": ["OPPOSITE_SIGNAL"]},
                "HARD_GATE_BLOCKED",
            ),
            (
                {"status": "HARD_GATE_BLOCKED", "hard_blockers": ["spread"]},
                "ENTER",
            ),
            ({"status": "DATA_UNAVAILABLE"}, "DATA_UNAVAILABLE"),
            (
                {
                    "status": "REVALIDATED",
                    "new_entry_allowed": False,
                    "hard_blockers": [],
                },
                "WAIT",
            ),
            (
                {
                    "status": "REVALIDATED",
                    "new_entry_allowed": True,
                    "hard_blockers": ["spread"],
                },
                "ENTER",
            ),
        )
        for confirmation, expected in cases:
            with self.subTest(confirmation=confirmation):
                direct = _legacy._canonical_single_decision(
                    _legacy.allow_entry(_legacy.signal()), ready, confirmation
                )
                self.assertEqual(direct["final"]["status"], expected)
                self.assertEqual(
                    direct["final"]["new_entry_allowed"], expected == "ENTER"
                )

                merged = _legacy._merge_preflight_confirmation(ready, confirmation)
                canonical = _legacy._canonical_single_decision(
                    _legacy.allow_entry(_legacy.signal()),
                    merged,
                    merged["latest_confirmation"],
                )
                self.assertEqual(canonical["final"]["status"], expected)
                self.assertEqual(
                    canonical["final"]["new_entry_allowed"], expected == "ENTER"
                )

    def test_optional_execution_estimate_gap_does_not_block_ready_entry(self):
        confirmation = {
            "status": "REVALIDATED",
            "new_entry_allowed": True,
            "hard_blockers": [],
            "risk_warnings": ["EXECUTION_DATA_UNAVAILABLE"],
        }

        def payload(source):
            return {
                "direction": "LONG",
                "verdict": {
                    "status": "ENTRY_READY",
                    "situation": "IN_ENTRY_AREA",
                    "actionable": True,
                    "new_entry_allowed": True,
                    "hard_blockers": [],
                    "risk_warnings": ["EXECUTION_DATA_ADVISORY"],
                },
                "signal_lifecycle": {
                    "status": "ACTIVE",
                    "active": True,
                    "triggered": True,
                    "terminal": False,
                },
                "plan_state": {
                    "status": "ACTIVE",
                    "new_entry_status": "READY",
                    "new_entry_allowed": True,
                    "old_plan_reusable": True,
                    "old_plan_reusable_for_new_entry": True,
                    "direction_still_valid": True,
                },
                "data_quality": {
                    "status": "PARTIAL",
                    "ticker_available": True,
                    "missing_sources": [source],
                    "required_missing_sources": [source],
                    "optional_missing_sources": [],
                },
            }

        for source in ("ticker_quote_volume_24h", "order_book_depth"):
            with self.subTest(advisory_source=source):
                merged = _legacy._merge_preflight_confirmation(
                    payload(source), confirmation
                )
                decision = _legacy._canonical_single_decision(
                    _legacy.allow_entry(_legacy.signal()),
                    merged,
                    merged["latest_confirmation"],
                )
                self.assertEqual(decision["final"]["status"], "ENTER")
                self.assertTrue(decision["final"]["new_entry_allowed"])

        required = payload("core_15m")
        decision = _legacy._canonical_single_decision(
            _legacy.allow_entry(_legacy.signal()), required, confirmation
        )
        self.assertEqual(decision["final"]["status"], "DATA_UNAVAILABLE")
        self.assertFalse(decision["final"]["new_entry_allowed"])

    def test_legacy_denial_markers_cannot_become_enter(self):
        def ready_payload():
            return {
                "direction": "LONG",
                "verdict": {
                    "status": "ENTRY_READY",
                    "situation": "IN_ENTRY_AREA",
                    "actionable": True,
                    "new_entry_allowed": True,
                    "hard_blockers": [],
                    "risk_warnings": [],
                },
                "signal_lifecycle": {
                    "status": "ACTIVE",
                    "active": True,
                    "triggered": True,
                    "terminal": False,
                },
                "plan_state": {
                    "status": "ACTIVE",
                    "new_entry_status": "READY",
                    "new_entry_allowed": True,
                    "old_plan_reusable": True,
                    "old_plan_reusable_for_new_entry": True,
                    "direction_still_valid": True,
                },
                "data_quality": {"status": "AVAILABLE", "missing_sources": []},
            }

        confirmation = {
            "status": "REVALIDATED",
            "new_entry_allowed": True,
            "hard_blockers": [],
            "risk_warnings": [],
        }
        for code in (
            "SPREAD_TOO_HIGH",
            "EXECUTION_DATA_UNAVAILABLE",
            "LIQUIDITY_TOO_LOW",
            "RR_INSUFFICIENT",
        ):
            with self.subTest(advisory_verdict_code=code):
                payload = ready_payload()
                payload["verdict"]["risk_warnings"] = [code]
                merged = _legacy._merge_preflight_confirmation(payload, confirmation)
                decision = _legacy._canonical_single_decision(
                    _legacy.allow_entry(_legacy.signal()),
                    merged,
                    merged["latest_confirmation"],
                )
                self.assertEqual(decision["final"]["status"], "ENTER")

        advisory_quality = ready_payload()
        advisory_quality["data_quality"]["missing_sources"] = ["order_book"]
        decision = _legacy._canonical_single_decision(
            _legacy.allow_entry(_legacy.signal()),
            advisory_quality,
            confirmation,
        )
        self.assertEqual(decision["final"]["status"], "ENTER")

        required_quality = ready_payload()
        required_quality["data_quality"]["missing_sources"] = ["core_15m"]
        decision = _legacy._canonical_single_decision(
            _legacy.allow_entry(_legacy.signal()), required_quality, confirmation
        )
        self.assertEqual(decision["final"]["status"], "DATA_UNAVAILABLE")

        for code, expected in (
            ("OPPOSITE_SIGNAL", "HARD_GATE_BLOCKED"),
            ("SIGNAL_DATA_UNAVAILABLE", "DATA_UNAVAILABLE"),
            ("SPREAD_TOO_HIGH", "ENTER"),
            ("EXECUTION_DATA_UNAVAILABLE", "ENTER"),
        ):
            with self.subTest(confirmation_code=code):
                current_confirmation = {
                    **confirmation,
                    "risk_warnings": [code],
                }
                merged = _legacy._merge_preflight_confirmation(
                    ready_payload(), current_confirmation
                )
                decision = _legacy._canonical_single_decision(
                    _legacy.allow_entry(_legacy.signal()),
                    merged,
                    merged["latest_confirmation"],
                )
                self.assertEqual(decision["final"]["status"], expected)
                self.assertEqual(
                    decision["final"]["new_entry_allowed"], expected == "ENTER"
                )
