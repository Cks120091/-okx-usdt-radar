from __future__ import annotations

from . import _preflight_core as _core

CORE_SOURCE_SHA = "28236646fca9144da99a31c474889ce93631034d"

for _name in dir(_core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_core, _name)

_original_build_preflight_payload = _core.build_preflight_payload
_SOFT_CODES = {
    "ANOMALY",
    "ANOMALOUS_MARKET",
    "LIQUIDITY",
    "LIQUIDITY_TOO_LOW",
    "QUOTE_VOLUME_DATA_UNAVAILABLE",
    "SPREAD",
    "SPREAD_TOO_HIGH",
    "SLIPPAGE",
    "SLIPPAGE_TOO_HIGH",
    "EXECUTION_DATA_UNAVAILABLE",
    "DEEP_DATA_UNAVAILABLE",
    "RR_INSUFFICIENT",
    "EXECUTION_COST_TOO_HIGH",
    "RISK_REWARD",
    "EXECUTION_COST",
    "STOP_LOSS",
}
_ADVISORY_CODES = {
    "ANOMALY": "MARKET_ANOMALY_ADVISORY",
    "ANOMALOUS_MARKET": "MARKET_ANOMALY_ADVISORY",
    "LIQUIDITY": "LIQUIDITY_ADVISORY",
    "LIQUIDITY_TOO_LOW": "LIQUIDITY_ADVISORY",
    "QUOTE_VOLUME_DATA_UNAVAILABLE": "LIQUIDITY_DATA_ADVISORY",
    "SPREAD": "SPREAD_ADVISORY",
    "SPREAD_TOO_HIGH": "SPREAD_ADVISORY",
    "SLIPPAGE": "SLIPPAGE_ADVISORY",
    "SLIPPAGE_TOO_HIGH": "SLIPPAGE_ADVISORY",
    "EXECUTION_DATA_UNAVAILABLE": "EXECUTION_DATA_ADVISORY",
    "DEEP_DATA_UNAVAILABLE": "DEEP_DATA_ADVISORY",
    "RR_INSUFFICIENT": "RR_ADVISORY",
    "RISK_REWARD": "RR_ADVISORY",
    "EXECUTION_COST_TOO_HIGH": "EXECUTION_COST_ADVISORY",
    "EXECUTION_COST": "EXECUTION_COST_ADVISORY",
    "STOP_LOSS": "STOP_WIDTH_ADVISORY",
}
_ADVISORY_DATA_SOURCES = {
    "deep_data",
    "execution_depth",
    "funding",
    "open_interest",
    "order_book",
    "order_book_depth",
    "quote_volume",
    "quote_volume_24h",
    "taker",
    "ticker_quote_volume_24h",
}


def _explicit_anomaly(signal) -> bool:
    metrics = getattr(signal, "market_metrics", {}) or {}
    state = str(metrics.get("anomaly_state") or "").upper()
    return state in {"ANOMALY", "ABNORMAL", "BLOCKED", "ERROR"}


