"""One completed-1H direction for short-radar display, selection and permission."""
from __future__ import annotations

import math

POLICY = "SHORT_1H_DIRECTION_15M_TRIGGER_V1"


def hourly_direction(value):
    try:
        score = float(value) if value is not None and not isinstance(value, bool) else math.nan
    except (TypeError, ValueError, OverflowError):
        score = math.nan
    if not math.isfinite(score) or not 0 <= score <= 100:
        return {"direction": "UNKNOWN", "state": "UNKNOWN", "label": "方向資料不足", "score": None}
    # Match the precision saved by strategy._feature_metrics, so a scan and
    # its later preflight cannot disagree at a rounding boundary.
    score = round(score, 2)
    if score >= 60:
        direction, state, label = "LONG", "LONG", "偏多"
    elif score >= 52:
        direction, state, label = "LONG", "LONG_WEAKENING", "偏多（轉弱）"
    elif score > 48:
        direction, state, label = "NEUTRAL", "TRANSITION", "中性／方向未定"
    elif score > 40:
        direction, state, label = "SHORT", "SHORT_WEAKENING", "偏空（轉弱）"
    else:
        direction, state, label = "SHORT", "SHORT", "偏空"
    return {"direction": direction, "state": state, "label": label, "score": score}
