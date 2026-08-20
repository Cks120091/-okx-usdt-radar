from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .indicators import TimeframeFeatures, atr, ema_series, features
from .models import Candle, MarketContext


STRATEGY_VERSION = "V3.3_MASTER"
FEATURE_SCHEMA_VERSION = "3.3.0"


@dataclass(frozen=True)
class DynamicZone:
    side: str
    tier: str
    lower: float
    upper: float
    center: float
    tests: int
    rejections: int
    last_touch_bars: int
    source: str
    role: str = "ORIGINAL"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["width"] = self.upper - self.lower
        return payload


@dataclass(frozen=True)
class AttackWave:
    direction: str
    start_index: int
    end_index: int
    start_ts: int
    end_ts: int
    duration_bars: int
    price_move_pct: float
    price_result_atr: float
    macd_extreme: float
    time_efficiency: float
    follow_through: float
    retracement: float
    efficiency: float
    quality: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StoryAssessment:
    horizon: str
    regime: str
    direction: str
    direction_state: str
    direction_label: str
    bias_direction: str
    trigger_direction: str
    trigger_type: str
    stage: str
    freshness: str
    event_ts: int
    event_age_bars: int
    trigger: dict[str, Any]
    zones: dict[str, Any]
    location: dict[str, Any]
    attack_waves: dict[str, list[dict[str, Any]]]
    attack_efficiency: dict[str, Any]
    price_acceptance: dict[str, Any]
    control_transfer: dict[str, Any]
    timeframe_states: dict[str, dict[str, Any]]
    groups: dict[str, dict[str, Any]]
    supporting: list[str]
    conflicts: list[str]
    neutral: list[str]
    summary: str
    invalidation_price: float | None
    raw: dict[str, Any]
    market_participation: dict[str, Any] = field(default_factory=dict)
    execution_quality: dict[str, Any] = field(default_factory=dict)
    data_quality: dict[str, Any] = field(default_factory=dict)

    @property
    def triggered(self) -> bool:
        return bool(self.trigger.get("triggered"))

    @property
    def readiness(self) -> float:
        # Compatibility telemetry only. It is never the Trigger gate.
        return float(self.trigger.get("explainability_score", 0.0))

    @property
    def entry_quality(self) -> dict[str, Any]:
        return dict(self.execution_quality.get("entry_location", {}))

    def group_dicts(self) -> dict[str, dict[str, Any]]:
        return {key: dict(value) for key, value in self.groups.items()}

    def story_dict(self) -> dict[str, Any]:
        return {
            "where": self.location,
            "attack_waves": self.attack_waves,
            "attack_efficiency": self.attack_efficiency,
            "price_acceptance": self.price_acceptance,
            "control_transfer": self.control_transfer,
            "zones": self.zones,
            "trigger": self.trigger,
            "raw": self.raw,
            "strategy_version": STRATEGY_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
        }


class MarketStoryEngine:
    """Price-first V3.3 engine.

    Scores in this module are explanatory telemetry.  A formal Trigger is
    created only from recorded price facts: a useful Zone, a meaningful attack
    / response, real Push-Away or Price Acceptance, MA/MACD response inside one
    bounded window, and a closed core candle.
    """

    def __init__(
        self,
        early_signal_max_age_bars: int = 2,
        max_early_entry_extension_atr: float = 0.50,
    ):
        self.early_signal_max_age_bars = max(1, min(int(early_signal_max_age_bars), 5))
        self.max_early_entry_extension_atr = max(
            0.10,
            float(max_early_entry_extension_atr),
        )

    def analyze_short(
        self,
        candles_4h: list[Candle],
        candles_1h: list[Candle],
        candles_15m: list[Candle],
        candles_5m: list[Candle] | None = None,
        previous_story: dict[str, Any] | None = None,
    ) -> StoryAssessment:
        return self._analyze(
            horizon="SHORT",
            higher_candles=candles_4h,
            bias_candles=candles_1h,
            core_candles=candles_15m,
            timing_candles=candles_5m,
            frame_names=("4H", "1H", "15m", "5m"),
            previous_story=previous_story,
        )

    def analyze_long(
        self,
        candles_1d: list[Candle],
        candles_4h: list[Candle],
        candles_1h: list[Candle],
        previous_story: dict[str, Any] | None = None,
    ) -> StoryAssessment:
        return self._analyze(
            horizon="LONG",
            higher_candles=candles_1d,
            bias_candles=candles_1d,
            core_candles=candles_4h,
            timing_candles=candles_1h,
            frame_names=("1D", "1D", "4H", "1H"),
            previous_story=previous_story,
        )

    def _analyze(
        self,
        horizon: str,
        higher_candles: list[Candle],
        bias_candles: list[Candle],
        core_candles: list[Candle],
        timing_candles: list[Candle] | None,
        frame_names: tuple[str, str, str, str],
        previous_story: dict[str, Any] | None,
    ) -> StoryAssessment:
        if min(len(higher_candles), len(bias_candles), len(core_candles)) < 60:
            raise ValueError("V3.3 requires at least 60 closed candles per core timeframe")
        if timing_candles is not None and len(timing_candles) < 60:
            timing_candles = None

        tf_higher = features(higher_candles)
        tf_bias = features(bias_candles)
        tf_core = features(core_candles)
        tf_timing = features(timing_candles) if timing_candles else None
        regime = _regime(tf_core)
        higher_long = _direction_score(tf_higher)
        bias_long = _direction_score(tf_bias)
        core_long = _direction_score(tf_core)
        long_score = (
            higher_long * 0.20 + bias_long * 0.40 + core_long * 0.40
            if horizon == "SHORT"
            else bias_long * 0.60 + core_long * 0.40
        )
        direction, direction_state, direction_label = _direction_state(long_score)

        zones = _dynamic_zones(core_candles, tf_core)
        location = _price_location(tf_core.close, tf_core.atr14, zones)
        waves = _attack_waves(core_candles, tf_core.atr14)
        efficiencies = {
            "BULL": _compare_attacks(waves.get("BULL", [])),
            "BEAR": _compare_attacks(waves.get("BEAR", [])),
        }
        compression = _compression_state(core_candles, tf_core, zones, efficiencies)
        confirmation_window = _confirmation_window(regime, tf_core)

        candidates = {
            candidate_direction: _trigger_candidate(
                candidate_direction,
                core_candles,
                tf_core,
                tf_bias,
                zones,
                location,
                efficiencies,
                compression,
                regime,
                confirmation_window,
                self.early_signal_max_age_bars,
                self.max_early_entry_extension_atr,
            )
            for candidate_direction in ("LONG", "SHORT")
        }
        selected = _select_candidate(candidates, direction)
        trigger_direction = str(selected.get("direction", "NEUTRAL"))
        stage = str(selected.get("stage", "WATCH"))
        freshness = str(selected.get("freshness", "NONE"))
        event_ts = int(selected.get("event_ts", core_candles[-1].ts))
        event_age = int(selected.get("event_age_bars", 0))

        prior_active = (previous_story or {}).get("active_trigger_direction")
        prior_invalidated = bool((previous_story or {}).get("invalidated", False))
        if (
            bool(selected.get("triggered"))
            and selected.get("type") == "CONTINUATION"
            and event_age <= self.early_signal_max_age_bars
            and prior_active == trigger_direction
            and not prior_invalidated
        ):
            selected = dict(selected)
            selected["stage"] = "REENTRY"
            selected["freshness"] = "REACTIVATED"
            stage = "REENTRY"
            freshness = "REACTIVATED"
        if (
            prior_active in ("LONG", "SHORT")
            and trigger_direction not in ("NEUTRAL", prior_active)
            and not prior_invalidated
            and not bool(selected.get("price_invalidated_previous"))
        ):
            selected = dict(selected)
            selected["opposite_warning_only"] = True
            selected["triggered"] = False
            selected["stage"] = "NEAR_TRIGGER"
            selected.setdefault("conflicts", []).append(
                "原方向尚未被價格失效，反向變化先列警告"
            )
            stage = "NEAR_TRIGGER"
            freshness = "NONE"

        acceptance = dict(selected.get("price_acceptance", {}))
        control = dict(selected.get("control_transfer", {}))
        supporting = _unique(
            [
                *selected.get("supporting", []),
                f"{frame_names[2]} 使用已收盤 K 線判定",
            ]
        )
        conflicts = _unique(
            [
                *selected.get("conflicts", []),
                *_context_conflicts(trigger_direction, higher_long, bias_long, horizon),
            ]
        )
        neutral = _unique(
            [
                *selected.get("neutral", []),
                "市場參與深度資料尚未加入判定；不影響價格 Trigger",
            ]
        )
        groups = _evidence_groups(
            trigger_direction,
            selected,
            long_score,
            location,
            supporting,
            conflicts,
            neutral,
        )
        timeframe_states = _timeframe_states(
            horizon,
            frame_names,
            higher_long,
            bias_long,
            selected,
            tf_timing,
            trigger_direction,
        )
        summary = _human_summary(
            trigger_direction,
            stage,
            location,
            efficiencies,
            acceptance,
            control,
            compression,
            bool(selected.get("triggered")),
        )
        entry_location = _entry_location_quality(
            tf_core.extension_atr,
            stage,
        )
        data_quality = {
            "core": "AVAILABLE",
            "core_candle_count": len(core_candles),
            "core_timestamp": core_candles[-1].ts,
            "closed_candle": bool(core_candles[-1].confirmed),
            "deep": "PENDING",
            "missing_sources": [],
        }
        raw = {
            "direction_long_score": round(long_score, 1),
            "higher_long_score": round(higher_long, 1),
            "bias_long_score": round(bias_long, 1),
            "core_long_score": round(core_long, 1),
            "confirmation_window_bars": confirmation_window,
            "compression": compression,
            "core_close": tf_core.close,
            "core_high": core_candles[-1].high,
            "core_low": core_candles[-1].low,
            "core_atr": tf_core.atr14,
            "core_return_pct": _pct_change(core_candles[-1].close, core_candles[-2].close),
            "noise": selected.get("noise", {}),
        }
        selected = dict(selected)
        selected["trigger_event_key"] = (
            f"{horizon}:{trigger_direction}:{selected.get('type', 'NONE')}:"
            f"{event_ts}:{selected.get('zone_key', 'NO_ZONE')}"
        )
        selected["strategy_version"] = STRATEGY_VERSION
        selected["feature_schema_version"] = FEATURE_SCHEMA_VERSION
        return StoryAssessment(
            horizon=horizon,
            regime=regime,
            direction=direction,
            direction_state=direction_state,
            direction_label=direction_label,
            bias_direction="LONG" if bias_long >= 56.0 else "SHORT" if bias_long <= 44.0 else "NEUTRAL",
            trigger_direction=trigger_direction,
            trigger_type=str(selected.get("type", "NONE")),
            stage=stage,
            freshness=freshness,
            event_ts=event_ts,
            event_age_bars=event_age,
            trigger=selected,
            zones={key: value.to_dict() if value else None for key, value in zones.items()},
            location=location,
            attack_waves={key: [wave.to_dict() for wave in value] for key, value in waves.items()},
            attack_efficiency=efficiencies,
            price_acceptance=acceptance,
            control_transfer=control,
            timeframe_states=timeframe_states,
            groups=groups,
            supporting=supporting,
            conflicts=conflicts,
            neutral=neutral,
            summary=summary,
            invalidation_price=selected.get("invalidation_price"),
            raw=raw,
            market_participation={
                "state": "DATA_PENDING",
                "label": "資料待取得",
                "supporting": [],
                "conflicts": [],
                "neutral": ["深度資料尚未取得"],
            },
            execution_quality={
                "score": entry_location["score"],
                "label": entry_location["label"],
                "entry_location": entry_location,
                "reasons": [],
                "is_historical_win_rate": False,
            },
            data_quality=data_quality,
        )


