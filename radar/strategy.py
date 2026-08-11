from __future__ import annotations

import math
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP

from .indicators import TimeframeFeatures, features
from .models import Candle, Instrument, MarketContext, MarketState, Signal, Ticker


@dataclass(frozen=True)
class StrategyConfig:
    min_quote_volume_24h: float = 5_000_000.0
    max_spread_pct: float = 0.10
    min_open_interest_usd: float = 3_000_000.0
    require_micro_volume_anomaly: bool = True
    minimum_rr: float = 1.8
    estimated_taker_fee_pct: float = 0.05
    max_execution_cost_to_risk_pct: float = 12.0
    max_entry_extension_atr: float = 0.80


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
    signal_stage: str = "CONFIRMED"
    trend_strength_label: str = "中等"
    trend_strength_score: float = 50.0
    management_plan: dict[str, object] | None = None


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
            self._early_expansion_plan("LONG", tf4, tf1, tf15),
            self._early_expansion_plan("SHORT", tf4, tf1, tf15),
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
        plan = self._adapt_trade_management(plan, tf4, tf1, tf15)
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
        if len(plan.evidence) < 3:
            return AnalysisResult(
                None,
                "insufficient_independent_evidence",
                replace(market_state, status="WATCH", missing_conditions=["至少需要三組獨立證據"]),
            )

        zone_offset = tf15.atr14 * 0.12
        if plan.direction == "LONG":
            entry_low = plan.entry - zone_offset
            entry_high = plan.entry + (zone_offset * 0.45)
        else:
            entry_low = plan.entry - (zone_offset * 0.45)
            entry_high = plan.entry + zone_offset
        signal_metrics = dict(market_state.market_metrics)
        signal_metrics.update(
            {
                "signal_stage": plan.signal_stage,
                "trend_strength_score": round(plan.trend_strength_score, 1),
                "trend_strength_label": plan.trend_strength_label,
                "technical_stop_pct": round(risk_pct, 4),
                "entry_extension_atr": round(tf15.extension_atr, 2),
            }
        )
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
            market_metrics=signal_metrics,
            signal_stage=plan.signal_stage,
            trend_strength_label=plan.trend_strength_label,
            trend_strength_score=round(plan.trend_strength_score, 1),
            management_plan=dict(plan.management_plan or {}),
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
                market_metrics=signal_metrics,
            ),
        )

    def apply_market_context(
        self,
        result: AnalysisResult,
        context: MarketContext,
        btc_bias: str = "NEUTRAL",
        candles_5m: list[Candle] | None = None,
        market_bias: dict[str, object] | None = None,
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
                "execution_notional_usdt": context.execution_notional_usdt,
                "bid_depth_usd": context.bid_depth_usd,
                "ask_depth_usd": context.ask_depth_usd,
                "buy_slippage_pct": context.buy_slippage_pct,
                "sell_slippage_pct": context.sell_slippage_pct,
                "execution_quality_complete": context.execution_quality_complete,
            }
        )
        factors = dict(state.factor_scores)
        direction = state.direction
        is_long = direction == "LONG"
        missing = list(state.missing_conditions)
        passed = list(state.passed_conditions)
        market_bias = market_bias or {"score": 50.0, "label": "中性"}
        market_bias_score = float(market_bias.get("score", 50.0))
        directional_market_score = (
            market_bias_score
            if is_long
            else 100.0 - market_bias_score
            if direction == "SHORT"
            else 50.0
        )
        factors["market_bias"] = round(directional_market_score, 1)
        metrics.update(
            {
                "bull_bear_score": round(market_bias_score, 1),
                "bull_bear_label": str(market_bias.get("label", "中性")),
            }
        )

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

        execution_cost_pct: float | None = None
        execution_cost_to_risk_pct: float | None = None
        execution_quality_label = (
            "良好"
            if state.spread_pct <= 0.03
            else "正常"
            if state.spread_pct <= 0.06
            else "成本偏高"
        )
        if direction in ("LONG", "SHORT") and context.execution_notional_usdt > 0:
            if context.execution_quality_complete:
                entry_slippage = (
                    context.buy_slippage_pct
                    if direction == "LONG"
                    else context.sell_slippage_pct
                )
                exit_slippage = (
                    context.sell_slippage_pct
                    if direction == "LONG"
                    else context.buy_slippage_pct
                )
                execution_cost_pct = round(
                    state.spread_pct
                    + float(entry_slippage or 0.0)
                    + float(exit_slippage or 0.0)
                    + (self.config.estimated_taker_fee_pct * 2.0),
                    4,
                )
                technical_stop_pct = float(metrics.get("technical_stop_pct", 0.0) or 0.0)
                if technical_stop_pct > 0:
                    execution_cost_to_risk_pct = round(
                        execution_cost_pct / technical_stop_pct * 100.0,
                        1,
                    )
                    if execution_cost_to_risk_pct > 10.0:
                        execution_quality_label = "成本偏高"
                quality_score = _clamp(
                    100.0
                    - (state.spread_pct / max(self.config.max_spread_pct, 0.0001) * 45.0)
                    - ((execution_cost_to_risk_pct or 0.0) * 3.0),
                    0.0,
                    100.0,
                )
                factors["execution_quality"] = round(quality_score, 1)
                if execution_cost_to_risk_pct is not None:
                    if execution_cost_to_risk_pct <= 10.0:
                        passed.append("預估來回交易成本低於原始風險的 10%")
                    else:
                        missing.append("預估交易成本已吃掉超過原始風險的 10%")
            elif result.signal is not None:
                metrics.update(
                    {
                        "execution_quality_label": "深度不足",
                        "execution_cost_pct": None,
                        "execution_cost_to_risk_pct": None,
                    }
                )
                return AnalysisResult(
                    None,
                    "execution_quality_unavailable",
                    replace(
                        state,
                        status="FILTERED",
                        readiness_score=min(state.readiness_score, 49.0),
                        passed_conditions=_unique(passed)[:6],
                        missing_conditions=_unique(
                            [
                                f"前 20 檔深度不足以估算 {context.execution_notional_usdt:,.0f} USDT 市價成交",
                                *missing,
                            ]
                        )[:6],
                        factor_scores=factors,
                        market_metrics=metrics,
                    ),
                )
        metrics.update(
            {
                "execution_quality_label": execution_quality_label,
                "estimated_round_trip_cost_pct": execution_cost_pct,
                "execution_cost_to_risk_pct": execution_cost_to_risk_pct,
                "estimated_taker_fee_pct_each_side": self.config.estimated_taker_fee_pct,
            }
        )
        if (
            result.signal is not None
            and execution_cost_to_risk_pct is not None
            and execution_cost_to_risk_pct > self.config.max_execution_cost_to_risk_pct
        ):
            return AnalysisResult(
                None,
                "execution_cost_too_high",
                replace(
                    state,
                    status="FILTERED",
                    readiness_score=min(state.readiness_score, 49.0),
                    passed_conditions=_unique(passed)[:6],
                    missing_conditions=_unique(
                        [
                            f"預估交易成本占原始風險 {execution_cost_to_risk_pct:.1f}%，超過 {self.config.max_execution_cost_to_risk_pct:.1f}% 上限",
                            *missing,
                        ]
                    )[:6],
                    factor_scores=factors,
                    market_metrics=metrics,
                ),
            )

        micro_confirmed = not self.config.require_micro_volume_anomaly
        micro_ratio_5m: float | None = None
        micro_pressure_pct: float | None = None
        if candles_5m is not None and len(candles_5m) >= 60 and direction in ("LONG", "SHORT"):
            tf5 = features(candles_5m)
            micro_ratio_5m = round(tf5.volume_ratio, 2)
            micro_pressure = tf5.directional_volume_ratio if is_long else 1.0 - tf5.directional_volume_ratio
            micro_pressure_pct = round(tf5.directional_volume_ratio * 100.0, 1)
            ratio_15m = float(metrics.get("volume_ratio_15m", 0.0))
            if metrics.get("signal_stage") == "EARLY":
                volume_move = tf5.volume_ratio >= 1.25 or ratio_15m >= 1.10
            elif state.regime in ("BREAKOUT", "BREAKOUT_READY"):
                volume_move = tf5.volume_ratio >= 1.50 or ratio_15m >= 1.30
            elif state.regime == "TREND":
                volume_move = tf5.volume_ratio >= 1.25 or ratio_15m >= 1.15
            else:
                volume_move = tf5.volume_ratio >= 1.20 or ratio_15m >= 1.10
            micro_confirmed = volume_move and micro_pressure >= 0.52
            micro_score = _clamp(
                max(tf5.volume_ratio / 1.50, ratio_15m / 1.30) * 70.0
                + _clamp((micro_pressure - 0.50) / 0.20 * 30.0, 0.0, 30.0),
                0.0,
                100.0,
            )
            factors["volume_order_flow"] = round(
                (factors.get("volume_order_flow", 50.0) * 0.45) + (micro_score * 0.55),
                1,
            )
        metrics.update(
            {
                "volume_ratio_5m": micro_ratio_5m,
                "buy_pressure_5m_pct": micro_pressure_pct,
                "micro_volume_anomaly": micro_confirmed,
            }
        )
        if micro_confirmed:
            passed.append("5m／15m 成交額異動與方向一致")
        else:
            missing.append("等待 5m／15m 成交額放大並與方向一致")

        if direction in ("LONG", "SHORT"):
            flow_parts: list[float] = []
            strong_book_opposition = False
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
                    strong_book_opposition = signed_book <= -0.20
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
            strong_book_opposition = False

        macro_opposition = (
            (market_bias_score >= 65.0 and direction == "SHORT")
            or (market_bias_score <= 35.0 and direction == "LONG")
        )
        if macro_opposition:
            missing.append("牛熊指標與此方向相反，逆勢訊號需更高強度")

        overall = self._weighted_factor_score(factors)
        metrics["holistic_evidence_score"] = overall
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

        final_score = round((result.signal.score * 0.60) + (overall * 0.40), 1)
        conflicts = sum(
            (strong_flow_opposition, strong_book_opposition, crowded, macro_opposition)
        )
        flexible_override = (
            final_score >= 84.0
            and overall >= 78.0
            and conflicts <= 1
        )
        context_rejected = (
            not context.complete
            or not micro_confirmed
            or (conflicts >= 2)
            or (strong_flow_opposition and not flexible_override)
            or (crowded and final_score < 82.0)
            or (macro_opposition and not flexible_override)
            or (result.signal.signal_stage == "EARLY" and final_score < 70.0)
        )
        if context_rejected:
            if not context.complete:
                missing = ["即時資金費率、訂單簿或主動成交資料未完整", *missing]
            updated_state = replace(
                updated_state,
                status="NEAR_TRIGGER",
                readiness_score=min(updated_state.readiness_score, 89.0),
                missing_conditions=_unique(missing)[:6],
            )
            reason = (
                "micro_volume_not_confirmed"
                if not micro_confirmed
                else "evidence_conflict"
                if conflicts >= 2
                else "market_bias_opposed"
                if macro_opposition and not flexible_override
                else "market_context_not_confirmed"
            )
            return AnalysisResult(None, reason, updated_state)

        evidence = list(result.signal.evidence)
        if context.taker_buy_ratio is not None:
            evidence.append(f"近期主動買方占比 {context.taker_buy_ratio * 100.0:.1f}%")
        if context.order_book_imbalance is not None:
            evidence.append(f"前 20 檔委託簿失衡 {context.order_book_imbalance * 100.0:+.1f}%")
        if context.funding_rate is not None:
            evidence.append(f"預估資金費率 {context.funding_rate * 100.0:+.4f}%")
        if execution_cost_pct is not None:
            evidence.append(
                f"估算 {context.execution_notional_usdt:,.0f} USDT 來回成本 {execution_cost_pct:.3f}%"
            )
        live_strength = result.signal.trend_strength_score
        live_strength_label = result.signal.trend_strength_label
        management_plan = dict(result.signal.management_plan)
        management_plan["strength"] = live_strength_label
        management_plan["review"] = "每 15 分鐘以最新趨勢、成交異動與訂單流重算；沒有接入持倉，不會代替交易所自動移動委託。"
        updated_signal = replace(
            result.signal,
            score=final_score,
            evidence=_unique(evidence),
            factor_scores=factors,
            market_metrics=metrics,
            trend_strength_score=live_strength,
            trend_strength_label=live_strength_label,
            management_plan=management_plan,
        )
        updated_state = replace(
            updated_state,
            status=("EARLY_SIGNAL" if updated_signal.signal_stage == "EARLY" else "CONFIRMED"),
            readiness_score=100.0,
            missing_conditions=[],
        )
        return AnalysisResult(updated_signal, "qualified", updated_state)

    @staticmethod
    def _weighted_factor_score(factors: dict[str, float]) -> float:
        weights = {
            "structure_trend": 0.22,
            "momentum": 0.13,
            "volatility": 0.10,
            "volume_order_flow": 0.18,
            "derivatives": 0.13,
            "liquidity_risk": 0.10,
            "market_bias": 0.14,
            "execution_quality": 0.08,
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
                "volume_ratio_15m": round(tf15.volume_ratio, 2),
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

    def _early_expansion_plan(
        self,
        direction: str,
        tf4: TimeframeFeatures,
        tf1: TimeframeFeatures,
        tf15: TimeframeFeatures,
    ) -> _Plan | None:
        """Detects the first closed 15m expansion without waiting for a 1H breakout close."""
        is_long = direction == "LONG"
        opposite = "SHORT" if is_long else "LONG"
        if self._bias(tf4) == opposite:
            return None

        structure_break = (
            tf15.close > tf15.prior_high20
            if is_long
            else tf15.close < tf15.prior_low20
        )
        if not structure_break:
            return None

        background_votes = sum(
            (
                self._bias(tf4) == direction,
                (tf4.close > tf4.ema21 and tf4.ema21_slope_atr >= 0.0)
                if is_long
                else (tf4.close < tf4.ema21 and tf4.ema21_slope_atr <= 0.0),
                (tf1.close > tf1.ema21) if is_long else (tf1.close < tf1.ema21),
            )
        )
        emerging_trend_votes = sum(
            (
                (tf1.sma5 > tf1.sma10) if is_long else (tf1.sma5 < tf1.sma10),
                (tf1.macd_line > tf1.macd_signal)
                if is_long
                else (tf1.macd_line < tf1.macd_signal),
                (tf1.ema21_slope_atr > 0.0) if is_long else (tf1.ema21_slope_atr < 0.0),
                (tf1.close > tf1.vwap20) if is_long else (tf1.close < tf1.vwap20),
            )
        )
        momentum_votes = sum(
            (
                (tf15.close > tf15.ema21) if is_long else (tf15.close < tf15.ema21),
                (tf15.macd_line > tf15.macd_signal)
                if is_long
                else (tf15.macd_line < tf15.macd_signal),
                (tf15.macd_hist > tf15.macd_prev_hist)
                if is_long
                else (tf15.macd_hist < tf15.macd_prev_hist),
                (tf15.close > tf15.vwap20) if is_long else (tf15.close < tf15.vwap20),
                (tf15.rsi14 >= 50.0) if is_long else (tf15.rsi14 <= 50.0),
            )
        )
        pressure = (
            tf15.directional_volume_ratio
            if is_long
            else 1.0 - tf15.directional_volume_ratio
        )
        participation_votes = sum(
            (
                tf15.volume_ratio >= 1.10,
                tf1.volume_ratio >= 1.05,
                pressure >= 0.54,
            )
        )
        timing_ok = (
            tf15.extension_atr <= self.config.max_entry_extension_atr
            and (tf15.rsi14 <= 76.0 if is_long else tf15.rsi14 >= 24.0)
        )
        evidence_groups = sum(
            (
                background_votes >= 2,
                emerging_trend_votes >= 2,
                momentum_votes >= 3,
                participation_votes >= 1,
            )
        )
        if background_votes < 1 or not timing_ok or evidence_groups < 3:
            return None

        entry = tf15.close
        if is_long:
            stop = min(
                tf15.recent_low - (0.20 * tf15.atr14),
                entry - (1.15 * tf15.atr14),
            )
            risk = entry - stop
            tp1 = entry + (risk * 1.8)
            tp2 = entry + (risk * 2.4)
            invalidation = "15m 收盤重新跌回突破區並跌破最近抬高低點，或觸及止損。"
        else:
            stop = max(
                tf15.recent_high + (0.20 * tf15.atr14),
                entry + (1.15 * tf15.atr14),
            )
            risk = stop - entry
            tp1 = entry - (risk * 1.8)
            tp2 = entry - (risk * 2.4)
            invalidation = "15m 收盤重新站回跌破區並突破最近降低高點，或觸及止損。"
        if risk <= 0:
            return None

        score = (
            58.0
            + (background_votes * 4.0)
            + (emerging_trend_votes * 2.5)
            + (momentum_votes * 2.5)
            + (participation_votes * 3.0)
        )
        return _Plan(
            direction=direction,
            strategy="早期動能擴張",
            regime="BREAKOUT_READY",
            score=min(score, 88.0),
            evidence=[
                "15m 已收盤突破近 20 根整理邊界，不等待 1H 大 K 棒完成",
                f"4H／1H 背景支持票 {background_votes + emerging_trend_votes}／7",
                f"15m 動能支持票 {momentum_votes}／5",
                f"早期成交參與支持票 {participation_votes}／3",
            ],
            entry=entry,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            rr=1.8,
            invalidation=invalidation,
            notes=[
                "這是提早訊號，必須再由 5m／15m 成交異動與即時資金流驗證。",
                f"距離 15m EMA21 已達 {tf15.extension_atr:.2f} ATR；超過 {self.config.max_entry_extension_atr:.2f} ATR 不追價。",
            ],
            signal_stage="EARLY",
        )

    def _adapt_trade_management(
        self,
        plan: _Plan,
        tf4: TimeframeFeatures,
        tf1: TimeframeFeatures,
        tf15: TimeframeFeatures,
    ) -> _Plan:
        is_long = plan.direction == "LONG"
        direction = plan.direction
        alignment_score = (
            (20.0 if self._bias(tf4) == direction else 10.0 if self._bias(tf4) == "NEUTRAL" else 0.0)
            + (15.0 if self._aligned_direction(tf1) == direction else 7.5)
        )
        adx_score = _clamp((tf1.adx14 - 14.0) / 26.0 * 20.0, 0.0, 20.0)
        momentum_votes = sum(
            (
                (tf1.macd_line > tf1.macd_signal) if is_long else (tf1.macd_line < tf1.macd_signal),
                (tf15.macd_line > tf15.macd_signal) if is_long else (tf15.macd_line < tf15.macd_signal),
                (tf15.rsi14 >= 50.0) if is_long else (tf15.rsi14 <= 50.0),
                (tf15.close > tf15.vwap20) if is_long else (tf15.close < tf15.vwap20),
            )
        )
        momentum_score = momentum_votes / 4.0 * 20.0
        volume_score = _clamp(max(tf1.volume_ratio, tf15.volume_ratio) / 1.8 * 15.0, 0.0, 15.0)
        slope_support = (
            tf1.ema21_slope_atr > 0 if is_long else tf1.ema21_slope_atr < 0
        )
        slope_score = 10.0 if slope_support else 0.0
        strength = round(
            _clamp(alignment_score + adx_score + momentum_score + volume_score + slope_score, 0.0, 100.0),
            1,
        )
        label = "強" if strength >= 75.0 else "中等" if strength >= 55.0 else "偏弱"

        risk = abs(plan.entry - plan.stop)
        if risk <= 0:
            return plan
        if plan.regime == "RANGE":
            tp1 = plan.tp1
            tp2 = plan.tp2
            rr = plan.rr
            trailing = "區間策略不追蹤趨勢；TP1 後保本，TP2 以區間另一側為上限。"
        else:
            if strength >= 75.0:
                tp1_r, tp2_r = 2.1, 3.4
                trailing = "TP1 後移至成本價；其後以 15m EMA21 或 1.5 ATR 中較緊者追蹤。"
            elif strength >= 55.0:
                tp1_r, tp2_r = 1.9, 2.8
                trailing = "TP1 後移至成本價；其後以最近 15m 結構低／高點追蹤。"
            else:
                tp1_r, tp2_r = 1.8, 2.3
                trailing = "偏弱趨勢優先落袋；TP1 後移至成本價，不放寬原始止損。"
            if plan.regime == "BREAKOUT" and strength >= 75.0:
                tp2_r = 3.6
            if is_long:
                tp1 = plan.entry + (risk * tp1_r)
                tp2 = plan.entry + (risk * tp2_r)
            else:
                tp1 = plan.entry - (risk * tp1_r)
                tp2 = plan.entry - (risk * tp2_r)
            rr = tp1_r

        management = {
            "strength": label,
            "initial_stop": "依原始結構與 ATR 設定；進場後只能收緊，不能放寬。",
            "tp1_action": "TP1 建議部分止盈，剩餘部位止損移至實際成交成本。",
            "trailing": trailing,
            "weakness_exit": "若 15m 動能反轉、跌回／站回 EMA21 且成交量支持反向，提前收緊或退出。",
        }
        return replace(
            plan,
            tp1=tp1,
            tp2=tp2,
            rr=rr,
            trend_strength_label=label,
            trend_strength_score=strength,
            management_plan=management,
        )

    def _breakout_plan(
        self,
        direction: str,
        tf4: TimeframeFeatures,
        tf1: TimeframeFeatures,
        tf15: TimeframeFeatures,
    ) -> _Plan | None:
        opposite = "SHORT" if direction == "LONG" else "LONG"
        if self._bias(tf4) == opposite:
            return None
        participation_votes = sum(
            (
                tf1.volume_ratio >= 1.15,
                tf15.volume_ratio >= 1.15,
                (tf15.directional_volume_ratio >= 0.52)
                if direction == "LONG"
                else (tf15.directional_volume_ratio <= 0.48),
            )
        )
        background_votes = sum(
            (
                self._bias(tf4) == direction,
                self._aligned_direction(tf1) == direction,
                tf1.adx14 >= 18.0,
            )
        )
        if participation_votes < 1 or background_votes < 1:
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
        score = (
            68.0
            + (participation_votes * 3.5)
            + (background_votes * 3.0)
            + min(tf1.volume_ratio, 3.0) * 2.0
            + min(max(tf1.adx14 - 18.0, 0.0), 25.0) * 0.25
        )
        return _Plan(
            direction=direction,
            strategy="放量突破",
            regime="BREAKOUT",
            score=score,
            evidence=[
                f"大週期背景支持票 {background_votes}／3",
                "1H 收盤突破近 20 根結構邊界",
                f"成交參與支持票 {participation_votes}／3（1H 量比 {tf1.volume_ratio:.2f}）",
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
        opposite = "SHORT" if direction == "LONG" else "LONG"
        if self._bias(tf4) == opposite:
            return None
        near_fast_average = abs(tf15.close - tf15.ema21) <= (
            self.config.max_entry_extension_atr * tf15.atr14
        )
        if direction == "LONG":
            background_votes = sum(
                (
                    self._bias(tf4) == direction,
                    tf1.close > tf1.ema21,
                    tf1.sma5 > tf1.sma10,
                    tf1.ema21_slope_atr > 0.0,
                    tf1.adx14 >= 18.0,
                )
            )
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
            if not (background_votes >= 3 and trigger and near_fast_average):
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
            background_votes = sum(
                (
                    self._bias(tf4) == direction,
                    tf1.close < tf1.ema21,
                    tf1.sma5 < tf1.sma10,
                    tf1.ema21_slope_atr < 0.0,
                    tf1.adx14 >= 18.0,
                )
            )
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
            if not (background_votes >= 3 and trigger and near_fast_average):
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
        score = (
            62.0
            + (background_votes * 3.0)
            + min(max(tf1.adx14 - 16.0, 0.0), 30.0) * 0.35
            + min(tf1.volume_ratio, 2.0) * 3.0
        )
        return _Plan(
            direction=direction,
            strategy="趨勢回踩續行",
            regime="TREND",
            score=score,
            evidence=[
                f"4H／1H 趨勢背景支持票 {background_votes}／5",
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
