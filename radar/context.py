from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo


_FIVE_MINUTES_MS = 300_000
_FLOW_STATES = {
    "STRENGTHENING",
    "STABLE",
    "WEAKENING",
    "MIXED",
    "UNKNOWN",
}

_SESSION_SPECS = (
    ("ASIA", "亞洲盤", "Asia Session", "Asia/Taipei", 8, 15),
    ("LONDON", "倫敦盤", "London Session", "Europe/London", 8, 17),
    ("NEW_YORK", "紐約盤", "New York Session", "America/New_York", 8, 17),
)

_REGIME_LABELS = {
    "TREND": ("趨勢行情", "Trend"),
    "RANGE": ("盤整行情", "Range"),
    "COMPRESSION": ("壓縮行情", "Compression"),
    "TRANSITION": ("轉折行情", "Transition"),
    "BREAKOUT": ("突破行情", "Breakout"),
    "UNKNOWN": ("行情未知", "Unknown"),
}

_PHASE_LABELS = {
    "FORMING": ("訊號形成中", "Forming"),
    "BREAKOUT": ("突破階段", "Breakout"),
    "RETEST": ("回踩階段", "Retest"),
    "CONTINUATION": ("延續階段", "Continuation"),
    "REVERSAL": ("轉折階段", "Reversal"),
    "MATURE": ("成熟階段", "Mature"),
    "WEAKENING": ("轉弱階段", "Weakening"),
    "UNKNOWN": ("階段未知", "Unknown"),
}

_VOLATILITY_LABELS = {
    "NORMAL": ("一般波動", "Normal Volatility"),
    "HIGH": ("高波動", "High Volatility"),
    "ANOMALOUS": ("異常波動", "Anomalous Volatility"),
    "UNKNOWN": ("波動未知", "Unknown Volatility"),
}

_DRIVER_LABELS = {
    "BTC_DRIVEN": ("BTC／大盤帶動", "BTC-driven"),
    "INDEPENDENT": ("個幣獨立行情", "Independent"),
    "MARKET_RESONANCE": ("市場共振", "Market Resonance"),
    "UNKNOWN": ("市場來源未知", "Unknown Driver"),
}

_ANOMALY_LABELS = {
    "NORMAL": "行情正常",
    "WATCH": "異常風險觀察",
    "BLOCK": "異常行情｜等待穩定",
    "UNKNOWN": "異常狀態未知",
}

_DEFAULT_ANOMALY_THRESHOLDS = {
    "wick_atr_watch": 1.8,
    "wick_atr_block": 3.0,
    "range_atr_watch": 2.5,
    "range_atr_block": 4.0,
    "volume_ratio_watch": 3.0,
    "volume_ratio_block": 6.0,
    "oi_velocity_watch_pct_per_5m": 4.0,
    "oi_velocity_block_pct_per_5m": 8.0,
    "funding_rate_watch": 0.0005,
    "funding_rate_block": 0.001,
    "spread_pct_watch": 0.12,
    "spread_pct_block": 0.30,
    "slippage_pct_watch": 0.15,
    "slippage_pct_block": 0.35,
}


def active_sessions(now: datetime) -> list[dict[str, Any]]:
    """Return all three trading sessions at ``now`` using real timezone rules.

    London and New York are defined in their own local civil time, therefore
    ``zoneinfo`` applies daylight-saving transitions.  The returned display
    window is always Taipei/Hong Kong time (UTC+8), and more than one session
    may be active at once.
    """

    instant = _aware_datetime(now)
    taipei = ZoneInfo("Asia/Taipei")
    output: list[dict[str, Any]] = []
    for key, label, label_en, zone_name, start_hour, end_hour in _SESSION_SPECS:
        zone = ZoneInfo(zone_name)
        local_now = instant.astimezone(zone)
        start_local = local_now.replace(
            hour=start_hour,
            minute=0,
            second=0,
            microsecond=0,
        )
        end_local = local_now.replace(
            hour=end_hour,
            minute=0,
            second=0,
            microsecond=0,
        )
        start_utc8 = start_local.astimezone(taipei)
        end_utc8 = end_local.astimezone(taipei)
        output.append(
            {
                "key": key,
                "label": label,
                "label_en": label_en,
                "active": start_local <= local_now < end_local,
                "timezone": zone_name,
                "utc8_window": (
                    f"{start_utc8:%H:%M}\u2013{end_utc8:%H:%M}"
                ),
                "utc8_start": start_utc8.isoformat(),
                "utc8_end": end_utc8.isoformat(),
            }
        )
    return output