def enrich_story_context(
    story: StoryAssessment,
    context: MarketContext,
    timing: TimeframeFeatures | None,
    market_bias: dict[str, object] | None = None,
) -> StoryAssessment:
    """Attach deep data without mutating or cancelling the core Trigger."""

    direction = story.trigger_direction if story.trigger_direction in ("LONG", "SHORT") else story.direction
    sign = 1.0 if direction == "LONG" else -1.0
    supporting: list[str] = []
    conflicts: list[str] = []
    neutral: list[str] = []
    available: list[str] = []
    missing = list(context.failures)
    price_move = float(story.raw.get("core_return_pct", 0.0) or 0.0)
    timeframe_states = {
        key: dict(value) for key, value in story.timeframe_states.items()
    }

    if context.taker_buy_ratio is not None:
        available.append("taker")
        directional_taker = (
            context.taker_buy_ratio if direction == "LONG" else 1.0 - context.taker_buy_ratio
        )
        if directional_taker >= 0.60 and price_move * sign > 0.02:
            supporting.append("主動成交與價格成果同向")
        elif directional_taker >= 0.62 and price_move * sign <= 0.0:
            conflicts.append("主動成交很強但價格推不動，可能遭對手吸收")
        elif directional_taker <= 0.35 and price_move * sign < 0.0:
            conflicts.append("主動成交與價格反應明顯反向")
        else:
            neutral.append("主動成交中性")
    else:
        missing.append("taker")

    if context.open_interest_change_pct is not None:
        available.append("open_interest_change")
        oi_change = context.open_interest_change_pct
        if oi_change >= 0.5 and price_move * sign > 0:
            supporting.append("價格與持倉量同向增加，顯示新增部位參與")
        elif oi_change <= -0.8:
            neutral.append("持倉量下降，行情可能由平倉或回補推動")
        else:
            neutral.append("持倉量變化中性；OI 本身不判多空")
    elif context.open_interest_usd is None:
        missing.append("open_interest")

    if context.funding_rate is not None:
        available.append("funding")
        directional_funding = context.funding_rate * sign
        if directional_funding > 0.0008:
            conflicts.append("同方向 Funding 極端擁擠")
        elif directional_funding > 0.0005:
            neutral.append("同方向 Funding 偏擁擠")
        else:
            neutral.append("Funding 未見極端擁擠")
    else:
        missing.append("funding")

    sequence = context.order_book_sequence or {}
    sequence_state = str(sequence.get("state", ""))
    if sequence_state:
        available.append("order_book_sequence")
        if sequence_state in ("PERSISTENT_SUPPORT", "REFILL_ABSORPTION"):
            supporting.append(str(sequence.get("reason", "委託簿時間序列支持方向")))
        elif sequence_state in ("LIQUIDITY_WITHDRAWAL", "PERSISTENT_OPPOSITION"):
            conflicts.append(str(sequence.get("reason", "委託簿時間序列形成反向證據")))
        else:
            neutral.append(str(sequence.get("reason", "委託簿時間序列中性")))
    elif context.order_book_imbalance is not None:
        available.append("order_book_snapshot")
        signed_book = context.order_book_imbalance * sign
        if signed_book >= 0.20:
            neutral.append("單張委託簿偏同向，但未經時間序列驗證")
        elif signed_book <= -0.20:
            conflicts.append("委託簿快照明顯反向；仍需防撤單與假牆")
        else:
            neutral.append("委託簿中性")
    else:
        missing.append("order_book")

    if context.cvd is not None:
        available.append("cvd")
        if context.cvd * sign > 0 and price_move * sign > 0:
            supporting.append("近期 CVD 與價格成果同向")
        elif context.cvd * sign > 0 and price_move * sign <= 0:
            conflicts.append("CVD 同向但價格未跟進，可能存在吸收")
        else:
            neutral.append("CVD 未提供額外支持")

    if timing is not None:
        timing_score = _direction_score(timing)
        timing_direction, _, timing_label = _direction_state(timing_score)
        timing_key = "5m" if story.horizon == "SHORT" else "1H"
        timeframe_states[timing_key] = {
            "role": "Timing／預警／加速",
            "direction": timing_direction,
            "label": (
                f"{_direction_cn(timing_direction)}加速"
                if timing_direction in ("LONG", "SHORT")
                else timing_label
            ),
            "score": round(timing_score, 1),
            "can_block_trigger": False,
        }
        directional_timing = timing_score if direction == "LONG" else 100.0 - timing_score
        if directional_timing >= 62.0:
            supporting.append("Timing 週期加速")
        elif directional_timing <= 35.0:
            conflicts.append("Timing 週期反向，只列短線降速警告")
        else:
            neutral.append("Timing 週期中性")

    macro = market_bias or {"score": 50.0, "label": "中性"}
    macro_score = float(macro.get("score", 50.0) or 50.0)
    directional_macro = macro_score if direction == "LONG" else 100.0 - macro_score
    if directional_macro < 30.0:
        conflicts.append("全市場背景與 Trigger 方向相反（逆勢）")
    elif directional_macro < 45.0:
        neutral.append("全市場背景未支持此方向")

    if conflicts and supporting:
        state, label = "CONFLICT", "支持中帶反證"
    elif conflicts:
        state, label = "CONFLICT", "存在反向證據"
    elif supporting:
        state, label = "SUPPORT", "支持"
    elif available:
        state, label = "NEUTRAL", "中性"
    else:
        state, label = "DATA_MISSING", "資料暫缺"
    participation = {
        "state": state,
        "label": label,
        "supporting": _unique(supporting),
        "conflicts": _unique(conflicts),
        "neutral": _unique(neutral),
        "available_sources": sorted(set(available)),
        "missing_sources": sorted(set(missing)),
        "complete": context.complete,
        "permission": "CONTEXT_ONLY_NEVER_CANCELS_TRIGGER",
    }
    groups = {key: dict(value) for key, value in story.groups.items()}
    participation_score = 70.0 if state == "SUPPORT" else 35.0 if state == "CONFLICT" else 50.0
    groups["participation_flow"] = _group(
        "participation_flow",
        "市場參與",
        participation_score,
        supporting,
        conflicts,
        neutral,
        confidence=min(100.0, 25.0 + len(set(available)) * 15.0),
    )
    data_quality = dict(story.data_quality)
    data_quality.update(
        {
            "deep": "AVAILABLE" if available else "MISSING",
            "deep_available_sources": sorted(set(available)),
            "missing_sources": sorted(set(missing)),
            "context_sampled_at": context.sampled_at,
        }
    )
    return replace(
        story,
        groups=groups,
        timeframe_states=timeframe_states,
        market_participation=participation,
        supporting=_unique([*story.supporting, *supporting]),
        conflicts=_unique([*story.conflicts, *conflicts]),
        neutral=_unique([*story.neutral, *neutral]),
        data_quality=data_quality,
    )


