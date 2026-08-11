from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from .indicators import TimeframeFeatures, features
from .models import Candle, Instrument, Signal, Ticker


@dataclass(frozen=True)
class StrategyConfig:
    min_quote_volume_24h: float = 1_000_000.0
    max_spread_pct: float = 0.25
    minimum_rr: float = 1.8


@dataclass
class AnalysisResult:
    signal: Signal | None
    reason: str


@dataclass
class _Plan:
    direction: str
    strategy: str
    regime: str
    score: float
    evidence: list[str]
    entry: float
    stop: float
    tp1: float
    tp2: float
    rr: float
    invalidation: str
    notes: list[str]


class AdaptiveStrategyEngine:
    """Selects a conservative regime-specific setup for each instrument."""

    def __init__(self, config: StrategyConfig | None = None):
        self.config = config or StrategyConfig()

    def analyze(
        self,
        instrument: Instrument,
        ticker: Ticker,
        candles_4h: list[Candle],
        candles_1h: list[Candle],
        candles_15m: list[Candle],
    ) -> AnalysisResult:
        if min(len(candles_4h), len(candles_1h), len(candles_15m)) < 60:
            return AnalysisResult(None, "insufficient_history")
        if ticker.last <= 0 or ticker.bid <= 0 or ticker.ask <= 0:
            return AnalysisResult(None, "invalid_ticker")
        if ticker.spread_pct > self.config.max_spread_pct:
            return AnalysisResult(None, "spread_too_wide")
        quote_volume_24h = sum(item.quote_volume for item in candles_1h[-24:])
        if quote_volume_24h < self.config.min_quote_volume_24h:
            return AnalysisResult(None, "liquidity_too_low")

        tf4 = features(candles_4h)
        tf1 = features(candles_1h)
        tf15 = features(candles_15m)
        if not all(
            math.isfinite(value)
            for value in (tf4.atr14, tf1.atr14, tf15.atr14, tf1.adx14, tf15.rsi14)
        ):
            return AnalysisResult(None, "indicator_unavailable")

        plans = [
            self._breakout_plan("LONG", tf4, tf1, tf15),
            self._breakout_plan("SHORT", tf4, tf1, tf15),
            self._trend_pullback_plan("LONG", tf4, tf1, tf15),
            self._trend_pullback_plan("SHORT", tf4, tf1, tf15),
            self._range_reversal_plan("LONG", tf4, tf1, tf15),
            self._range_reversal_plan("SHORT", tf4, tf1, tf15),
        ]
        valid = [item for item in plans if item is not None]
        if not valid:
            return AnalysisResult(None, "no_confirmed_setup")
        plan = max(valid, key=lambda item: item.score)
        if plan.rr < self.config.minimum_rr:
            return AnalysisResult(None, "rr_below_minimum")
        risk_pct = abs(plan.entry - plan.stop) / plan.entry * 100.0
        if risk_pct <= 0 or risk_pct > 5.0:
            return AnalysisResult(None, "stop_distance_unacceptable")
        if len(plan.evidence) < 2:
            return AnalysisResult(None, "insufficient_independent_evidence")

        zone_offset = tf15.atr14 * 0.12
        if plan.direction == "LONG":
            entry_low = plan.entry - zone_offset
            entry_high = plan.entry + (zone_offset * 0.45)
        else:
            entry_low = plan.entry - (zone_offset * 0.45)
            entry_high = plan.entry + zone_offset
        signal = Signal(
            inst_id=instrument.inst_id,
            direction=plan.direction,
            strategy=plan.strategy,
            score=round(min(plan.score, 99.0), 1),
            evidence=plan.evidence,
            entry_low=_format_price(entry_low, instrument.tick_size),
            entry_high=_format_price(entry_high, instrument.tick_size),
            stop_loss=_format_price(plan.stop, instrument.tick_size),
            take_profit_1=_format_price(plan.tp1, instrument.tick_size),
            take_profit_2=_format_price(plan.tp2, instrument.tick_size),
            risk_reward=round(plan.rr, 2),
            invalidation=plan.invalidation,
            spread_pct=round(ticker.spread_pct, 4),
            quote_volume_24h=round(quote_volume_24h, 2),
            closed_candle_ts=candles_15m[-1].ts,
            regime=plan.regime,
            notes=plan.notes,
        )
        return AnalysisResult(signal, "qualified")

    @staticmethod
    def _bias(tf: TimeframeFeatures) -> str:
        if tf.close > tf.ema21 > tf.ema55 and tf.ema21_slope_atr > 0.10:
            return "LONG"
        if tf.close < tf.ema21 < tf.ema55 and tf.ema21_slope_atr < -0.10:
            return "SHORT"
        return "NEUTRAL"

    def _breakout_plan(
        self,
        direction: str,
        tf4: TimeframeFeatures,
        tf1: TimeframeFeatures,
        tf15: TimeframeFeatures,
    ) -> _Plan | None:
        if self._bias(tf4) != direction or tf1.adx14 < 20.0 or tf1.volume_ratio < 1.25:
            return None
        if direction == "LONG":
            if not (
                tf1.close > tf1.prior_high20
                and tf1.macd_hist > 0
                and tf15.close > tf15.ema21
                and tf15.macd_hist > 0
                and tf1.extension_atr <= 1.45
            ):
                return None
            entry = tf15.close
            stop = min(tf1.prior_high20 - (0.25 * tf1.atr14), entry - (1.2 * tf15.atr14))
            risk = entry - stop
            if risk <= 0:
                return None
            tp1 = entry + (risk * 2.0)
            tp2 = entry + (risk * 2.7)
            invalidation = "15m 收盤重新跌回突破位下方，或觸及止損。"
        else:
            if not (
                tf1.close < tf1.prior_low20
                and tf1.macd_hist < 0
                and tf15.close < tf15.ema21
                and tf15.macd_hist < 0
                and tf1.extension_atr <= 1.45
            ):
                return None
            entry = tf15.close
            stop = max(tf1.prior_low20 + (0.25 * tf1.atr14), entry + (1.2 * tf15.atr14))
            risk = stop - entry
            if risk <= 0:
                return None
            tp1 = entry - (risk * 2.0)
            tp2 = entry - (risk * 2.7)
            invalidation = "15m 收盤重新站回跌破位上方，或觸及止損。"
        score = 72.0 + min(tf1.volume_ratio, 3.0) * 4.5 + min(max(tf1.adx14 - 20.0, 0.0), 25.0) * 0.35
        return _Plan(
            direction=direction,
            strategy="放量突破",
            regime="BREAKOUT",
            score=score,
            evidence=[
                "4H 趨勢背景同向",
                "1H 收盤突破近 20 根結構邊界",
                f"1H 成交量為基準的 {tf1.volume_ratio:.2f} 倍",
                "15m 動能同向確認",
            ],
            entry=entry,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            rr=2.0,
            invalidation=invalidation,
            notes=["只使用已收盤 K 線。", "若進場前價格再延伸超過 0.5 ATR，放棄追價。"],
        )

    def _trend_pullback_plan(
        self,
        direction: str,
        tf4: TimeframeFeatures,
        tf1: TimeframeFeatures,
        tf15: TimeframeFeatures,
    ) -> _Plan | None:
        if self._bias(tf4) != direction or tf1.adx14 < 21.0:
            return None
        near_fast_average = abs(tf15.close - tf15.ema21) <= (0.65 * tf15.atr14)
        if direction == "LONG":
            aligned = tf1.close > tf1.ema21 > tf1.ema55 and tf1.sma5 > tf1.sma10 > tf1.sma20
            trigger = tf15.close > tf15.ema21 and tf15.macd_hist > tf15.macd_prev_hist and 47 <= tf15.rsi14 <= 72
            if not (aligned and trigger and near_fast_average and tf15.extension_atr <= 0.75):
                return None
            entry = tf15.close
            stop = min(tf15.recent_low - (0.20 * tf15.atr14), entry - (1.25 * tf15.atr14))
            risk = entry - stop
            if risk <= 0:
                return None
            obstacle = min(
                [level for level in (tf1.prior_high20, tf4.prior_high20) if level > entry],
                default=float("inf"),
            )
            if math.isfinite(obstacle) and (obstacle - entry) / risk < self.config.minimum_rr:
                return None
            tp1 = entry + (risk * 1.9)
            tp2 = entry + (risk * 2.6)
            invalidation = "15m 跌破回踩低點且收在 EMA21 下方，或觸及止損。"
        else:
            aligned = tf1.close < tf1.ema21 < tf1.ema55 and tf1.sma5 < tf1.sma10 < tf1.sma20
            trigger = tf15.close < tf15.ema21 and tf15.macd_hist < tf15.macd_prev_hist and 28 <= tf15.rsi14 <= 53
            if not (aligned and trigger and near_fast_average and tf15.extension_atr <= 0.75):
                return None
            entry = tf15.close
            stop = max(tf15.recent_high + (0.20 * tf15.atr14), entry + (1.25 * tf15.atr14))
            risk = stop - entry
            if risk <= 0:
                return None
            obstacle = max(
                [level for level in (tf1.prior_low20, tf4.prior_low20) if level < entry],
                default=-float("inf"),
            )
            if math.isfinite(obstacle) and (entry - obstacle) / risk < self.config.minimum_rr:
                return None
            tp1 = entry - (risk * 1.9)
            tp2 = entry - (risk * 2.6)
            invalidation = "15m 站回回踩高點且收在 EMA21 上方，或觸及止損。"
        score = 68.0 + min(max(tf1.adx14 - 20.0, 0.0), 30.0) * 0.45 + min(tf1.volume_ratio, 2.0) * 3.0
        return _Plan(
            direction=direction,
            strategy="趨勢回踩續行",
            regime="TREND",
            score=score,
            evidence=[
                "4H 與 1H 趨勢排列同向",
                "15m 回到 EMA21 附近，沒有過度延伸",
                "15m MACD 動能重新轉向",
            ],
            entry=entry,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            rr=1.9,
            invalidation=invalidation,
            notes=["只使用已收盤 K 線。", "若未回到進場區，不追價。"],
        )

    def _range_reversal_plan(
        self,
        direction: str,
        tf4: TimeframeFeatures,
        tf1: TimeframeFeatures,
        tf15: TimeframeFeatures,
    ) -> _Plan | None:
        if tf1.adx14 > 20.0 or self._bias(tf4) == ("SHORT" if direction == "LONG" else "LONG"):
            return None
        width = tf1.prior_high20 - tf1.prior_low20
        if width <= 0:
            return None
        position = (tf1.close - tf1.prior_low20) / width
        if direction == "LONG":
            location_ok = position <= 0.22 and tf1.rsi14 <= 40.0
            reversal = tf15.macd_prev_hist <= 0 < tf15.macd_hist and tf15.close > tf15.ema21
            if not (location_ok and reversal and tf15.volume_ratio >= 1.05):
                return None
            entry = tf15.close
            stop = tf1.prior_low20 - (0.25 * tf1.atr14)
            risk = entry - stop
            tp1 = tf1.prior_low20 + (width * 0.55)
            tp2 = tf1.prior_low20 + (width * 0.85)
            rr = (tp1 - entry) / risk if risk > 0 else 0.0
            invalidation = "1H 收盤有效跌破區間下緣，或觸及止損。"
        else:
            location_ok = position >= 0.78 and tf1.rsi14 >= 60.0
            reversal = tf15.macd_prev_hist >= 0 > tf15.macd_hist and tf15.close < tf15.ema21
            if not (location_ok and reversal and tf15.volume_ratio >= 1.05):
                return None
            entry = tf15.close
            stop = tf1.prior_high20 + (0.25 * tf1.atr14)
            risk = stop - entry
            tp1 = tf1.prior_low20 + (width * 0.45)
            tp2 = tf1.prior_low20 + (width * 0.15)
            rr = (entry - tp1) / risk if risk > 0 else 0.0
            invalidation = "1H 收盤有效突破區間上緣，或觸及止損。"
        if rr < self.config.minimum_rr:
            return None
        score = 64.0 + min(rr, 3.0) * 5.0 + min(tf15.volume_ratio, 2.0) * 3.0
        return _Plan(
            direction=direction,
            strategy="區間邊緣反轉",
            regime="RANGE",
            score=score,
            evidence=[
                "1H 低 ADX，判定為區間而非追趨勢",
                "價格位於近 20 根區間極端位置",
                "15m MACD 反轉並有量能確認",
            ],
            entry=entry,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            rr=rr,
            invalidation=invalidation,
            notes=["逆向反轉風險較高，倉位應低於趨勢訊號。", "只使用已收盤 K 線。"],
        )


def _format_price(value: float, tick_size: float) -> str:
    if tick_size <= 0:
        return f"{value:.8f}".rstrip("0").rstrip(".")
    tick = Decimal(str(tick_size))
    price = Decimal(str(value))
    rounded = (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick
    decimal_places = max(0, -tick.as_tuple().exponent)
    return f"{rounded:.{decimal_places}f}"