def build_preflight_payload(*args, **kwargs):
    payload = _original_build_preflight_payload(*args, **kwargs)
    signal = args[0] if args else kwargs.get("signal")
    verdict = dict(payload.get("verdict", {}) or {})
    lifecycle = dict(payload.get("signal_lifecycle", {}) or {})
    plan_state = dict(payload.get("plan_state", {}) or {})
    live = dict(payload.get("live", {}) or {})

    blockers = [str(code).strip().upper() for code in verdict.get("hard_blockers", [])]
    softened: list[str] = []
    retained: list[str] = []
    for code in blockers:
        soft = code in _SOFT_CODES
        # STOP_LOSS here means the decision-layer stop *width* check. A real
        # crossed stop has already changed lifecycle to INVALIDATED above.
        if code == "STOP_LOSS" and lifecycle.get("status") == "ACTIVE":
            soft = True
        if (
            code == "ANOMALY"
            and signal is not None
            and str(getattr(signal, "regime", "")).upper() == "DISORDER"
            and not _explicit_anomaly(signal)
        ):
            soft = True
        (softened if soft else retained).append(code)

    verdict["hard_blockers"] = retained
    verdict["advisory_blockers"] = softened
    original_warnings = [
        str(code).strip().upper()
        for code in verdict.get("risk_warnings", [])
        if str(code).strip()
    ]
    softened_set = set(softened)
    risk_warnings = [
        _ADVISORY_CODES.get(code, code) if code in softened_set else code
        for code in original_warnings
    ]
    risk_warnings.extend(_ADVISORY_CODES.get(code, code) for code in softened)
    verdict["risk_warnings"] = list(dict.fromkeys(risk_warnings))

    data_quality = dict(payload.get("data_quality", {}) or {})
    required_missing = [
        str(value).strip()
        for value in list(data_quality.get("required_missing_sources", []) or [])
        if str(value).strip()
    ]
    advisory_missing = [
        value
        for value in required_missing
        if value.lower() in _ADVISORY_DATA_SOURCES
    ]
    if data_quality.get("quote_volume_available") is False:
        advisory_missing.append("ticker_quote_volume_24h")
    if advisory_missing:
        advisory_set = set(advisory_missing)
        data_quality["required_missing_sources"] = [
            value for value in required_missing if value not in advisory_set
        ]
        data_quality["optional_missing_sources"] = list(
            dict.fromkeys(
                [
                    *list(data_quality.get("optional_missing_sources", []) or []),
                    *advisory_missing,
                ]
            )
        )
        data_quality["risk_data_advisory_only"] = True
        payload["data_quality"] = data_quality

    has_advisory_reason = bool(
        softened
        or any(code in _SOFT_CODES for code in original_warnings)
        or verdict.get("status") == "ANOMALY"
    )
    can_reopen = bool(
        verdict.get("status") in {"HARD_GATE_BLOCKED", "DATA_UNAVAILABLE", "ANOMALY"}
        and has_advisory_reason
        and not retained
        and lifecycle.get("status") == "ACTIVE"
        and not plan_state.get("new_trigger_required")
        and verdict.get("situation") == "IN_ENTRY_AREA"
    )
    if can_reopen:
        verdict.update({
            "status": "ENTRY_READY",
            "label": "掃描條件通過｜附風險建議",
            "reason": (
                "價格位於原 Entry Zone，且訊號與原計畫仍有效；"
                "流動性、Spread、滑價、R:R、SL 寬度、行情異常與交易成本"
                "僅列風險建議，不替使用者禁止進場。"
            ),
            "actionable": True,
            "new_entry_allowed": True,
        })

    confirmation_advisory = bool(
        verdict.get("status") == "ENTRY_READY"
        and live.get("reentry_confirmation_required") is True
        and live.get("closed_retest_confirmed") is not True
        and live.get("entry_ready_once") is not True
    )
    if confirmation_advisory:
        live["reentry_confirmation_advisory"] = True
        live["confirmation_role"] = "QUALITY_ONLY"
    else:
        live.setdefault("reentry_confirmation_advisory", False)

    if verdict.get("status") == "ENTRY_READY" and not retained:
        verdict["new_entry_allowed"] = True
        plan_state.update({
            "status": "ACTIVE",
            "old_plan_reusable_for_new_entry": True,
            "new_entry_status": "READY",
            "new_entry_allowed": True,
        })

    payload["verdict"] = verdict
    payload["signal_lifecycle"] = lifecycle
    payload["plan_state"] = plan_state
    payload["live"] = live
    payload["entry_policy_version"] = "ADVISORY_RISK_V1"
    return _separate_signal_and_position(payload, signal)