def execution_quality(
    story: StoryAssessment,
    spread_pct: float,
    risk_pct: float,
    risk_reward: float,
    context: MarketContext | None,
    target_rr: float = 1.8,
    max_cost_to_risk_pct: float = 12.0,
    max_spread_pct: float = 0.10,
    max_slippage_pct: float = 0.15,
    estimated_taker_fee_pct: float = 0.05,
) -> dict[str, Any]:
    reasons: list[str] = []
    warnings: list[str] = []
    entry = dict(story.execution_quality.get("entry_location", {}))
    score = float(entry.get("score", 50.0)) * 0.30
    spread_score = _clamp(
        100.0 - spread_pct / max(max_spread_pct, 0.001) * 50.0,
        0.0,
        100.0,
    )
    score += spread_score * 0.20
    rr_score = _clamp(risk_reward / max(target_rr, 0.1) * 75.0, 0.0, 100.0)
    score += rr_score * 0.25
    stop_score = 90.0 if 0 < risk_pct <= 2.5 else 60.0 if risk_pct <= 5.0 else 20.0
    score += stop_score * 0.15
    execution_cost = None
    execution_to_risk = None
    if context is not None and context.execution_quality_complete:
        direction = story.trigger_direction
        entry_slippage = context.buy_slippage_pct if direction == "LONG" else context.sell_slippage_pct
        exit_slippage = context.sell_slippage_pct if direction == "LONG" else context.buy_slippage_pct
        execution_cost = (
            spread_pct
            + float(entry_slippage or 0.0)
            + float(exit_slippage or 0.0)
            + estimated_taker_fee_pct * 2.0
        )
        execution_to_risk = execution_cost / risk_pct * 100.0 if risk_pct > 0 else None
        cost_score = _clamp(
            100.0
            - float(execution_to_risk or 100.0)
            * (50.0 / max(max_cost_to_risk_pct, 0.1)),
            0.0,
            100.0,
        )
        score += cost_score * 0.10
        if execution_to_risk is not None and execution_to_risk > max_cost_to_risk_pct:
            warnings.append("交易成本占原始風險偏高")
        if max(float(entry_slippage or 0.0), float(exit_slippage or 0.0)) > max_slippage_pct:
            warnings.append("估算滑價偏高")
    else:
        score += 50.0 * 0.10
        warnings.append("Order Book 深度不足，無法完整估算成交成本")
    if risk_reward < target_rr:
        warnings.append(f"目前結構 R:R 低於 {target_rr:.1f}R")
    if entry.get("key") in ("EXTENDED", "HIGHLY_EXTENDED", "SEVERE_CHASE"):
        warnings.append("價格已有延伸，注意追價")
    if spread_pct > max_spread_pct:
        warnings.append("Spread 偏高")
    reasons.extend(
        [
            f"位置：{entry.get('label', '待評估')}",
            f"結構 R:R：{risk_reward:.2f}R",
            f"Stop 距離：{risk_pct:.2f}%",
        ]
    )
    final_score = round(_clamp(score, 0.0, 100.0), 1)
    label = "良好" if final_score >= 75.0 else "普通" if final_score >= 50.0 else "偏低"
    recommendation = "NORMAL" if final_score >= 60.0 else "CAUTION" if final_score >= 35.0 else "AVOID_EXECUTION"
    return {
        "score": final_score,
        "label": label,
        "recommendation": recommendation,
        "reasons": reasons,
        "warnings": _unique(warnings),
        "entry_location": entry,
        "spread_pct": round(spread_pct, 4),
        "risk_pct": round(risk_pct, 4),
        "risk_reward": round(risk_reward, 2),
        "quality_thresholds": {
            "target_rr": target_rr,
            "max_cost_to_risk_pct": max_cost_to_risk_pct,
            "max_spread_pct": max_spread_pct,
            "max_slippage_pct": max_slippage_pct,
            "estimated_taker_fee_pct": estimated_taker_fee_pct,
        },
        "estimated_round_trip_cost_pct": round(execution_cost, 4) if execution_cost is not None else None,
        "execution_cost_to_risk_pct": round(execution_to_risk, 1) if execution_to_risk is not None else None,
        "is_historical_win_rate": False,
        "permission": "NEVER_CREATES_OR_CANCELS_TRIGGER",
    }


def _dynamic_zones(
    candles: list[Candle],
    tf: TimeframeFeatures,
) -> dict[str, DynamicZone | None]:
    history = candles[:-1]
    close = tf.close
    width = _clamp(tf.atr14 * 0.28, close * 0.0006, min(tf.atr14 * 0.70, close * 0.012))
    pivot_highs = _pivots(history, "HIGH")
    pivot_lows = _pivots(history, "LOW")
    high_candidates = [*pivot_highs, (len(history) - 1, tf.prior_high20), (max(0, len(history) - 50), tf.prior_high50)]
    low_candidates = [*pivot_lows, (len(history) - 1, tf.prior_low20), (max(0, len(history) - 50), tf.prior_low50)]
    high_clusters = _cluster_levels(high_candidates, width)
    low_clusters = _cluster_levels(low_candidates, width)
    resistance = _nearest_zone(high_clusters, close, width, "RESISTANCE", len(history))
    support = _nearest_zone(low_clusters, close, width, "SUPPORT", len(history))
    major_resistance = _outer_zone(high_clusters, close, width, "RESISTANCE", len(history))
    major_support = _outer_zone(low_clusters, close, width, "SUPPORT", len(history))

    recent = history[-8:]
    micro_high = max(item.high for item in recent)
    micro_low = min(item.low for item in recent)
    micro_resistance = _single_zone(
        "RESISTANCE", "MICRO", micro_high, width * 0.62, history, "recent_8_high"
    )
    micro_support = _single_zone(
        "SUPPORT", "MICRO", micro_low, width * 0.62, history, "recent_8_low"
    )
    return {
        "support": support,
        "resistance": resistance,
        "major_support": major_support,
        "major_resistance": major_resistance,
        "micro_support": micro_support,
        "micro_resistance": micro_resistance,
    }


def _pivots(candles: list[Candle], side: str, wing: int = 2) -> list[tuple[int, float]]:
    output: list[tuple[int, float]] = []
    for index in range(wing, len(candles) - wing):
        window = candles[index - wing : index + wing + 1]
        value = candles[index].high if side == "HIGH" else candles[index].low
        values = [item.high if side == "HIGH" else item.low for item in window]
        if (side == "HIGH" and value == max(values)) or (side == "LOW" and value == min(values)):
            output.append((index, value))
    return output[-24:]


def _cluster_levels(
    candidates: list[tuple[int, float]],
    tolerance: float,
) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    for index, value in sorted(candidates, key=lambda item: item[1]):
        target = next((row for row in clusters if abs(row["center"] - value) <= tolerance), None)
        if target is None:
            clusters.append({"values": [value], "indices": [index], "center": value})
        else:
            target["values"].append(value)
            target["indices"].append(index)
            target["center"] = sum(target["values"]) / len(target["values"])
    return clusters


