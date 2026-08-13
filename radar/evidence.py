from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any

from .indicators import TimeframeFeatures
from .models import MarketContext


GROUP_LABELS = {
    "position_structure": "位置／結構",
    "trend_momentum": "趨勢／動能",
    "participation_flow": "市場參與",
}

REGIME_WEIGHTS = {
    "TREND": {
        "position_structure": 0.35,
        "trend_momentum": 0.40,
        "participation_flow": 0.25,
    },
    "BREAKOUT_READY": {
        "position_structure": 0.35,
        "trend_momentum": 0.30,
        "participation_flow": 0.35,
    },
    "RANGE": {
        "position_structure": 0.45,
        "trend_momentum": 0.30,
        "participation_flow": 0.25,
    },
    "DISORDER": {
        "position_structure": 0.34,
        "trend_momentum": 0.33,
        "participation_flow": 0.33,
    },
}


@dataclass(frozen=True)
class EvidenceGroup:
    key: str
    score: float
    stance: str
    confidence: float
    supporting: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    neutral: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": GROUP_LABELS[self.key],
            "score": round(self.score, 1),
            "stance": self.stance,
            "confidence": round(self.confidence, 1),
            "supporting": list(self.supporting),
            "conflicts": list(self.conflicts),
            "neutral": list(self.neutral),
        }


