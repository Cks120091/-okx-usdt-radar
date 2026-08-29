from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Protocol

from .market_story import execution_quality
from .models import MarketContext, Signal, Ticker
from .strategy import _entry_eligibility


class PreflightConfig(Protocol):
    minimum_rr: float
    max_execution_cost_to_risk_pct: float
    max_spread_pct: float
    max_slippage_pct: float
    estimated_taker_fee_pct: float
    entry_ready_max_chase_atr: float
    entry_missed_chase_atr: float


def build_preflight_payload(
    signal: Signal,
    ticker: Ticker,
    context: MarketContext,
    config: PreflightConfig,
    *,
    report_generated_at: str,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Re-evaluate execution conditions without mutating the stored Trigger."""

    entry_low = _required_number(signal.entry_low, "entry_low")
    entry_high = _required_number(signal.entry_high, "entry_high")
    stop = _required_number(signal.stop_loss, "stop_loss")
    target_1 = _required_number(signal.take_profit_1, "take_profit_1")
    target_2 = _required_number(signal.take_profit_2, "take_profit_2")
    original_price = _optional_number(signal.market_metrics.get("last_price"))
    current_price = _required_number(ticker.last, "current_price")
    atr = _signal_atr(signal)

    best_bid = _optional_number(context.best_bid) or _required_number(ticker.bid, "best_bid")
    best_ask = _optional_number(context.best_ask) or _required_number(ticker.ask, "best_ask")
    live_spread_pct = _spread_pct(best_bid, best_ask)
    eligibility = _entry_eligibility(
        direction=signal.direction,
        current_price=current_price,
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        target=target_1,
        atr=atr,
        stage=signal.signal_stage,
        minimum_rr=config.minimum_rr,
        ready_max_chase_atr=config.entry_ready_max_chase_atr,
        missed_chase_atr=config.entry_missed_chase_atr,
    )

    is_long = signal.direction == "LONG"
    current_risk = current_price - stop if is_long else stop - current_price
    current_reward = target_1 - current_price if is_long else current_price - target_1
    risk_pct = (
        abs(current_risk) / max(abs(current_price), 1e-9) * 100.0
        if current_risk > 0
        else 0.0
    )
    remaining_rr = eligibility.get("remaining_rr")
    quality_rr = (
        float(remaining_rr)
        if isinstance(remaining_rr, (int, float)) and math.isfinite(remaining_rr)
        else 0.0
    )
    invalidated = current_risk <= 0
    target_reached = current_reward <= 0 and not invalidated
    entry_location = _live_entry_location(
        eligibility,
        invalidated=invalidated,
        target_reached=target_reached,
    )
    live_story = SimpleNamespace(
        execution_quality={"entry_location": entry_location},
        trigger_direction=signal.direction,
    )
    quality = execution_quality(
        live_story,
        live_spread_pct,
        risk_pct,
        max(quality_rr, 0.0),
        context,
        target_rr=config.minimum_rr,
        max_cost_to_risk_pct=config.max_execution_cost_to_risk_pct,
        max_spread_pct=config.max_spread_pct,
        max_slippage_pct=config.max_slippage_pct,
        estimated_taker_fee_pct=config.estimated_taker_fee_pct,
    )
    book_available = bool(context.source_timestamps.get("order_book"))
    execution_complete = context.execution_quality_complete and book_available
    directional_slippage = (
        context.buy_slippage_pct
        if signal.direction == "LONG"
        else context.sell_slippage_pct
    )
    cost_to_risk = _optional_number(quality.get("execution_cost_to_risk_pct"))
    hard_blockers: list[str] = []
    if not execution_complete or directional_slippage is None:
        hard_blockers.append("EXECUTION_DATA_UNAVAILABLE")
    elif directional_slippage > config.max_slippage_pct:
        hard_blockers.append("SLIPPAGE_TOO_HIGH")
    if live_spread_pct > config.max_spread_pct:
        hard_blockers.append("SPREAD_TOO_HIGH")
    if cost_to_risk is None:
        hard_blockers.append("EXECUTION_DATA_UNAVAILABLE")
    elif cost_to_risk > config.max_execution_cost_to_risk_pct:
        hard_blockers.append("EXECUTION_COST_TOO_HIGH")
    # On the adverse side of Entry the plan first needs a structural retest,
    # so ``remaining_rr`` is intentionally not applicable.  Do not turn that
    # positional WAIT into a fabricated zero-R:R Hard Gate; the gate is
    # evaluated again as soon as a live entry becomes eligible.
    if isinstance(remaining_rr, (int, float)) and quality_rr < config.minimum_rr:
        hard_blockers.append("RR_INSUFFICIENT")
    hard_blockers = _unique(hard_blockers)

    verdict_status = eligibility["status"]
    verdict_label = eligibility["label"]
    verdict_reason = eligibility["reason"]
    chase_atr = float(eligibility.get("chase_atr", 0.0) or 0.0)
    adverse_atr = float(eligibility.get("adverse_atr", 0.0) or 0.0)
    invalidation_progress_pct = float(
        eligibility.get("invalidation_progress_pct", 0.0) or 0.0
    )
    entry_situation = "IN_ENTRY_AREA"
    if invalidated:
        entry_situation = "INVALIDATED"
        verdict_status = "PLAN_INVALIDATED"
        verdict_label = "原交易計畫失效｜禁止沿用舊價位"
        verdict_reason = (
            "最新價格已越過原始止損／失效位置；舊理想價格、SL、TP 已停用。"
            "這不等於原做多／做空方向已反轉，方向必須等待新 K 線與新 Trigger 重新判定。"
        )
    elif target_reached:
        entry_situation = "TARGET_REACHED"
        verdict_status = "MISSED_ENTRY"
        verdict_label = "已到達第一目標｜禁止追價"
        verdict_reason = "最新價格已到達或越過原始 TP1，這個進場機會已經結束。"
    elif adverse_atr > 0:
        if invalidation_progress_pct >= 80.0:
            entry_situation = "NEAR_INVALIDATION"
            verdict_label = "接近失效｜暫停新進場"
            verdict_reason = (
                f"價格位於最佳進場點位不利側 {adverse_atr:.2f} ATR，"
                f"已走過進場區至原始 SL 距離的 {invalidation_progress_pct:.1f}%；"
                "原 Trigger 尚未失效，但禁止新進場。"
            )
        else:
            entry_situation = "ADVERSE_TOLERANCE"
            verdict_label = "容許回測中｜等待重新確認"
            verdict_reason = (
                f"價格位於最佳進場點位不利側 {adverse_atr:.2f} ATR，"
                "但尚未越過原始 SL／失效位置；原 Trigger 仍有效，"
                "新進場必須等待重新站回並確認。"
            )
    elif verdict_status == "WAIT_RETEST" and chase_atr > 0:
        entry_situation = "FAVORABLE_AWAY"
        verdict_label = "已離開最佳進場點｜等待回踩"
        verdict_reason = (
            f"價格已朝原訊號有利方向離開最佳進場點位 {chase_atr:.2f} ATR；"
            "原 Trigger 仍有效，但尚未進場者現在不應追價。"
        )
    elif verdict_status == "MISSED_ENTRY" and chase_atr > 0:
        entry_situation = "FAVORABLE_MISSED"
        verdict_label = "已離開最佳進場點｜禁止追價"
        verdict_reason = (
            f"價格已朝原訊號有利方向離開最佳進場點位 {chase_atr:.2f} ATR；"
            "原 Trigger 仍保留，但本次新進場機會已錯過。"
        )
    elif verdict_status == "WAIT_RETEST":
        entry_situation = "WAIT_RETEST"
    elif verdict_status == "MISSED_ENTRY":
        entry_situation = "ENTRY_WINDOW_CLOSED"

    # Execution Hard Gates block only a new order.  They never erase the
    # original price Trigger and never turn a temporary no-entry state into a
    # terminal invalidation.
    if not invalidated and not target_reached and hard_blockers:
        missing_execution = "EXECUTION_DATA_UNAVAILABLE" in hard_blockers
        entry_situation = (
            "DATA_UNAVAILABLE" if missing_execution else "HARD_GATE_BLOCKED"
        )
        verdict_status = entry_situation
        verdict_label = (
            "成交資料不足｜禁止新進場"
            if missing_execution
            else "執行風控未通過｜暫停新進場"
        )
        hard_labels = {
            "EXECUTION_DATA_UNAVAILABLE": "Order Book／Slippage 資料不足",
            "SLIPPAGE_TOO_HIGH": "Slippage（滑價）超過上限",
            "SPREAD_TOO_HIGH": "Spread（買賣價差）超過上限",
            "EXECUTION_COST_TOO_HIGH": "交易成本占風險過高",
            "RR_INSUFFICIENT": "R:R（風險報酬比）不足",
        }
        verdict_reason = "；".join(
            hard_labels.get(item, item) for item in hard_blockers[:3]
        )

    if invalidated:
        lifecycle_status = "INVALIDATED"
        lifecycle_label = "已觸發・已失效"
        lifecycle_note = "最新價格已越過原始 SL／失效位置；同一筆 Trigger 不會復活。"
    elif target_reached:
        lifecycle_status = "TARGET_REACHED"
        lifecycle_label = "已觸發・目標已達"
        lifecycle_note = "原始 TP1 已到達；本次新進場機會已結束。"
    else:
        lifecycle_status = "ACTIVE"
        lifecycle_label = "已觸發・有效中"
        lifecycle_note = (
            "正式 Trigger 已成立；價格位置只改變目前進場資格，"
            "不會把已觸發訊號改回未觸發。"
        )

    if invalidated or target_reached:
        warning = "原交易計畫已失效" if invalidated else "原始第一目標已到達"
        quality = {
            **quality,
            "score": 0.0,
            "label": "不可執行",
            "recommendation": "AVOID_EXECUTION",
            "warnings": _unique([warning, *quality.get("warnings", [])]),
        }

    new_plan_required = invalidated or target_reached or verdict_status == "MISSED_ENTRY"
    if invalidated:
        plan_status = "INVALIDATED"
    elif target_reached:
        plan_status = "TARGET_REACHED"
    elif verdict_status == "MISSED_ENTRY":
        plan_status = "MISSED"
    elif verdict_status == "WAIT_RETEST":
        plan_status = "WAITING_RETEST"
    elif verdict_status in {"DATA_UNAVAILABLE", "HARD_GATE_BLOCKED"}:
        plan_status = "ACTIVE_ENTRY_BLOCKED"
    else:
        plan_status = "ACTIVE"

    sampled_at = max(int(ticker.ts or 0), int(context.sampled_at or 0))
    trigger_age_bars = _trigger_age_bars(signal, sampled_at or now_ms)
    original_quality = _optional_number(signal.execution_quality.get("score"))
    price_change_from_scan = (
        (current_price - original_price) / original_price * 100.0
        if original_price is not None and original_price > 0
        else None
    )
    return {
        "inst_id": signal.inst_id,
        "horizon": signal.radar_horizon,
        "horizon_label": "4H 長線" if signal.radar_horizon == "LONG" else "15m 短線",
        "direction": signal.direction,
        "strategy": signal.strategy,
        "trigger_type": signal.trigger_type,
        "signal_stage": signal.signal_stage,
        "verdict": {
            "status": verdict_status,
            "situation": entry_situation,
            "label": verdict_label,
            "reason": verdict_reason,
            "actionable": verdict_status == "ENTRY_READY",
            "hard_blockers": hard_blockers,
        },
        "signal_lifecycle": {
            "status": lifecycle_status,
            "label": lifecycle_label,
            "triggered": True,
            "active": lifecycle_status == "ACTIVE",
            "terminal": lifecycle_status in {"INVALIDATED", "TARGET_REACHED"},
            "note": lifecycle_note,
        },
        "plan_state": {
            "status": plan_status,
            "old_plan_reusable": not new_plan_required,
            "old_plan_reusable_for_new_entry": not new_plan_required,
            "existing_position_plan_active": lifecycle_status == "ACTIVE",
            "new_entry_status": (
                "READY"
                if verdict_status == "ENTRY_READY"
                else "WAIT"
                if verdict_status
                in {"WAIT_RETEST", "DATA_UNAVAILABLE", "HARD_GATE_BLOCKED"}
                else "CLOSED"
            ),
            "new_entry_allowed": verdict_status == "ENTRY_READY",
            "direction_still_valid": not invalidated,
            "direction_status": (
                "PENDING_REASSESSMENT"
                if invalidated
                else "ORIGINAL_BIAS_RETAINED"
            ),
            "new_trigger_required": new_plan_required,
            "note": (
                "舊交易計畫失效不等於方向反轉；若行情重新成立，必須由新的 Trigger／REENTRY "
                "建立全新的理想價格、SL 與 TP。"
                if invalidated
                else "原始 TP1 已到達；本次機會已完成，任何新進場都必須等待新的 Trigger。"
                if target_reached
                else "原 Trigger 仍保留作生命週期追蹤，但已不再提供新進場；"
                "若已持倉，仍依原始 SL／TP 管理。"
                if verdict_status == "MISSED_ENTRY"
                else "原始方向偏向仍保留，但只有即時判定為目前可進時才具備進場資格。"
            ),
        },
        "original": {
            "report_generated_at": report_generated_at,
            "triggered_at": (
                signal.lifecycle.get("triggered_at")
                or _iso_from_ms(
                    int(signal.market_story.get("trigger", {}).get("event_ts") or 0)
                )
            ),
            "trigger_age_bars_at_scan": _original_age_bars(signal),
            "price": _round_or_none(original_price, 12),
            "quality_score": _round_or_none(original_quality, 1),
            "entry_low": entry_low,
            "entry_high": entry_high,
            "stop_loss": stop,
            "take_profit_1": target_1,
            "take_profit_2": target_2,
        },
        "live": {
            "sampled_at": _iso_from_ms(sampled_at),
            "price": round(current_price, 12),
            "price_change_from_scan_pct": _round_or_none(price_change_from_scan, 3),
            "trigger_age_bars": trigger_age_bars,
            "chase_atr": eligibility["chase_atr"],
            "remaining_rr": _round_or_none(remaining_rr, 3),
            "remaining_rr_applicable": eligibility.get(
                "remaining_rr_applicable",
                False,
            ),
            "adverse_atr": eligibility.get("adverse_atr", 0.0),
            "invalidation_progress_pct": eligibility.get(
                "invalidation_progress_pct",
                0.0,
            ),
            "risk_pct": round(risk_pct, 4),
            "quality_score": quality["score"],
            "quality_label": quality["label"],
            "quality_recommendation": quality["recommendation"],
        },
        "execution": {
            "best_bid": round(best_bid, 12),
            "best_ask": round(best_ask, 12),
            "spread_pct": round(live_spread_pct, 4),
            "buy_slippage_pct": _round_or_none(context.buy_slippage_pct, 5),
            "sell_slippage_pct": _round_or_none(context.sell_slippage_pct, 5),
            "estimated_round_trip_cost_pct": quality.get("estimated_round_trip_cost_pct"),
            "execution_cost_to_risk_pct": quality.get("execution_cost_to_risk_pct"),
            "bid_depth_usd": _round_or_none(context.bid_depth_usd, 2),
            "ask_depth_usd": _round_or_none(context.ask_depth_usd, 2),
            "order_book_imbalance_pct": _round_or_none(
                (context.order_book_imbalance or 0.0) * 100.0
                if context.order_book_imbalance is not None
                else None,
                1,
            ),
            "execution_notional_usdt": context.execution_notional_usdt,
        },
        "warnings": _unique(list(quality.get("warnings", []))),
        "data_quality": {
            "status": "AVAILABLE" if execution_complete else "PARTIAL",
            "ticker_available": True,
            "order_book_available": book_available,
            "execution_depth_complete": context.execution_quality_complete,
            "missing_sources": [] if execution_complete else ["order_book_depth"],
        },
        "safety": {
            "analysis_only": True,
            "auto_ordering": False,
            "stored_trigger_unchanged": True,
            "note": "即時檢查只更新執行條件，不產生、刪除或改寫核心 Trigger。",
        },
    }


def _live_entry_location(
    eligibility: dict[str, Any],
    *,
    invalidated: bool,
    target_reached: bool,
) -> dict[str, Any]:
    chase_atr = float(eligibility.get("chase_atr", 0.0) or 0.0)
    if invalidated:
        return {
            "key": "INVALIDATED",
            "label": "原交易計畫已失效",
            "score": 0.0,
            "extension_atr": round(chase_atr, 3),
        }
    if target_reached:
        return {
            "key": "SEVERE_CHASE",
            "label": "第一目標已到達",
            "score": 0.0,
            "extension_atr": round(chase_atr, 3),
        }
    status = eligibility.get("status")
    if status == "ENTRY_READY":
        ready_limit = max(float(eligibility.get("ready_max_chase_atr", 0.15)), 1e-9)
        score = max(75.0, 95.0 - min(chase_atr / ready_limit, 1.0) * 20.0)
        key, label = "LIVE_ACCEPTABLE", "仍在合理進場區"
    elif status == "WAIT_RETEST":
        score = 55.0
        key, label = "RETEST_REQUIRED", "等待回踩／重新確認"
    else:
        score = 10.0
        key, label = "SEVERE_CHASE", "已錯過／不宜追價"
    return {
        "key": key,
        "label": label,
        "score": round(score, 1),
        "extension_atr": round(chase_atr, 3),
    }


def _signal_atr(signal: Signal) -> float:
    story = signal.market_story or {}
    trigger = story.get("trigger", {}) if isinstance(story, dict) else {}
    raw = story.get("raw", {}) if isinstance(story, dict) else {}
    for value in (
        trigger.get("event_atr") if isinstance(trigger, dict) else None,
        raw.get("core_atr") if isinstance(raw, dict) else None,
    ):
        number = _optional_number(value)
        if number is not None and number > 0:
            return number
    raise ValueError("原始訊號缺少 ATR，無法安全重新判定進場距離")


def _original_age_bars(signal: Signal) -> int | None:
    lifecycle = signal.lifecycle or {}
    story = signal.market_story or {}
    trigger = story.get("trigger", {}) if isinstance(story, dict) else {}
    for value in (
        lifecycle.get("age_bars") if isinstance(lifecycle, dict) else None,
        trigger.get("event_age_bars") if isinstance(trigger, dict) else None,
    ):
        number = _optional_number(value)
        if number is not None:
            return max(0, int(number))
    return None


def _trigger_age_bars(signal: Signal, reference_ms: int | None) -> int | None:
    story = signal.market_story or {}
    trigger = story.get("trigger", {}) if isinstance(story, dict) else {}
    event_ts = _optional_number(trigger.get("event_ts")) if isinstance(trigger, dict) else None
    if event_ts is None or event_ts <= 0:
        return _original_age_bars(signal)
    current_ms = int(reference_ms or time.time() * 1000)
    interval_ms = 14_400_000 if signal.radar_horizon == "LONG" else 900_000
    return max(0, int((current_ms - int(event_ts)) // interval_ms))


def _spread_pct(bid: float, ask: float) -> float:
    midpoint = (bid + ask) / 2.0
    if midpoint <= 0 or ask < bid:
        raise ValueError("即時 Order Book 買賣價無效")
    return (ask - bid) / midpoint * 100.0


def _required_number(value: Any, field: str) -> float:
    number = _optional_number(value)
    if number is None:
        raise ValueError(f"原始訊號缺少 {field}")
    return number


def _optional_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round_or_none(value: Any, digits: int) -> float | None:
    number = _optional_number(value)
    return round(number, digits) if number is not None else None


def _iso_from_ms(value: int) -> str | None:
    if value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000.0, timezone.utc).isoformat()


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