def _separate_signal_and_position(payload, signal):
    """One final preflight policy: live location describes cost, not validity.

    This runs after quote/SL/TP and execution checks. It only removes named
    location rules. True lifecycle, opposite, core-data and direction failures
    retain priority at every price, including far outside the Entry zone.
    """
    from .position_advisory import (ACTIVE_SIGNAL_STAGES, POLICY_VERSION, POSITION_CODES,
                                    mapping, number, position_advisory)
    from .decision import _timeframe_direction_alignment
    from . import _decision_core as decision_core

    result = dict(payload)
    verdict = dict(result.get("verdict", {}))
    plan = dict(result.get("plan_state", {}))
    lifecycle = dict(result.get("signal_lifecycle", {}))
    live = dict(result.get("live", {}))
    position = position_advisory(signal, price=live.get("price"), source=live.get("price_source"))
    position["legacy_position_status"] = verdict.get("status")
    position["legacy_situation"] = verdict.get("situation")
    position["chase_atr"], position["adverse_atr"] = number(live.get("chase_atr")), number(live.get("adverse_atr"))
    result["position_advisory"] = position
    result["entry_policy_version"] = POLICY_VERSION
    verdict["position_affects_signal"] = False

    stored_life = mapping(getattr(signal, "lifecycle", {}))
    stored_entry = mapping(getattr(signal, "entry_eligibility", {}))
    stored_terminal = decision_core._terminal_invalidation(signal, stored_life, stored_entry)
    stored_target = decision_core._target_completed(signal, stored_life, stored_entry)
    if lifecycle.get("terminal") is True or stored_terminal or stored_target:
        if stored_terminal and lifecycle.get("terminal") is not True:
            lifecycle.update(status="INVALIDATED", active=False, terminal=True)
            verdict.update(status="PLAN_INVALIDATED", label="訊號已失效", situation="INVALIDATED")
        elif stored_target and lifecycle.get("terminal") is not True:
            lifecycle.update(status="TARGET_REACHED", active=False, terminal=True)
            verdict.update(status="MISSED_ENTRY", label="目標已達｜計畫已結束", situation="TARGET_REACHED")
        verdict.update(actionable=False, new_entry_allowed=False, signal_status=lifecycle.get("status"))
        plan.update(new_entry_allowed=False, old_plan_reusable_for_new_entry=False, new_trigger_required=True)
        result.update(verdict=verdict, signal_lifecycle=lifecycle, plan_state=plan)
        return result

    blockers = [str(k).upper() for k in verdict.get("hard_blockers", []) if str(k).upper() not in POSITION_CODES]
    story = mapping(getattr(signal, "market_story", {}))
    trigger = mapping(story.get("trigger"))
    if signal.signal_stage not in ACTIVE_SIGNAL_STAGES or (trigger.get("triggered") is False and trigger.get("active_episode_preserved") is not True):
        blockers.append("NO_FORMAL_TRIGGER")
    direction = str(signal.direction).upper()
    if direction not in {"LONG", "SHORT"}:
        blockers.append("DIRECTION_UNAVAILABLE")
    alignment = _timeframe_direction_alignment(signal, direction)
    if alignment.get("passed") is False:
        blockers.append("TIMEFRAME_DIRECTION_ALIGNMENT")
    if trigger.get("new_entry_suspended") or trigger.get("opposite_warning_only"):
        blockers.append("OPPOSITE_SIGNAL")
    final = mapping(mapping(getattr(signal, "decision_context", {})).get("final"))
    wait_code = str(mapping(final.get("wait_reason")).get("code") or "").upper()
    if wait_code in {"EVIDENCE_CONFLICT", "TIMEFRAME_DIRECTION_ALIGNMENT"}:
        blockers.append(wait_code)
    dq = mapping(getattr(signal, "data_quality", {}))
    core_state = str(dq.get("core") or dq.get("core_status") or "").upper()
    if core_state and core_state not in {"AVAILABLE", "COMPLETE", "COMPLETED", "FRESH", "OK"}:
        blockers.append("CORE_DATA_UNAVAILABLE")
    if dq.get("closed_candle") is False:
        blockers.append("CORE_CANDLE_UNCONFIRMED")
    # Missing optional execution/flow inputs are still only advisory.
    if any(str(k).lower() not in _ADVISORY_DATA_SOURCES | {"publication_ticker"}
           for k in dq.get("required_missing_sources", [])):
        blockers.append("CORE_DATA_UNAVAILABLE")
    blockers = list(dict.fromkeys(blockers))
    permitted = not blockers
    if permitted:
        verdict.update(status="ENTRY_READY", label="訊號已觸發", reason=position["note"],
                       signal_status="TRIGGERED", actionable=True, new_entry_allowed=True)
        plan.update(status="ACTIVE", old_plan_reusable=True, old_plan_reusable_for_new_entry=True,
                    new_entry_status="READY", new_entry_allowed=True, new_trigger_required=False,
                    direction_still_valid=True, note="訊號與原計畫仍有效；可進位置只供參考，不是放行條件。")
        lifecycle["note"] = "訊號已觸發；目前價格位置只作提示，不取消訊號。"
    else:
        unavailable = any("DATA" in k or "UNAVAILABLE" in k or "UNCONFIRMED" in k for k in blockers)
        verdict.update(status="DATA_UNAVAILABLE" if unavailable else "HARD_GATE_BLOCKED",
                       label="核心資料待更新" if unavailable else "核心訊號條件未成立",
                       reason=alignment.get("reason") if "TIMEFRAME_DIRECTION_ALIGNMENT" in blockers else "已出現正式反向訊號，原方向暫停新進場。" if "OPPOSITE_SIGNAL" in blockers else "核心條件：" + "、".join(blockers),
                       signal_status="UNCONFIRMED", actionable=False, new_entry_allowed=False)
        plan.update(status="ACTIVE_ENTRY_BLOCKED", new_entry_status="WAIT", new_entry_allowed=False,
                    old_plan_reusable_for_new_entry=False)
    verdict["hard_blockers"] = blockers
    live["reentry_confirmation_advisory"] = bool(live.get("reentry_confirmation_required"))
    result.update(verdict=verdict, signal_lifecycle=lifecycle, plan_state=plan, live=live,
                  timeframe_alignment=alignment)
    return result
