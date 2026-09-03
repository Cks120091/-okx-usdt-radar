from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


DEFAULT_THRESHOLDS: dict[str, float] = {
    "min_quote_volume_24h": 5_000_000.0,
    "max_spread_pct": 0.10,
    "max_slippage_pct": 0.15,
    "execution_cost_warning_to_risk_pct": 10.0,
    "max_execution_cost_to_risk_pct": 15.0,
    "minimum_rr": 1.8,
    "max_stop_pct": 5.0,
    "severe_entry_extension_atr": 1.8,
}

_AVAILABLE = {"AVAILABLE", "COMPLETE", "COMPLETED", "FRESH", "OK"}
_UNAVAILABLE = {
    "MISSING",
    "PARTIAL",
    "PENDING",
    "UNAVAILABLE",
    "STALE",
    "ERROR",
    "FAILED",
    "UNKNOWN",
}
_FORMAL_STAGES = {"EARLY_SIGNAL", "REENTRY", "CONFIRMED"}


def build_decision_context(
    item: Any,
    thresholds: Mapping[str, Any] | Any | None = None,
) -> dict[str, Any]:
    """Build one price-first trading decision from a radar item.

    The function is deliberately pure: it does not mutate ``item``, persist a
    Signal Episode, fetch market data, or create/cancel a Trigger.  Execution
    and market-risk checks are advisory; they never cancel or hide a formal
    price Trigger.
    """

    limits = {
        key: _threshold(thresholds, key, default)
        for key, default in DEFAULT_THRESHOLDS.items()
    }
    direction = _direction(item)
    metrics = _mapping(_read(item, "market_metrics", {}))
    story = _mapping(_read(item, "market_story", {}))
    trigger = _mapping(
        _read(item, "trigger", None)
        or story.get("trigger")
        or {}
    )
    lifecycle = _mapping(_read(item, "lifecycle", {}))
    entry = _mapping(_read(item, "entry_eligibility", {}))
    execution = _mapping(_read(item, "execution_quality", {}))
    data_quality = _mapping(_read(item, "data_quality", {}))
    groups = _mapping(_read(item, "evidence_groups", {}))
    safety_checks = _safety_checks(_read(item, "safety_checks", []))

    target_completed = _target_completed(item, lifecycle, entry)
    terminal_invalidation = _terminal_invalidation(item, lifecycle, entry)
    anomalies = _anomalies(item, metrics, story, safety_checks)
    anomaly_warnings = _anomaly_warnings(item, metrics, story)
    plan_present = _plan_present(item, trigger)

    hard_gate = _hard_gate(
        item=item,
        limits=limits,
        metrics=metrics,
        trigger=trigger,
        lifecycle=lifecycle,
        entry=entry,
        execution=execution,
        data_quality=data_quality,
        safety_checks=safety_checks,
        plan_present=plan_present,
        terminal_invalidation=terminal_invalidation,
        anomalies=anomalies,
    )
    evidence = _evidence_layer(item, direction, groups)
    market_context = _market_context_layer(
        item,
        direction,
        metrics,
        story,
        trigger,
        anomalies,
        anomaly_warnings,
    )
    conflict = _conflict_layer(item, direction, groups)
    continuation_confirmation = _continuation_confirmation_layer(
        item=item,
        direction=direction,
        metrics=metrics,
        story=story,
        trigger=trigger,
        groups=groups,
        terminal_invalidation=terminal_invalidation,
        target_completed=target_completed,
    )
    quality = _quality_layer(evidence, execution)
    confidence = _confidence_layer(
        direction,
        evidence,
        conflict,
        hard_gate,
        data_quality,
        anomaly_warnings,
    )
    episode = _episode_layer(
        item,
        lifecycle,
        terminal_invalidation=terminal_invalidation,
        target_completed=target_completed,
    )
    final = _final_layer(
        item=item,
        direction=direction,
        plan_present=plan_present,
        hard_gate=hard_gate,
        evidence=evidence,
        market_context=market_context,
        conflict=conflict,
        quality=quality,
        confidence=confidence,
        episode=episode,
        entry=entry,
        terminal_invalidation=terminal_invalidation,
        target_completed=target_completed,
        anomaly_warnings=anomaly_warnings,
    )

    return {
        "schema_version": "1.0",
        "hard_gate": hard_gate,
        "evidence": evidence,
        "market_context": market_context,
        "conflict": conflict,
        "continuation_confirmation": continuation_confirmation,
        "quality": quality,
        "confidence": confidence,
        "episode": episode,
        "final": final,
    }


