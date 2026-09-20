from __future__ import annotations

from . import _decision_core as _core
from .position_advisory import POSITION_CODES, POLICY_VERSION

CORE_SOURCE_SHA = "39a284fe4b377d5c7228812758240c8dba6b6971"

for _name in dir(_core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_core, _name)

_original_hard_gate = _core._hard_gate
_original_anomalies = _core._anomalies
_original_conflict_layer = _core._conflict_layer
_original_build_decision_context = _core.build_decision_context

# Risk/quality checks remain visible, but they no longer veto an otherwise
# valid formal signal. Location, chase and entry-window observations are
# advisory; invalidation, formal opposition and missing core/plan data bind.
_SOFT_GATE_KEYS = set(POSITION_CODES) | {
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

_ADVISORY_SAFETY_KEYS = set(POSITION_CODES) | {
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



def _timeframe_direction_alignment(item, direction):
    """Require the direction timeframe to agree with the formal Trigger.

    SHORT: completed 1H direction + 15m Trigger; 4H is background.
    LONG: completed 1D direction + 4H Trigger; 1H is timing context.
    The hidden fusion consolidates correlated EMA/RSI/MACD observations.
    """
    horizon = str(_core._read(item, "radar_horizon", "SHORT")).upper()
    if horizon not in {"SHORT", "LONG"} or direction not in {"LONG", "SHORT"}:
        return {"required": False, "passed": True, "state": "NOT_APPLICABLE"}

    direction_tf = "1H" if horizon == "SHORT" else "1D"
    trigger_tf = "15m" if horizon == "SHORT" else "4H"
    metrics = _core._mapping(_core._read(item, "market_metrics", {}))
    raw = _core._mapping(metrics.get("raw_indicators", {}))
    frame = _core._mapping(raw.get(direction_tf, {}))
    long_score = _core._number(frame.get("fusion_long_score"))
    if long_score is None or not 0.0 <= long_score <= 100.0:
        return {
            "required": True,
            # A present but corrupt direction frame is not a legacy omission.
            "passed": False if direction_tf in raw else None,
            "state": "UNKNOWN",
            "timeframe": direction_tf,
            "trigger_timeframe": trigger_tf,
            "reason": f"{direction_tf} 方向資料不足；最新掃描需重新取得方向確認。",
        }

    timeframe_direction = (
        "LONG" if long_score >= 55.0
        else "SHORT" if long_score <= 45.0
        else "NEUTRAL"
    )
    passed = timeframe_direction == direction
    return {
        "required": True,
        "passed": passed,
        "state": "ALIGNED" if passed else "NOT_ALIGNED",
        "timeframe": direction_tf,
        "trigger_timeframe": trigger_tf,
        "timeframe_direction": timeframe_direction,
        "trigger_direction": direction,
        "long_score": round(long_score, 1),
        "reason": (
            f"{direction_tf} {('偏多' if direction == 'LONG' else '偏空')}與 {trigger_tf} Trigger 同向。"
            if passed
            else f"{direction_tf} 目前為 {timeframe_direction}，未與 {trigger_tf} {direction} Trigger 同向。"
        ),
    }



def _swing_maturity(item, direction):
    """Reject a new entry when the current core leg is already mature."""
    if direction not in {"LONG", "SHORT"}:
        return {"required": False, "passed": True, "state": "NOT_APPLICABLE"}
    metrics = _core._mapping(_core._read(item, "market_metrics", {}))
    story = _core._mapping(_core._read(item, "market_story", {}))
    raw = _core._mapping(story.get("raw", {}))
    path = metrics.get("_core_path", [])
    atr = _core._number(raw.get("core_atr"))
    price = (_core._number(metrics.get("entry_execution_price"))
             or _core._number(metrics.get("last_price"))
             or _core._number(raw.get("core_close")))
    if atr is None or atr <= 0 or price is None or not isinstance(path, list):
        return {"required": True, "passed": None, "state": "UNKNOWN", "reason": "波段成熟度資料不足。"}
    rows = []
    for row in path[-20:]:
        if not isinstance(row, (list, tuple)) or len(row) != 4:
            continue
        high, low = _core._number(row[1]), _core._number(row[2])
        if high is not None and low is not None and high >= low > 0:
            rows.append((high, low))
    if len(rows) < 8:
        return {"required": True, "passed": None, "state": "UNKNOWN", "reason": "波段成熟度樣本不足。"}
    anchor = min(low for _, low in rows) if direction == "LONG" else max(high for high, _ in rows)
    extension_atr = (price - anchor) / atr if direction == "LONG" else (anchor - price) / atr
    trigger_type = str(_core._read(item, "trigger_type", "") or _core._mapping(story.get("trigger", {})).get("type", "")).upper()
    stage = str(_core._read(item, "signal_stage", "")).upper()
    fresh_retest = trigger_type in {"CONTINUATION", "REENTRY"} or stage == "REENTRY"
    limit = 4.0 if fresh_retest else 3.0
    passed = extension_atr <= limit
    side = "低點" if direction == "LONG" else "高點"
    suffix = "仍在可接受範圍。" if passed else "行情已走一段；追價風險較高，可等回踩／反彈再評估（不影響訊號）。"
    return {
        "required": True, "passed": passed,
        "state": "ACCEPTABLE" if passed else "MATURE",
        "extension_atr": round(extension_atr, 2), "limit_atr": limit,
        "anchor_price": anchor, "fresh_retest": fresh_retest,
        "reason": f"波段自近期{side}已走 {extension_atr:.2f} ATR，{suffix}",
    }

def _oi_resonance(item, direction):
    """Publish OI as a quality-confirmation layer, never as a standalone Trigger."""
    metrics = _core._mapping(_core._read(item, "market_metrics", {}))
    lookback = _core._mapping(metrics.get("continuation_lookback", {}))
    capital = _core._mapping(lookback.get("capital_flow", {}))
    detected = capital.get("detected") is True
    oi_direction = str(capital.get("headline_direction") or "NEUTRAL").upper()
    aligned = bool(detected and oi_direction == direction)
    opposite = bool(detected and oi_direction in {"LONG", "SHORT"} and oi_direction != direction)
    state = "RESONANCE" if aligned else "OPPOSITE" if opposite else "UNCONFIRMED"
    return {
        "state": state,
        "label": (
            "關鍵方向 OI 共振"
            if aligned
            else "OI 出現反向增倉證據"
            if opposite
            else "OI 尚待確認"
        ),
        "trigger_direction": direction,
        "oi_direction": oi_direction,
        "detected": detected,
        "strongest_window": capital.get("strongest_window"),
        "headline_label": capital.get("headline_label"),
        "role": "QUALITY_CONFIRMATION",
        "standalone_trigger": False,
    }

_core._hard_gate = _hard_gate
_core._anomalies = _anomalies
_core._conflict_layer = _conflict_layer

def build_decision_context(*args, **kwargs):
    # The retained builder resolves the patched globals at call time.  Normalize
    # only its presentation labels; Trigger and trade-plan geometry stay intact.
    payload = _original_build_decision_context(*args, **kwargs)
    item = kwargs.get("item") if "item" in kwargs else (args[0] if args else None)
    final = dict(payload.get("final", {}) or {})
    direction = str(final.get("direction") or "").upper()
    alignment = _timeframe_direction_alignment(item, direction) if item is not None else {"required": False, "passed": True}
    oi_resonance = _oi_resonance(item, direction) if item is not None else {"state": "UNCONFIRMED", "label": "OI 尚待確認"}
    maturity = _swing_maturity(item, direction) if item is not None else {"required": False, "passed": True}

    # Maturity is advisory only: expose late-leg risk without cancelling a
    # valid price Trigger.  The user still sees that the move has already run.
    final["swing_maturity"] = maturity
    if maturity.get("required") is True and maturity.get("passed") is False:
        warnings = list(final.get("risk_warnings", []) or [])
        warnings.append(str(maturity.get("reason") or "行情已走一段，追價風險較高。"))
        final["risk_warnings"] = _core._unique(warnings)[:3]

    # Direction/trigger contract: SHORT uses 1H -> 15m; LONG uses 1D -> 4H.
    # 4H is SHORT background; 1H is LONG timing context.
    if (
        str(final.get("status") or "").upper() == "ENTER"
        and alignment.get("required") is True
        and alignment.get("passed") is False
    ):
        direction_tf = str(alignment.get("timeframe") or "方向週期")
        trigger_tf = str(alignment.get("trigger_timeframe") or "Trigger")
        final.update({
            "status": "WAIT",
            "label": f"週期方向不同步｜等待 {direction_tf} × {trigger_tf} 同向",
            "new_entry_allowed": False,
            "wait_reason": {
                "code": "TIMEFRAME_DIRECTION_ALIGNMENT",
                "label": str(alignment.get("reason") or f"等待 {direction_tf} 與 {trigger_tf} Trigger 同向"),
            },
            "reasons": _core._unique([
                str(alignment.get("reason") or ""),
                *list(final.get("reasons", []) or []),
            ])[:3],
        })

    final["timeframe_alignment"] = alignment
    # Keep OI observer outside the canonical final decision object so enriching
    # advisory OI data cannot mutate the decision contract.  UI/API consumers
    # can read it from the top-level decision context.
    payload["oi_resonance"] = oi_resonance
    status = str(final.get("status") or "").upper()
    if status == "ENTER":
        final["label"] = (
            "訊號已觸發｜附風險建議"
            if list(final.get("risk_warnings", []) or [])
            or list(payload.get("hard_gate", {}).get("warnings", []) or [])
            else "訊號已觸發"
        )
    elif status == "HARD_GATE_BLOCKED":
        final["label"] = "必要條件未成立｜先更新確認"
        wait = dict(final.get("wait_reason", {}) or {})
        wait["label"] = "等待必要條件重新成立"
        final["wait_reason"] = wait
    elif status == "DATA_UNAVAILABLE":
        final["label"] = "必要資料不足｜先更新確認"
    payload["final"] = final
    final["signal_status"] = "TRIGGERED" if final.get("status") == "ENTER" else final.get("status")
    payload["policy"] = POLICY_VERSION
    payload["entry_policy_version"] = POLICY_VERSION
    return payload
