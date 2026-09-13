from __future__ import annotations

from . import _decision_core as _core

CORE_SOURCE_SHA = "de6cddf55c7c4b2cec7dcdeb7ef67a1e5673950e"

for _name in dir(_core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_core, _name)

_original_hard_gate = _core._hard_gate
_original_anomalies = _core._anomalies
_original_conflict_layer = _core._conflict_layer

# These remain visible risk/quality warnings, but no longer veto an otherwise
# valid Entry Zone.  Actual invalidation, opposite Trigger, liquidity, spread,
# measured slippage, severe chase and required data remain hard gates.
_SOFT_GATE_KEYS = {
    "RISK_REWARD",
    "STOP_LOSS",
    "EXECUTION_COST",
    "RR_INSUFFICIENT",
    "EXECUTION_COST_TOO_HIGH",
}


def _rebalance_hard_gate(payload):
    result = dict(payload or {})
    checks = [dict(row) for row in result.get("checks", [])]
    for row in checks:
        key = str(row.get("key") or "").strip().upper()
        if key not in _SOFT_GATE_KEYS:
            continue
        row["hard"] = False
        if row.get("status") in {"BLOCKED", "UNKNOWN"}:
            reason = str(row.get("reason") or "").strip()
            suffix = "僅列風險提醒，不單獨封鎖進場。"
            row["reason"] = f"{reason} {suffix}".strip()

    blocked = [
        row
        for row in checks
        if row.get("status") == "BLOCKED" and bool(row.get("hard", True))
    ]
    unknown = [
        row
        for row in checks
        if row.get("status") == "UNKNOWN" and bool(row.get("hard", True))
    ]
    advisory = [
        row
        for row in checks
        if row.get("status") in {"BLOCKED", "UNKNOWN"}
        and not bool(row.get("hard", True))
    ]
    status = "BLOCKED" if blocked else "UNKNOWN" if unknown else "PASSED"
    result.update(
        {
            "status": status,
            "passed": status == "PASSED",
            "blocked": status == "BLOCKED",
            "unknown": status == "UNKNOWN",
            "checks": checks,
            "blockers": [row.get("key") for row in blocked],
            "unknowns": [row.get("key") for row in unknown],
            "reasons": _core._unique(
                [str(row.get("reason") or "") for row in [*blocked, *unknown]]
            )[:6],
            "warnings": _core._unique(
                [
                    *list(result.get("warnings", []) or []),
                    *[str(row.get("reason") or "") for row in advisory],
                ]
            )[:8],
            "policy": "FUSION_BALANCED_V1",
        }
    )
    return result


def _hard_gate(*args, **kwargs):
    return _rebalance_hard_gate(_original_hard_gate(*args, **kwargs))


def _anomalies(item, metrics, story, safety_checks):
    anomalies = list(_original_anomalies(item, metrics, story, safety_checks))
    if str(_core._read(item, "regime", "")).upper() == "DISORDER":
        # DISORDER means price structure is messy, not that the market/data is
        # abnormal.  Keep it as confidence/context instead of an automatic veto.
        anomalies = [
            value for value in anomalies if value != "市場結構異常／失序"
        ]
    return anomalies


def _fusion_snapshot(item, direction):
    metrics = _core._mapping(_core._read(item, "market_metrics", {}))
    raw = _core._mapping(metrics.get("raw_indicators", {}))
    horizon = str(_core._read(item, "radar_horizon", "SHORT")).upper()
    key = "15m" if horizon == "SHORT" else "4H_TRIGGER"
    frame = _core._mapping(raw.get(key, {}))
    long_score = _core._number(frame.get("fusion_long_score"))
    if long_score is None:
        return None
    directional = (
        long_score
        if direction == "LONG"
        else 100.0 - long_score
        if direction == "SHORT"
        else 50.0
    )
    return {
        "timeframe": key,
        "directional_score": round(directional, 1),
        "long_score": round(long_score, 1),
        "trend_score": _core._number(frame.get("fusion_trend_score")),
        "retest_score": _core._number(frame.get("fusion_retest_score")),
        "momentum_score": _core._number(frame.get("fusion_momentum_score")),
        "role": "CORE_CONSOLIDATION",
    }


def _conflict_layer(item, direction, groups):
    result = dict(_original_conflict_layer(item, direction, groups))
    short_scope = str(_core._read(item, "radar_horizon", "SHORT")).upper() == "SHORT"
    fusion = _fusion_snapshot(item, direction)
    result["fusion_core"] = fusion
    if not short_scope or not fusion or not result.get("blocks_entry"):
        return result

    blocking = set(result.get("blocking_domains", []) or [])
    # The old trend/momentum domain bundled EMA/RSI/MACD observations that are
    # correlated.  The fused core now owns that family.  Unless the fused view
    # is itself clearly against the trade (<42 directional), do not let the old
    # duplicate trend vote combine with structure to form a hidden veto.
    if (
        "TREND_MOMENTUM" in blocking
        and float(fusion["directional_score"]) >= 42.0
    ):
        blocking.discard("TREND_MOMENTUM")
        other = blocking - {"POSITION_STRUCTURE", "TREND_MOMENTUM"}
        core_pair = {"POSITION_STRUCTURE", "TREND_MOMENTUM"}.issubset(blocking)
        result["blocking_domains"] = sorted(blocking)
        result["blocks_entry"] = bool(other or core_pair)
        result["fusion_core"] = {
            **fusion,
            "duplicate_trend_block_softened": True,
        }
    return result


_core._hard_gate = _hard_gate
_core._anomalies = _anomalies
_core._conflict_layer = _conflict_layer

# build_decision_context is defined in the retained module and resolves these
# globals at call time, so the patched policy applies without touching Trigger
# generation or any trade-plan geometry.
build_decision_context = _core.build_decision_context