@dataclass(frozen=True)
class EvidenceAssessment:
    direction: str
    regime: str
    groups: dict[str, EvidenceGroup]
    timeframe_states: dict[str, dict[str, Any]]
    bias_quality: float
    setup_maturity: float
    trigger_maturity: float
    micro_acceleration: float
    alignment_score: float
    entry_quality: dict[str, Any]
    readiness: float
    conflict_severity: float
    stage: str
    supporting: list[str]
    conflicts: list[str]
    neutral: list[str]
    summary: str
    context_complete: bool = False
    context_available_count: int = 0
    context_required_count: int = 3

    def group_dicts(self) -> dict[str, dict[str, Any]]:
        return {key: group.to_dict() for key, group in self.groups.items()}

    def with_live_context(
        self,
        context: MarketContext,
        tf5: TimeframeFeatures | None,
        market_bias: dict[str, object] | None = None,
    ) -> "EvidenceAssessment":
        direction_sign = 1.0 if self.direction == "LONG" else -1.0
        base = self.groups["participation_flow"]
        parts: list[tuple[float, float]] = [(base.score, 0.45)]
        supporting = list(base.supporting)
        conflicts = list(base.conflicts)
        neutral = list(base.neutral)
        available = 0
        strong_taker_opposition = False
        strong_book_opposition = False

        if context.taker_buy_ratio is not None:
            available += 1
            score = (
                context.taker_buy_ratio * 100.0
                if self.direction == "LONG"
                else (1.0 - context.taker_buy_ratio) * 100.0
            )
            parts.append((score, 0.24))
            if score >= 60.0:
                supporting.append(f"主動成交支持{_direction_cn(self.direction)}")
            elif score < 35.0:
                strong_taker_opposition = True
                conflicts.append("主動成交明顯反向")
            else:
                neutral.append("主動成交中性")

        if context.order_book_imbalance is not None:
            available += 1
            signed = context.order_book_imbalance * direction_sign
            score = _clamp(50.0 + signed * 100.0, 0.0, 100.0)
            parts.append((score, 0.16))
            if signed >= 0.10:
                supporting.append("委託簿深度支持方向")
            elif signed <= -0.20:
                strong_book_opposition = True
                conflicts.append("委託簿出現明顯反向失衡")
            else:
                neutral.append("委託簿中性")

        if context.open_interest_change_pct is not None:
            oi_change = context.open_interest_change_pct
            oi_score = _clamp(50.0 + oi_change * 12.0, 25.0, 85.0)
            parts.append((oi_score, 0.10))
            if oi_change >= 0.5:
                supporting.append("持倉量增加，顯示有新增部位參與")
            elif oi_change <= -0.8:
                conflicts.append("持倉量下降，行情可能主要由平倉推動")
            else:
                neutral.append("持倉量變化中性")

        if context.funding_rate is not None:
            available += 1
            directional_funding = context.funding_rate * direction_sign
            if directional_funding > 0.0008:
                funding_score = 20.0
                conflicts.append("資金費率顯示同方向部位極端擁擠")
            elif directional_funding > 0.0005:
                funding_score = 35.0
                conflicts.append("資金費率顯示同方向部位偏擁擠")
            else:
                funding_score = 55.0
                neutral.append("資金費率未見極端擁擠")
            parts.append((funding_score, 0.05))

        micro_score = 50.0
        timeframe_states = dict(self.timeframe_states)
        if tf5 is not None:
            pressure = (
                tf5.directional_volume_ratio
                if self.direction == "LONG"
                else 1.0 - tf5.directional_volume_ratio
            )
            direction_score = _relative_timeframe_score(tf5, self.direction)
            volume_score = _volume_score(tf5.volume_ratio)
            pressure_score = _clamp(50.0 + (pressure - 0.50) * 250.0, 0.0, 100.0)
            micro_score = round(
                direction_score * 0.45 + volume_score * 0.30 + pressure_score * 0.25,
                1,
            )
            parts.append((micro_score, 0.10))
            if micro_score >= 62.0:
                supporting.append(f"5m {_direction_cn(self.direction)}加速")
            elif micro_score < 35.0:
                conflicts.append("5m 出現反向加速")
            else:
                neutral.append("5m 動能中性")
            timeframe_states["5m"] = {
                "role": "加速／提前預警",
                "score": micro_score,
                "direction": self.direction if micro_score >= 60.0 else "NEUTRAL",
                "label": (
                    f"{_direction_cn(self.direction)}方加速"
                    if micro_score >= 62.0
                    else "反向干擾"
                    if micro_score < 35.0
                    else "中性"
                ),
            }
        else:
            timeframe_states["5m"] = {
                "role": "加速／提前預警",
                "score": 50.0,
                "direction": "NEUTRAL",
                "label": "尚未取得",
            }

        denominator = sum(weight for _, weight in parts)
        participation_score = sum(score * weight for score, weight in parts) / denominator
        if strong_taker_opposition and strong_book_opposition:
            participation_score = min(participation_score, 5.0)
            conflicts.append("主動成交與委託簿同時強烈反向")
        participation = _make_group(
            "participation_flow",
            participation_score,
            supporting,
            conflicts,
            neutral,
            confidence=_clamp(45.0 + available / 3.0 * 55.0, 0.0, 100.0),
        )
        groups = dict(self.groups)
        groups["participation_flow"] = participation

        macro_conflict = False
        market_bias = market_bias or {"score": 50.0, "label": "中性"}
        macro_score = float(market_bias.get("score", 50.0) or 50.0)
        directional_macro = macro_score if self.direction == "LONG" else 100.0 - macro_score
        merged_conflicts = _unique(
            [item for group in groups.values() for item in group.conflicts]
        )
        merged_supporting = _unique(
            [item for group in groups.values() for item in group.supporting]
        )
        merged_neutral = _unique(
            [item for group in groups.values() for item in group.neutral]
        )
        if directional_macro < 25.0:
            macro_conflict = True
            merged_conflicts.append("全市場方向與候選方向強烈相反")
        elif directional_macro < 40.0:
            merged_neutral.append("全市場方向未支持此候選")

        alignment = _alignment_score(groups, self.regime)
        conflict = _conflict_severity(groups, macro_conflict)
        readiness = _readiness(
            self.bias_quality,
            self.setup_maturity,
            self.trigger_maturity,
            micro_score,
            alignment,
            float(self.entry_quality["score"]),
            conflict,
        )
        stage = _classify_stage(
            self.regime,
            groups,
            self.setup_maturity,
            self.trigger_maturity,
            readiness,
            conflict,
            str(self.entry_quality["key"]),
        )
        summary = _summary(
            self.direction,
            timeframe_states,
            participation.score,
            stage,
        )
        return replace(
            self,
            groups=groups,
            timeframe_states=timeframe_states,
            micro_acceleration=micro_score,
            alignment_score=alignment,
            readiness=readiness,
            conflict_severity=conflict,
            stage=stage,
            supporting=_unique(merged_supporting),
            conflicts=_unique(merged_conflicts),
            neutral=_unique(merged_neutral),
            summary=summary,
            context_complete=context.complete,
            context_available_count=available,
        )


