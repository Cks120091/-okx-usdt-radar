from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .price_display import signal_plan_display_fields


_MISSING = object()

_REPORT_FIELDS = (
    "status",
    "generated_at",
    "target_count",
    "fetched_count",
    "analyzable_count",
    "coverage_pct",
    "duration_seconds",
    "message",
    "market_regime_counts",
    "context_target_count",
    "context_enriched_count",
    "completed_at",
    "runtime_status",
    "actionable",
    "signals_suppressed_reason",
    "max_signals",
    "version",
    "scan_mode",
    "short_completed_at",
    "long_completed_at",
)

_SIGNAL_FIELDS = (
    "trigger_id",
    "inst_id",
    "direction",
    "strategy",
    "entry_low",
    "entry_high",
    "stop_loss",
    "take_profit_1",
    "take_profit_2",
    "risk_reward",
    "trend_strength_label",
    "trend_strength_score",
    "invalidation",
    "quote_volume_24h",
    "regime",
    "signal_stage",
    "readiness_score",
    "summary",
    "radar_horizon",
    "trigger_type",
    "freshness",
    # Safe scalar timestamps used only to keep the browser's tie-break order
    # identical to the backend (quality -> actual data freshness -> R:R).
    "data_timestamp",
    "closed_candle_ts",
)

_WATCH_FIELDS = (
    "inst_id",
    "regime",
    "direction",
    "preferred_strategy",
    "readiness_score",
    "status",
    "summary",
    "radar_horizon",
    "freshness",
)

_CANDIDATE_METRIC_FIELDS = frozenset(
    {
        "last_price",
        "price_change_15m_pct",
        "price_change_1h_pct",
        "price_change_24h_pct",
        "volume_ratio_15m",
        "volume_ratio_5m",
        "taker_buy_pct",
        "open_interest_usd",
        "open_interest_change_pct",
        "oi_flow_state",
        "funding_rate_pct",
        "order_book_imbalance_pct",
        "estimated_round_trip_cost_pct",
        "execution_cost_to_risk_pct",
        "buy_slippage_pct",
        "sell_slippage_pct",
    }
)

_MAP_METRIC_FIELDS = frozenset(
    {
        "last_price",
        "price_change_15m_pct",
        "price_change_1h_pct",
        "price_change_24h_pct",
        "rsi_15m",
        "open_interest_usd",
        "open_interest_change_pct",
        "oi_flow_state",
        "funding_rate_pct",
    }
)

_PUBLIC_REPORT_DATA_QUALITY_FIELDS = (
    # Keep the legacy summary fields for older clients.
    "core",
    "core_status",
    "deep",
    "deep_status",
    "missing_sources",
    # ``deep_enriched_count`` only means that at least one optional context
    # source was available.  Publish the real completeness counters as well so
    # the UI never has to present that number as full depth coverage.
    "deep_candidate_limit",
    "deep_target_count",
    "deep_enriched_count",
    "deep_complete_count",
    "deep_completeness_pct",
    "deep_source_completeness_pct",
    "source_success",
    "source_missing",
    "context_failure_count",
)


def public_report_payload(report: Any) -> dict[str, Any]:
    """Build the mobile/API projection without copying developer-only data.

    Persistence and the strategy repository continue to use the complete report.
    This projection is only for browser/API delivery, where raw indicators,
    attack-wave histories, internal API metrics and the unused long market map
    would otherwise add several megabytes to every refresh.
    """

    payload = _select(report, _REPORT_FIELDS)
    market_bias = _read(report, "market_bias", {})
    payload["market_bias"] = _select(
        market_bias,
        ("label", "score", "market_breadth_long_pct", "liquid_breadth_long_pct"),
    )
    for key in ("btc", "resonance", "exposure_warning"):
        value = _read(market_bias, key, None)
        if isinstance(value, Mapping):
            payload["market_bias"][key] = dict(value)
    payload["data_quality"] = _select(
        _read(report, "data_quality", {}),
        _PUBLIC_REPORT_DATA_QUALITY_FIELDS,
    )
    payload["historical_performance"] = _public_performance(
        _read(report, "historical_performance", {})
    )
    payload["signals"] = [
        _public_candidate(item, signal=True)
        for item in _read(report, "signals", [])
    ]
    payload["closed_signals"] = [
        _public_candidate(item, signal=True)
        for item in _read(report, "closed_signals", [])
    ]
    payload["watchlist"] = [
        _public_candidate(item, signal=False)
        for item in _read(report, "watchlist", [])
    ]
    payload["market_map"] = [
        _public_market_map_item(item)
        for item in _read(report, "market_map", [])
    ]
    payload["long_signals"] = [
        _public_candidate(item, signal=True)
        for item in _read(report, "long_signals", [])
    ]
    payload["long_closed_signals"] = [
        _public_candidate(item, signal=True)
        for item in _read(report, "long_closed_signals", [])
    ]
    payload["long_watchlist"] = [
        _public_candidate(item, signal=False)
        for item in _read(report, "long_watchlist", [])
    ]
    actionable = bool(_read(report, "actionable", True))
    payload["safety"] = {
        "mode": "analysis_only",
        "auto_ordering": False,
        "paper_trading": False,
        "live_trading": False,
        "actionable": actionable,
        "max_risk_per_trade_pct": 1.0,
        "note": "Radar Signal 與是否下單分離；目前只分析，不連接私人交易 API。",
    }
    return payload


