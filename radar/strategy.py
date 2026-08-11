from __future__ import annotations

import math
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP

from .indicators import TimeframeFeatures, features
from .models import Candle, Instrument, MarketContext, MarketState, Signal, Ticker


@dataclass(frozen=True)
class StrategyConfig:
    min_quote_volume_24h: float = 10_000_000.0
    max_spread_pct: float = 0.10
    min_open_interest_usd: float = 3_000_000.0
    minimum_rr: float = 1.8


@dataclass
class AnalysisResult:
    signal: Signal | None
    reason: str
    market_state: MarketState | None = None


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
        quote_volume_24h = sum(item.quote_volume for item in candles_1h[-24:])

        tf4 = features(candles_4h)
        tf1 = features(candles_1h)
        tf15 = features(candles_15m)
        indicator_values = (tf4.atr14, tf1.atr14, tf15.atr14, tf1.adx14, tf15.rsi14)
        if not all(
            math.isfinite(value)
            for value in indicator_values
        ) or min(tf4.atr14, tf1.atr14, tf15.atr14) <= 0:
            return AnalysisResult(None, "indicator_unavailable")

        market_state = self._market_state(
            instrument,
            ticker,
            quote_volume_24h,
            candles_15m[-1].ts,
            tf4,
            tf1,
            tf15,
        )
        if ticker.spread_pct > self.config.max_spread_pct:
            return AnalysisResult(
                None,
                "spread_too_wide",
                replace(
                    market_state,
                    status="FILTERED",
                    missing_conditions=[
                        f"買賣價差需低於 {self.config.max_spread_pct:.2f}%",
                        *market_state.missing_conditions,
                    ][:4],
                ),
            )
        if quote_volume_24h < self.config.min_quote_volume_24h:
            return AnalysisResult(
                None,
                "liquidity_too_low",
                replace(
                    market_state,
                    status="FILTERED",
                    missing_conditions=[
                        f"24H 成交額需達 {self.config.min_quote_volume_24h:,.0f} USDT",
                        *market_state.missing_conditions,
                    ][:4],
                ),
            )

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
            missing = list(market_state.missing_conditions)
            readiness_score = market_state.readiness_score
            if market_state.readiness_score >= 99.0 or not missing:
                missing.append("止損距離或前方空間需通過最低 1.8R")
                readiness_score = min(readiness_score, 92.0)
            return AnalysisResult(
                None,
                "no_confirmed_setup",
                replace(
                    market_state,
                    status=(
                        "NEAR_TRIGGER"
                        if market_state.regime != "DISORDER"
                        and market_state.direction != "NEUTRAL"
                        and market_state.readiness_score >= 65.0
                        else "WATCH"
                    ),
                    readiness_score=readiness_score,
                    missing_conditions=missing[:4],
                ),
            )
        plan = max(valid, key=lambda item: item.score)
        if plan.rr < self.config.minimum_rr:
            return AnalysisResult(
                None,
                "rr_below_minimum",
                replace(
                    market_state,
                    status="WATCH",
                    missing_conditions=[f"風報比需達 {self.config.minimum_rr:.1f}R"],
                ),
            )
        risk_pct = abs(plan.entry - plan.stop) / plan.entry * 100.0
        if risk_pct <= 0 or risk_pct > 5.0:
            return AnalysisResult(
                None,
                "stop_distance_unacceptable",
                replace(market_state, status="WATCH", missing_conditions=["止損距離需低於價格的 5%"]),
            )
        if len(plan.evidence) < 2:
            return AnalysisResult(
                None,
                "insufficient_independent_evidence",
                replace(market_state, status="WATCH", missing_conditions=["至少需要兩類獨立證據"]),
            )

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
            factor_scores=dict(market_state.factor_scores),
            market_metrics=dict(market_state.market_metrics),
        )
        return AnalysisResult(
            signal,
            "qualified",
            replace(
                market_state,
                regime=plan.regime,
                direction=plan.direction,
                preferred_strategy=plan.strategy,
                readiness_score=100.0,
                status="CONFIRMED",
                missing_conditions=[],
            ),
        )

    def apply_market_context(
        self,
        result: AnalysisResult,
        context: MarketContext,
        btc_bias: str = "NEUTRAL",
    ) -> AnalysisResult:
        state = result.market_state
        if state is None:
            return result

        metrics = dict(state.market_metrics)
        metrics.update(
            {
                "open_interest_usd": context.open_interest_usd,
                "funding_rate_pct": (
                    round(context.funding_rate * 100.0, 5)
                    if context.funding_rate is not None
                    else None
                ),
                "order_book_imbalance_pct": (
                    round(context.order_book_imbalance * 100.0, 1)
                    if context.order_book_imbalance is not None
                    else None
                ),
                "taker_buy_pct": (
                    round(context.taker_buy_ratio * 100.0, 1)
                    if context.taker_buy_ratio is not None
                    else None
                ),
                "btc_bias": btc_bias,
                "context_complete": context.complete,
            }
        )
        factors = dict(state.factor_scores)
        direction = state.direction
        is_long = direction == "LONG"
        missing = list(state.missing_conditions)
        passed = list(state.passed_conditions)

        if self.config.min_open_interest_usd > 0 and (
            context.open_interest_usd is None
            or context.open_interest_usd < self.config.min_open_interest_usd
        ):
            factors["liquidity_risk"] = 0.0
            condition = (
                "無法取得持倉量，依流動性安全規則淘汰"
                if context.open_interest_usd is None
                else f"持倉量需達 {self.config.min_open_interest_usd:,.0f} USD"
            )
            return AnalysisResult(
                None,
                "open_interest_too_low",
                replace(
                    state,
                    status="FILTERED",
                    readiness_score=min(state.readiness_score, 49.0),
                    passed_conditions=_unique(passed)[:6],
                    missing_conditions=_unique([condition, *missing])[:6],
                    factor_scores=factors,
                    market_metrics=metrics,
                ),
            )

        if direction in ("LONG", "SHORT"):
            flow_parts: list[float] = []
            if context.taker_buy_ratio is not None:
                taker_score = (
                    context.taker_buy_ratio * 100.0
                    if is_long
                    else (1.0 - context.taker_buy_ratio) * 100.0
                )
                flow_parts.append(taker_score)
                taker_confirmed = (
                    context.taker_buy_ratio >= 0.52
                    if is_long
                    else context.taker_buy_ratio <= 0.48
                )
                (passed if taker_confirmed else missing).append(
                    "近期主動買賣方向需與訊號一致"
                )
            if context.order_book_imbalance is not None:
                signed_book = context.order_book_imbalance if is_long else -context.order_book_imbalance
                flow_parts.append(50.0 + (signed_book * 50.0))
                if signed_book >= 0.08:
                    passed.append("前 20 檔委託簿深度支持訊號方向")
                elif signed_book <= -0.08:
                    missing.append("委託簿深度目前與訊號方向相反")
            if flow_parts:
                live_flow_score = _clamp(sum(flow_parts) / len(flow_parts), 0.0, 100.0)
                factors["volume_order_flow"] = round(
                    (factors.get("volume_order_flow", 50.0) * 0.35) + (live_flow_score * 0.65),
                    1,
                )

            derivative_parts: list[float] = []
            crowded = False
            if context.funding_rate is not None:
                directional_funding = context.funding_rate if is_long else -context.funding_rate
                crowded = directional_funding > 0.0005
                if crowded:
                    derivative_parts.append(20.0)
                    missing.append("資金費率顯示同方向部位過度擁擠")
                elif directional_funding > 0.0002:
                    derivative_parts.append(55.0)
                else:
                    derivative_parts.append(80.0)
                    passed.append("資金費率未出現同方向過度擁擠")
            if context.open_interest_usd is not None:
                oi = context.open_interest_usd
                oi_score = 100.0 if oi >= 100_000_000 else 85.0 if oi >= 20_000_000 else 70.0 if oi >= 5_000_000 else 55.0 if oi >= 1_000_000 else 35.0
                derivative_parts.append(oi_score)
            if derivative_parts:
                factors["derivatives"] = round(sum(derivative_parts) / len(derivative_parts), 1)

            if btc_bias not in ("NEUTRAL", direction) and not state.inst_id.startswith("BTC-"):
                factors["liquidity_risk"] = max(
                    0.0,
                    factors.get("liquidity_risk", 50.0) - 15.0,
                )
                missing.append("BTC 大盤方向目前與此訊號相反")

            strong_flow_opposition = (
                context.taker_buy_ratio is not None
                and (
                    (is_long and context.taker_buy_ratio < 0.44)
                    or (not is_long and context.taker_buy_ratio > 0.56)
                )
            )
        else:
            crowded = False
            strong_flow_opposition = False

        overall = self._weighted_factor_score(factors)
        readiness = round((state.readiness_score * 0.60) + (overall * 0.40), 1)
        updated_state = replace(
            state,
            readiness_score=readiness,
            status=(
                state.status
                if state.status in ("FILTERED", "CONFIRMED")
                else "NEAR_TRIGGER" if readiness >= 70.0 else "WATCH"
            ),
            passed_conditions=_unique(passed)[:6],
            missing_conditions=_unique(missing)[:6],
            factor_scores=factors,
            market_metrics=metrics,
        )

        if result.signal is None:
            return AnalysisResult(None, result.reason, updated_state)

        if not context.complete or strong_flow_opposition or crowded:
            if not context.complete:
                missing = ["即時資金費率、訂單簿或主動成交資料未完整", *missing]
            updated_state = replace(
                updated_state,
                status="NEAR_TRIGGER",
                readiness_score=min(updated_state.readiness_score, 89.0),
                missing_conditions=_unique(missing)[:6],
            )
            return AnalysisResult(None, "market_context_not_confirmed", updated_state)

        evidence = list(result.signal.evidence)
        if context.taker_buy_ratio is not None:
            evidence.append(f"近期主動買方占比 {context.taker_buy_ratio * 100.0:.1f}%")
        if context.order_book_imbalance is not None:
            evidence.append(f"前 20 檔委託簿失衡 {context.order_book_imbalance * 100.0:+.1f}%")
        if context.funding_rate is not None:
            evidence.append(f"預估資金費率 {context.funding_rate * 100.0:+.4f}%")
        updated_signal = replace(
            result.signal,
            score=round((result.signal.score * 0.60) + (overall * 0.40), 1),
            evidence=_unique(evidence),
            factor_scores=factors,
            market_metrics=metrics,
        )
        updated_state = replace(
            updated_state,
            status="CONFIRMED",
            readiness_score=100.0,
            missing_conditions=[],
        )
        return AnalysisResult(updated_signal, "qualified", updated_state)

    @staticmethod
    def _weighted_factor_score(factors: dict[str, float]) -> float:
        weights = {
            "structure_trend": 0.24,
            "momentum": 0.14,
            "volatility": 0.12,
            "volume_order_flow": 0.20,
            "derivatives": 0.15,
            "liquidity_risk": 0.15,
        }
        available = [(factors[key], weight) for key, weight in weights.items() if key in factors]
        denominator = sum(weight for _, weight in available)
        return round(sum(score * weight for score, weight in available) / denominator, 1) if denominator > 0 else 0.0

    def _market_state(
        self,
        instrument: Instrument,
        ticker: Ticker,
        quote_volume_24h: float,
        closed_candle_ts: int,
        tf4: TimeframeFeatures,
        tf1: TimeframeFeatures,
        tf15: TimeframeFeatures,
    ) -> MarketState:
        bias4 = self._bias(tf4)
        trend1 = self._aligned_direction(tf1)
        width = tf1.prior_high20 - tf1.prior_low20
        range_position = (
            (tf1.close - tf1.prior_low20) / width
            if width > 0
            else 0.5
        )
        broke_high = tf1.close > tf1.prior_high20
        broke_low = tf1.close < tf1.prior_low20
        near_high = (tf1.prior_high20 - tf1.close) / tf1.atr14 <= 0.45
        near_low = (tf1.close - tf1.prior_low20) / tf1.atr14 <= 0.45
        compressed_at_edge = tf1.compression_ratio <= 0.90 and (near_high or near_low)

        if bias4 != "NEUTRAL" and trend1 == bias4:
            regime = "TREND"
            direction = bias4
            strategy = "趨勢回踩續行"
        elif broke_high or broke_low or compressed_at_edge:
            regime = "BREAKOUT_READY"
            if broke_high:
                direction = "LONG"
            elif broke_low:
                direction = "SHORT"
            elif bias4 != "NEUTRAL":
                direction = bias4
            else:
                direction = "LONG" if near_high and not near_low else "SHORT"
            strategy = "放量突破"
        elif tf1.adx14 <= 20.0 and width > 0:
            regime = "RANGE"
            if range_position <= 0.45:
                direction = "LONG"
            elif range_position >= 0.55:
                direction = "SHORT"
            else:
                direction = "NEUTRAL"
            strategy = "區間邊緣反轉"
        else:
            regime = "DISORDER"
            direction = bias4 if bias4 != "NEUTRAL" else trend1
            strategy = "等待型態清楚"

        condition_score, passed, missing = self._readiness(tf4, tf1, tf15, regime, direction)
        factor_scores = self._base_factor_scores(
            tf4,
            tf1,
            tf15,
            regime,
            direction,
            ticker.spread_pct,
            quote_volume_24h,
        )
        factor_score = self._weighted_factor_score(factor_scores)
        score = round((condition_score * 0.70) + (factor_score * 0.30), 1)
        return MarketState(
            inst_id=instrument.inst_id,
            regime=regime,
            direction=direction,
            preferred_strategy=strategy,
            readiness_score=score,
            status="WATCH",
            missing_conditions=missing[:4],
            spread_pct=round(ticker.spread_pct, 4),
            quote_volume_24h=round(quote_volume_24h, 2),
            closed_candle_ts=closed_candle_ts,
            passed_conditions=passed[:6],
            factor_scores=factor_scores,
            market_metrics={
                "adx_1h": round(tf1.adx14, 1),
                "rsi_15m": round(tf15.rsi14, 1),
                "volume_ratio_1h": round(tf1.volume_ratio, 2),
                "atr_pct_15m": round(tf15.atr_pct, 2),
                "bollinger_width_pct_1h": round(tf1.bollinger_width_pct, 2),
                "vwap_position_15m": "ABOVE" if tf15.close >= tf15.vwap20 else "BELOW",
                "candle_buy_pressure_pct": round(tf15.directional_volume_ratio * 100.0, 1),
            },
        )

    @staticmethod
    def _aligned_direction(tf: TimeframeFeatures) -> str:
        if tf.close > tf.ema21 > tf.ema55 and tf.sma5 > tf.sma10 > tf.sma20:
            return "LONG"
        if tf.close < tf.ema21 < tf.ema55 and tf.sma5 < tf.sma10 < tf.sma20:
            return "SHORT"
        return "NEUTRAL"

    def _base_factor_scores(
        self,
        tf4: TimeframeFeatures,
        tf1: TimeframeFeatures,
        tf15: TimeframeFeatures,
        regime: str,
        direction: str,
        spread_pct: float,
        quote_volume_24h: float,
    ) -> dict[str, float]:
        if direction not in ("LONG", "SHORT"):
            return {
                "structure_trend": 25.0 if regime == "RANGE" else 0.0,
                "momentum": 0.0,
                "volatility": 50.0,
                "volume_order_flow": 50.0,
                "liquidity_risk": self._liquidity_score(spread_pct, quote_volume_24h),
            }
        is_long = direction == "LONG"
        if regime == "RANGE":
            width = tf1.prior_high20 - tf1.prior_low20
            position = (tf1.close - tf1.prior_low20) / width if width > 0 else 0.5
            structure_score = (
                (35.0 if tf1.adx14 <= 20.0 else 0.0)
                + (35.0 if (position <= 0.22 if is_long else position >= 0.78) else 0.0)
                + (30.0 if self._bias(tf4) != ("SHORT" if is_long else "LONG") else 0.0)
            )
        else:
            structure_score = (
                (35.0 if self._bias(tf4) == direction else 0.0)
                + (30.0 if self._aligned_direction(tf1) == direction else 0.0)
                + (20.0 if regime in ("TREND", "BREAKOUT_READY") else 0.0)
                + (15.0 if ((tf1.close > tf1.ema21) if is_long else (tf1.close < tf1.ema21)) else 0.0)
            )
        macd_ok = tf15.macd_line > tf15.macd_signal if is_long else tf15.macd_line < tf15.macd_signal
        rsi_ok = tf15.rsi14 >= 50.0 if is_long else tf15.rsi14 <= 50.0
        ema_ok = tf15.close > tf15.ema21 if is_long else tf15.close < tf15.ema21
        vwap_ok = tf15.close > tf15.vwap20 if is_long else tf15.close < tf15.vwap20
        pressure = tf15.directional_volume_ratio if is_long else 1.0 - tf15.directional_volume_ratio
        momentum_score = (
            (30.0 if macd_ok else 0.0)
            + (25.0 if rsi_ok else 0.0)
            + (20.0 if ema_ok else 0.0)
            + (15.0 if vwap_ok else 0.0)
            + _clamp((pressure - 0.35) / 0.30 * 10.0, 0.0, 10.0)
        )
        compression_ok = tf1.compression_ratio <= 0.90 or tf1.bollinger_width_pct <= 5.0
        regime_volatility_ok = compression_ok if regime == "BREAKOUT_READY" else tf15.extension_atr <= 1.0
        volatility_score = (
            (35.0 if tf15.extension_atr <= 1.45 else 0.0)
            + (30.0 if 0.10 <= tf15.atr_pct <= 8.0 else 0.0)
            + (35.0 if regime_volatility_ok else 0.0)
        )
        volume_score = _clamp(tf1.volume_ratio / 1.25 * 60.0, 0.0, 60.0) + _clamp(
            (pressure - 0.35) / 0.30 * 40.0,
            0.0,
            40.0,
        )
        return {
            "structure_trend": round(structure_score, 1),
            "momentum": round(momentum_score, 1),
            "volatility": round(volatility_score, 1),
            "volume_order_flow": round(volume_score, 1),
            "liquidity_risk": self._liquidity_score(spread_pct, quote_volume_24h),
        }

    def _liquidity_score(self, spread_pct: float, quote_volume_24h: float) -> float:
        spread_score = _clamp(
            (1.0 - spread_pct / max(self.config.max_spread_pct, 0.0001)) * 100.0,
            0.0,
            100.0,
        )
        volume_score = _clamp(
            quote_volume_24h / max(self.config.min_quote_volume_24h * 5.0, 1.0) * 100.0,
            0.0,
            100.0,
        )
        return round((spread_score * 0.45) + (volume_score * 0.55), 1)

    def _readiness(
        self,
        tf4: TimeframeFeatures,
        tf1: TimeframeFeatures,
        tf15: TimeframeFeatures,
        regime: str,
        direction: str,
    ) -> tuple[float, list[str], list[str]]:
        if regime == "DISORDER" or direction == "NEUTRAL":
            return 0.0, [], ["等待 4H、1H 方向或區間位置清楚"]

        is_long = direction == "LONG"
        if regime == "TREND":
            conditions = [
                (self._bias(tf4) == direction, "4H 趨勢需同向"),
                (self._aligned_direction(tf1) == direction, "1H EMA21/55 與 MA5/10/20 需同向排列"),
                (tf1.adx14 >= 21.0, "1H ADX 需達 21"),
                (abs(tf15.close - tf15.ema21) <= 0.65 * tf15.atr14, "15m 需回到 EMA21 附近"),
                ((tf15.close > tf15.ema21) if is_long else (tf15.close < tf15.ema21), "15m 收盤需回到趨勢側"),
                ((tf15.macd_line > tf15.macd_signal) if is_long else (tf15.macd_line < tf15.macd_signal), "15m MACD 快慢線需回到同方向"),
                (tf15.extension_atr <= 0.75, "15m 不可離 EMA21 超過 0.75 ATR"),
            ]
        elif regime == "BREAKOUT_READY":
            conditions = [
                ((tf1.close > tf1.prior_high20) if is_long else (tf1.close < tf1.prior_low20), "1H 收盤需突破近 20 根結構邊界"),
                (self._bias(tf4) in (direction, "NEUTRAL"), "4H 不可與突破方向相反"),
                (tf1.volume_ratio >= 1.25, "1H 成交量需達基準 1.25 倍"),
                ((tf1.macd_line > tf1.macd_signal) if is_long else (tf1.macd_line < tf1.macd_signal), "1H MACD 快慢線需與突破同向"),
                ((tf15.close > tf15.ema21) if is_long else (tf15.close < tf15.ema21), "15m 收盤需站在 EMA21 趨勢側"),
                ((tf15.macd_line > tf15.macd_signal) if is_long else (tf15.macd_line < tf15.macd_signal), "15m MACD 快慢線需同向確認"),
                (tf1.extension_atr <= 1.45, "1H 不可過度追價"),
            ]
        else:
            width = tf1.prior_high20 - tf1.prior_low20
            position = (tf1.close - tf1.prior_low20) / width if width > 0 else 0.5
            conditions = [
                (tf1.adx14 <= 20.0, "1H ADX 需低於 20"),
                ((position <= 0.22) if is_long else (position >= 0.78), "價格需到達區間外側 22%"),
                ((tf1.rsi14 <= 40.0) if is_long else (tf1.rsi14 >= 60.0), "1H RSI 需進入反轉區"),
                (
                    (tf15.macd_prev_hist <= 0 < tf15.macd_hist)
                    if is_long
                    else (tf15.macd_prev_hist >= 0 > tf15.macd_hist),
                    "15m MACD 需完成反向交叉",
                ),
                ((tf15.close > tf15.ema21) if is_long else (tf15.close < tf15.ema21), "15m 收盤需穿回 EMA21"),
                (tf15.volume_ratio >= 1.05, "15m 成交量需達基準 1.05 倍"),
            ]

        passed = sum(1 for ok, _ in conditions if ok)
        score = round(passed / len(conditions) * 100.0, 1)
        return (
            score,
            [label for ok, label in conditions if ok],
            [label for ok, label in conditions if not ok],
        )

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
            momentum_votes = sum(
                (
                    tf1.macd_line > tf1.macd_signal,
                    tf1.rsi14 >= 52.0,
                    tf15.macd_line > tf15.macd_signal,
                    tf15.rsi14 >= 50.0,
                    tf15.close > tf15.vwap20,
                    tf15.directional_volume_ratio >= 0.52,
                )
            )
            if not (
                tf1.close > tf1.prior_high20
                and tf15.close > tf15.ema21
                and tf1.extension_atr <= 1.45
                and momentum_votes >= 3
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
            momentum_votes = sum(
                (
                    tf1.macd_line < tf1.macd_signal,
                    tf1.rsi14 <= 48.0,
                    tf15.macd_line < tf15.macd_signal,
                    tf15.rsi14 <= 50.0,
                    tf15.close < tf15.vwap20,
                    tf15.directional_volume_ratio <= 0.48,
                )
            )
            if not (
                tf1.close < tf1.prior_low20
                and tf15.close < tf15.ema21
                and tf1.extension_atr <= 1.45
                and momentum_votes >= 3
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
                "RSI、MACD 快慢線、VWAP 與量能至少三項同向",
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
            momentum_votes = sum(
                (
                    tf15.macd_line > tf15.macd_signal,
                    50.0 <= tf15.rsi14 <= 72.0,
                    tf15.close > tf15.vwap20,
                    tf15.directional_volume_ratio >= 0.52,
                    tf15.lower_wick_ratio >= 0.20,
                )
            )
            trigger = tf15.close > tf15.ema21 and momentum_votes >= 3
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
            momentum_votes = sum(
                (
                    tf15.macd_line < tf15.macd_signal,
                    28.0 <= tf15.rsi14 <= 50.0,
                    tf15.close < tf15.vwap20,
                    tf15.directional_volume_ratio <= 0.48,
                    tf15.upper_wick_ratio >= 0.20,
                )
            )
            trigger = tf15.close < tf15.ema21 and momentum_votes >= 3
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
                "15m RSI、MACD、VWAP、K 棒與量能至少三項同向",
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
            reversal_votes = sum(
                (
                    tf15.macd_prev_hist <= 0 < tf15.macd_hist,
                    tf15.lower_wick_ratio >= 0.28,
                    tf15.close > tf15.ema21,
                    tf15.close > tf15.vwap20,
                    tf15.directional_volume_ratio >= 0.52,
                )
            )
            if not (location_ok and reversal_votes >= 3 and tf15.volume_ratio >= 0.90):
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
            reversal_votes = sum(
                (
                    tf15.macd_prev_hist >= 0 > tf15.macd_hist,
                    tf15.upper_wick_ratio >= 0.28,
                    tf15.close < tf15.ema21,
                    tf15.close < tf15.vwap20,
                    tf15.directional_volume_ratio <= 0.48,
                )
            )
            if not (location_ok and reversal_votes >= 3 and tf15.volume_ratio >= 0.90):
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
                "15m K 棒、MACD、VWAP 與量能至少三項反轉確認",
            ],
            entry=entry,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            rr=rr,
            invalidation=invalidation,
            notes=["逆向反轉風險較高，倉位應低於趨勢訊號。", "只使用已收盤 K 線。"],
        )


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(value, maximum))


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _format_price(value: float, tick_size: float) -> str:
    if tick_size <= 0:
        return f"{value:.8f}".rstrip("0").rstrip(".")
    tick = Decimal(str(tick_size))
    price = Decimal(str(value))
    rounded = (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick
    decimal_places = max(0, -tick.as_tuple().exponent)
    return f"{rounded:.{decimal_places}f}"
