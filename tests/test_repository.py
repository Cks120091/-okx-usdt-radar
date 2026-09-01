import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

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
        strategy_version="V3.4_CONTEXT",
        feature_schema_version="3.4.0",
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

    def test_dual_horizon_batch_rolls_back_short_when_long_reconcile_fails(self):
        short_signal = signal_fixture("ATOMIC-SHORT-USDT-SWAP")
        short_state = state_fixture(
            short_signal,
            short_signal.data_timestamp,
        )
        long_signal = replace(
            signal_fixture("ATOMIC-LONG-USDT-SWAP"),
            radar_horizon="LONG",
            market_story={
                "trigger": {
                    "event_ts": 1_700_000_000_000,
                    "trigger_event_key": (
                        "LONG:LONG:BREAKOUT:1700000000000:ZONE-A"
                    ),
                }
            },
        )
        long_state = state_fixture(
            long_signal,
            long_signal.data_timestamp,
        )
        original_reconcile = self.repository.reconcile

        def fail_long(raw_signals, market_states, completed_at, horizon):
            if horizon == "LONG":
                raise RuntimeError("simulated LONG persistence failure")
            return original_reconcile(
                raw_signals,
                market_states,
                completed_at,
                horizon,
            )

        with patch.object(
            self.repository,
            "reconcile",
            side_effect=fail_long,
        ):
            with self.assertRaisesRegex(RuntimeError, "LONG persistence"):
                self.repository.reconcile_batch(
                    {
                        "SHORT": ([short_signal], [short_state]),
                        "LONG": ([long_signal], [long_state]),
                    },
                    "2026-08-29T00:00:00+00:00",
                )

        for table in ("signals", "signal_events", "story_state"):
            count = self.repository._connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            self.assertEqual(count, 0, f"{table} retained a half-commit")
        self.assertEqual(self.repository._transaction_depth, 0)

        # A rolled-back batch must leave the connection usable by the existing
        # independent single-horizon path.
        committed = self.repository.reconcile(
            [short_signal],
            [short_state],
            "2026-08-29T00:01:00+00:00",
            "SHORT",
        )
        self.assertEqual(len(committed), 1)

    def test_recent_history_is_compact_and_excludes_raw_payload(self):
        raw = replace(
            signal_fixture(),
            risk_reward=3.27,
            market_metrics={
                **signal_fixture().market_metrics,
                "instrument_tick_size": 0.01,
            },
            management_plan={"tp2_rr_model": 5.875},
        )
        created = self.repository.reconcile(
            [raw],
            [],
            "2026-08-21T00:00:00+00:00",
            "SHORT",
        )

        rows = self.repository.recent_history(10)

        self.assertEqual(len(created), 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["inst_id"], "AAA-USDT-SWAP")
        self.assertEqual(rows[0]["instrument_tick_size"], 0.01)
        self.assertEqual(rows[0]["display_precision"], 2)
        self.assertEqual(rows[0]["tp1_r"], 3.27)
        self.assertEqual(rows[0]["tp2_r"], 5.875)
        self.assertNotIn("payload_json", rows[0])

    def test_recent_history_keeps_legacy_rows_without_display_metadata_safe(self):
        created = self.repository.reconcile(
            [signal_fixture()],
            [],
            "2026-08-21T00:00:00+00:00",
            "SHORT",
        )[0]
        self.repository._connection.execute(
            "UPDATE signals SET payload_json=? WHERE signal_id=?",
            ("{}", created.trigger_id),
        )
        self.repository._connection.commit()

        rows = self.repository.recent_history(10)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tp1_r"], 2.0)
        self.assertIsNone(rows[0]["tp2_r"])
        self.assertIsNone(rows[0]["instrument_tick_size"])
        self.assertIsNone(rows[0]["display_precision"])
        self.assertNotIn("payload_json", rows[0])

    def test_recent_history_filters_by_horizon_and_original_trigger_age(self):
        recent_ts = int(datetime(2026, 8, 24, 12, tzinfo=timezone.utc).timestamp() * 1000)
        old_ts = int(datetime(2026, 8, 23, 11, tzinfo=timezone.utc).timestamp() * 1000)
        recent = signal_fixture("RECENT-USDT-SWAP", event_ts=recent_ts)
        old = signal_fixture("OLD-USDT-SWAP", event_ts=old_ts)
        long_signal = replace(
            signal_fixture("LONG-USDT-SWAP", event_ts=old_ts),
            radar_horizon="LONG",
        )
        self.repository.reconcile(
            [recent, old], [], "2026-08-24T12:05:00+00:00", "SHORT"
        )
        self.repository.reconcile(
            [long_signal], [], "2026-08-24T12:05:00+00:00", "LONG"
        )

        as_of = datetime(2026, 8, 24, 12, 30, tzinfo=timezone.utc)
        short_rows = self.repository.recent_history(
            60, horizon="SHORT", max_age_hours=24, as_of=as_of
        )
        long_rows = self.repository.recent_history(
            60, horizon="LONG", max_age_hours=24 * 7, as_of=as_of
        )

        self.assertEqual([row["inst_id"] for row in short_rows], ["RECENT-USDT-SWAP"])
        self.assertEqual([row["inst_id"] for row in long_rows], ["LONG-USDT-SWAP"])

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

    def test_duplicate_raw_candidates_are_applied_in_core_time_order(self):
        raw = signal_fixture("RAW-ORDER-USDT-SWAP")
        first_ts = raw.data_timestamp + 900_000
        second_ts = raw.data_timestamp + 1_800_000
        first_update = replace(
            raw,
            score=87.0,
            data_timestamp=first_ts,
            closed_candle_ts=first_ts,
            market_metrics={
                **raw.market_metrics,
                "core_timestamp": first_ts,
            },
        )
        second_update = replace(
            raw,
            score=88.0,
            data_timestamp=second_ts,
            closed_candle_ts=second_ts,
            market_metrics={
                **raw.market_metrics,
                "core_timestamp": second_ts,
            },
        )

        def run(repository, candidates):
            repository.reconcile(
                [raw],
                [state_fixture(raw, raw.data_timestamp)],
                "2026-08-20T00:00:00+00:00",
                "SHORT",
            )
            output = repository.reconcile(
                candidates,
                [state_fixture(second_update, second_ts)],
                "2026-08-20T00:30:00+00:00",
                "SHORT",
            )
            row = repository._connection.execute(
                "SELECT status, outcome FROM signals WHERE inst_id=?",
                (raw.inst_id,),
            ).fetchone()
            return output, row

        forward_output, forward_row = run(
            self.repository,
            [first_update, second_update],
        )
        reverse_repository = SignalRepository(
            Path(self.temp_dir.name) / "reverse-order.sqlite3"
        )
        try:
            reverse_output, reverse_row = run(
                reverse_repository,
                [second_update, first_update],
            )
        finally:
            reverse_repository.close()

        for output, row in (
            (forward_output, forward_row),
            (reverse_output, reverse_row),
        ):
            self.assertEqual(len(output), 1)
            self.assertEqual(output[0].score, 88.0)
            self.assertEqual(output[0].lifecycle["age_bars"], 2)
            self.assertEqual(row["status"], "ACTIVE")
            self.assertIsNone(row["outcome"])

    def test_load_story_exposes_active_last_evaluated_core_timestamp(self):
        raw = signal_fixture("STORY-CORE-USDT-SWAP")
        next_ts = raw.data_timestamp + 900_000
        self.repository.reconcile(
            [raw],
            [state_fixture(raw, raw.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )
        state = state_fixture(raw, next_ts)
        self.repository.reconcile(
            [],
            [state],
            "2026-08-20T00:15:00+00:00",
            "SHORT",
        )

        story = self.repository.load_story(raw.inst_id, "SHORT")

        self.assertEqual(story["active_trigger_direction"], "LONG")
        self.assertEqual(story["last_evaluated_core_ts"], next_ts)

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
        created = self.repository.reconcile(
            [raw],
            [state_fixture(raw, raw.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )[0]
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
            "SELECT stage, outcome, final_r, tp_sl_order, status FROM signals WHERE inst_id=?",
            (raw.inst_id,),
        ).fetchone()

        self.assertEqual(output, [])
        self.assertEqual(row["outcome"], "DATA_GAP")
        self.assertIsNone(row["final_r"])
        self.assertEqual(row["tp_sl_order"], "DATA_GAP")
        self.assertEqual(row["status"], "CLOSED")
        self.assertEqual(row["stage"], "CLOSED_UNKNOWN")
        self.assertFalse(self.repository.performance()["available"])
        self.assertEqual(
            self.repository.preflight_terminal_kind(created),
            "CLOSED_UNKNOWN",
        )

    def test_preflight_stop_closes_exact_plan_without_fabricating_tp_sl_order(self):
        raw = signal_fixture("PREFLIGHT-USDT-SWAP")
        created = self.repository.reconcile(
            [raw],
            [state_fixture(raw, raw.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )[0]

        closed = self.repository.invalidate_preflight_plan(
            created,
            "2026-08-20T00:05:00+00:00",
        )
        row = self.repository._connection.execute(
            """
            SELECT stage, freshness, status, outcome, final_r, tp_sl_order
            FROM signals WHERE signal_id=?
            """,
            (created.trigger_id,),
        ).fetchone()

        self.assertTrue(closed)
        self.assertFalse(
            self.repository.invalidate_preflight_plan(
                created,
                "2026-08-20T00:06:00+00:00",
            )
        )
        self.assertEqual(row["stage"], "INVALIDATED")
        self.assertEqual(row["freshness"], "INVALIDATED")
        self.assertEqual(row["status"], "CLOSED")
        self.assertEqual(row["outcome"], "PREFLIGHT_STOP_CROSSED")
        self.assertIsNone(row["final_r"])
        self.assertEqual(row["tp_sl_order"], "UNKNOWN_FROM_LIVE_TICKER")
        self.assertFalse(self.repository.performance()["available"])
        self.assertEqual(
            self.repository.preflight_terminal_kind(created),
            "INVALIDATED",
        )
        events = self.repository._connection.execute(
            "SELECT to_stage, event_type FROM signal_events WHERE signal_id=?",
            (created.trigger_id,),
        ).fetchall()
        self.assertEqual(
            [
                tuple(event)
                for event in events
                if event["event_type"] == "PREFLIGHT_PLAN_INVALIDATED"
            ],
            [("INVALIDATED", "PREFLIGHT_PLAN_INVALIDATED")],
        )

    def test_ambiguous_same_bar_is_closed_unknown_not_a_fabricated_stop(self):
        raw = signal_fixture("AMBIGUOUS-USDT-SWAP")
        created = self.repository.reconcile(
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
            market_metrics={
                **raw.market_metrics,
                "core_timestamp": next_ts,
                "core_high": 121.0,
                "core_low": 89.0,
                "_core_path": [[next_ts, 121.0, 89.0, 100.0]],
            },
        )

        self.assertEqual(
            self.repository.reconcile(
                [refreshed],
                [state_fixture(refreshed, next_ts, 121.0, 89.0)],
                "2026-08-20T00:15:00+00:00",
                "SHORT",
            ),
            [],
        )
        row = self.repository._connection.execute(
            "SELECT stage, outcome, status FROM signals WHERE signal_id=?",
            (created.trigger_id,),
        ).fetchone()
        self.assertEqual(row["outcome"], "AMBIGUOUS_SAME_BAR")
        self.assertEqual(row["status"], "CLOSED")
        self.assertEqual(row["stage"], "CLOSED_UNKNOWN")
        self.assertEqual(
            self.repository.preflight_terminal_kind(created),
            "CLOSED_UNKNOWN",
        )

    def test_preflight_target_completion_is_terminal_without_fabricating_performance(self):
        raw = signal_fixture("PREFLIGHT-TP-USDT-SWAP")
        created = self.repository.reconcile(
            [raw],
            [state_fixture(raw, raw.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )[0]

        completed = self.repository.complete_preflight_plan(
            created,
            "2026-08-20T00:05:00+00:00",
        )
        row = self.repository._connection.execute(
            """
            SELECT stage, freshness, status, outcome, final_r, tp_sl_order,
                   payload_json
            FROM signals WHERE signal_id=?
            """,
            (created.trigger_id,),
        ).fetchone()
        stored = Signal.from_dict(json.loads(row["payload_json"]))
        history = self.repository.recent_history(10)
        performance = self.repository.performance()

        self.assertTrue(completed)
        self.assertFalse(
            self.repository.complete_preflight_plan(
                created,
                "2026-08-20T00:06:00+00:00",
            )
        )
        self.assertEqual(row["stage"], "COMPLETED")
        self.assertEqual(row["freshness"], "COMPLETED")
        self.assertEqual(row["status"], "CLOSED")
        self.assertEqual(row["outcome"], "PREFLIGHT_TARGET_REACHED")
        self.assertIsNone(row["final_r"])
        self.assertEqual(row["tp_sl_order"], "UNKNOWN_FROM_LIVE_TICKER")
        self.assertEqual(
            self.repository.preflight_terminal_kind(created),
            "COMPLETED",
        )
        self.assertTrue(stored.lifecycle["terminal"])
        self.assertEqual(stored.signal_stage, "COMPLETED")
        self.assertEqual(stored.lifecycle["current_stage"], "COMPLETED")
        self.assertEqual(stored.lifecycle["status"], "COMPLETED")
        self.assertEqual(stored.entry_eligibility["status"], "COMPLETED")
        self.assertFalse(stored.entry_eligibility["new_entry_allowed"])
        self.assertEqual(history[0]["outcome"], "PREFLIGHT_TARGET_REACHED")
        self.assertIsNone(history[0]["final_r"])
        self.assertFalse(performance["available"])
        self.assertEqual(performance["overall"]["sample_size"], 0)
        self.assertIsNone(performance["overall"]["win_rate_pct"])
        self.assertEqual(performance["research"]["sample_size"], 0)
        events = self.repository._connection.execute(
            "SELECT to_stage, event_type FROM signal_events WHERE signal_id=?",
            (created.trigger_id,),
        ).fetchall()
        self.assertEqual(
            [tuple(event) for event in events if event["event_type"] == "PREFLIGHT_PLAN_COMPLETED"],
            [("COMPLETED", "PREFLIGHT_PLAN_COMPLETED")],
        )

    def test_terminal_cards_use_five_hour_and_twenty_four_hour_windows(self):
        short_raw = replace(
            signal_fixture("SHORT-RETENTION-USDT-SWAP"),
            entry_eligibility={"status": "ENTRY_READY", "actionable": True},
        )
        short_created = self.repository.reconcile(
            [short_raw],
            [state_fixture(short_raw, short_raw.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )[0]
        short_closed_at = datetime(2026, 8, 20, 0, 5, tzinfo=timezone.utc)
        self.assertTrue(
            self.repository.complete_preflight_plan(
                short_created,
                short_closed_at.isoformat(),
            )
        )

        short_terminal = self.repository.load_terminal_signal(short_created)
        self.assertIsNotNone(short_terminal)
        self.assertEqual(
            short_terminal.entry_eligibility["label"],
            "已達止盈｜本次交易計畫完成",
        )
        self.assertEqual(
            short_terminal.lifecycle["retention_until"],
            (short_closed_at + timedelta(hours=5)).isoformat(),
        )
        self.assertEqual(
            [
                item.trigger_id
                for item in self.repository.recent_terminal_signals(
                    "SHORT",
                    as_of=short_closed_at + timedelta(hours=4, minutes=59),
                )
            ],
            [short_created.trigger_id],
        )
        self.assertEqual(
            self.repository.recent_terminal_signals(
                "SHORT",
                as_of=short_closed_at + timedelta(hours=5),
            ),
            [],
        )

        long_event_ts = short_raw.data_timestamp + 14_400_000
        long_raw = replace(
            signal_fixture(
                "LONG-RETENTION-USDT-SWAP",
                event_ts=long_event_ts,
                core_timestamp=long_event_ts,
            ),
            radar_horizon="LONG",
            entry_eligibility={"status": "ENTRY_READY", "actionable": True},
            market_story={
                "trigger": {
                    "event_ts": long_event_ts,
                    "trigger_event_key": f"LONG:LONG:BREAKOUT:{long_event_ts}:ZONE-A",
                }
            },
        )
        long_created = self.repository.reconcile(
            [long_raw],
            [state_fixture(long_raw, long_event_ts)],
            "2026-08-20T04:00:00+00:00",
            "LONG",
        )[0]
        long_closed_at = datetime(2026, 8, 20, 4, 5, tzinfo=timezone.utc)
        self.assertTrue(
            self.repository.invalidate_preflight_plan(
                long_created,
                long_closed_at.isoformat(),
            )
        )
        long_terminal = self.repository.load_terminal_signal(long_created)
        self.assertEqual(
            long_terminal.entry_eligibility["label"],
            "已達止損｜本次交易計畫結束",
        )
        self.assertEqual(
            long_terminal.lifecycle["retention_until"],
            (long_closed_at + timedelta(hours=24)).isoformat(),
        )
        self.assertEqual(
            [
                item.trigger_id
                for item in self.repository.recent_terminal_signals(
                    "LONG",
                    as_of=long_closed_at + timedelta(hours=23, minutes=59),
                )
            ],
            [long_created.trigger_id],
        )
        self.assertEqual(
            self.repository.recent_terminal_signals(
                "LONG",
                as_of=long_closed_at + timedelta(hours=24),
            ),
            [],
        )

    def test_entry_ready_membership_stays_sticky_when_price_leaves_zone(self):
        raw = replace(
            signal_fixture("STICKY-READY-USDT-SWAP"),
            entry_eligibility={"status": "ENTRY_READY", "actionable": True},
        )
        created = self.repository.reconcile(
            [raw],
            [state_fixture(raw, raw.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )[0]
        next_ts = raw.data_timestamp + 900_000
        moved_away = replace(
            raw,
            entry_low="999",
            entry_high="1000",
            stop_loss="998",
            take_profit_1="1005",
            entry_eligibility={
                "status": "MISSED_ENTRY",
                "actionable": False,
                "new_entry_allowed": False,
            },
            data_timestamp=next_ts,
            closed_candle_ts=next_ts,
            market_metrics={
                **raw.market_metrics,
                "core_timestamp": next_ts,
                "core_high": 105.0,
                "core_low": 99.0,
            },
        )
        updated = self.repository.reconcile(
            [moved_away],
            [state_fixture(moved_away, next_ts, 105.0, 99.0)],
            "2026-08-20T00:15:00+00:00",
            "SHORT",
        )[0]

        self.assertEqual(updated.trigger_id, created.trigger_id)
        self.assertTrue(updated.lifecycle["entry_ready_once"])
        self.assertEqual(
            updated.lifecycle["entry_ready_at"],
            "2026-08-20T00:00:00+00:00",
        )
        self.assertEqual(updated.entry_eligibility["status"], "MISSED_ENTRY")
        self.assertEqual(updated.entry_low, created.entry_low)
        self.assertEqual(updated.stop_loss, created.stop_loss)
        self.assertEqual(updated.take_profit_1, created.take_profit_1)

    def test_same_scan_can_close_old_plan_and_create_independent_new_trigger(self):
        old_raw = replace(
            signal_fixture("ROLLOVER-USDT-SWAP"),
            entry_eligibility={"status": "ENTRY_READY", "actionable": True},
        )
        old_created = self.repository.reconcile(
            [old_raw],
            [state_fixture(old_raw, old_raw.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )[0]
        next_ts = old_raw.data_timestamp + 900_000
        new_event_key = f"SHORT:LONG:BREAKOUT:{next_ts}:ZONE-NEW"
        new_raw = replace(
            old_raw,
            entry_low="122",
            entry_high="123",
            stop_loss="110",
            take_profit_1="140",
            take_profit_2="150",
            data_timestamp=next_ts,
            closed_candle_ts=next_ts,
            market_story={
                "trigger": {
                    "event_ts": next_ts,
                    "trigger_event_key": new_event_key,
                }
            },
            market_metrics={
                **old_raw.market_metrics,
                "core_timestamp": next_ts,
                "core_high": 121.0,
                "core_low": 99.0,
                "core_close": 120.0,
                "_core_path": [[next_ts, 121.0, 99.0, 120.0]],
            },
        )

        output = self.repository.reconcile(
            [new_raw],
            [state_fixture(new_raw, next_ts, 121.0, 99.0)],
            "2026-08-20T00:15:00+00:00",
            "SHORT",
        )
        self.assertEqual(len(output), 1)
        new_created = output[0]
        self.assertNotEqual(new_created.trigger_id, old_created.trigger_id)
        self.assertEqual(new_created.entry_low, "122")
        self.assertEqual(new_created.stop_loss, "110")
        self.assertEqual(new_created.take_profit_1, "140")

        old_terminal = self.repository.load_terminal_signal(old_created)
        self.assertEqual(old_terminal.signal_stage, "COMPLETED")
        self.assertNotIn(new_event_key, old_terminal.lifecycle["event_keys"])
        self.assertEqual(
            self.repository.load_active_signal(old_raw.inst_id, "SHORT").trigger_id,
            new_created.trigger_id,
        )
        rows = self.repository._connection.execute(
            "SELECT signal_id, status FROM signals WHERE inst_id=? ORDER BY updated_at",
            (old_raw.inst_id,),
        ).fetchall()
        self.assertEqual(
            {(row["signal_id"], row["status"]) for row in rows},
            {
                (old_created.trigger_id, "CLOSED"),
                (new_created.trigger_id, "ACTIVE"),
            },
        )

    def test_completed_preflight_episode_cannot_revive_or_close_a_new_episode(self):
        raw = signal_fixture("PREFLIGHT-TP-CAS-USDT-SWAP")
        created = self.repository.reconcile(
            [raw],
            [state_fixture(raw, raw.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )[0]
        self.assertTrue(
            self.repository.complete_preflight_plan(
                created,
                "2026-08-20T00:05:00+00:00",
            )
        )

        returned_ts = raw.data_timestamp + 900_000
        returned_to_entry = replace(
            raw,
            data_timestamp=returned_ts,
            closed_candle_ts=returned_ts,
            market_metrics={
                **raw.market_metrics,
                "core_timestamp": returned_ts,
                "core_high": 101.0,
                "core_low": 99.0,
                "core_close": 100.0,
            },
        )
        terminal_projection = self.repository._reconcile_raw_signal(
            returned_to_entry,
            "2026-08-20T00:15:00+00:00",
        )
        self.assertEqual(terminal_projection.signal_stage, "COMPLETED")
        self.assertEqual(terminal_projection.freshness, "COMPLETED")
        self.assertEqual(
            terminal_projection.lifecycle["status"],
            "COMPLETED",
        )
        self.assertFalse(terminal_projection.actionable)
        replay = self.repository.reconcile(
            [returned_to_entry],
            [state_fixture(returned_to_entry, returned_ts)],
            "2026-08-20T00:15:00+00:00",
            "SHORT",
        )
        self.assertEqual(replay, [])
        self.assertIsNone(
            self.repository.load_active_signal(raw.inst_id, "SHORT")
        )

        new_event_ts = returned_ts + 900_000
        new_trigger = replace(
            raw,
            data_timestamp=new_event_ts,
            closed_candle_ts=new_event_ts,
            market_metrics={
                **raw.market_metrics,
                "core_timestamp": new_event_ts,
            },
            market_story={
                "trigger": {
                    "event_ts": new_event_ts,
                    "trigger_event_key": (
                        f"SHORT:LONG:BREAKOUT:{new_event_ts}:ZONE-NEW"
                    ),
                }
            },
        )
        new_episode = self.repository.reconcile(
            [new_trigger],
            [state_fixture(new_trigger, new_event_ts)],
            "2026-08-20T00:30:00+00:00",
            "SHORT",
        )[0]

        self.assertNotEqual(new_episode.trigger_id, created.trigger_id)
        self.assertFalse(
            self.repository.complete_preflight_plan(
                created,
                "2026-08-20T00:31:00+00:00",
            )
        )
        active = self.repository.load_active_signal(raw.inst_id, "SHORT")
        self.assertIsNotNone(active)
        self.assertEqual(active.trigger_id, new_episode.trigger_id)

    def test_closed_event_tombstone_survives_repository_restart(self):
        raw = signal_fixture("TOMBSTONE-USDT-SWAP")
        created = self.repository.reconcile(
            [raw],
            [state_fixture(raw, raw.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )[0]
        self.assertTrue(
            self.repository.invalidate_preflight_plan(
                created,
                "2026-08-20T00:05:00+00:00",
            )
        )

        database_path = Path(self.temp_dir.name) / "radar-state.sqlite3"
        self.repository.close()
        self.repository = SignalRepository(database_path)
        replay = replace(
            raw,
            data_timestamp=raw.data_timestamp + 900_000,
            closed_candle_ts=raw.data_timestamp + 900_000,
            market_metrics={
                **raw.market_metrics,
                "core_timestamp": raw.data_timestamp + 900_000,
                "core_close": 100.0,
            },
        )

        output = self.repository.reconcile(
            [replay],
            [state_fixture(replay, replay.data_timestamp)],
            "2026-08-20T00:15:00+00:00",
            "SHORT",
        )
        rows = self.repository._connection.execute(
            "SELECT signal_id, status FROM signals WHERE inst_id=?",
            (raw.inst_id,),
        ).fetchall()

        self.assertEqual(output, [])
        self.assertIsNone(self.repository.load_active_signal(raw.inst_id, "SHORT"))
        self.assertEqual([(row["signal_id"], row["status"]) for row in rows], [(created.trigger_id, "CLOSED")])

    def test_price_returning_to_entry_cannot_revive_closed_plan(self):
        raw = signal_fixture("RETURN-USDT-SWAP")
        created = self.repository.reconcile(
            [raw],
            [state_fixture(raw, raw.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )[0]
        self.repository.invalidate_preflight_plan(
            created,
            "2026-08-20T00:05:00+00:00",
        )
        returned_to_entry = replace(
            raw,
            data_timestamp=raw.data_timestamp + 900_000,
            closed_candle_ts=raw.data_timestamp + 900_000,
            market_metrics={
                **raw.market_metrics,
                "core_timestamp": raw.data_timestamp + 900_000,
                "core_high": 101.0,
                "core_low": 99.0,
                "core_close": 100.0,
            },
        )

        replay = self.repository.reconcile(
            [returned_to_entry],
            [state_fixture(returned_to_entry, returned_to_entry.data_timestamp)],
            "2026-08-20T00:15:00+00:00",
            "SHORT",
        )
        active_count = self.repository._connection.execute(
            "SELECT COUNT(*) FROM signals WHERE inst_id=? AND status='ACTIVE'",
            (raw.inst_id,),
        ).fetchone()[0]

        self.assertEqual(replay, [])
        self.assertEqual(active_count, 0)

        renamed_old_event = replace(
            returned_to_entry,
            market_story={
                "trigger": {
                    "event_ts": raw.market_story["trigger"]["event_ts"],
                    "trigger_event_key": "SHORT:LONG:BREAKOUT:renamed-old:ZONE-X",
                }
            },
        )
        self.assertEqual(
            self.repository.reconcile(
                [renamed_old_event],
                [state_fixture(renamed_old_event, renamed_old_event.data_timestamp)],
                "2026-08-20T00:16:00+00:00",
                "SHORT",
            ),
            [],
        )

        new_event_ts = raw.data_timestamp + 1_800_000
        new_trigger = replace(
            returned_to_entry,
            entry_low="101",
            entry_high="101",
            stop_loss="91",
            take_profit_1="121",
            take_profit_2="131",
            data_timestamp=new_event_ts,
            closed_candle_ts=new_event_ts,
            market_story={
                "trigger": {
                    "event_ts": new_event_ts,
                    "trigger_event_key": f"SHORT:LONG:BREAKOUT:{new_event_ts}:ZONE-B",
                }
            },
            market_metrics={
                **raw.market_metrics,
                "core_timestamp": new_event_ts,
                "core_high": 102.0,
                "core_low": 100.0,
                "core_close": 101.0,
            },
        )
        replacement = self.repository.reconcile(
            [new_trigger],
            [state_fixture(new_trigger, new_event_ts, 102.0, 100.0)],
            "2026-08-20T00:30:00+00:00",
            "SHORT",
        )[0]

        self.assertNotEqual(replacement.trigger_id, created.trigger_id)
        self.assertEqual(replacement.entry_low, "101")

    def test_closed_event_replay_cannot_mutate_a_newer_active_episode(self):
        old = signal_fixture("REPLAY-USDT-SWAP")
        old_created = self.repository.reconcile(
            [old],
            [state_fixture(old, old.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )[0]
        self.repository.invalidate_preflight_plan(
            old_created,
            "2026-08-20T00:05:00+00:00",
        )
        new_ts = old.data_timestamp + 900_000
        new = replace(
            old,
            score=94.0,
            evidence=["新 Episode 證據"],
            entry_low="101",
            entry_high="101",
            stop_loss="91",
            take_profit_1="121",
            take_profit_2="131",
            data_timestamp=new_ts,
            closed_candle_ts=new_ts,
            market_story={
                "trigger": {
                    "event_ts": new_ts,
                    "trigger_event_key": f"SHORT:LONG:BREAKOUT:{new_ts}:ZONE-B",
                }
            },
            market_metrics={
                **old.market_metrics,
                "core_timestamp": new_ts,
                "core_high": 102.0,
                "core_low": 100.0,
                "core_close": 101.0,
            },
        )
        new_created = self.repository.reconcile(
            [new],
            [state_fixture(new, new_ts, 102.0, 100.0)],
            "2026-08-20T00:15:00+00:00",
            "SHORT",
        )[0]
        replay_ts = new_ts + 900_000
        old_replay = replace(
            old,
            score=1.0,
            evidence=["不可倒灌的舊證據"],
            data_timestamp=replay_ts,
            closed_candle_ts=replay_ts,
            market_metrics={
                **old.market_metrics,
                "core_timestamp": replay_ts,
                "core_high": 81.0,
                "core_low": 79.0,
                "core_close": 80.0,
            },
        )

        output = self.repository.reconcile(
            [old_replay],
            [state_fixture(old_replay, replay_ts, 81.0, 79.0)],
            "2026-08-20T00:30:00+00:00",
            "SHORT",
        )
        active_row = self.repository._connection.execute(
            """
            SELECT status, outcome, payload_json FROM signals
            WHERE signal_id=?
            """,
            (new_created.trigger_id,),
        ).fetchone()
        persisted = self.repository.load_active_signal(old.inst_id, "SHORT")

        self.assertEqual(len(output), 1)
        self.assertEqual(output[0].trigger_id, new_created.trigger_id)
        self.assertEqual(output[0].freshness, "DATA_UNAVAILABLE")
        self.assertEqual(
            output[0].lifecycle["transition"],
            "TERMINAL_REPLAY_IGNORED",
        )
        self.assertFalse(output[0].actionable)
        self.assertEqual(active_row["status"], "ACTIVE")
        self.assertIsNone(active_row["outcome"])
        self.assertEqual(persisted.score, 94.0)
        self.assertEqual(persisted.evidence, ["新 Episode 證據"])

        ordered_ts = replay_ts
        valid_update = replace(
            new,
            score=96.0,
            evidence=["有效的新資料"],
            data_timestamp=ordered_ts,
            closed_candle_ts=ordered_ts,
            market_metrics={
                **new.market_metrics,
                "core_timestamp": ordered_ts,
            },
        )
        replay_same_round = replace(
            old_replay,
            data_timestamp=ordered_ts,
            closed_candle_ts=ordered_ts,
            market_metrics={
                **old.market_metrics,
                "core_timestamp": ordered_ts,
            },
        )
        valid_first = self.repository.reconcile(
            [valid_update, replay_same_round],
            [state_fixture(valid_update, ordered_ts)],
            "2026-08-20T00:45:00+00:00",
            "SHORT",
        )
        self.assertEqual(len(valid_first), 1)
        self.assertEqual(valid_first[0].score, 96.0)
        self.assertNotEqual(valid_first[0].freshness, "DATA_UNAVAILABLE")

        reverse_ts = ordered_ts + 900_000
        reverse_valid = replace(
            valid_update,
            score=97.0,
            evidence=["反序仍採用有效資料"],
            data_timestamp=reverse_ts,
            closed_candle_ts=reverse_ts,
            market_metrics={
                **valid_update.market_metrics,
                "core_timestamp": reverse_ts,
            },
        )
        reverse_replay = replace(
            replay_same_round,
            data_timestamp=reverse_ts,
            closed_candle_ts=reverse_ts,
            market_metrics={
                **replay_same_round.market_metrics,
                "core_timestamp": reverse_ts,
            },
        )
        replay_first = self.repository.reconcile(
            [reverse_replay, reverse_valid],
            [state_fixture(reverse_valid, reverse_ts)],
            "2026-08-20T01:00:00+00:00",
            "SHORT",
        )
        self.assertEqual(len(replay_first), 1)
        self.assertEqual(replay_first[0].score, 97.0)
        self.assertNotEqual(replay_first[0].freshness, "DATA_UNAVAILABLE")

    def test_one_active_direction_per_horizon_while_horizons_stay_independent(self):
        long_short_term = signal_fixture("ISOLATED-USDT-SWAP")
        created = self.repository.reconcile(
            [long_short_term],
            [state_fixture(long_short_term, long_short_term.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )[0]
        opposite_ts = long_short_term.data_timestamp + 900_000
        short_short_term = replace(
            long_short_term,
            direction="SHORT",
            entry_low="100",
            entry_high="100",
            stop_loss="110",
            take_profit_1="80",
            take_profit_2="70",
            data_timestamp=opposite_ts,
            closed_candle_ts=opposite_ts,
            market_story={
                "trigger": {
                    "event_ts": opposite_ts,
                    "trigger_event_key": f"SHORT:SHORT:REVERSAL:{opposite_ts}:ZONE-B",
                }
            },
            market_metrics={
                **long_short_term.market_metrics,
                "core_timestamp": opposite_ts,
                "core_high": 101.0,
                "core_low": 99.0,
                "core_close": 100.0,
            },
        )

        short_output = self.repository.reconcile(
            [short_short_term],
            [state_fixture(short_short_term, opposite_ts)],
            "2026-08-20T00:15:00+00:00",
            "SHORT",
        )
        short_rows = self.repository._connection.execute(
            """
            SELECT signal_id, direction FROM signals
            WHERE inst_id=? AND horizon='SHORT' AND status='ACTIVE'
            """,
            (long_short_term.inst_id,),
        ).fetchall()

        self.assertEqual(len(short_output), 1)
        self.assertEqual(short_output[0].trigger_id, created.trigger_id)
        self.assertEqual(short_output[0].direction, "LONG")
        self.assertEqual([(row["signal_id"], row["direction"]) for row in short_rows], [(created.trigger_id, "LONG")])

        short_long_term = replace(
            short_short_term,
            radar_horizon="LONG",
            market_story={
                "trigger": {
                    "event_ts": opposite_ts,
                    "trigger_event_key": f"LONG:SHORT:REVERSAL:{opposite_ts}:ZONE-B",
                }
            },
        )
        long_output = self.repository.reconcile(
            [short_long_term],
            [state_fixture(short_long_term, opposite_ts)],
            "2026-08-20T00:15:00+00:00",
            "LONG",
        )

        self.assertEqual(long_output[0].direction, "SHORT")
        self.assertEqual(
            self.repository.load_active_signal(long_short_term.inst_id, "SHORT").direction,
            "LONG",
        )
        self.assertEqual(
            self.repository.load_active_signal(long_short_term.inst_id, "LONG").direction,
            "SHORT",
        )

    def test_restart_repairs_legacy_dual_active_rows_and_enforces_unique_main(self):
        raw = signal_fixture("LEGACY-DUAL-USDT-SWAP")
        created = self.repository.reconcile(
            [raw],
            [state_fixture(raw, raw.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )[0]
        connection = self.repository._connection
        connection.execute("DROP INDEX uq_signals_active_episode")
        source = connection.execute(
            "SELECT * FROM signals WHERE signal_id=?",
            (created.trigger_id,),
        ).fetchone()
        columns = [
            row["name"]
            for row in connection.execute("PRAGMA table_info(signals)").fetchall()
        ]
        values = {column: source[column] for column in columns}
        legacy_id = "legacy-opposite-active"
        legacy_payload = created.to_dict()
        legacy_payload.update(
            {
                "trigger_id": legacy_id,
                "direction": "LONG",
            }
        )
        legacy_payload["market_story"] = raw.market_story
        values.update(
            {
                "signal_id": legacy_id,
                "event_key": raw.market_story["trigger"]["trigger_event_key"],
                "direction": "LONG",
                "event_ts": raw.data_timestamp,
                "updated_at": "2026-08-20T00:15:00+00:00",
                "payload_json": json.dumps(legacy_payload, ensure_ascii=False),
            }
        )
        placeholders = ", ".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO signals({', '.join(columns)}) VALUES({placeholders})",
            [values[column] for column in columns],
        )
        connection.commit()
        self.assertEqual(
            connection.execute(
                """
                SELECT COUNT(*) FROM signals
                WHERE inst_id=? AND horizon='SHORT' AND status='ACTIVE'
                """,
                (raw.inst_id,),
            ).fetchone()[0],
            2,
        )

        database_path = Path(self.temp_dir.name) / "radar-state.sqlite3"
        self.repository.close()
        self.repository = SignalRepository(database_path)
        rows = self.repository._connection.execute(
            """
            SELECT signal_id, status, outcome FROM signals
            WHERE inst_id=? ORDER BY signal_id
            """,
            (raw.inst_id,),
        ).fetchall()

        active = [row for row in rows if row["status"] == "ACTIVE"]
        retired = [row for row in rows if row["status"] == "CLOSED"]
        self.assertEqual([row["signal_id"] for row in active], [legacy_id])
        self.assertEqual([row["signal_id"] for row in retired], [created.trigger_id])
        self.assertEqual(retired[0]["outcome"], "REPOSITORY_ACTIVE_CONFLICT")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository._connection.execute(
                "UPDATE signals SET status='ACTIVE' WHERE signal_id=?",
                (created.trigger_id,),
            )
        self.repository._connection.rollback()

        next_ts = raw.data_timestamp + 900_000
        refreshed = replace(
            raw,
            score=99.0,
            data_timestamp=next_ts,
            closed_candle_ts=next_ts,
            market_metrics={
                **raw.market_metrics,
                "core_timestamp": next_ts,
            },
        )
        winner = self.repository.reconcile(
            [refreshed],
            [state_fixture(refreshed, next_ts)],
            "2026-08-20T00:30:00+00:00",
            "SHORT",
        )[0]

        self.assertEqual(winner.trigger_id, legacy_id)
        self.assertEqual(winner.score, 99.0)
        self.assertNotEqual(winner.freshness, "DATA_UNAVAILABLE")

    def test_active_reentry_keeps_episode_id_and_original_trade_plan(self):
        base = signal_fixture("REENTRY-USDT-SWAP")
        raw = replace(
            base,
            management_plan={"plan": "ORIGINAL"},
            market_story={
                "trigger": {
                    **base.market_story["trigger"],
                    "invalidation_price": 90.0,
                }
            },
        )
        created = self.repository.reconcile(
            [raw],
            [state_fixture(raw, raw.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )[0]
        next_ts = raw.data_timestamp + 900_000
        reentry = replace(
            raw,
            trigger_type="CONTINUATION",
            signal_stage="REENTRY",
            freshness="REACTIVATED",
            entry_low="105",
            entry_high="106",
            stop_loss="95",
            take_profit_1="125",
            take_profit_2="135",
            risk_reward=9.0,
            invalidation="新的失效說明不可覆蓋舊計畫",
            management_plan={"plan": "REPLACEMENT"},
            data_timestamp=next_ts,
            closed_candle_ts=next_ts,
            market_story={
                "trigger": {
                    "event_ts": next_ts,
                    "trigger_event_key": f"SHORT:LONG:CONTINUATION:{next_ts}:ZONE-B",
                    "invalidation_price": 95.0,
                }
            },
            market_metrics={
                **raw.market_metrics,
                "core_timestamp": next_ts,
            },
        )

        updated = self.repository.reconcile(
            [reentry],
            [state_fixture(reentry, next_ts)],
            "2026-08-20T00:15:00+00:00",
            "SHORT",
        )[0]
        row_count = self.repository._connection.execute(
            "SELECT COUNT(*) FROM signals WHERE inst_id=?",
            (raw.inst_id,),
        ).fetchone()[0]

        self.assertEqual(updated.trigger_id, created.trigger_id)
        self.assertEqual(updated.signal_stage, "REENTRY")
        self.assertEqual(updated.entry_low, raw.entry_low)
        self.assertEqual(updated.entry_high, raw.entry_high)
        self.assertEqual(updated.stop_loss, raw.stop_loss)
        self.assertEqual(updated.take_profit_1, raw.take_profit_1)
        self.assertEqual(updated.take_profit_2, raw.take_profit_2)
        self.assertEqual(updated.risk_reward, raw.risk_reward)
        self.assertEqual(updated.invalidation, raw.invalidation)
        self.assertEqual(updated.management_plan, {"plan": "ORIGINAL"})
        self.assertEqual(
            updated.market_story["trigger"]["invalidation_price"],
            90.0,
        )
        self.assertEqual(row_count, 1)

        self.assertTrue(
            self.repository.invalidate_preflight_plan(
                created,
                "2026-08-20T00:20:00+00:00",
            )
        )
        replay_ts = next_ts + 900_000
        replayed_reentry = replace(
            reentry,
            data_timestamp=replay_ts,
            closed_candle_ts=replay_ts,
            market_metrics={
                **reentry.market_metrics,
                "core_timestamp": replay_ts,
            },
        )
        replay_output = self.repository.reconcile(
            [replayed_reentry],
            [state_fixture(replayed_reentry, replay_ts)],
            "2026-08-20T00:30:00+00:00",
            "SHORT",
        )

        self.assertEqual(replay_output, [])
        self.assertIsNone(self.repository.load_active_signal(raw.inst_id, "SHORT"))

    def test_stale_advance_cannot_resurrect_concurrently_closed_plan(self):
        raw = signal_fixture("CAS-CLOSED-USDT-SWAP")
        created = self.repository.reconcile(
            [raw],
            [state_fixture(raw, raw.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )[0]
        stale_row = self.repository._connection.execute(
            "SELECT * FROM signals WHERE signal_id=?",
            (created.trigger_id,),
        ).fetchone()
        self.assertTrue(
            self.repository.invalidate_preflight_plan(
                created,
                "2026-08-20T00:05:00+00:00",
            )
        )

        next_ts = raw.data_timestamp + 900_000
        stale_result = self.repository._advance_existing(
            created,
            stale_row,
            {
                **raw.market_metrics,
                "core_timestamp": next_ts,
                "core_close": 100.0,
            },
            "2026-08-20T00:15:00+00:00",
        )
        row = self.repository._connection.execute(
            "SELECT status, stage, freshness FROM signals WHERE signal_id=?",
            (created.trigger_id,),
        ).fetchone()

        self.assertFalse(stale_result.actionable)
        self.assertEqual(stale_result.freshness, "INVALIDATED")
        self.assertEqual(row["status"], "CLOSED")
        self.assertEqual(row["stage"], "INVALIDATED")
        self.assertEqual(row["freshness"], "INVALIDATED")

    def test_out_of_order_candidate_cannot_roll_back_any_episode_content(self):
        raw = signal_fixture("ORDERED-USDT-SWAP")
        self.repository.reconcile(
            [raw],
            [state_fixture(raw, raw.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )
        newer_ts = raw.data_timestamp + 900_000
        newer = replace(
            raw,
            score=94.0,
            evidence=["較新的證據"],
            summary="較新的情境",
            market_participation={"state": "STRONG_SUPPORT", "trend": "IMPROVING"},
            execution_quality={"score": 91.0, "label": "HIGH"},
            data_quality={"status": "COMPLETE", "marker": "NEWER"},
            entry_eligibility={"status": "ENTRY_READY", "actionable": True},
            data_timestamp=newer_ts,
            closed_candle_ts=newer_ts,
            market_story={
                "context_marker": "NEWER",
                "trigger": {
                    "event_ts": newer_ts,
                    "trigger_event_key": f"SHORT:LONG:BREAKOUT:{newer_ts}:ZONE-B",
                },
            },
            market_metrics={
                **raw.market_metrics,
                "core_timestamp": newer_ts,
                "context_marker": "NEWER",
            },
        )
        accepted = self.repository.reconcile(
            [newer],
            [state_fixture(newer, newer_ts)],
            "2026-08-20T00:15:00+00:00",
            "SHORT",
        )[0]
        older_ts = raw.data_timestamp + 450_000
        delayed = replace(
            raw,
            score=1.0,
            evidence=["過期證據"],
            summary="過期情境",
            market_participation={"state": "CONFLICT", "trend": "WORSENING"},
            execution_quality={"score": 1.0, "label": "LOW"},
            data_quality={"status": "MISSING", "marker": "OLDER"},
            entry_eligibility={"status": "MISSED_ENTRY", "actionable": False},
            data_timestamp=older_ts,
            closed_candle_ts=older_ts,
            market_story={
                "context_marker": "OLDER",
                "trigger": raw.market_story["trigger"],
            },
            market_metrics={
                **raw.market_metrics,
                "core_timestamp": older_ts,
                "context_marker": "OLDER",
            },
        )

        returned = self.repository.reconcile(
            [delayed],
            [state_fixture(delayed, older_ts)],
            "2026-08-20T00:07:30+00:00",
            "SHORT",
        )[0]
        persisted = self.repository.load_active_signal(raw.inst_id, "SHORT")

        self.assertEqual(returned.trigger_id, accepted.trigger_id)
        self.assertEqual(returned.lifecycle["transition"], "UNCHANGED")
        for signal in (returned, persisted):
            self.assertEqual(signal.score, 94.0)
            self.assertEqual(signal.evidence, ["較新的證據"])
            self.assertEqual(signal.summary, "較新的情境")
            self.assertEqual(signal.market_participation["trend"], "IMPROVING")
            self.assertEqual(signal.execution_quality["score"], 91.0)
            self.assertEqual(signal.data_quality["marker"], "NEWER")
            self.assertEqual(signal.entry_eligibility["status"], "ENTRY_READY")
            self.assertEqual(signal.market_story["context_marker"], "NEWER")
            self.assertEqual(signal.market_metrics["context_marker"], "NEWER")
            self.assertEqual(signal.data_timestamp, newer_ts)

    def test_out_of_order_state_only_update_cannot_invalidate_or_regress_story(self):
        raw = signal_fixture("STATE-ORDER-USDT-SWAP")
        initial_state = replace(
            state_fixture(raw, raw.data_timestamp),
            market_story={"context_marker": "INITIAL"},
        )
        created = self.repository.reconcile(
            [raw],
            [initial_state],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )[0]
        newer_ts = raw.data_timestamp + 900_000
        newer_state = replace(
            state_fixture(raw, newer_ts),
            market_story={"context_marker": "NEWER"},
            market_metrics={
                **raw.market_metrics,
                "core_timestamp": newer_ts,
                "core_high": 101.0,
                "core_low": 99.0,
                "core_close": 100.0,
                "context_marker": "NEWER",
            },
        )
        accepted = self.repository.reconcile(
            [],
            [newer_state],
            "2026-08-20T00:15:00+00:00",
            "SHORT",
        )[0]
        event_count_before = self.repository._connection.execute(
            "SELECT COUNT(*) FROM signal_events WHERE signal_id=?",
            (created.trigger_id,),
        ).fetchone()[0]
        older_ts = raw.data_timestamp + 450_000
        stale_state = replace(
            state_fixture(raw, older_ts, core_high=81.0, core_low=79.0),
            market_story={"context_marker": "OLDER"},
            market_metrics={
                **raw.market_metrics,
                "core_timestamp": older_ts,
                "core_high": 81.0,
                "core_low": 79.0,
                "core_close": 80.0,
                "context_marker": "OLDER",
            },
        )

        returned = self.repository.reconcile(
            [],
            [stale_state],
            "2026-08-20T00:07:30+00:00",
            "SHORT",
        )[0]
        row = self.repository._connection.execute(
            """
            SELECT status, outcome, payload_json FROM signals
            WHERE signal_id=?
            """,
            (created.trigger_id,),
        ).fetchone()
        event_count_after = self.repository._connection.execute(
            "SELECT COUNT(*) FROM signal_events WHERE signal_id=?",
            (created.trigger_id,),
        ).fetchone()[0]
        story = self.repository.load_story(raw.inst_id, "SHORT")

        self.assertEqual(returned.trigger_id, accepted.trigger_id)
        self.assertEqual(returned.market_metrics["context_marker"], "NEWER")
        self.assertEqual(returned.lifecycle["transition"], "UNCHANGED")
        self.assertEqual(row["status"], "ACTIVE")
        self.assertIsNone(row["outcome"])
        self.assertEqual(event_count_after, event_count_before)
        self.assertEqual(story["closed_candle_ts"], newer_ts)
        self.assertEqual(story["market_story"]["context_marker"], "NEWER")

    def test_missing_symbol_state_keeps_read_only_non_actionable_episode(self):
        raw = replace(
            signal_fixture("MISSING-USDT-SWAP"),
            entry_eligibility={"status": "ENTRY_READY", "actionable": True},
            data_quality={"status": "COMPLETE"},
        )
        created = self.repository.reconcile(
            [raw],
            [state_fixture(raw, raw.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )[0]

        output = self.repository.reconcile(
            [],
            [],
            "2026-08-20T00:15:00+00:00",
            "SHORT",
        )
        row = self.repository._connection.execute(
            "SELECT status FROM signals WHERE signal_id=?",
            (created.trigger_id,),
        ).fetchone()

        self.assertEqual(len(output), 1)
        self.assertEqual(output[0].trigger_id, created.trigger_id)
        self.assertEqual(output[0].freshness, "DATA_UNAVAILABLE")
        self.assertEqual(output[0].lifecycle["transition"], "DATA_UNAVAILABLE")
        self.assertTrue(output[0].lifecycle["read_only"])
        self.assertEqual(output[0].data_quality["status"], "DATA_UNAVAILABLE")
        self.assertEqual(output[0].entry_eligibility["status"], "DATA_UNAVAILABLE")
        self.assertFalse(output[0].entry_eligibility["actionable"])
        self.assertIsNone(output[0].market_metrics["last_price"])
        self.assertFalse(output[0].actionable)
        self.assertEqual(row["status"], "ACTIVE")

    def test_single_instrument_reconcile_keeps_id_and_never_touches_other_coins(self):
        first_raw = signal_fixture("SINGLE-A-USDT-SWAP")
        other_raw = signal_fixture("SINGLE-B-USDT-SWAP")
        created = self.repository.reconcile(
            [first_raw, other_raw],
            [
                state_fixture(first_raw, first_raw.data_timestamp),
                state_fixture(other_raw, other_raw.data_timestamp),
            ],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )
        by_inst = {item.inst_id: item for item in created}
        first_id = by_inst[first_raw.inst_id].trigger_id
        other_id = by_inst[other_raw.inst_id].trigger_id
        other_before = self.repository._connection.execute(
            "SELECT * FROM signals WHERE signal_id=?",
            (other_id,),
        ).fetchone()

        for step in (1, 2):
            core_ts = first_raw.data_timestamp + step * 900_000
            refreshed = replace(
                first_raw,
                score=86.0 + step,
                data_timestamp=core_ts,
                closed_candle_ts=core_ts,
                market_metrics={
                    **first_raw.market_metrics,
                    "core_timestamp": core_ts,
                },
            )
            current = self.repository.reconcile_instrument(
                refreshed,
                state_fixture(refreshed, core_ts),
                f"2026-08-20T00:{step * 15:02d}:00+00:00",
                "SHORT",
            )
            self.assertEqual(current.trigger_id, first_id)
            self.assertEqual(current.score, 86.0 + step)

        other_after = self.repository._connection.execute(
            "SELECT * FROM signals WHERE signal_id=?",
            (other_id,),
        ).fetchone()
        self.assertEqual(dict(other_after), dict(other_before))

        state_only_ts = first_raw.data_timestamp + 3 * 900_000
        state_only = state_fixture(first_raw, state_only_ts)
        state_only_result = self.repository.reconcile_instrument(
            None,
            state_only,
            "2026-08-20T00:45:00+00:00",
            "SHORT",
        )
        self.assertEqual(state_only_result.trigger_id, first_id)

        missing_ts = first_raw.data_timestamp + 4 * 900_000
        missing_state_view = self.repository.reconcile_instrument(
            replace(
                first_raw,
                data_timestamp=missing_ts,
                closed_candle_ts=missing_ts,
                market_metrics={
                    **first_raw.market_metrics,
                    "core_timestamp": missing_ts,
                },
            ),
            None,
            "2026-08-20T01:00:00+00:00",
            "SHORT",
        )

        self.assertEqual(missing_state_view.trigger_id, first_id)
        self.assertEqual(missing_state_view.freshness, "DATA_UNAVAILABLE")
        self.assertFalse(missing_state_view.actionable)
        self.assertEqual(
            self.repository.load_active_signal(first_raw.inst_id, "SHORT").score,
            88.0,
        )
        self.assertEqual(
            self.repository.load_active_signal(other_raw.inst_id, "SHORT").trigger_id,
            other_id,
        )

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

    def test_performance_research_is_read_only_and_hides_tiny_group_rates(self):
        base = signal_fixture("RESEARCH-USDT-SWAP")
        raw = replace(
            base,
            market_story={
                **base.market_story,
                "context": {
                    "sessions": {"active": ["LONDON"]},
                    "market_driver": {"key": "INDEPENDENT"},
                    "counter_higher_timeframe": True,
                },
            },
        )
        self.repository.reconcile(
            [raw],
            [state_fixture(raw, raw.data_timestamp)],
            "2026-08-20T00:00:00+00:00",
            "SHORT",
        )
        next_ts = raw.data_timestamp + 900_000
        completed = replace(
            raw,
            data_timestamp=next_ts,
            closed_candle_ts=next_ts,
            market_metrics={
                **raw.market_metrics,
                "core_timestamp": next_ts,
                "core_high": 121.0,
                "core_low": 99.0,
            },
        )
        self.repository.reconcile(
            [completed],
            [state_fixture(completed, next_ts, 121.0, 99.0)],
            "2026-08-20T00:15:00+00:00",
            "SHORT",
        )

        research = self.repository.performance()["research"]

        self.assertEqual(research["sample_size"], 1)
        self.assertEqual(research["avg_mfe_r"], 2.1)
        self.assertEqual(research["avg_mae_r"], 0.1)
        self.assertTrue(research["read_only"])
        self.assertFalse(research["auto_tuning"])
        self.assertEqual(research["by_session"]["LONDON"], {"sample_size": 1})
        self.assertEqual(
            research["by_market_driver"]["INDEPENDENT"],
            {"sample_size": 1},
        )
        self.assertEqual(
            research["by_horizon_trigger"]["SHORT:BREAKOUT"],
            {"sample_size": 1},
        )
        self.assertEqual(
            research["by_higher_timeframe_alignment"][
                "COUNTER_HIGHER_TIMEFRAME"
            ],
            {"sample_size": 1},
        )
        self.assertNotIn("win_rate_pct", research["by_session"]["LONDON"])

    def test_excursion_profile_uses_price_percent_and_winner_mae_only(self):
        for index in range(6):
            event_ts = 1_710_000_000_000 + index * 900_000
            raw = replace(
                signal_fixture("LEARN-USDT-SWAP", event_ts=event_ts),
                entry_low="100",
                entry_high="100",
                stop_loss="99",
                take_profit_1="102",
                take_profit_2="103",
                market_story={
                    "trigger": {
                        "event_ts": event_ts,
                        "trigger_event_key": (
                            f"SHORT:LONG:BREAKOUT:{event_ts}:LEARN-ZONE"
                        ),
                    }
                },
            )
            created = self.repository.reconcile(
                [raw],
                [state_fixture(raw, event_ts)],
                f"2026-08-20T00:{index:02d}:00+00:00",
                "SHORT",
            )[0]
            final_r = 2.0 if index < 5 else -1.0
            mae_r = 0.4 if index < 5 else 4.0
            mfe_r = 2.5 if index < 5 else 0.2
            self.repository._connection.execute(
                """
                UPDATE signals
                SET status='CLOSED', outcome=?, final_r=?, mfe_r=?, mae_r=?,
                    tp_sl_order='TP_FIRST', closed_at=updated_at
                WHERE signal_id=?
                """,
                (
                    "TP_FIRST" if final_r > 0 else "SL_FIRST",
                    final_r,
                    mfe_r,
                    mae_r,
                    created.trigger_id,
                ),
            )
        self.repository._connection.commit()

        profile = self.repository.excursion_profile(
            "LEARN-USDT-SWAP",
            "SHORT",
            "LONG",
            "BREAKOUT",
        )

        self.assertTrue(profile["enabled"])
        self.assertEqual(profile["stop"]["sample_size"], 5)
        self.assertEqual(profile["target"]["sample_size"], 6)
        self.assertAlmostEqual(profile["stop"]["mae_p80_pct"], 0.4)
        self.assertAlmostEqual(profile["target"]["mfe_p60_pct"], 2.5)
        self.assertTrue(profile["stop"]["profitable_episodes_only"])

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
            """
            SELECT stage, freshness, outcome, final_r, tp_sl_order, status,
                   payload_json
            FROM signals WHERE inst_id=?
            """,
            (raw.inst_id,),
        ).fetchone()
        stored = Signal.from_dict(json.loads(row["payload_json"]))

        self.assertEqual(active_output, [])
        self.assertEqual(row["outcome"], "TP1_FIRST")
        self.assertEqual(row["final_r"], 2.0)
        self.assertEqual(row["tp_sl_order"], "TP_FIRST")
        self.assertEqual(row["status"], "CLOSED")
        self.assertEqual(row["stage"], "COMPLETED")
        self.assertEqual(row["freshness"], "COMPLETED")
        self.assertEqual(stored.signal_stage, "COMPLETED")
        self.assertEqual(stored.lifecycle["status"], "COMPLETED")

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

    def test_microstructure_raw_history_is_bounded_monotonic_and_durable(self):
        inst_id = "FLOW-USDT-SWAP"
        for timestamp in range(1, 11):
            self.repository.save_microstructure(
                MarketContext(
                    inst_id,
                    1_000_000.0 + timestamp,
                    0.0001 * timestamp,
                    0.01 * timestamp,
                    0.50 + timestamp / 100.0,
                    timestamp,
                    bid_depth_usd=100_000.0 + timestamp,
                    ask_depth_usd=90_000.0 + timestamp,
                    best_bid=100.0 + timestamp,
                    best_ask=100.2 + timestamp,
                ),
                f"2026-08-20T00:00:{timestamp:02d}+00:00",
            )

        self.repository.save_microstructure(
            MarketContext(
                inst_id,
                9_999_999.0,
                9.0,
                9.0,
                0.99,
                10,
                bid_depth_usd=9.0,
                ask_depth_usd=9.0,
                best_bid=9.0,
                best_ask=9.1,
            ),
            "2026-08-20T00:01:00+00:00",
        )
        self.repository.save_microstructure(
            MarketContext(
                inst_id,
                8_888_888.0,
                8.0,
                8.0,
                0.98,
                5,
                bid_depth_usd=8.0,
                ask_depth_usd=8.0,
                best_bid=8.0,
                best_ask=8.1,
            ),
            "2026-08-20T00:01:01+00:00",
        )
        loaded = self.repository.load_microstructure(inst_id)
        history = loaded["raw_history"]

        self.assertEqual(len(history), 8)
        self.assertEqual(
            [item["timestamp_ms"] for item in history],
            list(range(3, 11)),
        )
        self.assertEqual(history[-1]["open_interest_usd"], 1_000_010.0)
        self.assertAlmostEqual(history[-1]["mid_price"], 110.1)
        self.assertNotIn("direction", history[-1])
        self.assertNotIn("signal", history[-1])

        database_path = Path(self.temp_dir.name) / "radar-state.sqlite3"
        self.repository.close()
        self.repository = SignalRepository(database_path)

        self.assertEqual(
            self.repository.load_microstructure(inst_id)["raw_history"],
            history,
        )


if __name__ == "__main__":
    unittest.main()
