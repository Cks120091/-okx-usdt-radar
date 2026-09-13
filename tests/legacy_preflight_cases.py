import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import patch

from radar.config import AppConfig
from radar.models import MarketContext, RadarReport, Signal, Ticker
from radar.price_display import display_precision_from_tick_size
from radar.preflight import build_preflight_payload
from radar.public_payload import public_candidate_payload
from radar.service import PreflightError, RadarRuntime, serve


def make_signal() -> Signal:
    now_ms = int(time.time() * 1000)
    return Signal(
        inst_id="AAA-USDT-SWAP",
        direction="LONG",
        strategy="突破與價格接受",
        score=82.0,
        evidence=["fixture"],
        entry_low="99.8",
        entry_high="100.2",
        stop_loss="98",
        take_profit_1="104.2",
        take_profit_2="106",
        risk_reward=2.0,
        invalidation="跌破結構低點",
        spread_pct=0.02,
        quote_volume_24h=20_000_000,
        closed_candle_ts=now_ms - 900_000,
        regime="TREND",
        signal_stage="EARLY_SIGNAL",
        readiness_score=82.0,
        radar_horizon="SHORT",
        trigger_type="BREAKOUT",
        trigger_id="old-trigger-id",
        freshness="NEW",
        market_metrics={
            "last_price": 100.0,
            "instrument_tick_size": 0.05,
        },
        management_plan={
            "target_rr_model": 2.625,
            "tp2_rr_model": 3.75,
        },
        market_story={
            "raw": {"core_atr": 2.0},
            "trigger": {
                "event_ts": now_ms - 900_000,
                "event_age_bars": 1,
                "trigger_event_key": "old-trigger-event",
            },
        },
        lifecycle={
            "age_bars": 1,
            "event_key": "old-trigger-event",
            "triggered_at": datetime.fromtimestamp(
                (now_ms - 900_000) / 1000,
                tz=timezone.utc,
            ).isoformat(),
        },
        execution_quality={"score": 87.0, "label": "良好"},
        entry_eligibility={
            "status": "ENTRY_READY",
            "label": "目前可進",
            "actionable": True,
        },
    )


def make_report(item: Signal) -> RadarReport:
    stamp = datetime.now(timezone.utc).isoformat()
    return RadarReport(
        status="SIGNALS_FOUND",
        generated_at=stamp,
        scope="fixture",
        target_count=1,
        fetched_count=1,
        analyzable_count=1,
        coverage_pct=100.0,
        target_instruments=[item.inst_id],
        failed_instruments={},
        signals=[item] if item.radar_horizon == "SHORT" else [],
        exclusion_counts={},
        duration_seconds=1.0,
        message="fixture",
        completed_at=stamp,
        long_signals=[item] if item.radar_horizon == "LONG" else [],
    )


def make_capital_flow(as_of_close_ms: int = 1_700_000_000_000) -> dict:
    windows = {}
    for key, hours, required in (("1h", 1, 8), ("2h", 2, 15), ("4h", 4, 29)):
        windows[key] = {
            "key": key,
            "hours": hours,
            "ready": True,
            "sample_count": required,
            "required_sample_count": required,
            "baseline_window_count": 6,
            "state": "LARGE_LONG",
            "label": f"{hours}H 推定大量偏多資金流入",
            "as_of_close_ms": as_of_close_ms,
            "latest_value": 102_000_000.0,
            "window_start_value": 100_000_000.0,
            "change_amount": 2_000_000.0,
            "change_pct": 2.0,
            "baseline_average_change_pct": 0.8,
            "change_vs_average_ratio": 2.5,
            "above_average": True,
            "large_inflow": True,
            "persistence_pct": 100.0,
            "unit": "CONTRACTS",
            "directional_bias": "LONG",
            "directional_bias_label": "價格推定偏多",
            "price_return_pct": 0.6,
            "price_consistency_pct": 100.0,
            "samples": [{"private": True}],
        }
    windows["8h"] = {"ready": True, "samples": [{"private": True}]}
    return {
        "algorithm_version": "CAPITAL_FLOW_LOOKBACK_V1",
        "source_mode": "HISTORICAL_CLOSED_1H_AT_SCAN",
        "status": "READY",
        "sample_count": 29,
        "required_sample_count": 29,
        "baseline_window_count": 6,
        "as_of_close_ms": as_of_close_ms,
        "detected": True,
        "strongest_window": "4h",
        "headline_state": "LARGE_LONG",
        "headline_direction": "LONG",
        "headline_label": "發現相對異常增倉，價格推定偏多主導",
        "direction_basis": "SAME_WINDOW_PRICE_ACTION_INFERENCE",
        "long_short_split_available": False,
        "minimum_change_pct": 0.5,
        "large_ratio_threshold": 1.5,
        "persistence_threshold_pct": 60.0,
        "meaning": "fixture",
        "permission": "ADVISORY_ONLY_NEVER_CHANGES_TRIGGER_OR_PLAN",
        "windows": windows,
        "raw_points": [{"private": True}],
    }


class PreflightClient:
    def __init__(
        self,
        price: float = 100.1,
        quote_volume_24h: float | None = 20_000_000.0,
    ):
        self.price = price
        self.quote_volume_24h = quote_volume_24h
        self.ticker_calls = 0
        self.context_calls = 0

    def get_ticker(self, inst_id: str) -> Ticker:
        self.ticker_calls += 1
        now_ms = int(time.time() * 1000)
        return Ticker(
            inst_id=inst_id,
            last=self.price,
            bid=self.price - 0.01,
            ask=self.price + 0.01,
            ts=now_ms,
            quote_volume_24h=self.quote_volume_24h,
        )

    def get_execution_context(self, inst_id: str) -> MarketContext:
        self.context_calls += 1
        now_ms = int(time.time() * 1000)
        return MarketContext(
            inst_id=inst_id,
            open_interest_usd=None,
            funding_rate=None,
            order_book_imbalance=0.12,
            taker_buy_ratio=None,
            sampled_at=now_ms,
            bid_depth_usd=25_000,
            ask_depth_usd=22_000,
            buy_slippage_pct=0.01,
            sell_slippage_pct=0.012,
            execution_notional_usdt=1_000,
            best_bid=self.price - 0.01,
            best_ask=self.price + 0.01,
            source_timestamps={"order_book": now_ms},
        )


class MutatingPreflightClient(PreflightClient):
    def __init__(self, price: float = 100.1):
        super().__init__(price)
        self.after_context = None

    def get_execution_context(self, inst_id: str) -> MarketContext:
        context = super().get_execution_context(inst_id)
        if self.after_context is not None:
            self.after_context()
        return context


class PreflightScanner:
    def __init__(self, client: PreflightClient):
        self.client = client


class ContinuationPreflightScanner(PreflightScanner):
    def refresh_continuation_for_signal(self, signal):
        return {
            "key": "CONFIRMED",
            "core_votes": {
                "OI": {"state": "SUPPORT"},
                "TAKER_CVD": {"state": "SUPPORT"},
                "VOLUME": {"state": "SUPPORT"},
            },
            "observer": {
                "status": "READY",
                "primary_window": "10m",
                "as_of_close_ms": 1_700_000_000_000,
                "windows": {"10m": {"ready": True}},
                "capital_flow": make_capital_flow(),
            },
        }


class FailingContinuationPreflightScanner(PreflightScanner):
    def refresh_continuation_for_signal(self, signal):
        raise RuntimeError("fixture history unavailable")


class FullCapablePreflightScanner(PreflightScanner):
    def __init__(self, client: PreflightClient):
        super().__init__(client)
        self.single_scan_calls = 0

    def scan_instrument(self, *args, **kwargs):
        self.single_scan_calls += 1
        raise AssertionError("進場前更新不應啟動多週期幣種掃描")


class ReanalysisPreflightScanner(PreflightScanner):
    def __init__(self, client: PreflightClient, new_signal: Signal | None):
        super().__init__(client)
        self.new_signal = new_signal
        self.reanalysis_calls = 0
        self.commit_calls = 0

    def reanalyze_instrument(self, previous_signal, market_bias):
        self.reanalysis_calls += 1
        return SimpleNamespace(
            previous_signal=previous_signal,
            ticker=self.client.get_ticker(previous_signal.inst_id),
            context=self.client.get_execution_context(previous_signal.inst_id),
            market_state=None,
            raw_signal=self.new_signal,
            analyzed_at=datetime.now(timezone.utc).isoformat(),
            reason="qualified" if self.new_signal is not None else "no_fresh_trigger",
        )

    def commit_single_reanalysis(self, analysis):
        self.commit_calls += 1
        return analysis.raw_signal