def _nearest_zone(
    clusters: list[dict[str, Any]],
    close: float,
    width: float,
    side: str,
    history_length: int,
) -> DynamicZone | None:
    if not clusters:
        return None
    if side == "RESISTANCE":
        eligible = [row for row in clusters if row["center"] >= close - width * 2.2]
        selected = min(eligible or clusters, key=lambda row: abs(row["center"] - close))
    else:
        eligible = [row for row in clusters if row["center"] <= close + width * 2.2]
        selected = min(eligible or clusters, key=lambda row: abs(row["center"] - close))
    return _cluster_zone(selected, side, "SECONDARY", width, history_length)


def _outer_zone(
    clusters: list[dict[str, Any]],
    close: float,
    width: float,
    side: str,
    history_length: int,
) -> DynamicZone | None:
    if not clusters:
        return None
    eligible = (
        [row for row in clusters if row["center"] >= close]
        if side == "RESISTANCE"
        else [row for row in clusters if row["center"] <= close]
    )
    if not eligible:
        eligible = clusters
    selected = max(eligible, key=lambda row: (len(row["values"]), max(row["indices"])))
    return _cluster_zone(selected, side, "MAJOR", width * 1.18, history_length)


def _cluster_zone(
    cluster: dict[str, Any],
    side: str,
    tier: str,
    width: float,
    history_length: int,
) -> DynamicZone:
    center = float(cluster["center"])
    tests = len(cluster["values"])
    last_index = max(cluster["indices"])
    return DynamicZone(
        side=side,
        tier=tier,
        lower=center - width,
        upper=center + width,
        center=center,
        tests=tests,
        rejections=max(0, tests - 1),
        last_touch_bars=max(0, history_length - 1 - last_index),
        source="swing_cluster",
    )


def _single_zone(
    side: str,
    tier: str,
    center: float,
    width: float,
    history: list[Candle],
    source: str,
) -> DynamicZone:
    tests = sum(
        1
        for candle in history[-30:]
        if candle.low - width <= center <= candle.high + width
    )
    return DynamicZone(
        side=side,
        tier=tier,
        lower=center - width,
        upper=center + width,
        center=center,
        tests=max(1, tests),
        rejections=max(0, tests - 1),
        last_touch_bars=0,
        source=source,
    )


def _price_location(
    close: float,
    atr_value: float,
    zones: dict[str, DynamicZone | None],
) -> dict[str, Any]:
    support = zones.get("support")
    resistance = zones.get("resistance")
    support_distance = (close - support.center) / atr_value if support and atr_value > 0 else None
    resistance_distance = (resistance.center - close) / atr_value if resistance and atr_value > 0 else None
    if support and support.lower <= close <= support.upper:
        key, label = "IN_SUPPORT", "支撐 Zone 內"
    elif resistance and resistance.lower <= close <= resistance.upper:
        key, label = "IN_RESISTANCE", "壓力 Zone 內"
    elif support_distance is not None and 0 <= support_distance <= 0.75:
        key, label = "NEAR_SUPPORT", "支撐附近"
    elif resistance_distance is not None and 0 <= resistance_distance <= 0.75:
        key, label = "NEAR_RESISTANCE", "壓力附近"
    elif resistance and close > resistance.upper:
        key, label = "ABOVE_RESISTANCE", "壓力上方／等待接受度"
    elif support and close < support.lower:
        key, label = "BELOW_SUPPORT", "支撐下方／等待接受度"
    else:
        key, label = "RANGE_MIDDLE", "區間中央"
    return {
        "key": key,
        "label": label,
        "support_distance_atr": round(support_distance, 3) if support_distance is not None else None,
        "resistance_distance_atr": round(resistance_distance, 3) if resistance_distance is not None else None,
    }


def _attack_waves(candles: list[Candle], atr_value: float) -> dict[str, list[AttackWave]]:
    closes = [item.close for item in candles]
    macd_line, _, _ = _macd_series(closes)
    pivot_highs = [index for index, _ in _pivots(candles, "HIGH")]
    pivot_lows = [index for index, _ in _pivots(candles, "LOW")]
    pivot_highs.append(len(candles) - 1)
    pivot_lows.append(len(candles) - 1)
    output: dict[str, list[AttackWave]] = {"BULL": [], "BEAR": []}
    threshold = max(atr_value * 0.42, closes[-1] * 0.0012)

    for direction, ends in (("BULL", pivot_highs), ("BEAR", pivot_lows)):
        candidates: list[AttackWave] = []
        for end in sorted(set(ends))[-14:]:
            start_floor = max(0, end - 24)
            if end - start_floor < 2:
                continue
            window = range(start_floor, end)
            start = (
                min(window, key=lambda idx: candles[idx].low)
                if direction == "BULL"
                else max(window, key=lambda idx: candles[idx].high)
            )
            start_price = candles[start].low if direction == "BULL" else candles[start].high
            end_price = candles[end].high if direction == "BULL" else candles[end].low
            raw_move = end_price - start_price if direction == "BULL" else start_price - end_price
            if raw_move < threshold or end - start < 2:
                continue
            candidates.append(
                _make_attack_wave(
                    candles,
                    macd_line,
                    direction,
                    start,
                    end,
                    atr_value,
                    raw_move,
                )
            )
        dedup: dict[tuple[int, int], AttackWave] = {
            (wave.start_index, wave.end_index): wave for wave in candidates
        }
        output[direction] = sorted(dedup.values(), key=lambda wave: wave.end_index)[-3:]
    return output


def _make_attack_wave(
    candles: list[Candle],
    macd_line: list[float],
    direction: str,
    start: int,
    end: int,
    atr_value: float,
    raw_move: float,
) -> AttackWave:
    duration = max(1, end - start)
    move_atr = raw_move / max(atr_value, 1e-9)
    segment_macd = macd_line[start : end + 1]
    extreme = max(segment_macd) if direction == "BULL" else abs(min(segment_macd))
    time_efficiency = _clamp(move_atr / duration * 3.2, 0.0, 1.0)
    follow_end = min(len(candles) - 1, end + 3)
    follow_price = candles[follow_end].close
    end_close = candles[end].close
    follow_raw = follow_price - end_close if direction == "BULL" else end_close - follow_price
    follow_through = _clamp(0.5 + follow_raw / max(atr_value, 1e-9), 0.0, 1.0)
    if end < len(candles) - 1:
        tail = candles[end + 1 : min(len(candles), end + 5)]
        adverse = (
            end_close - min(item.low for item in tail)
            if direction == "BULL"
            else max(item.high for item in tail) - end_close
        )
    else:
        adverse = 0.0
    retracement = _clamp(adverse / max(raw_move, 1e-9), 0.0, 1.5)
    momentum_norm = _clamp(extreme / max(atr_value * 0.22, 1e-9), 0.0, 1.0)
    efficiency = _clamp(
        move_atr / 1.8 * 45.0
        + time_efficiency * 20.0
        + momentum_norm * 15.0
        + follow_through * 15.0
        - min(retracement, 1.0) * 15.0,
        0.0,
        100.0,
    )
    quality = "CLEAR" if move_atr >= 1.1 and duration <= 12 else "NORMAL" if move_atr >= 0.55 else "NOISY"
    return AttackWave(
        direction=direction,
        start_index=start,
        end_index=end,
        start_ts=candles[start].ts,
        end_ts=candles[end].ts,
        duration_bars=duration,
        price_move_pct=round(raw_move / max(abs(candles[start].close), 1e-9) * 100.0, 4),
        price_result_atr=round(move_atr, 3),
        macd_extreme=round(extreme, 10),
        time_efficiency=round(time_efficiency, 3),
        follow_through=round(follow_through, 3),
        retracement=round(retracement, 3),
        efficiency=round(efficiency, 1),
        quality=quality,
    )


