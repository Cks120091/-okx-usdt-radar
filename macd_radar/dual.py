"""Strict, order-INDEPENDENT MA crossover + MACD crossover confirmation.

Formal MA confirmation requires actual historical crosses of MA5/MA10,
MA5/MA20 AND MA10/MA20, the corresponding price-side ordering and a
same-direction MA20 slope. Formal MACD confirmation requires a completed
MACD-vs-Signal crossover. Both can occur in either order.

No arbitrary five-candle event expiry: the first confirmation stays eligible
while its directional state remains valid. Opposite crosses invalidate it.
Emit one NEW signal when both states first become valid for the current
pair of crossover episodes. Same historical crosses never re-alert merely
because slope briefly changed or the browser refreshed.

Only closed candles may be supplied by callers. No future look-ahead.
"""
from __future__ import annotations

from typing import Sequence

from .core import ema
from .technical import averages

MIN_CONFIRM_BARS = 100


def _cross(first_before: float, second_before: float,
           first_now: float, second_now: float) -> int:
    if first_before <= second_before and first_now > second_now:
        return 1
    if first_before >= second_before and first_now < second_now:
        return -1
    return 0


def dual_confirm(closes: Sequence[float], ma_type: str = "SMA") -> dict:
    if len(closes) < MIN_CONFIRM_BARS:
        raise ValueError("Need at least 100 CLOSED candles for strict confirmation")

    n = len(closes)
    a, b, c = [averages(closes, p, ma_type) for p in (5, 10, 20)]
    f, s = ema(closes, 12), ema(closes, 26)
    macd = [x-y for x, y in zip(f, s)]
    signal = ema(macd, 9)
    hist = [x-y for x, y in zip(macd, signal)]

    # Cross records contain only observed bar crossings, never inferred crosses
    # from current ordering. A record stays effective until the next opposite
    # or same-direction crossing of that pair.
    last_ma = {"5_10": (0, None), "5_20": (0, None), "10_20": (0, None)}
    last_macd = (0, None)
    first_ma_confirm_at: dict[tuple, int] = {}
    fired_pair_keys: set[tuple] = set()
    last_fired_at: dict[tuple, int] = {}

    latest_state = {
        "direction": "無", "confirmed": False, "is_new": False,
        "ma_confirm_bars_ago": None, "macd_confirm_bars_ago": None,
        "confirmation_bars_ago": None, "event_age_bars": None,
        "ma_crosses_bars_ago": {}, "window_bars": None,
    }

    for i in range(21, n):
        updates = {
            "5_10": _cross(a[i-1], b[i-1], a[i], b[i]),
            "5_20": _cross(a[i-1], c[i-1], a[i], c[i]),
            "10_20": _cross(b[i-1], c[i-1], b[i], c[i]),
        }
        for name, direction in updates.items():
            if direction:
                last_ma[name] = (direction, i)

        # Warm up MACD for 90 confirmed bars before recognizing its crossings.
        # The signal EMA begins at the first MACD reading, matching the
        # existing radar's deterministic calculation convention.
        if i >= 90:
            crossing = _cross(hist[i-1], 0.0, hist[i], 0.0)
            if crossing:
                last_macd = (crossing, i)

        if i < MIN_CONFIRM_BARS - 1:
            continue

        bar_state = dict(latest_state)
        bar_state.update({
            "direction": "無", "confirmed": False, "is_new": False,
            "ma_confirm_bars_ago": None, "macd_confirm_bars_ago": None,
            "confirmation_bars_ago": None, "event_age_bars": None,
            "ma_crosses_bars_ago": {},
        })

        for direction, label in ((1, "多"), (-1, "空")):
            if direction > 0:
                ordered = a[i] > b[i] > c[i]
                slope = c[i] > c[i-1] > c[i-2]
                macd_valid = hist[i] > 0
            else:
                ordered = a[i] < b[i] < c[i]
                slope = c[i] < c[i-1] < c[i-2]
                macd_valid = hist[i] < 0

            # Last observed crossover of *each pair* must match direction.
            ma_crosses_valid = all(last_ma[k][0] == direction for k in last_ma)
            if not (ordered and slope and ma_crosses_valid):
                continue
            ma_key = (direction, *(last_ma[k][1] for k in ("5_10", "5_20", "10_20")))
            if ma_key not in first_ma_confirm_at:
                first_ma_confirm_at[ma_key] = i

            if not (macd_valid and last_macd[0] == direction):
                continue

            macd_at = last_macd[1]
            ma_at = first_ma_confirm_at[ma_key]
            pair_key = (ma_key, macd_at)
            if pair_key not in fired_pair_keys:
                fired_pair_keys.add(pair_key)
                last_fired_at[pair_key] = i

            is_new = last_fired_at[pair_key] == i
            # The "is_new" of the final result must mean THIS most recent
            # confirmed bar, not the first evaluation of a previous bar.
            bar_state = {
                "direction": label,
                "confirmed": True,
                "is_new": is_new,
                "ma_confirm_bars_ago": n-1-ma_at,
                "macd_confirm_bars_ago": n-1-macd_at,
                "confirmation_bars_ago": n-1-last_fired_at[pair_key],
                "event_age_bars": abs(ma_at-macd_at),
                "ma_crosses_bars_ago": {
                    k: n-1-last_ma[k][1] for k in ("5_10", "5_20", "10_20")
                },
                "window_bars": None,
            }
            break

        latest_state = bar_state

    # A signal is newly emitted only on the most recent completed bar.
    is_new = latest_state["confirmed"] and latest_state["confirmation_bars_ago"] == 0
    direction = latest_state["direction"]
    latest_state["is_new"] = is_new
    latest_state["trigger"] = f"正式雙確認做{'多' if direction=='多' else '空'}" if is_new else "無新雙確認"
    latest_state["state"] = (
        f"正式雙確認做{'多' if direction=='多' else '空'}" if is_new else
        f"雙確認{'做多' if direction=='多' else '做空'}維持" if latest_state["confirmed"] else
        "等待雙確認"
    )
    latest_state["reasons"] = (
        ["MA5/10、MA5/20、MA10/20同向交叉且MA20斜率同向",
         "MACD主線與Signal同向交叉，兩項無先後限制"]
        if latest_state["confirmed"] else
        ["尚未完成所有同向交叉、MA20方向或MACD確認"]
    )
    return latest_state
