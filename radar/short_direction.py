"""One completed-1H direction for short-radar display, selection and permission."""
from __future__ import annotations

import math
from collections.abc import Mapping

POLICY = "SHORT_1H_MACD_MA_RESONANCE_15M_TRIGGER_V2"
LONG_POLICY = "LONG_4H_MACD_MA_RESONANCE_1H_TRIGGER_V2"


def _finite(value):
    try:
        number = float(value) if value is not None and not isinstance(value, bool) else math.nan
    except (TypeError, ValueError, OverflowError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def _read(frame, key):
    if isinstance(frame, Mapping):
        return frame.get(key)
    return getattr(frame, key, None)


def _legacy_fusion_direction(value):
    """Backward compatibility for stored episodes that only contain fusion score."""
    score = _finite(value)
    if not math.isfinite(score) or not 0 <= score <= 100:
        return {"direction": "UNKNOWN", "state": "UNKNOWN", "label": "方向資料不足", "score": None,
                "policy": "LEGACY_FUSION_FALLBACK"}
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
    return {"direction": direction, "state": state, "label": label, "score": score,
            "policy": "LEGACY_FUSION_FALLBACK"}


def macd_ma_direction(value, *, timeframe="1H", policy=POLICY):
    """Use MA5/10/20 x MACD resonance as the canonical timeframe direction.

    MACD or MA may lead first. A formal direction exists only when the complete
    MA stack and MACD agree. If only one family leads, the state remains
    forming/transition so the lower timeframe may prepare but may not create a
    new formal position.
    """

    if not isinstance(value, Mapping) and not hasattr(value, "sma5"):
        return _legacy_fusion_direction(value)

    if isinstance(value, Mapping):
        has_trend_fields = all(
            value.get(key) is not None
            for key in ("ma5", "ma10", "ma20", "macd_line", "macd_signal")
        )
        if not has_trend_fields:
            if "fusion_long_score" in value:
                return _legacy_fusion_direction(value.get("fusion_long_score"))
            return {"direction": "UNKNOWN", "state": "UNKNOWN", "label": "方向資料不足",
                    "score": None, "policy": policy}

    ma5 = _finite(_read(value, "ma5"))
    if not math.isfinite(ma5):
        ma5 = _finite(_read(value, "sma5"))
    ma10 = _finite(_read(value, "ma10"))
    if not math.isfinite(ma10):
        ma10 = _finite(_read(value, "sma10"))
    ma20 = _finite(_read(value, "ma20"))
    if not math.isfinite(ma20):
        ma20 = _finite(_read(value, "sma20"))
    macd_line = _finite(_read(value, "macd_line"))
    macd_signal = _finite(_read(value, "macd_signal"))
    hist = _finite(_read(value, "macd_hist"))
    prev_hist = _finite(_read(value, "macd_prev_hist"))

    required = (ma5, ma10, ma20, macd_line, macd_signal)
    if not all(math.isfinite(number) for number in required):
        fallback = _read(value, "fusion_long_score")
        if fallback is not None:
            return _legacy_fusion_direction(fallback)
        return {"direction": "UNKNOWN", "state": "UNKNOWN", "label": "方向資料不足",
                "score": None, "policy": policy}

    bull_ma = ma5 > ma10 > ma20
    bear_ma = ma5 < ma10 < ma20
    bull_fast = ma5 > ma10
    bear_fast = ma5 < ma10
    bull_macd = macd_line > macd_signal
    bear_macd = macd_line < macd_signal
    bull_improving = math.isfinite(hist) and math.isfinite(prev_hist) and hist > prev_hist
    bear_improving = math.isfinite(hist) and math.isfinite(prev_hist) and hist < prev_hist

    if bull_ma and bull_macd:
        return {
            "direction": "LONG", "state": "LONG", "label": "多頭共振",
            "score": 100.0, "ma_state": "BULL", "macd_state": "BULL",
            "leading": "BOTH", "policy": policy,
        }
    if bear_ma and bear_macd:
        return {
            "direction": "SHORT", "state": "SHORT", "label": "空頭共振",
            "score": 0.0, "ma_state": "BEAR", "macd_state": "BEAR",
            "leading": "BOTH", "policy": policy,
        }

    bull_forming = (
        (bull_ma and (bull_macd or bull_improving))
        or (bull_macd and not bear_ma)
        or (bull_fast and bull_improving and not bear_ma)
    )
    bear_forming = (
        (bear_ma and (bear_macd or bear_improving))
        or (bear_macd and not bull_ma)
        or (bear_fast and bear_improving and not bull_ma)
    )

    if bull_forming and not bear_forming:
        leading = "MA" if bull_ma and not bull_macd else "MACD" if bull_macd and not bull_ma else "MIXED"
        return {
            "direction": "NEUTRAL", "state": "LONG_FORMING",
            "label": "多頭形成中｜等待 MACD × MA 共振",
            "score": 55.0, "ma_state": "BULL" if bull_ma else "MIXED",
            "macd_state": "BULL" if bull_macd else "FORMING",
            "leading": leading, "policy": policy,
        }
    if bear_forming and not bull_forming:
        leading = "MA" if bear_ma and not bear_macd else "MACD" if bear_macd and not bear_ma else "MIXED"
        return {
            "direction": "NEUTRAL", "state": "SHORT_FORMING",
            "label": "空頭形成中｜等待 MACD × MA 共振",
            "score": 45.0, "ma_state": "BEAR" if bear_ma else "MIXED",
            "macd_state": "BEAR" if bear_macd else "FORMING",
            "leading": leading, "policy": policy,
        }

    return {
        "direction": "NEUTRAL", "state": "TRANSITION",
        "label": f"{timeframe} 趨勢未共振", "score": 50.0,
        "ma_state": "BULL" if bull_ma else "BEAR" if bear_ma else "MIXED",
        "macd_state": "BULL" if bull_macd else "BEAR" if bear_macd else "NEUTRAL",
        "leading": "NONE", "policy": policy,
    }


def hourly_direction(value):
    """Backward-compatible completed-1H direction used by the short radar."""
    return macd_ma_direction(value, timeframe="1H", policy=POLICY)


def swing_direction(value):
    """Canonical completed-4H MACD/MA direction used by the swing radar."""
    return macd_ma_direction(value, timeframe="4H", policy=LONG_POLICY)