def summarize_flow_history(
    samples: Iterable[Mapping[str, Any]],
    direction: str,
) -> dict[str, Any]:
    """Summarize direction-aware flow changes without creating a Trigger.

    A conclusion requires at least three distinct samples.  Every metric must
    cover the same first/last sample window; partial windows remain UNKNOWN.
    Velocities use the actual elapsed milliseconds and are normalized to five
    minutes, so irregular scan intervals do not masquerade as acceleration.
    """

    normalized_direction = str(direction or "").strip().upper()
    prepared = _prepare_samples(samples)
    consistent_window, declared_window = _consistent_declared_window(prepared)
    window = _window_payload(prepared, consistent_window, declared_window)
    if (
        normalized_direction not in {"LONG", "SHORT"}
        or len(prepared) < 3
        or not consistent_window
    ):
        return _unknown_flow_summary(
            normalized_direction,
            len(prepared),
            window,
        )

    sign = 1.0 if normalized_direction == "LONG" else -1.0
    price_points = _aligned_points(prepared, _price)

    oi_points = _aligned_points(
        prepared,
        lambda sample: _value(sample, "open_interest_usd", "open_interest", "oi"),
    )
    oi = _oi_summary(oi_points, price_points, sign)

    taker_points = _aligned_points(
        prepared,
        lambda sample: _directional_taker(sample, normalized_direction),
    )
    taker = _share_summary(
        taker_points,
        epsilon_points=3.0,
        abnormal_points_per_5m=30.0,
        velocity_key="velocity_per_5m_pp",
    )

    funding_points = _aligned_points(
        prepared,
        lambda sample: _signed_value(
            _value(sample, "funding_rate", "funding"),
            sign,
        ),
    )
    funding = _funding_summary(funding_points)

    depth_points = _aligned_points(
        prepared,
        lambda sample: _directional_depth_share(sample, normalized_direction),
    )
    raw_depth_points = _aligned_points(
        prepared,
        lambda sample: _directional_depth(sample, normalized_direction),
    )
    depth = _depth_summary(depth_points, raw_depth_points)

    book_points = _aligned_points(
        prepared,
        lambda sample: _signed_value(
            _value(sample, "order_book_imbalance", "book_imbalance"),
            sign,
        ),
    )
    book = _share_summary(
        book_points,
        epsilon_points=5.0,
        abnormal_points_per_5m=50.0,
        velocity_key="velocity_per_5m_pp",
    )

    components = {
        "oi": oi,
        "taker": taker,
        "funding": funding,
        "depth": depth,
        "book": book,
    }
    aggregate = _aggregate_flow_state(components)
    abnormal = any(
        component.get("abnormal_speed") is True
        for component in components.values()
    )
    return {
        "state": aggregate,
        "label": _flow_label(aggregate),
        "direction": normalized_direction,
        "valid_sample_count": len(prepared),
        "window": window,
        **components,
        "abnormal_speed": abnormal,
        "permission": "CONTEXT_ONLY_NEVER_CREATES_OR_CANCELS_TRIGGER",
    }