def infer_regime_direction(
    tf4: TimeframeFeatures,
    tf1: TimeframeFeatures,
    tf15: TimeframeFeatures,
) -> tuple[str, str]:
    score4 = _long_timeframe_score(tf4)
    score1 = _long_timeframe_score(tf1)
    score15 = _long_timeframe_score(tf15)
    combined = score4 * 0.25 + score1 * 0.40 + score15 * 0.35
    width = tf1.prior_high20 - tf1.prior_low20
    range_position = (tf1.close - tf1.prior_low20) / width if width > 0 else 0.5
    broke_high = tf15.close > tf15.prior_high20 or tf1.close > tf1.prior_high20
    broke_low = tf15.close < tf15.prior_low20 or tf1.close < tf1.prior_low20
    near_high = (tf1.prior_high20 - tf1.close) / tf1.atr14 <= 0.55
    near_low = (tf1.close - tf1.prior_low20) / tf1.atr14 <= 0.55
    compression = tf1.compression_ratio <= 0.92 and (near_high or near_low)

    if broke_high or broke_low or compression:
        regime = "BREAKOUT_READY"
        if broke_high:
            direction = "LONG"
        elif broke_low:
            direction = "SHORT"
        elif near_high and not near_low:
            direction = "LONG"
        elif near_low and not near_high:
            direction = "SHORT"
        else:
            direction = "LONG" if combined >= 50.0 else "SHORT"
    elif tf1.adx14 <= 22.0 and width > 0:
        regime = "RANGE"
        direction = (
            "LONG"
            if range_position <= 0.42
            else "SHORT"
            if range_position >= 0.58
            else "NEUTRAL"
        )
    elif combined >= 57.0 or combined <= 43.0:
        regime = "TREND"
        direction = "LONG" if combined >= 57.0 else "SHORT"
    else:
        regime = "DISORDER"
        direction = "NEUTRAL"
    return regime, direction


