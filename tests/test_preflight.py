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
            self.assertEqual(payload["verdict"]["status"], "ENTRY_READY")
            self.assertTrue(payload["verdict"]["actionable"])
            self.assertIn("接近原止損", payload["entry_position"]["risk_note"])
            self.assertEqual(payload["verdict"]["situation"], "NEAR_INVALIDATION")
            self.assertIn("EXECUTION_COST_ADVISORY", payload["verdict"]["risk_warnings"])
            self.assertNotIn("EXECUTION_COST_TOO_HIGH", payload["verdict"]["hard_blockers"])
            self.assertTrue(payload["plan_state"]["old_plan_reusable_for_new_entry"])
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

    def test_live_quote_volume_uses_200m_entry_and_150m_member_exit_lines(self):
        def with_policy(signal, *, member):
            return _legacy.replace(
                signal,
                data_quality={
                    **signal.data_quality,
                    "universe_volume_policy": {
                        "version": 1,
                        "trusted": True,
                        "member": member,
                        "entry_usdt": 2_000_000.0,
                        "exit_usdt": 1_500_000.0,
                        "effective_min_usdt": 1_500_000.0 if member else 2_000_000.0,
                        "volume_usdt": 20_000_000.0,
                        "volume_status": "AVAILABLE",
                        "source": "PUBLICATION_TICKER",
                    },
                },
            )

        cases = (
            (False, 1_999_999.0, True),
            (False, 2_000_000.0, False),
            (True, 1_499_999.0, True),
            (True, 1_500_000.0, False),
            (True, 1_750_000.0, False),
        )
        for member, volume, warned in cases:
            with self.subTest(member=member, volume=volume):
                signal = with_policy(_legacy.make_signal(), member=member)
                client = _legacy.PreflightClient(price=100.0, quote_volume_24h=volume)
                payload = build_preflight_payload(
                    signal,
                    client.get_ticker(signal.inst_id),
                    client.get_execution_context(signal.inst_id),
                    _legacy.AppConfig(),
                    report_generated_at=_legacy.datetime.now(_legacy.timezone.utc).isoformat(),
                )
                self.assertEqual(payload["verdict"]["status"], "ENTRY_READY")
                self.assertTrue(payload["verdict"]["actionable"])
                self.assertEqual(payload["verdict"]["hard_blockers"], [])
                self.assertEqual(
                    "LIQUIDITY_TOO_LOW" in payload["verdict"]["advisory_blockers"],
                    warned,
                )
                policy = payload["execution"]["liquidity_policy"]
                self.assertEqual(policy["member"], member)
                self.assertEqual(policy["effective_min_usdt"], 1_500_000.0 if member else 2_000_000.0)
                self.assertEqual(payload["live"]["quote_volume_24h_usdt"], volume)

    def test_missing_or_untrusted_live_quote_volume_fails_closed(self):
        signal = _legacy.make_signal()
        signal.data_quality = {
            "universe_volume_policy": {
                "version": 1,
                "trusted": True,
                "member": True,
                "entry_usdt": 2_000_000.0,
                "exit_usdt": 1_400_000.0,
                "effective_min_usdt": 1_400_000.0,
            }
        }
        for volume, expected_code, expected_warning in (
            (None, "QUOTE_VOLUME_DATA_UNAVAILABLE", "LIQUIDITY_DATA_ADVISORY"),
            (-1.0, "QUOTE_VOLUME_DATA_UNAVAILABLE", "LIQUIDITY_DATA_ADVISORY"),
            (1_750_000.0, "LIQUIDITY_TOO_LOW", "LIQUIDITY_ADVISORY"),
        ):
            with self.subTest(volume=volume):
                client = _legacy.PreflightClient(price=100.0, quote_volume_24h=volume)
                payload = build_preflight_payload(
                    signal,
                    client.get_ticker(signal.inst_id),
                    client.get_execution_context(signal.inst_id),
                    _legacy.AppConfig(),
                    report_generated_at=_legacy.datetime.now(_legacy.timezone.utc).isoformat(),
                )
                self.assertTrue(payload["verdict"]["actionable"])
                self.assertEqual(payload["verdict"]["status"], "ENTRY_READY")
                self.assertNotIn(expected_code, payload["verdict"]["hard_blockers"])
                self.assertIn(expected_code, payload["verdict"]["advisory_blockers"])
                self.assertIn(expected_warning, payload["verdict"]["risk_warnings"])
                self.assertFalse(payload["execution"]["liquidity_policy"]["member"])
                if volume is None or volume < 0:
                    self.assertEqual(
                        payload["data_quality"]["required_missing_sources"], []
                    )
                    self.assertIn(
                        "ticker_quote_volume_24h",
                        payload["data_quality"]["optional_missing_sources"],
                    )
                    self.assertTrue(
                        payload["data_quality"]["risk_data_advisory_only"]
                    )
        self.assertEqual(payload["execution"]["liquidity_policy"]["source"], "PREFLIGHT_TICKER")

    def test_fresh_known_high_slippage_still_blocks_partial_book(self):
        signal = _legacy.make_signal()
        client = _legacy.PreflightClient(price=100.0)
        context = _legacy.replace(
            client.get_execution_context(signal.inst_id),
            buy_slippage_pct=0.20,
            sell_slippage_pct=None,
        )
        payload = build_preflight_payload(
            signal,
            client.get_ticker(signal.inst_id),
            context,
            _legacy.AppConfig(),
            report_generated_at=_legacy.datetime.now(_legacy.timezone.utc).isoformat(),
        )
        self.assertEqual(payload["verdict"]["status"], "ENTRY_READY")
        self.assertEqual(payload["verdict"]["hard_blockers"], [])
        self.assertIn("SLIPPAGE_TOO_HIGH", payload["verdict"]["advisory_blockers"])
        self.assertIn("SLIPPAGE_ADVISORY", payload["verdict"]["risk_warnings"])
        self.assertTrue(payload["verdict"]["actionable"])
        self.assertTrue(payload["plan_state"]["new_entry_allowed"])

    def test_stored_hard_gate_cannot_be_cleared_by_ticker_only_preflight(self):
        cases = (
            (
                {"status": "BLOCKED", "blocked": False, "blockers": ["anomaly"]},
                "ENTRY_READY",
                True,
                "MARKET_ANOMALY_ADVISORY",
            ),
            (
                {"status": "UNKNOWN", "unknown": False, "unknowns": ["data_quality"]},
                "DATA_UNAVAILABLE",
                False,
                "DATA_QUALITY",
            ),
        )
        for gate, expected, allowed, expected_code in cases:
            with self.subTest(gate=gate["status"]):
                signal = _legacy.make_signal()
                signal.entry_eligibility["closed_retest_confirmed"] = True
                signal.decision_context = {
                    "hard_gate": gate,
                    "final": {"status": "ENTER", "new_entry_allowed": False},
                }
                client = _legacy.PreflightClient(price=100.0)
                payload = build_preflight_payload(
                    signal,
                    client.get_ticker(signal.inst_id),
                    client.get_execution_context(signal.inst_id),
                    _legacy.AppConfig(),
                    report_generated_at=_legacy.datetime.now(_legacy.timezone.utc).isoformat(),
                )
                self.assertEqual(payload["verdict"]["status"], expected)
                self.assertEqual(payload["verdict"]["actionable"], allowed)
                self.assertEqual(payload["plan_state"]["new_entry_allowed"], allowed)
                self.assertIn(expected_code, payload["verdict"]["risk_warnings"])
                self.assertTrue(payload["plan_state"]["existing_position_plan_active"])
