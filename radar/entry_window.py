"""Short-radar entry-window continuity, separate from Trigger and risk permission.

An OPEN record only avoids asking for a *second* retest of the same opportunity.
It never bypasses current prices, closed-core rules, terminal state, or hard gates.
No polling interval is claimed as complete intrabar market history.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

VERSION = 'SHORT_ENTRY_WINDOW_V1'
CORE_MS = 900_000
MAX_AGE_MS = 1_800_000


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def core_ts(signal: Any) -> int:
    return int(number(signal.market_metrics.get('core_timestamp'))
               or number(signal.data_timestamp) or number(signal.closed_candle_ts) or 0)


def plan_key(signal: Any) -> str:
    values = [signal.inst_id, signal.radar_horizon, signal.direction,
              str(signal.trigger_id or '')]
    for key in ('entry_low', 'entry_high', 'stop_loss', 'take_profit_1', 'take_profit_2'):
        value = number(getattr(signal, key, None))
        if value is None or value <= 0:
            return ''
        values.append(format(value, '.12g'))
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def is_ready(signal: Any) -> bool:
    entry = signal.entry_eligibility or {}
    final = (signal.decision_context or {}).get('final', {})
    return bool(signal.actionable and entry.get('status') == 'ENTRY_READY'
                and entry.get('actionable') is not False
                and entry.get('new_entry_allowed') is not False
                and final.get('status') == 'ENTER'
                and final.get('new_entry_allowed') is True)


def snapshot(signal: Any, observed_ms: int, state: str) -> dict[str, Any]:
    trigger = signal.market_story.get('trigger', {})
    raw = signal.market_story.get('raw', {})
    return {'version': VERSION, 'state': state, 'plan_key': plan_key(signal),
            'core_ts': core_ts(signal), 'observed_ms': observed_ms,
            'atr': number(trigger.get('event_atr') or raw.get('core_atr')),
            'ready_max_chase_atr': number(signal.entry_eligibility.get('ready_max_chase_atr'))}


def can_continue(signal: Any, observed_ms: int) -> bool:
    """Preserve only a recent, same-plan window with no observed departure.

    A new closed core requires every intervening candle to remain in the prior
    allowed price envelope. Missing history, changed plans and unknown legacy
    permissions fail closed. A previous SUSPENDED window cannot be resurrected
    by an in-zone quote; the strategy's new closed retest remains necessary.
    """
    if signal.radar_horizon != 'SHORT' or not signal.trigger_id or not plan_key(signal):
        return False
    lifecycle = signal.lifecycle or {}
    if lifecycle.get('terminal') is True or lifecycle.get('read_only') is True or str(lifecycle.get('status', '')).upper() in {
        'COMPLETED', 'INVALIDATED', 'CLOSED', 'CLOSED_UNKNOWN', 'SUPERSEDED', 'TARGET_REACHED'
    } or signal.signal_stage not in {'EARLY_SIGNAL', 'CONFIRMED', 'REENTRY'}:
        return False
    window = lifecycle.get('entry_window')
    if window is None:
        # Migrate only a still-ready accepted same-core projection, never the
        # historical entry_ready_once flag or a previously denied old plan.
        observed = number(signal.market_metrics.get('ticker_sampled_at'))
        if not is_ready(signal) or observed is None:
            return False
        window = snapshot(signal, int(observed), 'OPEN')
    if (not isinstance(window, dict) or window.get('version') != VERSION
            or window.get('state') != 'OPEN' or window.get('plan_key') != plan_key(signal)):
        return False
    prior_ts, prior_observed = number(window.get('core_ts')), number(window.get('observed_ms'))
    current = core_ts(signal)
    if (prior_ts is None or prior_observed is None or prior_ts <= 0 or current < prior_ts
            or observed_ms < prior_observed or observed_ms - prior_observed > MAX_AGE_MS
            or (current - prior_ts) % CORE_MS != 0
            or current + CORE_MS > observed_ms + 5000
            or observed_ms - (current + CORE_MS) > MAX_AGE_MS):
        return False
    if current == prior_ts:
        return True
    atr, tolerance = number(window.get('atr')), number(window.get('ready_max_chase_atr'))
    lo, hi = number(signal.entry_low), number(signal.entry_high)
    stop, target = number(signal.stop_loss), number(signal.take_profit_1)
    if atr is None or atr <= 0 or tolerance is None or tolerance < 0:
        return False
    if signal.direction == 'LONG':
        hi += atr * tolerance
    else:
        lo -= atr * tolerance
    rows = {}
    for row in signal.market_metrics.get('_core_path', []):
        if not isinstance(row, (list, tuple)) or len(row) != 4:
            return False
        values = [number(v) for v in row]
        if any(v is None for v in values):
            return False
        ts, high, low, close = values
        if ts in rows and rows[ts] != (high, low, close):
            return False
        rows[ts] = (high, low, close)
    for ts in range(int(prior_ts) + CORE_MS, current + 1, CORE_MS):
        if ts not in rows:
            return False
        high, low, close = rows[ts]
        if not (0 < low <= close <= high and lo <= low and high <= hi):
            return False
        if ((signal.direction == 'LONG' and (low <= stop or high >= target))
                or (signal.direction == 'SHORT' and (high >= stop or low <= target))):
            return False
    return True