def assess_evidence(
    tf4: TimeframeFeatures,
    tf1: TimeframeFeatures,
    tf15: TimeframeFeatures,
    regime: str,
    direction: str,
    tf5: TimeframeFeatures | None = None,
    acceptable_entry_extension_atr: float = 0.80,
    severe_entry_extension_atr: float = 1.80,
) -> EvidenceAssessment:
    if direction not in ("LONG", "SHORT"):
        groups = {
            key: _make_group(key, 50.0, [], [], ["方向尚未形成"], 100.0)
            for key in GROUP_LABELS
        }
        timeframe_states = {
            "4H": _timeframe_state("大方向／Bias", 50.0, direction, "中性"),
            "1H": _timeframe_state("Setup／準備層", 50.0, direction, "準備不足"),
            "15m": _timeframe_state("Main Trigger／主要觸發", 50.0, direction, "尚未觸發"),
            "5m": _timeframe_state("加速／提前預警", 50.0, direction, "尚未取得"),
        }
        return EvidenceAssessment(
            direction="NEUTRAL",
            regime=regime,
            groups=groups,
            timeframe_states=timeframe_states,
            bias_quality=50.0,
            setup_maturity=50.0,
            trigger_maturity=50.0,
            micro_acceleration=50.0,
            alignment_score=50.0,
            entry_quality=_entry_quality(
                tf15.extension_atr,
                acceptable_entry_extension_atr,
                severe_entry_extension_atr,
            ),
            readiness=0.0,
            conflict_severity=0.0,
            stage="WATCH",
            supporting=[],
            conflicts=[],
            neutral=["4H／1H／15m 尚未形成可交易方向"],
            summary="多時間框架尚未形成清楚方向，目前維持觀望。",
        )

    bias = _relative_timeframe_score(tf4, direction)
    setup = _setup_score(tf1, direction, regime)
    trigger = _trigger_score(tf15, direction, regime)
    micro = _micro_score(tf5, direction) if tf5 is not None else 50.0
    entry = _entry_quality(
        tf15.extension_atr,
        acceptable_entry_extension_atr,
        severe_entry_extension_atr,
    )

    structure_1h = _structure_score(tf1, direction, regime)
    structure_15m = _structure_score(tf15, direction, regime)
    position_score = bias * 0.35 + structure_1h * 0.40 + structure_15m * 0.25
    trend_score = setup * 0.45 + trigger * 0.55
    participation_score = _base_participation_score(tf1, tf15, tf5, direction)

    position_support, position_conflicts, position_neutral = _position_reasons(
        bias, structure_1h, structure_15m, direction
    )
    trend_support, trend_conflicts, trend_neutral = _trend_reasons(
        setup, trigger, tf15.adx14, direction
    )
    part_support, part_conflicts, part_neutral = _participation_reasons(
        tf1, tf15, tf5, direction
    )

    groups = {
        "position_structure": _make_group(
            "position_structure",
            position_score,
            position_support,
            position_conflicts,
            position_neutral,
            100.0,
        ),
        "trend_momentum": _make_group(
            "trend_momentum",
            trend_score,
            trend_support,
            trend_conflicts,
            trend_neutral,
            100.0,
        ),
        "participation_flow": _make_group(
            "participation_flow",
            participation_score,
            part_support,
            part_conflicts,
            part_neutral,
            55.0 if tf5 is None else 70.0,
        ),
    }
    timeframe_states = {
        "4H": {
            "role": "大方向／Bias",
            "score": round(bias, 1),
            "direction": _raw_direction(_long_timeframe_score(tf4)),
            "label": _bias_label(_long_timeframe_score(tf4)),
        },
        "1H": {
            "role": "Setup／準備層",
            "score": round(setup, 1),
            "direction": direction if setup >= 55.0 else "NEUTRAL",
            "label": (
                f"{_direction_cn(direction)}方準備完成"
                if setup >= 70.0
                else f"{_direction_cn(direction)}方準備形成"
                if setup >= 55.0
                else "準備不足"
            ),
        },
        "15m": {
            "role": "Main Trigger／主要觸發",
            "score": round(trigger, 1),
            "direction": direction if trigger >= 62.0 else "NEUTRAL",
            "label": (
                f"{_direction_cn(direction)}觸發"
                if trigger >= 62.0
                else f"接近{_direction_cn(direction)}觸發"
                if trigger >= 50.0
                else "尚未觸發"
            ),
        },
        "5m": {
            "role": "加速／提前預警",
            "score": round(micro, 1),
            "direction": direction if micro >= 62.0 else "NEUTRAL",
            "label": (
                f"{_direction_cn(direction)}方加速"
                if micro >= 62.0
                else "反向干擾"
                if tf5 is not None and micro < 35.0
                else "中性"
                if tf5 is not None
                else "尚未取得"
            ),
        },
    }
    alignment = _alignment_score(groups, regime)
    conflict = _conflict_severity(groups, False)
    readiness = _readiness(
        bias,
        setup,
        trigger,
        micro,
        alignment,
        float(entry["score"]),
        conflict,
    )
    stage = _classify_stage(
        regime,
        groups,
        setup,
        trigger,
        readiness,
        conflict,
        str(entry["key"]),
    )
    supporting = _unique([item for group in groups.values() for item in group.supporting])
    conflicts = _unique([item for group in groups.values() for item in group.conflicts])
    neutral = _unique([item for group in groups.values() for item in group.neutral])
    summary = _summary(direction, timeframe_states, participation_score, stage)
    return EvidenceAssessment(
        direction=direction,
        regime=regime,
        groups=groups,
        timeframe_states=timeframe_states,
        bias_quality=round(bias, 1),
        setup_maturity=round(setup, 1),
        trigger_maturity=round(trigger, 1),
        micro_acceleration=round(micro, 1),
        alignment_score=round(alignment, 1),
        entry_quality=entry,
        readiness=readiness,
        conflict_severity=conflict,
        stage=stage,
        supporting=supporting,
        conflicts=conflicts,
        neutral=neutral,
        summary=summary,
    )