def detect_anomaly(
    metrics: Mapping[str, Any] | None,
    flow_summary: Mapping[str, Any] | None,
    thresholds: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify abnormal execution conditions; BLOCK only blocks new entry."""

    source = metrics or {}
    flow = flow_summary or {}
    limits = dict(_DEFAULT_ANOMALY_THRESHOLDS)
    for key, value in (thresholds or {}).items():
        numeric = _finite(value)
        if key in limits and numeric is not None and numeric >= 0:
            limits[key] = numeric

    reasons: list[dict[str, str]] = []

    def add(code: str, label: str, severity: str) -> None:
        reasons.append({"code": code, "label": label, "severity": severity})

    wick_atr = _first_number(source, "wick_atr", "max_wick_atr")
    if wick_atr is not None:
        if wick_atr >= limits["wick_atr_block"]:
            add("PRICE_WICK", "瞬間插針幅度異常", "BLOCK")
        elif wick_atr >= limits["wick_atr_watch"]:
            add("PRICE_WICK", "插針幅度偏高", "WATCH")

    range_atr = _first_number(source, "range_atr", "candle_range_atr")
    if range_atr is not None:
        if range_atr >= limits["range_atr_block"]:
            add("PRICE_JUMP", "價格瞬間跳動異常", "BLOCK")
        elif range_atr >= limits["range_atr_watch"]:
            add("PRICE_JUMP", "價格波幅快速擴大", "WATCH")

    volume_ratio = _first_number(
        source,
        "volume_ratio",
        "volume_ratio_core",
        "volume_ratio_15m",
        "volume_ratio_4h",
    )
    if volume_ratio is not None:
        if volume_ratio >= limits["volume_ratio_block"]:
            add("EXTREME_VOLUME", "瞬間巨量異常", "BLOCK")
        elif volume_ratio >= limits["volume_ratio_watch"]:
            add("EXTREME_VOLUME", "成交量快速放大", "WATCH")

    oi_section = flow.get("oi") if isinstance(flow.get("oi"), Mapping) else {}
    oi_velocity = _finite(oi_section.get("velocity_per_5m_pct"))
    if oi_velocity is not None:
        absolute_oi_velocity = abs(oi_velocity)
        if absolute_oi_velocity >= limits["oi_velocity_block_pct_per_5m"]:
            add("OI_VELOCITY", "OI 變化速度異常", "BLOCK")
        elif absolute_oi_velocity >= limits["oi_velocity_watch_pct_per_5m"]:
            add("OI_VELOCITY", "OI 變化速度偏快", "WATCH")
    elif oi_section.get("abnormal_speed") is True:
        add("OI_VELOCITY", "OI 變化速度異常", "BLOCK")

    funding_rate = _first_number(source, "funding_rate")
    if funding_rate is None:
        funding_pct = _first_number(source, "funding_rate_pct")
        funding_rate = funding_pct / 100.0 if funding_pct is not None else None
    if funding_rate is not None:
        if abs(funding_rate) >= limits["funding_rate_block"]:
            add("FUNDING_EXTREME", "Funding（資金費率）極端擁擠", "BLOCK")
        elif abs(funding_rate) >= limits["funding_rate_watch"]:
            add("FUNDING_CROWDED", "Funding（資金費率）偏擁擠", "WATCH")

    spread = _first_number(source, "spread_pct")
    if spread is not None:
        if spread >= limits["spread_pct_block"]:
            add("SPREAD", "Spread（買賣價差）異常", "BLOCK")
        elif spread >= limits["spread_pct_watch"]:
            add("SPREAD", "Spread（買賣價差）偏高", "WATCH")

    slippage_values = [
        value
        for value in (
            _first_number(source, "slippage_pct"),
            _first_number(source, "buy_slippage_pct"),
            _first_number(source, "sell_slippage_pct"),
        )
        if value is not None
    ]
    if slippage_values:
        slippage = max(abs(value) for value in slippage_values)
        if slippage >= limits["slippage_pct_block"]:
            add("SLIPPAGE", "Slippage（滑價）異常", "BLOCK")
        elif slippage >= limits["slippage_pct_watch"]:
            add("SLIPPAGE", "Slippage（滑價）偏高", "WATCH")

    depth = flow.get("depth") if isinstance(flow.get("depth"), Mapping) else {}
    sequence = (
        source.get("order_book_sequence")
        if isinstance(source.get("order_book_sequence"), Mapping)
        else {}
    )
    if depth.get("withdrawal") is True or sequence.get("state") == "LIQUIDITY_WITHDRAWAL":
        add("DEPTH_WITHDRAWAL", "Order Book（訂單簿）深度快速撤離", "BLOCK")

    required_missing = _unique_strings(
        [
            *_string_list(source.get("required_missing_sources")),
            *_string_list(source.get("missing_sources")),
        ]
    )
    failures = _string_list(source.get("api_failures")) or _string_list(source.get("failures"))
    optional_missing = _string_list(source.get("optional_missing_sources"))
    if required_missing:
        add("REQUIRED_DATA_MISSING", "必要即時資料缺失", "BLOCK")
    if failures:
        add("API_FAILURE", "API 即時資料抓取失敗", "BLOCK")
    if optional_missing:
        add("PARTIAL_DATA", "非必要情境資料未知", "WATCH")

    observed_checks = {
        "price_wick": wick_atr is not None,
        "price_range": range_atr is not None,
        "volume": volume_ratio is not None,
        "oi_velocity": oi_velocity is not None
        or oi_section.get("abnormal_speed") is not None,
        "funding": funding_rate is not None,
        "spread": spread is not None,
        "slippage": bool(slippage_values),
        "order_book_depth": isinstance(depth.get("withdrawal"), bool)
        or bool(sequence.get("state")),
    }
    available_checks = [
        key for key, available in observed_checks.items() if available
    ]
    unknown_checks = [
        key for key, available in observed_checks.items() if not available
    ]
    if not available_checks:
        coverage_status = "UNKNOWN"
    elif not unknown_checks:
        coverage_status = "COMPLETE"
    else:
        coverage_status = "PARTIAL"
    coverage = {
        "status": coverage_status,
        "available": available_checks,
        "unknown": unknown_checks,
        "available_count": len(available_checks),
        "total_count": len(observed_checks),
        "coverage_pct": round(len(available_checks) / len(observed_checks) * 100.0, 1),
        "required_missing_sources": required_missing,
    }

    reasons = _deduplicate_reasons(reasons)
    severity_rank = {"NORMAL": 0, "WATCH": 1, "BLOCK": 2}
    # Absence of measurements is not proof that the market is normal.  It is
    # explicitly UNKNOWN unless the caller identified the missing source as a
    # required input, in which case the severity remains BLOCK as a prominent
    # warning. Partial optional coverage may still be NORMAL when every
    # available check is within limits, but the coverage remains visible.
    status = "UNKNOWN" if coverage_status == "UNKNOWN" and not reasons else "NORMAL"
    for reason in reasons:
        if status == "UNKNOWN" or severity_rank[reason["severity"]] > severity_rank[status]:
            status = reason["severity"]
    return {
        "status": status,
        "label": _ANOMALY_LABELS[status],
        "reasons": reasons,
        "coverage": coverage,
        "entry_block": False,
        "entry_permission": "ADVISORY_ONLY",
        "may_create_trigger": False,
        "may_cancel_trigger": False,
    }


def classify_market_driver(
    symbol_change: Any,
    btc_change: Any,
    participation: Mapping[str, Any] | str | None,
    breadth: Mapping[str, Any] | float | int | None,
    resonance: Mapping[str, Any] | float | int | bool | None,
) -> dict[str, Any]:
    """Describe market origin without granting or removing trade permission."""

    symbol_value = _finite(symbol_change)
    btc_value = _finite(btc_change)
    if symbol_value is None or btc_value is None:
        return _driver_payload(
            "UNKNOWN",
            None,
            "個幣或 BTC 同區間變化資料不足",
            "UNKNOWN",
        )

    relative_strength = round(symbol_value - btc_value, 2)
    own_participation = _participation_supports(participation)
    resonance_active = _resonance_active(resonance)
    breadth_ratio = _breadth_ratio(breadth)
    breadth_aligned = (
        breadth_ratio is None
        or (symbol_value > 0 and breadth_ratio >= 0.60)
        or (symbol_value < 0 and breadth_ratio <= 0.40)
    )

    same_direction = (
        symbol_value != 0
        and btc_value != 0
        and math.copysign(1.0, symbol_value) == math.copysign(1.0, btc_value)
    )
    btc_material = abs(btc_value) >= 1.0
    follows_without_outperformance = abs(symbol_value) <= abs(btc_value) * 1.25

    if resonance_active and breadth_aligned:
        key = "MARKET_RESONANCE"
        reason = "同方向標的同步增加，較像市場共振而非單一獨立機會"
        confidence = "HIGH" if breadth_ratio is not None else "MEDIUM"
    elif (
        btc_material
        and same_direction
        and follows_without_outperformance
        and not own_participation
    ):
        key = "BTC_DRIVEN"
        reason = "走勢跟隨 BTC，但自身 OI／Taker／Volume 尚未確認"
        confidence = "MEDIUM"
    elif own_participation and (
        not same_direction or abs(relative_strength) >= 0.75
    ):
        key = "INDEPENDENT"
        reason = "相對 BTC 有明顯差異，且自身市場參與提供支持"
        confidence = "HIGH"
    else:
        key = "UNKNOWN"
        reason = "目前不足以區分 BTC 帶動、市場共振或個幣獨立行情"
        confidence = "LOW"

    return _driver_payload(key, relative_strength, reason, confidence)


def build_market_context(
    *,
    regime: Mapping[str, Any] | str | None,
    phase: Mapping[str, Any] | str | None,
    volatility: Mapping[str, Any] | str | None,
    anomaly: Mapping[str, Any] | None,
    driver: Mapping[str, Any] | None,
    sessions: Iterable[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Build the compact serializable Market Context schema."""

    anomaly_payload = _anomaly_projection(anomaly)
    driver_payload = _driver_projection(driver)
    session_items = [_session_projection(item) for item in (sessions or [])]
    session_items = [item for item in session_items if item]
    return {
        "regime": _enum_projection(regime, _REGIME_LABELS),
        "phase": _enum_projection(phase, _PHASE_LABELS),
        "volatility": _enum_projection(volatility, _VOLATILITY_LABELS),
        "anomaly": anomaly_payload,
        "market_driver": driver_payload,
        "sessions": {
            "active": [item["key"] for item in session_items if item["active"]],
            "items": session_items,
        },
        "permission": "CONTEXT_ONLY_NEVER_CREATES_OR_CANCELS_TRIGGER",
    }


def build_interpretation(
    *,
    evidence_groups: Mapping[str, Any] | None,
    flow_summary: Mapping[str, Any] | None,
    anomaly: Mapping[str, Any] | None,
    main_conflicts: Iterable[str] | None = None,
    change_conditions: Mapping[str, Iterable[str]] | None = None,
    data_quality: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build direction/confidence interpretation without changing a Trigger."""

    groups = evidence_groups or {}
    direction_groups = [
        groups.get("position_structure"),
        groups.get("trend_momentum"),
    ]
    valid_direction_groups = [
        group for group in direction_groups if _group_is_available(group)
    ]
    scores = [
        score
        for score in (_group_score(group) for group in valid_direction_groups)
        if score is not None
    ]
    direction_score = round(sum(scores) / len(scores)) if scores else None
    direction_label = (
        "未知"
        if direction_score is None
        else "高"
        if direction_score >= 70
        else "中"
        if direction_score >= 50
        else "低"
    )
    direction_reasons = [
        f"{str(group.get('label', '證據'))}：{_stance_label(group.get('stance'))}"
        for group in valid_direction_groups
        if isinstance(group, Mapping) and group.get("stance")
    ][:3]

    conflicts = _unique_strings(main_conflicts)
    for group in groups.values():
        if _group_is_available(group):
            conflicts.extend(_string_list(group.get("conflicts")))
    anomaly_status = str((anomaly or {}).get("status", "UNKNOWN")).upper()
    if anomaly_status in {"WATCH", "BLOCK"}:
        for reason in (anomaly or {}).get("reasons", []):
            if isinstance(reason, Mapping) and reason.get("label"):
                conflicts.append(str(reason["label"]))
    conflicts = _unique_strings(conflicts)[:4]

    flow_state = str((flow_summary or {}).get("state", "UNKNOWN")).upper()
    if flow_state not in _FLOW_STATES:
        flow_state = "UNKNOWN"
    valid_groups = {
        key: group
        for key, group in groups.items()
        if _group_is_available(group)
    }
    evidence_coverage = _evidence_coverage(groups, valid_groups)
    confidence = _confidence_label(
        valid_groups,
        conflicts,
        anomaly_status,
        data_quality or {},
    )
    conditions = change_conditions or {}
    return {
        "direction_quality": {
            "score": direction_score,
            "label": direction_label,
            "reasons": direction_reasons,
        },
        "confidence": {
            "key": confidence,
            "label": {
                "HIGH": "高",
                "MEDIUM": "中",
                "LOW": "低",
                "UNKNOWN": "未知",
            }[confidence],
        },
        "evidence_coverage": evidence_coverage,
        "participation_trend": {
            "state": flow_state,
            "label": _flow_label(flow_state),
        },
        "main_conflicts": conflicts,
        "change_conditions": {
            "weaken": _unique_strings(conditions.get("weaken"))[:3],
            "invalidate": _unique_strings(conditions.get("invalidate"))[:3],
        },
        "trigger_permission": "NEVER_CREATES_OR_CANCELS_TRIGGER",
        "entry_permission": "ADVISORY_ONLY",
    }


def _aware_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("now must be a datetime")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _first_number(source: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        number = _finite(source.get(key))
        if number is not None:
            return number
    return None


def _value(sample: Mapping[str, Any], *keys: str) -> float | None:
    return _first_number(sample, *keys)


def _timestamp_ms(sample: Mapping[str, Any]) -> int | None:
    value = _first_number(sample, "timestamp_ms", "sampled_at", "timestamp", "ts")
    if value is None or value < 0:
        return None
    return int(value)


def _prepare_samples(samples: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_timestamp: dict[int, dict[str, Any]] = {}
    for sample in samples or []:
        if not isinstance(sample, Mapping):
            continue
        timestamp = _timestamp_ms(sample)
        if timestamp is None:
            continue
        by_timestamp.setdefault(timestamp, dict(sample))
    return [by_timestamp[key] for key in sorted(by_timestamp)]


def _declared_window(sample: Mapping[str, Any]) -> int | None:
    value = _first_number(sample, "window_ms", "interval_ms", "timeframe_ms")
    return int(value) if value is not None and value > 0 else None


def _consistent_declared_window(
    samples: list[Mapping[str, Any]],
) -> tuple[bool, int | None]:
    declared = [_declared_window(sample) for sample in samples]
    windows = {value for value in declared if value is not None}
    if len(windows) > 1:
        return False, None
    if windows and any(value is None for value in declared):
        return False, None
    return True, next(iter(windows), None)


def _window_payload(
    samples: list[Mapping[str, Any]],
    consistent: bool,
    declared_window: int | None,
) -> dict[str, Any]:
    if not samples:
        return {
            "start_ms": None,
            "end_ms": None,
            "duration_ms": None,
            "declared_window_ms": declared_window,
            "consistent": consistent,
        }
    start = _timestamp_ms(samples[0])
    end = _timestamp_ms(samples[-1])
    return {
        "start_ms": start,
        "end_ms": end,
        "duration_ms": end - start if start is not None and end is not None else None,
        "declared_window_ms": declared_window,
        "consistent": consistent,
    }


def _aligned_points(
    samples: list[Mapping[str, Any]],
    extractor: Callable[[Mapping[str, Any]], float | None],
) -> list[tuple[int, float]] | None:
    points = [
        (timestamp, value)
        for sample in samples
        if (timestamp := _timestamp_ms(sample)) is not None
        and (value := extractor(sample)) is not None
    ]
    if len(points) < 3:
        return None
    first_ts = _timestamp_ms(samples[0])
    last_ts = _timestamp_ms(samples[-1])
    if points[0][0] != first_ts or points[-1][0] != last_ts:
        return None
    if points[-1][0] <= points[0][0]:
        return None
    return points


def _price(sample: Mapping[str, Any]) -> float | None:
    direct = _value(sample, "mid_price", "price", "last_price", "close")
    if direct is not None and direct > 0:
        return direct
    bid = _value(sample, "best_bid", "bid")
    ask = _value(sample, "best_ask", "ask")
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (bid + ask) / 2.0


def _signed_value(value: float | None, sign: float) -> float | None:
    return value * sign if value is not None else None


def _directional_taker(
    sample: Mapping[str, Any],
    direction: str,
) -> float | None:
    ratio = _value(sample, "taker_buy_ratio", "taker_ratio")
    if ratio is None or not 0.0 <= ratio <= 1.0:
        return None
    return ratio if direction == "LONG" else 1.0 - ratio


def _directional_depth(
    sample: Mapping[str, Any],
    direction: str,
) -> float | None:
    return _value(
        sample,
        "bid_depth_usd" if direction == "LONG" else "ask_depth_usd",
        "bid_depth" if direction == "LONG" else "ask_depth",
    )


def _directional_depth_share(
    sample: Mapping[str, Any],
    direction: str,
) -> float | None:
    bid = _value(sample, "bid_depth_usd", "bid_depth")
    ask = _value(sample, "ask_depth_usd", "ask_depth")
    if bid is None or ask is None or bid < 0 or ask < 0 or bid + ask <= 0:
        return None
    return bid / (bid + ask) if direction == "LONG" else ask / (bid + ask)


def _velocity(first: float, last: float, elapsed_ms: int) -> float | None:
    if elapsed_ms <= 0:
        return None
    return (last - first) * (_FIVE_MINUTES_MS / elapsed_ms)


def _percent_velocity(
    first: float,
    last: float,
    elapsed_ms: int,
) -> float | None:
    if first <= 0 or elapsed_ms <= 0:
        return None
    return ((last - first) / first * 100.0) * (_FIVE_MINUTES_MS / elapsed_ms)


def _sequence_state(values: list[float], epsilon: float) -> str:
    net = values[-1] - values[0]
    steps = [right - left for left, right in zip(values, values[1:])]
    positive = sum(step > epsilon / 2.0 for step in steps)
    negative = sum(step < -epsilon / 2.0 for step in steps)
    if abs(net) <= epsilon:
        return "MIXED" if positive and negative else "STABLE"
    required = max(1, math.ceil(len(steps) * 0.60))
    if net > epsilon and positive >= required:
        return "STRENGTHENING"
    if net < -epsilon and negative >= required:
        return "WEAKENING"
    return "MIXED"


def _oi_summary(
    points: list[tuple[int, float]] | None,
    price_points: list[tuple[int, float]] | None,
    direction_sign: float,
) -> dict[str, Any]:
    if points is None or price_points is None:
        return _unknown_component()
    if points[0][0] != price_points[0][0] or points[-1][0] != price_points[-1][0]:
        return _unknown_component()
    timestamps = [point[0] for point in points]
    values = [point[1] for point in points]
    elapsed = timestamps[-1] - timestamps[0]
    raw_change = (
        (values[-1] - values[0]) / values[0] * 100.0
        if values[0] > 0
        else None
    )
    velocity = _percent_velocity(values[0], values[-1], elapsed)
    price_values = [point[1] for point in price_points]
    directional_price_change = (
        (price_values[-1] - price_values[0]) / price_values[0] * 100.0 * direction_sign
        if price_values[0] > 0
        else None
    )
    if raw_change is None or directional_price_change is None:
        return _unknown_component()
    raw_state = (
        "INCREASING"
        if raw_change >= 0.25
        else "DECREASING"
        if raw_change <= -0.25
        else "STABLE"
    )
    if raw_state == "INCREASING":
        state = (
            "STRENGTHENING"
            if directional_price_change > 0.05
            else "WEAKENING"
            if directional_price_change < -0.05
            else "MIXED"
        )
        alignment = (
            "SAME_DIRECTION_BUILD"
            if state == "STRENGTHENING"
            else "OPPOSITE_BUILD"
            if state == "WEAKENING"
            else "UNRESOLVED_BUILD"
        )
    elif raw_state == "DECREASING":
        state = "WEAKENING" if directional_price_change <= 0.05 else "MIXED"
        alignment = "POSITION_EXIT"
    else:
        state, alignment = "STABLE", "STABLE"
    return {
        "state": state,
        "raw_trend": raw_state,
        "alignment": alignment,
        "change_pct": round(raw_change, 2),
        "directional_price_change_pct": round(directional_price_change, 2),
        "velocity_per_5m_pct": round(velocity, 2) if velocity is not None else None,
        "abnormal_speed": abs(velocity) >= 8.0 if velocity is not None else None,
    }


def _share_summary(
    points: list[tuple[int, float]] | None,
    *,
    epsilon_points: float,
    abnormal_points_per_5m: float,
    velocity_key: str,
) -> dict[str, Any]:
    if points is None:
        return _unknown_component(velocity_key)
    timestamps = [point[0] for point in points]
    values = [point[1] for point in points]
    percentage_points = [value * 100.0 for value in values]
    state = _sequence_state(percentage_points, epsilon_points)
    velocity = _velocity(percentage_points[0], percentage_points[-1], timestamps[-1] - timestamps[0])
    return {
        "state": state,
        "change_pp": round(percentage_points[-1] - percentage_points[0], 1),
        velocity_key: round(velocity, 1) if velocity is not None else None,
        "abnormal_speed": (
            abs(velocity) >= abnormal_points_per_5m
            if velocity is not None
            else None
        ),
    }


def _funding_summary(
    points: list[tuple[int, float]] | None,
) -> dict[str, Any]:
    if points is None:
        return _unknown_component("velocity_per_5m_bps")
    timestamps = [point[0] for point in points]
    values = [point[1] for point in points]
    bps = [value * 10_000.0 for value in values]
    state = _sequence_state(bps, 0.5)
    crowded = values[-1] >= 0.0008
    if crowded and state == "STRENGTHENING":
        state = "MIXED"
    velocity = _velocity(bps[0], bps[-1], timestamps[-1] - timestamps[0])
    return {
        "state": state,
        "change_bps": round(bps[-1] - bps[0], 2),
        "velocity_per_5m_bps": round(velocity, 2) if velocity is not None else None,
        "crowded": crowded,
        "abnormal_speed": abs(velocity) >= 3.0 if velocity is not None else None,
    }


def _depth_summary(
    share_points: list[tuple[int, float]] | None,
    raw_points: list[tuple[int, float]] | None,
) -> dict[str, Any]:
    if share_points is None or raw_points is None:
        return _unknown_component("velocity_per_5m_pct")
    if share_points[0][0] != raw_points[0][0] or share_points[-1][0] != raw_points[-1][0]:
        return _unknown_component("velocity_per_5m_pct")
    state_payload = _share_summary(
        share_points,
        epsilon_points=3.0,
        abnormal_points_per_5m=35.0,
        velocity_key="share_velocity_per_5m_pp",
    )
    raw_values = [point[1] for point in raw_points]
    elapsed = raw_points[-1][0] - raw_points[0][0]
    raw_change = (
        (raw_values[-1] - raw_values[0]) / raw_values[0] * 100.0
        if raw_values[0] > 0
        else None
    )
    raw_velocity = _percent_velocity(raw_values[0], raw_values[-1], elapsed)
    withdrawal = raw_change is not None and raw_change <= -45.0
    if withdrawal:
        state_payload["state"] = "WEAKENING"
    state_payload.update(
        {
            "change_pct": round(raw_change, 1) if raw_change is not None else None,
            "velocity_per_5m_pct": round(raw_velocity, 1) if raw_velocity is not None else None,
            "withdrawal": withdrawal,
            "abnormal_speed": (
                abs(raw_velocity) >= 60.0 if raw_velocity is not None else None
            ),
        }
    )
    return state_payload


def _unknown_component(velocity_key: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "state": "UNKNOWN",
        "abnormal_speed": None,
    }
    if velocity_key:
        payload[velocity_key] = None
    return payload


def _unknown_flow_summary(
    direction: str,
    sample_count: int,
    window: dict[str, Any],
) -> dict[str, Any]:
    return {
        "state": "UNKNOWN",
        "label": _flow_label("UNKNOWN"),
        "direction": direction,
        "valid_sample_count": sample_count,
        "window": window,
        "oi": _unknown_component("velocity_per_5m_pct"),
        "taker": _unknown_component("velocity_per_5m_pp"),
        "funding": _unknown_component("velocity_per_5m_bps"),
        "depth": _unknown_component("velocity_per_5m_pct"),
        "book": _unknown_component("velocity_per_5m_pp"),
        "abnormal_speed": None,
        "permission": "CONTEXT_ONLY_NEVER_CREATES_OR_CANCELS_TRIGGER",
    }


def _aggregate_flow_state(components: Mapping[str, Mapping[str, Any]]) -> str:
    states = [
        str(component.get("state", "UNKNOWN"))
        for component in components.values()
        if str(component.get("state", "UNKNOWN")) != "UNKNOWN"
    ]
    if not states:
        return "UNKNOWN"
    strengthening = states.count("STRENGTHENING")
    weakening = states.count("WEAKENING")
    if strengthening and weakening:
        return "MIXED"
    if strengthening >= 2 or (strengthening == 1 and len(states) <= 2):
        return "STRENGTHENING"
    if weakening >= 2 or (weakening == 1 and len(states) <= 2):
        return "WEAKENING"
    if "MIXED" in states:
        return "MIXED"
    return "STABLE"


def _flow_label(state: str) -> str:
    return {
        "STRENGTHENING": "正在增強",
        "STABLE": "維持穩定",
        "WEAKENING": "正在轉弱",
        "MIXED": "證據分歧",
        "UNKNOWN": "資料不足",
    }.get(state, "資料不足")


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, Iterable) or isinstance(value, Mapping):
        return []
    return [str(item) for item in value if str(item).strip()]


def _unique_strings(value: Any) -> list[str]:
    return list(dict.fromkeys(_string_list(value)))


def _deduplicate_reasons(reasons: list[dict[str, str]]) -> list[dict[str, str]]:
    severity = {"WATCH": 1, "BLOCK": 2}
    by_code: dict[str, dict[str, str]] = {}
    for reason in reasons:
        previous = by_code.get(reason["code"])
        if previous is None or severity[reason["severity"]] > severity[previous["severity"]]:
            by_code[reason["code"]] = reason
    return list(by_code.values())


def _participation_supports(participation: Mapping[str, Any] | str | None) -> bool:
    if isinstance(participation, str):
        return participation.strip().upper() in {"SUPPORT", "CONFIRMED", "STRONG"}
    if not isinstance(participation, Mapping):
        return False
    state = str(participation.get("state", "")).upper()
    confirmed = participation.get("confirmed") is True
    score = _finite(participation.get("score"))
    return confirmed or state in {"SUPPORT", "CONFIRMED", "STRONG"} or (
        score is not None and score >= 60.0
    )


def _resonance_active(resonance: Mapping[str, Any] | float | int | bool | None) -> bool:
    if isinstance(resonance, bool):
        return resonance
    if isinstance(resonance, Mapping):
        if resonance.get("active") is True:
            return True
        ratio = _first_number(resonance, "ratio", "same_direction_ratio")
        return ratio is not None and (ratio >= 0.60 if ratio <= 1.0 else ratio >= 60.0)
    value = _finite(resonance)
    return value is not None and (value >= 0.60 if value <= 1.0 else value >= 60.0)


def _breadth_ratio(breadth: Mapping[str, Any] | float | int | None) -> float | None:
    if isinstance(breadth, Mapping):
        value = _first_number(
            breadth,
            "long_ratio",
            "long_pct",
            "market_breadth_long_pct",
            "ratio",
        )
    else:
        value = _finite(breadth)
    if value is None:
        return None
    ratio = value / 100.0 if value > 1.0 else value
    return ratio if 0.0 <= ratio <= 1.0 else None


def _driver_payload(
    key: str,
    relative_strength: float | None,
    reason: str,
    confidence: str,
) -> dict[str, Any]:
    label, label_en = _DRIVER_LABELS[key]
    relative_label = (
        "UNKNOWN"
        if relative_strength is None
        else "STRONGER"
        if relative_strength >= 0.50
        else "WEAKER"
        if relative_strength <= -0.50
        else "IN_LINE"
    )
    return {
        "key": key,
        "label": label,
        "label_en": label_en,
        "relative_strength_pct": relative_strength,
        "relative_strength": relative_label,
        "confidence": confidence,
        "reason": reason,
        "permission": "CONTEXT_ONLY_NEVER_CREATES_OR_CANCELS_TRIGGER",
    }


def _enum_projection(
    value: Mapping[str, Any] | str | None,
    labels: Mapping[str, tuple[str, str]],
) -> dict[str, str]:
    key = str(value.get("key") if isinstance(value, Mapping) else value or "UNKNOWN").upper()
    if key not in labels:
        key = "UNKNOWN"
    default_label, default_en = labels[key]
    return {
        "key": key,
        "label": str(value.get("label", default_label)) if isinstance(value, Mapping) else default_label,
        "label_en": str(value.get("label_en", default_en)) if isinstance(value, Mapping) else default_en,
    }


def _anomaly_projection(value: Mapping[str, Any] | None) -> dict[str, Any]:
    status = str((value or {}).get("status", "UNKNOWN")).upper()
    if status not in _ANOMALY_LABELS:
        status = "UNKNOWN"
    reasons = []
    for reason in (value or {}).get("reasons", []):
        if isinstance(reason, Mapping):
            reasons.append(
                {
                    "code": str(reason.get("code", "UNKNOWN")),
                    "label": str(reason.get("label", "異常原因未知")),
                    "severity": str(reason.get("severity", status)),
                }
            )
    raw_coverage = (
        (value or {}).get("coverage")
        if isinstance((value or {}).get("coverage"), Mapping)
        else {}
    )
    coverage_status = str(raw_coverage.get("status", "UNKNOWN")).upper()
    if coverage_status not in {"UNKNOWN", "PARTIAL", "COMPLETE"}:
        coverage_status = "UNKNOWN"
    coverage = {
        "status": coverage_status,
        "available": _unique_strings(raw_coverage.get("available")),
        "unknown": _unique_strings(raw_coverage.get("unknown")),
        "available_count": int(_finite(raw_coverage.get("available_count")) or 0),
        "total_count": int(_finite(raw_coverage.get("total_count")) or 0),
        "coverage_pct": _finite(raw_coverage.get("coverage_pct")),
        "required_missing_sources": _unique_strings(
            raw_coverage.get("required_missing_sources")
        ),
    }
    return {
        "status": status,
        "label": str((value or {}).get("label", _ANOMALY_LABELS[status])),
        "reasons": reasons[:5],
        "coverage": coverage,
        "entry_block": False,
        "entry_permission": "ADVISORY_ONLY",
        "may_create_trigger": False,
        "may_cancel_trigger": False,
    }


def _driver_projection(value: Mapping[str, Any] | None) -> dict[str, Any]:
    key = str((value or {}).get("key", "UNKNOWN")).upper()
    if key not in _DRIVER_LABELS:
        key = "UNKNOWN"
    label, label_en = _DRIVER_LABELS[key]
    return {
        "key": key,
        "label": str((value or {}).get("label", label)),
        "label_en": str((value or {}).get("label_en", label_en)),
        "relative_strength_pct": _finite((value or {}).get("relative_strength_pct")),
        "reason": str((value or {}).get("reason", "資料不足")),
    }


def _session_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    key = str(value.get("key", "")).upper()
    if key not in {item[0] for item in _SESSION_SPECS}:
        return {}
    spec = next(item for item in _SESSION_SPECS if item[0] == key)
    return {
        "key": key,
        "label": str(value.get("label", spec[1])),
        "label_en": str(value.get("label_en", spec[2])),
        "active": bool(value.get("active", False)),
        "utc8_window": str(value.get("utc8_window", "")),
    }


def _group_score(group: Any) -> float | None:
    return _finite(group.get("score")) if isinstance(group, Mapping) else None


def _group_is_available(group: Any) -> bool:
    """Return whether an evidence group contains usable measured evidence.

    Legacy groups do not carry an availability field, so a finite score remains
    usable unless an explicit unavailable state or zero/invalid confidence says
    otherwise.  This keeps existing assessments compatible while preventing a
    neutral placeholder (commonly score=50, confidence=0) from influencing
    direction averages or confidence.
    """

    if not isinstance(group, Mapping) or _group_score(group) is None:
        return False
    if group.get("available") is False:
        return False
    if "confidence" in group:
        confidence = _finite(group.get("confidence"))
        if confidence is None or confidence <= 0:
            return False
    explicit_state = next(
        (
            group.get(key)
            for key in (
                "availability",
                "data_status",
                "data_state",
                "state",
                "status",
            )
            if key in group
        ),
        None,
    )
    if explicit_state is not None:
        state = str(explicit_state).strip().upper()
        if state in {
            "UNKNOWN",
            "NOT_AVAILABLE",
            "UNAVAILABLE",
            "MISSING",
            "DATA_MISSING",
            "DATA_UNAVAILABLE",
            "PENDING",
            "DATA_PENDING",
            "STALE",
            "FAILED",
            "ERROR",
            "NOT_SCANNED",
        }:
            return False
    stance = str(group.get("stance", "")).strip().upper()
    score = _group_score(group)
    supporting = _string_list(group.get("supporting"))
    conflicts = _string_list(group.get("conflicts"))
    neutral = _string_list(group.get("neutral"))
    placeholder_markers = (
        "待取得",
        "暫缺",
        "資料不足",
        "資料缺失",
        "DATA PENDING",
        "DATA MISSING",
        "UNAVAILABLE",
    )
    if (
        stance in {"", "NEUTRAL"}
        and score == 50.0
        and not supporting
        and not conflicts
        and any(
            marker in text.upper()
            for text in neutral
            for marker in placeholder_markers
        )
    ):
        return False
    return True


def _evidence_coverage(
    groups: Mapping[str, Any],
    valid_groups: Mapping[str, Any],
) -> dict[str, Any]:
    declared = [key for key, value in groups.items() if isinstance(value, Mapping)]
    available = [key for key in declared if key in valid_groups]
    unavailable = [key for key in declared if key not in valid_groups]
    if not available:
        status = "UNKNOWN"
    elif unavailable:
        status = "PARTIAL"
    else:
        status = "AVAILABLE"
    return {
        "status": status,
        "available_groups": available,
        "unavailable_groups": unavailable,
        "available_count": len(available),
        "total_count": len(declared),
    }


def _stance_label(value: Any) -> str:
    return {
        "SUPPORT": "支持",
        "CONFLICT": "反證",
        "NEUTRAL": "中性",
    }.get(str(value or "").upper(), "未知")


def _confidence_label(
    groups: Mapping[str, Any],
    conflicts: list[str],
    anomaly_status: str,
    data_quality: Mapping[str, Any],
) -> str:
    stances = [
        str(group.get("stance", "NEUTRAL")).upper()
        for group in groups.values()
        if isinstance(group, Mapping)
    ]
    known = [stance for stance in stances if stance in {"SUPPORT", "CONFLICT"}]
    required_missing = _string_list(data_quality.get("required_missing_sources"))
    missing = _string_list(data_quality.get("missing_sources"))
    if not known:
        return "UNKNOWN"
    if anomaly_status == "BLOCK" or required_missing:
        return "LOW"
    if "SUPPORT" in known and "CONFLICT" in known:
        return "LOW"
    if conflicts or missing or len(known) < 2:
        return "MEDIUM"
    return "HIGH"
