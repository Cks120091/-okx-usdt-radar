from __future__ import annotations

import json
import math
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import MarketContext, MarketState, Signal


ACTIVE_STAGES = {
    "EARLY_SIGNAL",
    "CONFIRMED",
    "TRENDING",
    "REENTRY",
    "EXTENDED",
    "NO_FOLLOW_THROUGH",
}


class SignalRepository:
    """SQLite state memory, duplicate lock, lifecycle and outcome ledger."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        early_signal_max_age_bars: int = 2,
    ):
        self.path = str(path)
        self.early_signal_max_age_bars = max(
            1,
            min(int(early_signal_max_age_bars), 5),
        )
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        # FULL scans reconcile both horizons as one durable unit. Nested write
        # helpers use this depth to avoid committing one horizon early.
        self._transaction_depth = 0
        if self.path != ":memory:":
            self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    signal_id TEXT PRIMARY KEY,
                    event_key TEXT NOT NULL,
                    inst_id TEXT NOT NULL,
                    horizon TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    event_kind TEXT NOT NULL,
                    triggered_at TEXT NOT NULL,
                    event_ts INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT,
                    stage TEXT NOT NULL,
                    freshness TEXT NOT NULL,
                    status TEXT NOT NULL,
                    trigger_price REAL NOT NULL,
                    stop_price REAL NOT NULL,
                    tp1_price REAL NOT NULL,
                    tp2_price REAL NOT NULL,
                    risk_reward REAL NOT NULL,
                    participation_state TEXT,
                    execution_quality REAL,
                    mfe_r REAL NOT NULL DEFAULT 0,
                    mae_r REAL NOT NULL DEFAULT 0,
                    outcome TEXT,
                    final_r REAL,
                    tp_sl_order TEXT,
                    payload_json TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    feature_schema_version TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_signals_active
                    ON signals(inst_id, horizon, direction, status);
                CREATE INDEX IF NOT EXISTS idx_signals_stats
                    ON signals(strategy_version, horizon, trigger_type, outcome);

                CREATE TABLE IF NOT EXISTS signal_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id TEXT NOT NULL,
                    event_at TEXT NOT NULL,
                    from_stage TEXT,
                    to_stage TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(signal_id) REFERENCES signals(signal_id)
                );

                CREATE TABLE IF NOT EXISTS story_state (
                    inst_id TEXT NOT NULL,
                    horizon TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(inst_id, horizon)
                );

                CREATE TABLE IF NOT EXISTS microstructure_state (
                    inst_id TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scan_runs (
                    scan_id TEXT PRIMARY KEY,
                    started_at TEXT,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    target_count INTEGER NOT NULL,
                    analyzable_count INTEGER NOT NULL,
                    signal_count INTEGER NOT NULL,
                    duration_seconds REAL NOT NULL,
                    metrics_json TEXT NOT NULL
                );
                """
            )
            self._repair_active_episode_conflicts()
            self._connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_signals_active_episode
                ON signals(inst_id, horizon)
                WHERE status='ACTIVE'
                """
            )

    def _repair_active_episode_conflicts(self) -> None:
        """Retire legacy duplicate ACTIVE rows before adding the invariant.

        Older versions allowed one active row per direction. Preserve the most
        recently updated row as the formal main episode and retain every other
        row as terminal history instead of deleting it.
        """

        conflicts = self._connection.execute(
            """
            SELECT inst_id, horizon
            FROM signals
            WHERE status='ACTIVE'
            GROUP BY inst_id, horizon
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        retired_at = datetime.now(timezone.utc).isoformat()
        for conflict in conflicts:
            rows = self._connection.execute(
                """
                SELECT * FROM signals
                WHERE inst_id=? AND horizon=? AND status='ACTIVE'
                ORDER BY updated_at DESC, signal_id DESC
                """,
                (conflict["inst_id"], conflict["horizon"]),
            ).fetchall()
            for row in rows[1:]:
                signal = Signal.from_dict(json.loads(row["payload_json"]))
                lifecycle = dict(signal.lifecycle)
                lifecycle.update(
                    {
                        "last_seen_at": retired_at,
                        "previous_stage": row["stage"],
                        "current_stage": "INVALIDATED",
                        "transition": "REPOSITORY_ACTIVE_CONFLICT_RETIRED",
                        "terminal": True,
                        "duplicate_locked": True,
                    }
                )
                retired = replace(
                    signal,
                    signal_stage="INVALIDATED",
                    freshness="INVALIDATED",
                    lifecycle=lifecycle,
                    actionable=False,
                )
                self._connection.execute(
                    """
                    UPDATE signals SET
                        updated_at=?, closed_at=?, stage='INVALIDATED',
                        freshness='INVALIDATED', status='CLOSED',
                        outcome='REPOSITORY_ACTIVE_CONFLICT', final_r=NULL,
                        tp_sl_order='REPOSITORY_INVARIANT', payload_json=?
                    WHERE signal_id=? AND status='ACTIVE'
                    """,
                    (
                        retired_at,
                        retired_at,
                        json.dumps(_signal_payload(retired), ensure_ascii=False),
                        row["signal_id"],
                    ),
                )
                self._connection.execute(
                    """
                    INSERT INTO signal_events(
                        signal_id, event_at, from_stage, to_stage,
                        event_type, payload_json
                    ) VALUES(?, ?, ?, 'INVALIDATED', ?, ?)
                    """,
                    (
                        row["signal_id"],
                        retired_at,
                        row["stage"],
                        "REPOSITORY_ACTIVE_CONFLICT_RETIRED",
                        json.dumps(lifecycle, ensure_ascii=False),
                    ),
                )

    def load_story(self, inst_id: str, horizon: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM story_state WHERE inst_id=? AND horizon=?",
                (inst_id, horizon),
            ).fetchone()
            payload = json.loads(row["payload_json"]) if row else None
            active = self._connection.execute(
                """
                SELECT direction, stage, stop_price, signal_id, payload_json
                FROM signals
                WHERE inst_id=? AND horizon=? AND status='ACTIVE'
                ORDER BY updated_at DESC LIMIT 1
                """,
                (inst_id, horizon),
            ).fetchone()
        if payload is None and active is None:
            return None
        output = dict(payload or {})
        if active is not None:
            active_payload = json.loads(active["payload_json"])
            active_lifecycle = active_payload.get("lifecycle", {})
            output.update(
                {
                    "active_trigger_direction": active["direction"],
                    "active_stage": active["stage"],
                    "active_signal_id": active["signal_id"],
                    "invalidation_price": active["stop_price"],
                    "last_evaluated_core_ts": int(
                        active_lifecycle.get("last_evaluated_core_ts") or 0
                    ),
                    "invalidated": False,
                }
            )
        return output

    def load_active_signal(self, inst_id: str, horizon: str) -> Signal | None:
        """Return the one persisted active episode for an instrument/horizon.

        Callers receive a detached model value, not a mutable repository row.
        Direction is deliberately not part of this lookup: one horizon may
        have only one formal main direction at a time.
        """

        row = self._active_episode_row(inst_id, horizon)
        if row is None:
            return None
        return Signal.from_dict(json.loads(row["payload_json"]))

    def save_story(self, state: MarketState, updated_at: str) -> None:
        payload = {
            "direction_state": state.direction_state,
            "direction": state.direction,
            "stage": state.status,
            "freshness": state.freshness,
            "trigger": state.trigger,
            "market_story": state.market_story,
            "conflicts": state.conflicts,
            "market_participation": state.market_participation,
            "closed_candle_ts": state.closed_candle_ts,
        }
        with self._write_scope():
            current = self._connection.execute(
                "SELECT payload_json FROM story_state WHERE inst_id=? AND horizon=?",
                (state.inst_id, state.radar_horizon),
            ).fetchone()
            if current is not None:
                current_payload = json.loads(current["payload_json"])
                current_core_ts = int(
                    current_payload.get("closed_candle_ts") or 0
                )
                candidate_core_ts = int(
                    state.market_metrics.get("core_timestamp")
                    or state.closed_candle_ts
                    or 0
                )
                if current_core_ts > 0 and candidate_core_ts <= current_core_ts:
                    return
            self._connection.execute(
                """
                INSERT INTO story_state(inst_id, horizon, updated_at, payload_json)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(inst_id, horizon) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
                """,
                (
                    state.inst_id,
                    state.radar_horizon,
                    updated_at,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def reconcile(
        self,
        raw_signals: list[Signal],
        market_states: list[MarketState],
        completed_at: str,
        horizon: str,
    ) -> list[Signal]:
        state_map: dict[str, MarketState] = {}
        for item in market_states:
            if item.radar_horizon != horizon:
                continue
            existing_state = state_map.get(item.inst_id)
            if (
                existing_state is None
                or _market_state_core_timestamp(item)
                > _market_state_core_timestamp(existing_state)
            ):
                state_map[item.inst_id] = item
        output_by_id: dict[str, Signal] = {}
        seen_signal_ids: set[str] = set()
        raw_by_slot: dict[tuple[str, str], list[tuple[int, Signal]]] = {}
        slot_order: list[tuple[str, str]] = []
        for index, item in enumerate(raw_signals):
            if item.radar_horizon != horizon:
                continue
            slot = (item.inst_id, item.radar_horizon)
            if slot not in raw_by_slot:
                raw_by_slot[slot] = []
                slot_order.append(slot)
            raw_by_slot[slot].append((index, item))
        ordered_raw = [
            item
            for slot in slot_order
            for _index, item in sorted(
                raw_by_slot[slot],
                key=lambda pair: (_signal_core_timestamp(pair[1]), pair[0]),
            )
        ]

        for raw in ordered_raw:
            current = self._reconcile_raw_signal(raw, completed_at)
            seen_signal_ids.add(current.trigger_id)
            if current.freshness not in ("COMPLETED", "INVALIDATED"):
                # Multiple raw candidates may resolve to the same active
                # episode. Keep its last reconciled view once, without card
                # duplication or intra-scan reordering.
                if (
                    current.lifecycle.get("transition")
                    == "TERMINAL_REPLAY_IGNORED"
                ):
                    output_by_id.setdefault(current.trigger_id, current)
                else:
                    output_by_id[current.trigger_id] = current
            else:
                output_by_id.pop(current.trigger_id, None)

        for row in self._active_rows(horizon):
            if row["signal_id"] in seen_signal_ids:
                continue
            state = state_map.get(row["inst_id"])
            stored = Signal.from_dict(json.loads(row["payload_json"]))
            if state is None:
                # Missing this symbol in the new scan must not erase its
                # episode. Return a read-only last-known plan with an explicit
                # hard data gate; do not mutate the persisted active plan.
                unavailable = self._data_unavailable_projection(stored)
                output_by_id[unavailable.trigger_id] = unavailable
                seen_signal_ids.add(unavailable.trigger_id)
                continue
            updated = self._advance_existing(stored, row, state.market_metrics, completed_at)
            if updated.freshness not in ("COMPLETED", "INVALIDATED"):
                output_by_id[updated.trigger_id] = updated
                seen_signal_ids.add(updated.trigger_id)
            else:
                output_by_id.pop(updated.trigger_id, None)

        for state in market_states:
            if state.radar_horizon == horizon:
                self.save_story(state, completed_at)
        return list(output_by_id.values())

    def reconcile_batch(
        self,
        batches: dict[str, tuple[list[Signal], list[MarketState]]],
        completed_at: str,
    ) -> dict[str, list[Signal]]:
        """Atomically reconcile multiple horizons from one market scan.

        A FULL scan must not commit a SHORT lifecycle update when LONG
        reconciliation fails (or vice versa). Partial scans and on-demand
        single-instrument scans continue to use their independent APIs.
        """

        normalized: list[tuple[str, list[Signal], list[MarketState]]] = []
        seen_horizons: set[str] = set()
        for horizon, payload in batches.items():
            normalized_horizon = str(horizon or "").strip().upper()
            if normalized_horizon not in {"SHORT", "LONG"}:
                raise ValueError("batch horizon must be SHORT or LONG")
            if normalized_horizon in seen_horizons:
                raise ValueError("batch horizons must be unique")
            try:
                raw_signals, market_states = payload
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "batch payload must contain raw signals and market states"
                ) from exc
            seen_horizons.add(normalized_horizon)
            normalized.append(
                (normalized_horizon, list(raw_signals), list(market_states))
            )

        if not normalized:
            return {}

        with self._lock:
            self._transaction_depth += 1
            try:
                with self._connection:
                    return {
                        horizon: self.reconcile(
                            raw_signals,
                            market_states,
                            completed_at,
                            horizon,
                        )
                        for horizon, raw_signals, market_states in normalized
                    }
            finally:
                self._transaction_depth -= 1

    def reconcile_instrument(
        self,
        raw_signal: Signal | None,
        state: MarketState | None,
        updated_at: str,
        horizon: str,
    ) -> Signal | None:
        """Reconcile exactly one instrument without touching the universe.

        This is the durable path for on-demand single-coin scans. It shares the
        same episode/tombstone invariants as a full scan but never interprets
        other active instruments as missing merely because they were not part
        of this request.
        """

        candidates = [
            item.inst_id
            for item in (raw_signal, state)
            if item is not None
        ]
        if not candidates:
            return None
        inst_id = candidates[0]
        if any(candidate != inst_id for candidate in candidates[1:]):
            raise ValueError("raw signal and state must target one instrument")
        if raw_signal is not None and raw_signal.radar_horizon != horizon:
            raise ValueError("raw signal horizon does not match reconciliation")
        if state is not None and state.radar_horizon != horizon:
            raise ValueError("market state horizon does not match reconciliation")

        if state is None:
            row = self._active_episode_row(inst_id, horizon)
            if row is None:
                return None
            stored = Signal.from_dict(json.loads(row["payload_json"]))
            return self._data_unavailable_projection(stored)

        if raw_signal is not None:
            current = self._reconcile_raw_signal(raw_signal, updated_at)
        else:
            row = self._active_episode_row(inst_id, horizon)
            if row is None:
                current = None
            else:
                stored = Signal.from_dict(json.loads(row["payload_json"]))
                current = self._advance_existing(
                    stored,
                    row,
                    state.market_metrics,
                    updated_at,
                )

        if state is not None:
            self.save_story(state, updated_at)
        if current is None or current.freshness in (
            "COMPLETED",
            "INVALIDATED",
        ):
            return None
        return current

    def invalidate_preflight_plan(
        self,
        signal: Signal,
        observed_at: str,
    ) -> bool:
        """Close one exact plan after live preflight observed its stop crossed.

        This deliberately leaves ``final_r`` empty because a ticker snapshot
        proves that the stop was crossed, but cannot prove whether TP1 was
        touched first between two closed-candle evaluations.
        """

        signal_id = str(signal.trigger_id or "").strip()
        if not signal_id:
            return False
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM signals WHERE signal_id=?",
                (signal_id,),
            ).fetchone()
        if row is None or row["status"] != "ACTIVE":
            return False

        persisted = Signal.from_dict(json.loads(row["payload_json"]))
        lifecycle = dict(persisted.lifecycle)
        lifecycle.update(
            {
                "last_seen_at": observed_at,
                "previous_stage": row["stage"],
                "current_stage": "INVALIDATED",
                "transition": "PREFLIGHT_PLAN_INVALIDATED",
                "outcome": "PREFLIGHT_STOP_CROSSED",
                "tp_sl_order": "UNKNOWN_FROM_LIVE_TICKER",
            }
        )
        updated = replace(
            persisted,
            signal_stage="INVALIDATED",
            freshness="INVALIDATED",
            lifecycle=lifecycle,
            actionable=False,
        )
        with self._write_scope():
            cursor = self._connection.execute(
                """
                UPDATE signals SET
                    updated_at=?, closed_at=?, stage='INVALIDATED',
                    freshness='INVALIDATED', status='CLOSED',
                    outcome='PREFLIGHT_STOP_CROSSED', final_r=NULL,
                    tp_sl_order='UNKNOWN_FROM_LIVE_TICKER', payload_json=?
                WHERE signal_id=? AND status='ACTIVE' AND payload_json=?
                """,
                (
                    observed_at,
                    observed_at,
                    json.dumps(_signal_payload(updated), ensure_ascii=False),
                    signal_id,
                    row["payload_json"],
                ),
            )
        if cursor.rowcount <= 0:
            return False
        self._append_event(
            signal_id,
            observed_at,
            str(row["stage"]),
            "INVALIDATED",
            "PREFLIGHT_PLAN_INVALIDATED",
            lifecycle,
        )
        return True

    def _reconcile_raw_signal(self, raw: Signal, completed_at: str) -> Signal:
        # The active lookup and possible insert form one repository operation.
        # RLock keeps concurrent scans in this process from creating two main
        # directions for the same instrument/horizon.
        with self._lock:
            return self._reconcile_raw_signal_locked(raw, completed_at)

    def _reconcile_raw_signal_locked(
        self,
        raw: Signal,
        completed_at: str,
    ) -> Signal:
        logical_event_ts = _signal_trigger_event_timestamp(raw)
        event_key = str(
            raw.market_story.get("trigger", {}).get("trigger_event_key")
            or f"{raw.radar_horizon}:{raw.inst_id}:{raw.direction}:{raw.trigger_type}:{logical_event_ts}"
        )
        active = self._active_episode_row(raw.inst_id, raw.radar_horizon)
        active_claims_event = bool(
            active is not None
            and str(active["direction"]) == raw.direction
            and self._row_claims_event(active, event_key)
        )
        terminal = self._closed_event_row(
            raw.inst_id,
            raw.radar_horizon,
            event_key,
        )
        if (
            terminal is not None
            and active_claims_event
            and terminal["outcome"] == "REPOSITORY_ACTIVE_CONFLICT"
        ):
            # The only safe same-key exception is a duplicate row retired by
            # this repository's migration. A real historical terminal outcome
            # always outranks an accidentally resurrected ACTIVE row.
            terminal = None
        if terminal is not None:
            # Check the tombstone before looking at another active episode. A
            # delayed replay of closed event A must never contaminate active B.
            if active is not None:
                active_signal = Signal.from_dict(
                    json.loads(active["payload_json"])
                )
                unavailable = self._data_unavailable_projection(active_signal)
                lifecycle = dict(unavailable.lifecycle)
                lifecycle["transition"] = "TERMINAL_REPLAY_IGNORED"
                return replace(unavailable, lifecycle=lifecycle)
            return self._terminal_projection(terminal)

        if active is not None:
            existing = Signal.from_dict(json.loads(active["payload_json"]))
            candidate_core_ts = _signal_core_timestamp(raw)
            last_evaluated_core_ts = int(
                existing.lifecycle.get("last_evaluated_core_ts")
                or active["event_ts"]
                or 0
            )
            if (
                last_evaluated_core_ts > 0
                and candidate_core_ts <= last_evaluated_core_ts
            ):
                # Score, evidence, context and execution facts belong to the
                # same closed-core snapshot. A delayed/equal candidate cannot
                # roll any part of the accepted episode backwards.
                return self._unchanged_projection(existing)

            if str(active["direction"]) != raw.direction:
                # Opposite evidence may update the original plan's observed
                # price path, but it is not a second formal direction while
                # the current episode remains ACTIVE.
                return self._advance_existing(
                    existing,
                    active,
                    raw.market_metrics,
                    completed_at,
                )

            # CONTINUATION / REENTRY while the same-direction episode remains
            # active is a lifecycle update, not a new trade plan. _merge_signal
            # intentionally keeps the original Entry / SL / TP and trigger.
            merged = self._merge_signal(existing, raw)
            return self._advance_existing(
                merged,
                active,
                raw.market_metrics,
                completed_at,
            )

        latest_closed = self._latest_closed_episode_row(
            raw.inst_id,
            raw.radar_horizon,
        )
        if (
            latest_closed is not None
            and logical_event_ts
            <= self._closed_episode_watermark(latest_closed)
        ):
            # A renamed/different-key replay is still old if its logical
            # trigger time did not advance beyond the terminal episode.
            return self._terminal_projection(latest_closed)

        signal_id = str(uuid.uuid4())
        triggered_at = _iso_from_millis(raw.market_story.get("trigger", {}).get("event_ts")) or completed_at
        lifecycle = {
            "first_seen_at": completed_at,
            "triggered_at": triggered_at,
            "last_seen_at": completed_at,
            "previous_stage": None,
            "current_stage": raw.signal_stage,
            "transition": "NEW",
            "duplicate_locked": True,
            "event_key": event_key,
            "event_keys": [event_key],
            "last_trigger_event_ts": logical_event_ts,
            "last_evaluated_core_ts": int(
                raw.market_metrics.get("core_timestamp")
                or raw.data_timestamp
                or 0
            ),
        }
        created = replace(
            raw,
            trigger_id=signal_id,
            lifecycle=lifecycle,
            generated_at=completed_at,
        )
        inserted = self._insert_signal(
            created,
            event_key,
            triggered_at,
            completed_at,
        )
        if not inserted:
            # A second repository/process may have committed the main episode
            # after our lookup. Reconcile against that winner instead of
            # surfacing an error or creating a second direction.
            competing = self._active_episode_row(raw.inst_id, raw.radar_horizon)
            if competing is not None:
                return self._reconcile_raw_signal_locked(raw, completed_at)
            raise RuntimeError("signal insert violated a repository invariant")
        self._append_event(
            signal_id,
            completed_at,
            None,
            created.signal_stage,
            "CREATED",
            lifecycle,
        )
        return created

    def _advance_existing(
        self,
        signal: Signal,
        row: sqlite3.Row,
        metrics: dict[str, Any],
        completed_at: str,
    ) -> Signal:
        stage_before = str(row["stage"])
        persisted = Signal.from_dict(json.loads(row["payload_json"]))
        # Age advances with the latest closed core candle, not with the original
        # trigger timestamp (which intentionally remains stable for de-duplication).
        data_ts = int(
            metrics.get("core_timestamp")
            or signal.data_timestamp
            or metrics.get("trigger_event_ts")
            or 0
        )
        event_ts = int(row["event_ts"] or signal.market_story.get("trigger", {}).get("event_ts", 0) or 0)
        previous_evaluated_core_ts = max(
            int(persisted.lifecycle.get("last_evaluated_core_ts") or 0),
            _signal_core_timestamp(persisted),
            int(row["event_ts"] or 0),
        )
        if (
            previous_evaluated_core_ts > 0
            and data_ts <= previous_evaluated_core_ts
        ):
            # State-only carryover goes through this method too. Never let an
            # older/equal state mutate lifecycle, metrics, or invalidate a plan
            # that was already evaluated on a newer closed core candle.
            return self._unchanged_projection(persisted)
        same_core_snapshot = bool(
            data_ts
            and previous_evaluated_core_ts
            and data_ts <= previous_evaluated_core_ts
        )
        interval_ms = 900_000 if signal.radar_horizon == "SHORT" else 14_400_000
        age_bars = max(0, int((data_ts - event_ts) / interval_ms)) if data_ts and event_ts else int(signal.market_story.get("trigger", {}).get("event_age_bars", 0) or 0)
        (
            mfe_r,
            mae_r,
            outcome,
            final_r,
            order,
            last_evaluated_core_ts,
        ) = self._outcome_update(signal, row, metrics)
        close_price = _number(metrics.get("core_close") or metrics.get("last_price"))
        stop = _number(signal.stop_loss)
        invalidated = (
            close_price is not None
            and stop is not None
            and (
                close_price <= stop
                if signal.direction == "LONG"
                else close_price >= stop
            )
        )
        if outcome == "DATA_GAP":
            stage = "INVALIDATED"
            freshness = "INVALIDATED"
            status = "CLOSED"
        elif outcome == "AMBIGUOUS_SAME_BAR":
            stage = "INVALIDATED"
            freshness = "INVALIDATED"
            status = "CLOSED"
        elif outcome in ("TP1_FIRST", "SL_FIRST"):
            stage = "INVALIDATED" if outcome == "SL_FIRST" else "CONFIRMED"
            freshness = "INVALIDATED" if outcome == "SL_FIRST" else "COMPLETED"
            status = "CLOSED"
        elif invalidated:
            outcome, final_r, order = "PRICE_INVALIDATED", -1.0, "SL_FIRST"
            stage, freshness, status = "INVALIDATED", "INVALIDATED", "CLOSED"
        elif same_core_snapshot:
            stage = stage_before
            freshness = str(row["freshness"])
            status = "ACTIVE"
        elif age_bars >= 3 and mfe_r < 0.25:
            stage, freshness, status = "NO_FOLLOW_THROUGH", "NO_FOLLOW_THROUGH", "ACTIVE"
        elif age_bars > (8 if signal.radar_horizon == "SHORT" else 6):
            stage, freshness, status = "EXTENDED", "EXTENDED", "ACTIVE"
        elif (
            signal.signal_stage == "REENTRY"
            and age_bars <= self.early_signal_max_age_bars
        ):
            stage, freshness, status = "REENTRY", "REACTIVATED", "ACTIVE"
        elif signal.signal_stage == "CONFIRMED" or mfe_r >= 0.35:
            stage, freshness, status = "CONFIRMED", "ACTIVE", "ACTIVE"
        elif age_bars <= self.early_signal_max_age_bars:
            stage, freshness, status = "EARLY_SIGNAL", "NEW", "ACTIVE"
        else:
            stage, freshness, status = "TRENDING", "ACTIVE", "ACTIVE"

        lifecycle = dict(signal.lifecycle)
        lifecycle.update(
            {
                "last_seen_at": completed_at,
                "previous_stage": stage_before,
                "current_stage": stage,
                "transition": (
                    "INVALIDATED"
                    if stage == "INVALIDATED"
                    else "UPGRADED"
                    if _stage_rank(stage) > _stage_rank(stage_before)
                    else "DOWNGRADED"
                    if _stage_rank(stage) < _stage_rank(stage_before)
                    else "UNCHANGED"
                ),
                "age_bars": age_bars,
                "mfe_r": round(mfe_r, 3),
                "mae_r": round(mae_r, 3),
                "outcome": outcome,
                "tp_sl_order": order,
                "duplicate_locked": True,
                "last_evaluated_core_ts": last_evaluated_core_ts,
            }
        )
        merged_metrics = dict(signal.market_metrics)
        merged_metrics.update(metrics)
        updated = replace(
            signal,
            signal_stage=stage,
            freshness=freshness,
            lifecycle=lifecycle,
            market_metrics=merged_metrics,
            generated_at=signal.generated_at or completed_at,
            actionable=(
                status == "ACTIVE"
                and stage in ("EARLY_SIGNAL", "CONFIRMED", "REENTRY")
            ),
        )
        with self._write_scope():
            cursor = self._connection.execute(
                """
                UPDATE signals SET
                    updated_at=?, closed_at=?, stage=?, freshness=?, status=?,
                    mfe_r=?, mae_r=?, outcome=?, final_r=?, tp_sl_order=?, payload_json=?
                WHERE signal_id=? AND status='ACTIVE' AND payload_json=?
                """,
                (
                    completed_at,
                    completed_at if status == "CLOSED" else None,
                    stage,
                    freshness,
                    status,
                    mfe_r,
                    mae_r,
                    outcome,
                    final_r,
                    order,
                    json.dumps(_signal_payload(updated), ensure_ascii=False),
                    row["signal_id"],
                    row["payload_json"],
                ),
            )
            if cursor.rowcount <= 0:
                current = self._connection.execute(
                    "SELECT * FROM signals WHERE signal_id=?",
                    (row["signal_id"],),
                ).fetchone()
                if current is None:
                    return self._terminal_projection(row)
                accepted = Signal.from_dict(json.loads(current["payload_json"]))
                if current["status"] != "ACTIVE":
                    return accepted
                return self._unchanged_projection(accepted)
        if stage != stage_before:
            self._append_event(
                row["signal_id"],
                completed_at,
                stage_before,
                stage,
                lifecycle["transition"],
                lifecycle,
            )
        return updated

    @staticmethod
    def _merge_signal(existing: Signal, raw: Signal) -> Signal:
        # Entry, Stop and targets belong to the original event.  Explanatory
        # context and current market facts are refreshed every scan.
        market_story = dict(raw.market_story)
        refreshed_trigger = dict(market_story.get("trigger", {}))
        original_trigger = existing.market_story.get("trigger", {})
        for key in (
            "event_ts",
            "event_price",
            "event_atr",
            "trigger_event_key",
            "zone_key",
            "entry_reference_price",
            "entry_low",
            "entry_high",
            "invalidation_price",
            "stop_price",
            "stop_loss",
            "tp1_price",
            "tp2_price",
            "take_profit_1",
            "take_profit_2",
        ):
            if original_trigger.get(key) not in (None, ""):
                refreshed_trigger[key] = original_trigger[key]
        if refreshed_trigger:
            market_story["trigger"] = refreshed_trigger
        lifecycle = dict(existing.lifecycle)
        event_keys = [
            str(value)
            for value in lifecycle.get("event_keys", [])
            if str(value).strip()
        ]
        original_event_key = str(lifecycle.get("event_key") or "").strip()
        candidate_event_key = str(
            raw.market_story.get("trigger", {}).get("trigger_event_key") or ""
        ).strip()
        for value in (original_event_key, candidate_event_key):
            if value and value not in event_keys:
                event_keys.append(value)
        if event_keys:
            lifecycle["event_keys"] = event_keys
        lifecycle["last_trigger_event_ts"] = max(
            int(lifecycle.get("last_trigger_event_ts") or 0),
            _signal_trigger_event_timestamp(existing),
            _signal_trigger_event_timestamp(raw),
        )
        return replace(
            existing,
            score=raw.score,
            evidence=raw.evidence,
            spread_pct=raw.spread_pct,
            quote_volume_24h=raw.quote_volume_24h,
            closed_candle_ts=raw.closed_candle_ts,
            regime=raw.regime,
            notes=raw.notes,
            factor_scores=raw.factor_scores,
            market_metrics=raw.market_metrics,
            trend_strength_label=raw.trend_strength_label,
            trend_strength_score=raw.trend_strength_score,
            readiness_score=raw.readiness_score,
            evidence_groups=raw.evidence_groups,
            timeframe_states=raw.timeframe_states,
            supporting_evidence=raw.supporting_evidence,
            conflicts=raw.conflicts,
            neutral_evidence=raw.neutral_evidence,
            safety_checks=raw.safety_checks,
            entry_quality=raw.entry_quality,
            summary=raw.summary,
            direction_state=raw.direction_state,
            market_participation=raw.market_participation,
            execution_quality=raw.execution_quality,
            data_quality=raw.data_quality,
            market_story=market_story,
            lifecycle=lifecycle,
            data_timestamp=raw.data_timestamp,
            entry_eligibility=raw.entry_eligibility,
            signal_stage=(
                "REENTRY"
                if raw.signal_stage == "REENTRY"
                else existing.signal_stage
            ),
        )

    @staticmethod
    def _unchanged_projection(signal: Signal) -> Signal:
        lifecycle = dict(signal.lifecycle)
        lifecycle.update(
            {
                "previous_stage": signal.signal_stage,
                "current_stage": signal.signal_stage,
                "transition": "UNCHANGED",
                "duplicate_locked": True,
            }
        )
        return replace(signal, lifecycle=lifecycle)

    @staticmethod
    def _terminal_projection(row: sqlite3.Row) -> Signal:
        signal = Signal.from_dict(json.loads(row["payload_json"]))
        lifecycle = dict(signal.lifecycle)
        lifecycle.update(
            {
                "previous_stage": signal.signal_stage,
                "current_stage": "INVALIDATED",
                "transition": "TERMINAL_EVENT_LOCKED",
                "terminal": True,
                "duplicate_locked": True,
                "event_key": row["event_key"],
            }
        )
        return replace(
            signal,
            signal_stage="INVALIDATED",
            freshness="INVALIDATED",
            lifecycle=lifecycle,
            actionable=False,
        )

    @staticmethod
    def _data_unavailable_projection(signal: Signal) -> Signal:
        lifecycle = dict(signal.lifecycle)
        lifecycle.update(
            {
                "previous_stage": signal.signal_stage,
                "current_stage": signal.signal_stage,
                "transition": "DATA_UNAVAILABLE",
                "read_only": True,
                "duplicate_locked": True,
            }
        )
        data_quality = dict(signal.data_quality)
        data_quality.update(
            {
                "status": "DATA_UNAVAILABLE",
                "core": "UNAVAILABLE",
                "reason": "本輪未取得此幣種最新資料；僅保留舊交易計畫供參考。",
                "read_only": True,
            }
        )
        entry_eligibility = dict(signal.entry_eligibility)
        entry_eligibility.update(
            {
                "status": "DATA_UNAVAILABLE",
                "label": "資料不足｜禁止進場",
                "reason": "本輪沒有可驗證的最新資料，舊計畫只讀保留。",
                "actionable": False,
            }
        )
        market_metrics = dict(signal.market_metrics)
        # The previous price remains part of history, but it must not be
        # reusable as a live execution input by downstream eligibility code.
        market_metrics["last_price"] = None
        market_metrics["data_status"] = "DATA_UNAVAILABLE"
        return replace(
            signal,
            freshness="DATA_UNAVAILABLE",
            lifecycle=lifecycle,
            actionable=False,
            data_quality=data_quality,
            entry_eligibility=entry_eligibility,
            market_metrics=market_metrics,
        )

    def _outcome_update(
        self,
        signal: Signal,
        row: sqlite3.Row,
        metrics: dict[str, Any],
    ) -> tuple[float, float, str | None, float | None, str | None, int]:
        last_evaluated = int(
            signal.lifecycle.get("last_evaluated_core_ts")
            or row["event_ts"]
            or 0
        )
        entry_low = _number(signal.entry_low)
        entry_high = _number(signal.entry_high)
        stop = _number(signal.stop_loss)
        tp1 = _number(signal.take_profit_1)
        if None in (entry_low, entry_high, stop, tp1):
            return (
                float(row["mfe_r"]),
                float(row["mae_r"]),
                row["outcome"],
                row["final_r"],
                row["tp_sl_order"],
                last_evaluated,
            )
        entry = (float(entry_low) + float(entry_high)) / 2.0
        risk = abs(entry - float(stop))
        if risk <= 0:
            return (
                float(row["mfe_r"]),
                float(row["mae_r"]),
                row["outcome"],
                row["final_r"],
                row["tp_sl_order"],
                last_evaluated,
            )
        bars = _closed_core_path(metrics, last_evaluated)
        mfe_r = float(row["mfe_r"])
        mae_r = float(row["mae_r"])
        outcome = row["outcome"]
        final_r = row["final_r"]
        order = row["tp_sl_order"]
        interval_ms = 900_000 if signal.radar_horizon == "SHORT" else 14_400_000
        if (
            bars
            and last_evaluated > 0
            and bars[0][0] - last_evaluated > interval_ms * 1.5
        ):
            # A missing core-candle interval makes TP/SL order unknowable. Close
            # the tracked event without a return sample instead of inventing an
            # outcome from the first candle that happens to be available again.
            return (
                mfe_r,
                mae_r,
                "DATA_GAP",
                None,
                "DATA_GAP",
                max(last_evaluated, bars[-1][0]),
            )
        for ts, high, low, _close in bars:
            last_evaluated = max(last_evaluated, ts)
            favorable = (
                (high - entry) / risk
                if signal.direction == "LONG"
                else (entry - low) / risk
            )
            adverse = (
                (entry - low) / risk
                if signal.direction == "LONG"
                else (high - entry) / risk
            )
            mfe_r = max(mfe_r, favorable)
            mae_r = max(mae_r, adverse)
            tp_hit = (
                high >= float(tp1)
                if signal.direction == "LONG"
                else low <= float(tp1)
            )
            sl_hit = (
                low <= float(stop)
                if signal.direction == "LONG"
                else high >= float(stop)
            )
            if tp_hit and sl_hit:
                outcome, final_r, order = (
                    "AMBIGUOUS_SAME_BAR",
                    None,
                    "AMBIGUOUS_SAME_BAR",
                )
                break
            if tp_hit:
                outcome, final_r, order = (
                    "TP1_FIRST",
                    float(signal.risk_reward),
                    "TP_FIRST",
                )
                break
            if sl_hit:
                outcome, final_r, order = "SL_FIRST", -1.0, "SL_FIRST"
                break
        return mfe_r, mae_r, outcome, final_r, order, last_evaluated

    def _insert_signal(
        self,
        signal: Signal,
        event_key: str,
        triggered_at: str,
        updated_at: str,
    ) -> bool:
        entry_low = _number(signal.entry_low) or 0.0
        entry_high = _number(signal.entry_high) or 0.0
        entry = (entry_low + entry_high) / 2.0
        participation = signal.market_participation.get("state")
        quality = _number(signal.execution_quality.get("score"))
        try:
            with self._write_scope():
                self._connection.execute(
                    """
                    INSERT INTO signals(
                        signal_id, event_key, inst_id, horizon, direction, trigger_type,
                        event_kind, triggered_at, event_ts, updated_at, stage, freshness,
                        status, trigger_price, stop_price, tp1_price, tp2_price,
                        risk_reward, participation_state, execution_quality, payload_json,
                        strategy_version, feature_schema_version
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        signal.trigger_id,
                        event_key,
                        signal.inst_id,
                        signal.radar_horizon,
                        signal.direction,
                        signal.trigger_type,
                        "REENTRY" if signal.signal_stage == "REENTRY" else "INITIAL",
                        triggered_at,
                        int(signal.market_story.get("trigger", {}).get("event_ts", signal.data_timestamp) or 0),
                        updated_at,
                        signal.signal_stage,
                        signal.freshness,
                        entry,
                        _number(signal.stop_loss) or 0.0,
                        _number(signal.take_profit_1) or 0.0,
                        _number(signal.take_profit_2) or 0.0,
                        signal.risk_reward,
                        participation,
                        quality,
                        json.dumps(_signal_payload(signal), ensure_ascii=False),
                        signal.strategy_version,
                        signal.feature_schema_version,
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def _append_event(
        self,
        signal_id: str,
        event_at: str,
        from_stage: str | None,
        to_stage: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        with self._write_scope():
            self._connection.execute(
                """
                INSERT INTO signal_events(
                    signal_id, event_at, from_stage, to_stage, event_type, payload_json
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id,
                    event_at,
                    from_stage,
                    to_stage,
                    event_type,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def _close_row(
        self,
        signal_id: str,
        closed_at: str,
        outcome: str,
        final_r: float | None,
        order: str | None,
    ) -> None:
        with self._write_scope():
            self._connection.execute(
                """
                UPDATE signals SET status='CLOSED', closed_at=?, updated_at=?,
                    outcome=?, final_r=?, tp_sl_order=?
                WHERE signal_id=?
                """,
                (closed_at, closed_at, outcome, final_r, order, signal_id),
            )

    @contextmanager
    def _write_scope(self):
        """Serialize a write and commit unless an atomic batch owns it."""

        with self._lock:
            if self._transaction_depth > 0:
                yield
            else:
                with self._connection:
                    yield

    def _active_row(self, inst_id: str, horizon: str, direction: str) -> sqlite3.Row | None:
        row = self._active_episode_row(inst_id, horizon)
        if row is None or str(row["direction"]) != direction:
            return None
        return row

    def _active_episode_row(self, inst_id: str, horizon: str) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(
                """
                SELECT * FROM signals
                WHERE inst_id=? AND horizon=? AND status='ACTIVE'
                ORDER BY updated_at DESC LIMIT 1
                """,
                (inst_id, horizon),
            ).fetchone()

    def _closed_event_row(
        self,
        inst_id: str,
        horizon: str,
        event_key: str,
    ) -> sqlite3.Row | None:
        with self._lock:
            exact = self._connection.execute(
                """
                SELECT * FROM signals
                WHERE inst_id=? AND horizon=? AND event_key=? AND status='CLOSED'
                ORDER BY
                    CASE WHEN outcome='REPOSITORY_ACTIVE_CONFLICT' THEN 1 ELSE 0 END,
                    updated_at DESC
                LIMIT 1
                """,
                (inst_id, horizon, event_key),
            ).fetchone()
            if exact is not None:
                return exact
            rows = self._connection.execute(
                """
                SELECT * FROM signals
                WHERE inst_id=? AND horizon=? AND status='CLOSED'
                ORDER BY
                    CASE WHEN outcome='REPOSITORY_ACTIVE_CONFLICT' THEN 1 ELSE 0 END,
                    updated_at DESC
                """,
                (inst_id, horizon),
            ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            aliases = payload.get("lifecycle", {}).get("event_keys", [])
            if event_key in {str(value) for value in aliases}:
                return row
        return None

    @staticmethod
    def _row_claims_event(row: sqlite3.Row, event_key: str) -> bool:
        if str(row["event_key"]) == event_key:
            return True
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        aliases = payload.get("lifecycle", {}).get("event_keys", [])
        return event_key in {str(value) for value in aliases}

    def _latest_closed_episode_row(
        self,
        inst_id: str,
        horizon: str,
    ) -> sqlite3.Row | None:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM signals
                WHERE inst_id=? AND horizon=? AND status='CLOSED'
                """,
                (inst_id, horizon),
            ).fetchall()
        return max(
            rows,
            key=self._closed_episode_watermark,
            default=None,
        )

    @staticmethod
    def _closed_episode_watermark(row: sqlite3.Row) -> int:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        lifecycle = payload.get("lifecycle", {})
        trigger = payload.get("market_story", {}).get("trigger", {})
        values = (
            lifecycle.get("last_trigger_event_ts"),
            trigger.get("event_ts"),
            row["event_ts"],
        )
        numeric: list[int] = []
        for value in values:
            try:
                numeric.append(int(value or 0))
            except (TypeError, ValueError):
                continue
        return max(numeric, default=0)

    def _active_rows(self, horizon: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._connection.execute(
                    "SELECT * FROM signals WHERE horizon=? AND status='ACTIVE'",
                    (horizon,),
                ).fetchall()
            )

    def save_microstructure(self, context: MarketContext, updated_at: str) -> None:
        timestamp_ms = _market_context_timestamp(context)
        best_bid = _number(context.best_bid)
        best_ask = _number(context.best_ask)
        mid_price = (
            (best_bid + best_ask) / 2.0
            if best_bid is not None
            and best_ask is not None
            and best_bid > 0
            and best_ask >= best_bid
            else None
        )
        snapshot = {
            "sampled_at": int(context.sampled_at or 0),
            "timestamp_ms": timestamp_ms,
            "mid_price": mid_price,
            "open_interest_usd": context.open_interest_usd,
            "taker_buy_ratio": context.taker_buy_ratio,
            "funding_rate": context.funding_rate,
            "bid_depth_usd": context.bid_depth_usd,
            "ask_depth_usd": context.ask_depth_usd,
            "order_book_imbalance": context.order_book_imbalance,
        }
        payload = {
            "bid_depth_usd": context.bid_depth_usd,
            "ask_depth_usd": context.ask_depth_usd,
            "order_book_imbalance": context.order_book_imbalance,
            "taker_buy_ratio": context.taker_buy_ratio,
            "open_interest_usd": context.open_interest_usd,
            "funding_rate": context.funding_rate,
            "cvd": context.cvd,
            "sampled_at": context.sampled_at,
            "timestamp_ms": timestamp_ms,
            "order_book_ts": context.source_timestamps.get("order_book"),
            "source_timestamps": dict(context.source_timestamps),
            "best_bid": context.best_bid,
            "best_ask": context.best_ask,
        }
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT payload_json FROM microstructure_state WHERE inst_id=?",
                (context.inst_id,),
            ).fetchone()
            previous = json.loads(row["payload_json"]) if row else {}
            history = [
                dict(item)
                for item in previous.get("raw_history", [])
                if isinstance(item, dict)
            ]
            if not history and previous:
                legacy_ts = int(
                    previous.get("timestamp_ms")
                    or previous.get("sampled_at")
                    or 0
                )
                if legacy_ts > 0:
                    legacy_bid = _number(previous.get("best_bid"))
                    legacy_ask = _number(previous.get("best_ask"))
                    history.append(
                        {
                            "sampled_at": int(previous.get("sampled_at") or 0),
                            "timestamp_ms": legacy_ts,
                            "mid_price": (
                                (legacy_bid + legacy_ask) / 2.0
                                if legacy_bid is not None
                                and legacy_ask is not None
                                and legacy_bid > 0
                                and legacy_ask >= legacy_bid
                                else None
                            ),
                            "open_interest_usd": previous.get(
                                "open_interest_usd"
                            ),
                            "taker_buy_ratio": previous.get("taker_buy_ratio"),
                            "funding_rate": previous.get("funding_rate"),
                            "bid_depth_usd": previous.get("bid_depth_usd"),
                            "ask_depth_usd": previous.get("ask_depth_usd"),
                            "order_book_imbalance": previous.get(
                                "order_book_imbalance"
                            ),
                        }
                    )
            latest_ts = max(
                (int(item.get("timestamp_ms") or 0) for item in history),
                default=0,
            )
            if history and timestamp_ms <= latest_ts:
                return
            payload["raw_history"] = [*history, snapshot][-8:]
            self._connection.execute(
                """
                INSERT INTO microstructure_state(inst_id, updated_at, payload_json)
                VALUES(?, ?, ?)
                ON CONFLICT(inst_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
                """,
                (context.inst_id, updated_at, json.dumps(payload, ensure_ascii=False)),
            )

    def load_microstructure(self, inst_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM microstructure_state WHERE inst_id=?",
                (inst_id,),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def record_scan(
        self,
        scan_id: str,
        started_at: str,
        completed_at: str,
        status: str,
        target_count: int,
        analyzable_count: int,
        signal_count: int,
        duration_seconds: float,
        metrics: dict[str, Any],
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO scan_runs(
                    scan_id, started_at, completed_at, status, target_count,
                    analyzable_count, signal_count, duration_seconds, metrics_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_id,
                    started_at,
                    completed_at,
                    status,
                    target_count,
                    analyzable_count,
                    signal_count,
                    duration_seconds,
                    json.dumps(metrics, ensure_ascii=False),
                ),
            )

    def performance(self, strategy_version: str = "V3.4_CONTEXT") -> dict[str, Any]:
        with self._lock:
            rows = list(
                self._connection.execute(
                    """
                    SELECT direction, horizon, trigger_type, participation_state,
                           execution_quality, final_r
                    FROM signals
                    WHERE strategy_version=? AND final_r IS NOT NULL
                    ORDER BY triggered_at
                    """,
                    (strategy_version,),
                ).fetchall()
            )
            research_rows = list(
                self._connection.execute(
                    """
                    SELECT horizon, trigger_type, mfe_r, mae_r, final_r,
                           payload_json
                    FROM signals
                    WHERE strategy_version=? AND status='CLOSED'
                      AND outcome!='REPOSITORY_ACTIVE_CONFLICT'
                    ORDER BY triggered_at
                    """,
                    (strategy_version,),
                ).fetchall()
            )
        records = [dict(row) for row in rows]
        return {
            "strategy_version": strategy_version,
            "available": bool(records),
            "note": (
                "歷史績效來自實際保存的 Radar Signal；交易品質不是勝率。"
                if records
                else "尚無已完成樣本；禁止顯示假勝率。"
            ),
            "overall": _performance_bucket(records),
            "by_direction": _group_performance(records, "direction"),
            "by_horizon": _group_performance(records, "horizon"),
            "by_trigger_type": _group_performance(records, "trigger_type"),
            "by_participation": _group_performance(records, "participation_state"),
            "by_execution_quality": _quality_performance(records),
            "research": _research_performance(research_rows),
        }

    def recent_history(
        self,
        limit: int = 60,
        *,
        horizon: str | None = None,
        max_age_hours: int | None = None,
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent trigger snapshots without changing lifecycle decisions."""

        safe_limit = max(1, min(int(limit), 100))
        conditions: list[str] = []
        parameters: list[Any] = []
        if horizon is not None:
            normalized_horizon = str(horizon).strip().upper()
            if normalized_horizon not in ("SHORT", "LONG"):
                return []
            conditions.append("horizon=?")
            parameters.append(normalized_horizon)
        if max_age_hours is not None:
            reference_time = as_of or datetime.now(timezone.utc)
            if reference_time.tzinfo is None:
                reference_time = reference_time.replace(tzinfo=timezone.utc)
            cutoff = reference_time.astimezone(timezone.utc) - timedelta(
                hours=max(1, int(max_age_hours))
            )
            conditions.append("triggered_at>=?")
            parameters.append(cutoff.isoformat())
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        parameters.append(safe_limit)
        with self._lock:
            rows = list(
                self._connection.execute(
                    f"""
                    SELECT signal_id, inst_id, horizon, direction, trigger_type,
                           triggered_at, updated_at, closed_at, stage, freshness,
                           status, trigger_price, stop_price, tp1_price, tp2_price,
                           risk_reward, execution_quality, mfe_r, mae_r, outcome,
                           final_r
                    FROM signals
                    {where_clause}
                    ORDER BY triggered_at DESC, signal_id DESC
                    LIMIT ?
                    """,
                    parameters,
                ).fetchall()
            )
        return [dict(row) for row in rows]


def classify_microstructure(
    previous: dict[str, Any] | None,
    current: MarketContext,
    direction: str,
    price_change_pct: float | None,
) -> dict[str, Any]:
    """Compare real snapshots; never promote one large displayed order to S/R."""

    if previous is None:
        return {
            "state": "FIRST_SNAPSHOT",
            "reason": "首輪委託簿快照，尚無 persistence／撤單／補單序列",
            "persistence": None,
        }
    previous_ts = int(
        previous.get("order_book_ts")
        or previous.get("source_timestamps", {}).get("order_book", 0)
        or previous.get("sampled_at", 0)
        or 0
    )
    current_ts = int(
        current.source_timestamps.get("order_book", 0)
        or current.sampled_at
        or 0
    )
    if previous_ts and current_ts and current_ts <= previous_ts:
        return {
            "state": "STALE_SNAPSHOT",
            "reason": "委託簿快照時間未前進，不建立 persistence／撤單／補單證據",
            "persistence": None,
            "previous_ts": previous_ts,
            "current_ts": current_ts,
            "snapshot_only_is_not_support_resistance": True,
        }
    is_long = direction == "LONG"
    current_side = current.bid_depth_usd if is_long else current.ask_depth_usd
    previous_side = previous.get("bid_depth_usd") if is_long else previous.get("ask_depth_usd")
    opposite_side = current.ask_depth_usd if is_long else current.bid_depth_usd
    previous_opposite = previous.get("ask_depth_usd") if is_long else previous.get("bid_depth_usd")
    if not all(isinstance(value, (int, float)) and value > 0 for value in (current_side, previous_side, opposite_side, previous_opposite)):
        return {
            "state": "DATA_MISSING",
            "reason": "委託簿序列資料不足",
            "persistence": None,
        }
    side_ratio = float(current_side) / float(previous_side)
    opposite_ratio = float(opposite_side) / float(previous_opposite)
    signed_taker = (
        (float(current.taker_buy_ratio) - 0.5) * 2.0
        if current.taker_buy_ratio is not None
        else 0.0
    ) * (1.0 if is_long else -1.0)
    signed_price = float(price_change_pct or 0.0) * (1.0 if is_long else -1.0)
    if side_ratio <= 0.55:
        state, reason = "LIQUIDITY_WITHDRAWAL", "同方向流動性在價格接近時明顯撤離"
    elif side_ratio >= 1.12 and signed_taker < -0.20 and signed_price >= -0.02:
        state, reason = "REFILL_ABSORPTION", "反向主動成交湧入但價格推不動，且同方向深度補回"
    elif side_ratio >= 0.85 and opposite_ratio <= 1.10:
        state, reason = "PERSISTENT_SUPPORT", "同方向深度跨掃描持續存在；僅作微結構支持"
    elif opposite_ratio >= 1.35:
        state, reason = "PERSISTENT_OPPOSITION", "反向深度跨掃描增加"
    else:
        state, reason = "NEUTRAL", "委託簿序列未形成清楚支持或反證"
    return {
        "state": state,
        "reason": reason,
        "persistence": round(side_ratio, 3),
        "opposite_persistence": round(opposite_ratio, 3),
        "cancel_behavior": "WITHDRAWAL" if side_ratio <= 0.55 else "STABLE",
        "refill": side_ratio >= 1.12,
        "absorption": state == "REFILL_ABSORPTION",
        "snapshot_only_is_not_support_resistance": True,
    }


def _closed_core_path(
    metrics: dict[str, Any],
    after_ts: int,
) -> list[tuple[int, float, float, float]]:
    by_timestamp: dict[int, tuple[int, float, float, float]] = {}
    raw_path = metrics.get("_core_path", [])
    if isinstance(raw_path, list):
        for item in raw_path:
            if isinstance(item, dict):
                raw_ts = item.get("ts")
                raw_high = item.get("high")
                raw_low = item.get("low")
                raw_close = item.get("close")
            elif isinstance(item, (list, tuple)) and len(item) >= 4:
                raw_ts, raw_high, raw_low, raw_close = item[:4]
            else:
                continue
            try:
                ts = int(raw_ts)
            except (TypeError, ValueError):
                continue
            high = _number(raw_high)
            low = _number(raw_low)
            close = _number(raw_close)
            if ts <= after_ts or None in (high, low, close) or high < low:
                continue
            by_timestamp[ts] = (ts, float(high), float(low), float(close))
    if not by_timestamp:
        try:
            ts = int(metrics.get("core_timestamp") or 0)
        except (TypeError, ValueError):
            ts = 0
        high = _number(metrics.get("core_high") or metrics.get("last_price"))
        low = _number(metrics.get("core_low") or metrics.get("last_price"))
        close = _number(metrics.get("core_close") or metrics.get("last_price"))
        if ts > after_ts and None not in (high, low, close) and high >= low:
            by_timestamp[ts] = (ts, float(high), float(low), float(close))
    return [by_timestamp[ts] for ts in sorted(by_timestamp)]


def _signal_payload(signal: Signal) -> dict[str, Any]:
    payload = signal.to_dict()
    payload["market_metrics"] = {
        key: value
        for key, value in signal.market_metrics.items()
        if not key.startswith("_")
    }
    return payload


def _signal_core_timestamp(signal: Signal) -> int:
    try:
        return int(
            signal.market_metrics.get("core_timestamp")
            or signal.data_timestamp
            or 0
        )
    except (TypeError, ValueError):
        return 0


def _signal_trigger_event_timestamp(signal: Signal) -> int:
    try:
        return int(
            signal.market_story.get("trigger", {}).get("event_ts")
            or signal.data_timestamp
            or signal.closed_candle_ts
            or 0
        )
    except (TypeError, ValueError):
        return 0


def _market_state_core_timestamp(state: MarketState) -> int:
    try:
        return int(
            state.market_metrics.get("core_timestamp")
            or state.closed_candle_ts
            or 0
        )
    except (TypeError, ValueError):
        return 0


def _market_context_timestamp(context: MarketContext) -> int:
    try:
        sampled_at = int(context.sampled_at or 0)
    except (TypeError, ValueError):
        sampled_at = 0
    if sampled_at > 0:
        return sampled_at
    values: list[int] = []
    for value in context.source_timestamps.values():
        try:
            values.append(int(value or 0))
        except (TypeError, ValueError):
            continue
    return max(values, default=0)


def _performance_bucket(records: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(item["final_r"]) for item in records if item.get("final_r") is not None]
    if not values:
        return {
            "sample_size": 0,
            "win_rate_pct": None,
            "average_r": None,
            "expectancy_r": None,
            "profit_factor": None,
            "max_consecutive_losses": None,
            "max_drawdown_r": None,
        }
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    profit_factor = sum(wins) / abs(sum(losses)) if losses else None
    consecutive = 0
    max_consecutive = 0
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in values:
        if value < 0:
            consecutive += 1
            max_consecutive = max(max_consecutive, consecutive)
        else:
            consecutive = 0
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    average = sum(values) / len(values)
    return {
        "sample_size": len(values),
        "win_rate_pct": round(len(wins) / len(values) * 100.0, 2),
        "average_r": round(average, 3),
        "expectancy_r": round(average, 3),
        "profit_factor": round(profit_factor, 3) if profit_factor is not None else None,
        "max_consecutive_losses": max_consecutive,
        "max_drawdown_r": round(max_drawdown, 3),
    }


def _group_performance(records: list[dict[str, Any]], key: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in records:
        groups.setdefault(str(item.get(key) or "UNKNOWN"), []).append(item)
    return {name: _performance_bucket(items) for name, items in sorted(groups.items())}


def _quality_performance(records: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {"80_PLUS": [], "50_TO_79": [], "BELOW_50": [], "MISSING": []}
    for item in records:
        quality = item.get("execution_quality")
        bucket = (
            "MISSING"
            if quality is None
            else "80_PLUS"
            if float(quality) >= 80.0
            else "50_TO_79"
            if float(quality) >= 50.0
            else "BELOW_50"
        )
        groups[bucket].append(item)
    return {name: _performance_bucket(items) for name, items in groups.items()}


def _research_performance(rows: list[sqlite3.Row]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        story_value = payload.get("market_story", {})
        story = story_value if isinstance(story_value, dict) else {}
        context_value = story.get("context", {})
        context = context_value if isinstance(context_value, dict) else {}
        sessions_value = context.get("sessions", {})
        active_sessions = (
            sessions_value.get("active", [])
            if isinstance(sessions_value, dict)
            else []
        )
        sessions = [
            str(value)
            for value in active_sessions
            if isinstance(active_sessions, list) and str(value).strip()
        ]
        driver_value = context.get("market_driver", {})
        driver = (
            driver_value.get("key")
            if isinstance(driver_value, dict)
            else driver_value
        )
        records.append(
            {
                "horizon": str(row["horizon"] or "UNKNOWN"),
                "trigger_type": str(row["trigger_type"] or "UNKNOWN"),
                "mfe_r": float(row["mfe_r"] or 0.0),
                "mae_r": float(row["mae_r"] or 0.0),
                "final_r": row["final_r"],
                "sessions": sessions or ["UNKNOWN"],
                "market_driver": str(driver or "UNKNOWN"),
                "higher_timeframe_alignment": _higher_timeframe_alignment(
                    payload,
                    context,
                ),
            }
        )

    def grouped(key: str) -> dict[str, dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            values = record[key]
            if isinstance(values, list):
                labels = values
            else:
                labels = [values]
            for label in labels:
                buckets.setdefault(str(label), []).append(record)
        return {
            label: _research_bucket(items)
            for label, items in sorted(buckets.items())
        }

    horizon_trigger: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        key = f"{record['horizon']}:{record['trigger_type']}"
        horizon_trigger.setdefault(key, []).append(record)
    return {
        "sample_size": len(records),
        "avg_mfe_r": _average_metric(records, "mfe_r"),
        "avg_mae_r": _average_metric(records, "mae_r"),
        "minimum_group_sample": 5,
        "read_only": True,
        "auto_tuning": False,
        "by_session": grouped("sessions"),
        "by_market_driver": grouped("market_driver"),
        "by_horizon_trigger": {
            label: _research_bucket(items)
            for label, items in sorted(horizon_trigger.items())
        },
        "by_higher_timeframe_alignment": grouped(
            "higher_timeframe_alignment"
        ),
    }


def _research_bucket(records: list[dict[str, Any]]) -> dict[str, Any]:
    if len(records) < 5:
        return {"sample_size": len(records)}
    outcomes = [
        {"final_r": record["final_r"]}
        for record in records
        if record.get("final_r") is not None
    ]
    if len(outcomes) < 5:
        return {"sample_size": len(records)}
    bucket = _performance_bucket(outcomes)
    bucket["sample_size"] = len(records)
    bucket["outcome_sample_size"] = len(outcomes)
    bucket["avg_mfe_r"] = _average_metric(records, "mfe_r")
    bucket["avg_mae_r"] = _average_metric(records, "mae_r")
    return bucket


def _average_metric(records: list[dict[str, Any]], key: str) -> float | None:
    values = [float(record[key]) for record in records if record.get(key) is not None]
    return round(sum(values) / len(values), 3) if values else None


def _higher_timeframe_alignment(
    payload: dict[str, Any],
    context: dict[str, Any],
) -> str:
    explicit = context.get("counter_higher_timeframe")
    if explicit is None:
        explicit = payload.get("market_story", {}).get(
            "counter_higher_timeframe"
        )
    if isinstance(explicit, bool):
        return "COUNTER_HIGHER_TIMEFRAME" if explicit else "ALIGNED_OR_NEUTRAL"
    alignment = str(
        context.get("higher_timeframe_alignment")
        or payload.get("market_story", {}).get("higher_timeframe_alignment")
        or ""
    ).upper()
    if alignment in {"COUNTER", "OPPOSED", "CONFLICT"}:
        return "COUNTER_HIGHER_TIMEFRAME"
    conflicts = [str(value).upper() for value in payload.get("conflicts", [])]
    if any(
        ("4H" in value or "高週期" in value)
        and ("衝突" in value or "反向" in value or "OPPOS" in value)
        for value in conflicts
    ):
        return "COUNTER_HIGHER_TIMEFRAME"
    return "ALIGNED_OR_NEUTRAL"


def _stage_rank(stage: str) -> int:
    return {
        "WATCH": 0,
        "NEAR_TRIGGER": 1,
        "EARLY_SIGNAL": 2,
        "REENTRY": 3,
        "CONFIRMED": 4,
        "TRENDING": 5,
        "EXTENDED": 6,
        "NO_FOLLOW_THROUGH": 1,
        "INVALIDATED": -1,
    }.get(stage, 0)


def _iso_from_millis(value: Any) -> str | None:
    try:
        numeric = int(value)
        if numeric <= 0:
            return None
        return datetime.fromtimestamp(numeric / 1000.0, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _number(value: Any) -> float | None:
    try:
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    except (TypeError, ValueError):
        return None