def adx_quality(adx_value: float) -> float:
    """Continuous ADX quality: no cliff at 20/21."""
    if not math.isfinite(adx_value):
        return 0.0
    if adx_value <= 10.0:
        return 10.0
    if adx_value <= 25.0:
        return 10.0 + (adx_value - 10.0) / 15.0 * 55.0
    if adx_value <= 40.0:
        return 65.0 + (adx_value - 25.0) / 15.0 * 30.0
    return 95.0


def _long_timeframe_score(tf: TimeframeFeatures) -> float:
    atr = max(tf.atr14, abs(tf.close) * 0.0001, 1e-9)
    components = (
        (_smooth((tf.close - tf.ema21) / atr, 0.9), 0.20),
        (_smooth((tf.ema21 - tf.ema55) / atr, 1.4), 0.20),
        (_smooth((tf.sma5 - tf.sma10) / atr, 0.6), 0.10),
        (_smooth((tf.sma10 - tf.sma20) / atr, 0.8), 0.08),
        (_smooth(tf.ema21_slope_atr, 0.35), 0.16),
        (_smooth((tf.macd_line - tf.macd_signal) / atr, 0.20), 0.12),
        (_smooth((tf.macd_hist - tf.macd_prev_hist) / atr, 0.12), 0.07),
        (_clamp((tf.rsi14 - 50.0) / 25.0, -1.0, 1.0), 0.07),
    )
    signed = sum(value * weight for value, weight in components)
    return round(_clamp(50.0 + signed * 50.0, 0.0, 100.0), 1)


def _relative_timeframe_score(tf: TimeframeFeatures, direction: str) -> float:
    long_score = _long_timeframe_score(tf)
    return long_score if direction == "LONG" else 100.0 - long_score


def _setup_score(tf: TimeframeFeatures, direction: str, regime: str) -> float:
    relative = _relative_timeframe_score(tf, direction)
    sign = 1.0 if direction == "LONG" else -1.0
    improving_hist = (tf.macd_hist - tf.macd_prev_hist) * sign
    atr = max(tf.atr14, 1e-9)
    turn_score = _clamp(50.0 + _smooth(improving_hist / atr, 0.12) * 50.0, 0.0, 100.0)
    price_side = 100.0 if (tf.close - tf.ema21) * sign > 0 else 35.0
    structure = _structure_score(tf, direction, regime)
    strength = adx_quality(tf.adx14)
    strength_weight = 0.08 if regime == "RANGE" else 0.12
    return round(
        relative * 0.38
        + turn_score * 0.22
        + price_side * 0.13
        + structure * (0.27 - strength_weight)
        + strength * strength_weight,
        1,
    )


def _trigger_score(tf: TimeframeFeatures, direction: str, regime: str) -> float:
    sign = 1.0 if direction == "LONG" else -1.0
    boundary = tf.prior_high20 if direction == "LONG" else tf.prior_low20
    distance = (tf.close - boundary) * sign / max(tf.atr14, 1e-9)
    structure = _clamp(55.0 + distance * 45.0, 0.0, 100.0)
    if distance > 0:
        structure = max(structure, 82.0)
    ema_side = _clamp(50.0 + (tf.close - tf.ema21) * sign / max(tf.atr14, 1e-9) * 35.0, 0.0, 100.0)
    macd = _clamp(
        50.0 + _smooth((tf.macd_line - tf.macd_signal) * sign / max(tf.atr14, 1e-9), 0.18) * 50.0,
        0.0,
        100.0,
    )
    impulse = _clamp(
        50.0 + _smooth((tf.macd_hist - tf.macd_prev_hist) * sign / max(tf.atr14, 1e-9), 0.10) * 50.0,
        0.0,
        100.0,
    )
    vwap = 75.0 if (tf.close - tf.vwap20) * sign > 0 else 30.0
    rsi = _clamp(50.0 + (tf.rsi14 - 50.0) * sign * 2.0, 0.0, 100.0)
    pressure = tf.directional_volume_ratio if direction == "LONG" else 1.0 - tf.directional_volume_ratio
    participation = _clamp(
        _volume_score(tf.volume_ratio) * 0.55 + (50.0 + (pressure - 0.5) * 250.0) * 0.45,
        0.0,
        100.0,
    )
    structure_weight = 0.30 if regime == "BREAKOUT_READY" else 0.22
    return round(
        structure * structure_weight
        + ema_side * 0.16
        + macd * 0.16
        + impulse * 0.14
        + vwap * 0.10
        + rsi * 0.08
        + participation * (0.36 - structure_weight),
        1,
    )


