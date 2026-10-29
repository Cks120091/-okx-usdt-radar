"""Read-only MA5/10/20/90 structure and *confirmed* MACD divergence research.

The 4MA type is not shown in the supplied screenshots; SMA is a PROVISIONAL
default, explicitly identified in the UI, not an asserted TradingView match.
Every index is a closed candle. A pivot requires two right-hand CLOSED bars.
"""
from __future__ import annotations

from typing import Sequence

from .core import ema

WINDOW = 2


def averages(closes: Sequence[float], period: int, ma_type: str = "SMA") -> list[float | None]:
    if period < 1 or len(closes) < period:
        raise ValueError("insufficient candles for moving average")
    if ma_type == "EMA":
        return [None] * (period - 1) + ema(closes, period)[period - 1:]
    if ma_type != "SMA":
        raise ValueError("MA type must be SMA or EMA")
    result: list[float | None] = [None] * (period - 1)
    window_sum = sum(closes[:period])
    result.append(window_sum / period)
    for index in range(period, len(closes)):
        window_sum += closes[index] - closes[index-period]
        result.append(window_sum / period)
    return result


def ma_structure(closes: Sequence[float], ma_type: str = "SMA") -> dict:
    if len(closes) < 100:
        raise ValueError("100 confirmed candles are needed for MA90 and slope")
    data = {p: averages(closes, p, ma_type) for p in (5, 10, 20, 90)}
    a, b, c, d = (data[p] for p in (5, 10, 20, 90))

    def check(index: int) -> str:
        # Require MA20 to decline/incline on two consecutive CLOSED bars.
        if a[index] < b[index] < c[index] and c[index] < c[index-1] < c[index-2]:
            return "空方完整排列"
        if a[index] > b[index] > c[index] and c[index] > c[index-1] > c[index-2]:
            return "多方完整排列"
        if a[index] < b[index] and a[index] < c[index] and b[index] < c[index]:
            return "空方排列待20轉向"
        if a[index] > b[index] and a[index] > c[index] and b[index] > c[index]:
            return "多方排列待20轉向"
        return "交叉形成中"

    cross = "無新交叉"
    if a[-2] <= b[-2] and a[-1] > b[-1]:
        cross = "MA5上穿MA10"
    elif a[-2] >= b[-2] and a[-1] < b[-1]:
        cross = "MA5下穿MA10"

    def recent_cross(first: list, second: list, name: str) -> str:
        # Only the most recent completed cross over a bounded 12-bar window;
        # "already above/below" is not mislabeled as a new crossing.
        for i in range(len(first)-1, max(0, len(first)-13), -1):
            if first[i-1] <= second[i-1] and first[i] > second[i]:
                return f"{name}上穿20（{len(first)-1-i}根前）"
            if first[i-1] >= second[i-1] and first[i] < second[i]:
                return f"{name}下穿20（{len(first)-1-i}根前）"
        return "近12根無穿越20"

    phase = check(-1)
    prev_phase = check(-2)
    trigger = ("空方結構新成立" if phase == "空方完整排列" and prev_phase != phase
               else "多方結構新成立" if phase == "多方完整排列" and prev_phase != phase
               else "無新結構確認")
    return {
        "ma_type": ma_type, "ma5": a[-1], "ma10": b[-1], "ma20": c[-1],
        "ma90": d[-1], "ma20_slope_2bars": (c[-1] - c[-3]) / c[-3],
        "cross_5_10": cross,
        "ma5_vs20_recent": recent_cross(a, c, "MA5"),
        "ma10_vs20_recent": recent_cross(b, c, "MA10"),
        "structure": phase, "structure_trigger": trigger,
        "ma90_position": "價格高於MA90" if closes[-1] > d[-1] else "價格低於MA90",
    }


def _pivots(values: Sequence[float], kind: str) -> list[int]:
    result: list[int] = []
    for i in range(WINDOW, len(values) - WINDOW):
        before = values[i-WINDOW:i]
        after = values[i+1:i+1+WINDOW]
        if kind == "high" and all(values[i] > x for x in before) and all(values[i] >= x for x in after):
            result.append(i)
        elif kind == "low" and all(values[i] < x for x in before) and all(values[i] <= x for x in after):
            result.append(i)
    return result


