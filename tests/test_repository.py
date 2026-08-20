import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from radar.models import MarketContext, MarketState, Signal
from radar.repository import SignalRepository, classify_microstructure


def signal_fixture(
    inst_id="AAA-USDT-SWAP",
    event_ts=1_700_000_000_000,
    core_timestamp=None,
    core_high=101.0,
    core_low=99.0,
):
    core_timestamp = core_timestamp or event_ts
    event_key = f"SHORT:LONG:BREAKOUT:{event_ts}:ZONE-A"
    return Signal(
        inst_id=inst_id,
        direction="LONG",
        strategy="突破與價格接受",
        score=86.0,
        evidence=["價格接受", "控制權轉移"],
        entry_low="100",
        entry_high="100",
        stop_loss="90",
        take_profit_1="120",
        take_profit_2="130",
        risk_reward=2.0,
        invalidation="收盤跌破失效 Zone",
        spread_pct=0.02,
        quote_volume_24h=20_000_000,
        closed_candle_ts=core_timestamp,
        regime="BREAKOUT",
        market_metrics={
            "core_timestamp": core_timestamp,
            "core_high": core_high,
            "core_low": core_low,
            "core_close": 100.5,
        },
        signal_stage="CONFIRMED",
        readiness_score=86.0,
        radar_horizon="SHORT",
        trigger_type="BREAKOUT",
        freshness="NEW",
        market_participation={"state": "SUPPORT"},
        execution_quality={"score": 75.0},
        market_story={
            "trigger": {
                "event_ts": event_ts,
                "trigger_event_key": event_key,
            }
        },
        data_timestamp=core_timestamp,
    )


def state_fixture(signal, core_timestamp, core_high=101.0, core_low=99.0):
    return MarketState(
        inst_id=signal.inst_id,
        regime="BREAKOUT",
        direction=signal.direction,
        preferred_strategy=signal.strategy,
        readiness_score=signal.readiness_score,
        status="CONFIRMED",
        missing_conditions=[],
        spread_pct=signal.spread_pct,
        quote_volume_24h=signal.quote_volume_24h,
        closed_candle_ts=core_timestamp,
        radar_horizon=signal.radar_horizon,
        market_metrics={
            "core_timestamp": core_timestamp,
            "core_high": core_high,
            "core_low": core_low,
            "core_close": 100.5,
        },
    )


class SignalRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = SignalRepository(
            Path(self.temp_dir.name) / "radar-state.sqlite3"
        )

    def tearDown(self):
        self.repository.close()
        self.temp_dir.cleanup()

    def test_same_event_is_deduplicated_and_age_uses_closed_core_time(self):
        raw = signal_fixture()
        first = self.repository.reconcile(
            [raw],
            [state_fixture(raw, raw.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )[0]
        next_ts = raw.data_timestamp + 900_000
        refreshed = replace(
            raw,
            data_timestamp=next_ts,
            closed_candle_ts=next_ts,
            market_story={
                "trigger": {
                    "event_ts": next_ts,
                    "trigger_event_key": f"SHORT:LONG:BREAKOUT:{next_ts}:ZONE-A",
                    "zone_key": "ZONE-A",
                }
            },
            market_metrics={
                **raw.market_metrics,
                "core_timestamp": next_ts,
            },
        )
        second = self.repository.reconcile(
            [refreshed],
            [state_fixture(refreshed, next_ts)],
            "2026-08-20T00:15:00+00:00",
            "SHORT",
        )[0]

        self.assertEqual(first.trigger_id, second.trigger_id)
        self.assertEqual(
            second.market_story["trigger"]["event_ts"],
            raw.market_story["trigger"]["event_ts"],
        )
        self.assertEqual(second.lifecycle["age_bars"], 1)
        self.assertTrue(second.lifecycle["duplicate_locked"])
        count = self.repository._connection.execute(
            "SELECT COUNT(*) FROM signals"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_same_core_snapshot_does_not_advance_lifecycle_twice(self):
        event_ts = 1_700_000_000_000
        core_ts = event_ts + 3 * 900_000
        raw = replace(
            signal_fixture(event_ts=event_ts, core_timestamp=core_ts),
            signal_stage="EXTENDED",
            freshness="EXTENDED",
        )
        state = state_fixture(raw, core_ts)

        first = self.repository.reconcile(
            [raw],
            [state],
            "2026-08-20T00:45:00+00:00",
            "SHORT",
        )[0]
        second = self.repository.reconcile(
            [raw],
            [state],
            "2026-08-20T00:45:01+00:00",
            "SHORT",
        )[0]

        self.assertEqual(first.trigger_id, second.trigger_id)
        self.assertEqual(second.signal_stage, "EXTENDED")
        self.assertEqual(second.freshness, "EXTENDED")
        self.assertEqual(second.lifecycle["transition"], "UNCHANGED")

    def test_no_follow_through_is_a_lifecycle_state_not_a_new_signal(self):
        raw = signal_fixture()
        first = self.repository.reconcile(
            [raw],
            [state_fixture(raw, raw.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )[0]
        later_ts = raw.data_timestamp + 3 * 900_000
        state = state_fixture(first, later_ts, core_high=101.0, core_low=99.5)
        state.market_metrics["_core_path"] = [
            [raw.data_timestamp + step * 900_000, 101.0, 99.5, 100.5]
            for step in range(1, 4)
        ]
        later = self.repository.reconcile(
            [],
            [state],
            "2026-08-20T00:45:00+00:00",
            "SHORT",
        )[0]

        self.assertEqual(first.trigger_id, later.trigger_id)
        self.assertEqual(later.signal_stage, "NO_FOLLOW_THROUGH")
        self.assertEqual(later.freshness, "NO_FOLLOW_THROUGH")
        self.assertFalse(later.actionable)

    def test_early_signal_is_retained_for_three_closed_15m_bars(self):
        raw = replace(signal_fixture(), signal_stage="EARLY_SIGNAL")
        first = self.repository.reconcile(
            [raw],
            [state_fixture(raw, raw.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )[0]
        later_ts = raw.data_timestamp + 2 * 900_000
        state = state_fixture(first, later_ts, core_high=101.0, core_low=99.5)
        state.market_metrics["_core_path"] = [
            [raw.data_timestamp + step * 900_000, 101.0, 99.5, 100.5]
            for step in range(1, 3)
        ]
        later = self.repository.reconcile(
            [],
            [state],
            "2026-08-20T00:30:00+00:00",
            "SHORT",
        )[0]

        self.assertEqual(later.lifecycle["age_bars"], 2)
        self.assertEqual(later.signal_stage, "EARLY_SIGNAL")
        self.assertEqual(later.freshness, "NEW")

    def test_missing_core_interval_closes_without_fabricating_performance(self):
        raw = signal_fixture("GAP-USDT-SWAP")
        self.repository.reconcile(
            [raw],
            [state_fixture(raw, raw.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )
        later_ts = raw.data_timestamp + 2 * 900_000
        state = state_fixture(raw, later_ts, core_high=121.0, core_low=89.0)
        state.market_metrics["_core_path"] = [[later_ts, 121.0, 89.0, 100.0]]

        output = self.repository.reconcile(
            [],
            [state],
            "2026-08-20T00:30:00+00:00",
            "SHORT",
        )
        row = self.repository._connection.execute(
            "SELECT outcome, final_r, tp_sl_order, status FROM signals WHERE inst_id=?",
            (raw.inst_id,),
        ).fetchone()

        self.assertEqual(output, [])
        self.assertEqual(row["outcome"], "DATA_GAP")
        self.assertIsNone(row["final_r"])
        self.assertEqual(row["tp_sl_order"], "DATA_GAP")
        self.assertEqual(row["status"], "CLOSED")
        self.assertFalse(self.repository.performance()["available"])

    def test_real_closed_samples_feed_statistics_without_fake_win_rate(self):
        empty = self.repository.performance()
        self.assertFalse(empty["available"])
        self.assertIsNone(empty["overall"]["win_rate_pct"])

        win = signal_fixture("WIN-USDT-SWAP")
        loss = signal_fixture("LOSS-USDT-SWAP")
        for index, raw in enumerate((win, loss)):
            started = f"2026-08-20T00:0{index}:00+00:00"
            self.repository.reconcile(
                [raw],
                [state_fixture(raw, raw.data_timestamp)],
                started,
                "SHORT",
            )
        win_next = replace(
            win,
            data_timestamp=win.data_timestamp + 900_000,
            market_metrics={
                **win.market_metrics,
                "core_timestamp": win.data_timestamp + 900_000,
                "core_high": 121.0,
                "core_low": 99.0,
            },
        )
        loss_next = replace(
            loss,
            data_timestamp=loss.data_timestamp + 900_000,
            market_metrics={
                **loss.market_metrics,
                "core_timestamp": loss.data_timestamp + 900_000,
                "core_high": 101.0,
                "core_low": 89.0,
            },
        )
        self.repository.reconcile(
            [win_next, loss_next],
            [
                state_fixture(win_next, win_next.data_timestamp, 121.0, 99.0),
                state_fixture(loss_next, loss_next.data_timestamp, 101.0, 89.0),
            ],
            "2026-08-20T00:15:00+00:00",
            "SHORT",
        )
        stats = self.repository.performance()

        self.assertTrue(stats["available"])
        self.assertEqual(stats["overall"]["sample_size"], 2)
        self.assertEqual(stats["overall"]["win_rate_pct"], 50.0)
        self.assertEqual(stats["overall"]["average_r"], 0.5)
        self.assertEqual(stats["overall"]["expectancy_r"], 0.5)
        self.assertEqual(stats["overall"]["profit_factor"], 2.0)
        self.assertEqual(stats["overall"]["max_consecutive_losses"], 1)

    def test_all_closed_bars_between_scans_preserve_tp_sl_order(self):
        raw = signal_fixture("PATH-USDT-SWAP")
        self.repository.reconcile(
            [raw],
            [state_fixture(raw, raw.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )
        later_ts = raw.data_timestamp + 2 * 900_000
        refreshed = replace(
            raw,
            data_timestamp=later_ts,
            market_metrics={
                **raw.market_metrics,
                "core_timestamp": later_ts,
                "core_high": 101.0,
                "core_low": 89.0,
                "_core_path": [
                    [raw.data_timestamp + 900_000, 121.0, 95.0, 118.0],
                    [later_ts, 101.0, 89.0, 91.0],
                ],
            },
        )
        active_output = self.repository.reconcile(
            [refreshed],
            [state_fixture(refreshed, later_ts, 101.0, 89.0)],
            "2026-08-20T00:30:00+00:00",
            "SHORT",
        )
        row = self.repository._connection.execute(
            "SELECT outcome, final_r, tp_sl_order, status FROM signals WHERE inst_id=?",
            (raw.inst_id,),
        ).fetchone()

        self.assertEqual(active_output, [])
        self.assertEqual(row["outcome"], "TP1_FIRST")
        self.assertEqual(row["final_r"], 2.0)
        self.assertEqual(row["tp_sl_order"], "TP_FIRST")
        self.assertEqual(row["status"], "CLOSED")

    def test_order_book_requires_time_sequence_before_support_label(self):
        current = MarketContext(
            "AAA-USDT-SWAP",
            20_000_000,
            0.0,
            0.10,
            0.35,
            2,
            bid_depth_usd=120_000,
            ask_depth_usd=90_000,
        )
        first = classify_microstructure(None, current, "LONG", 0.0)
        sequenced = classify_microstructure(
            {"bid_depth_usd": 100_000, "ask_depth_usd": 100_000},
            current,
            "LONG",
            0.0,
        )

        self.assertEqual(first["state"], "FIRST_SNAPSHOT")
        self.assertEqual(sequenced["state"], "REFILL_ABSORPTION")
        self.assertTrue(sequenced["snapshot_only_is_not_support_resistance"])

    def test_out_of_order_book_snapshot_is_neutralized(self):
        current = MarketContext(
            "AAA-USDT-SWAP",
            20_000_000,
            0.0,
            0.40,
            0.70,
            100,
            bid_depth_usd=200_000,
            ask_depth_usd=50_000,
            source_timestamps={"order_book": 100},
        )
        result = classify_microstructure(
            {
                "bid_depth_usd": 100_000,
                "ask_depth_usd": 100_000,
                "order_book_ts": 101,
            },
            current,
            "LONG",
            0.2,
        )

        self.assertEqual(result["state"], "STALE_SNAPSHOT")
        self.assertIsNone(result["persistence"])


if __name__ == "__main__":
    unittest.main()