def _structure_score(tf: TimeframeFeatures, direction: str, regime: str) -> float:
    sign = 1.0 if direction == "LONG" else -1.0
    atr = max(tf.atr14, 1e-9)
    if regime == "RANGE":
        width = tf.prior_high20 - tf.prior_low20
        if width <= 0:
            return 50.0
        position = (tf.close - tf.prior_low20) / width
        edge = 1.0 - position if direction == "SHORT" else position
        return round(_clamp((0.45 - edge) / 0.45 * 100.0, 0.0, 100.0), 1)
    boundary = tf.prior_high20 if direction == "LONG" else tf.prior_low20
    boundary_score = _clamp(55.0 + (tf.close - boundary) * sign / atr * 35.0, 0.0, 100.0)
    trend_position = _clamp(50.0 + (tf.close - tf.ema21) * sign / atr * 30.0, 0.0, 100.0)
    return round(boundary_score * 0.55 + trend_position * 0.45, 1)


def _base_participation_score(
    tf1: TimeframeFeatures,
    tf15: TimeframeFeatures,
    tf5: TimeframeFeatures | None,
    direction: str,
) -> float:
    pressure15 = tf15.directional_volume_ratio if direction == "LONG" else 1.0 - tf15.directional_volume_ratio
    parts = [
        (_volume_score(tf1.volume_ratio), 0.25),
        (_volume_score(tf15.volume_ratio), 0.40),
        (_clamp(50.0 + (pressure15 - 0.50) * 250.0, 0.0, 100.0), 0.25),
    ]
    if tf5 is not None:
        parts.append((_micro_score(tf5, direction), 0.10))
    denominator = sum(weight for _, weight in parts)
    return round(sum(score * weight for score, weight in parts) / denominator, 1)


def _micro_score(tf: TimeframeFeatures, direction: str) -> float:
    pressure = tf.directional_volume_ratio if direction == "LONG" else 1.0 - tf.directional_volume_ratio
    return round(
        _relative_timeframe_score(tf, direction) * 0.45
        + _volume_score(tf.volume_ratio) * 0.30
        + _clamp(50.0 + (pressure - 0.50) * 250.0, 0.0, 100.0) * 0.25,
        1,
    )


def _volume_score(volume_ratio: float) -> float:
    return _clamp(30.0 + (volume_ratio - 0.60) / 1.20 * 70.0, 15.0, 100.0)


def _entry_quality(
    extension_atr: float,
    acceptable_atr: float = 0.80,
    severe_atr: float = 1.80,
) -> dict[str, Any]:
    excellent_atr = min(0.35, acceptable_atr)
    highly_extended_atr = acceptable_atr + (severe_atr - acceptable_atr) * 0.70
    if extension_atr <= excellent_atr:
        key, label, score = "EXCELLENT", "位置很好", 95.0
    elif extension_atr <= acceptable_atr:
        key, label = "ACCEPTABLE", "可以接受"
        span = max(acceptable_atr - excellent_atr, 1e-9)
        score = 95.0 - (extension_atr - excellent_atr) / span * 20.0
    elif extension_atr <= highly_extended_atr:
        key, label = "EXTENDED", "有些延伸"
        span = max(highly_extended_atr - acceptable_atr, 1e-9)
        score = 75.0 - (extension_atr - acceptable_atr) / span * 35.0
    elif extension_atr <= severe_atr:
        key, label = "HIGHLY_EXTENDED", "高度延伸"
        span = max(severe_atr - highly_extended_atr, 1e-9)
        score = 40.0 - (extension_atr - highly_extended_atr) / span * 20.0
    else:
        key, label, score = "SEVERE_CHASE", "嚴重追價", 0.0
    return {
        "key": key,
        "label": label,
        "score": round(_clamp(score, 0.0, 100.0), 1),
        "extension_atr": round(extension_atr, 2),
    }