def public_candidate_payload(item: Any, *, signal: bool) -> dict[str, Any]:
    """Project one on-demand analysis item through the mobile-safe schema."""

    return _public_candidate(item, signal=signal)


def _public_candidate(item: Any, *, signal: bool) -> dict[str, Any]:
    payload = _select(item, _SIGNAL_FIELDS if signal else _WATCH_FIELDS)
    display_fields = signal_plan_display_fields(item)
    payload["instrument_tick_size"] = display_fields["instrument_tick_size"]
    payload["display_precision"] = display_fields["display_precision"]
    if signal:
        payload["tp1_r"] = display_fields["tp1_r"]
        payload["tp2_r"] = display_fields["tp2_r"]
    payload["market_metrics"] = _public_metrics(
        _read(item, "market_metrics", {}),
        _CANDIDATE_METRIC_FIELDS,
        include_order_book_reason=True,
    )
    payload["evidence_groups"] = _public_evidence_groups(
        _read(item, "evidence_groups", {})
    )
    payload["timeframe_states"] = _public_timeframes(
        _read(item, "timeframe_states", {})
    )
    payload["supporting_evidence"] = list(
        _read(item, "supporting_evidence", []) or []
    )[:6]
    payload["conflicts"] = list(_read(item, "conflicts", []) or [])[:6]
    payload["neutral_evidence"] = list(
        _read(item, "neutral_evidence", []) or []
    )[:4]
    payload["safety_checks"] = _public_safety_checks(
        _read(item, "safety_checks", [])
    )
    payload["lifecycle"] = _select(
        _read(item, "lifecycle", {}),
        (
            "age_bars",
            "triggered_at",
            "closed_at",
            "retention_until",
            "status",
            "terminal_status",
            "outcome",
            "terminal",
            "entry_ready_once",
            "entry_ready_at",
        ),
    )
    payload["execution_quality"] = _select(
        _read(item, "execution_quality", {}),
        ("score", "label", "recommendation"),
    )
    payload["management_plan"] = _select(
        _read(item, "management_plan", {}),
        (
            "adaptive_market_plan",
            "frozen_at_trigger",
            "market_strength_score",
            "market_strength_label",
            "target_method",
            "stop_method",
            "target_rr_model",
            "tp2_rr_model",
            "market_plan_sources",
            "structural_target_price",
            "structural_target_rr",
            "first_obstacle_action",
        ),
    )
    payload["data_quality"] = _select(
        _read(item, "data_quality", {}),
        ("core", "core_status", "deep", "deep_status", "missing_sources"),
    )
    payload["market_story"] = _public_market_story(
        _read(item, "market_story", {})
    )
    payload["decision_context"] = _public_decision_context(
        _read(item, "decision_context", {})
    )
    if signal:
        payload["entry_eligibility"] = _select(
            _read(item, "entry_eligibility", {}),
            (
                "status",
                "label",
                "reason",
                "chase_atr",
                "adverse_atr",
                "invalidation_progress_pct",
                "remaining_rr",
                "remaining_rr_applicable",
                "new_entry_allowed",
                "direction_still_valid",
                "hard_blockers",
                "risk_warnings",
                "wait_reason_code",
            ),
        )
    return payload


def _public_market_map_item(item: Any) -> dict[str, Any]:
    payload = _select(
        item,
        ("inst_id", "regime", "direction", "readiness_score", "status"),
    )
    display_fields = signal_plan_display_fields(item)
    payload["instrument_tick_size"] = display_fields["instrument_tick_size"]
    payload["display_precision"] = display_fields["display_precision"]
    payload["market_metrics"] = _public_metrics(
        _read(item, "market_metrics", {}),
        _MAP_METRIC_FIELDS,
    )
    return payload


def _public_metrics(
    metrics: Any,
    fields: frozenset[str],
    *,
    include_order_book_reason: bool = False,
) -> dict[str, Any]:
    payload = _select(metrics, fields)
    if include_order_book_reason:
        sequence = _select(
            _read(metrics, "order_book_sequence", {}),
            ("reason",),
        )
        if sequence:
            payload["order_book_sequence"] = sequence
    return payload


