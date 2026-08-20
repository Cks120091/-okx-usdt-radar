from __future__ import annotations

import math
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP

from .evidence import (
    EvidenceAssessment,
    assess_evidence,
    infer_regime_direction,
    summary_for_stage,
)
from .indicators import TimeframeFeatures, features
from .market_story import (
    FEATURE_SCHEMA_VERSION,
    STRATEGY_VERSION,
    MarketStoryEngine,
    StoryAssessment,
    enrich_story_context,
    execution_quality,
)
from .models import Candle, Instrument, MarketContext, MarketState, Signal, Ticker


@dataclass(frozen=True)
class StrategyConfig:
    min_quote_volume_24h: float = 5_000_000.0
    max_spread_pct: float = 0.10
    min_open_interest_usd: float = 3_000_000.0
    require_micro_volume_anomaly: bool = False
    minimum_rr: float = 1.8
    estimated_taker_fee_pct: float = 0.05
    max_execution_cost_to_risk_pct: float = 12.0
    max_entry_extension_atr: float = 0.80
    severe_entry_extension_atr: float = 1.80
    max_slippage_pct: float = 0.15
    universe_max_spread_pct: float = 1.00
    early_signal_max_age_bars: int = 2
    entry_ready_max_chase_atr: float = 0.15
    entry_missed_chase_atr: float = 0.50


