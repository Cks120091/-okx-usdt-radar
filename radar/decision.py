from __future__ import annotations

from . import _decision_core as _core

CORE_SOURCE_SHA = "de6cddf55c7c4b2cec7dcdeb7ef67a1e5673950e"

for _name in dir(_core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_core, _name)

_original_hard_gate = _core._hard_gate
_original_anomalies = _core._anomalies
_original_conflict_layer = _core._conflict_layer
_original_build_decision_context = _core.build_decision_context

# Risk/quality checks remain visible, but they no longer veto an otherwise
# valid Entry Zone.  Only the price contract itself remains binding here:
# invalidation, a formal opposite Trigger, missing required core/live-price
# data, a missing trade plan, or an Entry Window that is not open.
_SOFT_GATE_KEYS = {
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
    "SAFETY_INTEGRITY",
    "RISK_REWARD",
    "STOP_LOSS",
    "EXECUTION_COST",
    "RR_INSUFFICIENT",
    "EXECUTION_COST_TOO_HIGH",
}

_ADVISORY_SAFETY_KEYS = {
    "ANOMALY",
    "ANOMALOUS_MARKET",
    "LIQUIDITY",
    "LIQUIDITY_TOO_LOW",
    "UNIVERSE_LIQUIDITY",
    "UNIVERSE_SPREAD",
    "SPREAD",
    "SPREAD_TOO_HIGH",
    "SLIPPAGE",
    "SLIPPAGE_TOO_HIGH",
    "EXECUTION_DEPTH",
    "EXECUTION_COST",
    "EXECUTION_COST_TOO_HIGH",
    "RISK_REWARD",
    "RR_INSUFFICIENT",
    "STOP_LOSS",
    "DEEP_DATA_AVAILABLE",
    "CONTEXT_DATA",
    "OPEN_INTEREST",
}


def _rebalance_hard_gate(payload):
    result = dict(payload or {})
    checks = [dict(row) for row in result.get("checks", [])]
    softened_original_reasons: set[str] = set()
    for row in checks:
        key = str(row.get("key") or "").strip().upper()
        safety_values = {
            str(value).strip().upper()
            for value in list(row.get("value") or [])
            if str(value).strip()
        } if key == "SAFETY_CHECKS" and isinstance(row.get("value"), list) else set()
        advisory_safety_group = bool(
            key == "SAFETY_CHECKS"
            and safety_values
            and safety_values.issubset(_ADVISORY_SAFETY_KEYS)
        )
        if key == "STOP_LOSS" and row.get("status") == "UNKNOWN":
            # A known-but-wide stop is advice.  No usable Entry/SL geometry is
            # a missing formal plan, so it must remain a necessary condition.
            continue
        if key not in _SOFT_GATE_KEYS and not advisory_safety_group:
            continue
        row["hard"] = False
        if row.get("status") in {"BLOCKED", "UNKNOWN"}:
            reason = str(row.get("reason") or "").strip()
            if reason:
                softened_original_reasons.add(reason)
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
                    *[
                        str(reason)
                        for reason in list(result.get("warnings", []) or [])
                        if str(reason) not in softened_original_reasons
                    ],
                    *[str(row.get("reason") or "") for row in advisory],
                ]
            )[:8],
            "policy": "ADVISORY_RISK_V1",
            "risk_checks_advisory_only": True,
        }
    )
    return result


def _hard_gate(*args, **kwargs):
    item = kwargs.get("item")
    if item is not None and kwargs.get("plan_present"):
        formal_plan_present = all(
            _core._number(_core._read(item, key, None)) is not None
            for key in ("entry_low", "entry_high", "stop_loss", "take_profit_1")
        )
        if not formal_plan_present:
            kwargs = {**kwargs, "plan_present": False}
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

def build_decision_context(*args, **kwargs):
    # The retained builder resolves the patched globals at call time.  Normalize
    # only its presentation labels; Trigger and trade-plan geometry stay intact.
    payload = _original_build_decision_context(*args, **kwargs)
    final = dict(payload.get("final", {}) or {})
    status = str(final.get("status") or "").upper()
    if status == "ENTER":
        final["label"] = (
            "掃描條件通過｜附風險建議"
            if list(final.get("risk_warnings", []) or [])
            or list(payload.get("hard_gate", {}).get("warnings", []) or [])
            else "掃描條件通過"
        )
    elif status == "HARD_GATE_BLOCKED":
        final["label"] = "必要條件未成立｜先更新確認"
        wait = dict(final.get("wait_reason", {}) or {})
        wait["label"] = "等待必要條件重新成立"
        final["wait_reason"] = wait
    elif status == "DATA_UNAVAILABLE":
        final["label"] = "必要資料不足｜先更新確認"
    payload["final"] = final
    payload["policy"] = "ADVISORY_RISK_V1"
    return payload