def _compare_attacks(waves: list[AttackWave]) -> dict[str, Any]:
    if not waves:
        return {"state": "NO_CLEAR_ATTACK", "label": "沒有清楚攻擊波", "current": None, "previous": None, "change": None}
    current = waves[-1]
    if len(waves) < 2:
        return {
            "state": "SINGLE_ATTACK",
            "label": "只有一個有效攻擊波",
            "current": current.to_dict(),
            "previous": None,
            "change": None,
        }
    previous = waves[-2]
    change = current.efficiency - previous.efficiency
    price_ratio = current.price_result_atr / max(previous.price_result_atr, 1e-9)
    if change <= -12.0 or price_ratio <= 0.60:
        state, label = "DECLINING", "攻擊效率衰退"
    elif change >= 12.0 and price_ratio >= 1.05:
        state, label = "IMPROVING", "攻擊效率增加"
    else:
        state, label = "STABLE", "攻擊效率相近"
    return {
        "state": state,
        "label": label,
        "current": current.to_dict(),
        "previous": previous.to_dict(),
        "change": round(change, 1),
        "price_result_ratio": round(price_ratio, 3),
    }


def _compression_state(
    candles: list[Candle],
    tf: TimeframeFeatures,
    zones: dict[str, DynamicZone | None],
    efficiency: dict[str, Any],
) -> dict[str, Any]:
    recent = candles[-6:]
    higher_lows = sum(a.low <= b.low for a, b in zip(recent, recent[1:])) >= 4
    lower_highs = sum(a.high >= b.high for a, b in zip(recent, recent[1:])) >= 4
    resistance = zones.get("resistance")
    support = zones.get("support")
    near_resistance = bool(resistance and abs(tf.close - resistance.center) <= tf.atr14 * 0.72)
    near_support = bool(support and abs(tf.close - support.center) <= tf.atr14 * 0.72)
    if near_resistance and higher_lows and efficiency["BEAR"].get("state") in ("DECLINING", "NO_CLEAR_ATTACK", "SINGLE_ATTACK"):
        return {"state": "UPWARD", "label": "壓力壓縮｜向上突破風險", "blocks_direction": "SHORT"}
    if near_support and lower_highs and efficiency["BULL"].get("state") in ("DECLINING", "NO_CLEAR_ATTACK", "SINGLE_ATTACK"):
        return {"state": "DOWNWARD", "label": "支撐壓縮｜向下跌破風險", "blocks_direction": "LONG"}
    return {"state": "NONE", "label": "未見明顯壓縮", "blocks_direction": None}


def _trigger_candidate(
    direction: str,
    candles: list[Candle],
    tf: TimeframeFeatures,
    tf_bias: TimeframeFeatures,
    zones: dict[str, DynamicZone | None],
    location: dict[str, Any],
    efficiency: dict[str, Any],
    compression: dict[str, Any],
    regime: str,
    confirmation_window: int,
    early_signal_max_age_bars: int,
    max_early_entry_extension_atr: float,
) -> dict[str, Any]:
    is_long = direction == "LONG"
    side_zone = zones.get("support" if is_long else "resistance")
    breakout_zone = zones.get("resistance" if is_long else "support")
    micro_zone = zones.get("micro_resistance" if is_long else "micro_support")
    acceptance = _price_acceptance(candles, breakout_zone, direction, tf.atr14)
    momentum = _momentum_confirmation(candles, direction, confirmation_window)
    control = _control_transfer(
        candles,
        tf,
        direction,
        side_zone,
        micro_zone,
        acceptance,
        momentum,
    )
    opposing = efficiency["BEAR" if is_long else "BULL"]
    own = efficiency["BULL" if is_long else "BEAR"]
    at_reversal_zone = location["key"] in (
        "IN_SUPPORT" if is_long else "IN_RESISTANCE",
        "NEAR_SUPPORT" if is_long else "NEAR_RESISTANCE",
    )
    rejection = tf.lower_wick_ratio >= 0.32 if is_long else tf.upper_wick_ratio >= 0.32
    opponent_declining = opposing.get("state") == "DECLINING"
    pullback = _pullback_reactivation(candles, tf, direction, side_zone)
    bias_score = _direction_score(tf_bias)
    bias_aligned = bias_score >= 55.0 if is_long else bias_score <= 45.0
    compression_block = compression.get("blocks_direction") == direction

    reversal = (
        at_reversal_zone
        and rejection
        and opponent_declining
        and bool(control["transferred"])
        and bool(momentum["confirmed"])
        and not compression_block
    )
    full_breakout = (
        acceptance["state"] in ("ACCEPTED", "ROLE_REVERSAL_RETEST")
        and bool(control["transferred"])
        and bool(momentum["confirmed"])
    )
    early_breakout = (
        acceptance["state"] in ("BREAKING", "ACCEPTED")
        and float(acceptance.get("excursion_atr", 0.0)) >= 0.05
        and bool(control["push_away"])
        and bool(control["micro_defense_broken"])
        and bool(momentum["partial"])
    )
    breakout = full_breakout or early_breakout
    continuation = (
        bias_aligned
        and pullback["reactivated"]
        and bool(control["push_away"])
        and bool(momentum["partial"])
        and (bool(control["micro_defense_broken"]) or bool(momentum["confirmed"]))
        and not compression_block
    )
    triggered = bool(reversal or breakout or continuation)
    trigger_type = "REVERSAL" if reversal else "BREAKOUT" if breakout else "CONTINUATION" if continuation else "NONE"
    momentum_index = int(momentum.get("event_index", 0))
    acceptance_index = int(acceptance.get("event_index", 0))
    pullback_confirmation_index = (
        int(pullback.get("event_index", 0)) if continuation else 0
    )
    confirmation_index = max(
        momentum_index,
        acceptance_index if breakout else 0,
        pullback_confirmation_index,
    )
    if breakout:
        onset_indices = [
            index
            for index in (acceptance_index, momentum_index)
            if index > 0
        ]
        event_index = min(onset_indices, default=confirmation_index)
    elif continuation:
        event_index = int(pullback.get("touch_index", 0)) or confirmation_index
    else:
        event_index = confirmation_index
    event_index = min(max(event_index, 0), len(candles) - 1)
    confirmation_index = min(
        max(confirmation_index, event_index),
        len(candles) - 1,
    )
    event_age = len(candles) - 1 - event_index
    full = bool(momentum.get("full_confirmation")) and (
        acceptance["state"] == "ROLE_REVERSAL_RETEST"
        or control.get("follow_through")
        or (continuation and momentum.get("confirmed"))
    )
    if early_breakout and not full_breakout:
        full = False
    entry_reference = (
        acceptance.get("boundary")
        if breakout
        else pullback.get("reference_price")
        if continuation
        else side_zone.center
        if reversal and side_zone is not None
        else candles[event_index].close
    )
    try:
        entry_reference = float(entry_reference)
    except (TypeError, ValueError):
        entry_reference = float(candles[event_index].close)
    favorable_extension = (
        candles[-1].close - entry_reference
        if is_long
        else entry_reference - candles[-1].close
    )
    structural_extension_atr = max(0.0, favorable_extension) / max(
        tf.atr14,
        1e-9,
    )
    move_from_defense_atr = max(
        0.0,
        float(control.get("close_push_away_atr", 0.0) or 0.0),
    )
    # Chase distance must be measured from the entry reference that belongs to
    # this setup: breakout boundary, pullback reference, or reversal zone.  The
    # recent move from local defence is useful context, but for a breakout it
    # can include the entire approach into resistance.  Treating that approach
    # as distance travelled *after* the entry incorrectly marks a fresh break
    # as EXTENDED even while price is still inside its executable entry zone.
    entry_extension_atr = structural_extension_atr
    if triggered:
        if entry_extension_atr > max_early_entry_extension_atr:
            stage, freshness = "EXTENDED", "EXTENDED"
        elif event_age <= early_signal_max_age_bars:
            stage = (
                "EARLY_SIGNAL"
                if trigger_type == "CONTINUATION" or not full
                else "CONFIRMED"
            )
            freshness = "NEW"
        elif event_age <= 5:
            stage, freshness = (
                ("CONFIRMED", "ACTIVE")
                if full
                else ("NO_FOLLOW_THROUGH", "NO_FOLLOW_THROUGH")
            )
        elif event_age <= 8:
            stage, freshness = (
                ("TRENDING", "ACTIVE")
                if full
                else ("NO_FOLLOW_THROUGH", "NO_FOLLOW_THROUGH")
            )
        else:
            stage, freshness = "EXTENDED", "EXTENDED"
    else:
        near_facts = sum(
            (
                at_reversal_zone or acceptance["state"] in ("BREAKING", "ACCEPTED"),
                bool(momentum["partial"]),
                bool(control["push_away"]),
                opponent_declining or bias_aligned,
            )
        )
        stage, freshness = ("NEAR_TRIGGER", "NONE") if near_facts >= 3 and not compression_block else ("WATCH", "NONE")

    event_zone = breakout_zone if breakout else side_zone
    invalidation = (
        event_zone.lower if is_long and event_zone
        else event_zone.upper if (not is_long and event_zone)
        else tf.recent_low - tf.atr14 * 0.20 if is_long
        else tf.recent_high + tf.atr14 * 0.20
    )
    supporting = []
    conflicts = []
    neutral = []
    if at_reversal_zone:
        supporting.append("位於合理支撐／壓力 Zone")
    elif acceptance["state"] in ("ACCEPTED", "ROLE_REVERSAL_RETEST"):
        supporting.append(acceptance["label"])
    else:
        neutral.append("尚未位於最佳價格位置")
    if opponent_declining:
        supporting.append("原攻擊方效率衰退")
    else:
        neutral.append("原攻擊方效率尚未明顯衰退")
    if control["transferred"]:
        supporting.append(control["label"])
    elif control["push_away"]:
        neutral.append("已出現推離，但控制權尚在轉移")
    else:
        neutral.append("對手尚未展現真實推離能力")
    if momentum["confirmed"]:
        supporting.append(momentum["label"])
    elif momentum["partial"]:
        neutral.append(momentum["label"])
    else:
        conflicts.append("MA／MACD 尚未在合理窗口呼應")
    if compression_block:
        conflicts.append(compression["label"])
    if own.get("state") == "DECLINING" and triggered:
        conflicts.append("觸發方向自身攻擊效率仍偏弱")
    noise = _noise_state(candles, tf)
    if noise["high"] and not acceptance["state"] in ("ACCEPTED", "ROLE_REVERSAL_RETEST"):
        conflicts.append("15m 雜訊高；需依價格事實而非小型交叉")
        if not control["transferred"]:
            triggered = False
            stage = "WATCH"
            freshness = "NONE"

    explainability_score = round(
        _clamp(
            (85.0 if at_reversal_zone or acceptance["state"] in ("ACCEPTED", "ROLE_REVERSAL_RETEST") else 45.0) * 0.30
            + float(control["score"]) * 0.35
            + float(momentum["score"]) * 0.25
            + (75.0 if opponent_declining or bias_aligned else 45.0) * 0.10,
            0.0,
            100.0,
        ),
        1,
    )
    return {
        "direction": direction,
        "triggered": triggered,
        "type": trigger_type,
        "stage": stage,
        "freshness": freshness,
        "event_ts": candles[event_index].ts,
        "event_price": candles[event_index].close,
        "event_atr": tf.atr14,
        "event_index": event_index,
        "event_age_bars": event_age,
        "confirmation_ts": candles[confirmation_index].ts,
        "confirmation_index": confirmation_index,
        "entry_reference_price": entry_reference,
        "entry_extension_atr": round(entry_extension_atr, 3),
        "structural_entry_extension_atr": round(
            structural_extension_atr,
            3,
        ),
        "move_from_defense_atr": round(move_from_defense_atr, 3),
        "move_from_defense_warning": bool(
            move_from_defense_atr > max_early_entry_extension_atr
        ),
        "confirmation_level": "FULL" if full else "EARLY",
        "zone_key": f"{event_zone.tier}:{round(event_zone.center, 10)}" if event_zone else "NO_ZONE",
        "invalidation_price": invalidation,
        "position_valid": at_reversal_zone or acceptance["state"] in ("ACCEPTED", "ROLE_REVERSAL_RETEST") or pullback["reactivated"],
        "momentum_confirmation": momentum,
        "price_acceptance": acceptance,
        "control_transfer": control,
        "pullback": pullback,
        "compression_block": compression_block,
        "supporting": _unique(supporting),
        "conflicts": _unique(conflicts),
        "neutral": _unique(neutral),
        "noise": noise,
        "explainability_score": explainability_score,
        "permission_note": "Trigger 只由核心價格事實決定；Context 與 Execution Quality 無權取消。",
    }


