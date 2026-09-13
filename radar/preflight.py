from __future__ import annotations

from . import _preflight_core as _core

CORE_SOURCE_SHA = "d31cc6d6a43c78ed6689c59f53edc4884270f9d4"

for _name in dir(_core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_core, _name)

_original_build_preflight_payload = _core.build_preflight_payload
_SOFT_CODES = {
    "RR_INSUFFICIENT",
    "EXECUTION_COST_TOO_HIGH",
    "RISK_REWARD",
    "EXECUTION_COST",
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
        # STOP_LOSS here is the old decision-layer stop-distance check, not an
        # actual crossed stop.  A real crossed stop has already made lifecycle
        # INVALIDATED before this post-processing step.
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
    if softened:
        verdict["risk_warnings"] = list(
            dict.fromkeys([*verdict.get("risk_warnings", []), *softened])
        )

    can_reopen = bool(
        verdict.get("status") == "HARD_GATE_BLOCKED"
        and not retained
        and lifecycle.get("status") == "ACTIVE"
        and not plan_state.get("new_trigger_required")
        and verdict.get("situation") == "IN_ENTRY_AREA"
    )
    if can_reopen:
        verdict.update(
            {
                "status": "ENTRY_READY",
                "label": "目前可進｜附風險提醒",
                "reason": (
                    "價格位於原 Entry Zone，必要成交與失效條件未阻擋；"
                    "R:R／SL 寬度／交易成本占風險改列提醒，不再單獨封鎖。"
                ),
                "actionable": True,
            }
        )

    confirmation_advisory = bool(
        verdict.get("status") == "ENTRY_READY"
        and live.get("reentry_confirmation_required") is True
        and live.get("closed_retest_confirmed") is not True
    )
    if confirmation_advisory:
        live["reentry_confirmation_advisory"] = True
        live["confirmation_role"] = "QUALITY_ONLY"
    else:
        live.setdefault("reentry_confirmation_advisory", False)

    if verdict.get("status") == "ENTRY_READY" and not retained:
        plan_state.update(
            {
                "status": "ACTIVE",
                "old_plan_reusable_for_new_entry": True,
                "new_entry_status": "READY",
                "new_entry_allowed": True,
            }
        )

    payload["verdict"] = verdict
    payload["signal_lifecycle"] = lifecycle
    payload["plan_state"] = plan_state
    payload["live"] = live
    payload["entry_policy_version"] = "FUSION_BALANCED_V1"
    return payload