def _public_market_story(story: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, fields in (
        ("where", ("label",)),
        ("price_acceptance", ("label",)),
        (
            "control_transfer",
            ("label", "push_away", "micro_defense_broken"),
        ),
        ("trigger", ("type", "event_age_bars")),
    ):
        section = _select(_read(story, key, {}), fields)
        if section:
            payload[key] = section
    attacks: dict[str, Any] = {}
    attack_source = _read(story, "attack_efficiency", {})
    for direction in ("BULL", "BEAR"):
        attack = _select(_read(attack_source, direction, {}), ("label",))
        if attack:
            attacks[direction] = attack
    if attacks:
        payload["attack_efficiency"] = attacks
    for key in ("context", "interpretation"):
        value = _read(story, key, None)
        if isinstance(value, Mapping):
            payload[key] = dict(value)
    return payload


def _public_decision_context(decision: Any) -> dict[str, Any]:
    if not isinstance(decision, Mapping):
        return {}
    payload = _select(decision, ("schema_version",))
    hard_gate = _read(decision, "hard_gate", {})
    payload["hard_gate"] = _select(
        hard_gate,
        (
            "status",
            "passed",
            "blocked",
            "unknown",
            "blockers",
            "unknowns",
            "reasons",
            "thresholds",
            "advisory_only",
            "entry_veto_enabled",
        ),
    )
    payload["hard_gate"]["checks"] = [
        _select(
            item,
            ("key", "label", "status", "value", "reason", "hard"),
        )
        for item in (_read(hard_gate, "checks", []) or [])
        if isinstance(item, Mapping)
    ]
    evidence = _read(decision, "evidence", {})
    payload["evidence"] = _select(evidence, ("main_direction", "direction_quality", "participation", "supporting"))
    payload["market_context"] = _select(
        _read(decision, "market_context", {}),
        ("regime", "phase", "driver", "relative_strength", "resonance", "sessions", "anomalies", "main_direction"),
    )
    payload["conflict"] = _select(
        _read(decision, "conflict", {}),
        (
            "main_direction",
            "level",
            "label",
            "items",
            "domains",
            "blocking_domains",
            "blocks_entry",
            "severity_score",
            "countertrend",
            "opposite_signal_created",
        ),
    )
    continuation = _read(decision, "continuation_confirmation", {})
    payload["continuation_confirmation"] = _select(
        continuation,
        (
            "key",
            "label",
            "score",
            "supporting",
            "conflicts",
            "missing",
            "warnings",
            "meaning",
        ),
    )
    core_votes = _read(continuation, "core_votes", {})
    payload["continuation_confirmation"]["core_votes"] = {
        key: _select(value, ("state", "label", "detail"))
        for key in ("OI", "TAKER_CVD", "VOLUME")
        if isinstance((value := _read(core_votes, key, None)), Mapping)
    }
    payload["quality"] = _select(
        _read(decision, "quality", {}),
        ("direction", "execution", "participation", "combined_score", "note"),
    )
    payload["confidence"] = _select(
        _read(decision, "confidence", {}),
        ("key", "label", "reasons", "meaning"),
    )
    payload["episode"] = _select(
        _read(decision, "episode", {}),
        ("status", "label", "trend", "arrow", "source_stage", "transition", "trigger_id", "terminal"),
    )
    payload["final"] = _select(
        _read(decision, "final", {}),
        ("status", "label", "direction", "direction_label", "new_entry_allowed", "trigger_preserved", "reasons", "wait_reason", "weakening_conditions", "invalidation_condition", "confidence", "warnings"),
    )
    return payload


def _public_evidence_groups(groups: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in ("position_structure", "trend_momentum", "participation_flow"):
        group = _select(
            _read(groups, key, {}),
            ("label", "score", "stance", "confidence"),
        )
        if group:
            payload[key] = group
    return payload


def _public_timeframes(frames: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in ("1D", "4H", "1H", "15m", "5m"):
        frame = _select(_read(frames, key, {}), ("label",))
        if frame:
            payload[key] = frame
    return payload


def _public_safety_checks(checks: Any) -> list[dict[str, Any]]:
    return [
        _select(
            item,
            ("key", "status", "passed", "label", "hard", "value", "reason"),
        )
        for item in (checks or [])
        if isinstance(item, Mapping)
    ]


def _public_performance(performance: Any) -> dict[str, Any]:
    return _select(performance, ("available", "note", "overall"))


def _select(source: Any, keys: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in keys:
        value = _read(source, key, _MISSING)
        if value is not _MISSING:
            payload[key] = value
    return payload


def _read(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)
