"""Canonical MACD x MA crossover direction engine for both radar horizons."""
from __future__ import annotations

import math
from collections.abc import Mapping

POLICY = "SHORT_1H_MACD_MA_CROSS_RESONANCE_15M_TRIGGER_V3"
LONG_POLICY = "LONG_4H_MACD_MA_CROSS_RESONANCE_1H_TRIGGER_V3"


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
        return {
            "direction": "UNKNOWN",
            "pending_direction": "UNKNOWN",
            "state": "UNKNOWN",
            "label": "方向資料不足",
            "score": None,
            "resonance": False,
            "strength": "UNKNOWN",
            "policy": "LEGACY_FUSION_FALLBACK",
        }
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
    return {
        "direction": direction,
        "pending_direction": direction,
        "state": state,
        "label": label,
        "score": score,
        "resonance": direction in {"LONG", "SHORT"},
        "strength": "LEGACY",
        "policy": "LEGACY_FUSION_FALLBACK",
    }


def _cross_age(frame, key):
    value = _read(frame, key)
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return -1


def _recent_pending_direction(
    frame,
    ma_direction: str,
    macd_direction: str,
) -> tuple[str, str]:
    """Resolve which family most recently changed when the two disagree."""
    if ma_direction in {"LONG", "SHORT"} and macd_direction not in {"LONG", "SHORT"}:
        return ma_direction, "MA"
    if macd_direction in {"LONG", "SHORT"} and ma_direction not in {"LONG", "SHORT"}:
        return macd_direction, "MACD"
    if ma_direction == macd_direction and ma_direction in {"LONG", "SHORT"}:
        return ma_direction, "BOTH"
    if ma_direction not in {"LONG", "SHORT"} or macd_direction not in {"LONG", "SHORT"}:
        return "NEUTRAL", "NONE"

    ma_age = _cross_age(frame, "ma_last_cross_bars_ago")
    macd_age = _cross_age(frame, "macd_last_cross_bars_ago")
    # Smaller bars_ago means the family crossed more recently and therefore
    # represents the new side that is waiting for the other family to follow.
    if ma_age >= 0 and macd_age >= 0 and ma_age != macd_age:
        return (ma_direction, "MA") if ma_age < macd_age else (macd_direction, "MACD")
    if ma_age >= 0 > macd_age:
        return ma_direction, "MA"
    if macd_age >= 0 > ma_age:
        return macd_direction, "MACD"
    return "NEUTRAL", "CONFLICT"