def detect_divergences(highs: Sequence[float], lows: Sequence[float],
                       macd_line: Sequence[float], timestamps: Sequence[int] | None = None) -> list[dict]:
    """Compare MACD line at two PRICE swing pivots; regular divergence only.

    Minor/major are experimental swing *scope* labels, NOT win rate or strength.
    """
    n = len(highs)
    if n != len(lows) or n != len(macd_line) or n < 45:
        raise ValueError("Need 45+ aligned confirmed OHLC/MACD bars")
    if timestamps is not None and len(timestamps) != n:
        raise ValueError("Timestamps must align to candles")
    found = []
    for kind in ("high", "low"):
        pivots = [p for p in _pivots(highs if kind == "high" else lows, kind) if p >= n-75]
        for second in reversed(pivots):
            if (n-1-second) > 12:  # Only recently confirmed divergences
                break
            for first in reversed(pivots):
                gap = second-first
                if gap < 3:
                    continue
                if gap > 40:
                    break
                p1 = highs[first] if kind == "high" else lows[first]
                p2 = highs[second] if kind == "high" else lows[second]
                m1, m2 = macd_line[first], macd_line[second]
                price_pct = (p2-p1) / max(abs(p1), 1e-12)
                oscillator_delta = m2-m1
                meaningful = abs(oscillator_delta) >= max(abs(m1), abs(m2), 1e-12) * 0.08
                if not meaningful:
                    continue
                if kind == "high" and price_pct >= 0.003 and oscillator_delta < 0:
                    label, side = "頂背離", "偏空"
                elif kind == "low" and price_pct <= -0.003 and oscillator_delta > 0:
                    label, side = "底背離", "偏多"
                else:
                    continue
                level = "大背離候選" if gap >= 12 and abs(price_pct) >= 0.012 else "小背離候選"
                found.append({
                    "pattern": label, "side": side, "level": level,
                    "first_pivot_index": first, "second_pivot_index": second,
                    "pivot_gap_bars": gap, "confirmed_bars_ago": n-1-second-WINDOW,
                    "price_difference_pct": round(price_pct * 100, 3),
                    "macd_first": m1, "macd_second": m2,
                    "second_pivot_ts": timestamps[second] if timestamps else None,
                    "confirmation_ts": timestamps[second + WINDOW] if timestamps else None,
                    "note": "只比較已確認的兩個價格波峰／波谷與對應MACD；候選不等於已反轉。",
                })
                break  # Best recent corresponding price pivot for this second pivot
    found.sort(key=lambda d: (d["confirmed_bars_ago"], -d["pivot_gap_bars"]))
    return found[:3]


def combined_analysis(closes: Sequence[float], highs: Sequence[float],
                      lows: Sequence[float], timestamps: Sequence[int],
                      ma_type: str = "SMA") -> dict:
    if not (len(closes) == len(highs) == len(lows) == len(timestamps)):
        raise ValueError("OHLC data lengths differ")
    fast, slow = ema(closes, 12), ema(closes, 26)
    macd = [f-s for f, s in zip(fast, slow)]
    zero_cross = "無新零軸交叉"
    if macd[-2] <= 0 < macd[-1]:
        zero_cross = "EMA12上穿EMA26（MACD上穿零軸）"
    elif macd[-2] >= 0 > macd[-1]:
        zero_cross = "EMA12下穿EMA26（MACD下穿零軸）"
    ma = ma_structure(closes, ma_type)
    return {
        "ma": ma,
        "ema12_26_cross": zero_cross,
        "divergences": detect_divergences(highs, lows, macd, timestamps),
        "confluence": (
            "均線空方結構+MACD零軸下方" if ma["structure"] == "空方完整排列" and macd[-1] < 0
            else "均線多方結構+MACD零軸上方" if ma_structure(closes, ma_type)["structure"] == "多方完整排列" and macd[-1] > 0
            else "條件未共振"
        )
    }