def _select_candidate(candidates: dict[str, dict[str, Any]], direction: str) -> dict[str, Any]:
    triggered = [item for item in candidates.values() if item.get("triggered")]
    if triggered:
        return max(
            triggered,
            key=lambda item: (
                -int(item.get("event_age_bars", 999)),
                float(item.get("explainability_score", 0.0)),
            ),
        )
    near = [item for item in candidates.values() if item.get("stage") == "NEAR_TRIGGER"]
    if near:
        return max(near, key=lambda item: float(item.get("explainability_score", 0.0)))
    if direction in candidates:
        return candidates[direction]
    return max(candidates.values(), key=lambda item: float(item.get("explainability_score", 0.0)))


def _price_acceptance(
    candles: list[Candle],
    zone: DynamicZone | None,
    direction: str,
    atr_value: float,
) -> dict[str, Any]:
    if zone is None:
        return {"state": "NO_ZONE", "label": "沒有可判讀 Zone", "event_index": 0}
    is_long = direction == "LONG"
    boundary = zone.upper if is_long else zone.lower
    latest = candles[-1]
    previous = candles[-2]
    outside = latest.close > boundary if is_long else latest.close < boundary
    previous_outside = previous.close > boundary if is_long else previous.close < boundary
    excursion = (latest.close - boundary if is_long else boundary - latest.close) / max(atr_value, 1e-9)
    swept = latest.high > zone.upper and latest.close <= zone.upper if is_long else latest.low < zone.lower and latest.close >= zone.lower
    event_index = 0
    for index in range(max(1, len(candles) - 12), len(candles)):
        current_outside = candles[index].close > boundary if is_long else candles[index].close < boundary
        prior_outside = candles[index - 1].close > boundary if is_long else candles[index - 1].close < boundary
        if current_outside and not prior_outside:
            event_index = index
    retested = False
    if outside and event_index:
        # A breakout candle cannot also prove a later role-reversal retest; OHLC
        # has no intrabar ordering. Require at least one subsequent closed bar.
        for candle in candles[event_index + 1 :]:
            touched = candle.low <= zone.upper if is_long else candle.high >= zone.lower
            held = candle.close > zone.center if is_long else candle.close < zone.center
            if touched and held:
                retested = True
                break
    if outside and retested:
        state, label = "ROLE_REVERSAL_RETEST", "突破後角色轉換回踩守住"
    elif outside and (previous_outside or excursion >= 0.12):
        state, label = "ACCEPTED", "Zone 外新價格獲得接受"
    elif outside:
        state, label = "BREAKING", "正在突破，等待價格接受"
    elif swept:
        state, label = "REJECTED", "影線掃過後收回，未接受 Zone 外價格"
    else:
        state, label = "INSIDE", "仍在 Zone 內／尚未突破"
    return {
        "state": state,
        "label": label,
        "boundary": boundary,
        "event_index": event_index,
        "excursion_atr": round(excursion, 3),
        "retested": retested,
        "sweep": swept,
    }


