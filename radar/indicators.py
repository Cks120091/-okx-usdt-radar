from __future__ import annotations

import math
from dataclasses import dataclass

from .models import Candle


def sma(values: list[float], period: int) -> float:
    if period <= 0 or len(values) < period:
        return math.nan
    return sum(values[-period:]) / period


def ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    output = [values[0]]
    for value in values[1:]:
        output.append((value * alpha) + (output[-1] * (1.0 - alpha)))
    return output


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) <= period:
        return math.nan
    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    relative = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + relative))


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, float, float, float]:
    if len(values) < slow + signal:
        return math.nan, math.nan, math.nan, math.nan
    fast_line = ema_series(values, fast)
    slow_line = ema_series(values, slow)
    macd_line = [a - b for a, b in zip(fast_line, slow_line)]
    signal_line = ema_series(macd_line, signal)
    hist = macd_line[-1] - signal_line[-1]
    prev_hist = macd_line[-2] - signal_line[-2]
    return macd_line[-1], signal_line[-1], hist, prev_hist


def true_ranges(candles: list[Candle]) -> list[float]:
    if not candles:
        return []
    output = [candles[0].high - candles[0].low]
    for previous, current in zip(candles, candles[1:]):
        output.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return output


def atr(candles: list[Candle], period: int = 14) -> float:
    ranges = true_ranges(candles)
    if len(ranges) < period:
        return math.nan
    value = sum(ranges[:period]) / period
    for item in ranges[period:]:
        value = ((value * (period - 1)) + item) / period
    return value


def adx(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < (period * 2) + 1:
        return math.nan
    trs: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for previous, current in zip(candles, candles[1:]):
        up = current.high - previous.high
        down = previous.low - current.low
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        trs.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    smoothed_tr = sum(trs[:period])
    smoothed_plus = sum(plus_dm[:period])
    smoothed_minus = sum(minus_dm[:period])
    dx_values: list[float] = []
    for index in range(period, len(trs)):
        if index > period:
            smoothed_tr = smoothed_tr - (smoothed_tr / period) + trs[index]
            smoothed_plus = smoothed_plus - (smoothed_plus / period) + plus_dm[index]
            smoothed_minus = smoothed_minus - (smoothed_minus / period) + minus_dm[index]
        if smoothed_tr <= 0:
            continue
        plus_di = 100.0 * smoothed_plus / smoothed_tr
        minus_di = 100.0 * smoothed_minus / smoothed_tr
        denominator = plus_di + minus_di
        dx_values.append(0.0 if denominator == 0 else 100.0 * abs(plus_di - minus_di) / denominator)
    if len(dx_values) < period:
        return math.nan
    value = sum(dx_values[:period]) / period
    for item in dx_values[period:]:
        value = ((value * (period - 1)) + item) / period
    return value


@dataclass(frozen=True)
class TimeframeFeatures:
    close: float
    ema21: float
    ema55: float
    ema21_slope_atr: float
    sma5: float
    sma10: float
    sma20: float
    rsi14: float
    macd_line: float
    macd_signal: float
    macd_hist: float
    macd_prev_hist: float
    atr14: float
    adx14: float
    volume_ratio: float
    prior_high20: float
    prior_low20: float
    recent_high: float
    recent_low: float
    extension_atr: float
    compression_ratio: float
    vwap20: float
    bollinger_width_pct: float
    directional_volume_ratio: float
    lower_wick_ratio: float
    upper_wick_ratio: float
    atr_pct: float


def features(candles: list[Candle]) -> TimeframeFeatures:
    if len(candles) < 60:
        raise ValueError("at least 60 closed candles are required")
    closes = [item.close for item in candles]
    volumes = [item.quote_volume if item.quote_volume > 0 else item.volume for item in candles]
    ema21_values = ema_series(closes, 21)
    ema55_values = ema_series(closes, 55)
    current_atr = atr(candles, 14)
    macd_line, macd_signal, histogram, previous_histogram = macd(closes)
    prior_high20 = max(item.high for item in candles[-21:-1])
    prior_low20 = min(item.low for item in candles[-21:-1])
    recent_high = max(item.high for item in candles[-7:-1])
    recent_low = min(item.low for item in candles[-7:-1])
    previous_volume = sma(volumes[:-1], 20)
    volume_ratio = volumes[-1] / previous_volume if previous_volume > 0 else 0.0
    ranges = true_ranges(candles)
    recent_range = sma(ranges, 5)
    baseline_range = sma(ranges, 20)
    compression = recent_range / baseline_range if baseline_range > 0 else 1.0
    slope = (ema21_values[-1] - ema21_values[-6]) / current_atr if current_atr > 0 else 0.0
    extension = abs(closes[-1] - ema21_values[-1]) / current_atr if current_atr > 0 else float("inf")
    recent = candles[-20:]
    recent_volumes = volumes[-20:]
    typical_prices = [(item.high + item.low + item.close) / 3.0 for item in recent]
    volume_sum = sum(recent_volumes)
    vwap20 = (
        sum(price * volume for price, volume in zip(typical_prices, recent_volumes)) / volume_sum
        if volume_sum > 0
        else closes[-1]
    )
    mean20 = sma(closes, 20)
    variance20 = sum((value - mean20) ** 2 for value in closes[-20:]) / 20.0
    bollinger_width = (4.0 * math.sqrt(variance20) / mean20 * 100.0) if mean20 > 0 else 0.0
    signed_buy_volume = 0.0
    signed_total_volume = 0.0
    for candle, volume in zip(candles[-12:], volumes[-12:]):
        signed_total_volume += volume
        if candle.close > candle.open:
            signed_buy_volume += volume
        elif candle.close == candle.open:
            signed_buy_volume += volume * 0.5
    directional_volume_ratio = signed_buy_volume / signed_total_volume if signed_total_volume > 0 else 0.5
    latest = candles[-1]
    latest_range = max(latest.high - latest.low, 0.0)
    lower_wick_ratio = (
        (min(latest.open, latest.close) - latest.low) / latest_range
        if latest_range > 0
        else 0.0
    )
    upper_wick_ratio = (
        (latest.high - max(latest.open, latest.close)) / latest_range
        if latest_range > 0
        else 0.0
    )
    return TimeframeFeatures(
        close=closes[-1],
        ema21=ema21_values[-1],
        ema55=ema55_values[-1],
        ema21_slope_atr=slope,
        sma5=sma(closes, 5),
        sma10=sma(closes, 10),
        sma20=sma(closes, 20),
        rsi14=rsi(closes, 14),
        macd_line=macd_line,
        macd_signal=macd_signal,
        macd_hist=histogram,
        macd_prev_hist=previous_histogram,
        atr14=current_atr,
        adx14=adx(candles, 14),
        volume_ratio=volume_ratio,
        prior_high20=prior_high20,
        prior_low20=prior_low20,
        recent_high=recent_high,
        recent_low=recent_low,
        extension_atr=extension,
        compression_ratio=compression,
        vwap20=vwap20,
        bollinger_width_pct=bollinger_width,
        directional_volume_ratio=directional_volume_ratio,
        lower_wick_ratio=lower_wick_ratio,
        upper_wick_ratio=upper_wick_ratio,
        atr_pct=(current_atr / closes[-1] * 100.0) if closes[-1] > 0 else float("inf"),
    )
