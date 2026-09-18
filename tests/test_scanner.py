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
            "high_slippage": ({"buy_slippage_pct": 0.30}, "SLIPPAGE_TOO_HIGH", True),
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

    def test_candidate_volume_policy_uses_exact_hysteresis_boundaries(self):
        scanner = _legacy.MarketScanner(
            _legacy.FakeClient(),
            _legacy.ScannerConfig(
                min_quote_volume_24h=2_000_000,
                quote_volume_buffer_24h=500_000,
            ),
        )
        cases = (
            (False, 1_999_999.0, True, 2_000_000.0),
            (False, 2_000_000.0, False, 2_000_000.0),
            (True, 1_499_999.0, True, 1_500_000.0),
            (True, 1_500_000.0, False, 1_500_000.0),
        )
        for member, volume, warned, effective_min in cases:
            with self.subTest(member=member, volume=volume):
                scanner._volume_eligible_ids = {"AAA-USDT-SWAP"} if member else set()
                ticker = _legacy.Ticker(
                    "AAA-USDT-SWAP",
                    100.0,
                    99.99,
                    100.01,
                    int(_legacy.time.time() * 1_000),
                    volume,
                )
                projected = scanner._apply_publication_ticker_to_item(
                    _legacy.qualified_signal(),
                    ticker=ticker,
                    scan_start_ticker=ticker,
                    failure=None,
                )
                decided = scanner._attach_decision_context(projected)
                policy = decided.data_quality["universe_volume_policy"]
                self.assertEqual(policy["member"], member)
                self.assertEqual(policy["entry_usdt"], 2_000_000.0)
                self.assertEqual(policy["exit_usdt"], 1_500_000.0)
                self.assertEqual(policy["effective_min_usdt"], effective_min)
                self.assertTrue(decided.actionable)
                self.assertNotIn("liquidity", decided.decision_context["hard_gate"]["blockers"])
                self.assertEqual(
                    any("24H 成交額低於" in value for value in decided.decision_context["hard_gate"]["warnings"]),
                    warned,
                )

    def test_missing_publication_volume_fails_closed_without_candle_fallback(self):
        class MissingPublicationVolumeClient(_legacy.VolumeFilterClient):
            def __init__(self):
                super().__init__({"AAA-USDT-SWAP": 2_100_000.0})
                self.ticker_calls = 0

            def get_swap_tickers(self):
                self.ticker_calls += 1
                volume = 2_100_000.0 if self.ticker_calls == 1 else None
                return {
                    "AAA-USDT-SWAP": _legacy.Ticker(
                        "AAA-USDT-SWAP",
                        100.0,
                        99.99,
                        100.01,
                        self.ticker_calls,
                        volume,
                    )
                }

        client = MissingPublicationVolumeClient()
        scanner = _legacy.MarketScanner(
            client,
            _legacy.ScannerConfig(
                workers=1,
                min_quote_volume_24h=2_000_000,
                quote_volume_buffer_24h=500_000,
            ),
        )
        scanner.engine = _legacy.AlwaysSignalEngine()
        report = scanner.scan_once(scan_mode="SHORT")
        self.assertEqual(report.data_quality["publication_ticker_status"], "PARTIAL")
        self.assertEqual(report.data_quality["publication_ticker_refreshed_count"], 1)
        self.assertEqual(len(report.signals), 1)
        signal = report.signals[0]
        self.assertIsNone(signal.quote_volume_24h)
        self.assertEqual(signal.market_metrics["entry_execution_price"], 100.01)
        self.assertEqual(signal.data_quality["publication_ticker_status"], "AVAILABLE")
        self.assertEqual(signal.data_quality["universe_volume_policy"]["volume_status"], "UNAVAILABLE")
        liquidity = next(
            row for row in signal.decision_context["hard_gate"]["checks"]
            if row["key"] == "liquidity"
        )
        self.assertEqual(liquidity["status"], "UNKNOWN")
        self.assertFalse(liquidity["hard"])
        self.assertNotIn("liquidity", signal.decision_context["hard_gate"]["blockers"])
        self.assertTrue(any("缺少 24H 成交額" in value for value in signal.decision_context["hard_gate"]["warnings"]))

        ticker = _legacy.Ticker(
            "AAA-USDT-SWAP",
            100.0,
            99.99,
            100.01,
            int(_legacy.time.time() * 1_000),
            None,
        )
        projected = scanner._apply_publication_ticker_to_item(
            _legacy.qualified_signal(),
            ticker=ticker,
            scan_start_ticker=ticker,
            failure="volume missing",
        )
        decided = scanner._attach_decision_context(
            scanner._refresh_entry_eligibility(projected)
        )
        self.assertTrue(decided.actionable)
        self.assertEqual(decided.decision_context["final"]["status"], "ENTER")

    def test_publication_volume_must_be_finite_and_nonnegative(self):
        class PublicationClient(_legacy.FakeClient):
            def __init__(self):
                super().__init__()
                self.instruments = self.instruments[:1]
                self.volume = 0.0

            def get_swap_tickers(self):
                return {
                    "AAA-USDT-SWAP": _legacy.Ticker(
                        "AAA-USDT-SWAP",
                        100.0,
                        99.99,
                        100.01,
                        2,
                        self.volume,
                    )
                }

        client = PublicationClient()
        scanner = _legacy.MarketScanner(client)
        scan_start = {
            "AAA-USDT-SWAP": _legacy.Ticker(
                "AAA-USDT-SWAP",
                100.0,
                99.99,
                100.01,
                1,
                2_000_000.0,
            )
        }
        for invalid in (None, -1.0, float("nan"), float("inf")):
            with self.subTest(volume=invalid):
                client.volume = invalid
                accepted, failures = scanner._load_publication_tickers(
                    ["AAA-USDT-SWAP"],
                    scan_start,
                )
                self.assertIn("AAA-USDT-SWAP", accepted)
                self.assertIn("24H USDT 成交額", failures["AAA-USDT-SWAP"])
                projected = scanner._apply_publication_ticker_to_item(
                    _legacy.qualified_signal(),
                    ticker=accepted["AAA-USDT-SWAP"],
                    scan_start_ticker=scan_start["AAA-USDT-SWAP"],
                    failure=failures["AAA-USDT-SWAP"],
                )
                self.assertEqual(projected.market_metrics["entry_execution_price"], 100.01)
                self.assertIsNone(projected.quote_volume_24h)
                self.assertEqual(projected.data_quality["universe_volume_policy"]["volume_status"], "UNAVAILABLE")

        client.volume = 0.0
        accepted, failures = scanner._load_publication_tickers(
            ["AAA-USDT-SWAP"],
            scan_start,
        )
        self.assertIn("AAA-USDT-SWAP", accepted)
        self.assertEqual(failures, {})

    def test_publication_spread_block_is_persisted_as_not_entry_ready(self):
        class SpreadAdvisoryClient(_legacy.FakeClient):
            def __init__(self):
                super().__init__()
                self.instruments = self.instruments[:1]
                self.ticker_calls = 0

            def get_swap_tickers(self):
                self.ticker_calls += 1
                item = self.instruments[0]
                return {
                    item.inst_id: _legacy.Ticker(
                        item.inst_id,
                        100.0,
                        99.99,
                        100.01,
                        int(_legacy.time.time() * 1_000) + self.ticker_calls,
                        20_000_000.0,
                    )
                }

        class EntryReadyEngine:
            def analyze(self, instrument, ticker, *args, **kwargs):
                raw = _legacy.replace(
                    _legacy.qualified_signal(instrument.inst_id),
                    trigger_id="",
                    lifecycle={
                        "current_stage": "CONFIRMED",
                        "transition": "TECHNICAL_EVENT",
                    },
                    market_metrics={
                        **_legacy.qualified_signal(instrument.inst_id).market_metrics,
                        "last_price": ticker.last,
                    },
                )
                return _legacy.AnalysisResult(
                    raw,
                    "qualified",
                    _legacy.replace(
                        _legacy.qualified_state(raw),
                        lifecycle={
                            "current_stage": "CONFIRMED",
                            "transition": "TECHNICAL_SNAPSHOT",
                        },
                    ),
                )

        client = SpreadAdvisoryClient()
        scanner = _legacy.MarketScanner(
            client,
            _legacy.ScannerConfig(
                workers=1,
                min_quote_volume_24h=0,
                minimum_rr=1.0,
                max_spread_pct=0.01,
            ),
        )
        scanner.engine = EntryReadyEngine()
        report = scanner.scan_once(scan_mode="SHORT")
        self.assertEqual(client.ticker_calls, 2)
        published = report.signals[0]
        self.assertEqual(published.entry_eligibility["status"], "ENTRY_READY")
        self.assertEqual(published.decision_context["final"]["status"], "ENTER")
        self.assertTrue(published.actionable)
        self.assertNotIn("spread", published.decision_context["hard_gate"]["blockers"])
        self.assertTrue(any("Spread" in value for value in published.decision_context["hard_gate"]["warnings"]))
        persisted = scanner.repository.load_active_signal(published.inst_id, "SHORT")
        self.assertIsNotNone(persisted)
        self.assertTrue(persisted.actionable)
        self.assertTrue(persisted.entry_eligibility.get("actionable"))
        self.assertTrue(persisted.entry_eligibility.get("new_entry_allowed"))
        self.assertTrue(persisted.lifecycle.get("entry_ready_once"))
        self.assertEqual(persisted.decision_context.get("final", {}).get("status"), "ENTER")