def macd_ma_direction(value, *, timeframe="1H", policy=POLICY):
    """Determine direction exactly from the user's MACD/MA crossover contract.

    MACD:
      close source, EMA(12) - EMA(26), EMA signal(9).
    MA:
      SMA 5 / 10 / 20 / 90.
      MA5 > MA10 = golden-side; MA5 < MA10 = death-side.
      MA20 only grades strength:
        LONG strong when MA5 and MA10 are both above MA20.
        SHORT strong when MA5 and MA10 are both below MA20.

    One family may cross first, but it is only FORMING.  Formal LONG/SHORT
    direction exists only after MACD and MA5/10 point to the same side.
    Their crosses do not need to happen on the same candle.
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
            return {
                "direction": "UNKNOWN",
                "pending_direction": "UNKNOWN",
                "state": "UNKNOWN",
                "label": "方向資料不足",
                "score": None,
                "resonance": False,
                "strength": "UNKNOWN",
                "policy": policy,
            }

    ma5 = _finite(_read(value, "ma5"))
    if not math.isfinite(ma5):
        ma5 = _finite(_read(value, "sma5"))
    ma10 = _finite(_read(value, "ma10"))
    if not math.isfinite(ma10):
        ma10 = _finite(_read(value, "sma10"))
    ma20 = _finite(_read(value, "ma20"))
    if not math.isfinite(ma20):
        ma20 = _finite(_read(value, "sma20"))
    ma90 = _finite(_read(value, "ma90"))
    if not math.isfinite(ma90):
        ma90 = _finite(_read(value, "sma90"))
    close = _finite(_read(value, "close"))
    macd_line = _finite(_read(value, "macd_line"))
    macd_signal = _finite(_read(value, "macd_signal"))

    required = (ma5, ma10, ma20, macd_line, macd_signal)
    if not all(math.isfinite(number) for number in required):
        fallback = _read(value, "fusion_long_score")
        if fallback is not None:
            return _legacy_fusion_direction(fallback)
        return {
            "direction": "UNKNOWN",
            "pending_direction": "UNKNOWN",
            "state": "UNKNOWN",
            "label": "方向資料不足",
            "score": None,
            "resonance": False,
            "strength": "UNKNOWN",
            "policy": policy,
        }

    ma_direction = "LONG" if ma5 > ma10 else "SHORT" if ma5 < ma10 else "NEUTRAL"
    macd_direction = (
        "LONG"
        if macd_line > macd_signal
        else "SHORT"
        if macd_line < macd_signal
        else "NEUTRAL"
    )
    resonance = ma_direction == macd_direction and ma_direction in {"LONG", "SHORT"}

    long_above_20 = ma5 > ma20 and ma10 > ma20
    short_below_20 = ma5 < ma20 and ma10 < ma20
    ma20_state = (
        "ABOVE_BOTH"
        if long_above_20
        else "BELOW_BOTH"
        if short_below_20
        else "MIXED"
    )
    ma90_state = (
        "ABOVE"
        if math.isfinite(ma90) and math.isfinite(close) and close > ma90
        else "BELOW"
        if math.isfinite(ma90) and math.isfinite(close) and close < ma90
        else "AT_OR_UNAVAILABLE"
    )

    ma_cross = str(_read(value, "ma_last_cross_direction") or "NONE").upper()
    macd_cross = str(_read(value, "macd_last_cross_direction") or "NONE").upper()
    ma_cross_age = _cross_age(value, "ma_last_cross_bars_ago")
    macd_cross_age = _cross_age(value, "macd_last_cross_bars_ago")

    if resonance:
        direction = ma_direction
        strong = long_above_20 if direction == "LONG" else short_below_20
        strength = "STRONG" if strong else "WEAK"
        state = f"{direction}_{strength}"
        side_cn = "多頭" if direction == "LONG" else "空頭"
        cross_cn = "黃金" if direction == "LONG" else "死亡"
        strength_cn = "強訊號" if strong else "弱訊號"
        ma20_cn = (
            "MA5、MA10 都在 MA20 上方"
            if direction == "LONG" and strong
            else "MA5、MA10 尚未都站上 MA20"
            if direction == "LONG"
            else "MA5、MA10 都在 MA20 下方"
            if strong
            else "MA5、MA10 尚未都跌到 MA20 下方"
        )
        score = 85.0 if direction == "LONG" and strong else 68.0 if direction == "LONG" else 15.0 if strong else 32.0
        # "leading" describes which family crossed first when both observed
        # crosses exist; it never changes the direction result.
        if ma_cross_age >= 0 and macd_cross_age >= 0 and ma_cross_age != macd_cross_age:
            leading = "MA" if ma_cross_age > macd_cross_age else "MACD"
        else:
            leading = "BOTH"
        return {
            "direction": direction,
            "pending_direction": direction,
            "state": state,
            "label": f"{side_cn}共振｜MACD {cross_cn}交叉 + MA5/10 {cross_cn}交叉｜{strength_cn}",
            "score": score,
            "resonance": True,
            "strength": strength,
            "ma_state": "BULL" if direction == "LONG" else "BEAR",
            "macd_state": "BULL" if direction == "LONG" else "BEAR",
            "ma20_state": ma20_state,
            "ma90_state": ma90_state,
            "ma90": ma90 if math.isfinite(ma90) else None,
            "leading": leading,
            "ma_last_cross_direction": ma_cross,
            "ma_last_cross_bars_ago": ma_cross_age,
            "macd_last_cross_direction": macd_cross,
            "macd_last_cross_bars_ago": macd_cross_age,
            "policy": policy,
            "reason": ma20_cn,
        }

    pending_direction, leading = _recent_pending_direction(
        value,
        ma_direction,
        macd_direction,
    )
    if pending_direction in {"LONG", "SHORT"}:
        side_cn = "多頭" if pending_direction == "LONG" else "空頭"
        cross_cn = "黃金" if pending_direction == "LONG" else "死亡"
        waiting = "MA5/10" if leading == "MACD" else "MACD" if leading == "MA" else "另一組"
        state = f"{pending_direction}_FORMING"
        label = f"{side_cn}形成中｜{leading} 已{cross_cn}交叉，等待 {waiting} 同方向交叉共振"
        score = 55.0 if pending_direction == "LONG" else 45.0
    else:
        state = "CONFLICT" if ma_direction != macd_direction else "TRANSITION"
        label = f"{timeframe} MACD／MA 尚未同向共振"
        score = 50.0

    return {
        "direction": "NEUTRAL",
        "pending_direction": pending_direction,
        "state": state,
        "label": label,
        "score": score,
        "resonance": False,
        "strength": "PENDING",
        "ma_state": "BULL" if ma_direction == "LONG" else "BEAR" if ma_direction == "SHORT" else "NEUTRAL",
        "macd_state": "BULL" if macd_direction == "LONG" else "BEAR" if macd_direction == "SHORT" else "NEUTRAL",
        "ma20_state": ma20_state,
        "ma90_state": ma90_state,
        "ma90": ma90 if math.isfinite(ma90) else None,
        "leading": leading,
        "ma_last_cross_direction": ma_cross,
        "ma_last_cross_bars_ago": ma_cross_age,
        "macd_last_cross_direction": macd_cross,
        "macd_last_cross_bars_ago": macd_cross_age,
        "policy": policy,
    }


def hourly_direction(value):
    """Completed 1H direction for the short radar."""
    return macd_ma_direction(value, timeframe="1H", policy=POLICY)


def swing_direction(value):
    """Completed 4H direction for the swing radar using the exact same rules."""
    return macd_ma_direction(value, timeframe="4H", policy=LONG_POLICY)
