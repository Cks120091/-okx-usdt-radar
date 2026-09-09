"""Read-only, bounded exit review. Frozen plan and entry permission never change.

Prices remain the original plan. Optional references use closed structure and
verified same-window flow; they are not exchange orders or calibrated forecasts.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

VERSION = "EXIT_REVIEW_V1"


def _map(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def review_exit_plan(plan: Any, flow: Any, *, current_price: Any = None,
                     now_ms: int, terminal: bool = False) -> dict[str, Any]:
    """Review existing plan, without changing a price, position or Signal.

    The protective reference can only tighten the original SL, after at least
    1R favourable movement, with room below/above live price and closed local
    structure. A snapshot wall or OI monetary value never sets a stop.
    """
    if hasattr(plan, "to_dict"):
        plan = plan.to_dict()
    plan, flow = _map(plan), _map(flow)
    management = _map(plan.get("management_plan"))
    result: dict[str, Any] = {
        "version": VERSION, "status": "INSUFFICIENT", "label": "依原計畫，等待完整資料",
        "reasons": [], "sources": [], "protective_stop_reference": None,
        "partial_take_profit_reference": None, "reference_only": True,
        "plan_unchanged": True, "auto_ordering": False,
        "note": "最新管理參考，不代表持倉指令；不改原 Entry／SL／TP，不放寬止損；若你已移動止損，不得因本頁參考而放寬。",
    }
    direction = str(plan.get("direction", ""))
    sign = 1 if direction == "LONG" else -1 if direction == "SHORT" else 0
    entry = _num(plan.get("plan_entry_price"))
    if entry is None:
        low, high = _num(plan.get("entry_low")), _num(plan.get("entry_high"))
        entry = (low + high) / 2 if low is not None and high is not None else None
    stop, tp1, tp2 = (_num(plan.get(k)) for k in ("stop_loss", "take_profit_1", "take_profit_2"))
    current = _num(current_price)
    if not sign or any(v is None or v <= 0 for v in (entry, stop, tp1, tp2)):
        result.update(status="NO_PLAN", label="尚無正式計畫，不產生止盈止損價")
        return result
    risk = sign * (entry - stop)
    if risk <= 0 or sign * (tp1 - entry) <= 0 or sign * (tp2 - tp1) <= 0:
        result.update(label="原計畫價位不完整，先核對資料")
        return result
    result["original"] = {"entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2}
    lifecycle = _map(plan.get("lifecycle"))
    terminal_values = {str(lifecycle.get(k, "")).upper() for k in ("outcome", "state", "status", "terminal_status")}
    terminal_values.update(str(plan.get(k, "")).upper() for k in ("freshness", "signal_stage"))
    if terminal or lifecycle.get("terminal") is True or terminal_values & {
        "SUPERSEDED", "STOP_LOSS", "TAKE_PROFIT", "CLOSED_UNKNOWN", "CLOSED",
        "INVALIDATED", "COMPLETED", "SL_FIRST", "TP1_FIRST", "TARGET_REACHED",
        "PREFLIGHT_STOP_CROSSED", "PREFLIGHT_TARGET_REACHED", "PRICE_INVALIDATED",
    }:
        result.update(status="CLOSED", label="舊計畫已結束，只保留紀錄")
        return result
    if current is None or current <= 0:
        result["reasons"] = ["缺少最新價格，不提供保護價。"]
        return result
    if sign * (current - stop) <= 0:
        result.update(status="STOP_REACHED", label="現價已越過原止損，不沿用舊計畫",
                      reasons=["只描述報價越線；不代表已在交易所成交。"])
        return result
    if sign * (current - tp1) >= 0:
        result.update(status="TARGET_REACHED", label="現價已到原第一目標，核對分批處理",
                      reasons=["不假設你已進場或已止盈成交。"])
        return result
    as_of = _num(flow.get("as_of_ms"))
    if (flow.get("schema_version") != "INTRADAY_FLOW_V1"
            or flow.get("source") != "OKX_CONTRACT_HISTORY"
            or flow.get("inst_id") != plan.get("inst_id")
            or as_of is None or not 0 <= now_ms - as_of <= 600_000):
        result["reasons"] = ["最新同幣數據不足或過期；保留原計畫，不猜測新價位。"]
        return result
    frame = "4H" if plan.get("radar_horizon") == "LONG" else "15m"
    row = _map(_map(flow.get("windows")).get(frame))
    p, o, c = (_map(row.get(k)) for k in ("price", "oi", "cvd"))
    move = _num(p.get("change_pct")) if p.get("status") == "OK" else None
    oi = _num(o.get("change_pct")) if o.get("status") == "OK" and o.get("unit") in {"contracts", "base"} else None
    imbalance = _num(c.get("imbalance_pct")) if c.get("status") == "OK" and c.get("volume_alignment") == "VERIFIED" else None
    expected_span = 14_400_000 if frame == "4H" else 900_000
    start, end = _num(row.get("start_ms")), _num(row.get("end_ms"))
    if start is None or end != as_of or end - start != expected_span or move is None:
        result["reasons"] = [f"{frame} 價格區間不完整；不使用不同時間的持倉或成交推算。"]
        return result
    result.update(as_of_ms=int(as_of), horizon=frame, status="OBSERVE", label="按原計畫，留意後續力道")
    result["sources"] = ["原始結構與波動計畫", f"{frame} 同窗價格"]
    if oi is not None:
        result["sources"].append("OI 原始數量")
    if imbalance is not None:
        result["sources"].append("主動成交（CVD 同源，只計一次）")
    adverse = sign * move < -.05
    flow_adverse = imbalance is not None and sign * imbalance < -10
    flow_aligned = imbalance is not None and sign * imbalance > 10
    if adverse:
        result.update(status="DEFENSIVE", label="最新價格反推，優先保護風險")
        result["reasons"].append(f"最近 {frame} 價格與本卡方向相反。")
    elif flow_adverse:
        result.update(status="CAUTION", label="主動成交反向，留意回吐")
        result["reasons"].append("成交力道與本卡相反；不是自動平倉指令。")
    elif flow_aligned and sign * move > .05 and oi is not None:
        result.update(status="FOLLOW", label="價格與成交配合，分批觀察原延伸目標")
        result["reasons"].append("價格有同向成果；仍不因資金數字上升而無限拉遠 TP。")
    elif imbalance is None or oi is None:
        result["reasons"].append("持倉或主動成交不完整；不補假數據，也不改原目標。")
    if oi is not None:
        result["reasons"].append("持倉增加，仍須由價格判斷主導方向。" if oi > .1 else "持倉減少，不等於行情一定結束。" if oi < -.1 else "持倉大致持平，不單獨構成多空理由。")
    obstacle = _num(management.get("structural_target_price"))
    if result["status"] in {"DEFENSIVE", "CAUTION"} and obstacle and sign * (obstacle - current) > 0 and sign * (tp1 - obstacle) > 0:
        result["partial_take_profit_reference"] = obstacle
        result["reasons"].append("前方近端結構可作分批處理參考；原 TP 不改寫。")
    gain_r = sign * (current - entry) / risk
    result["favourable_r"] = round(gain_r, 3)
    structure = _map(flow.get("closed_structure"))
    # Intraday structure is not allowed to set a swing-radar protective stop.
    if frame == "15m" and structure.get("as_of_ms") == as_of and gain_r >= 1:
        low, high, atr = (_num(structure.get(k)) for k in ("low_15m", "high_15m", "atr_5m"))
        tick = _num(plan.get("instrument_tick_size"))
        metrics = _map(plan.get("market_metrics"))
        tick = tick or _num(metrics.get("instrument_tick_size"))
        if low and high and atr and atr > 0 and tick and tick > 0:
            # Buffer avoids claiming an ordinary last-price touch is protected.
            buffer = max(atr * .25, tick * 2)
            candidate = low - buffer if sign == 1 else high + buffer
            candidate = (math.floor(candidate / tick) if sign == 1 else math.ceil(candidate / tick)) * tick
            if candidate > 0 and sign * (candidate - stop) > 0 and sign * (candidate - entry) >= 0 and sign * (current - candidate) >= buffer:
                result["protective_stop_reference"] = round(candidate, 12)
                result["sources"].append("已收線 15m 高低點＋5m 波動緩衝")
                result["reasons"].append("有至少 1R 順向空間，可核對更靠近的保護止損；不保證淨保本或成交價。")
    result["stop_basis"] = str(management.get("stop_method") or "原止損由結構失效位與波動空間決定")
    result["target_basis"] = str(management.get("target_method") or "原目標依前方結構與波動／力度決定")
    result["reasons"] = result["reasons"][:4]
    return result