def _make_group(
    key: str,
    score: float,
    supporting: list[str],
    conflicts: list[str],
    neutral: list[str],
    confidence: float,
) -> EvidenceGroup:
    score = _clamp(score, 0.0, 100.0)
    stance = "SUPPORT" if score >= 60.0 else "CONFLICT" if score < 40.0 else "NEUTRAL"
    return EvidenceGroup(
        key=key,
        score=round(score, 1),
        stance=stance,
        confidence=confidence,
        supporting=_unique(supporting),
        conflicts=_unique(conflicts),
        neutral=_unique(neutral),
    )


def _alignment_score(groups: dict[str, EvidenceGroup], regime: str) -> float:
    weights = REGIME_WEIGHTS.get(regime, REGIME_WEIGHTS["DISORDER"])
    return round(sum(groups[key].score * weight for key, weight in weights.items()), 1)


def _conflict_severity(groups: dict[str, EvidenceGroup], macro_conflict: bool) -> float:
    strong = [max(0.0, 40.0 - group.score) * 1.7 for group in groups.values()]
    conflicting_groups = sum(group.score < 40.0 for group in groups.values())
    cross_group = 20.0 if conflicting_groups >= 2 else 0.0
    macro = 25.0 if macro_conflict else 0.0
    return round(_clamp(max(strong, default=0.0) + cross_group + macro, 0.0, 100.0), 1)


def _readiness(
    bias: float,
    setup: float,
    trigger: float,
    micro: float,
    alignment: float,
    entry: float,
    conflict: float,
) -> float:
    score = (
        bias * 0.15
        + setup * 0.25
        + trigger * 0.25
        + micro * 0.05
        + alignment * 0.20
        + entry * 0.10
        - min(conflict, 25.0)
    )
    return round(_clamp(score, 0.0, 100.0), 1)


def _classify_stage(
    regime: str,
    groups: dict[str, EvidenceGroup],
    setup: float,
    trigger: float,
    readiness: float,
    conflict: float,
    entry_key: str,
) -> str:
    if regime == "DISORDER" or entry_key == "SEVERE_CHASE":
        return "WATCH"
    scores = [group.score for group in groups.values()]
    support_count = sum(score >= 60.0 for score in scores)
    confirmed_groups = sum(score >= 70.0 for score in scores)
    early = (
        setup >= 55.0
        and trigger >= 62.0
        and support_count >= 2
        and min(scores) >= 35.0
        and conflict < 50.0
        and readiness >= 60.0
    )
    confirmed = (
        early
        and setup >= 70.0
        and trigger >= 72.0
        and confirmed_groups >= 2
        and min(scores) >= 50.0
        and groups["participation_flow"].score >= 60.0
        and conflict < 35.0
        and readiness >= 72.0
    )
    if confirmed:
        return "CONFIRMED"
    if early:
        return "EARLY_SIGNAL"
    if setup >= 45.0 and trigger >= 48.0 and readiness >= 48.0:
        return "NEAR_TRIGGER"
    return "WATCH"


def _position_reasons(
    bias: float,
    structure_1h: float,
    structure_15m: float,
    direction: str,
) -> tuple[list[str], list[str], list[str]]:
    support: list[str] = []
    conflicts: list[str] = []
    neutral: list[str] = []
    if bias >= 60.0:
        support.append(f"4H 背景支持{_direction_cn(direction)}")
    elif bias < 30.0:
        conflicts.append("4H 背景明顯反向")
    else:
        neutral.append("4H 背景中性／未確認")
    if structure_1h >= 60.0:
        support.append("1H 結構位置具交易價值")
    elif structure_1h < 35.0:
        conflicts.append("1H 位置與候選方向衝突")
    else:
        neutral.append("1H 結構位置中性")
    if structure_15m >= 60.0:
        support.append("15m 小結構支持觸發")
    elif structure_15m < 35.0:
        conflicts.append("15m 小結構尚未支持")
    else:
        neutral.append("15m 結構接近但尚未完成")
    return support, conflicts, neutral


