from __future__ import annotations

from . import _preflight_core as _core

CORE_SOURCE_SHA = "d31cc6d6a43c78ed6689c59f53edc4884270f9d4"

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
    from .preflight_position import apply_position_policy
    return apply_position_policy(payload, signal)