@dataclass
class AnalysisResult:
    signal: Signal | None
    reason: str
    market_state: MarketState | None = None
    assessment: EvidenceAssessment | StoryAssessment | None = None
    candidate_plan: _Plan | None = None
    candidate_signal: Signal | None = None


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
        self.story_engine = MarketStoryEngine(
            self.config.early_signal_max_age_bars,
            self.config.entry_missed_chase_atr,
        )

    def analyze(
        self,
        instrument: Instrument,
        ticker: Ticker,
        candles_4h: list[Candle],
        candles_1h: list[Candle],
        candles_15m: list[Candle],
        candles_5m: list[Candle] | None = None,
        previous_story: dict[str, object] | None = None,
    ) -> AnalysisResult:
        return self._analyze_v33(
            instrument,
            ticker,
            candles_4h,
            candles_1h,
            candles_15m,
            candles_5m,
            previous_story,
            horizon="SHORT",
        )

    def analyze_long(
        self,
        instrument: Instrument,
        ticker: Ticker,
        candles_1d: list[Candle],
        candles_4h: list[Candle],
        candles_1h: list[Candle],
        previous_story: dict[str, object] | None = None,
    ) -> AnalysisResult:
        return self._analyze_v33(
            instrument,
            ticker,
            candles_1d,
            candles_4h,
            candles_4h,
            candles_1h,
            previous_story,
            horizon="LONG",
        )

    def _analyze_v33(
        self,
        instrument: Instrument,
        ticker: Ticker,
        candles_higher: list[Candle],
        candles_bias: list[Candle],
        candles_core: list[Candle],
        candles_timing: list[Candle] | None,
        previous_story: dict[str, object] | None,
        horizon: str,
    ) -> AnalysisResult:
        required = (candles_higher, candles_bias, candles_core)
        if min((len(items) for items in required), default=0) < 60:
            return AnalysisResult(None, "insufficient_history")
        if ticker.last <= 0 or ticker.bid <= 0 or ticker.ask <= 0:
            return AnalysisResult(None, "invalid_ticker")
        if not all(items[-1].confirmed for items in required):
            return AnalysisResult(None, "core_candle_unconfirmed")

        try:
            story = (
                self.story_engine.analyze_long(
                    candles_higher,
                    candles_bias,
                    candles_timing or [],
                    previous_story,
                )
                if horizon == "LONG"
                else self.story_engine.analyze_short(
                    candles_higher,
                    candles_bias,
                    candles_core,
                    candles_timing,
                    previous_story,
                )
            )
        except (ValueError, ArithmeticError) as exc:
            return AnalysisResult(None, f"market_story_unavailable:{exc}")

        tf_higher = features(candles_higher)
        tf_bias = features(candles_bias)
        tf_core = features(candles_core)
        tf_timing = (
            features(candles_timing)
            if candles_timing is not None and len(candles_timing) >= 60
            else None
        )
        volume_source = (
            candles_bias if horizon == "SHORT" else candles_timing or candles_bias
        )
        quote_volume_24h = sum(item.quote_volume for item in volume_source[-24:])
        metrics = {
            "last_price": ticker.last,
            "price_change_core_pct": _price_change_pct(
                ticker.last,
                candles_core[-2].close,
            ),
            "price_change_15m_pct": (
                _price_change_pct(ticker.last, candles_core[-2].close)
                if horizon == "SHORT"
                else None
            ),
            "price_change_1h_pct": _price_change_pct(
                ticker.last,
                (candles_bias if horizon == "SHORT" else candles_timing or candles_bias)[-2].close,
            ),
            "price_change_24h_pct": _price_change_pct(
                ticker.last,
                volume_source[-25].close if len(volume_source) >= 25 else volume_source[0].close,
            ),
            "adx_core": round(tf_core.adx14, 1),
            "adx_1h": round(tf_bias.adx14, 1),
            "rsi_core": round(tf_core.rsi14, 1),
            "rsi_15m": round(tf_core.rsi14, 1) if horizon == "SHORT" else None,
            "volume_ratio_core": round(tf_core.volume_ratio, 2),
            "volume_ratio_15m": round(tf_core.volume_ratio, 2) if horizon == "SHORT" else None,
            "volume_ratio_5m": round(tf_timing.volume_ratio, 2) if horizon == "SHORT" and tf_timing else None,
            "atr_pct_core": round(tf_core.atr_pct, 3),
            "core_high": candles_core[-1].high,
            "core_low": candles_core[-1].low,
            "core_close": candles_core[-1].close,
            "core_timestamp": candles_core[-1].ts,
            # Internal ledger input. Scanner/DB serializers remove underscore
            # fields before publishing, while the repository can still recover
            # every closed bar between two on-demand scans.
            "_core_path": [
                [item.ts, item.high, item.low, item.close]
                for item in candles_core
            ],
            "trigger_event_ts": story.event_ts,
            "trigger_age_bars": story.event_age_bars,
            "raw_indicators": {
                ("4H" if horizon == "SHORT" else "1D"): self._feature_metrics(tf_higher),
                ("1H" if horizon == "SHORT" else "4H"): self._feature_metrics(tf_bias),
                ("15m" if horizon == "SHORT" else "4H_TRIGGER"): self._feature_metrics(tf_core),
                **(
                    {("5m" if horizon == "SHORT" else "1H_TIMING"): self._feature_metrics(tf_timing)}
                    if tf_timing is not None
                    else {}
                ),
            },
        }
        checks = [
            self._safety_check("core_data", "核心 K 線與 Ticker 可用", True),
            self._safety_check(
                "universe_liquidity",
                "24H 成交額符合 Universe 門檻",
                quote_volume_24h >= self.config.min_quote_volume_24h,
                quote_volume_24h,
            ),
            self._safety_check(
                "universe_spread",
                "Spread 未達極端異常排除門檻",
                ticker.spread_pct <= self.config.universe_max_spread_pct,
                round(ticker.spread_pct, 4),
            ),
        ]
        direction = (
            story.trigger_direction
            if story.trigger_direction in ("LONG", "SHORT")
            else story.direction
        )
        market_state = MarketState(
            inst_id=instrument.inst_id,
            regime=story.regime,
            direction=direction,
            preferred_strategy=self._v33_strategy_name(story.trigger_type),
            readiness_score=story.readiness,
            status=story.stage,
            missing_conditions=_unique([*story.conflicts, *story.neutral])[:10],
            spread_pct=round(ticker.spread_pct, 4),
            quote_volume_24h=round(quote_volume_24h, 2),
            closed_candle_ts=candles_core[-1].ts,
            passed_conditions=story.supporting[:10],
            factor_scores={key: float(group["score"]) for key, group in story.groups.items()},
            market_metrics=metrics,
            evidence_groups=story.group_dicts(),
            timeframe_states=story.timeframe_states,
            supporting_evidence=story.supporting,
            conflicts=story.conflicts,
            neutral_evidence=story.neutral,
            safety_checks=checks,
            entry_quality=story.entry_quality,
            summary=story.summary,
            radar_horizon=horizon,
            direction_state=story.direction_state,
            trigger=dict(story.trigger),
            lifecycle={"current_stage": story.stage, "transition": "TECHNICAL_SNAPSHOT"},
            freshness=story.freshness,
            market_participation=dict(story.market_participation),
            execution_quality=dict(story.execution_quality),
            data_quality=dict(story.data_quality),
            market_story=story.story_dict(),
            human_reason=story.summary,
            actionable=story.triggered and story.stage in ("EARLY_SIGNAL", "CONFIRMED", "REENTRY"),
        )

        universe_failures = [
            item for item in checks if item["key"].startswith("universe_") and not item["passed"]
        ]
        if universe_failures:
            reason = (
                "liquidity_too_low"
                if universe_failures[0]["key"] == "universe_liquidity"
                else "spread_extremely_abnormal"
            )
            return AnalysisResult(
                None,
                reason,
                replace(market_state, status="FILTERED", actionable=False),
                story,
            )
        if not story.triggered:
            return AnalysisResult(
                None,
                "near_trigger" if story.stage == "NEAR_TRIGGER" else "no_fresh_trigger",
                market_state,
                story,
            )

        plan = self._v33_plan(story, tf_core)
        signal = self._signal_from_v33(
            instrument,
            ticker,
            quote_volume_24h,
            candles_core[-1].ts,
            story,
            market_state,
            plan,
        )
        market_state = replace(
            market_state,
            market_metrics=dict(signal.market_metrics),
            safety_checks=list(signal.safety_checks),
            actionable=signal.actionable,
        )
        return AnalysisResult(
            signal,
            "qualified",
            market_state,
            story,
            plan,
            signal,
        )

    def _v33_plan(
        self,
        story: StoryAssessment,
        tf_core: TimeframeFeatures,
    ) -> _Plan:
        direction = story.trigger_direction
        is_long = direction == "LONG"
        entry = float(
            story.trigger.get("entry_reference_price")
            or story.trigger.get("event_price")
            or tf_core.close
        )
        proposed_stop = story.invalidation_price
        if proposed_stop is None or (is_long and proposed_stop >= entry) or (not is_long and proposed_stop <= entry):
            proposed_stop = (
                min(tf_core.recent_low - tf_core.atr14 * 0.20, entry - tf_core.atr14 * 0.65)
                if is_long
                else max(tf_core.recent_high + tf_core.atr14 * 0.20, entry + tf_core.atr14 * 0.65)
            )
        stop = float(proposed_stop)
        risk = abs(entry - stop)
        if risk <= 0:
            risk = max(tf_core.atr14 * 0.65, abs(entry) * 0.003)
            stop = entry - risk if is_long else entry + risk

        obstacle_key = "major_resistance" if is_long else "major_support"
        obstacle = story.zones.get(obstacle_key) or story.zones.get(
            "resistance" if is_long else "support"
        )
        structural_target = float(obstacle["center"]) if obstacle else None
        structural_rr = (
            (structural_target - entry) / risk
            if is_long and structural_target is not None and structural_target > entry
            else (entry - structural_target) / risk
            if not is_long and structural_target is not None and structural_target < entry
            else None
        )
        if structural_rr is not None and structural_rr > 0:
            tp1 = float(structural_target)
            rr = structural_rr
        else:
            rr = self.config.minimum_rr
            tp1 = entry + risk * rr if is_long else entry - risk * rr
        tp2 = entry + risk * max(2.7, rr + 0.8) if is_long else entry - risk * max(2.7, rr + 0.8)
        strength_score = float(story.trigger.get("explainability_score", 50.0))
        strength_label = "強" if strength_score >= 78.0 else "中等" if strength_score >= 58.0 else "偏弱"
        management = {
            "tp1_action": "TP1 後可把 Stop 移到 Break-even；實際委託由使用者決定。",
            "tp2_action": "TP2 或結構目標分批處理。",
            "review": "每個核心週期收盤後更新 Lifecycle；5m/1H Timing 不單獨反手。",
            "auto_ordering": False,
        }
        return _Plan(
            direction=direction,
            strategy=self._v33_strategy_name(story.trigger_type),
            regime=story.regime,
            score=story.readiness,
            evidence=story.supporting,
            entry=entry,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            rr=max(0.0, rr),
            invalidation=(
                "核心週期收盤跌破失效 Zone／Micro 防守，原做多故事失效。"
                if is_long
                else "核心週期收盤站回失效 Zone／Micro 防守，原做空故事失效。"
            ),
            notes=[
                "Market Participation、Conflict 與 Execution Quality 均獨立顯示，不取消核心 Trigger。",
                "只使用已收盤核心 K 線形成正式訊號。",
            ],
            signal_stage=story.stage,
            trend_strength_label=strength_label,
            trend_strength_score=strength_score,
            management_plan=management,
        )

    def _signal_from_v33(
        self,
        instrument: Instrument,
        ticker: Ticker,
        quote_volume_24h: float,
        closed_candle_ts: int,
        story: StoryAssessment,
        state: MarketState,
        plan: _Plan,
    ) -> Signal:
        tf_atr = float(story.raw.get("core_atr", 0.0) or 0.0)
        zone_offset = max(tf_atr * 0.12, abs(plan.entry) * 0.0003)
        if plan.direction == "LONG":
            entry_low, entry_high = plan.entry - zone_offset, plan.entry + zone_offset * 0.45
        else:
            entry_low, entry_high = plan.entry - zone_offset * 0.45, plan.entry + zone_offset
        risk_pct = abs(plan.entry - plan.stop) / max(abs(plan.entry), 1e-9) * 100.0
        initial_quality = execution_quality(
            story,
            ticker.spread_pct,
            risk_pct,
            plan.rr,
            None,
            target_rr=self.config.minimum_rr,
            max_cost_to_risk_pct=self.config.max_execution_cost_to_risk_pct,
            max_spread_pct=self.config.max_spread_pct,
            max_slippage_pct=self.config.max_slippage_pct,
            estimated_taker_fee_pct=self.config.estimated_taker_fee_pct,
        )
        eligibility = _entry_eligibility(
            direction=plan.direction,
            current_price=ticker.last,
            entry_low=entry_low,
            entry_high=entry_high,
            stop=plan.stop,
            target=plan.tp1,
            atr=tf_atr,
            stage=story.stage,
            minimum_rr=self.config.minimum_rr,
            ready_max_chase_atr=self.config.entry_ready_max_chase_atr,
            missed_chase_atr=self.config.entry_missed_chase_atr,
        )
        metrics = dict(state.market_metrics)
        metrics.update(
            {
                "technical_stop_pct": round(risk_pct, 4),
                "trigger_type": story.trigger_type,
                "signal_stage": story.stage,
                "execution_quality_score": initial_quality["score"],
                "entry_status": eligibility["status"],
                "entry_chase_atr": eligibility["chase_atr"],
                "remaining_rr": eligibility["remaining_rr"],
            }
        )
        return Signal(
            inst_id=instrument.inst_id,
            direction=plan.direction,
            strategy=plan.strategy,
            score=story.readiness,
            evidence=story.supporting,
            entry_low=_format_price(entry_low, instrument.tick_size),
            entry_high=_format_price(entry_high, instrument.tick_size),
            stop_loss=_format_price(plan.stop, instrument.tick_size),
            take_profit_1=_format_price(plan.tp1, instrument.tick_size),
            take_profit_2=_format_price(plan.tp2, instrument.tick_size),
            risk_reward=round(plan.rr, 2),
            invalidation=plan.invalidation,
            spread_pct=round(ticker.spread_pct, 4),
            quote_volume_24h=round(quote_volume_24h, 2),
            closed_candle_ts=closed_candle_ts,
            regime=story.regime,
            notes=plan.notes,
            factor_scores={key: float(group["score"]) for key, group in story.groups.items()},
            market_metrics=metrics,
            signal_stage=story.stage,
            trend_strength_label=plan.trend_strength_label,
            trend_strength_score=plan.trend_strength_score,
            management_plan=dict(plan.management_plan or {}),
            readiness_score=story.readiness,
            evidence_groups=story.group_dicts(),
            timeframe_states=story.timeframe_states,
            supporting_evidence=story.supporting,
            conflicts=story.conflicts,
            neutral_evidence=story.neutral,
            safety_checks=[
                *state.safety_checks,
                self._safety_check(
                    "entry_eligibility",
                    eligibility["label"],
                    bool(eligibility["actionable"]),
                    eligibility["chase_atr"],
                    hard=False,
                ),
            ],
            entry_quality=story.entry_quality,
            summary=story.summary,
            lifecycle={
                "previous_stage": None,
                "current_stage": story.stage,
                "transition": "TECHNICAL_EVENT",
            },
            actionable=bool(eligibility["actionable"]),
            radar_horizon=story.horizon,
            trigger_type=story.trigger_type,
            direction_state=story.direction_state,
            freshness=story.freshness,
            market_participation=dict(story.market_participation),
            execution_quality=initial_quality,
            data_quality=dict(story.data_quality),
            market_story=story.story_dict(),
            data_timestamp=closed_candle_ts,
            strategy_version=STRATEGY_VERSION,
            feature_schema_version=FEATURE_SCHEMA_VERSION,
            entry_eligibility=eligibility,
        )

    @staticmethod
    def _v33_strategy_name(trigger_type: str) -> str:
        return {
            "REVERSAL": "控制權轉移反轉",
            "BREAKOUT": "突破與價格接受",
            "CONTINUATION": "回踩再發動",
        }.get(trigger_type, "等待市場故事完成")

    def _analyze_v2(
        self,
        instrument: Instrument,
        ticker: Ticker,
        candles_4h: list[Candle],
        candles_1h: list[Candle],
        candles_15m: list[Candle],
        candles_5m: list[Candle] | None = None,
    ) -> AnalysisResult:
        if min(len(candles_4h), len(candles_1h), len(candles_15m)) < 60:
            return AnalysisResult(None, "insufficient_history")
        if candles_5m is not None and len(candles_5m) < 60:
            return AnalysisResult(None, "insufficient_history")
        if ticker.last <= 0 or ticker.bid <= 0 or ticker.ask <= 0:
            return AnalysisResult(None, "invalid_ticker")
        quote_volume_24h = sum(item.quote_volume for item in candles_1h[-24:])

        tf4 = features(candles_4h)
        tf1 = features(candles_1h)
        tf15 = features(candles_15m)
        tf5 = features(candles_5m) if candles_5m is not None else None
        indicator_values = (tf4.atr14, tf1.atr14, tf15.atr14, tf1.adx14, tf15.rsi14)
        if not all(
            math.isfinite(value)
            for value in indicator_values
        ) or min(tf4.atr14, tf1.atr14, tf15.atr14) <= 0:
            return AnalysisResult(None, "indicator_unavailable")

        regime, direction = infer_regime_direction(tf4, tf1, tf15)
        assessment = assess_evidence(
            tf4,
            tf1,
            tf15,
            regime,
            direction,
            tf5,
            self.config.max_entry_extension_atr,
            self.config.severe_entry_extension_atr,
        )
        market_state = self._state_from_assessment(
            instrument,
            ticker,
            quote_volume_24h,
            candles_15m[-1].ts,
            tf4,
            tf1,
            tf15,
            tf5,
            assessment,
        )
        snapshot_metrics = dict(market_state.market_metrics)
        snapshot_metrics.update(
            {
                "last_price": ticker.last,
                "price_change_15m_pct": _price_change_pct(
                    ticker.last,
                    candles_15m[-2].close,
                ),
                "price_change_1h_pct": _price_change_pct(
                    ticker.last,
                    candles_1h[-2].close,
                ),
                "price_change_24h_pct": _price_change_pct(
                    ticker.last,
                    candles_1h[-25].close,
                ),
            }
        )
        market_state = replace(market_state, market_metrics=snapshot_metrics)
        if ticker.spread_pct > self.config.max_spread_pct:
            return AnalysisResult(
                None,
                "spread_too_wide",
                self._fail_safety(
                    market_state,
                    "spread",
                    f"買賣價差需低於 {self.config.max_spread_pct:.2f}%",
                ),
                assessment,
            )
        if quote_volume_24h < self.config.min_quote_volume_24h:
            return AnalysisResult(
                None,
                "liquidity_too_low",
                self._fail_safety(
                    market_state,
                    "liquidity",
                    f"24H 成交額需達 {self.config.min_quote_volume_24h:,.0f} USDT",
                ),
                assessment,
            )

        if assessment.entry_quality["key"] == "SEVERE_CHASE":
            return AnalysisResult(
                None,
                "severe_chase",
                self._fail_safety(
                    market_state,
                    "chase",
                    f"距離 15m EMA21 已達 {tf15.extension_atr:.2f} ATR，屬嚴重追價",
                ),
                assessment,
            )

        plan = self._v2_plan(assessment, tf4, tf1, tf15)
        if plan is None:
            return AnalysisResult(
                None,
                "no_trade_plan",
                replace(
                    market_state,
                    status=(
                        "NEAR_TRIGGER"
                        if assessment.stage == "NEAR_TRIGGER"
                        else "WATCH"
                    ),
                ),
                assessment,
            )
        plan = self._adapt_trade_management(plan, tf4, tf1, tf15)
        if plan.rr < self.config.minimum_rr:
            return AnalysisResult(
                None,
                "rr_below_minimum",
                self._fail_safety(
                    market_state,
                    "risk_reward",
                    f"風報比需達 {self.config.minimum_rr:.1f}R",
                ),
                assessment,
                plan,
            )
        risk_pct = abs(plan.entry - plan.stop) / plan.entry * 100.0
        if risk_pct <= 0 or risk_pct > 5.0:
            return AnalysisResult(
                None,
                "stop_distance_unacceptable",
                self._fail_safety(
                    market_state,
                    "stop_loss",
                    "無法建立價格 5% 以內的合理止損",
                ),
                assessment,
                plan,
            )

        signal_metrics = dict(market_state.market_metrics)
        signal_metrics.update(
            {
                "signal_stage": assessment.stage,
                "trend_strength_score": round(plan.trend_strength_score, 1),
                "trend_strength_label": plan.trend_strength_label,
                "technical_stop_pct": round(risk_pct, 4),
                "entry_extension_atr": round(tf15.extension_atr, 2),
            }
        )
        safe_state = self._pass_safety(
            self._pass_safety(market_state, "risk_reward"),
            "stop_loss",
        )
        candidate_signal = self._signal_from_plan(
            instrument,
            ticker,
            quote_volume_24h,
            candles_15m[-1].ts,
            tf15,
            plan,
            assessment,
            replace(safe_state, market_metrics=signal_metrics),
        )
        formal = assessment.stage in ("EARLY_SIGNAL", "CONFIRMED")
        return AnalysisResult(
            candidate_signal if formal else None,
            "qualified" if formal else "evidence_not_aligned",
            replace(
                safe_state,
                regime=plan.regime,
                direction=plan.direction,
                preferred_strategy=plan.strategy,
                readiness_score=assessment.readiness,
                status=assessment.stage,
                market_metrics=signal_metrics,
            ),
            assessment,
            plan,
            candidate_signal,
        )

    def apply_market_context(
        self,
        result: AnalysisResult,
        context: MarketContext,
        btc_bias: str = "NEUTRAL",
        candles_5m: list[Candle] | None = None,
        market_bias: dict[str, object] | None = None,
    ) -> AnalysisResult:
        if isinstance(result.assessment, StoryAssessment):
            return self._apply_v33_market_context(
                result,
                context,
                candles_5m,
                market_bias,
            )
        return self._apply_legacy_market_context(
            result,
            context,
            btc_bias,
            candles_5m,
            market_bias,
        )

    def _apply_v33_market_context(
        self,
        result: AnalysisResult,
        context: MarketContext,
        timing_candles: list[Candle] | None,
        market_bias: dict[str, object] | None,
    ) -> AnalysisResult:
        state = result.market_state
        story = result.assessment
        if state is None or not isinstance(story, StoryAssessment):
            return result
        timing = (
            features(timing_candles)
            if timing_candles is not None and len(timing_candles) >= 60
            else None
        )
        live_story = enrich_story_context(story, context, timing, market_bias)
        metrics = dict(state.market_metrics)
        if timing is not None:
            raw = dict(metrics.get("raw_indicators", {}))
            timing_key = "5m" if story.horizon == "SHORT" else "1H_TIMING"
            raw[timing_key] = self._feature_metrics(timing)
            metrics["raw_indicators"] = raw
            timing_long_score = float(
                live_story.timeframe_states.get(timing_key, {}).get("score", 50.0)
            )
            directional_timing_score = (
                timing_long_score
                if live_story.trigger_direction == "LONG"
                else 100.0 - timing_long_score
                if live_story.trigger_direction == "SHORT"
                else 50.0
            )
            metrics["timing_directional_score"] = round(
                directional_timing_score,
                1,
            )
            if story.horizon == "SHORT":
                # Retain the established API field while giving it V3.3 semantics:
                # this is timing telemetry, never permission to create/cancel a Trigger.
                metrics["micro_acceleration_5m"] = round(
                    directional_timing_score,
                    1,
                )
        metrics.update(
            {
                "open_interest_usd": context.open_interest_usd,
                "open_interest_change_pct": context.open_interest_change_pct,
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
                "order_book_sequence": dict(context.order_book_sequence),
                "taker_buy_pct": (
                    round(context.taker_buy_ratio * 100.0, 1)
                    if context.taker_buy_ratio is not None
                    else None
                ),
                "taker_buy_volume": context.taker_buy_volume,
                "taker_sell_volume": context.taker_sell_volume,
                "cvd": context.cvd,
                "context_sampled_at": context.sampled_at,
                "context_complete": context.complete,
                "context_available_count": len(
                    live_story.market_participation.get("available_sources", [])
                ),
                "context_missing_sources": live_story.data_quality.get("missing_sources", []),
                "execution_notional_usdt": context.execution_notional_usdt,
                "bid_depth_usd": context.bid_depth_usd,
                "ask_depth_usd": context.ask_depth_usd,
                "buy_slippage_pct": context.buy_slippage_pct,
                "sell_slippage_pct": context.sell_slippage_pct,
                "execution_quality_complete": context.execution_quality_complete,
                "market_participation_state": live_story.market_participation.get("state"),
            }
        )
        checks = [
            item
            for item in state.safety_checks
            if item.get("key")
            not in {
                "context_data",
                "open_interest",
                "execution_depth",
                "slippage",
                "execution_cost",
            }
        ]
        checks.extend(
            [
                self._safety_check(
                    "context_data",
                    "Deep Data 完整度（只作 Context）",
                    context.complete,
                    len(live_story.market_participation.get("available_sources", [])),
                    hard=False,
                ),
                self._safety_check(
                    "open_interest",
                    "Open Interest 可用（本身無多空方向）",
                    context.open_interest_usd is not None,
                    context.open_interest_usd,
                    hard=False,
                ),
                self._safety_check(
                    "execution_depth",
                    "Order Book 深度足以估算成交",
                    context.execution_quality_complete,
                    context.execution_notional_usdt,
                    hard=False,
                ),
            ]
        )

        updated_state = replace(
            state,
            readiness_score=live_story.readiness,
            status=live_story.stage,
            passed_conditions=live_story.supporting[:10],
            missing_conditions=_unique([*live_story.conflicts, *live_story.neutral])[:10],
            factor_scores={key: float(group["score"]) for key, group in live_story.groups.items()},
            market_metrics=metrics,
            evidence_groups=live_story.group_dicts(),
            timeframe_states=live_story.timeframe_states,
            supporting_evidence=live_story.supporting,
            conflicts=live_story.conflicts,
            neutral_evidence=live_story.neutral,
            safety_checks=checks,
            summary=live_story.summary,
            market_participation=dict(live_story.market_participation),
            data_quality=dict(live_story.data_quality),
            market_story=live_story.story_dict(),
            human_reason=live_story.summary,
        )
        if result.signal is None or result.candidate_plan is None:
            return AnalysisResult(
                None,
                result.reason,
                updated_state,
                live_story,
                result.candidate_plan,
                result.candidate_signal,
            )

        plan = result.candidate_plan
        risk_pct = abs(plan.entry - plan.stop) / max(abs(plan.entry), 1e-9) * 100.0
        quality = execution_quality(
            live_story,
            state.spread_pct,
            risk_pct,
            plan.rr,
            context,
            target_rr=self.config.minimum_rr,
            max_cost_to_risk_pct=self.config.max_execution_cost_to_risk_pct,
            max_spread_pct=self.config.max_spread_pct,
            max_slippage_pct=self.config.max_slippage_pct,
            estimated_taker_fee_pct=self.config.estimated_taker_fee_pct,
        )
        metrics.update(
            {
                "execution_quality_score": quality["score"],
                "execution_quality_label": quality["label"],
                "estimated_round_trip_cost_pct": quality["estimated_round_trip_cost_pct"],
                "execution_cost_to_risk_pct": quality["execution_cost_to_risk_pct"],
            }
        )
        checks.extend(
            [
                self._safety_check(
                    "slippage",
                    "滑價評估（不取消 Radar Trigger）",
                    quality["recommendation"] != "AVOID_EXECUTION",
                    max(float(context.buy_slippage_pct or 0.0), float(context.sell_slippage_pct or 0.0)),
                    hard=False,
                ),
                self._safety_check(
                    "execution_cost",
                    "Execution Quality（與 Trigger 分離）",
                    quality["score"] >= 50.0,
                    quality["score"],
                    hard=False,
                ),
            ]
        )
        updated_state = replace(
            updated_state,
            market_metrics=metrics,
            safety_checks=checks,
            execution_quality=quality,
            entry_quality=dict(quality.get("entry_location", {})),
            actionable=(
                live_story.stage in ("EARLY_SIGNAL", "CONFIRMED", "REENTRY")
                and bool(result.signal.entry_eligibility.get("actionable"))
            ),
        )
        signal = replace(
            result.signal,
            evidence=live_story.supporting,
            factor_scores={key: float(group["score"]) for key, group in live_story.groups.items()},
            market_metrics=metrics,
            evidence_groups=live_story.group_dicts(),
            timeframe_states=live_story.timeframe_states,
            supporting_evidence=live_story.supporting,
            conflicts=live_story.conflicts,
            neutral_evidence=live_story.neutral,
            safety_checks=checks,
            entry_quality=dict(quality.get("entry_location", {})),
            summary=live_story.summary,
            actionable=(
                live_story.stage in ("EARLY_SIGNAL", "CONFIRMED", "REENTRY")
                and bool(result.signal.entry_eligibility.get("actionable"))
            ),
            market_participation=dict(live_story.market_participation),
            execution_quality=quality,
            data_quality=dict(live_story.data_quality),
            market_story=live_story.story_dict(),
        )
        return AnalysisResult(
            signal,
            "qualified",
            updated_state,
            live_story,
            plan,
            signal,
        )

    def _apply_legacy_market_context(
        self,
        result: AnalysisResult,
        context: MarketContext,
        btc_bias: str = "NEUTRAL",
        candles_5m: list[Candle] | None = None,
        market_bias: dict[str, object] | None = None,
    ) -> AnalysisResult:
        if result.assessment is not None:
            return self._apply_v2_market_context(
                result,
                context,
                candles_5m,
                market_bias,
            )
        state = result.market_state
        if state is None:
            return result

        metrics = dict(state.market_metrics)
        metrics.update(
            {
                "open_interest_usd": context.open_interest_usd,
                "open_interest_change_pct": context.open_interest_change_pct,
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
            if context.open_interest_change_pct is not None:
                oi_change = context.open_interest_change_pct
                if oi_change >= 0.5:
                    derivative_parts.append(85.0)
                    passed.append(
                        f"持倉量較上一輪增加 {oi_change:.2f}%，顯示有新增部位參與"
                    )
                elif oi_change <= -0.8:
                    derivative_parts.append(35.0)
                    missing.append(
                        f"持倉量較上一輪下降 {abs(oi_change):.2f}%，行情可能由平倉或回補推動"
                    )
                else:
                    derivative_parts.append(65.0)
                    passed.append(
                        f"持倉量較上一輪變化 {oi_change:+.2f}%，未見明顯去槓桿"
                    )
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

    def _apply_v2_market_context(
        self,
        result: AnalysisResult,
        context: MarketContext,
        candles_5m: list[Candle] | None,
        market_bias: dict[str, object] | None,
    ) -> AnalysisResult:
        state = result.market_state
        assessment = result.assessment
        if state is None or assessment is None:
            return result

        tf5 = (
            features(candles_5m)
            if candles_5m is not None and len(candles_5m) >= 60
            else None
        )
        live = assessment.with_live_context(context, tf5, market_bias)
        metrics = dict(state.market_metrics)
        raw = dict(metrics.get("raw_indicators", {}))
        if tf5 is not None:
            raw["5m"] = self._feature_metrics(tf5)
        metrics.update(
            {
                "raw_indicators": raw,
                "open_interest_usd": context.open_interest_usd,
                "open_interest_change_pct": context.open_interest_change_pct,
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
                "context_sampled_at": context.sampled_at,
                "context_complete": context.complete,
                "context_available_count": live.context_available_count,
                "context_required_count": live.context_required_count,
                "execution_notional_usdt": context.execution_notional_usdt,
                "bid_depth_usd": context.bid_depth_usd,
                "ask_depth_usd": context.ask_depth_usd,
                "buy_slippage_pct": context.buy_slippage_pct,
                "sell_slippage_pct": context.sell_slippage_pct,
                "execution_quality_complete": context.execution_quality_complete,
                "volume_ratio_5m": round(tf5.volume_ratio, 2) if tf5 is not None else None,
                "buy_pressure_5m_pct": (
                    round(tf5.directional_volume_ratio * 100.0, 1)
                    if tf5 is not None
                    else None
                ),
                "evidence_alignment_score": live.alignment_score,
                "conflict_severity": live.conflict_severity,
                "setup_maturity_1h": live.setup_maturity,
                "trigger_maturity_15m": live.trigger_maturity,
                "micro_acceleration_5m": live.micro_acceleration,
            }
        )

        updated = replace(
            state,
            readiness_score=live.readiness,
            status=live.stage,
            passed_conditions=live.supporting[:8],
            missing_conditions=_unique([*live.conflicts, *live.neutral])[:8],
            factor_scores={
                key: group.score for key, group in live.groups.items()
            },
            market_metrics=metrics,
            evidence_groups=live.group_dicts(),
            timeframe_states=live.timeframe_states,
            supporting_evidence=live.supporting,
            conflicts=live.conflicts,
            neutral_evidence=live.neutral,
            entry_quality=dict(live.entry_quality),
            summary=live.summary,
        )

        updated = self._set_safety(
            updated,
            "context_data",
            context.complete,
            "即時 Funding／Order Book／Taker 資料完整",
            f"{live.context_available_count}/{live.context_required_count}",
        )
        oi_ok = (
            self.config.min_open_interest_usd <= 0
            or (
                context.open_interest_usd is not None
                and context.open_interest_usd >= self.config.min_open_interest_usd
            )
        )
        updated = self._set_safety(
            updated,
            "open_interest",
            oi_ok,
            "持倉量符合基本流動性",
            context.open_interest_usd,
        )

        formal_candidate = (
            result.candidate_plan is not None
            and live.stage in ("EARLY_SIGNAL", "CONFIRMED")
        )
        requires_execution = (
            formal_candidate
            and context.execution_notional_usdt > 0
        )
        depth_ok = context.execution_quality_complete
        updated = self._set_safety(
            updated,
            "execution_depth",
            depth_ok or not requires_execution,
            "Order Book 深度足以估算成交",
            context.execution_notional_usdt,
        )

        direction = live.direction
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
        slippage_ok = (
            not requires_execution
            or (
                entry_slippage is not None
                and exit_slippage is not None
                and entry_slippage <= self.config.max_slippage_pct
                and exit_slippage <= self.config.max_slippage_pct
            )
        )
        updated = self._set_safety(
            updated,
            "slippage",
            slippage_ok,
            "預估滑價在安全範圍",
            max(
                float(entry_slippage or 0.0),
                float(exit_slippage or 0.0),
            ),
        )

        execution_cost_pct: float | None = None
        execution_cost_to_risk_pct: float | None = None
        if depth_ok and direction in ("LONG", "SHORT"):
            execution_cost_pct = round(
                state.spread_pct
                + float(entry_slippage or 0.0)
                + float(exit_slippage or 0.0)
                + self.config.estimated_taker_fee_pct * 2.0,
                4,
            )
            technical_stop_pct = float(metrics.get("technical_stop_pct", 0.0) or 0.0)
            if technical_stop_pct > 0:
                execution_cost_to_risk_pct = round(
                    execution_cost_pct / technical_stop_pct * 100.0,
                    1,
                )
        execution_ok = (
            not requires_execution
            or (
                execution_cost_to_risk_pct is not None
                and execution_cost_to_risk_pct
                <= self.config.max_execution_cost_to_risk_pct
            )
        )
        metrics.update(
            {
                "estimated_round_trip_cost_pct": execution_cost_pct,
                "execution_cost_to_risk_pct": execution_cost_to_risk_pct,
                "estimated_taker_fee_pct_each_side": self.config.estimated_taker_fee_pct,
                "execution_quality_label": (
                    "良好"
                    if execution_cost_to_risk_pct is not None
                    and execution_cost_to_risk_pct <= 8.0
                    else "正常"
                    if execution_cost_to_risk_pct is not None
                    and execution_cost_to_risk_pct
                    <= self.config.max_execution_cost_to_risk_pct
                    else "成本偏高"
                ),
            }
        )
        updated = replace(updated, market_metrics=metrics)
        updated = self._set_safety(
            updated,
            "execution_cost",
            execution_ok,
            "Execution Cost 在風險上限內",
            execution_cost_to_risk_pct,
        )
        major_conflict_ok = (
            live.conflict_severity < 55.0
            and sum(group.score < 35.0 for group in live.groups.values()) < 2
        )
        updated = self._set_safety(
            updated,
            "major_conflict",
            major_conflict_ok,
            "沒有重大跨群反向證據",
            live.conflict_severity,
        )

        failed_hard = [
            check
            for check in updated.safety_checks
            if check.get("hard", True) and not check.get("passed", False)
        ]
        if not major_conflict_ok and result.candidate_plan is not None:
            return AnalysisResult(
                None,
                "major_evidence_conflict",
                replace(updated, status="FILTERED"),
                live,
                result.candidate_plan,
                result.candidate_signal,
            )
        if failed_hard and formal_candidate:
            first = str(failed_hard[0].get("key", "safety"))
            reasons = {
                "context_data": "market_context_incomplete",
                "open_interest": "open_interest_too_low",
                "execution_depth": "execution_quality_unavailable",
                "slippage": "slippage_too_high",
                "execution_cost": "execution_cost_too_high",
                "major_conflict": "major_evidence_conflict",
            }
            return AnalysisResult(
                None,
                reasons.get(first, "safety_gate_failed"),
                replace(updated, status="FILTERED"),
                live,
                result.candidate_plan,
                result.candidate_signal,
            )

        if (
            live.stage not in ("EARLY_SIGNAL", "CONFIRMED")
            or result.candidate_plan is None
            or result.candidate_signal is None
        ):
            final_stage = (
                "NEAR_TRIGGER"
                if live.stage == "NEAR_TRIGGER"
                else "WATCH"
            )
            return AnalysisResult(
                None,
                "evidence_not_aligned",
                replace(
                    updated,
                    status=final_stage,
                    summary=summary_for_stage(live, final_stage),
                ),
                live,
                result.candidate_plan,
                result.candidate_signal,
            )

        plan = replace(
            result.candidate_plan,
            score=live.readiness,
            signal_stage=live.stage,
            evidence=live.supporting,
        )
        management = dict(plan.management_plan or {})
        management["review"] = "只在重新打開雷達或按下立即掃描時，以最新公開市場資料重新計算。"
        plan = replace(plan, management_plan=management)
        factor_scores = {key: group.score for key, group in live.groups.items()}
        signal = replace(
            result.candidate_signal,
            score=live.readiness,
            evidence=live.supporting,
            notes=_unique([*plan.notes, *live.neutral]),
            factor_scores=factor_scores,
            market_metrics=metrics,
            signal_stage=live.stage,
            readiness_score=live.readiness,
            evidence_groups=live.group_dicts(),
            timeframe_states=live.timeframe_states,
            supporting_evidence=live.supporting,
            conflicts=live.conflicts,
            neutral_evidence=live.neutral,
            safety_checks=list(updated.safety_checks),
            entry_quality=dict(live.entry_quality),
            summary=live.summary,
            management_plan=management,
            actionable=True,
            lifecycle={
                "previous_stage": None,
                "current_stage": live.stage,
                "transition": "NEW",
            },
        )
        return AnalysisResult(
            signal,
            "qualified",
            replace(updated, status=live.stage),
            live,
            plan,
            signal,
        )

    def _state_from_assessment(
        self,
        instrument: Instrument,
        ticker: Ticker,
        quote_volume_24h: float,
        closed_candle_ts: int,
        tf4: TimeframeFeatures,
        tf1: TimeframeFeatures,
        tf15: TimeframeFeatures,
        tf5: TimeframeFeatures | None,
        assessment: EvidenceAssessment,
    ) -> MarketState:
        checks = [
            self._safety_check("data_complete", "核心 K 線與 Ticker 完整", True),
            self._safety_check(
                "liquidity",
                "24H 成交額符合基本流動性",
                quote_volume_24h >= self.config.min_quote_volume_24h,
                quote_volume_24h,
            ),
            self._safety_check(
                "spread",
                "Spread 在安全上限內",
                ticker.spread_pct <= self.config.max_spread_pct,
                round(ticker.spread_pct, 4),
            ),
            self._safety_check(
                "chase",
                "未嚴重追價",
                assessment.entry_quality["key"] != "SEVERE_CHASE",
                assessment.entry_quality["extension_atr"],
            ),
        ]
        metrics = {
            "adx_1h": round(tf1.adx14, 1),
            "rsi_15m": round(tf15.rsi14, 1),
            "volume_ratio_1h": round(tf1.volume_ratio, 2),
            "volume_ratio_15m": round(tf15.volume_ratio, 2),
            "volume_ratio_5m": round(tf5.volume_ratio, 2) if tf5 else None,
            "atr_pct_15m": round(tf15.atr_pct, 2),
            "bollinger_width_pct_1h": round(tf1.bollinger_width_pct, 2),
            "vwap_position_15m": "ABOVE" if tf15.close >= tf15.vwap20 else "BELOW",
            "candle_buy_pressure_pct": round(tf15.directional_volume_ratio * 100.0, 1),
            "setup_maturity_1h": assessment.setup_maturity,
            "trigger_maturity_15m": assessment.trigger_maturity,
            "micro_acceleration_5m": assessment.micro_acceleration,
            "evidence_alignment_score": assessment.alignment_score,
            "conflict_severity": assessment.conflict_severity,
            "raw_indicators": {
                "4H": self._feature_metrics(tf4),
                "1H": self._feature_metrics(tf1),
                "15m": self._feature_metrics(tf15),
                **({"5m": self._feature_metrics(tf5)} if tf5 is not None else {}),
            },
        }
        return MarketState(
            inst_id=instrument.inst_id,
            regime=assessment.regime,
            direction=assessment.direction,
            preferred_strategy=self._strategy_name(assessment.regime),
            readiness_score=assessment.readiness,
            status=assessment.stage,
            missing_conditions=_unique([*assessment.conflicts, *assessment.neutral])[:8],
            spread_pct=round(ticker.spread_pct, 4),
            quote_volume_24h=round(quote_volume_24h, 2),
            closed_candle_ts=closed_candle_ts,
            passed_conditions=assessment.supporting[:8],
            factor_scores={key: group.score for key, group in assessment.groups.items()},
            market_metrics=metrics,
            evidence_groups=assessment.group_dicts(),
            timeframe_states=assessment.timeframe_states,
            supporting_evidence=assessment.supporting,
            conflicts=assessment.conflicts,
            neutral_evidence=assessment.neutral,
            safety_checks=checks,
            entry_quality=dict(assessment.entry_quality),
            summary=assessment.summary,
        )

    def _v2_plan(
        self,
        assessment: EvidenceAssessment,
        tf4: TimeframeFeatures,
        tf1: TimeframeFeatures,
        tf15: TimeframeFeatures,
    ) -> _Plan | None:
        if (
            assessment.direction not in ("LONG", "SHORT")
            or assessment.regime == "DISORDER"
            or assessment.setup_maturity < 48.0
            or assessment.trigger_maturity < 58.0
            or assessment.entry_quality["key"] == "SEVERE_CHASE"
            or assessment.conflict_severity >= 70.0
        ):
            return None
        direction = assessment.direction
        is_long = direction == "LONG"
        entry = tf15.close
        if assessment.regime == "RANGE":
            if is_long:
                stop = tf1.prior_low20 - 0.25 * tf1.atr14
                target = tf1.prior_low20 + (tf1.prior_high20 - tf1.prior_low20) * 0.55
                risk = entry - stop
                rr = (target - entry) / risk if risk > 0 else 0.0
                tp1 = target
                tp2 = tf1.prior_low20 + (tf1.prior_high20 - tf1.prior_low20) * 0.85
                invalidation = "1H 收盤有效跌破區間下緣，或觸及止損。"
            else:
                stop = tf1.prior_high20 + 0.25 * tf1.atr14
                target = tf1.prior_low20 + (tf1.prior_high20 - tf1.prior_low20) * 0.45
                risk = stop - entry
                rr = (entry - target) / risk if risk > 0 else 0.0
                tp1 = target
                tp2 = tf1.prior_low20 + (tf1.prior_high20 - tf1.prior_low20) * 0.15
                invalidation = "1H 收盤有效突破區間上緣，或觸及止損。"
        else:
            if is_long:
                stop = min(tf15.recent_low - 0.20 * tf15.atr14, entry - 1.15 * tf15.atr14)
                risk = entry - stop
                tp1 = entry + risk * self.config.minimum_rr
                tp2 = entry + risk * 2.7
                invalidation = "15m 收盤跌回觸發區並跌破最近結構低點，或觸及止損。"
            else:
                stop = max(tf15.recent_high + 0.20 * tf15.atr14, entry + 1.15 * tf15.atr14)
                risk = stop - entry
                tp1 = entry - risk * self.config.minimum_rr
                tp2 = entry - risk * 2.7
                invalidation = "15m 收盤站回觸發區並突破最近結構高點，或觸及止損。"
            rr = self.config.minimum_rr
            obstacle = self._nearest_obstacle(direction, entry, tf4, tf1)
            if obstacle is not None and risk > 0:
                headroom = (
                    (obstacle - entry) / risk
                    if is_long
                    else (entry - obstacle) / risk
                )
                if headroom < self.config.minimum_rr:
                    return None
        if risk <= 0 or rr < self.config.minimum_rr:
            return None
        stage = (
            assessment.stage
            if assessment.stage in ("EARLY_SIGNAL", "CONFIRMED")
            else "EARLY_SIGNAL"
        )
        return _Plan(
            direction=direction,
            strategy=self._strategy_name(assessment.regime),
            regime=assessment.regime,
            score=assessment.readiness,
            evidence=assessment.supporting,
            entry=entry,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            rr=rr,
            invalidation=invalidation,
            notes=[
                f"進場品質：{assessment.entry_quality['label']}（{assessment.entry_quality['extension_atr']:.2f} ATR）。",
                "只使用已收盤 K 線；5m 僅作加速與精細 Timing。",
            ],
            signal_stage=stage,
        )

    def _signal_from_plan(
        self,
        instrument: Instrument,
        ticker: Ticker,
        quote_volume_24h: float,
        closed_candle_ts: int,
        tf15: TimeframeFeatures,
        plan: _Plan,
        assessment: EvidenceAssessment,
        state: MarketState,
    ) -> Signal:
        zone_offset = tf15.atr14 * 0.12
        if plan.direction == "LONG":
            entry_low = plan.entry - zone_offset
            entry_high = plan.entry + zone_offset * 0.45
        else:
            entry_low = plan.entry - zone_offset * 0.45
            entry_high = plan.entry + zone_offset
        return Signal(
            inst_id=instrument.inst_id,
            direction=plan.direction,
            strategy=plan.strategy,
            score=round(assessment.readiness, 1),
            evidence=assessment.supporting,
            entry_low=_format_price(entry_low, instrument.tick_size),
            entry_high=_format_price(entry_high, instrument.tick_size),
            stop_loss=_format_price(plan.stop, instrument.tick_size),
            take_profit_1=_format_price(plan.tp1, instrument.tick_size),
            take_profit_2=_format_price(plan.tp2, instrument.tick_size),
            risk_reward=round(plan.rr, 2),
            invalidation=plan.invalidation,
            spread_pct=round(ticker.spread_pct, 4),
            quote_volume_24h=round(quote_volume_24h, 2),
            closed_candle_ts=closed_candle_ts,
            regime=plan.regime,
            notes=plan.notes,
            factor_scores=dict(state.factor_scores),
            market_metrics=dict(state.market_metrics),
            signal_stage=assessment.stage,
            trend_strength_label=plan.trend_strength_label,
            trend_strength_score=round(plan.trend_strength_score, 1),
            management_plan=dict(plan.management_plan or {}),
            readiness_score=assessment.readiness,
            evidence_groups=assessment.group_dicts(),
            timeframe_states=assessment.timeframe_states,
            supporting_evidence=assessment.supporting,
            conflicts=assessment.conflicts,
            neutral_evidence=assessment.neutral,
            safety_checks=list(state.safety_checks),
            entry_quality=dict(assessment.entry_quality),
            summary=assessment.summary,
            actionable=assessment.stage in ("EARLY_SIGNAL", "CONFIRMED"),
            lifecycle={
                "previous_stage": None,
                "current_stage": assessment.stage,
                "transition": "NEW",
            },
        )

    @staticmethod
    def _feature_metrics(tf: TimeframeFeatures) -> dict[str, float]:
        return {
            "close": round(tf.close, 10),
            "ma5": round(tf.sma5, 10),
            "ma10": round(tf.sma10, 10),
            "ma20": round(tf.sma20, 10),
            "ema21": round(tf.ema21, 10),
            "ema55": round(tf.ema55, 10),
            "ema21_slope_atr": round(tf.ema21_slope_atr, 4),
            "macd_line": round(tf.macd_line, 10),
            "macd_signal": round(tf.macd_signal, 10),
            "macd_hist": round(tf.macd_hist, 10),
            "macd_prev_hist": round(tf.macd_prev_hist, 10),
            "rsi14": round(tf.rsi14, 2),
            "adx14": round(tf.adx14, 2),
            "atr14": round(tf.atr14, 10),
            "atr_pct": round(tf.atr_pct, 4),
            "vwap20": round(tf.vwap20, 10),
            "bollinger_width_pct": round(tf.bollinger_width_pct, 4),
            "volume_ratio": round(tf.volume_ratio, 4),
            "directional_volume_ratio": round(tf.directional_volume_ratio, 4),
            "compression_ratio": round(tf.compression_ratio, 4),
            "extension_atr": round(tf.extension_atr, 4),
            "prior_high20": round(tf.prior_high20, 10),
            "prior_low20": round(tf.prior_low20, 10),
            "prior_high50": round(tf.prior_high50, 10),
            "prior_low50": round(tf.prior_low50, 10),
            "prior_high100": round(tf.prior_high100, 10),
            "prior_low100": round(tf.prior_low100, 10),
            "lower_wick_ratio": round(tf.lower_wick_ratio, 4),
            "upper_wick_ratio": round(tf.upper_wick_ratio, 4),
        }

    @staticmethod
    def _strategy_name(regime: str) -> str:
        return {
            "TREND": "趨勢回踩續行",
            "BREAKOUT_READY": "結構突破／續行",
            "RANGE": "區間邊緣反轉",
            "DISORDER": "等待型態清楚",
        }.get(regime, "等待型態清楚")

    @staticmethod
    def _safety_check(
        key: str,
        label: str,
        passed: bool,
        value: object | None = None,
        hard: bool = True,
    ) -> dict[str, object]:
        return {
            "key": key,
            "label": label,
            "passed": bool(passed),
            "hard": hard,
            "value": value,
        }

    def _set_safety(
        self,
        state: MarketState,
        key: str,
        passed: bool,
        label: str,
        value: object | None = None,
    ) -> MarketState:
        checks = [item for item in state.safety_checks if item.get("key") != key]
        checks.append(self._safety_check(key, label, passed, value))
        missing = list(state.missing_conditions)
        if not passed:
            missing = _unique([label, *missing])
        return replace(state, safety_checks=checks, missing_conditions=missing[:8])

    def _pass_safety(self, state: MarketState, key: str) -> MarketState:
        labels = {
            "risk_reward": "Minimum R:R 符合安全下限",
            "stop_loss": "可以建立合理 Stop Loss",
        }
        return self._set_safety(state, key, True, labels.get(key, key))

    def _fail_safety(self, state: MarketState, key: str, label: str) -> MarketState:
        return replace(
            self._set_safety(state, key, False, label),
            status="FILTERED",
        )

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

    def _apply_micro_preview(
        self,
        state: MarketState,
        tf5: TimeframeFeatures,
    ) -> MarketState:
        metrics = dict(state.market_metrics)
        factors = dict(state.factor_scores)
        passed = list(state.passed_conditions)
        missing = list(state.missing_conditions)
        direction = state.direction
        is_long = direction == "LONG"
        directional_pressure = (
            tf5.directional_volume_ratio
            if is_long
            else 1.0 - tf5.directional_volume_ratio
            if direction == "SHORT"
            else 0.5
        )
        threshold = (
            1.25
            if state.regime in ("TREND", "BREAKOUT_READY", "BREAKOUT")
            else 1.15
        )
        micro_support = (
            direction in ("LONG", "SHORT")
            and tf5.volume_ratio >= threshold
            and directional_pressure >= 0.52
        )
        micro_score = _clamp(
            (tf5.volume_ratio / max(threshold, 0.01) * 65.0)
            + _clamp((directional_pressure - 0.50) / 0.20 * 35.0, 0.0, 35.0),
            0.0,
            100.0,
        )
        metrics.update(
            {
                "volume_ratio_5m": round(tf5.volume_ratio, 2),
                "buy_pressure_5m_pct": round(tf5.directional_volume_ratio * 100.0, 1),
                "micro_preview_confirmed": micro_support,
                "micro_preview_score": round(micro_score, 1),
            }
        )
        if direction in ("LONG", "SHORT"):
            factors["volume_order_flow"] = round(
                (factors.get("volume_order_flow", 50.0) * 0.70) + (micro_score * 0.30),
                1,
            )
            if micro_support:
                passed.append("全市場 5m 成交異動預檢支持方向")
            else:
                missing.append("全市場 5m 成交異動預檢尚未支持方向")
        return replace(
            state,
            readiness_score=round(
                (state.readiness_score * 0.85) + (micro_score * 0.15),
                1,
            ),
            passed_conditions=_unique(passed)[:6],
            missing_conditions=_unique(missing)[:6],
            factor_scores=factors,
            market_metrics=metrics,
        )

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
        if direction == "LONG":
            room_1h_50 = (tf1.prior_high50 - tf1.close) / tf1.atr14
            room_1h_100 = (tf1.prior_high100 - tf1.close) / tf1.atr14
            room_4h_50 = (tf4.prior_high50 - tf4.close) / tf4.atr14
            room_4h_100 = (tf4.prior_high100 - tf4.close) / tf4.atr14
        elif direction == "SHORT":
            room_1h_50 = (tf1.close - tf1.prior_low50) / tf1.atr14
            room_1h_100 = (tf1.close - tf1.prior_low100) / tf1.atr14
            room_4h_50 = (tf4.close - tf4.prior_low50) / tf4.atr14
            room_4h_100 = (tf4.close - tf4.prior_low100) / tf4.atr14
        else:
            room_1h_50 = room_1h_100 = room_4h_50 = room_4h_100 = None
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
                "structure_room_1h_50_atr": round(room_1h_50, 2) if room_1h_50 is not None else None,
                "structure_room_1h_100_atr": round(room_1h_100, 2) if room_1h_100 is not None else None,
                "structure_room_4h_50_atr": round(room_4h_50, 2) if room_4h_50 is not None else None,
                "structure_room_4h_100_atr": round(room_4h_100, 2) if room_4h_100 is not None else None,
                "structure_windows": "20／50／100",
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

    @staticmethod
    def _nearest_obstacle(
        direction: str,
        entry: float,
        tf4: TimeframeFeatures,
        tf1: TimeframeFeatures,
    ) -> float | None:
        if direction == "LONG":
            levels = (
                tf1.prior_high20,
                tf1.prior_high50,
                tf1.prior_high100,
                tf4.prior_high20,
                tf4.prior_high50,
                tf4.prior_high100,
            )
            candidates = [level for level in levels if level > entry]
            return min(candidates, default=None)
        levels = (
            tf1.prior_low20,
            tf1.prior_low50,
            tf1.prior_low100,
            tf4.prior_low20,
            tf4.prior_low50,
            tf4.prior_low100,
        )
        candidates = [level for level in levels if level < entry]
        return max(candidates, default=None)

    def _early_expansion_plan(
        self,
        direction: str,
        tf4: TimeframeFeatures,
        tf1: TimeframeFeatures,
        tf15: TimeframeFeatures,
        tf5: TimeframeFeatures | None = None,
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
        participation_conditions = [
            tf15.volume_ratio >= 1.10,
            tf1.volume_ratio >= 1.05,
            pressure >= 0.54,
        ]
        if tf5 is not None:
            micro_pressure = (
                tf5.directional_volume_ratio
                if is_long
                else 1.0 - tf5.directional_volume_ratio
            )
            participation_conditions.extend(
                (tf5.volume_ratio >= 1.25, micro_pressure >= 0.54)
            )
        participation_votes = sum(participation_conditions)
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
        obstacle = self._nearest_obstacle(direction, entry, tf4, tf1)
        headroom_r = (
            ((obstacle - entry) if is_long else (entry - obstacle)) / risk
            if obstacle is not None
            else float("inf")
        )
        if headroom_r < self.config.minimum_rr:
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
                f"早期成交參與支持票 {participation_votes}／{len(participation_conditions)}",
                (
                    f"中長結構前方空間 {headroom_r:.2f}R"
                    if math.isfinite(headroom_r)
                    else "20／50／100 根中長結構前方沒有近距離障礙"
                ),
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
            signal_stage="EARLY_SIGNAL",
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
        obstacle = self._nearest_obstacle(direction, entry, tf4, tf1)
        headroom_r = (
            ((obstacle - entry) if direction == "LONG" else (entry - obstacle)) / risk
            if obstacle is not None
            else float("inf")
        )
        if headroom_r < self.config.minimum_rr:
            return None
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
                (
                    f"20／50／100 根結構前方空間 {headroom_r:.2f}R"
                    if math.isfinite(headroom_r)
                    else "20／50／100 根結構前方沒有近距離障礙"
                ),
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
            obstacle = self._nearest_obstacle(direction, entry, tf4, tf1)
            headroom_r = (obstacle - entry) / risk if obstacle is not None else float("inf")
            if headroom_r < self.config.minimum_rr:
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
            obstacle = self._nearest_obstacle(direction, entry, tf4, tf1)
            headroom_r = (entry - obstacle) / risk if obstacle is not None else float("inf")
            if headroom_r < self.config.minimum_rr:
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
                (
                    f"20／50／100 根結構前方空間 {headroom_r:.2f}R"
                    if math.isfinite(headroom_r)
                    else "20／50／100 根結構前方沒有近距離障礙"
                ),
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


def _entry_eligibility(
    *,
    direction: str,
    current_price: float,
    entry_low: float,
    entry_high: float,
    stop: float,
    target: float,
    atr: float,
    stage: str,
    minimum_rr: float,
    ready_max_chase_atr: float,
    missed_chase_atr: float,
) -> dict[str, Any]:
    is_long = direction == "LONG"
    safe_atr = max(abs(atr), 1e-9)
    favorable_distance = (
        max(0.0, current_price - entry_high)
        if is_long
        else max(0.0, entry_low - current_price)
    )
    adverse_outside = current_price < entry_low if is_long else current_price > entry_high
    chase_atr = favorable_distance / safe_atr
    current_risk = current_price - stop if is_long else stop - current_price
    current_reward = target - current_price if is_long else current_price - target
    remaining_rr = current_reward / current_risk if current_risk > 0 else -1.0
    active_stage = stage in ("EARLY_SIGNAL", "CONFIRMED", "REENTRY")

    if not active_stage:
        status = "MISSED_ENTRY"
        label = "已錯過｜生命週期已離開進場階段"
        reason = "訊號仍保留作追蹤，但目前階段不再提供新進場。"
    elif current_risk <= 0:
        status = "MISSED_ENTRY"
        label = "已失效｜禁止進場"
        reason = "價格已越過原失效／止損位置。"
    elif chase_atr > missed_chase_atr or remaining_rr < minimum_rr:
        status = "MISSED_ENTRY"
        label = "已錯過｜禁止追價"
        reason = (
            f"順向偏離 {chase_atr:.2f} ATR，剩餘風報 {remaining_rr:.2f}R；"
            f"門檻為 {missed_chase_atr:.2f} ATR 內且至少 {minimum_rr:.2f}R。"
        )
    elif adverse_outside or chase_atr > ready_max_chase_atr:
        status = "WAIT_RETEST"
        label = "等待回踩／重新確認"
        reason = (
            f"目前偏離理想進場區 {chase_atr:.2f} ATR；"
            "等待價格回到 Entry Zone，不追價。"
        )
    else:
        status = "ENTRY_READY"
        label = "目前可進｜仍在合理區"
        reason = (
            f"順向偏離 {chase_atr:.2f} ATR，剩餘風報 {remaining_rr:.2f}R。"
        )

    return {
        "status": status,
        "label": label,
        "reason": reason,
        "actionable": status == "ENTRY_READY",
        "trigger_price": round((entry_low + entry_high) / 2.0, 12),
        "current_price": round(current_price, 12),
        "chase_atr": round(chase_atr, 3),
        "remaining_rr": round(remaining_rr, 3),
        "entry_low": round(entry_low, 12),
        "entry_high": round(entry_high, 12),
        "ready_max_chase_atr": ready_max_chase_atr,
        "missed_chase_atr": missed_chase_atr,
        "minimum_rr": minimum_rr,
        "time_alone_never_invalidates": True,
    }


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


def _price_change_pct(current: float, baseline: float) -> float | None:
    if not math.isfinite(current) or not math.isfinite(baseline) or baseline <= 0:
        return None
    return round((current - baseline) / baseline * 100.0, 3)