def _momentum_confirmation(
    candles: list[Candle],
    direction: str,
    window: int,
) -> dict[str, Any]:
    closes = [item.close for item in candles]
    ma5 = _sma_series(closes, 5)
    ma10 = _sma_series(closes, 10)
    ma20 = _sma_series(closes, 20)
    macd_line, signal_line, hist = _macd_series(closes)
    sign = 1.0 if direction == "LONG" else -1.0
    start = max(20, len(candles) - window - 1)
    ma_event = None
    macd_event = None
    ma_cross_seen = False
    macd_cross_seen = False
    first_partial_index = None
    first_confirmed_index = None
    for index in range(start + 1, len(candles)):
        if (ma5[index] - ma10[index]) * sign > 0 and (ma5[index - 1] - ma10[index - 1]) * sign <= 0:
            ma_event = index
            ma_cross_seen = True
        if (macd_line[index] - signal_line[index]) * sign > 0 and (macd_line[index - 1] - signal_line[index - 1]) * sign <= 0:
            macd_event = index
            macd_cross_seen = True
        ma_aligned_at_index = (ma5[index] - ma10[index]) * sign > 0
        ma_turning_at_index = (
            (ma5[index] - ma5[index - 1]) * sign > 0
            and (ma10[index] - ma10[index - 1]) * sign >= 0
        )
        macd_aligned_at_index = (macd_line[index] - signal_line[index]) * sign > 0
        macd_turning_at_index = (
            (hist[index] - hist[index - 1]) * sign > 0
            and (macd_line[index] - macd_line[index - 1]) * sign > 0
        )
        ma_response_at_index = ma_aligned_at_index and (
            ma_cross_seen or ma_turning_at_index
        )
        macd_response_at_index = macd_aligned_at_index and (
            macd_cross_seen or macd_turning_at_index
        )
        if first_partial_index is None and (
            ma_response_at_index or macd_response_at_index
        ):
            first_partial_index = index
        if first_confirmed_index is None and (
            ma_response_at_index and macd_response_at_index
        ):
            first_confirmed_index = index
    ma_aligned = (ma5[-1] - ma10[-1]) * sign > 0
    ma_turning = (ma5[-1] - ma5[-2]) * sign > 0 and (ma10[-1] - ma10[-2]) * sign >= 0
    macd_aligned = (macd_line[-1] - signal_line[-1]) * sign > 0
    macd_turning = (hist[-1] - hist[-2]) * sign > 0 and (macd_line[-1] - macd_line[-2]) * sign > 0
    ma_response = ma_aligned and (ma_event is not None or ma_turning)
    macd_response = macd_aligned and (macd_event is not None or macd_turning)
    confirmed = ma_response and macd_response
    partial = ma_response or macd_response
    full = (
        (ma5[-1] - ma10[-1]) * sign > 0
        and (ma10[-1] - ma20[-1]) * sign > 0
        and (macd_line[-1] - signal_line[-1]) * sign > 0
        and hist[-1] * sign > 0
    )
    event_index = (
        first_confirmed_index
        if confirmed and first_confirmed_index is not None
        else first_partial_index
        if partial and first_partial_index is not None
        else 0
    )
    score = 85.0 if full else 72.0 if confirmed else 55.0 if partial else 25.0
    label = (
        "MA5/10 與 MACD 已在合理窗口同向呼應"
        if confirmed
        else "MA 或 MACD 已先轉向，等待另一者呼應"
        if partial
        else "MA／MACD 尚未形成同向反應"
    )
    return {
        "confirmed": confirmed,
        "partial": partial,
        "full_confirmation": full,
        "ma_response": ma_response,
        "macd_response": macd_response,
        "ma_cross_index": ma_event,
        "macd_cross_index": macd_event,
        "event_index": event_index,
        "window_bars": window,
        "score": score,
        "label": label,
    }


def _control_transfer(
    candles: list[Candle],
    tf: TimeframeFeatures,
    direction: str,
    defense_zone: DynamicZone | None,
    micro_zone: DynamicZone | None,
    acceptance: dict[str, Any],
    momentum: dict[str, Any],
) -> dict[str, Any]:
    is_long = direction == "LONG"
    sign = 1.0 if is_long else -1.0
    recent = candles[-4:]
    base = min(item.low for item in recent) if is_long else max(item.high for item in recent)
    close_base = (
        min(item.close for item in recent)
        if is_long
        else max(item.close for item in recent)
    )
    push_raw = candles[-1].close - base if is_long else base - candles[-1].close
    close_push_raw = (
        candles[-1].close - close_base
        if is_long
        else close_base - candles[-1].close
    )
    push_away = push_raw >= tf.atr14 * 0.18 or acceptance.get("state") in ("ACCEPTED", "ROLE_REVERSAL_RETEST")
    if micro_zone is not None:
        micro_break = candles[-1].close > micro_zone.center if is_long else candles[-1].close < micro_zone.center
    else:
        prior = candles[-7:-2]
        micro_level = max(item.high for item in prior) if is_long else min(item.low for item in prior)
        micro_break = candles[-1].close > micro_level if is_long else candles[-1].close < micro_level
    if defense_zone is not None:
        reclaimed_by_opponent = (
            candles[-1].close < defense_zone.center if is_long else candles[-1].close > defense_zone.center
        )
    else:
        reclaimed_by_opponent = False
    follow_through = (
        (candles[-1].close - candles[-3].close) * sign >= tf.atr14 * 0.20
        and not reclaimed_by_opponent
    )
    transferred = push_away and (micro_break or acceptance.get("state") in ("ACCEPTED", "ROLE_REVERSAL_RETEST")) and bool(momentum.get("confirmed"))
    score = (
        (30.0 if push_away else 0.0)
        + (25.0 if micro_break else 0.0)
        + (20.0 if not reclaimed_by_opponent else 0.0)
        + (15.0 if momentum.get("confirmed") else 0.0)
        + (10.0 if follow_through else 0.0)
    )
    label = (
        f"{'買方' if is_long else '賣方'}開始取得控制"
        if transferred
        else "控制權轉移中"
        if push_away
        else "尚未出現真實控制權轉移"
    )
    return {
        "state": "LONG_CONTROL" if transferred and is_long else "SHORT_CONTROL" if transferred else "TRANSFERRING" if push_away else "UNRESOLVED",
        "label": label,
        "transferred": transferred,
        "push_away": push_away,
        "push_away_atr": round(push_raw / max(tf.atr14, 1e-9), 3),
        "close_push_away_atr": round(
            close_push_raw / max(tf.atr14, 1e-9),
            3,
        ),
        "micro_defense_broken": micro_break,
        "opponent_reclaimed": reclaimed_by_opponent,
        "follow_through": follow_through,
        "score": score,
    }


def _pullback_reactivation(
    candles: list[Candle],
    tf: TimeframeFeatures,
    direction: str,
    zone: DynamicZone | None,
) -> dict[str, Any]:
    is_long = direction == "LONG"
    touched_zone = False
    saw_counter_move = False
    touch_index = 0
    reference_price = None
    for index in range(max(1, len(candles) - 6), len(candles) - 1):
        candle = candles[index]
        zone_touch = bool(
            zone
            and candle.low <= zone.upper
            and candle.high >= zone.lower
        )
        ma_touch = candle.low <= tf.sma10 <= candle.high
        counter_move = (
            candle.close < candle.open or candle.close < candles[index - 1].close
            if is_long
            else candle.close > candle.open or candle.close > candles[index - 1].close
        )
        if (zone_touch or ma_touch) and counter_move:
            touched_zone = True
            saw_counter_move = True
            touch_index = index
            reference_price = zone.center if zone_touch and zone else candle.close
    resumed = (
        candles[-1].close > candles[-1].open and candles[-1].close > candles[-2].close
        if is_long
        else candles[-1].close < candles[-1].open and candles[-1].close < candles[-2].close
    )
    return {
        "touched_zone_or_ma": touched_zone,
        "counter_move_seen": saw_counter_move,
        "resumed": resumed,
        "reactivated": touched_zone and resumed,
        "touch_index": touch_index if touched_zone else 0,
        "touch_ts": candles[touch_index].ts if touched_zone else None,
        "reference_price": reference_price,
        "event_index": len(candles) - 1 if touched_zone and resumed else 0,
    }


def _noise_state(candles: list[Candle], tf: TimeframeFeatures) -> dict[str, Any]:
    closes = [item.close for item in candles]
    _, _, hist = _macd_series(closes)
    crosses = sum(a * b < 0 for a, b in zip(hist[-9:-1], hist[-8:]))
    entangled = max(abs(tf.sma5 - tf.sma10), abs(tf.sma10 - tf.sma20)) <= tf.atr14 * 0.10
    displacement = (max(closes[-8:]) - min(closes[-8:])) / max(tf.atr14, 1e-9)
    high = crosses >= 3 and entangled and displacement < 1.05
    return {
        "high": high,
        "macd_zero_crosses": crosses,
        "ma_entangled": entangled,
        "price_displacement_atr": round(displacement, 3),
        "label": "震盪｜雜訊高" if high else "雜訊可控",
    }


def _evidence_groups(
    direction: str,
    candidate: dict[str, Any],
    direction_score: float,
    location: dict[str, Any],
    supporting: list[str],
    conflicts: list[str],
    neutral: list[str],
) -> dict[str, dict[str, Any]]:
    position_score = 80.0 if candidate.get("position_valid") else 50.0 if location["key"] != "RANGE_MIDDLE" else 25.0
    trend_direction_score = direction_score if direction == "LONG" else 100.0 - direction_score
    trend_score = _clamp(
        trend_direction_score * 0.35
        + float(candidate.get("momentum_confirmation", {}).get("score", 50.0)) * 0.35
        + float(candidate.get("control_transfer", {}).get("score", 50.0)) * 0.30,
        0.0,
        100.0,
    )
    return {
        "position_structure": _group(
            "position_structure",
            "位置／價格行為",
            position_score,
            [item for item in supporting if "Zone" in item or "突破" in item or "價格" in item],
            [item for item in conflicts if "壓縮" in item or "位置" in item],
            [item for item in neutral if "位置" in item],
            100.0,
        ),
        "trend_momentum": _group(
            "trend_momentum",
            "趨勢／動能",
            trend_score,
            [item for item in supporting if "MA" in item or "控制" in item or "效率" in item],
            [item for item in conflicts if "MA" in item or "效率" in item],
            [item for item in neutral if "攻擊" in item or "控制" in item or "MA" in item],
            100.0,
        ),
        "participation_flow": _group(
            "participation_flow",
            "市場參與",
            50.0,
            [],
            [],
            ["深度資料待取得；中性不取消 Trigger"],
            0.0,
        ),
    }