def make_new_short_signal() -> Signal:
    now_ms = int(time.time() * 1000)
    item = make_signal()
    return replace(
        item,
        direction="SHORT",
        entry_low="97",
        entry_high="98",
        stop_loss="99",
        take_profit_1="94",
        take_profit_2="92",
        trigger_id="new-trigger-id",
        trigger_type="REVERSAL",
        market_metrics={"last_price": 97.5},
        market_story={
            "raw": {"core_atr": 2.0},
            "trigger": {
                "event_ts": now_ms,
                "event_age_bars": 0,
                "trigger_event_key": "new-short-trigger-event",
            },
        },
        lifecycle={
            "age_bars": 0,
            "event_key": "new-short-trigger-event",
            "triggered_at": datetime.fromtimestamp(
                now_ms / 1000,
                tz=timezone.utc,
            ).isoformat(),
        },
        data_timestamp=now_ms,
    )


class PreflightTests(unittest.TestCase):
    def test_http_preflight_routes_require_and_forward_expected_trigger_id(self):
        captured = {}

        class FakeServer:
            def __init__(self, address, handler):
                captured["handler"] = handler

            def serve_forever(self, poll_interval=0.5):
                return None

            def server_close(self):
                return None

        class FakeRuntime:
            def __init__(self):
                self.calls = []

            def preflight_dict(self, inst_id, horizon, expected_trigger_id=None):
                self.calls.append((inst_id, horizon, expected_trigger_id))
                if expected_trigger_id == "trigger-mismatch":
                    raise PreflightError(
                        HTTPStatus.CONFLICT,
                        "Trigger changed",
                        code="TRIGGER_MISMATCH",
                        details={
                            "expected_trigger_id": expected_trigger_id,
                            "current_trigger_id": "trigger-current",
                        },
                    )
                return {"trigger_id": expected_trigger_id}

            def stop(self):
                return None

        runtime = FakeRuntime()
        with patch("radar.service.ThreadingHTTPServer", FakeServer):
            serve(runtime, "127.0.0.1", 0)
        handler_class = captured["handler"]

        missing = object.__new__(handler_class)
        missing.path = "/api/preflight?inst_id=AAA-USDT-SWAP&horizon=SHORT"
        missing_responses = []
        missing._send_json = lambda status, payload: missing_responses.append(
            (status, payload)
        )
        missing.do_GET()
        self.assertEqual(missing_responses[0][0].value, 400)
        self.assertEqual(
            missing_responses[0][1]["code"],
            "EXPECTED_TRIGGER_REQUIRED",
        )
        self.assertEqual(runtime.calls, [])

        missing_post = object.__new__(handler_class)
        missing_post.path = "/api/preflight/reanalyze"
        missing_post_responses = []
        missing_post._read_json_body = lambda: {
            "inst_id": "AAA-USDT-SWAP",
            "horizon": "SHORT",
        }
        missing_post._send_json = (
            lambda status, payload: missing_post_responses.append(
                (status, payload)
            )
        )
        missing_post.do_POST()
        self.assertEqual(missing_post_responses[0][0].value, 400)
        self.assertEqual(
            missing_post_responses[0][1]["code"],
            "EXPECTED_TRIGGER_REQUIRED",
        )
        self.assertEqual(runtime.calls, [])

        get_handler = object.__new__(handler_class)
        get_handler.path = (
            "/api/preflight?inst_id=AAA-USDT-SWAP&horizon=SHORT"
            "&expected_trigger_id=trigger-get"
        )
        get_responses = []
        get_handler._send_json = lambda status, payload: get_responses.append(
            (status, payload)
        )
        get_handler.do_GET()

        post_handler = object.__new__(handler_class)
        post_handler.path = "/api/preflight/reanalyze"
        post_responses = []
        post_handler._read_json_body = lambda: {
            "inst_id": "AAA-USDT-SWAP",
            "horizon": "LONG",
            "expected_trigger_id": "trigger-post",
        }
        post_handler._send_json = lambda status, payload: post_responses.append(
            (status, payload)
        )
        post_handler.do_POST()

        mismatch_handler = object.__new__(handler_class)
        mismatch_handler.path = (
            "/api/preflight?inst_id=AAA-USDT-SWAP&horizon=SHORT"
            "&expected_trigger_id=trigger-mismatch"
        )
        mismatch_responses = []
        mismatch_handler._send_json = lambda status, payload: mismatch_responses.append(
            (status, payload)
        )
        mismatch_handler.do_GET()

        self.assertEqual(
            runtime.calls,
            [
                ("AAA-USDT-SWAP", "SHORT", "trigger-get"),
                ("AAA-USDT-SWAP", "LONG", "trigger-post"),
                ("AAA-USDT-SWAP", "SHORT", "trigger-mismatch"),
            ],
        )
        self.assertEqual(get_responses[0][0].value, 200)
        self.assertEqual(post_responses[0][0].value, 200)
        self.assertEqual(mismatch_responses[0][0].value, 409)
        self.assertEqual(
            mismatch_responses[0][1]["code"],
            "TRIGGER_MISMATCH",
        )

    def test_frozen_plan_display_contract_uses_tick_and_model_r(self):
        signal = make_signal()
        client = PreflightClient(price=100.0)

        preflight = build_preflight_payload(
            signal,
            client.get_ticker(signal.inst_id),
            client.get_execution_context(signal.inst_id),
            AppConfig(),
            report_generated_at=datetime.now(timezone.utc).isoformat(),
        )
        public_signal = public_candidate_payload(signal, signal=True)

        for payload in (preflight, public_signal):
            self.assertEqual(payload["trigger_id"], "old-trigger-id")
            self.assertEqual(payload["instrument_tick_size"], 0.05)
            self.assertEqual(payload["display_precision"], 2)
            self.assertEqual(payload["tp1_r"], 2.0)
            self.assertEqual(payload["tp2_r"], 3.75)

        legacy = replace(signal, management_plan={})
        legacy_payload = public_candidate_payload(legacy, signal=True)
        self.assertEqual(legacy_payload["tp1_r"], 2.0)
        self.assertIsNone(legacy_payload["tp2_r"])

    def test_live_quote_volume_uses_200m_entry_and_150m_member_exit_lines(self):
        def with_policy(signal: Signal, *, member: bool) -> Signal:
            return replace(
                signal,
                data_quality={
                    **signal.data_quality,
                    "universe_volume_policy": {
                        "version": 1,
                        "trusted": True,
                        "member": member,
                        "entry_usdt": 2_000_000.0,
                        "exit_usdt": 1_500_000.0,
                        "effective_min_usdt": (
                            1_500_000.0 if member else 2_000_000.0
                        ),
                        "volume_usdt": 20_000_000.0,
                        "volume_status": "AVAILABLE",
                        "source": "PUBLICATION_TICKER",
                    },
                },
            )

        cases = (
            (False, 1_999_999.0, "HARD_GATE_BLOCKED"),
            (False, 2_000_000.0, "ENTRY_READY"),
            (True, 1_499_999.0, "HARD_GATE_BLOCKED"),
            (True, 1_500_000.0, "ENTRY_READY"),
            (True, 1_750_000.0, "ENTRY_READY"),
        )
        for member, volume, expected in cases:
            with self.subTest(member=member, volume=volume):
                signal = with_policy(make_signal(), member=member)
                client = PreflightClient(price=100.0, quote_volume_24h=volume)
                payload = build_preflight_payload(
                    signal,
                    client.get_ticker(signal.inst_id),
                    client.get_execution_context(signal.inst_id),
                    AppConfig(),
                    report_generated_at=datetime.now(timezone.utc).isoformat(),
                )
                self.assertEqual(payload["verdict"]["status"], expected)
                policy = payload["execution"]["liquidity_policy"]
                self.assertEqual(policy["member"], member)
                self.assertEqual(
                    policy["effective_min_usdt"],
                    1_500_000.0 if member else 2_000_000.0,
                )
                self.assertEqual(payload["live"]["quote_volume_24h_usdt"], volume)
                if expected == "ENTRY_READY":
                    self.assertNotIn(
                        "LIQUIDITY_TOO_LOW", payload["verdict"]["hard_blockers"]
                    )
                else:
                    self.assertIn(
                        "LIQUIDITY_TOO_LOW", payload["verdict"]["hard_blockers"]
                    )

    def test_missing_or_untrusted_live_quote_volume_fails_closed(self):
        signal = make_signal()
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
        for volume, expected_code in (
            (None, "QUOTE_VOLUME_DATA_UNAVAILABLE"),
            (-1.0, "QUOTE_VOLUME_DATA_UNAVAILABLE"),
            (1_750_000.0, "LIQUIDITY_TOO_LOW"),
        ):
            with self.subTest(volume=volume):
                client = PreflightClient(price=100.0, quote_volume_24h=volume)
                payload = build_preflight_payload(
                    signal,
                    client.get_ticker(signal.inst_id),
                    client.get_execution_context(signal.inst_id),
                    AppConfig(),
                    report_generated_at=datetime.now(timezone.utc).isoformat(),
                )
                self.assertFalse(payload["verdict"]["actionable"])
                self.assertIn(expected_code, payload["verdict"]["hard_blockers"])
                self.assertFalse(payload["execution"]["liquidity_policy"]["member"])
        self.assertEqual(payload["execution"]["liquidity_policy"]["source"], "PREFLIGHT_TICKER")

    def test_preflight_entry_geometry_uses_executable_side_not_last_price(self):
        long_signal = make_signal()
        long_client = PreflightClient(price=100.0)
        long_payload = build_preflight_payload(
            long_signal,
            long_client.get_ticker(long_signal.inst_id),
            long_client.get_execution_context(long_signal.inst_id),
            AppConfig(),
            report_generated_at=datetime.now(timezone.utc).isoformat(),
        )
        self.assertEqual(long_payload["live"]["price"], 100.01)
        self.assertEqual(long_payload["live"]["price_source"], "BEST_ASK")
        self.assertEqual(long_payload["live"]["ticker_last_price"], 100.0)

        short_signal = make_new_short_signal()
        short_client = PreflightClient(price=98.0)
        short_payload = build_preflight_payload(
            short_signal,
            short_client.get_ticker(short_signal.inst_id),
            short_client.get_execution_context(short_signal.inst_id),
            AppConfig(),
            report_generated_at=datetime.now(timezone.utc).isoformat(),
        )
        self.assertEqual(short_payload["live"]["price"], 97.99)
        self.assertEqual(short_payload["live"]["price_source"], "BEST_BID")
        self.assertEqual(short_payload["live"]["ticker_last_price"], 98.0)

    def test_tick_size_precision_handles_okx_increment_shapes(self):
        self.assertEqual(display_precision_from_tick_size(1), 0)
        self.assertEqual(display_precision_from_tick_size(0.1), 1)
        self.assertEqual(display_precision_from_tick_size(0.05), 2)
        self.assertEqual(display_precision_from_tick_size(0.00001), 5)
        self.assertIsNone(display_precision_from_tick_size(0))
        self.assertIsNone(display_precision_from_tick_size("bad"))
        self.assertIsNone(display_precision_from_tick_size(10**10000))

    def test_execution_cost_warning_band_only_allows_values_below_hard_limit(self):
        signal = make_signal()
        client = PreflightClient(price=100.0)
        ticker = client.get_ticker(signal.inst_id)
        base_context = client.get_execution_context(signal.inst_id)
        config = AppConfig(max_execution_cost_to_risk_pct=15.0)

        warning = build_preflight_payload(
            signal,
            ticker,
            replace(
                base_context,
                buy_slippage_pct=0.08,
                sell_slippage_pct=0.08,
            ),
            config,
            report_generated_at=datetime.now(timezone.utc).isoformat(),
        )

        self.assertGreater(
            warning["execution"]["execution_cost_to_risk_pct"],
            10.0,
        )
        self.assertLessEqual(
            warning["execution"]["execution_cost_to_risk_pct"],
            15.0,
        )
        self.assertEqual(warning["verdict"]["status"], "ENTRY_READY")
        self.assertTrue(warning["verdict"]["actionable"])
        self.assertEqual(warning["verdict"]["hard_blockers"], [])
        self.assertTrue(any("偏高" in value for value in warning["warnings"]))

        blocked = build_preflight_payload(
            signal,
            ticker,
            replace(
                base_context,
                buy_slippage_pct=0.11,
                sell_slippage_pct=0.11,
            ),
            config,
            report_generated_at=datetime.now(timezone.utc).isoformat(),
        )

        self.assertGreater(
            blocked["execution"]["execution_cost_to_risk_pct"],
            15.0,
        )
        self.assertEqual(blocked["verdict"]["status"], "HARD_GATE_BLOCKED")
        self.assertFalse(blocked["verdict"]["actionable"])
        self.assertIn(
            "EXECUTION_COST_TOO_HIGH",
            blocked["verdict"]["risk_warnings"],
        )
        self.assertIn(
            "EXECUTION_COST_TOO_HIGH",
            blocked["verdict"]["hard_blockers"],
        )
        self.assertFalse(blocked["plan_state"]["new_entry_allowed"])
        self.assertTrue(blocked["plan_state"]["existing_position_plan_active"])
        self.assertEqual(blocked["plan_state"]["new_entry_status"], "WAIT")
        self.assertTrue(blocked["safety"]["entry_veto_enabled"])

    def test_preflight_uses_raw_cost_and_rr_at_hard_boundaries(self):
        signal = make_signal()
        # LONG entry geometry uses the executable ask; choose a ticker last
        # one cent lower so the ask under test is exactly 100.0.
        client = PreflightClient(price=99.99)
        ticker = client.get_ticker(signal.inst_id)
        context = client.get_execution_context(signal.inst_id)
        config = AppConfig(max_execution_cost_to_risk_pct=15.0, minimum_rr=1.8)

        # Spread 0.02% + 0.0901% slippage each side + 0.10% fees =
        # 0.3002%, or 15.01% of the exact 2% stop distance.  The public
        # quality field rounds this to 15.0, but permission must use 15.01.
        cost = build_preflight_payload(
            signal,
            ticker,
            replace(
                context,
                buy_slippage_pct=0.0901,
                sell_slippage_pct=0.0901,
            ),
            config,
            report_generated_at=datetime.now(timezone.utc).isoformat(),
        )
        self.assertEqual(cost["execution"]["execution_cost_to_risk_pct"], 15.0)
        self.assertEqual(cost["verdict"]["status"], "HARD_GATE_BLOCKED")
        self.assertIn(
            "EXECUTION_COST_TOO_HIGH",
            cost["verdict"]["hard_blockers"],
        )

        # Choose a price whose exact remaining R:R is 1.7999; the displayed
        # field is 1.800, so this also guards against thresholding rounded UI
        # values.
        rr_price = (104.2 + 1.7999 * 98.0) / (1.0 + 1.7999)
        rr_client = PreflightClient(price=rr_price - 0.01)
        rr = build_preflight_payload(
            signal,
            rr_client.get_ticker(signal.inst_id),
            rr_client.get_execution_context(signal.inst_id),
            config,
            report_generated_at=datetime.now(timezone.utc).isoformat(),
        )
        self.assertEqual(rr["live"]["remaining_rr"], 1.8)
        self.assertEqual(rr["verdict"]["status"], "HARD_GATE_BLOCKED")
        self.assertIn("RR_INSUFFICIENT", rr["verdict"]["hard_blockers"])

    def test_missing_book_timestamp_is_advisory_and_does_not_reuse_numeric_cost(self):
        signal = make_signal()
        client = PreflightClient(price=100.0)
        context = replace(
            client.get_execution_context(signal.inst_id),
            source_timestamps={},
            buy_slippage_pct=1.0,
            sell_slippage_pct=1.0,
        )

        payload = build_preflight_payload(
            signal,
            client.get_ticker(signal.inst_id),
            context,
            AppConfig(),
            report_generated_at=datetime.now(timezone.utc).isoformat(),
        )

        self.assertEqual(payload["verdict"]["status"], "ENTRY_READY")
        self.assertEqual(payload["verdict"]["hard_blockers"], [])
        self.assertIn(
            "EXECUTION_ESTIMATE_UNAVAILABLE",
            payload["verdict"]["risk_warnings"],
        )
        self.assertTrue(payload["verdict"]["actionable"])
        self.assertTrue(payload["plan_state"]["new_entry_allowed"])
        self.assertIsNone(payload["execution"]["estimated_round_trip_cost_pct"])
        self.assertIsNone(payload["execution"]["execution_cost_to_risk_pct"])
        self.assertIsNone(payload["execution"]["buy_slippage_pct"])
        self.assertIsNone(payload["execution"]["sell_slippage_pct"])
        self.assertNotIn("估算滑價偏高", payload["warnings"])
        self.assertNotIn("交易成本占原始風險超過建議上限", payload["warnings"])
        self.assertEqual(
            payload["data_quality"]["required_missing_sources"],
            [],
        )
        self.assertEqual(
            payload["data_quality"]["optional_missing_sources"],
            ["order_book_depth"],
        )

    def test_fresh_known_high_slippage_still_blocks_partial_book(self):
        signal = make_signal()
        client = PreflightClient(price=100.0)
        context = replace(
            client.get_execution_context(signal.inst_id),
            buy_slippage_pct=0.20,
            sell_slippage_pct=None,
        )

        payload = build_preflight_payload(
            signal,
            client.get_ticker(signal.inst_id),
            context,
            AppConfig(),
            report_generated_at=datetime.now(timezone.utc).isoformat(),
        )

        self.assertEqual(payload["verdict"]["status"], "HARD_GATE_BLOCKED")
        self.assertIn("SLIPPAGE_TOO_HIGH", payload["verdict"]["hard_blockers"])
        self.assertFalse(payload["verdict"]["actionable"])
        self.assertFalse(payload["plan_state"]["new_entry_allowed"])

    def test_existing_episode_needs_closed_retest_before_live_price_can_reopen_it(self):
        signal = make_signal()
        signal.entry_eligibility = {
            **signal.entry_eligibility,
            "status": "WAIT_RETEST",
            "actionable": False,
            "new_entry_allowed": False,
            "existing_episode": True,
            "entry_ready_once": True,
            "closed_retest_confirmed": False,
        }
        signal.lifecycle = {**signal.lifecycle, "entry_ready_once": True}
        signal.decision_context = {
            "hard_gate": {
                "status": "BLOCKED",
                "blocked": True,
                "blockers": ["entry_permission"],
            },
            "final": {
                "status": "HARD_GATE_BLOCKED",
                "new_entry_allowed": False,
            },
        }
        client = PreflightClient(price=100.0)

        waiting = build_preflight_payload(
            signal,
            client.get_ticker(signal.inst_id),
            client.get_execution_context(signal.inst_id),
            AppConfig(),
            report_generated_at=datetime.now(timezone.utc).isoformat(),
        )

        self.assertEqual(waiting["verdict"]["status"], "WAIT_RETEST")
        self.assertFalse(waiting["verdict"]["actionable"])
        self.assertNotIn(
            "ENTRY_PERMISSION",
            waiting["verdict"]["hard_blockers"],
        )
        self.assertTrue(waiting["live"]["reentry_confirmation_required"])
        self.assertFalse(waiting["live"]["closed_retest_confirmed"])
        self.assertFalse(
            waiting["plan_state"]["old_plan_reusable_for_new_entry"]
        )
        self.assertTrue(waiting["plan_state"]["existing_position_plan_active"])

        signal.entry_eligibility["closed_retest_confirmed"] = True
        confirmed = build_preflight_payload(
            signal,
            client.get_ticker(signal.inst_id),
            client.get_execution_context(signal.inst_id),
            AppConfig(),
            report_generated_at=datetime.now(timezone.utc).isoformat(),
        )
        self.assertEqual(confirmed["verdict"]["status"], "ENTRY_READY")
        self.assertTrue(confirmed["verdict"]["actionable"])

    def test_stored_hard_gate_cannot_be_cleared_by_ticker_only_preflight(self):
        for gate, expected in (
            (
                {
                    "status": "BLOCKED",
                    "blocked": False,
                    "blockers": ["anomaly"],
                },
                "HARD_GATE_BLOCKED",
            ),
            (
                {
                    "status": "UNKNOWN",
                    "unknown": False,
                    "unknowns": ["data_quality"],
                },
                "DATA_UNAVAILABLE",
            ),
        ):
            with self.subTest(gate=gate["status"]):
                signal = make_signal()
                signal.entry_eligibility["closed_retest_confirmed"] = True
                signal.decision_context = {
                    "hard_gate": gate,
                    "final": {
                        "status": "ENTER",
                        "new_entry_allowed": False,
                    },
                }
                client = PreflightClient(price=100.0)

                payload = build_preflight_payload(
                    signal,
                    client.get_ticker(signal.inst_id),
                    client.get_execution_context(signal.inst_id),
                    AppConfig(),
                    report_generated_at=datetime.now(timezone.utc).isoformat(),
                )

                self.assertEqual(payload["verdict"]["status"], expected)
                self.assertFalse(payload["verdict"]["actionable"])
                self.assertFalse(payload["plan_state"]["new_entry_allowed"])
                self.assertTrue(payload["plan_state"]["existing_position_plan_active"])

    def test_live_preflight_rechecks_dynamic_stored_gate_without_stale_fallback(self):
        signal = make_signal()
        signal.entry_eligibility = {
            **signal.entry_eligibility,
            "actionable": False,
            "new_entry_allowed": False,
        }
        signal.lifecycle = {
            **signal.lifecycle,
            "status": "ACTIVE",
            "transition": "NEW",
            "first_seen_at": datetime.now(timezone.utc).isoformat(),
        }
        signal.decision_context = {
            "hard_gate": {
                "status": "BLOCKED",
                "blocked": True,
                "blockers": ["liquidity", "spread"],
            },
            "final": {
                "status": "HARD_GATE_BLOCKED",
                "new_entry_allowed": False,
            },
        }
        client = PreflightClient(price=100.0, quote_volume_24h=20_000_000.0)

        payload = build_preflight_payload(
            signal,
            client.get_ticker(signal.inst_id),
            client.get_execution_context(signal.inst_id),
            AppConfig(),
            report_generated_at=datetime.now(timezone.utc).isoformat(),
        )

        self.assertEqual(payload["verdict"]["status"], "ENTRY_READY")
        self.assertTrue(payload["verdict"]["actionable"])
        self.assertNotIn(
            "UPSTREAM_HARD_GATE_BLOCKED",
            payload["verdict"]["hard_blockers"],
        )

    def test_formal_opposite_signal_keeps_plan_but_blocks_new_entry(self):
        signal = make_signal()
        signal.market_story = {
            **signal.market_story,
            "trigger": {
                **signal.market_story["trigger"],
                "opposite_warning_only": True,
                "new_entry_suspended": True,
                "opposite_candidate": {
                    "direction": "SHORT",
                    "type": "REVERSAL",
                },
            },
        }
        client = PreflightClient(price=100.0)

        payload = build_preflight_payload(
            signal,
            client.get_ticker(signal.inst_id),
            client.get_execution_context(signal.inst_id),
            AppConfig(),
            report_generated_at=datetime.now(timezone.utc).isoformat(),
        )

        self.assertEqual(payload["verdict"]["status"], "HARD_GATE_BLOCKED")
        self.assertFalse(payload["verdict"]["actionable"])
        self.assertIn("OPPOSITE_SIGNAL", payload["verdict"]["hard_blockers"])
        self.assertIn("正式反向", payload["verdict"]["reason"])
        self.assertFalse(payload["plan_state"]["new_entry_allowed"])
        self.assertTrue(payload["plan_state"]["existing_position_plan_active"])
        self.assertTrue(payload["safety"]["entry_veto_enabled"])

    def test_either_opposite_flag_remains_binding_while_price_already_waits(self):
        for flag in ("new_entry_suspended", "opposite_warning_only"):
            with self.subTest(flag=flag):
                signal = make_signal()
                signal.market_story = {
                    **signal.market_story,
                    "trigger": {
                        **signal.market_story["trigger"],
                        flag: True,
                    },
                }
                client = PreflightClient(price=99.4)

                payload = build_preflight_payload(
                    signal,
                    client.get_ticker(signal.inst_id),
                    client.get_execution_context(signal.inst_id),
                    AppConfig(),
                    report_generated_at=datetime.now(timezone.utc).isoformat(),
                )

                self.assertEqual(payload["verdict"]["status"], "WAIT_RETEST")
                self.assertIn("OPPOSITE_SIGNAL", payload["verdict"]["hard_blockers"])
                self.assertFalse(
                    payload["plan_state"]["old_plan_reusable_for_new_entry"]
                )
                self.assertFalse(payload["plan_state"]["new_entry_allowed"])
                self.assertTrue(
                    payload["plan_state"]["existing_position_plan_active"]
                )

    def test_terminal_price_state_outranks_opposite_veto_for_both_directions(self):
        long_signal = make_signal()
        short_signal = replace(
            make_signal(),
            direction="SHORT",
            stop_loss="102",
            take_profit_1="96",
            take_profit_2="94",
        )
        cases = (
            (long_signal, 97.9, "PLAN_INVALIDATED", "INVALIDATED"),
            (long_signal, 104.3, "MISSED_ENTRY", "TARGET_REACHED"),
            (short_signal, 102.1, "PLAN_INVALIDATED", "INVALIDATED"),
            (short_signal, 95.9, "MISSED_ENTRY", "TARGET_REACHED"),
        )
        for signal, price, verdict_status, lifecycle_status in cases:
            with self.subTest(direction=signal.direction, price=price):
                signal.market_story = {
                    **signal.market_story,
                    "trigger": {
                        **signal.market_story["trigger"],
                        "new_entry_suspended": True,
                    },
                }
                client = PreflightClient(price=price)

                payload = build_preflight_payload(
                    signal,
                    client.get_ticker(signal.inst_id),
                    client.get_execution_context(signal.inst_id),
                    AppConfig(),
                    report_generated_at=datetime.now(timezone.utc).isoformat(),
                )

                self.assertEqual(payload["verdict"]["status"], verdict_status)
                self.assertEqual(
                    payload["signal_lifecycle"]["status"],
                    lifecycle_status,
                )
                self.assertIn("OPPOSITE_SIGNAL", payload["verdict"]["hard_blockers"])
                self.assertFalse(payload["verdict"]["actionable"])
                self.assertFalse(payload["plan_state"]["new_entry_allowed"])
                self.assertFalse(
                    payload["plan_state"]["existing_position_plan_active"]
                )

    def test_refreshes_one_signal_and_keeps_stored_trigger_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            item = make_signal()
            original_metrics = dict(item.market_metrics)
            original_quality = dict(item.execution_quality)
            client = PreflightClient()
            runtime = RadarRuntime(
                PreflightScanner(client),
                AppConfig(data_dir=directory),
            )
            runtime._latest = make_report(item)

            payload = runtime.preflight_dict(item.inst_id, "SHORT")

            self.assertEqual(payload["verdict"]["status"], "ENTRY_READY")
            self.assertTrue(payload["verdict"]["actionable"])
            self.assertEqual(payload["live"]["price"], 100.11)
            self.assertEqual(payload["original"]["quality_score"], 87.0)
            self.assertTrue(payload["data_quality"]["execution_depth_complete"])
            self.assertTrue(payload["safety"]["stored_trigger_unchanged"])
            self.assertEqual(payload["plan_state"]["status"], "ACTIVE")
            self.assertTrue(payload["plan_state"]["old_plan_reusable"])
            self.assertFalse(payload["plan_state"]["new_trigger_required"])
            self.assertEqual(payload["signal_lifecycle"]["status"], "ACTIVE")
            self.assertEqual(payload["signal_lifecycle"]["label"], "已觸發・有效中")
            self.assertTrue(payload["plan_state"]["existing_position_plan_active"])
            self.assertEqual(payload["plan_state"]["new_entry_status"], "READY")
            self.assertEqual(item.market_metrics, original_metrics)
            self.assertEqual(item.execution_quality, original_quality)

    def test_preflight_refresh_adds_directional_continuation_without_mutating_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            item = make_signal()
            original_flow = make_capital_flow(1_699_996_400_000)
            item.decision_context = {
                "continuation_confirmation": {
                    "key": "FORMING",
                    "core_votes": {"OI": {"state": "SUPPORT"}},
                    "observer": {
                        "status": "READY",
                        "primary_window": "10m",
                        "as_of_close_ms": 1_699_996_400_000,
                        "windows": {"10m": {"ready": True}},
                        "capital_flow": original_flow,
                    },
                }
            }
            original_plan = (
                item.direction,
                item.entry_low,
                item.entry_high,
                item.stop_loss,
                item.take_profit_1,
                item.take_profit_2,
            )
            runtime = RadarRuntime(
                ContinuationPreflightScanner(PreflightClient()),
                AppConfig(data_dir=directory),
            )
            runtime._latest = make_report(item)

            payload = runtime.preflight_dict(item.inst_id, "SHORT")

            self.assertEqual(payload["continuation"]["direction_label"], "多頭")
            self.assertEqual(payload["continuation"]["current"]["label"], "強")
            self.assertEqual(payload["continuation"]["current"]["primary_window"], "10m")
            self.assertNotIn("score", payload["continuation"]["current"])
            current_flow = payload["continuation"]["current"]["capital_flow"]
            self.assertEqual(current_flow["as_of_close_ms"], 1_700_000_000_000)
            self.assertEqual(set(current_flow["windows"]), {"1h", "2h", "4h"})
            self.assertEqual(
                current_flow["windows"]["1h"]["change_vs_average_ratio"],
                2.5,
            )
            self.assertNotIn("samples", current_flow["windows"]["1h"])
            self.assertNotIn("raw_points", current_flow)
            self.assertNotIn("8h", current_flow["windows"])
            self.assertEqual(
                payload["continuation"]["original"]["capital_flow"][
                    "as_of_close_ms"
                ],
                1_699_996_400_000,
            )
            self.assertTrue(payload["safety"]["stored_trigger_unchanged"])
            self.assertEqual(
                (
                    item.direction,
                    item.entry_low,
                    item.entry_high,
                    item.stop_loss,
                    item.take_profit_1,
                    item.take_profit_2,
                ),
                original_plan,
            )

    def test_failed_preflight_history_never_reuses_scan_time_capital_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            item = make_signal()
            item.decision_context = {
                "continuation_confirmation": {
                    "key": "CONFIRMED",
                    "observer": {
                        "status": "READY",
                        "primary_window": "10m",
                        "windows": {"10m": {"ready": True}},
                        "capital_flow": make_capital_flow(),
                    },
                }
            }
            runtime = RadarRuntime(
                FailingContinuationPreflightScanner(PreflightClient()),
                AppConfig(data_dir=directory),
            )
            runtime._latest = make_report(item)

            payload = runtime.preflight_dict(item.inst_id, "SHORT")

            self.assertTrue(payload["continuation"]["refresh_failed"])
            self.assertTrue(
                payload["continuation"]["original"]["capital_flow"]["detected"]
            )
            self.assertEqual(
                payload["continuation"]["current"]["capital_flow"],
                {},
            )

    def test_wrong_expected_trigger_is_structured_conflict_before_live_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            item = make_signal()
            client = PreflightClient()
            runtime = RadarRuntime(
                PreflightScanner(client),
                AppConfig(data_dir=directory),
            )
            runtime._latest = make_report(item)

            with self.assertRaises(PreflightError) as caught:
                runtime.preflight_dict(
                    item.inst_id,
                    "SHORT",
                    "stale-card-trigger",
                )

            self.assertEqual(caught.exception.status.value, 409)
            self.assertEqual(caught.exception.code, "TRIGGER_MISMATCH")
            self.assertEqual(
                caught.exception.response_payload(),
                {
                    "error": "這張卡的 Trigger 已不是目前有效版本，請重新載入訊號頁",
                    "code": "TRIGGER_MISMATCH",
                    "inst_id": item.inst_id,
                    "horizon": "SHORT",
                    "expected_trigger_id": "stale-card-trigger",
                    "current_trigger_id": item.trigger_id,
                    "refresh_required": True,
                },
            )
            self.assertEqual(client.ticker_calls, 0)
            self.assertEqual(client.context_calls, 0)

    def test_data_unavailable_card_cannot_be_reopened_by_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            original = make_signal()
            item = replace(
                original,
                freshness="DATA_UNAVAILABLE",
                actionable=False,
                entry_eligibility={
                    **original.entry_eligibility,
                    "status": "DATA_UNAVAILABLE",
                    "actionable": False,
                    "new_entry_allowed": False,
                },
                data_quality={"status": "DATA_UNAVAILABLE"},
                lifecycle={
                    **original.lifecycle,
                    "transition": "DATA_UNAVAILABLE",
                    "read_only": True,
                },
            )
            client = PreflightClient(price=100.0)
            runtime = RadarRuntime(
                PreflightScanner(client),
                AppConfig(data_dir=directory),
            )
            runtime._latest = make_report(item)
            runtime._latest.status = "NO_QUALIFIED_SIGNAL"

            with self.assertRaises(PreflightError) as caught:
                runtime.preflight_dict(item.inst_id, "SHORT", item.trigger_id)

            self.assertEqual(caught.exception.status.value, 409)
            self.assertEqual(
                caught.exception.code,
                "SIGNAL_DATA_UNAVAILABLE",
            )
            self.assertEqual(caught.exception.details["trigger_id"], item.trigger_id)
            self.assertEqual(client.ticker_calls, 0)
            self.assertEqual(client.context_calls, 0)

    def test_trigger_replacement_during_fetch_is_rejected_before_cache(self):
        for expected_trigger_id in ("old-trigger-id", None):
            with self.subTest(expected_trigger_id=expected_trigger_id):
                with tempfile.TemporaryDirectory() as directory:
                    item = make_signal()
                    client = MutatingPreflightClient()
                    runtime = RadarRuntime(
                        PreflightScanner(client),
                        AppConfig(data_dir=directory),
                    )
                    runtime._latest = make_report(item)

                    def replace_trigger() -> None:
                        replacement = replace(
                            item,
                            trigger_id="replacement-trigger-id",
                        )
                        runtime._latest.signals = [replacement]

                    client.after_context = replace_trigger

                    with self.assertRaises(PreflightError) as caught:
                        runtime.preflight_dict(
                            item.inst_id,
                            "SHORT",
                            expected_trigger_id,
                        )

                    self.assertEqual(caught.exception.status.value, 409)
                    self.assertEqual(caught.exception.code, "TRIGGER_MISMATCH")
                    self.assertEqual(
                        caught.exception.details["current_trigger_id"],
                        "replacement-trigger-id",
                    )
                    self.assertEqual(runtime._preflight_cache, {})

    def test_same_horizon_scan_start_before_cache_commit_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            item = make_signal()
            runtime = RadarRuntime(
                PreflightScanner(PreflightClient()),
                AppConfig(data_dir=directory),
            )
            runtime._latest = make_report(item)

            def start_scan(**kwargs):
                runtime._running = True
                runtime._scan_mode = "SHORT"
                runtime._last_attempt_status = "SCANNING"
                return None

            with patch.object(
                runtime,
                "_persist_preflight_terminal",
                side_effect=start_scan,
            ):
                with self.assertRaises(PreflightError) as caught:
                    runtime.preflight_dict(
                        item.inst_id,
                        "SHORT",
                        item.trigger_id,
                    )

            self.assertEqual(caught.exception.code, "HORIZON_SCAN_RUNNING")
            self.assertEqual(runtime._preflight_cache, {})

    def test_trigger_replacement_before_cache_commit_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            item = make_signal()
            runtime = RadarRuntime(
                PreflightScanner(PreflightClient()),
                AppConfig(data_dir=directory),
            )
            runtime._latest = make_report(item)

            def replace_trigger(**kwargs):
                runtime._latest.signals = [
                    replace(item, trigger_id="replacement-trigger-id")
                ]
                return None

            with patch.object(
                runtime,
                "_persist_preflight_terminal",
                side_effect=replace_trigger,
            ):
                with self.assertRaises(PreflightError) as caught:
                    runtime.preflight_dict(
                        item.inst_id,
                        "SHORT",
                        item.trigger_id,
                    )

            self.assertEqual(caught.exception.code, "TRIGGER_MISMATCH")
            self.assertEqual(
                caught.exception.details["current_trigger_id"],
                "replacement-trigger-id",
            )
            self.assertEqual(runtime._preflight_cache, {})

    def test_stale_cached_episode_is_not_returned_for_replacement_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            item = make_signal()
            client = PreflightClient()
            runtime = RadarRuntime(
                PreflightScanner(client),
                AppConfig(data_dir=directory),
            )
            runtime._latest = make_report(item)

            first = runtime.preflight_dict(
                item.inst_id,
                "SHORT",
                item.trigger_id,
            )
            replacement = replace(item, trigger_id="replacement-trigger-id")
            runtime._latest.signals = [replacement]
            second = runtime.preflight_dict(
                item.inst_id,
                "SHORT",
                replacement.trigger_id,
            )

            self.assertEqual(first["trigger_id"], item.trigger_id)
            self.assertEqual(second["trigger_id"], replacement.trigger_id)
            self.assertFalse(second["cached"])
            self.assertEqual(client.ticker_calls, 2)
            self.assertEqual(client.context_calls, 2)

    def test_partial_scan_only_blocks_preflight_for_its_own_horizon(self):
        for blocked_horizon, allowed_horizon in (
            ("SHORT", "LONG"),
            ("LONG", "SHORT"),
        ):
            with self.subTest(
                blocked_horizon=blocked_horizon,
                allowed_horizon=allowed_horizon,
            ):
                with tempfile.TemporaryDirectory() as directory:
                    short = make_signal()
                    long = replace(
                        make_signal(),
                        radar_horizon="LONG",
                        trigger_id="long-trigger-id",
                    )
                    current = make_report(short)
                    current.long_signals = [long]
                    current.short_completed_at = current.completed_at
                    current.long_completed_at = current.completed_at
                    runtime = RadarRuntime(
                        PreflightScanner(PreflightClient()),
                        AppConfig(data_dir=directory),
                    )
                    runtime._latest = current
                    runtime._running = True
                    runtime._scan_mode = blocked_horizon
                    runtime._last_attempt_status = "SCANNING"

                    allowed_signal = long if allowed_horizon == "LONG" else short
                    allowed = runtime.preflight_dict(
                        allowed_signal.inst_id,
                        allowed_horizon,
                        allowed_signal.trigger_id,
                    )
                    self.assertEqual(allowed["horizon"], allowed_horizon)

                    blocked_signal = short if blocked_horizon == "SHORT" else long
                    with self.assertRaises(PreflightError) as caught:
                        runtime.preflight_dict(
                            blocked_signal.inst_id,
                            blocked_horizon,
                            blocked_signal.trigger_id,
                        )
                    self.assertEqual(caught.exception.status.value, 409)
                    self.assertEqual(
                        caught.exception.code,
                        "HORIZON_SCAN_RUNNING",
                    )

    def test_failed_partial_scan_only_blocks_its_own_horizon(self):
        for blocked_horizon, allowed_horizon in (
            ("SHORT", "LONG"),
            ("LONG", "SHORT"),
        ):
            with self.subTest(
                blocked_horizon=blocked_horizon,
                allowed_horizon=allowed_horizon,
            ):
                with tempfile.TemporaryDirectory() as directory:
                    short = make_signal()
                    long = replace(
                        make_signal(),
                        radar_horizon="LONG",
                        trigger_id="long-trigger-id",
                    )
                    current = make_report(short)
                    current.long_signals = [long]
                    current.short_completed_at = current.completed_at
                    current.long_completed_at = current.completed_at
                    runtime = RadarRuntime(
                        PreflightScanner(PreflightClient()),
                        AppConfig(data_dir=directory),
                    )
                    runtime._latest = current
                    runtime._running = False
                    runtime._scan_mode = blocked_horizon
                    runtime._mark_horizon_attempts_locked(
                        blocked_horizon,
                        "ERROR",
                        f"{blocked_horizon} fixture failure",
                    )

                    allowed_signal = long if allowed_horizon == "LONG" else short
                    allowed = runtime.preflight_dict(
                        allowed_signal.inst_id,
                        allowed_horizon,
                        allowed_signal.trigger_id,
                    )
                    self.assertEqual(allowed["horizon"], allowed_horizon)

                    blocked_signal = short if blocked_horizon == "SHORT" else long
                    with self.assertRaises(PreflightError) as caught:
                        runtime.preflight_dict(
                            blocked_signal.inst_id,
                            blocked_horizon,
                            blocked_signal.trigger_id,
                        )
                    self.assertEqual(caught.exception.status.value, 409)
                    self.assertEqual(
                        caught.exception.code,
                        "HORIZON_SCAN_FAILED",
                    )

    def test_success_on_other_horizon_does_not_clear_prior_failure(self):
        for failed_horizon, successful_horizon in (
            ("LONG", "SHORT"),
            ("SHORT", "LONG"),
        ):
            with self.subTest(
                failed_horizon=failed_horizon,
                successful_horizon=successful_horizon,
            ):
                with tempfile.TemporaryDirectory() as directory:
                    short = make_signal()
                    long = replace(
                        make_signal(),
                        radar_horizon="LONG",
                        trigger_id="long-trigger-id",
                    )
                    current = make_report(short)
                    current.long_signals = [long]
                    current.short_completed_at = current.completed_at
                    current.long_completed_at = current.completed_at
                    runtime = RadarRuntime(
                        PreflightScanner(PreflightClient()),
                        AppConfig(data_dir=directory),
                    )
                    runtime._latest = current

                    runtime._mark_horizon_attempts_locked(
                        failed_horizon,
                        "ERROR",
                        f"{failed_horizon} fixture failure",
                    )
                    runtime._mark_horizon_attempts_locked(
                        successful_horizon,
                        "SUCCESS",
                    )

                    payload = runtime.latest_dict()
                    self.assertEqual(
                        payload["horizon_attempt_status"][failed_horizon],
                        "ERROR",
                    )
                    self.assertEqual(
                        payload["horizon_attempt_status"][successful_horizon],
                        "SUCCESS",
                    )
                    self.assertEqual(
                        payload["scan_unavailable_horizons"],
                        [failed_horizon],
                    )
                    self.assertFalse(
                        payload["safety"]["horizon_actionable"][failed_horizon]
                    )
                    self.assertTrue(
                        payload["safety"]["horizon_actionable"][
                            successful_horizon
                        ]
                    )

                    successful_signal = (
                        short if successful_horizon == "SHORT" else long
                    )
                    allowed = runtime.preflight_dict(
                        successful_signal.inst_id,
                        successful_horizon,
                        successful_signal.trigger_id,
                    )
                    self.assertEqual(allowed["horizon"], successful_horizon)

                    failed_signal = short if failed_horizon == "SHORT" else long
                    with self.assertRaises(PreflightError) as caught:
                        runtime.preflight_dict(
                            failed_signal.inst_id,
                            failed_horizon,
                            failed_signal.trigger_id,
                        )
                    self.assertEqual(
                        caught.exception.code,
                        "HORIZON_SCAN_FAILED",
                    )

    def test_full_scan_blocks_both_preflight_horizons(self):
        with tempfile.TemporaryDirectory() as directory:
            short = make_signal()
            long = replace(
                make_signal(),
                radar_horizon="LONG",
                trigger_id="long-trigger-id",
            )
            current = make_report(short)
            current.long_signals = [long]
            current.short_completed_at = current.completed_at
            current.long_completed_at = current.completed_at
            runtime = RadarRuntime(
                PreflightScanner(PreflightClient()),
                AppConfig(data_dir=directory),
            )
            runtime._latest = current
            runtime._running = True
            runtime._scan_mode = "FULL"
            runtime._last_attempt_status = "SCANNING"

            for horizon, item in (("SHORT", short), ("LONG", long)):
                with self.subTest(horizon=horizon):
                    with self.assertRaises(PreflightError) as caught:
                        runtime.preflight_dict(
                            item.inst_id,
                            horizon,
                            item.trigger_id,
                        )
                    self.assertEqual(
                        caught.exception.code,
                        "HORIZON_SCAN_RUNNING",
                    )

            runtime._running = False
            runtime._mark_horizon_attempts_locked(
                "FULL",
                "ERROR",
                "full fixture failure",
            )
            for horizon, item in (("SHORT", short), ("LONG", long)):
                with self.subTest(failed_horizon=horizon):
                    with self.assertRaises(PreflightError) as caught:
                        runtime.preflight_dict(
                            item.inst_id,
                            horizon,
                            item.trigger_id,
                        )
                    self.assertEqual(
                        caught.exception.code,
                        "HORIZON_SCAN_FAILED",
                    )

    def test_data_incomplete_snapshot_remains_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            item = make_signal()
            client = PreflightClient()
            current = make_report(item)
            current.status = "DATA_INCOMPLETE"
            runtime = RadarRuntime(
                PreflightScanner(client),
                AppConfig(data_dir=directory),
            )
            runtime._latest = current

            with self.assertRaises(PreflightError) as caught:
                runtime.preflight_dict(
                    item.inst_id,
                    "SHORT",
                    item.trigger_id,
                )

            self.assertEqual(caught.exception.status.value, 409)
            self.assertEqual(caught.exception.code, "DATA_INCOMPLETE")
            self.assertEqual(client.ticker_calls, 0)
            self.assertEqual(client.context_calls, 0)

    def test_preflight_stays_separate_when_full_coin_scan_is_available(self):
        with tempfile.TemporaryDirectory() as directory:
            item = make_signal()
            client = PreflightClient()
            scanner = FullCapablePreflightScanner(client)
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))
            runtime._latest = make_report(item)

            payload = runtime.preflight_dict(item.inst_id, "SHORT")

            self.assertEqual(payload["verdict"]["status"], "ENTRY_READY")
            self.assertEqual(scanner.single_scan_calls, 0)
            self.assertEqual(client.ticker_calls, 1)
            self.assertEqual(client.context_calls, 1)

    def test_twelve_second_cache_prevents_duplicate_okx_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            item = make_signal()
            client = PreflightClient()
            runtime = RadarRuntime(
                PreflightScanner(client),
                AppConfig(data_dir=directory),
            )
            runtime._latest = make_report(item)

            first = runtime.preflight_dict(item.inst_id, "15m")
            second = runtime.preflight_dict(item.inst_id, "SHORT")

            self.assertFalse(first["cached"])
            self.assertTrue(second["cached"])
            self.assertEqual(client.ticker_calls, 1)
            self.assertEqual(client.context_calls, 1)

    def test_crossing_stop_returns_invalidated_without_deleting_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            item = make_signal()
            runtime = RadarRuntime(
                PreflightScanner(PreflightClient(price=97.5)),
                AppConfig(data_dir=directory),
            )
            runtime._latest = make_report(item)

            payload = runtime.preflight_dict(item.inst_id, "SHORT")

            self.assertEqual(payload["verdict"]["status"], "PLAN_INVALIDATED")
            self.assertIn("原交易計畫失效", payload["verdict"]["label"])
            self.assertIn("不等於", payload["verdict"]["reason"])
            self.assertFalse(payload["verdict"]["actionable"])
            self.assertEqual(payload["live"]["quality_score"], 0.0)
            self.assertEqual(payload["plan_state"]["status"], "INVALIDATED")
            self.assertEqual(payload["signal_lifecycle"]["status"], "INVALIDATED")
            self.assertEqual(payload["signal_lifecycle"]["label"], "已觸發・已失效")
            self.assertFalse(payload["plan_state"]["existing_position_plan_active"])
            self.assertFalse(payload["plan_state"]["old_plan_reusable"])
            self.assertEqual(
                payload["plan_state"]["direction_status"],
                "PENDING_REASSESSMENT",
            )
            self.assertTrue(payload["plan_state"]["new_trigger_required"])
            self.assertIn("新的 Trigger／REENTRY", payload["plan_state"]["note"])
            self.assertEqual(len(runtime._latest.signals), 1)

    def test_adverse_side_hides_artificial_live_rr_without_mutating_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            item = make_signal()
            original_entry = dict(item.entry_eligibility)
            runtime = RadarRuntime(
                PreflightScanner(PreflightClient(price=98.1)),
                AppConfig(data_dir=directory),
            )
            runtime._latest = make_report(item)

            payload = runtime.preflight_dict(item.inst_id, "SHORT")

            # The original episode remains active and the positional WAIT keeps
            # priority.  The execution blocker is retained so no downstream
            # consumer can later treat the old plan as reusable for entry.
            self.assertEqual(payload["verdict"]["status"], "WAIT_RETEST")
            self.assertFalse(payload["verdict"]["actionable"])
            self.assertIn("接近失效", payload["verdict"]["label"])
            self.assertEqual(payload["verdict"]["situation"], "NEAR_INVALIDATION")
            self.assertIn(
                "EXECUTION_COST_TOO_HIGH",
                payload["verdict"]["risk_warnings"],
            )
            self.assertIn(
                "EXECUTION_COST_TOO_HIGH",
                payload["verdict"]["hard_blockers"],
            )
            self.assertFalse(payload["plan_state"]["old_plan_reusable_for_new_entry"])
            self.assertEqual(payload["signal_lifecycle"]["status"], "ACTIVE")
            self.assertIsNone(payload["live"]["remaining_rr"])
            self.assertFalse(payload["live"]["remaining_rr_applicable"])
            self.assertEqual(item.entry_eligibility, original_entry)
            self.assertTrue(payload["safety"]["stored_trigger_unchanged"])

    def test_small_adverse_move_keeps_trigger_active_with_retest_tolerance(self):
        with tempfile.TemporaryDirectory() as directory:
            item = make_signal()
            runtime = RadarRuntime(
                PreflightScanner(PreflightClient(price=99.4)),
                AppConfig(data_dir=directory),
            )
            runtime._latest = make_report(item)

            payload = runtime.preflight_dict(item.inst_id, "SHORT")

            self.assertEqual(payload["verdict"]["status"], "WAIT_RETEST")
            self.assertEqual(payload["verdict"]["situation"], "ADVERSE_TOLERANCE")
            self.assertIn("容許回測中", payload["verdict"]["label"])
            self.assertEqual(payload["signal_lifecycle"]["label"], "已觸發・有效中")
            self.assertTrue(payload["plan_state"]["existing_position_plan_active"])
            self.assertEqual(payload["plan_state"]["new_entry_status"], "WAIT")

    def test_favorable_move_shows_active_trigger_and_waits_without_chasing(self):
        with tempfile.TemporaryDirectory() as directory:
            item = replace(
                make_signal(),
                direction="SHORT",
                signal_stage="CONFIRMED",
                entry_low="99.8",
                entry_high="100.2",
                stop_loss="102",
                take_profit_1="92",
                take_profit_2="90",
            )
            runtime = RadarRuntime(
                PreflightScanner(PreflightClient(price=99.06)),
                AppConfig(data_dir=directory),
            )
            runtime._latest = make_report(item)

            payload = runtime.preflight_dict(item.inst_id, "SHORT")

            self.assertEqual(payload["verdict"]["status"], "WAIT_RETEST")
            self.assertEqual(payload["verdict"]["situation"], "FAVORABLE_AWAY")
            self.assertIn("已離開最佳進場點", payload["verdict"]["label"])
            self.assertEqual(payload["signal_lifecycle"]["label"], "已觸發・有效中")
            self.assertTrue(payload["plan_state"]["existing_position_plan_active"])
            self.assertFalse(payload["verdict"]["actionable"])

    def test_favorable_move_beyond_entry_window_closes_only_new_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            item = replace(
                make_signal(),
                direction="SHORT",
                signal_stage="CONFIRMED",
                entry_low="99.8",
                entry_high="100.2",
                stop_loss="102",
                take_profit_1="92",
                take_profit_2="90",
            )
            runtime = RadarRuntime(
                PreflightScanner(PreflightClient(price=98.6)),
                AppConfig(data_dir=directory),
            )
            runtime._latest = make_report(item)

            payload = runtime.preflight_dict(item.inst_id, "SHORT")

            self.assertEqual(payload["verdict"]["status"], "MISSED_ENTRY")
            self.assertEqual(payload["verdict"]["situation"], "FAVORABLE_MISSED")
            self.assertEqual(payload["signal_lifecycle"]["status"], "ACTIVE")
            self.assertTrue(payload["plan_state"]["existing_position_plan_active"])
            self.assertFalse(payload["plan_state"]["old_plan_reusable_for_new_entry"])
            self.assertEqual(
                payload["plan_state"]["direction_status"],
                "ORIGINAL_BIAS_RETAINED",
            )
            self.assertIn("若已持倉", payload["plan_state"]["note"])

    def test_reaching_target_completes_trigger_without_relabeling_it_untriggered(self):
        with tempfile.TemporaryDirectory() as directory:
            item = make_signal()
            runtime = RadarRuntime(
                PreflightScanner(PreflightClient(price=104.3)),
                AppConfig(data_dir=directory),
            )
            runtime._latest = make_report(item)

            payload = runtime.preflight_dict(item.inst_id, "SHORT")

            self.assertEqual(payload["verdict"]["situation"], "TARGET_REACHED")
            self.assertEqual(payload["signal_lifecycle"]["status"], "TARGET_REACHED")
            self.assertEqual(payload["signal_lifecycle"]["label"], "已觸發・目標已達")
            self.assertTrue(payload["signal_lifecycle"]["terminal"])
            self.assertFalse(payload["plan_state"]["existing_position_plan_active"])
            self.assertEqual(payload["plan_state"]["status"], "TARGET_REACHED")
            self.assertFalse(payload["plan_state"]["old_plan_reusable"])
            self.assertFalse(payload["plan_state"]["old_plan_reusable_for_new_entry"])
            self.assertTrue(payload["plan_state"]["new_trigger_required"])

            # A completed episode remains distinct from a stopped-out plan.
            stopped = RadarRuntime(
                PreflightScanner(PreflightClient(price=97.5)),
                AppConfig(data_dir=directory),
            )
            stopped._latest = make_report(item)
            invalidated = stopped.preflight_dict(item.inst_id, "SHORT")
            self.assertEqual(invalidated["verdict"]["situation"], "INVALIDATED")
            self.assertEqual(invalidated["signal_lifecycle"]["status"], "INVALIDATED")
            self.assertEqual(invalidated["plan_state"]["status"], "INVALIDATED")

    def test_second_refresh_after_invalidation_publishes_a_new_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            item = make_signal()
            scanner = ReanalysisPreflightScanner(
                PreflightClient(price=97.5),
                make_new_short_signal(),
            )
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))
            runtime._latest = make_report(item)

            invalidated = runtime.preflight_dict(item.inst_id, "SHORT")
            refreshed = runtime.reanalyze_preflight_dict(item.inst_id, "SHORT")

            self.assertEqual(invalidated["verdict"]["status"], "PLAN_INVALIDATED")
            self.assertEqual(
                refreshed["reanalysis"]["status"],
                "NEW_ENTRY_OPPORTUNITY",
            )
            self.assertEqual(refreshed["direction"], "SHORT")
            self.assertEqual(refreshed["original"]["entry_low"], 97.0)
            self.assertTrue(refreshed["original"]["triggered_at"])
            self.assertEqual(scanner.reanalysis_calls, 1)
            self.assertEqual(scanner.commit_calls, 1)
            self.assertEqual(
                [signal.trigger_id for signal in runtime._latest.signals],
                ["new-trigger-id"],
            )

    def test_second_refresh_without_new_trigger_shows_no_opportunity(self):
        with tempfile.TemporaryDirectory() as directory:
            item = make_signal()
            scanner = ReanalysisPreflightScanner(
                PreflightClient(price=97.5),
                None,
            )
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))
            runtime._latest = make_report(item)

            runtime.preflight_dict(item.inst_id, "SHORT")
            first = runtime.reanalyze_preflight_dict(item.inst_id, "SHORT")
            second = runtime.reanalyze_preflight_dict(item.inst_id, "SHORT")

            self.assertEqual(
                first["reanalysis"]["status"],
                "NO_NEW_ENTRY_OPPORTUNITY",
            )
            self.assertIn("沒有新的正式 Trigger", first["reanalysis"]["message"])
            self.assertEqual(runtime._latest.signals, [])
            self.assertEqual(scanner.reanalysis_calls, 2)
            self.assertEqual(
                second["reanalysis"]["status"],
                "NO_NEW_ENTRY_OPPORTUNITY",
            )

    def test_reanalysis_is_rejected_before_plan_is_confirmed_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            item = make_signal()
            scanner = ReanalysisPreflightScanner(
                PreflightClient(price=100.0),
                make_new_short_signal(),
            )
            runtime = RadarRuntime(scanner, AppConfig(data_dir=directory))
            runtime._latest = make_report(item)

            with self.assertRaises(PreflightError) as caught:
                runtime.reanalyze_preflight_dict(item.inst_id, "SHORT")

            self.assertEqual(caught.exception.status.value, 409)
            self.assertIn("必須先", str(caught.exception))
            self.assertEqual(scanner.reanalysis_calls, 0)

    def test_rejects_symbol_without_formal_trigger_for_requested_horizon(self):
        with tempfile.TemporaryDirectory() as directory:
            item = make_signal()
            runtime = RadarRuntime(
                PreflightScanner(PreflightClient()),
                AppConfig(data_dir=directory),
            )
            runtime._latest = make_report(item)

            with self.assertRaises(PreflightError) as caught:
                runtime.preflight_dict(item.inst_id, "LONG")

            self.assertEqual(caught.exception.status.value, 404)

    def test_long_signal_uses_same_preflight_page_with_four_hour_age(self):
        with tempfile.TemporaryDirectory() as directory:
            item = make_signal()
            item.radar_horizon = "LONG"
            runtime = RadarRuntime(
                PreflightScanner(PreflightClient()),
                AppConfig(data_dir=directory),
            )
            runtime._latest = make_report(item)

            payload = runtime.preflight_dict(item.inst_id, "4H")

            self.assertEqual(payload["horizon"], "LONG")
            self.assertEqual(payload["horizon_label"], "4H 長線")
            self.assertEqual(payload["verdict"]["status"], "ENTRY_READY")


if __name__ == "__main__":
    unittest.main()