def _trend_reasons(
    setup: float,
    trigger: float,
    adx_value: float,
    direction: str,
) -> tuple[list[str], list[str], list[str]]:
    support: list[str] = []
    conflicts: list[str] = []
    neutral: list[str] = []
    if setup >= 60.0:
        support.append(f"1H {_direction_cn(direction)}方 Setup 形成")
    elif setup < 35.0:
        conflicts.append("1H 動能與候選方向衝突")
    else:
        neutral.append("1H Setup 尚在形成")
    if trigger >= 62.0:
        support.append(f"15m 已出現{_direction_cn(direction)}觸發")
    elif trigger < 35.0:
        conflicts.append("15m 動能明顯反向")
    else:
        neutral.append("15m 觸發尚未完成")
    quality = adx_quality(adx_value)
    if quality >= 70.0:
        support.append("趨勢強度良好")
    else:
        neutral.append("趨勢強度普通，不作單獨否決")
    return support, conflicts, neutral


def _participation_reasons(
    tf1: TimeframeFeatures,
    tf15: TimeframeFeatures,
    tf5: TimeframeFeatures | None,
    direction: str,
) -> tuple[list[str], list[str], list[str]]:
    support: list[str] = []
    conflicts: list[str] = []
    neutral: list[str] = []
    pressure = tf15.directional_volume_ratio if direction == "LONG" else 1.0 - tf15.directional_volume_ratio
    if tf15.volume_ratio >= 1.15:
        support.append("15m 成交開始擴張")
    else:
        neutral.append("15m 成交量中性")
    if pressure >= 0.56:
        support.append("15m K 棒成交方向支持")
    elif pressure < 0.38:
        conflicts.append("15m 成交方向明顯反向")
    else:
        neutral.append("15m 成交方向中性")
    if tf1.volume_ratio >= 1.15:
        support.append("1H 成交參與增加")
    else:
        neutral.append("1H 成交參與普通")
    if tf5 is None:
        neutral.append("5m 加速資料待深度候選階段取得")
    return support, conflicts, neutral


def _summary(
    direction: str,
    timeframe_states: dict[str, dict[str, Any]],
    participation: float,
    stage: str,
) -> str:
    direction_text = _direction_cn(direction)
    stage_text = {
        "WATCH": "觀望",
        "NEAR_TRIGGER": "接近觸發",
        "EARLY_SIGNAL": f"早期{direction_text}",
        "CONFIRMED": f"完整確認{direction_text}",
    }[stage]
    participation_text = "良好" if participation >= 70.0 else "中等" if participation >= 50.0 else "偏弱"
    return (
        f"4H {timeframe_states['4H']['label']}，"
        f"1H {timeframe_states['1H']['label']}，"
        f"15m {timeframe_states['15m']['label']}；"
        f"市場參與{participation_text}，目前列為{stage_text}。"
    )


def _timeframe_state(role: str, score: float, direction: str, label: str) -> dict[str, Any]:
    return {"role": role, "score": score, "direction": direction, "label": label}


def _raw_direction(long_score: float) -> str:
    return "LONG" if long_score >= 60.0 else "SHORT" if long_score <= 40.0 else "NEUTRAL"


def _bias_label(long_score: float) -> str:
    return "偏多" if long_score >= 60.0 else "偏空" if long_score <= 40.0 else "中性"


def _direction_cn(direction: str) -> str:
    return "做多" if direction == "LONG" else "做空" if direction == "SHORT" else "中性"


def _smooth(value: float, scale: float) -> float:
    return math.tanh(value / max(scale, 1e-9))


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(value, maximum))


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
