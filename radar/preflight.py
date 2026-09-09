from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Protocol

from .market_story import execution_quality
from .models import MarketContext, Signal, Ticker
from .price_display import signal_plan_display_fields
from .strategy import _entry_eligibility


class PreflightConfig(Protocol):
    min_quote_volume_24h: float
    quote_volume_buffer_24h: float
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
    market_last_price = _required_number(ticker.last, "current_price")
    atr = _signal_atr(signal)
    stored_entry = dict(signal.entry_eligibility or {})
    stored_lifecycle = dict(signal.lifecycle or {})
    stored_decision = dict(signal.decision_context or {})
    stored_hard_gate = dict(stored_decision.get("hard_gate", {}) or {})
    stored_final = dict(stored_decision.get("final", {}) or {})
    stored_permission_denied = bool(
        ("new_entry_allowed" in stored_entry and stored_entry.get("new_entry_allowed") is False)
        or ("actionable" in stored_entry and stored_entry.get("actionable") is False)
        or stored_final.get("new_entry_allowed") is False
    )
    lifecycle_transition = str(
        stored_lifecycle.get("transition") or ""
    ).upper()
    lifecycle_status = str(stored_lifecycle.get("status") or "").upper()
    newly_created_episode = lifecycle_transition == "NEW"
    lifecycle_marks_existing = bool(
        stored_lifecycle.get("first_seen_at")
        or lifecycle_transition
        or (
            signal.trigger_id
            and lifecycle_status
            in {"ACTIVE", "COMPLETED", "INVALIDATED", "CLOSED_UNKNOWN"}
        )
    )
    # A newly created Episode can be scan-time blocked only by a dynamic
    # execution input (for example spread or volume).  The resulting false
    # permission flags do not make it an "old" Episode that needs a separate
    # closed-candle reclaim.  For established/legacy rows, keep the safer
    # fallback so a denied old plan cannot reopen from a quote alone.
    existing_episode = bool(
        not newly_created_episode
        and (
            stored_entry.get("existing_episode") is True
            or lifecycle_marks_existing
            or stored_permission_denied
        )
    )
    entry_ready_once = bool(
        stored_entry.get("entry_ready_once") is True
        or stored_lifecycle.get("entry_ready_once") is True
    )
    closed_retest_confirmed = stored_entry.get("closed_retest_confirmed") is True

    # The ticker is fetched for this exact preflight request and is the
    # authoritative executable quote.  Context depth may be sampled on a
    # separate request and must not replace this final bid/ask pair.
    best_bid = _required_number(ticker.bid, "best_bid")
    best_ask = _required_number(ticker.ask, "best_ask")
    current_price = best_ask if signal.direction == "LONG" else best_bid
    current_price_source = "BEST_ASK" if signal.direction == "LONG" else "BEST_BID"
    liquidity_policy = _preflight_liquidity_policy(signal, ticker, config)
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
        existing_episode=existing_episode,
        entry_ready_once=entry_ready_once,
        closed_retest_confirmed=closed_retest_confirmed,
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
    raw_remaining_rr = (
        current_reward / current_risk
        if current_risk > 0 and eligibility.get("remaining_rr_applicable") is True
        else None
    )
    quality_rr = (
        float(raw_remaining_rr)
        if isinstance(raw_remaining_rr, (int, float))
        and math.isfinite(raw_remaining_rr)
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
    book_available = bool(context.source_timestamps.get("order_book"))
    quality = execution_quality(
        live_story,
        live_spread_pct,
        risk_pct,
        max(quality_rr, 0.0),
        context if book_available else None,
        target_rr=config.minimum_rr,
        max_cost_to_risk_pct=config.max_execution_cost_to_risk_pct,
        max_spread_pct=config.max_spread_pct,
        max_slippage_pct=config.max_slippage_pct,
        estimated_taker_fee_pct=config.estimated_taker_fee_pct,
    )
    execution_complete = context.execution_quality_complete and book_available
    directional_slippage = (
        context.buy_slippage_pct
        if signal.direction == "LONG"
        else context.sell_slippage_pct
    )
    raw_execution_cost = None
    raw_cost_to_risk = None
    # Numeric depth/slippage left on a context without a matching order-book
    # sample timestamp is not fresh enough to decide entry permission.
    if execution_complete:
        raw_execution_cost = (
            live_spread_pct
            + float(context.buy_slippage_pct or 0.0)
            + float(context.sell_slippage_pct or 0.0)
            + config.estimated_taker_fee_pct * 2.0
        )
        raw_cost_to_risk = (
            raw_execution_cost / risk_pct * 100.0 if risk_pct > 0 else None
        )
    cost_to_risk = raw_cost_to_risk
    risk_warning_codes: list[str] = []
    unavailable_warning_codes: set[str] = set()
    advisory_warning_codes: set[str] = set()
    live_quote_volume = liquidity_policy["volume_usdt"]
    if live_quote_volume is None:
        risk_warning_codes.append("QUOTE_VOLUME_DATA_UNAVAILABLE")
        unavailable_warning_codes.add("QUOTE_VOLUME_DATA_UNAVAILABLE")
    elif live_quote_volume < liquidity_policy["effective_min_usdt"]:
        risk_warning_codes.append("LIQUIDITY_TOO_LOW")
    known_slippage = [
        value
        for value in (context.buy_slippage_pct, context.sell_slippage_pct)
        if book_available and value is not None
    ]
    if any(value > config.max_slippage_pct for value in known_slippage):
        risk_warning_codes.append("SLIPPAGE_TOO_HIGH")
    elif not execution_complete or directional_slippage is None:
        risk_warning_codes.append("EXECUTION_ESTIMATE_UNAVAILABLE")
        advisory_warning_codes.add("EXECUTION_ESTIMATE_UNAVAILABLE")
    if live_spread_pct > config.max_spread_pct:
        risk_warning_codes.append("SPREAD_TOO_HIGH")
    if cost_to_risk is None:
        risk_warning_codes.append("EXECUTION_ESTIMATE_UNAVAILABLE")
        advisory_warning_codes.add("EXECUTION_ESTIMATE_UNAVAILABLE")
    elif cost_to_risk > config.max_execution_cost_to_risk_pct:
        risk_warning_codes.append("EXECUTION_COST_TOO_HIGH")
    # On the adverse side of Entry the plan first needs a structural retest,
    # so ``remaining_rr`` is intentionally not applicable.  Do not turn that
    # positional WAIT into a fabricated zero-R:R warning; R:R is recalculated
    # as soon as a live entry becomes eligible.
    if raw_remaining_rr is not None and raw_remaining_rr < config.minimum_rr:
        risk_warning_codes.append("RR_INSUFFICIENT")
    stored_gate_status = str(stored_hard_gate.get("status") or "").upper()
    live_rechecked_blockers = {
        "LIQUIDITY",
        "SPREAD",
        "SLIPPAGE",
        "EXECUTION_COST",
        "RISK_REWARD",
        "CHASE",
        # This is the stored positional permission computed by the same
        # _entry_eligibility contract above.  Keeping the old code after a
        # newly confirmed closed retest would make recovery impossible.
        "ENTRY_PERMISSION",
    }
    raw_stored_gate_blockers = [
        str(value).strip().upper()
        for value in list(stored_hard_gate.get("blockers", []) or [])
        if str(value).strip()
    ]
    stored_gate_blockers = [
        value
        for value in raw_stored_gate_blockers
        if value not in live_rechecked_blockers
    ]
    raw_stored_gate_unknowns = [
        str(value).strip().upper()
        for value in list(stored_hard_gate.get("unknowns", []) or [])
        if str(value).strip()
    ]
    stored_gate_unknowns = [
        value
        for value in raw_stored_gate_unknowns
        if value not in live_rechecked_blockers
    ]
    # A preflight request has just re-fetched every dynamic execution input
    # named above.  Do not resurrect a generic upstream block merely because
    # the old gate's status was BLOCKED/UNKNOWN when its complete reason list
    # consisted only of inputs that were rechecked live.  Legacy gates with no
    # reason list still fail closed because there is nothing concrete to
    # re-evaluate.
    stored_block_needs_fallback = bool(
        (stored_hard_gate.get("blocked") is True or stored_gate_status == "BLOCKED")
        and not raw_stored_gate_blockers
    )
    if stored_gate_blockers or stored_block_needs_fallback:
        risk_warning_codes.extend(
            stored_gate_blockers or ["UPSTREAM_HARD_GATE_BLOCKED"]
        )
    stored_unknown_needs_fallback = bool(
        (stored_hard_gate.get("unknown") is True or stored_gate_status == "UNKNOWN")
        and not raw_stored_gate_unknowns
    )
    if stored_gate_unknowns or stored_unknown_needs_fallback:
        unknown_codes = stored_gate_unknowns or ["UPSTREAM_DATA_UNAVAILABLE"]
        risk_warning_codes.extend(unknown_codes)
        unavailable_warning_codes.update(unknown_codes)
    trigger = dict(signal.market_story.get("trigger", {}) or {})
    if trigger.get("new_entry_suspended") is True or trigger.get(
        "opposite_warning_only"
    ) is True:
        # Keep the binding direction change visible even when several
        # execution failures are present and the plain-language reason is
        # intentionally shortened.
        risk_warning_codes.insert(0, "OPPOSITE_SIGNAL")
    risk_warning_codes = _unique(risk_warning_codes)
    risk_labels = {
        "QUOTE_VOLUME_DATA_UNAVAILABLE": "最新 24H USDT 成交額資料不足",
        "LIQUIDITY_TOO_LOW": (
            f"24H USDT 成交額低於"
            f"{liquidity_policy['effective_min_usdt']:,.0f} 門檻"
        ),
        "EXECUTION_DATA_UNAVAILABLE": "Order Book／Slippage 資料不足",
        "EXECUTION_ESTIMATE_UNAVAILABLE": (
            "Order Book 深度暫缺，滑價／成本未估算（不禁止進場）"
        ),
        "SLIPPAGE_TOO_HIGH": "Slippage（滑價）超過建議值",
        "SPREAD_TOO_HIGH": "Spread（買賣價差）超過建議值",
        "EXECUTION_COST_TOO_HIGH": "交易成本占風險偏高",
        "RR_INSUFFICIENT": "R:R（風險報酬比）低於建議值",
        "OPPOSITE_SIGNAL": "已出現正式反向價格訊號",
        "UPSTREAM_HARD_GATE_BLOCKED": "原掃描的風險條件尚未重新通過",
        "UPSTREAM_DATA_UNAVAILABLE": "原掃描的安全資料尚未恢復",
    }
    risk_warning_labels = [
        risk_labels.get(item, item) for item in risk_warning_codes
    ]

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

    # A price Trigger remains visible for lifecycle/position management. Known
    # execution failures are binding for *new* entries, while a missing depth
    # estimate is advisory because the fresh Bid/Ask and Spread are checked
    # independently. Never treat "not measured" as a zero-cost pass.
    # Keep binding reasons on the payload even while price location already
    # says WAIT/MISSED.  Otherwise an opposite/risk block disappears from the
    # plan contract until price happens to become ENTRY_READY, and cached
    # consumers can incorrectly regard the old plan as reusable for entry.
    hard_blockers: list[str] = [
        code
        for code in risk_warning_codes
        if code not in advisory_warning_codes
    ]
    if verdict_status == "ENTRY_READY" and hard_blockers:
        if set(hard_blockers).issubset(unavailable_warning_codes):
            verdict_status = "DATA_UNAVAILABLE"
            verdict_label = "執行資料不足｜禁止新進場"
        else:
            verdict_status = "HARD_GATE_BLOCKED"
            verdict_label = "風險條件未通過｜禁止新進場"
        verdict_reason = "；".join(risk_warning_labels[:3])

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
    closed_retest_pending = bool(
        eligibility.get("reentry_confirmation_required") is True
        and eligibility.get("closed_retest_confirmed") is not True
    )
    if invalidated:
        plan_status = "INVALIDATED"
    elif target_reached:
        plan_status = "TARGET_REACHED"
    elif verdict_status == "MISSED_ENTRY":
        plan_status = "MISSED"
    elif verdict_status == "WAIT_RETEST":
        plan_status = "WAITING_RETEST"
    elif verdict_status in {"HARD_GATE_BLOCKED", "DATA_UNAVAILABLE"}:
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
    plan_display = signal_plan_display_fields(signal)
    return {
        "inst_id": signal.inst_id,
        "trigger_id": signal.trigger_id,
        "horizon": signal.radar_horizon,
        "horizon_label": "4H 長線" if signal.radar_horizon == "LONG" else "15m 短線",
        "direction": signal.direction,
        "strategy": signal.strategy,
        "trigger_type": signal.trigger_type,
        "signal_stage": signal.signal_stage,
        **plan_display,
        "verdict": {
            "status": verdict_status,
            "situation": entry_situation,
            "label": verdict_label,
            "reason": verdict_reason,
            "actionable": verdict_status == "ENTRY_READY",
            "hard_blockers": hard_blockers,
            "risk_warnings": risk_warning_codes,
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
            "old_plan_reusable_for_new_entry": (
                not new_plan_required
                and not hard_blockers
                and not closed_retest_pending
            ),
            "existing_position_plan_active": lifecycle_status == "ACTIVE",
            "new_entry_status": (
                "READY"
                if verdict_status == "ENTRY_READY"
                else "WAIT"
                if verdict_status
                in {"WAIT_RETEST", "HARD_GATE_BLOCKED", "DATA_UNAVAILABLE"}
                else "CLOSED"
            ),
            "new_entry_allowed": verdict_status == "ENTRY_READY" and not hard_blockers,
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
                else "已出現正式反向價格訊號；舊計畫只保留供既有持倉依原始 SL／TP 管理，"
                "原方向禁止建立新倉。"
                if "OPPOSITE_SIGNAL" in hard_blockers
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
            "price_source": current_price_source,
            "ticker_last_price": round(market_last_price, 12),
            "quote_volume_24h_usdt": _round_or_none(live_quote_volume, 2),
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
            "reentry_confirmation_required": eligibility.get(
                "reentry_confirmation_required",
                False,
            ),
            "closed_retest_confirmed": eligibility.get(
                "closed_retest_confirmed",
                False,
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
            "buy_slippage_pct": _round_or_none(
                context.buy_slippage_pct if book_available else None,
                5,
            ),
            "sell_slippage_pct": _round_or_none(
                context.sell_slippage_pct if book_available else None,
                5,
            ),
            "estimated_round_trip_cost_pct": _round_or_none(
                raw_execution_cost,
                4,
            ),
            "execution_cost_to_risk_pct": _round_or_none(
                raw_cost_to_risk,
                1,
            ),
            "bid_depth_usd": _round_or_none(
                context.bid_depth_usd if book_available else None,
                2,
            ),
            "ask_depth_usd": _round_or_none(
                context.ask_depth_usd if book_available else None,
                2,
            ),
            "order_book_imbalance_pct": _round_or_none(
                (context.order_book_imbalance or 0.0) * 100.0
                if book_available and context.order_book_imbalance is not None
                else None,
                1,
            ),
            "execution_notional_usdt": context.execution_notional_usdt,
            "liquidity_policy": liquidity_policy,
        },
        "warnings": _unique(
            [*list(quality.get("warnings", [])), *risk_warning_labels]
        ),
        "data_quality": {
            "status": (
                "AVAILABLE"
                if execution_complete and live_quote_volume is not None
                else "PARTIAL"
            ),
            "ticker_available": True,
            "quote_volume_available": live_quote_volume is not None,
            "order_book_available": book_available,
            "execution_depth_complete": context.execution_quality_complete,
            "required_missing_sources": [
                *(
                    []
                    if live_quote_volume is not None
                    else ["ticker_quote_volume_24h"]
                ),
            ],
            "optional_missing_sources": [
                *([] if execution_complete else ["order_book_depth"]),
            ],
            "missing_sources": [
                *([] if execution_complete else ["order_book_depth"]),
                *([] if live_quote_volume is not None else ["ticker_quote_volume_24h"]),
            ],
        },
        "safety": {
            "analysis_only": True,
            "auto_ordering": False,
            "stored_trigger_unchanged": True,
            "entry_veto_enabled": True,
            "note": (
                "即時檢查不產生、刪除、改寫或隱藏核心 Trigger；"
                "必要條件失敗或未知會禁止新進場；Order Book 深度估算暫缺只提醒。"
                "舊計畫仍保留供既有持倉管理。"
            ),
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


def _preflight_liquidity_policy(
    signal: Signal,
    ticker: Ticker,
    config: PreflightConfig,
) -> dict[str, Any]:
    """Use the live ticker volume with only a trusted stored member state.

    A previous Universe member may use the 150 萬 exit line.  Missing,
    malformed, or policy-incompatible metadata is treated as a non-member and
    therefore needs the normal 200 萬 admission line.
    """

    configured_entry = _optional_number(
        getattr(config, "min_quote_volume_24h", 2_000_000.0)
    )
    entry_threshold = (
        configured_entry
        if configured_entry is not None and configured_entry >= 0
        else 2_000_000.0
    )
    configured_buffer = _optional_number(
        getattr(config, "quote_volume_buffer_24h", 500_000.0)
    )
    buffer = (
        0.0
        if entry_threshold == 0
        else min(
            entry_threshold,
            configured_buffer
            if configured_buffer is not None and configured_buffer >= 0
            else 500_000.0,
        )
    )
    exit_threshold = max(0.0, entry_threshold - buffer)
    raw = signal.data_quality.get("universe_volume_policy", {})
    raw = raw if isinstance(raw, dict) else {}
    stored_entry = _optional_number(raw.get("entry_usdt"))
    stored_exit = _optional_number(raw.get("exit_usdt"))
    stored_effective = _optional_number(raw.get("effective_min_usdt"))
    stored_member = raw.get("member") is True
    trusted = bool(
        raw.get("version") == 1
        and raw.get("trusted") is True
        and stored_entry is not None
        and stored_exit is not None
        and stored_effective is not None
        and math.isclose(stored_entry, entry_threshold, rel_tol=0.0, abs_tol=1e-9)
        and math.isclose(stored_exit, exit_threshold, rel_tol=0.0, abs_tol=1e-9)
        and math.isclose(
            stored_effective,
            exit_threshold if stored_member else entry_threshold,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    )
    member = bool(trusted and stored_member)
    volume = _optional_number(ticker.quote_volume_24h)
    if volume is not None and volume < 0:
        volume = None
    return {
        "version": 1,
        "trusted": trusted,
        "member": member,
        "entry_usdt": entry_threshold,
        "exit_usdt": exit_threshold,
        "effective_min_usdt": exit_threshold if member else entry_threshold,
        "volume_usdt": volume,
        "volume_status": "AVAILABLE" if volume is not None else "UNAVAILABLE",
        "source": "PREFLIGHT_TICKER",
    }


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
