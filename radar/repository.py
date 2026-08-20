from __future__ import annotations

import json
import math
import sqlite3
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timezone
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

    def load_story(self, inst_id: str, horizon: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM story_state WHERE inst_id=? AND horizon=?",
                (inst_id, horizon),
            ).fetchone()
            payload = json.loads(row["payload_json"]) if row else None
            active = self._connection.execute(
                """
                SELECT direction, stage, stop_price, signal_id
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
            output.update(
                {
                    "active_trigger_direction": active["direction"],
                    "active_stage": active["stage"],
                    "active_signal_id": active["signal_id"],
                    "invalidation_price": active["stop_price"],
                    "invalidated": False,
                }
            )
        return output

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
        with self._lock, self._connection:
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
        state_map = {
            item.inst_id: item
            for item in market_states
            if item.radar_horizon == horizon
        }
        output: list[Signal] = []
        seen_signal_ids: set[str] = set()
        raw_keys: set[tuple[str, str]] = set()

        for raw in raw_signals:
            if raw.radar_horizon != horizon:
                continue
            raw_keys.add((raw.inst_id, raw.direction))
            current = self._reconcile_raw_signal(raw, completed_at)
            seen_signal_ids.add(current.trigger_id)
            if current.freshness not in ("COMPLETED", "INVALIDATED"):
                output.append(current)

        for row in self._active_rows(horizon):
            key = (row["inst_id"], row["direction"])
            if key in raw_keys or row["signal_id"] in seen_signal_ids:
                continue
            state = state_map.get(row["inst_id"])
            if state is None:
                continue
            stored = Signal.from_dict(json.loads(row["payload_json"]))
            updated = self._advance_existing(stored, row, state.market_metrics, completed_at)
            if updated.freshness not in ("COMPLETED", "INVALIDATED"):
                output.append(updated)
                seen_signal_ids.add(updated.trigger_id)

        for state in market_states:
            if state.radar_horizon == horizon:
                self.save_story(state, completed_at)
        return output

    def _reconcile_raw_signal(self, raw: Signal, completed_at: str) -> Signal:
        event_key = str(
            raw.market_story.get("trigger", {}).get("trigger_event_key")
            or f"{raw.radar_horizon}:{raw.inst_id}:{raw.direction}:{raw.trigger_type}:{raw.data_timestamp}"
        )
        active = self._active_row(raw.inst_id, raw.radar_horizon, raw.direction)
        if active is not None:
            existing = Signal.from_dict(json.loads(active["payload_json"]))
            is_new_reentry = (
                raw.trigger_type == "CONTINUATION"
                and event_key != active["event_key"]
                and raw.data_timestamp > int(active["event_ts"])
                and raw.freshness in ("NEW", "REACTIVATED")
            )
            if not is_new_reentry:
                merged = self._merge_signal(existing, raw)
                return self._advance_existing(
                    merged,
                    active,
                    raw.market_metrics,
                    completed_at,
                )
            self._close_row(
                active["signal_id"],
                completed_at,
                "REENTRY_REPLACED",
                None,
                "NEW_REENTRY",
            )

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
        self._insert_signal(created, event_key, triggered_at, completed_at)
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
        # Age advances with the latest closed core candle, not with the original
        # trigger timestamp (which intentionally remains stable for de-duplication).
        data_ts = int(
            metrics.get("core_timestamp")
            or signal.data_timestamp
            or metrics.get("trigger_event_ts")
            or 0
        )
        event_ts = int(row["event_ts"] or signal.market_story.get("trigger", {}).get("event_ts", 0) or 0)
        previous_evaluated_core_ts = int(
            signal.lifecycle.get("last_evaluated_core_ts")
            or row["event_ts"]
            or 0
        )
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
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE signals SET
                    updated_at=?, closed_at=?, stage=?, freshness=?, status=?,
                    mfe_r=?, mae_r=?, outcome=?, final_r=?, tp_sl_order=?, payload_json=?
                WHERE signal_id=?
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
                ),
            )
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
        ):
            if original_trigger.get(key) not in (None, ""):
                refreshed_trigger[key] = original_trigger[key]
        if refreshed_trigger:
            market_story["trigger"] = refreshed_trigger
        return replace(
            existing,
            score=raw.score,
            evidence=raw.evidence,
            notes=raw.notes,
            factor_scores=raw.factor_scores,
            market_metrics=raw.market_metrics,
            trend_strength_label=raw.trend_strength_label,
            trend_strength_score=raw.trend_strength_score,
            management_plan=raw.management_plan,
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
            data_timestamp=raw.data_timestamp,
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
    ) -> None:
        entry_low = _number(signal.entry_low) or 0.0
        entry_high = _number(signal.entry_high) or 0.0
        entry = (entry_low + entry_high) / 2.0
        participation = signal.market_participation.get("state")
        quality = _number(signal.execution_quality.get("score"))
        with self._lock, self._connection:
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

    def _append_event(
        self,
        signal_id: str,
        event_at: str,
        from_stage: str | None,
        to_stage: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        with self._lock, self._connection:
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
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE signals SET status='CLOSED', closed_at=?, updated_at=?,
                    outcome=?, final_r=?, tp_sl_order=?
                WHERE signal_id=?
                """,
                (closed_at, closed_at, outcome, final_r, order, signal_id),
            )

    def _active_row(self, inst_id: str, horizon: str, direction: str) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(
                """
                SELECT * FROM signals
                WHERE inst_id=? AND horizon=? AND direction=? AND status='ACTIVE'
                ORDER BY updated_at DESC LIMIT 1
                """,
                (inst_id, horizon, direction),
            ).fetchone()

    def _active_rows(self, horizon: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._connection.execute(
                    "SELECT * FROM signals WHERE horizon=? AND status='ACTIVE'",
                    (horizon,),
                ).fetchall()
            )

    def save_microstructure(self, context: MarketContext, updated_at: str) -> None:
        payload = {
            "bid_depth_usd": context.bid_depth_usd,
            "ask_depth_usd": context.ask_depth_usd,
            "order_book_imbalance": context.order_book_imbalance,
            "taker_buy_ratio": context.taker_buy_ratio,
            "cvd": context.cvd,
            "sampled_at": context.sampled_at,
            "order_book_ts": context.source_timestamps.get("order_book"),
            "source_timestamps": dict(context.source_timestamps),
            "best_bid": context.best_bid,
            "best_ask": context.best_ask,
        }
        with self._lock, self._connection:
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

    def performance(self, strategy_version: str = "V3.3_MASTER") -> dict[str, Any]:
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
        }


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