def _hard_gate(
    *,
    item: Any,
    limits: dict[str, float],
    metrics: dict[str, Any],
    trigger: dict[str, Any],
    lifecycle: dict[str, Any],
    entry: dict[str, Any],
    execution: dict[str, Any],
    data_quality: dict[str, Any],
    safety_checks: list[dict[str, Any]],
    plan_present: bool,
    terminal_invalidation: bool,
    anomalies: list[str],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    warnings = _unique(
        [
            str(row.get("label") or row.get("key") or "執行條件提醒")
            for row in safety_checks
            if row.get("hard") is False
            and row.get("passed") is False
            and str(row.get("key") or "") != "execution_cost_warning"
        ]
    )

    _add_check(
        checks,
        "plan_invalidation",
        "原交易計畫未失效",
        "BLOCKED" if terminal_invalidation else "PASSED",
        not terminal_invalidation,
        "原交易計畫已越過 SL／Invalidation，禁止復活。"
        if terminal_invalidation
        else "原交易計畫未被判定失效。",
    )
    _add_check(
        checks,
        "anomaly",
        "市場未處於異常行情",
        "BLOCKED" if anomalies else "PASSED",
        list(anomalies),
        "偵測到異常行情，等待市場穩定。" if anomalies else "未標記異常行情。",
    )

    core_status, deep_status, missing_sources = _data_states(data_quality)
    data_unknown = (
        core_status not in _AVAILABLE
        or deep_status not in _AVAILABLE
        or bool(missing_sources)
    )
    _add_check(
        checks,
        "data_quality",
        "核心與執行資料完整",
        "UNKNOWN" if data_unknown else "PASSED",
        {
            "core": core_status,
            "deep": deep_status,
            "missing_sources": missing_sources,
        },
        "資料缺失／部分／待更新，不能假裝成最新資料。"
        if data_unknown
        else "核心與執行資料可用。",
    )

    if not safety_checks:
        _add_check(
            checks,
            "safety_integrity",
            "風險提醒資料可核對",
            "UNKNOWN",
            None,
            "缺少風險提醒資料；標記為未知，但不取消正式價格 Trigger。",
        )
    else:
        hard = [row for row in safety_checks if bool(row.get("hard", True))]
        unknown_safety = [row for row in hard if row.get("passed") is None]
        failed_safety = [row for row in hard if row.get("passed") is False]
        _add_check(
            checks,
            "safety_checks",
            "既有風險檢查結果",
            (
                "BLOCKED"
                if failed_safety
                else "UNKNOWN"
                if unknown_safety or not hard
                else "PASSED"
            ),
            [str(row.get("key") or row.get("label") or "unknown") for row in failed_safety],
            (
                "既有風險檢查出現提醒。"
                if failed_safety
                else "風險檢查資料不完整。"
                if unknown_safety or not hard
                else "既有風險檢查未發現提醒。"
            ),
        )

    upstream_blockers = _unique(
        [str(value).strip().upper() for value in _strings(entry.get("hard_blockers"))]
    )
    for blocker in upstream_blockers:
        _add_check(
            checks,
            blocker,
            f"上游風險提醒：{blocker}",
            "BLOCKED",
            False,
            f"相容舊資料的上游風險標記：{blocker}；只作提醒。",
        )

    explicit_entry_permission = (
        entry.get("new_entry_allowed")
        if "new_entry_allowed" in entry
        else _read(item, "new_entry_allowed", None)
    )
    if explicit_entry_permission is False:
        _add_check(
            checks,
            "entry_permission",
            "上游目前位置允許新進場",
            "BLOCKED",
            False,
            "上游目前位置判定為不可進；保留作位置狀態，不視為風險一票否決。",
        )

    quote_volume = _number(_read(item, "quote_volume_24h", None))
    _numeric_limit_check(
        checks,
        key="liquidity",
        label="24H 成交額符合流動性建議值",
        value=quote_volume,
        limit=limits["min_quote_volume_24h"],
        comparison="MIN",
        missing_reason="缺少 24H 成交額，流動性未知。",
        blocked_reason="24H 成交額低於流動性建議值。",
    )
    spread = _number(
        _read(item, "spread_pct", None)
        if _read(item, "spread_pct", None) is not None
        else metrics.get("spread_pct")
    )
    _numeric_limit_check(
        checks,
        key="spread",
        label="Spread（買賣價差）未超過建議值",
        value=spread,
        limit=limits["max_spread_pct"],
        comparison="MAX",
        missing_reason="缺少 Spread，執行風險未知。",
        blocked_reason="Spread 超過建議值。",
    )

    buy_slippage = _number(metrics.get("buy_slippage_pct"))
    sell_slippage = _number(metrics.get("sell_slippage_pct"))
    slippage = (
        max(buy_slippage, sell_slippage)
        if buy_slippage is not None and sell_slippage is not None
        else None
    )
    _numeric_limit_check(
        checks,
        key="slippage",
        label="Slippage（滑價）未超過建議值",
        value=slippage,
        limit=limits["max_slippage_pct"],
        comparison="MAX",
        missing_reason="缺少完整買入／賣出滑價，執行風險未知。",
        blocked_reason="估算滑價超過建議值。",
    )

    cost_to_risk = _number(
        metrics.get("execution_cost_to_risk_pct")
        if metrics.get("execution_cost_to_risk_pct") is not None
        else execution.get("execution_cost_to_risk_pct")
    )
    _numeric_limit_check(
        checks,
        key="execution_cost",
        label="交易成本占原始風險未超過建議值",
        value=cost_to_risk,
        limit=limits["max_execution_cost_to_risk_pct"],
        comparison="MAX",
        missing_reason="缺少交易成本占風險資料；顯示未知提醒。",
        blocked_reason="交易成本占原始風險高於建議值。",
    )
    warning_limit = min(
        limits["execution_cost_warning_to_risk_pct"],
        limits["max_execution_cost_to_risk_pct"],
    )
    if (
        cost_to_risk is not None
        and warning_limit < cost_to_risk <= limits["max_execution_cost_to_risk_pct"]
    ):
        warnings.append(
            "Execution Cost（交易成本）占原始風險 "
            f"{cost_to_risk:.1f}%，高於 {warning_limit:.1f}% 建議線，"
            f"但仍低於 {limits['max_execution_cost_to_risk_pct']:.1f}% 建議上限。"
        )

    if plan_present:
        rr = _number(
            entry.get("remaining_rr")
            if entry.get("remaining_rr_applicable") is not False
            and entry.get("remaining_rr") is not None
            else _read(item, "risk_reward", None)
        )
        _numeric_limit_check(
            checks,
            key="risk_reward",
            label="R:R（風險報酬比）符合建議值",
            value=rr,
            limit=limits["minimum_rr"],
            comparison="MIN",
            missing_reason="缺少可用 R:R；顯示未知提醒。",
            blocked_reason="剩餘或原始 R:R 低於建議值。",
        )
        stop_pct = _stop_pct(item, metrics)
        _numeric_limit_check(
            checks,
            key="stop_loss",
            label="SL（止損）距離符合建議值",
            value=stop_pct,
            limit=limits["max_stop_pct"],
            comparison="POSITIVE_MAX",
            missing_reason="缺少可核對的 Entry／SL；顯示未知提醒。",
            blocked_reason="SL 距離無效或超過建議範圍。",
        )
    else:
        _add_check(
            checks,
            "trade_plan",
            "已有正式交易計畫",
            "BLOCKED",
            False,
            "目前尚未形成包含 Entry／SL／TP 的正式交易計畫。",
        )

    # The live Entry eligibility is the canonical execution-distance source.
    # ``entry_quality`` describes the setup's original EMA/structure extension
    # and can legitimately be stale after price returns to the immutable Entry
    # zone.  Mixing the two used to produce an impossible result such as
    # ``ENTRY_READY`` / ``0.00 ATR`` while the hidden historical quality key
    # independently blocked the trade as severe chase.
    quality = _mapping(_read(item, "entry_quality", {}))
    eligibility_chase_atr = _number(entry.get("chase_atr"))
    metrics_chase_atr = _number(metrics.get("entry_chase_atr"))
    quality_extension_atr = _number(quality.get("extension_atr"))
    if eligibility_chase_atr is not None:
        chase_source = "entry_eligibility.chase_atr"
        chase_atr = eligibility_chase_atr
        live_chase_available = True
    elif metrics_chase_atr is not None:
        chase_source = "market_metrics.entry_chase_atr"
        chase_atr = metrics_chase_atr
        live_chase_available = True
    else:
        chase_source = "live_chase_unavailable"
        chase_atr = None
        live_chase_available = False
    entry_status = str(entry.get("status", "")).upper()
    entry_label = str(entry.get("label", ""))
    missed_for_chase = entry_status == "MISSED_ENTRY" and any(
        token in entry_label for token in ("追價", "離開最佳")
    )
    if live_chase_available:
        severe_live_chase = bool(
            chase_atr is not None
            and chase_atr > limits["severe_entry_extension_atr"]
        )
        chase_status = "BLOCKED" if severe_live_chase else "PASSED"
    elif (
        quality_extension_atr is not None
        and quality_extension_atr > limits["severe_entry_extension_atr"]
    ):
        # A legacy setup extension is never sufficient to grant permission.
        # It may only conservatively block when its numeric value is already
        # beyond the same severe threshold.
        chase_source = "entry_quality.extension_atr"
        chase_atr = quality_extension_atr
        chase_status = "BLOCKED"
    elif missed_for_chase:
        chase_source = "entry_eligibility.status"
        chase_status = "BLOCKED"
    else:
        chase_status = "UNKNOWN"
    chase_value = {
        "source": chase_source,
        "chase_atr": round(chase_atr, 6) if chase_atr is not None else None,
        "threshold_atr": round(limits["severe_entry_extension_atr"], 6),
        "entry_status": entry_status or "UNKNOWN",
        "entry_quality_key": str(quality.get("key") or "").upper() or None,
        "entry_quality_extension_atr": (
            round(quality_extension_atr, 6)
            if quality_extension_atr is not None
            else None
        ),
    }
    if chase_status == "BLOCKED" and chase_atr is not None:
        chase_reason = (
            f"價格偏離 {chase_atr:.2f} ATR，超過延伸提醒值 "
            f"{limits['severe_entry_extension_atr']:.2f} ATR"
            f"（來源：{chase_source}）。"
        )
    elif chase_status == "BLOCKED":
        chase_reason = "即時進場狀態已標記禁止追價（來源：entry_eligibility.status）。"
    elif chase_status == "PASSED" and chase_atr is not None:
        chase_reason = (
            f"價格偏離 {chase_atr:.2f} ATR，未達延伸提醒值 "
            f"{limits['severe_entry_extension_atr']:.2f} ATR"
            f"（來源：{chase_source}）。"
        )
    else:
        chase_reason = (
            "未取得本輪 live 追價偏離值；舊 setup 的延伸資料不能作為"
            "目前可進的放行依據。"
        )
    _add_check(
        checks,
        "chase",
        "價格未構成嚴重追價",
        chase_status,
        chase_value,
        chase_reason,
    )

    blocked = [row for row in checks if row["status"] == "BLOCKED"]
    unknown = [row for row in checks if row["status"] == "UNKNOWN"]
    status = "BLOCKED" if blocked else "UNKNOWN" if unknown else "PASSED"
    return {
        "status": status,
        "passed": status == "PASSED",
        "blocked": status == "BLOCKED",
        "unknown": status == "UNKNOWN",
        "checks": checks,
        "blockers": [row["key"] for row in blocked],
        "unknowns": [row["key"] for row in unknown],
        "reasons": _unique([row["reason"] for row in [*blocked, *unknown]])[:6],
        "warnings": _unique(
            [*warnings, *[row["reason"] for row in [*blocked, *unknown]]]
        )[:8],
        "thresholds": dict(limits),
        "trigger_preserved": True,
        "advisory_only": True,
        "entry_veto_enabled": False,
    }


def _evidence_layer(
    item: Any,
    direction: str,
    groups: dict[str, Any],
) -> dict[str, Any]:
    position = _mapping(groups.get("position_structure", {}))
    trend = _mapping(groups.get("trend_momentum", {}))
    participation_group = _mapping(groups.get("participation_flow", {}))
    direction_scores = [
        score
        for score in (_number(position.get("score")), _number(trend.get("score")))
        if score is not None
    ]
    fallback = _number(_read(item, "readiness_score", None))
    direction_score = (
        round(sum(direction_scores) / len(direction_scores), 1)
        if direction_scores
        else fallback
    )
    direction_quality = _quality_value(direction_score)

    participation = _mapping(_read(item, "market_participation", {}))
    participation_score = _number(participation_group.get("score"))
    participation_state = str(
        participation.get("state")
        or participation_group.get("stance")
        or "UNKNOWN"
    ).upper()
    participation_quality = _quality_value(participation_score)
    participation_quality.update(
        {
            "state": participation_state,
            "label": str(
                participation.get("label")
                or participation_group.get("label")
                or participation_quality["label"]
            ),
        }
    )
    supporting = _strings(_read(item, "supporting_evidence", []))
    if not supporting:
        supporting = _strings(_read(item, "evidence", []))
    return {
        "main_direction": direction,
        "direction_quality": direction_quality,
        "participation": participation_quality,
        "groups": {
            key: _compact_group(value)
            for key, value in groups.items()
            if isinstance(value, Mapping)
        },
        "supporting": _unique(supporting)[:4],
    }


def _market_context_layer(
    item: Any,
    direction: str,
    metrics: dict[str, Any],
    story: dict[str, Any],
    trigger: dict[str, Any],
    anomalies: list[str],
    anomaly_warnings: list[str],
) -> dict[str, Any]:
    raw = _mapping(story.get("raw", {}))
    explicit = {
        **_mapping(story.get("context", {})),
        **_mapping(_read(item, "market_context", {})),
    }
    regime_value = (
        explicit.get("regime")
        or _read(item, "regime", None)
        or metrics.get("market_regime")
        or "UNKNOWN"
    )
    regime = str(
        _mapping(regime_value).get("key")
        or _mapping(regime_value).get("state")
        or regime_value
    ).upper()
    trigger_type = str(
        _read(item, "trigger_type", None)
        or trigger.get("type")
        or "NONE"
    ).upper()
    phase_value = (
        explicit.get("phase")
        or metrics.get("market_phase")
        or raw.get("market_phase")
        or _phase(regime, trigger_type, str(_stage(item)))
    )
    phase = str(
        _mapping(phase_value).get("key")
        or _mapping(phase_value).get("state")
        or phase_value
    ).upper()
    driver = _context_value(
        explicit.get("driver") or explicit.get("market_driver"),
        metrics.get("market_driver"),
        raw.get("market_driver"),
    )
    relative_strength = _context_value(
        explicit.get("relative_strength"),
        metrics.get("relative_strength"),
        raw.get("relative_strength"),
    )
    resonance = _context_value(
        explicit.get("resonance"),
        metrics.get("market_resonance"),
        metrics.get("resonance"),
        raw.get("market_resonance"),
    )
    session_source = explicit.get("sessions")
    if isinstance(session_source, Mapping) and isinstance(
        session_source.get("items"), Sequence
    ):
        session_source = session_source.get("items")
    sessions = _sessions(
        session_source
        or metrics.get("market_sessions")
        or metrics.get("sessions")
        or []
    )
    return {
        "regime": regime,
        "phase": phase,
        "driver": driver,
        "relative_strength": relative_strength,
        "resonance": resonance,
        "sessions": sessions,
        "anomalies": list(anomalies),
        "anomaly_warnings": list(anomaly_warnings),
        "main_direction": direction,
    }


def _conflict_layer(
    item: Any,
    direction: str,
    groups: dict[str, Any],
) -> dict[str, Any]:
    item_conflicts = _strings(_read(item, "conflicts", []))
    group_conflicts: dict[str, list[str]] = {}
    for key, value in groups.items():
        if isinstance(value, Mapping):
            group_conflicts[str(key)] = _strings(value.get("conflicts", []))
    participation = _mapping(_read(item, "market_participation", {}))
    participation_conflicts = _strings(participation.get("conflicts", []))
    items = _unique(
        [
            *item_conflicts,
            *(text for values in group_conflicts.values() for text in values),
            *participation_conflicts,
        ]
    )

    domain_items: dict[str, list[str]] = {}
    conflict_group_reasons = {
        text
        for key, value in groups.items()
        if isinstance(value, Mapping)
        and str(value.get("stance", "")).upper() == "CONFLICT"
        for text in group_conflicts.get(str(key), [])
    }
    # Aggregate story conflicts also contain the group conflicts.  Classify
    # only the standalone reasons here; a conflicting group is assigned its
    # canonical evidence domain below so vague wording cannot bypass it.
    for text in [*item_conflicts, *participation_conflicts]:
        if text in conflict_group_reasons:
            continue
        domain = _conflict_domain(text)
        domain_items.setdefault(domain, []).append(text)

    # Position and trend groups always retain their canonical domain.  Their
    # real-world reasons can be deliberately concise (for example, "價格仍在
    # 壓縮中段" or "攻擊效率未改善") and must not be downgraded to OTHER.
    # Participation keeps the more specific Taker/OI/Book/Volume domains.
    for key, value in groups.items():
        if not isinstance(value, Mapping):
            continue
        group = _mapping(value)
        if str(group.get("stance", "")).upper() != "CONFLICT":
            continue
        group_key = str(key)
        group_reasons = group_conflicts.get(group_key, [])
        canonical_domain = _group_conflict_domain(group_key)
        if canonical_domain in {"POSITION_STRUCTURE", "TREND_MOMENTUM"}:
            domain_items.setdefault(canonical_domain, []).extend(
                group_reasons or [str(group.get("label") or group_key)]
            )
        elif group_reasons:
            for text in group_reasons:
                domain = _conflict_domain(text)
                if domain == "OTHER":
                    domain = canonical_domain
                domain_items.setdefault(domain, []).append(text)
        else:
            if (
                group_key == "participation_flow"
                and items
                and all(
                    _conflict_domain(text)
                    in {"CONTEXT_COUNTERTREND", "TIMING_WARNING"}
                    for text in items
                )
            ):
                # Compatibility for episodes created before the participation
                # split: their public group stance may still say CONFLICT even
                # though every retained reason is only macro/Timing context.
                continue
            domain_items.setdefault(canonical_domain, []).append(
                str(group.get("label") or group_key)
            )

    domain_items = {
        key: _unique(values)
        for key, values in domain_items.items()
        if values
    }
    domains = set(domain_items)
    immediate_domains = domains & {
        "POSITION_STRUCTURE",
        "TREND_MOMENTUM",
        "TAKER_FLOW",
        "ORDER_BOOK",
        "DERIVATIVES",
        "PARTICIPATION_VOLUME",
    }
    context_countertrend = "CONTEXT_COUNTERTREND" in domains
    multiple_conflict_domains = len(immediate_domains) >= 2 or (
        context_countertrend and bool(immediate_domains)
    )
    explicit_severity = _number(
        _mapping(_read(item, "market_metrics", {})).get("conflict_severity")
    )
    if not domains and not (explicit_severity and explicit_severity > 0):
        level = "NONE"
    elif multiple_conflict_domains:
        level = "HIGH"
    elif context_countertrend:
        # 1H／4H／全市場背景反向是同一個情境域。它可以降低品質，
        # 但不能靠重複文案自行變成第二個正式方向或一票否決 Trigger。
        level = "MEDIUM" if len(items) >= 2 or (explicit_severity or 0) >= 40 else "LOW"
    elif len(immediate_domains) == 1:
        level = "HIGH" if (explicit_severity or 0) >= 70 else "MEDIUM"
    elif "TIMING_WARNING" in domains:
        level = "MEDIUM" if (explicit_severity or 0) >= 40 else "LOW"
    elif explicit_severity is not None:
        level = "HIGH" if explicit_severity >= 70 else "MEDIUM" if explicit_severity >= 40 else "LOW"
    else:
        level = "LOW"
    return {
        "main_direction": direction,
        "level": level,
        "label": {
            "NONE": "無明顯衝突",
            "LOW": "輕度衝突",
            "MEDIUM": "中度衝突",
            "HIGH": "高度衝突",
        }[level],
        "items": items[:6],
        "domains": [
            {"key": key, "items": values[:3]}
            for key, values in sorted(domain_items.items())
        ],
        # Conflict is explanatory telemetry only.  A core price Trigger that
        # has a valid price Trigger must not disappear because a
        # second interpretation layer counted contrary evidence.  The same
        # domains still lower confidence and remain visible in the details.
        "blocking_domains": [],
        "blocks_entry": False,
        "severity_score": explicit_severity,
        "countertrend": context_countertrend,
        "opposite_signal_created": False,
    }


def _continuation_confirmation_layer(
    *,
    item: Any,
    direction: str,
    metrics: dict[str, Any],
    story: dict[str, Any],
    trigger: dict[str, Any],
    groups: dict[str, Any],
    terminal_invalidation: bool,
    target_completed: bool,
) -> dict[str, Any]:
    """Explain whether an existing price Trigger has follow-through support.

    This layer is deliberately informational.  Price structure and MA/MACD
    keep using the facts already produced by Market Story, while directional
    participation has exactly three votes: OI, Taker/CVD (one shared vote),
    and closed-candle volume.  Funding, BTC and higher-timeframe context are
    warnings only and can never become a fourth vote.
    """

    participation = _mapping(_read(item, "market_participation", {}))
    participation_group = _mapping(groups.get("participation_flow", {}))
    conflict_texts = _unique(
        [
            *_strings(_read(item, "conflicts", [])),
            *_strings(participation.get("conflicts", [])),
            *_strings(participation_group.get("conflicts", [])),
            *_strings(trigger.get("conflicts", [])),
        ]
    )
    short_horizon = str(
        _read(item, "radar_horizon", "SHORT") or "SHORT"
    ).upper() == "SHORT"

    supporting: list[str] = []
    conflicts: list[str] = []
    missing: list[str] = []
    # Existing prose can come from another timeframe or an older payload.
    # Keep it visible as context, but only structured current-scan facts below
    # may cast one of the three continuation votes.
    warnings = list(conflict_texts)
    story_counterevidence = False
    counter_domains: set[str] = set()

    position = _mapping(groups.get("position_structure", {}))
    position_state = str(position.get("stance") or "").upper()
    acceptance = _mapping(story.get("price_acceptance", {}))
    acceptance_state = _state_key(acceptance)
    if acceptance_state in {"ACCEPTED", "ROLE_REVERSAL_RETEST"}:
        supporting.append(str(acceptance.get("label") or "價格已接受突破區外位置"))
    elif acceptance_state == "BREAKING":
        supporting.append(str(acceptance.get("label") or "價格正在突破形成中"))
    elif acceptance_state == "REJECTED":
        warnings.append(str(acceptance.get("label") or "價格未接受突破區外位置"))
        story_counterevidence = True
    elif position_state == "SUPPORT":
        supporting.append(
            str(position.get("label") or "結構／價格位置支持 Trigger 方向")
        )
    elif position_state == "CONFLICT":
        position_conflicts = _strings(position.get("conflicts", []))
        warnings.extend(
            position_conflicts
            or [str(position.get("label") or "結構／價格位置出現反證")]
        )
        story_counterevidence = True
    elif acceptance_state in {"", "UNKNOWN", "NO_ZONE"}:
        missing.append("結構／價格接受資料")

    trend = _mapping(groups.get("trend_momentum", {}))
    trend_state = str(trend.get("stance") or "").upper()
    momentum = _mapping(trigger.get("momentum_confirmation", {}))
    if momentum.get("confirmed") is True or momentum.get("full_confirmation") is True:
        supporting.append(str(momentum.get("label") or "MA／MACD 已同向呼應"))
    elif momentum.get("partial") is True:
        supporting.append(str(momentum.get("label") or "MA 或 MACD 已先轉向"))
    elif momentum and (
        momentum.get("confirmed") is False and momentum.get("partial") is False
    ):
        warnings.append(str(momentum.get("label") or "MA／MACD 尚未同向呼應"))
        story_counterevidence = True
    elif trend_state == "SUPPORT":
        supporting.append(str(trend.get("label") or "MA／MACD 支持 Trigger 方向"))
    elif trend_state == "CONFLICT":
        trend_conflicts = _strings(trend.get("conflicts", []))
        warnings.extend(
            trend_conflicts
            or [str(trend.get("label") or "MA／MACD 尚未同向呼應")]
        )
        story_counterevidence = True
    elif not trend:
        missing.append("MA／MACD 動能資料")

    directional_return = _directional_core_return(direction, story)
    observer = _mapping(metrics.get("continuation_observer"))
    observer_selected = _mapping(observer.get("selected"))
    observer_present = bool(observer.get("algorithm_version"))
    observer_ready = observer_selected.get("ready") is True
    if observer_present:
        observer_domains = _mapping(observer_selected.get("domains"))
        oi_vote = _continuation_observer_vote(
            _mapping(observer_domains.get("OI")),
            "OI 固定平均窗",
            ready=observer_ready,
        )
        taker_vote = _continuation_observer_vote(
            _mapping(observer_domains.get("TAKER_CVD")),
            "Taker／CVD 固定平均窗",
            ready=observer_ready,
        )
        volume_vote = _continuation_observer_vote(
            _mapping(observer_domains.get("VOLUME")),
            "成交量固定平均窗",
            ready=observer_ready,
        )
    else:
        oi_vote = _continuation_oi_vote(
            metrics,
            directional_return,
        )
        taker_vote = _continuation_taker_cvd_vote(
            direction,
            metrics,
            directional_return,
        )
        volume_vote = _continuation_volume_vote(
            metrics,
            directional_return,
            allow_15m_fallback=short_horizon,
        )
    votes = {
        "OI": oi_vote,
        "TAKER_CVD": taker_vote,
        "VOLUME": volume_vote,
    }
    for domain, vote in votes.items():
        state = vote["state"]
        if state == "SUPPORT":
            supporting.extend(vote["reasons"])
        elif state == "CONFLICT":
            conflicts.extend(vote["reasons"])
            counter_domains.add(domain)
        elif state == "UNKNOWN":
            missing.extend(vote["missing"])
        warnings.extend(vote["warnings"])

    flow_history_state = _state_key(
        metrics.get("flow_participation_state")
        if metrics.get("flow_participation_state") is not None
        else metrics.get("flow_trend")
        if metrics.get("flow_trend") is not None
        else _mapping(participation.get("trend", {}))
    )
    structured_history_complete = all(
        _state_key(metrics.get(key)) not in {"", "UNKNOWN"}
        for key in ("flow_oi_alignment", "flow_taker_state")
    )
    valid_history_samples = _number(metrics.get("flow_valid_sample_count"))
    observer_primary_ready = bool(
        observer_present
        and observer_ready
        and str(observer.get("selected_window") or "")
        == str(observer.get("primary_window") or "")
    )
    if observer_primary_ready:
        observer_windows = _mapping(observer.get("windows"))
        early_window = _mapping(
            observer_windows.get(str(observer.get("early_window") or ""))
        )
        if str(early_window.get("state") or "").upper() in {"CONFLICT", "MIXED"}:
            # The full window still describes the original post-trigger move,
            # but a reversing fast half-window must keep the current advisory
            # out of the top tier.
            story_counterevidence = True
            warnings.append("快速平均窗已轉弱；基準窗仍在，但近期接力不一致")
    history_insufficient = (
        not observer_primary_ready
        if observer_present
        else (
            not structured_history_complete
            or valid_history_samples is None
            or (
                valid_history_samples is not None
                and valid_history_samples < 3
            )
        )
    )
    if history_insufficient:
        if observer_present:
            primary_window = str(observer.get("primary_window") or "完整")
            bucket_count = int(_number(observer.get("bucket_count")) or 0)
            target_buckets = int(_number(observer.get("target_buckets")) or 0)
            missing.append(
                f"{primary_window} 固定平均窗（{bucket_count}/{target_buckets} 區間）"
            )
        else:
            missing.append("連續資金流歷史（至少 3 筆）")
    elif flow_history_state == "WEAKENING":
        warnings.append("整體資金參與歷史正在轉弱（僅作提醒，不計方向票）")
    elif flow_history_state == "MIXED":
        warnings.append("整體資金參與歷史分歧（僅作提醒，不計方向票）")

    support_count = sum(
        vote["state"] == "SUPPORT" for vote in votes.values()
    )
    capital_conflict_count = sum(
        vote["state"] == "CONFLICT" for vote in votes.values()
    )
    known_count = sum(vote["state"] != "UNKNOWN" for vote in votes.values())
    severe_counterevidence = any(vote["severe"] for vote in votes.values())
    capital_observed = known_count > 0

    explicit_triggered = trigger.get("triggered")
    source_stage = _stage(item)
    trade_plan_present = all(
        _number(_read(item, key, None)) is not None
        for key in ("entry_low", "entry_high", "stop_loss", "take_profit_1")
    )
    trigger_active = (
        not terminal_invalidation
        and not target_completed
        and trade_plan_present
        and source_stage in _FORMAL_STAGES
        and (
            trigger.get("active_episode_preserved") is True
            or explicit_triggered is True
            or (explicit_triggered is not False and "triggered" not in trigger)
        )
    )

    if (
        not trigger_active
        or direction == "NEUTRAL"
        or not capital_observed
        or (support_count == 0 and capital_conflict_count == 0)
    ):
        key = "UNKNOWN"
        score = None
    elif severe_counterevidence or len(counter_domains) >= 2:
        key = "CONFLICT"
        score = min(
            35.0,
            _continuation_score(support_count, capital_conflict_count),
        )
    elif (
        support_count >= 2
        and oi_vote["state"] == "SUPPORT"
        and not counter_domains
        and not story_counterevidence
        and not history_insufficient
    ):
        key = "CONFIRMED"
        score = max(
            75.0,
            _continuation_score(support_count, capital_conflict_count),
        )
    else:
        key = "FORMING"
        score = min(
            70.0,
            _continuation_score(support_count, capital_conflict_count),
        )

    if direction == "NEUTRAL":
        missing.append("明確 Trigger 方向")
    if not trigger_active:
        missing.append("有效中的正式價格 Trigger")
    if support_count == 0 and capital_conflict_count == 0:
        missing.append("至少一項同向資金證據")
    labels = {
        "CONFIRMED": "高｜同向續走證據一致",
        "FORMING": "中｜同向續走形成中",
        "CONFLICT": "低｜續走證據衝突",
        "UNKNOWN": "未知｜續走資料不足",
    }
    return {
        "key": key,
        "label": labels[key],
        "score": round(score, 1) if score is not None else None,
        "core_votes": {
            domain: _continuation_vote_summary(domain, vote)
            for domain, vote in votes.items()
        },
        "supporting": _unique(supporting)[:8],
        "conflicts": _unique(conflicts)[:8],
        "missing": _unique(missing)[:8],
        "warnings": _unique(warnings)[:6],
        "meaning": (
            "score 代表同向延續證據的一致度，不是勝率或價格上漲／下跌概率；"
            "本層只提供資訊，不會取消 Trigger、改變方向或改寫進場權限。"
        ),
        "observer": dict(observer) if observer_present else {},
    }


def _continuation_observer_vote(
    domain: Mapping[str, Any],
    label: str,
    *,
    ready: bool,
) -> dict[str, Any]:
    """Translate one same-window average domain into the existing 3-vote model."""

    if not ready:
        return _vote("UNKNOWN", missing=[f"{label}仍在採樣"])
    state = str(domain.get("state") or "UNKNOWN").strip().upper()
    reason = str(domain.get("reason") or domain.get("detail") or label)
    if state == "SUPPORT":
        return _vote("SUPPORT", reason)
    if state == "CONFLICT":
        return _vote(
            "CONFLICT",
            reason,
            severe=domain.get("severe") is True,
        )
    if state == "NEUTRAL":
        return _vote("NEUTRAL", warnings=[reason])
    return _vote(
        "UNKNOWN",
        missing=[str(domain.get("missing") or f"{label}資料不足")],
    )


def _continuation_oi_vote(
    metrics: dict[str, Any],
    directional_return: float | None,
) -> dict[str, Any]:
    alignment = _state_key(metrics.get("flow_oi_alignment"))
    structured = "flow_oi_alignment" in metrics
    if alignment in {"SAME_DIRECTION_BUILD", "ALIGNED", "SUPPORT"}:
        return _vote("SUPPORT", "OI 新增部位與 Trigger 方向同向")
    if alignment in {"OPPOSITE_BUILD", "OPPOSITE_DIRECTION_BUILD"}:
        return _vote(
            "CONFLICT",
            "OI 顯示反方向新增部位（opposite build）",
            severe=True,
        )
    if alignment in {"UNRESOLVED_BUILD", "POSITION_EXIT", "STABLE", "MIXED"}:
        warning = {
            "UNRESOLVED_BUILD": "OI 正在增加，但價格方向仍未解開",
            "POSITION_EXIT": "OI 下降，較像平倉／回補而非新增部位延續",
            "STABLE": "OI 目前穩定，未提供新增部位票",
            "MIXED": "OI 歷史分歧，未提供方向票",
        }[alignment]
        return _vote("NEUTRAL", warnings=[warning])
    oi_change = _number(metrics.get("open_interest_change_pct"))
    if oi_change is not None and directional_return is not None:
        if oi_change >= 0.5 and directional_return > 0:
            return _vote("SUPPORT", "價格與 OI 同向增加（單次快照）")
        if oi_change >= 0.5 and directional_return < 0:
            return _vote(
                "CONFLICT",
                "價格反向且 OI 增加（單次快照），可能有反方向新增部位",
                severe=True,
            )
        return _vote(
            "NEUTRAL",
            warnings=["OI 未形成同方向新增部位支持"],
        )
    if structured:
        return _vote("UNKNOWN", missing=["OI 連續變化歷史"])
    return _vote("UNKNOWN", missing=["OI 方向資料"])


def _continuation_taker_cvd_vote(
    direction: str,
    metrics: dict[str, Any],
    directional_return: float | None,
) -> dict[str, Any]:
    states: list[str] = []
    reasons: list[str] = []
    warnings: list[str] = []
    missing: list[str] = []
    severe = False

    structured = "flow_taker_state" in metrics
    flow_state = _state_key(metrics.get("flow_taker_state"))
    if flow_state == "STRENGTHENING":
        # STRENGTHENING only says the directional share rose over the history
        # window.  It does not say that side is currently dominant (for
        # example, LONG 20% -> 30% is still seller-dominated), so the absolute
        # snapshot below must earn the directional vote.
        states.append("NEUTRAL")
        warnings.append("Taker 同向占比正在增加，仍需目前占比確認主導方")
    elif flow_state == "WEAKENING":
        if directional_return is not None and directional_return < 0.0:
            states.append("CONFLICT")
            reasons.append("Taker 主動成交轉弱且價格反向")
        else:
            states.append("NEUTRAL")
            warnings.append("Taker 主動成交歷史沿 Trigger 方向轉弱")
    elif flow_state in {"STABLE", "MIXED"}:
        states.append("NEUTRAL")
    elif flow_state == "UNKNOWN" and structured:
        missing.append("Taker／CVD 連續變化歷史")

    taker_buy_pct = _number(metrics.get("taker_buy_pct"))
    raw_seen = taker_buy_pct is not None and 0.0 <= taker_buy_pct <= 100.0
    if raw_seen:
        buy_share = taker_buy_pct / 100.0
        directional_share = buy_share if direction == "LONG" else 1.0 - buy_share
        if directional_return is None:
            missing.append("Taker 缺少已收盤核心 K 線價格成果")
        elif directional_share >= 0.62 and directional_return <= 0.0:
            states.append("CONFLICT")
            reasons.append("Taker 很強但價格推不動，可能遭對手吸收")
            severe = True
        elif (
            directional_share >= 0.60
            and directional_return > 0.02
            and flow_state not in {"WEAKENING", "MIXED"}
        ):
            states.append("SUPPORT")
            reasons.append("Taker 主動成交與價格成果同向")
        elif directional_share <= 0.35 and directional_return < 0.0:
            states.append("CONFLICT")
            reasons.append("Taker 主動成交與價格反應明顯反向")
        else:
            states.append("NEUTRAL")

    cvd = _number(metrics.get("cvd"))
    if cvd is not None:
        raw_seen = True
        directional_cvd = cvd if direction == "LONG" else -cvd
        if directional_return is None:
            missing.append("CVD 缺少已收盤核心 K 線價格成果")
        elif directional_cvd > 0 and directional_return <= 0:
            states.append("CONFLICT")
            reasons.append("CVD 同向但價格未跟進，可能存在吸收")
            severe = True
        elif (
            directional_cvd > 0
            and directional_return > 0
            and flow_state not in {"WEAKENING", "MIXED"}
        ):
            states.append("SUPPORT")
            reasons.append("CVD 與價格成果同向")
        elif directional_cvd < 0 and directional_return < 0:
            states.append("CONFLICT")
            reasons.append("CVD 與 Trigger 方向相反")
        else:
            states.append("NEUTRAL")

    if severe:
        return _vote("CONFLICT", *reasons, severe=True)
    if "CONFLICT" in states:
        return _vote("CONFLICT", *reasons)
    if "SUPPORT" in states:
        return _vote("SUPPORT", *reasons)
    if "NEUTRAL" in states:
        return _vote("NEUTRAL", warnings=warnings)
    return _vote(
        "UNKNOWN",
        missing=missing or ["Taker／CVD 方向資料"],
    )


def _continuation_volume_vote(
    metrics: dict[str, Any],
    directional_return: float | None,
    *,
    allow_15m_fallback: bool,
) -> dict[str, Any]:
    ratio = _number(
        metrics.get("volume_ratio_core")
        if metrics.get("volume_ratio_core") is not None
        else metrics.get("volume_ratio_15m")
        if allow_15m_fallback
        else None
    )
    if ratio is not None and directional_return is not None:
        if ratio >= 1.2 and directional_return > 0:
            return _vote("SUPPORT", f"K 線量能放大至 {ratio:.2f} 倍且價格同向")
        if ratio >= 1.2 and directional_return < 0:
            return _vote(
                "CONFLICT",
                f"K 線量能放大至 {ratio:.2f} 倍但價格反向",
            )
        return _vote(
            "NEUTRAL",
            warnings=[f"K 線量能 {ratio:.2f} 倍，尚未形成同向放量票"],
        )

    if ratio is not None:
        return _vote("UNKNOWN", missing=["K 線量能對應的價格方向"])
    return _vote("UNKNOWN", missing=["K 線量能資料"])


def _vote(
    state: str,
    *reasons: str,
    severe: bool = False,
    warnings: Sequence[str] | None = None,
    missing: Sequence[str] | None = None,
) -> dict[str, Any]:
    return {
        "state": state,
        "reasons": _unique([str(reason) for reason in reasons if reason]),
        "severe": severe,
        "warnings": _unique([str(value) for value in (warnings or []) if value]),
        "missing": _unique([str(value) for value in (missing or []) if value]),
    }


def _continuation_vote_summary(
    domain: str,
    vote: Mapping[str, Any],
) -> dict[str, str]:
    state = str(vote.get("state") or "UNKNOWN").upper()
    labels = {
        "SUPPORT": "同向支持",
        "CONFLICT": "出現反證",
        "NEUTRAL": "尚未確認",
        "UNKNOWN": "資料不足",
    }
    details = [
        *_strings(vote.get("reasons", [])),
        *_strings(vote.get("warnings", [])),
        *_strings(vote.get("missing", [])),
    ]
    fallbacks = {
        "OI": "OI 尚未提供明確新增部位方向",
        "TAKER_CVD": "Taker／CVD 尚未提供明確主動成交方向",
        "VOLUME": "成交量尚未提供明確同向參與",
    }
    return {
        "state": state if state in labels else "UNKNOWN",
        "label": labels.get(state, labels["UNKNOWN"]),
        "detail": details[0] if details else fallbacks.get(domain, "方向資料不足"),
    }


def _continuation_score(support_count: int, conflict_count: int) -> float:
    return round(
        max(0.0, min(100.0, 50.0 + (support_count - conflict_count) * (50.0 / 3.0))),
        1,
    )


def _directional_core_return(
    direction: str,
    story: dict[str, Any],
) -> float | None:
    raw = _mapping(story.get("raw", {}))
    value = _number(raw.get("core_return_pct"))
    if value is None or direction not in {"LONG", "SHORT"}:
        return None
    return value if direction == "LONG" else -value


def _state_key(value: Any) -> str:
    if isinstance(value, Mapping):
        value = next(
            (
                value.get(key)
                for key in ("alignment", "state", "key", "value")
                if value.get(key) not in (None, "")
            ),
            "",
        )
    return str(value or "").strip().upper()


def _quality_layer(
    evidence: dict[str, Any],
    execution: dict[str, Any],
) -> dict[str, Any]:
    execution_value = _quality_value(_number(execution.get("score")))
    if execution.get("label"):
        execution_value["label"] = str(execution["label"])
    execution_value["recommendation"] = str(
        execution.get("recommendation") or "UNKNOWN"
    ).upper()
    return {
        "direction": dict(evidence["direction_quality"]),
        "execution": execution_value,
        "participation": dict(evidence["participation"]),
        "combined_score": None,
        "note": "Direction、Execution、Participation 分開判讀，不揉成單一分數。",
    }


def _confidence_layer(
    direction: str,
    evidence: dict[str, Any],
    conflict: dict[str, Any],
    hard_gate: dict[str, Any],
    data_quality: dict[str, Any],
    anomaly_warnings: list[str],
) -> dict[str, Any]:
    group_stances = [
        str(value.get("stance", "NEUTRAL")).upper()
        for value in evidence["groups"].values()
    ]
    support_count = group_stances.count("SUPPORT")
    conflict_count = group_stances.count("CONFLICT")
    _, _, missing = _data_states(data_quality)
    reasons: list[str] = []
    if direction == "NEUTRAL":
        key = "LOW"
        reasons.append("主方向尚未明確")
    elif hard_gate["unknown"] or missing:
        key = "LOW"
        reasons.append("資料完整度不足")
    elif conflict["level"] == "HIGH" or conflict_count >= 2:
        key = "LOW"
        reasons.append("關鍵證據高度衝突")
    elif (
        evidence["direction_quality"]["key"] == "HIGH"
        and support_count >= 2
        and conflict["level"] in {"NONE", "LOW"}
    ):
        key = "HIGH"
        reasons.append("方向證據一致且資料完整")
    else:
        key = "MEDIUM"
        reasons.append("方向可判讀，但仍有中性或反向證據")
    if anomaly_warnings:
        key = {"HIGH": "MEDIUM", "MEDIUM": "LOW", "LOW": "LOW"}[key]
        reasons.append("異常行情風險處於 WATCH，信心下調一級")
    return {
        "key": key,
        "label": {"HIGH": "高", "MEDIUM": "中", "LOW": "低"}[key],
        "reasons": reasons,
        "meaning": "代表證據一致程度與資料完整度，不是勝率。",
    }


def _episode_layer(
    item: Any,
    lifecycle: dict[str, Any],
    *,
    terminal_invalidation: bool,
    target_completed: bool,
) -> dict[str, Any]:
    stage = _stage(item, lifecycle)
    transition = str(lifecycle.get("transition") or "UNCHANGED").upper()
    if terminal_invalidation:
        status, trend, arrow, label = "INVALIDATED", "DOWN", "↓", "失效"
    elif target_completed:
        status, trend, arrow, label = "CONFIRMED", "FLAT", "→", "目標已達／本次完成"
    elif stage in {"NO_FOLLOW_THROUGH", "EXTENDED"} or transition == "DOWNGRADED":
        status, trend, arrow, label = "WEAKENING", "DOWN", "↓", "正在轉弱"
    elif transition == "UPGRADED" or stage == "REENTRY":
        status, trend, arrow, label = "STRENGTHENING", "UP", "↑", "正在增強"
    elif stage in {"CONFIRMED", "TRENDING"}:
        status, trend, arrow, label = "CONFIRMED", "FLAT", "→", "確認／有效中"
    else:
        status, trend, arrow, label = "EARLY", "FLAT", "→", "早期／形成中"
    return {
        "status": status,
        "label": label,
        "trend": trend,
        "arrow": arrow,
        "source_stage": stage,
        "transition": transition,
        "trigger_id": str(_read(item, "trigger_id", "") or ""),
        "terminal": terminal_invalidation or target_completed,
    }


def _final_layer(
    *,
    item: Any,
    direction: str,
    plan_present: bool,
    hard_gate: dict[str, Any],
    evidence: dict[str, Any],
    market_context: dict[str, Any],
    conflict: dict[str, Any],
    quality: dict[str, Any],
    confidence: dict[str, Any],
    episode: dict[str, Any],
    entry: dict[str, Any],
    terminal_invalidation: bool,
    target_completed: bool,
    anomaly_warnings: list[str],
) -> dict[str, Any]:
    blockers = set(hard_gate["blockers"])
    entry_status = str(entry.get("status") or "UNKNOWN").upper()
    entry_label = str(entry.get("label") or "")
    entry_reason = str(entry.get("reason") or "")
    entry_chase_atr = _number(entry.get("chase_atr"))
    missed_chase_limit = _number(entry.get("missed_chase_atr"))
    explicit_no_chase = any(
        token in f"{entry_label} {entry_reason}"
        for token in ("追價", "離開最佳")
    )
    beyond_missed_limit = bool(
        entry_chase_atr is not None
        and missed_chase_limit is not None
        and entry_chase_atr > missed_chase_limit
    )
    missed_entry_no_chase = entry_status == "MISSED_ENTRY" and (
        explicit_no_chase or beyond_missed_limit
    )
    stage = episode["source_stage"]
    active_trigger = plan_present and stage in _FORMAL_STAGES and not target_completed
    has_risk_warnings = bool(
        hard_gate["blocked"]
        or hard_gate["unknown"]
        or market_context["anomalies"]
    )

    if terminal_invalidation:
        status, label = "INVALIDATED", "交易計畫已失效"
        wait_code, wait_label = "NEW_TRIGGER_REQUIRED", "等待新的 Trigger／REENTRY"
    elif target_completed:
        status, label = "NO_EDGE", "本次目標已達｜不可重新追入"
        wait_code, wait_label = "NEW_TRIGGER_REQUIRED", "等待新的 Trigger／REENTRY"
    elif missed_entry_no_chase:
        status, label = "NO_CHASE", "已離開合理進場區｜禁止追價"
        wait_code, wait_label = "PRICE_TOO_FAR", "等待新的進場機會"
    elif entry_status == "MISSED_ENTRY":
        status, label = "WAIT", "進場窗口已關閉｜禁止新進場"
        wait_code, wait_label = (
            "ENTRY_WINDOW_CLOSED",
            "等待新的 Trigger／REENTRY",
        )
    elif not plan_present or direction == "NEUTRAL":
        if stage == "NEAR_TRIGGER":
            status, label = "WAIT", "訊號形成中｜等待正式 Trigger"
            wait_code, wait_label = "SIGNAL_FORMING", "等待價格觸發與收盤確認"
        else:
            status, label = "NO_EDGE", "目前無明確交易優勢"
            wait_code, wait_label = "NO_EDGE", "等待正式方向與交易計畫"
    elif entry_status == "ENTRY_READY" and active_trigger:
        status, label = (
            "ENTER",
            "目前可進｜附風險提醒" if has_risk_warnings else "目前可進",
        )
        wait_code, wait_label = "NONE", ""
    elif entry_status == "WAIT_RETEST":
        status, label = "WAIT", "等待回踩／重新確認"
        wait_code, wait_label = "ENTRY_RETEST", "等待重新站回合理進場區"
    elif stage in {"NEAR_TRIGGER", "WATCH", "NONE", ""}:
        status, label = "WAIT", "訊號形成中｜等待正式 Trigger"
        wait_code, wait_label = "SIGNAL_FORMING", "等待價格觸發與收盤確認"
    else:
        status, label = "WAIT", "條件尚未完整｜等待確認"
        wait_code, wait_label = "ENTRY_CONFIRMATION", "等待進場資格完整"

    reasons: list[str] = []
    if status == "ENTER":
        reasons.extend(
            [
                f"主方向：{_direction_label(direction)}",
                entry_label or "價格仍在合理進場區",
            ]
        )
        reasons.append(f"方向品質：{quality['direction']['label']}")
    elif status == "INVALIDATED":
        reasons.extend(
            [
                "原 Signal Episode 已永久失效",
                _invalidation_condition(item),
                "舊 Entry／SL／TP 不可復活",
            ]
        )
    elif status == "ANOMALY":
        reasons.extend([*market_context["anomalies"], "異常情境不可套用一般進場規則"])
    elif status == "DATA_UNAVAILABLE":
        reasons.extend(hard_gate["reasons"])
        reasons.append("不知道就顯示不知道，不使用替代值硬算")
    elif status == "NO_CHASE":
        hard_chase_reason = next(
            (
                str(check.get("reason") or "")
                for check in hard_gate.get("checks", [])
                if check.get("key") == "chase"
                and check.get("status") == "BLOCKED"
            ),
            "",
        )
        reasons.extend(
            [
                hard_chase_reason
                or entry_reason
                or entry_label
                or "價格已離開合理進場位置",
                "禁止追價不等於原方向失效",
            ]
        )
    elif wait_code == "ENTRY_WINDOW_CLOSED":
        reasons.extend(
            [
                entry_reason or entry_label or "原訊號的進場窗口已關閉",
                "這不是價格追價判定；目前仍禁止新進場",
                "等待新的 Trigger／REENTRY 建立新計畫",
            ]
        )
    elif status == "NO_EDGE":
        reasons.extend(hard_gate["reasons"])
        if not plan_present:
            reasons.append("目前尚無正式 Entry／SL／TP 計畫")
    else:
        reasons.extend(hard_gate["reasons"])
        reasons.extend(conflict["items"])
        if entry_reason:
            reasons.append(entry_reason)
    reasons = _unique([text for text in reasons if text])
    while len(reasons) < 2:
        reasons.append(
            "等待下一次已收盤資料重新確認"
            if status != "ENTER"
            else "Signal Episode 仍在有效進場階段"
        )

    weakening = _strings(_read(item, "weakening_conditions", []))
    if not weakening:
        weakening = [*conflict["items"]]
        participation = evidence["participation"]
        if participation.get("state") in {"CONFLICT", "DATA_MISSING", "UNKNOWN"}:
            weakening.append("市場參與轉弱或資料無法確認")
    if not weakening:
        weakening = ["OI／Taker／Volume 轉弱", "核心結構失去延續性"]

    return {
        "status": status,
        "label": label,
        "direction": direction,
        "direction_label": _direction_label(direction),
        "new_entry_allowed": status == "ENTER",
        "trigger_preserved": not terminal_invalidation,
        "reasons": reasons[:3],
        "wait_reason": (
            None
            if wait_code == "NONE"
            else {"code": wait_code, "label": wait_label}
        ),
        "weakening_conditions": _unique(weakening)[:3],
        "invalidation_condition": _invalidation_condition(item),
        "confidence": dict(confidence),
        "warnings": _unique(
            [
                *anomaly_warnings,
                *hard_gate.get("warnings", []),
                *(
                    [f"{conflict['label']}：請核對反向證據"]
                    if status == "ENTER" and conflict["level"] != "NONE"
                    else []
                ),
            ]
        )[:5],
    }


def _conflict_domain(text: str) -> str:
    value = str(text or "").lower()
    if any(
        token in value
        for token in (
            "逆勢",
            "高週期",
            "1h",
            "4h",
            "1d",
            "背景反向",
            "全市場",
            "大盤",
            "btc",
            "market bias",
            "countertrend",
        )
    ):
        return "CONTEXT_COUNTERTREND"
    if any(token in value for token in ("taker", "主動成交", "主動買", "主動賣", "cvd")):
        return "TAKER_FLOW"
    if any(token in value for token in ("order book", "委託簿", "訂單簿", "深度", "假牆", "撤單")):
        return "ORDER_BOOK"
    if any(token in value for token in ("funding", "open interest", "oi ", "oi（", "持倉量")):
        return "DERIVATIVES"
    if any(token in value for token in ("volume", "成交量", "量能", "市場參與", "資金流")):
        return "PARTICIPATION_VOLUME"
    if any(token in value for token in ("timing", "5m", "進場時機", "短線降速")):
        return "TIMING_WARNING"
    if any(token in value for token in ("結構", "支撐", "壓力", "突破", "跌破", "高低點")):
        return "POSITION_STRUCTURE"
    if any(token in value for token in ("趨勢", "動能", "macd", "均線", "rsi")):
        return "TREND_MOMENTUM"
    return "OTHER"


def _group_conflict_domain(key: str) -> str:
    normalized = key.strip().lower()
    if normalized == "position_structure":
        return "POSITION_STRUCTURE"
    if normalized == "trend_momentum":
        return "TREND_MOMENTUM"
    if normalized == "participation_flow":
        return "PARTICIPATION_VOLUME"
    return "OTHER"


def _numeric_limit_check(
    checks: list[dict[str, Any]],
    *,
    key: str,
    label: str,
    value: float | None,
    limit: float,
    comparison: str,
    missing_reason: str,
    blocked_reason: str,
) -> None:
    if value is None:
        _add_check(checks, key, label, "UNKNOWN", None, missing_reason)
        return
    if comparison == "MIN":
        passed = value >= limit
    elif comparison == "POSITIVE_MAX":
        passed = 0 < value <= limit
    else:
        passed = value <= limit
    _add_check(
        checks,
        key,
        label,
        "PASSED" if passed else "BLOCKED",
        round(value, 6),
        f"{label}。" if passed else blocked_reason,
    )


def _add_check(
    checks: list[dict[str, Any]],
    key: str,
    label: str,
    status: str,
    value: Any,
    reason: str,
) -> None:
    checks.append(
        {
            "key": key,
            "label": label,
            "status": status,
            "passed": status == "PASSED",
            "value": value,
            "reason": reason,
            "hard": True,
        }
    )


def _threshold(source: Any, key: str, default: float) -> float:
    value = _read(source, key, default)
    numeric = _number(value)
    return numeric if numeric is not None else default


def _data_states(data_quality: dict[str, Any]) -> tuple[str, str, list[str]]:
    if not data_quality:
        return "UNKNOWN", "UNKNOWN", ["data_quality"]
    core = str(
        data_quality.get("core")
        or data_quality.get("core_status")
        or "UNKNOWN"
    ).upper()
    deep = str(
        data_quality.get("deep")
        or data_quality.get("deep_status")
        or "UNKNOWN"
    ).upper()
    missing = _strings(data_quality.get("missing_sources", []))
    if core not in _AVAILABLE and core not in _UNAVAILABLE:
        core = "UNKNOWN"
    if deep not in _AVAILABLE and deep not in _UNAVAILABLE:
        deep = "UNKNOWN"
    return core, deep, missing


def _stop_pct(item: Any, metrics: dict[str, Any]) -> float | None:
    explicit = _number(metrics.get("technical_stop_pct"))
    if explicit is not None:
        return explicit
    stop = _number(_read(item, "stop_loss", None))
    low = _number(_read(item, "entry_low", None))
    high = _number(_read(item, "entry_high", None))
    if stop is None or low is None or high is None:
        return None
    entry = (low + high) / 2.0
    if entry == 0:
        return None
    return abs(entry - stop) / abs(entry) * 100.0


def _plan_present(item: Any, trigger: dict[str, Any]) -> bool:
    values = (
        _read(item, "entry_low", None),
        _read(item, "entry_high", None),
        _read(item, "stop_loss", None),
        _read(item, "take_profit_1", None),
    )
    if all(_number(value) is not None for value in values):
        return True
    return bool(trigger.get("triggered")) and _stage(item) in _FORMAL_STAGES


def _terminal_invalidation(
    item: Any,
    lifecycle: dict[str, Any],
    entry: dict[str, Any],
) -> bool:
    values = {
        str(_stage(item, lifecycle)).upper(),
        str(_read(item, "freshness", "")).upper(),
        str(lifecycle.get("status", "")).upper(),
        str(lifecycle.get("current_stage", "")).upper(),
        str(lifecycle.get("outcome", "")).upper(),
        str(entry.get("situation", "")).upper(),
        str(entry.get("status", "")).upper(),
    }
    invalid_tokens = {
        "INVALIDATED",
        "PLAN_INVALIDATED",
        "PRICE_INVALIDATED",
        "SL_FIRST",
        "STOP_HIT",
        "SL_HIT",
        "PREFLIGHT_STOP_CROSSED",
    }
    if values & invalid_tokens:
        return True
    target_tokens = {"TARGET_REACHED", "TP1_FIRST", "COMPLETED"}
    if "CLOSED" in values:
        return not bool(values & target_tokens)
    if lifecycle.get("terminal") is True:
        return not bool(values & target_tokens)
    return False


def _target_completed(
    item: Any,
    lifecycle: dict[str, Any],
    entry: dict[str, Any],
) -> bool:
    values = {
        str(_read(item, "freshness", "")).upper(),
        str(lifecycle.get("status", "")).upper(),
        str(lifecycle.get("outcome", "")).upper(),
        str(entry.get("situation", "")).upper(),
    }
    return bool(values & {"TARGET_REACHED", "TP1_FIRST", "COMPLETED"})


def _anomalies(
    item: Any,
    metrics: dict[str, Any],
    story: dict[str, Any],
    safety_checks: list[dict[str, Any]],
) -> list[str]:
    story_context = _mapping(story.get("context", {}))
    anomaly = _mapping(story_context.get("anomaly", {}))
    structured_status = str(anomaly.get("status", "")).upper()
    metrics_state = str(
        metrics.get("anomaly_state")
        or _read(item, "anomaly_state", "")
    ).upper()
    safe_states = {"", "NONE", "NORMAL", "STABLE", "FALSE", "WATCH"}
    blocking_state = structured_status == "BLOCK" or metrics_state not in safe_states
    explicit = _strings(_read(item, "anomalies", []))
    output: list[str] = []
    # A WATCH is context/confidence information and remains advisory. Keep
    # Backward-compatible explicit anomaly fields remain visible when no
    # structured WATCH/NORMAL classification is available; the final layer
    # treats every anomaly result as advisory.
    if blocking_state or (explicit and not structured_status and not metrics_state):
        output.extend(explicit)
        output.extend(_strings(metrics.get("anomalies", [])))
        output.extend(_strings(story_context.get("anomalies", [])))
    if structured_status == "BLOCK":
        for reason in anomaly.get("reasons", []) or []:
            if isinstance(reason, Mapping):
                label = reason.get("label") or reason.get("code")
                if label:
                    output.append(str(label))
            elif reason:
                output.append(str(reason))
        if not anomaly.get("reasons"):
            output.append(str(anomaly.get("label") or "異常行情"))
    if metrics_state not in safe_states:
        output.append(str(metrics.get("anomaly_label") or metrics_state))
    if str(_read(item, "regime", "")).upper() in {"ANOMALY", "ABNORMAL", "DISORDER"}:
        output.append("市場結構異常／失序")
    for row in safety_checks:
        key = str(row.get("key") or "").lower()
        if "anomal" in key and row.get("passed") is False:
            output.append(str(row.get("label") or "異常行情風險提醒"))
    return _unique(output)


def _anomaly_warnings(
    item: Any,
    metrics: dict[str, Any],
    story: dict[str, Any],
) -> list[str]:
    """Return non-blocking WATCH findings for context and confidence only."""

    story_context = _mapping(story.get("context", {}))
    anomaly = _mapping(story_context.get("anomaly", {}))
    structured_status = str(anomaly.get("status", "")).upper()
    metrics_state = str(
        metrics.get("anomaly_state")
        or _read(item, "anomaly_state", "")
    ).upper()
    output: list[str] = []
    if structured_status == "WATCH":
        for reason in anomaly.get("reasons", []) or []:
            if isinstance(reason, Mapping):
                label = reason.get("label") or reason.get("code")
                if label:
                    output.append(str(label))
            elif reason:
                output.append(str(reason))
        if not output:
            output.append(str(anomaly.get("label") or "異常行情風險觀察"))
    if metrics_state == "WATCH":
        output.extend(_strings(metrics.get("anomalies", [])))
        if metrics.get("anomaly_label"):
            output.append(str(metrics["anomaly_label"]))
        if not output:
            output.append("異常行情風險觀察")
    return _unique(output)


def _safety_checks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [dict(row) for row in value if isinstance(row, Mapping)]


def _compact_group(value: Mapping[str, Any]) -> dict[str, Any]:
    score = _number(value.get("score"))
    return {
        "label": str(value.get("label") or ""),
        "score": round(score, 1) if score is not None else None,
        "stance": str(value.get("stance") or "NEUTRAL").upper(),
        "confidence": _number(value.get("confidence")),
    }


def _quality_value(score: float | None) -> dict[str, Any]:
    if score is None:
        return {"key": "UNKNOWN", "label": "未知", "score": None}
    key = "HIGH" if score >= 75.0 else "MEDIUM" if score >= 50.0 else "LOW"
    return {
        "key": key,
        "label": {"HIGH": "高", "MEDIUM": "中", "LOW": "低"}[key],
        "score": round(score, 1),
    }


def _phase(regime: str, trigger_type: str, stage: str) -> str:
    if trigger_type == "BREAKOUT":
        return "BREAKOUT"
    if trigger_type == "CONTINUATION":
        return "PULLBACK"
    if trigger_type == "REVERSAL":
        return "TURNING"
    if stage in {"NO_FOLLOW_THROUGH", "EXTENDED"}:
        return "WEAKENING"
    return {
        "TREND": "TREND",
        "RANGE": "RANGE",
        "COMPRESSION": "RANGE",
        "TRANSITION": "TRANSITION",
        "ANOMALY": "ANOMALY",
        "ABNORMAL": "ANOMALY",
        "DISORDER": "ANOMALY",
    }.get(regime, "UNKNOWN")


def _context_value(*values: Any) -> dict[str, Any]:
    value = next((entry for entry in values if entry not in (None, "", [], {})), None)
    if value is None:
        return {"state": "UNKNOWN", "label": "資料不足"}
    if isinstance(value, Mapping):
        state = str(value.get("state") or value.get("key") or "AVAILABLE").upper()
        label = str(value.get("label") or value.get("reason") or state)
        return {"state": state, "label": label}
    return {"state": str(value).upper(), "label": str(value)}


def _sessions(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        output = []
        for key, row in value.items():
            if isinstance(row, Mapping):
                output.append(
                    {
                        "key": str(key).upper(),
                        "label": str(row.get("label") or key),
                        "active": bool(row.get("active")),
                    }
                )
            else:
                output.append(
                    {"key": str(key).upper(), "label": str(key), "active": bool(row)}
                )
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        output = []
        for row in value:
            if isinstance(row, Mapping):
                output.append(
                    {
                        "key": str(row.get("key") or row.get("name") or "UNKNOWN").upper(),
                        "label": str(row.get("label") or row.get("name") or "資料不足"),
                        "active": bool(row.get("active", True)),
                    }
                )
            else:
                output.append({"key": str(row).upper(), "label": str(row), "active": True})
        return output
    return []


def _hard_wait_reason(blockers: set[str]) -> tuple[str, str]:
    if "liquidity" in blockers:
        return "LIQUIDITY", "等待流動性恢復"
    if blockers & {"spread", "slippage", "execution_cost"}:
        return "EXECUTION_RISK", "等待價差、滑價與成交成本恢復"
    if "safety_checks" in blockers:
        return "RISK_WARNING", "留意異常行情風險"
    return "RISK_WARNING", "留意風險提醒"


def _invalidation_condition(item: Any) -> str:
    explicit = str(_read(item, "invalidation", "") or "").strip()
    if explicit:
        return explicit
    stop = _number(_read(item, "stop_loss", None))
    direction = _direction(item)
    if stop is not None:
        verb = "跌破" if direction == "LONG" else "站上" if direction == "SHORT" else "越過"
        return f"核心價格{verb}原 SL {stop:g}"
    return "失效條件資料不足；禁止自行假設"


def _stage(item: Any, lifecycle: Mapping[str, Any] | None = None) -> str:
    lifecycle = lifecycle or _mapping(_read(item, "lifecycle", {}))
    return str(
        lifecycle.get("current_stage")
        or _read(item, "signal_stage", None)
        or _read(item, "status", None)
        or "NONE"
    ).upper()


def _direction(item: Any) -> str:
    value = str(_read(item, "direction", "NEUTRAL") or "NEUTRAL").upper()
    return value if value in {"LONG", "SHORT"} else "NEUTRAL"


def _direction_label(direction: str) -> str:
    return {"LONG": "做多", "SHORT": "做空", "NEUTRAL": "中性"}[direction]


def _read(source: Any, key: str, default: Any = None) -> Any:
    if source is None:
        return default
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Sequence):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
