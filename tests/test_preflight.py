import tempfile
import time
import unittest
from datetime import datetime, timezone

from radar.config import AppConfig
from radar.models import MarketContext, RadarReport, Signal, Ticker
from radar.service import PreflightError, RadarRuntime


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
        freshness="NEW",
        market_metrics={"last_price": 100.0},
        market_story={
            "raw": {"core_atr": 2.0},
            "trigger": {
                "event_ts": now_ms - 900_000,
                "event_age_bars": 1,
            },
        },
        lifecycle={"age_bars": 1},
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


class PreflightClient:
    def __init__(self, price: float = 100.1):
        self.price = price
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


class PreflightScanner:
    def __init__(self, client: PreflightClient):
        self.client = client


class PreflightTests(unittest.TestCase):
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
            self.assertEqual(payload["live"]["price"], 100.1)
            self.assertEqual(payload["original"]["quality_score"], 87.0)
            self.assertTrue(payload["data_quality"]["execution_depth_complete"])
            self.assertTrue(payload["safety"]["stored_trigger_unchanged"])
            self.assertEqual(payload["plan_state"]["status"], "ACTIVE")
            self.assertTrue(payload["plan_state"]["old_plan_reusable"])
            self.assertFalse(payload["plan_state"]["new_trigger_required"])
            self.assertEqual(item.market_metrics, original_metrics)
            self.assertEqual(item.execution_quality, original_quality)

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

            self.assertEqual(payload["verdict"]["status"], "WAIT_RETEST")
            self.assertFalse(payload["verdict"]["actionable"])
            self.assertIn("接近失效", payload["verdict"]["label"])
            self.assertIsNone(payload["live"]["remaining_rr"])
            self.assertFalse(payload["live"]["remaining_rr_applicable"])
            self.assertEqual(item.entry_eligibility, original_entry)
            self.assertTrue(payload["safety"]["stored_trigger_unchanged"])

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