def _group(
    key: str,
    label: str,
    score: float,
    supporting: list[str],
    conflicts: list[str],
    neutral: list[str],
    confidence: float,
) -> dict[str, Any]:
    stance = "SUPPORT" if score >= 60.0 else "CONFLICT" if score < 40.0 else "NEUTRAL"
    return {
        "key": key,
        "label": label,
        "score": round(_clamp(score, 0.0, 100.0), 1),
        "stance": stance,
        "confidence": round(confidence, 1),
        "supporting": _unique(supporting),
        "conflicts": _unique(conflicts),
        "neutral": _unique(neutral),
    }


def _timeframe_states(
    horizon: str,
    frame_names: tuple[str, str, str, str],
    higher_long: float,
    bias_long: float,
    selected: dict[str, Any],
    timing: TimeframeFeatures | None,
    direction: str,
) -> dict[str, dict[str, Any]]:
    higher_direction, _, higher_label = _direction_state(higher_long)
    bias_direction, _, bias_label = _direction_state(bias_long)
    states = {
        frame_names[0]: {
            "role": "大環境 Context" if horizon == "SHORT" else "大方向 Bias",
            "direction": higher_direction,
            "label": higher_label,
            "score": round(higher_long, 1),
            "can_block_trigger": False,
        }
    }
    if frame_names[1] != frame_names[0]:
        states[frame_names[1]] = {
            "role": "Bias／Setup",
            "direction": bias_direction,
            "label": bias_label,
            "score": round(bias_long, 1),
            "can_block_trigger": False,
        }
    states[frame_names[2]] = {
        "role": "核心 Trigger",
        "direction": direction,
        "label": _stage_label(str(selected.get("stage", "WATCH")), direction),
        "score": float(selected.get("explainability_score", 0.0)),
        "can_block_trigger": True,
    }
    if timing is not None:
        timing_long = _direction_score(timing)
        timing_direction, _, timing_label = _direction_state(timing_long)
        label = f"{_direction_cn(timing_direction)}加速" if timing_direction in ("LONG", "SHORT") else timing_label
    else:
        timing_long = 50.0
        timing_direction = "NEUTRAL"
        label = "資料暫缺"
    states[frame_names[3]] = {
        "role": "Timing／預警／加速",
        "direction": timing_direction,
        "label": label,
        "score": round(timing_long, 1),
        "can_block_trigger": False,
    }
    return states


def _human_summary(
    direction: str,
    stage: str,
    location: dict[str, Any],
    efficiency: dict[str, Any],
    acceptance: dict[str, Any],
    control: dict[str, Any],
    compression: dict[str, Any],
    triggered: bool,
) -> str:
    if compression.get("state") != "NONE" and not triggered:
        return str(compression["label"])
    if direction not in ("LONG", "SHORT"):
        return f"{location['label']}；目前沒有清楚的控制權轉移。"
    opposing = efficiency["BEAR" if direction == "LONG" else "BULL"]
    if triggered:
        return (
            f"{location['label']}｜{opposing.get('label', '攻擊效率待比較')}｜"
            f"{acceptance.get('label', '')}｜{control.get('label', '')}｜"
            f"{_stage_label(stage, direction)}"
        )
    return (
        f"{location['label']}｜{control.get('label', '控制權未明')}｜"
        f"{_stage_label(stage, direction)}"
    )


def _entry_location_quality(extension_atr: float, stage: str) -> dict[str, Any]:
    if stage == "EXTENDED" or extension_atr > 1.80:
        key, label, score = "SEVERE_CHASE", "已延伸／不宜追價", 15.0
    elif extension_atr > 1.40:
        key, label, score = "HIGHLY_EXTENDED", "高度延伸", 30.0
    elif extension_atr > 0.80:
        key, label, score = "EXTENDED", "有些延伸", 55.0
    elif extension_atr > 0.35:
        key, label, score = "ACCEPTABLE", "可以接受", 78.0
    else:
        key, label, score = "EXCELLENT", "位置很好", 95.0
    return {"key": key, "label": label, "score": score, "extension_atr": round(extension_atr, 2)}


def _context_conflicts(
    trigger_direction: str,
    higher_long: float,
    bias_long: float,
    horizon: str,
) -> list[str]:
    if trigger_direction not in ("LONG", "SHORT"):
        return []
    direction_score = bias_long if trigger_direction == "LONG" else 100.0 - bias_long
    higher_score = higher_long if trigger_direction == "LONG" else 100.0 - higher_long
    output = []
    if direction_score < 35.0:
        output.append(f"{'1H' if horizon == 'SHORT' else '1D'} 背景反向，屬逆勢 Trigger")
    if horizon == "SHORT" and higher_score < 30.0:
        output.append("更高週期背景明顯反向；只列 Conflict，不取消核心 Trigger")
    return output


def _regime(tf: TimeframeFeatures) -> str:
    if tf.compression_ratio <= 0.82:
        return "COMPRESSION"
    if tf.adx14 >= 28.0 and abs(tf.ema21_slope_atr) >= 0.20:
        return "TREND"
    if tf.adx14 <= 18.0 and tf.compression_ratio <= 1.05:
        return "RANGE"
    return "TRANSITION"


def _confirmation_window(regime: str, tf: TimeframeFeatures) -> int:
    if regime == "TREND":
        return 3
    if regime == "COMPRESSION":
        return 5
    if tf.atr_pct <= 0.35:
        return 5
    return 4


def _direction_score(tf: TimeframeFeatures) -> float:
    atr_value = max(tf.atr14, abs(tf.close) * 0.0001, 1e-9)
    ma = math.tanh((tf.sma5 - tf.sma10) / (atr_value * 0.45)) * 0.22
    medium = math.tanh((tf.sma10 - tf.sma20) / (atr_value * 0.65)) * 0.18
    ema = math.tanh((tf.ema21 - tf.ema55) / (atr_value * 1.10)) * 0.20
    slope = math.tanh(tf.ema21_slope_atr / 0.35) * 0.15
    macd = math.tanh((tf.macd_line - tf.macd_signal) / (atr_value * 0.18)) * 0.15
    price = math.tanh((tf.close - tf.ema21) / (atr_value * 0.80)) * 0.10
    return round(_clamp(50.0 + (ma + medium + ema + slope + macd + price) * 50.0, 0.0, 100.0), 1)


def _direction_state(long_score: float) -> tuple[str, str, str]:
    if long_score >= 68.0:
        return "LONG", "STRONG_LONG", "強勢多"
    if long_score >= 56.0:
        return "LONG", "LONG", "偏多"
    if long_score <= 32.0:
        return "SHORT", "STRONG_SHORT", "強勢空"
    if long_score <= 44.0:
        return "SHORT", "SHORT", "偏空"
    return "NEUTRAL", "NEUTRAL", "中性"


def _macd_series(values: list[float]) -> tuple[list[float], list[float], list[float]]:
    fast = ema_series(values, 12)
    slow = ema_series(values, 26)
    line = [a - b for a, b in zip(fast, slow)]
    signal = ema_series(line, 9)
    hist = [a - b for a, b in zip(line, signal)]
    return line, signal, hist


def _sma_series(values: list[float], period: int) -> list[float]:
    output: list[float] = []
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        output.append(running / min(index + 1, period))
    return output


def _stage_label(stage: str, direction: str) -> str:
    name = _direction_cn(direction)
    return {
        "WATCH": "觀望",
        "NEAR_TRIGGER": "接近觸發",
        "EARLY_SIGNAL": f"{name}｜早期訊號",
        "CONFIRMED": f"{name}｜完整確認",
        "TRENDING": "趨勢進行中",
        "REENTRY": "回踩再發動",
        "EXTENDED": "已延伸",
        "NO_FOLLOW_THROUGH": "未獲延續",
        "INVALIDATED": "訊號失效",
    }.get(stage, stage)


def _direction_cn(direction: str) -> str:
    return "做多" if direction == "LONG" else "做空" if direction == "SHORT" else "中性"


def _pct_change(current: float, previous: float) -> float:
    return round((current - previous) / previous * 100.0, 5) if previous else 0.0


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
