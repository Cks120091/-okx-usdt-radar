"""Immutable price-location facts, deliberately independent of signal permission."""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

POLICY_VERSION = "SIGNAL_POSITION_SEPARATED_V1"
# Only these well-defined location codes are demoted. Unknown/direction/data
# blockers must never be cleared by a price-location refresh.
POSITION_CODES = frozenset({
    "CHASE", "ENTRY_PERMISSION", "ENTRY_ELIGIBILITY", "PRICE_TOO_FAR",
    "NO_CHASE", "WAIT_RETEST", "ENTRY_RETEST", "ENTRY_WINDOW_CLOSED",
    "FAVORABLE_AWAY", "FAVORABLE_MISSED", "ADVERSE_TOLERANCE", "NEAR_INVALIDATION",
})
ACTIVE_SIGNAL_STAGES = frozenset({"EARLY_SIGNAL", "CONFIRMED", "REENTRY", "TRENDING", "EXTENDED"})


def read(source: Any, key: str, default: Any = None) -> Any:
    return source.get(key, default) if isinstance(source, Mapping) else getattr(source, key, default)


def mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def current_quote(item: Any) -> float | None:
    metrics = mapping(read(item, "market_metrics", {}))
    entry = mapping(read(item, "entry_eligibility", {}))
    # If an authoritative execution value is present but corrupt, do not fall
    # back to an older scan value and pretend it is a current quote.
    for source, key in ((metrics, "entry_execution_price"), (entry, "current_price"),
                        (metrics, "last_price")):
        if key in source and source[key] is not None:
            value = number(source[key])
            return value if value is not None and value > 0 else None
    return None


def position_advisory(item: Any, *, price: Any = None, source: str | None = None) -> dict[str, Any]:
    entry = mapping(read(item, "entry_eligibility", {}))
    metrics = mapping(read(item, "market_metrics", {}))
    low, high = number(read(item, "entry_low")), number(read(item, "entry_high"))
    quote = current_quote(item) if price is None else number(price)
    side = str(read(item, "direction", "NEUTRAL")).upper()
    state, note, gap = "UNKNOWN", "位置資料不足；可進位置僅供參考。", None
    if low is not None and high is not None and 0 < low <= high and quote is not None and quote > 0:
        if quote > high:
            state, gap = "ABOVE", (quote / high - 1) * 100
            note = "現價高於可進位置；可等回踩，追高風險自行評估。" if side == "LONG" else "現價高於可進位置；留意反彈與原止損。"
        elif quote < low:
            state, gap = "BELOW", (quote / low - 1) * 100
            note = "現價低於可進位置；可等反彈，追空風險自行評估。" if side == "SHORT" else "現價低於可進位置；留意回落與原止損。"
        else:
            state, gap, note = "WITHIN", 0.0, "現價位於可進位置；仍請核對風險與最新報價。"
    return {
        "policy": POLICY_VERSION, "role": "DISPLAY_ONLY", "affects_signal": False,
        "state": state, "label": {"WITHIN": "位於可進位置", "ABOVE": "高於可進位置", "BELOW": "低於可進位置", "UNKNOWN": "位置資料不足"}[state],
        "note": note, "entry_low": low, "entry_high": high, "current_price": quote,
        "gap_pct": round(gap, 6) if gap is not None else None,
        "price_source": source or metrics.get("entry_execution_price_source") or entry.get("current_price_source"),
        "legacy_position_status": entry.get("status"),
        "chase_atr": number(entry.get("chase_atr")), "adverse_atr": number(entry.get("adverse_atr")),
    }
