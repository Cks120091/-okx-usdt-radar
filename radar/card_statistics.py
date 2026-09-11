"""Passive, first-published-entry TP1 statistics; never a trading permission.

No networking, scheduling, orders or historical backfill. A separate small
ledger freezes actual publication quotes and context; the original signal
ledger and its performance/learning calculations are never changed.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

VERSION = "CARD_STATISTICS_V1"
MIN_RESOLVED = 50
MIN_DAYS = 5
MIN_COVERAGE = 80.0
LOOKBACK_DAYS = 90
MAX_POOL = 5000
DAY = 86_400_000
HOLD_MS = {"SHORT": DAY, "LONG": 7 * DAY}
BAR_MS = {"SHORT": 900_000, "LONG": 14_400_000}
NOTE = "已判定樣本的 TP1 先達率；掃描報價假設、未扣費，非本單預測或實際成交勝率。"


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def mapping(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def read(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, Mapping) else getattr(item, key, default)


def timestamp(value: Any) -> int:
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return 0
            return int(dt.timestamp() * 1000)
        except (ValueError, OverflowError):
            return 0
    n = number(value)
    return int(n) if n is not None and n > 0 and n == int(n) else 0


@lru_cache(maxsize=1)
def _source_hash() -> str:
    root = Path(__file__).parent
    digest = hashlib.sha256(VERSION.encode())
    for name in ("strategy.py", "market_story.py", "decision.py", "entry_window.py",
                 "evidence.py", "scanner.py", "preflight.py", "service.py", "repository.py"):
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def fingerprint(config: Any) -> str:
    keys = ("min_quote_volume_24h", "quote_volume_buffer_24h", "minimum_rr",
            "max_spread_pct", "max_slippage_pct", "max_execution_cost_to_risk_pct",
            "estimated_taker_fee_pct", "max_entry_extension_atr", "severe_entry_extension_atr",
            "entry_ready_max_chase_atr", "entry_missed_chase_atr", "early_signal_max_age_bars",
            "max_signals", "context_candidates", "candle_limit", "candle_limit_1d",
            "candle_limit_4h", "candle_limit_1h", "candle_limit_15m", "candle_limit_5m")
    settings = {key: read(config, key) for key in keys}
    return hashlib.sha256((_source_hash() + json.dumps(settings, sort_keys=True,
                              allow_nan=False)).encode()).hexdigest()


def setup(signal: Any, entry: float | None = None) -> dict[str, Any] | None:
    horizon, direction = read(signal, "radar_horizon"), read(signal, "direction")
    if horizon not in HOLD_MS or direction not in {"LONG", "SHORT"}:
        return None
    stop, target = number(read(signal, "stop_loss")), number(read(signal, "take_profit_1"))
    low, high = number(read(signal, "entry_low")), number(read(signal, "entry_high"))
    if None in (stop, target, low, high) or min(stop, target, low, high) <= 0 or low > high:
        return None
    if entry is None:
        entry = number(mapping(read(signal, "market_metrics", {})).get("entry_execution_price"))
    if entry is None or entry <= 0:
        return None
    risk = entry - stop if direction == "LONG" else stop - entry
    reward = target - entry if direction == "LONG" else entry - target
    if risk <= 0 or reward <= 0:
        return None
    rr = reward / risk
    band = "<2R" if rr < 2 else "2–<3R" if rr < 3 else "3–<4R" if rr < 4 else "4–<6R" if rr < 6 else "≥6R"
    background_tf = "4H" if horizon == "SHORT" else "1D"
    background = mapping(mapping(read(signal, "timeframe_states", {})).get(background_tf))
    bg_direction = background.get("direction")
    relation = ("同向背景" if bg_direction == direction else "逆高週期背景"
                if bg_direction in {"LONG", "SHORT"} else "中性背景"
                if bg_direction == "NEUTRAL" else "背景未知")
    kind = str(read(signal, "trigger_type", "UNKNOWN"))
    stage = str(read(signal, "signal_stage", "UNKNOWN"))
    cohort = [horizon, direction, kind, stage, relation, band]
    names = {"CONTINUATION": "回踩續走", "BREAKOUT": "突破", "REVERSAL": "反轉", "REENTRY": "再次觸發"}
    label = f"{'15m' if horizon == 'SHORT' else '4H'} {'多' if direction == 'LONG' else '空'}｜{names.get(kind, '其他觸發')}｜{relation}｜{band}"
    return {"cohort": json.dumps(cohort, ensure_ascii=False), "label": label,
            "horizon": horizon, "direction": direction, "entry": entry,
            "stop": stop, "target": target, "rr": rr, "hold_ms": HOLD_MS[horizon]}


def initialize(connection: sqlite3.Connection) -> None:
    # Additive only. No ALTER/UPDATE/DELETE of the original signals ledger.
    connection.execute("""CREATE TABLE IF NOT EXISTS card_statistics_v1 (
        version TEXT NOT NULL, signal_id TEXT NOT NULL, inst_id TEXT NOT NULL,
        cohort TEXT NOT NULL, label TEXT NOT NULL, horizon TEXT NOT NULL,
        direction TEXT NOT NULL, entry REAL NOT NULL, stop REAL NOT NULL,
        target REAL NOT NULL, rr REAL NOT NULL, observed_ms INTEGER NOT NULL,
        deadline_ms INTEGER NOT NULL, next_bar_ms INTEGER NOT NULL,
        outcome TEXT NOT NULL DEFAULT 'PENDING', reason TEXT NOT NULL DEFAULT '',
        resolved_ms INTEGER, PRIMARY KEY(version, signal_id))""")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_card_statistics_cohort ON card_statistics_v1(version, cohort, observed_ms)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_card_statistics_pending ON card_statistics_v1(outcome, inst_id, horizon)")


def enroll(connection: sqlite3.Connection, signal: Any, version: str, now: int) -> bool:
    """Freeze only a final, published, executable quote; one sample per Episode."""
    identity = str(read(signal, "trigger_id", "") or "")
    if not identity or not now:
        return False
    metrics = mapping(read(signal, "market_metrics", {}))
    entry_state = mapping(read(signal, "entry_eligibility", {}))
    decision = mapping(read(signal, "decision_context", {}))
    final, gate = mapping(decision.get("final")), mapping(decision.get("hard_gate"))
    life = mapping(read(signal, "lifecycle", {}))
    if (read(signal, "actionable") is not True or final.get("new_entry_allowed") is not True
            or final.get("status") != "ENTER" or entry_state.get("new_entry_allowed") is not True
            or entry_state.get("status") != "ENTRY_READY" or gate.get("blocked") or gate.get("unknown")
            or life.get("terminal") or read(signal, "signal_stage") not in {"EARLY_SIGNAL", "CONFIRMED", "REENTRY"}):
        return False
    quote_ts = timestamp(metrics.get("ticker_sampled_at"))
    source = str(metrics.get("entry_execution_price_source") or "").upper()
    expected = {"ASK", "BEST_ASK"} if read(signal, "direction") == "LONG" else {"BID", "BEST_BID"}
    if source not in expected or not quote_ts or not 0 <= now - quote_ts <= 120_000:
        return False
    spec = setup(signal)
    if spec is None:
        return False
    bar = BAR_MS[spec["horizon"]]
    cursor = connection.execute("""INSERT OR IGNORE INTO card_statistics_v1
        (version, signal_id, inst_id, cohort, label, horizon, direction, entry, stop,
         target, rr, observed_ms, deadline_ms, next_bar_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (version, identity, read(signal, "inst_id"), spec["cohort"], spec["label"],
         spec["horizon"], spec["direction"], spec["entry"], spec["stop"], spec["target"],
         spec["rr"], now, now + spec["hold_ms"], now // bar * bar))
    return cursor.rowcount == 1


def _path(state: Any, now: int) -> dict[int, tuple[float, float, float]]:
    """Trusted closed-core path only; never substitute an instantaneous ticker."""
    interval = BAR_MS.get(read(state, "radar_horizon"))
    if interval is None:
        return {}
    rows = mapping(read(state, "market_metrics", {})).get("_core_path", [])
    result, conflicts = {}, set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, (tuple, list)) or len(row) < 4:
            continue
        ts = timestamp(row[0])
        high, low, close = (number(v) for v in row[1:4])
        if (not ts or ts % interval or ts + interval > now or None in (high, low, close)
                or not 0 < low <= close <= high):
            continue
        values = (high, low, close)
        if ts in result and result[ts] != values:
            conflicts.add(ts)
        result[ts] = values
    return {ts: value for ts, value in result.items() if ts not in conflicts}


def advance(connection: sqlite3.Connection, states: list[Any], now: int) -> None:
    paths: dict[tuple[str, str], dict] = {}
    for state in states:
        key = (read(state, "inst_id"), read(state, "radar_horizon"))
        # Duplicate sources must agree; inconsistent paths remain unavailable.
        path = _path(state, now)
        if key in paths and paths[key] != path:
            paths[key] = {}
        else:
            paths[key] = path
    pending = connection.execute("SELECT * FROM card_statistics_v1 WHERE outcome='PENDING'").fetchall()
    for row in pending:
        data = dict(row)
        if (data["inst_id"], data["horizon"]) not in paths:
            continue
        bar = BAR_MS[data["horizon"]]
        path = paths.get((data["inst_id"], data["horizon"]), {})
        cursor = data["next_bar_ms"]
        outcome, reason = "PENDING", ""
        while cursor + bar <= now and cursor < data["deadline_ms"]:
            if cursor not in path:
                # Missing path can be recovered by a later normal scan until
                # the fixed observation horizon has matured.
                if now >= data["deadline_ms"] + bar:
                    outcome, reason = "UNKNOWN", "DATA_GAP"
                break
            high, low, _ = path[cursor]
            stop_hit = low <= data["stop"] if data["direction"] == "LONG" else high >= data["stop"]
            tp_hit = high >= data["target"] if data["direction"] == "LONG" else low <= data["target"]
            if stop_hit or tp_hit:
                if cursor < data["observed_ms"] or cursor + bar > data["deadline_ms"]:
                    outcome, reason = "UNKNOWN", "PARTIAL_BOUNDARY_BAR"
                elif stop_hit and tp_hit:
                    outcome, reason = "UNKNOWN", "AMBIGUOUS_SAME_BAR"
                else:
                    outcome = "TP1_FIRST" if tp_hit else "SL_FIRST"
                    reason = "CLOSED_CORE_PATH"
                break
            cursor += bar
            if cursor >= data["deadline_ms"]:
                outcome, reason = "TIMEOUT", "NO_TARGET_OR_STOP_WITHIN_HORIZON"
                break
        if cursor != data["next_bar_ms"] or outcome != "PENDING":
            connection.execute("""UPDATE card_statistics_v1 SET next_bar_ms=?, outcome=?,
                reason=?, resolved_ms=? WHERE version=? AND signal_id=? AND outcome='PENDING'""",
                (cursor, outcome, reason, now if outcome != "PENDING" else None,
                 data["version"], data["signal_id"]))


def wilson(wins: int, total: int) -> list[float] | None:
    if not total:
        return None
    p, z = wins / total, 1.959963984540054
    scale = 1 + z * z / total
    middle = (p + z * z / (2 * total)) / scale
    margin = z / scale * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return [round(max(0, middle - margin) * 100, 1), round(min(1, middle + margin) * 100, 1)]


def summarize(connection: sqlite3.Connection, signal: Any, version: str, now: int) -> dict:
    identity = str(read(signal, "trigger_id", "") or "")
    own = connection.execute("SELECT * FROM card_statistics_v1 WHERE version=? AND signal_id=?",
                             (version, identity)).fetchone()
    spec = dict(own) if own is not None else setup(signal)
    result = {"schema_version": VERSION, "status": "INSUFFICIENT", "rate_pct": None,
              "interval_pct": None, "resolved": 0, "wins": 0, "losses": 0,
              "pending": 0, "immature": 0, "unknown": 0, "timeout": 0,
              "mature": 0, "total": 0, "days": 0, "coverage_pct": None,
              "minimum_resolved": MIN_RESOLVED, "minimum_days": MIN_DAYS,
              "minimum_coverage_pct": MIN_COVERAGE, "as_of_ms": now,
              "period_start_ms": now - LOOKBACK_DAYS * DAY,
              "version": version[:12], "note": NOTE, "cohort_label": "條件尚不足以配對",
              "lookback_days": LOOKBACK_DAYS, "max_pool": MAX_POOL,
              "pool_limited": False, "holding_hours": None,
              "interval_note": "Wilson 95% 描述區間未校正幣種／市場相關性，不是本單機率。"}
    if spec is None:
        return result
    hold = HOLD_MS[spec["horizon"]]
    result.update(cohort_label=spec["label"], holding_hours=hold // 3_600_000)
    rows = connection.execute("""SELECT * FROM card_statistics_v1 WHERE version=? AND cohort=?
        AND signal_id!=? AND observed_ms>=? AND observed_ms<=?
        ORDER BY observed_ms DESC LIMIT ?""",
        (version, spec["cohort"], identity, now - LOOKBACK_DAYS * DAY, now, MAX_POOL + 1)).fetchall()
    result["pool_limited"] = len(rows) > MAX_POOL
    rows = rows[:MAX_POOL]
    if rows:
        result["period_start_ms"] = min(row["observed_ms"] for row in rows)
    result["total"] = len(rows)
    mature = [row for row in rows if row["deadline_ms"] <= now]
    result["immature"] = len(rows) - len(mature)
    result["mature"] = len(mature)
    result["days"] = len({row["observed_ms"] // DAY for row in mature})
    # Immature winners and losers are not selectively counted ahead of still
    # open peers. Only results known by this exact as-of can be used.
    for row in mature:
        outcome = row["outcome"] if row["resolved_ms"] and row["resolved_ms"] <= now else "PENDING"
        key = {"TP1_FIRST": "wins", "SL_FIRST": "losses", "TIMEOUT": "timeout",
               "UNKNOWN": "unknown"}.get(outcome, "pending")
        result[key] += 1
    n = result["wins"] + result["losses"]
    result["resolved"] = n
    result["coverage_pct"] = round(n / len(mature) * 100, 1) if mature else None
    if n >= MIN_RESOLVED and result["days"] >= MIN_DAYS and n / len(mature) * 100 >= MIN_COVERAGE:
        result.update(status="AVAILABLE", rate_pct=round(result["wins"] / n * 100, 1),
                      interval_pct=wilson(result["wins"], n))
    elif n >= MIN_RESOLVED and (result["coverage_pct"] or 0) < MIN_COVERAGE:
        result["status"] = "LOW_COVERAGE"
    return result


def public_summary(raw: Any) -> dict:
    """No legacy quality score or raw sample history may masquerade as a rate."""
    raw = mapping(raw)
    if raw.get("schema_version") != VERSION:
        return {"schema_version": VERSION, "status": "INSUFFICIENT", "rate_pct": None,
                "resolved": 0, "minimum_resolved": MIN_RESOLVED, "note": NOTE}
    keys = ("schema_version", "status", "rate_pct", "interval_pct", "resolved", "wins", "losses",
            "pending", "immature", "unknown", "timeout", "mature", "total", "days", "coverage_pct",
            "minimum_resolved", "minimum_days", "minimum_coverage_pct", "as_of_ms", "period_start_ms",
            "version", "note", "cohort_label", "lookback_days", "max_pool", "pool_limited",
            "holding_hours", "interval_note")
    out = {key: raw[key] for key in keys if key in raw}
    counts = {key: number(out.get(key)) for key in ("resolved", "wins", "losses", "days", "mature")}
    valid = all(v is not None and v >= 0 and int(v) == v for v in counts.values())
    n, wins = counts["resolved"], counts["wins"]
    valid = valid and (n == wins + counts["losses"] and n >= MIN_RESOLVED
             and counts["days"] >= MIN_DAYS and counts["mature"] >= n
             and n / counts["mature"] * 100 >= MIN_COVERAGE)
    if not valid or out.get("status") != "AVAILABLE":
        out.update(rate_pct=None, interval_pct=None)
        if out.get("status") == "AVAILABLE":
            out["status"] = "INSUFFICIENT"
    else:
        out.update(rate_pct=round(wins / n * 100, 1), interval_pct=wilson(int(wins), int(n)))
    return out
