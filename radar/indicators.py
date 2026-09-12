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


def _ema_if_ready(values: list[float], period: int) -> float:
    """Return an EMA only when the requested period exists in the source.

    Long tunnel-style EMAs must never be fabricated from a short candle
    window.  The ordinary 7/12/21/55 EMAs still use the same EMA seed as the
    rest of the radar once their full requested period is present.
    """

    if period <= 0 or len(values) < period:
        return math.nan
    return ema_series(values, period)[-1]


def _rsi_series(values: list[float], period: int = 14) -> list[float]:
    """Wilder RSI series after the first complete RSI window."""

    if period <= 0 or len(values) <= period:
        return []
    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def value() -> float:
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        relative = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + relative))

    output = [value()]
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        output.append(value())
    return output


def _wilder_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 1.0 / max(period, 1)
    output = [values[0]]
    for value in values[1:]:
        output.append((value * alpha) + (output[-1] * (1.0 - alpha)))
    return output


def rsi(values: list[float], period: int = 14) -> float:
    series = _rsi_series(values, period)
    return series[-1] if series else math.nan


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


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _dynamic_rsi_persistence(closes: list[float]) -> tuple[float, float, float]:
    """Return smoothed RSI, dynamic band width, and bullish persistence score.

    This keeps the useful concept of a volatility-adjusted, smoothed RSI
    confirmation without adding a second independent RSI vote.  It is one
    component inside the fused trend/momentum description only.
    """

    raw = _rsi_series(closes, 14)
    if len(raw) < 8:
        return math.nan, math.nan, 50.0
    smooth = ema_series(raw, 5)
    changes = [abs(current - previous) for previous, current in zip(smooth, smooth[1:])]
    if not changes:
        return smooth[-1], 0.0, 50.0
    volatility = _wilder_series(_wilder_series(changes, 27), 27)
    band = max(volatility[-1] * 4.236, 0.25)
    current = smooth[-1]
    previous = smooth[-2]
    delta = current - previous
    distance = current - 50.0
    directional = 50.0 + (distance * 1.35) + (delta * 3.0)
    if abs(distance) <= band:
        directional = 50.0 + (directional - 50.0) * 0.55
    return current, band, _clamp(directional)


