from __future__ import annotations

from collections.abc import Mapping
from typing import Any


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
)

_SIGNAL_FIELDS = (
    "inst_id",
    "direction",
    "strategy",
    "entry_low",
    "entry_high",
    "stop_loss",
    "take_profit_1",
    "take_profit_2",
    "risk_reward",
    "invalidation",
    "regime",
    "signal_stage",
    "readiness_score",
    "summary",
    "radar_horizon",
    "trigger_type",
    "freshness",
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


def public_report_payload(report: Any) -> dict[str, Any]:
    """Build the mobile/API projection without copying developer-only data.

    Persistence and the strategy repository continue to use the complete report.
    This projection is only for browser/API delivery, where raw indicators,
    attack-wave histories, internal API metrics and the unused long market map
    would otherwise add several megabytes to every refresh.
    """

    payload = _select(report, _REPORT_FIELDS)
    payload["market_bias"] = _select(
        _read(report, "market_bias", {}),
        ("label", "score"),
    )
    payload["data_quality"] = _select(
        _read(report, "data_quality", {}),
        ("core", "core_status", "deep", "deep_status", "missing_sources"),
    )
    payload["historical_performance"] = _public_performance(
        _read(report, "historical_performance", {})
    )
    payload["signals"] = [
        _public_candidate(item, signal=True)
        for item in _read(report, "signals", [])
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
        ("age_bars", "triggered_at"),
    )
    payload["execution_quality"] = _select(
        _read(item, "execution_quality", {}),
        ("score", "label", "recommendation"),
    )
    payload["data_quality"] = _select(
        _read(item, "data_quality", {}),
        ("core", "core_status", "deep", "deep_status", "missing_sources"),
    )
    payload["market_story"] = _public_market_story(
        _read(item, "market_story", {})
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
            ),
        )
    return payload


def _public_market_map_item(item: Any) -> dict[str, Any]:
    payload = _select(
        item,
        ("inst_id", "regime", "direction", "readiness_score", "status"),
    )
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
    return payload


def _public_evidence_groups(groups: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in ("position_structure", "trend_momentum", "participation_flow"):
        group = _select(_read(groups, key, {}), ("label", "score"))
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
        _select(item, ("passed", "label", "hard", "value"))
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
