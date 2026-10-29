"""Pure MACD 12/26/9 calculations using EMA/EMA and CLOSED candle prices only."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Sequence

FAST = 12
SLOW = 26
SIGNAL = 9
MIN_BARS = 90


def ema(values: Sequence[float], period: int) -> list[float]:
    if not values or period < 1:
        raise ValueError("EMA needs nonempty values and positive period")
    alpha = 2.0 / (period + 1)
    result = [float(values[0])]
    for price in values[1:]:
        result.append(result[-1] + alpha * (float(price) - result[-1]))
    return result


@dataclass(frozen=True)
class MacdResult:
    macd: float
    signal: float
    histogram: float
    previous_histogram: float
    side: str
    event: str
    phase: str
    momentum: str
    normalized_histogram: float

    def as_dict(self) -> dict:
        return vars(self).copy()


def calculate(closes: Sequence[float]) -> MacdResult:
    if len(closes) < MIN_BARS:
        raise ValueError(f"Need {MIN_BARS}+ confirmed candles")
    values = [float(v) for v in closes]
    if any(v <= 0 for v in values):
        raise ValueError("Prices must be positive")
    fast = ema(values, FAST)
    slow = ema(values, SLOW)
    macd_line = [a - b for a, b in zip(fast, slow)]
    signal_line = ema(macd_line, SIGNAL)
    hist = [a - b for a, b in zip(macd_line, signal_line)]
    prev, now = hist[-2], hist[-1]

    # A cross is only an event on the most recently CONFIRMED candle.
    if prev <= 0 < now:
        event, side = "金叉", "多方"
    elif prev >= 0 > now:
        event, side = "死叉", "空方"
    else:
        event = "無新交叉"
        side = "多方" if now > 0 else ("空方" if now < 0 else "中性")

    # These are early WATCH states, never confirmed signals.
    if event != "無新交叉":
        phase = "正式交叉"
    elif now < 0 and hist[-3] < prev < now:
        phase = "金叉預備"
    elif now > 0 and hist[-3] > prev > now:
        phase = "死叉預備"
    else:
        phase = "方向維持"
    delta = abs(now) - abs(prev)
    momentum = "動能增強" if delta > 0 else ("動能減弱" if delta < 0 else "動能持平")
    return MacdResult(
        macd=macd_line[-1],
        signal=signal_line[-1],
        histogram=now,
        previous_histogram=prev,
        side=side,
        event=event,
        phase=phase,
        momentum=momentum,
        normalized_histogram=abs(now) / values[-1],
    )
