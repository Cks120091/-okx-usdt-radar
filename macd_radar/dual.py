"""Strict MA × MACD confirmation on confirmed candles, no look-ahead.

A dual-confirm signal is *new* only when the last completed bar closes the
second of two same-direction confirmations. The two events may be sequential,
within the latest five closed bars, and must remain valid on the current bar.

MA confirmation: MA5/10/20 full ordered alignment + MA20 trending in same
direction across 3 closed bars, when that alignment first becomes valid.
MACD confirmation: the MACD(EMA12-EMA26) LINE crossing the EMA9 SIGNAL line.
EMA12/26 mutual crossing (MACD crossing zero) is separate context, not
silently substituted for the signal-line crossover.
"""
from __future__ import annotations

from typing import Sequence

from .core import ema
from .technical import averages

DUAL_CONFIRM_WINDOW = 5


def dual_confirm(closes: Sequence[float], ma_type: str = "SMA",
                 window: int = DUAL_CONFIRM_WINDOW) -> dict:
    if len(closes) < 100:
        raise ValueError("Need at least 100 closed candles for strict confirmation")
    if not 2 <= window <= 20:
        raise ValueError("Window must be 2..20 closed candles")
    n = len(closes)
    a, b, c = [averages(closes, length, ma_type) for length in (5, 10, 20)]
    fast = ema(closes, 12)
    slow = ema(closes, 26)
    macd = [x-y for x, y in zip(fast, slow)]
    signal = ema(macd, 9)
    histogram = [x-y for x, y in zip(macd, signal)]

    def ma_valid(i: int, direction: int) -> bool:
        if direction > 0:
            return (a[i] > b[i] > c[i] and c[i] > c[i-1] > c[i-2])
        return (a[i] < b[i] < c[i] and c[i] < c[i-1] < c[i-2])

    def macd_event(i: int, direction: int) -> bool:
        if direction > 0:
            return histogram[i-1] <= 0 < histogram[i]
        return histogram[i-1] >= 0 > histogram[i]

    recent_start = n-window
    for direction, side in ((1, "做多"), (-1, "做空")):
        if not ma_valid(n-1, direction):
            continue
        if not (histogram[-1] > 0 if direction > 0 else histogram[-1] < 0):
            continue
        ma_events = [i for i in range(recent_start, n) if ma_valid(i, direction) and not ma_valid(i-1, direction)]
        macd_events = [i for i in range(recent_start, n) if macd_event(i, direction)]
        if not ma_events or not macd_events:
            continue
        ma_at = ma_events[-1]
        macd_at = macd_events[-1]
        # Both events must have happened within the configured window.
        # Current alignment and histogram side must still be valid.
        latest = max(ma_at, macd_at)
        is_new = latest == n-1
        return {
            "trigger": f"正式雙確認{side}" if is_new else "無新雙確認",
            "state": f"正式雙確認{side}" if is_new else f"雙確認{side}維持",
            "direction": "多" if direction > 0 else "空",
            "is_new": is_new,
            "confirmed": True,
            "ma_confirm_bars_ago": n-1-ma_at,
            "macd_confirm_bars_ago": n-1-macd_at,
            "confirmation_bars_ago": n-1-latest,
            "window_bars": window,
            "reasons": ["MA5/10/20完整排列且MA20同向", "MACD線與Signal線同向交叉"],
        }

    # Watchlist is still useful but must never be mislabeled "formal signal".
    recent_bull = any(histogram[i-1] <= 0 < histogram[i] for i in range(recent_start, n))
    recent_bear = any(histogram[i-1] >= 0 > histogram[i] for i in range(recent_start, n))
    if ma_valid(n-1, 1) and histogram[-1] > 0:
        reason = "多方均線完整、MACD同向，但雙確認時效未同時滿足"
    elif ma_valid(n-1, -1) and histogram[-1] < 0:
        reason = "空方均線完整、MACD同向，但雙確認時效未同時滿足"
    elif recent_bull or recent_bear:
        reason = "MACD近期交叉，等待同向均線完整結構"
    else:
        reason = "MA與MACD尚未形成有效雙確認"
    return {
        "trigger": "無新雙確認",
        "state": "等待雙確認",
        "direction": "無",
        "is_new": False,
        "confirmed": False,
        "ma_confirm_bars_ago": None,
        "macd_confirm_bars_ago": None,
        "confirmation_bars_ago": None,
        "window_bars": window,
        "reasons": [reason],
    }