def _fusion_components(
    candles: list[Candle],
    closes: list[float],
    ema21_values: list[float],
    ema55_values: list[float],
    current_atr: float,
    histogram: float,
    previous_histogram: float,
) -> dict[str, float | bool]:
    """Build a non-gating fused core view from trend, retest and persistence.

    The score is descriptive telemetry.  Trigger creation, Entry permission,
    SL/TP geometry and Signal Episode transitions do not read it.
    """

    ema7_values = ema_series(closes, 7)
    ema12_values = ema_series(closes, 12)
    ema144 = _ema_if_ready(closes, 144)
    ema169 = _ema_if_ready(closes, 169)
    ema576 = _ema_if_ready(closes, 576)
    ema676 = _ema_if_ready(closes, 676)
    medium_available = math.isfinite(ema144) and math.isfinite(ema169)
    deep_available = math.isfinite(ema576) and math.isfinite(ema676)
    atr_value = max(current_atr, abs(closes[-1]) * 0.0001, 1e-9)

    short_trend = 50.0
    short_trend += _clamp((ema21_values[-1] - ema55_values[-1]) / atr_value * 9.0, -16.0, 16.0)
    short_trend += _clamp((ema21_values[-1] - ema21_values[-6]) / atr_value * 8.0, -10.0, 10.0)
    short_trend = _clamp(short_trend)

    trend_score = short_trend
    if medium_available:
        medium = 50.0
        upper = max(ema144, ema169)
        lower = min(ema144, ema169)
        if closes[-1] > upper:
            medium += 16.0
        elif closes[-1] < lower:
            medium -= 16.0
        medium += 12.0 if ema144 > ema169 else -12.0 if ema144 < ema169 else 0.0
        trend_score = (short_trend * 0.55) + (_clamp(medium) * 0.45)
    if deep_available:
        deep = 50.0
        upper = max(ema576, ema676)
        lower = min(ema576, ema676)
        if closes[-1] > upper:
            deep += 14.0
        elif closes[-1] < lower:
            deep -= 14.0
        deep += 10.0 if ema576 > ema676 else -10.0 if ema576 < ema676 else 0.0
        trend_score = (trend_score * 0.80) + (_clamp(deep) * 0.20)

    current_ema12 = ema12_values[-1]
    previous_ema12 = ema12_values[-2]
    latest = candles[-1]
    previous = candles[-2]
    if latest.close >= current_ema12:
        retest_score = 62.0
        if previous.close < previous_ema12:
            retest_score = 76.0
        if latest.low <= current_ema12 <= latest.close:
            retest_score = max(retest_score, 80.0)
    else:
        retest_score = 38.0
        if previous.close > previous_ema12:
            retest_score = 24.0
        if latest.high >= current_ema12 >= latest.close:
            retest_score = min(retest_score, 20.0)

    smooth_rsi, dynamic_band, rsi_persistence = _dynamic_rsi_persistence(closes)
    macd_scale = max(abs(histogram), abs(previous_histogram), abs(closes[-1]) * 1e-8, 1e-12)
    macd_delta = (histogram - previous_histogram) / macd_scale
    macd_score = _clamp(50.0 + math.tanh(histogram / macd_scale) * 18.0 + math.tanh(macd_delta) * 10.0)
    momentum_score = (rsi_persistence * 0.65) + (macd_score * 0.35)

    current_ema7 = ema7_values[-1]
    previous_ema7 = ema7_values[-2]
    if latest.close >= current_ema7:
        fast_score = 60.0
        if previous.close < previous_ema7:
            fast_score = 72.0
    else:
        fast_score = 40.0
        if previous.close > previous_ema7:
            fast_score = 28.0

    fusion_long_score = _clamp(
        (trend_score * 0.40)
        + (retest_score * 0.25)
        + (momentum_score * 0.25)
        + (fast_score * 0.10)
    )
    return {
        "ema7": ema7_values[-1],
        "ema12": ema12_values[-1],
        "ema144": ema144,
        "ema169": ema169,
        "ema576": ema576,
        "ema676": ema676,
        "smoothed_rsi": smooth_rsi,
        "rsi_dynamic_band": dynamic_band,
        "fusion_trend_score": _clamp(trend_score),
        "fusion_retest_score": _clamp(retest_score),
        "fusion_momentum_score": _clamp(momentum_score),
        "fusion_fast_score": _clamp(fast_score),
        "fusion_long_score": fusion_long_score,
        "fusion_medium_tunnel_available": medium_available,
        "fusion_deep_tunnel_available": deep_available,
    }


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
    prior_high50: float
    prior_low50: float
    prior_high100: float
    prior_low100: float
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
    # Hidden/non-gating fusion telemetry.  Defaults preserve tests and any
    # callers that construct TimeframeFeatures manually.
    ema7: float = math.nan
    ema12: float = math.nan
    ema144: float = math.nan
    ema169: float = math.nan
    ema576: float = math.nan
    ema676: float = math.nan
    smoothed_rsi: float = math.nan
    rsi_dynamic_band: float = math.nan
    fusion_trend_score: float = 50.0
    fusion_retest_score: float = 50.0
    fusion_momentum_score: float = 50.0
    fusion_fast_score: float = 50.0
    fusion_long_score: float = 50.0
    fusion_medium_tunnel_available: bool = False
    fusion_deep_tunnel_available: bool = False


def features(candles: list[Candle]) -> TimeframeFeatures:
    if len(candles) < 60:
        raise ValueError("at least 60 closed candles are required")
    closes = [item.close for item in candles]
    volumes = [item.quote_volume if item.quote_volume > 0 else item.volume for item in candles]
    ema21_values = ema_series(closes, 21)
    ema55_values = ema_series(closes, 55)
    current_atr = atr(candles, 14)
    macd_line, macd_signal, histogram, previous_histogram = macd(closes)
    fusion = _fusion_components(
        candles,
        closes,
        ema21_values,
        ema55_values,
        current_atr,
        histogram,
        previous_histogram,
    )
    history = candles[:-1]

    def prior_bounds(lookback: int) -> tuple[float, float]:
        window = history[-min(lookback, len(history)):]
        return max(item.high for item in window), min(item.low for item in window)

    prior_high20, prior_low20 = prior_bounds(20)
    prior_high50, prior_low50 = prior_bounds(50)
    prior_high100, prior_low100 = prior_bounds(100)
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
        prior_high50=prior_high50,
        prior_low50=prior_low50,
        prior_high100=prior_high100,
        prior_low100=prior_low100,
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
        ema7=float(fusion["ema7"]),
        ema12=float(fusion["ema12"]),
        ema144=float(fusion["ema144"]),
        ema169=float(fusion["ema169"]),
        ema576=float(fusion["ema576"]),
        ema676=float(fusion["ema676"]),
        smoothed_rsi=float(fusion["smoothed_rsi"]),
        rsi_dynamic_band=float(fusion["rsi_dynamic_band"]),
        fusion_trend_score=float(fusion["fusion_trend_score"]),
        fusion_retest_score=float(fusion["fusion_retest_score"]),
        fusion_momentum_score=float(fusion["fusion_momentum_score"]),
        fusion_fast_score=float(fusion["fusion_fast_score"]),
        fusion_long_score=float(fusion["fusion_long_score"]),
        fusion_medium_tunnel_available=bool(fusion["fusion_medium_tunnel_available"]),
        fusion_deep_tunnel_available=bool(fusion["fusion_deep_tunnel_available"]),
    )
