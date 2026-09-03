from __future__ import annotations

import math
from typing import Any, Iterable, Mapping


ALGORITHM_VERSION = "CONTINUATION_AVG_V2"
LOOKBACK_ALGORITHM_VERSION = "CONTINUATION_LOOKBACK_V1"

_WINDOW_SPECS = {
    "SHORT": (
        ("5m", 5, 5),
        ("10m", 10, 10),
    ),
    "LONG": (
        ("30m", 30, 6),
        ("60m", 60, 12),
    ),
}

_INTERVAL_SECONDS = {"SHORT": 60, "LONG": 300}
_LOOKBACK_INTERVAL_SECONDS = 300
_LOOKBACK_WINDOW_SPECS = {
    # Historical lookback always uses completed 5m bars.  The number in the
    # third position is the number of intervals, so each window needs one
    # additional endpoint (2/3 points for SHORT and 7/13 for LONG).
    "SHORT": (
        ("5m", 5, 1),
        ("10m", 10, 2),
    ),
    "LONG": (
        ("30m", 30, 6),
        ("60m", 60, 12),
    ),
}
_OI_BUCKET_GRACE_MS = 15_000
_SOURCE_FUTURE_SKEW_MS = 5_000


def observer_schedule(horizon: str) -> dict[str, Any]:
    """Return the fixed sampling contract for one radar horizon."""

    normalized = _horizon(horizon)
    specs = _WINDOW_SPECS[normalized]
    return {
        "horizon": normalized,
        "interval_seconds": _INTERVAL_SECONDS[normalized],
        "target_buckets": specs[-1][2],
        "target_samples": specs[-1][2] + 1,
        "early_window": specs[0][0],
        "primary_window": specs[-1][0],
    }


def bounded_observer_samples(
    samples: Iterable[Mapping[str, Any]],
    horizon: str,
) -> list[dict[str, Any]]:
    """Normalize, de-duplicate and bound private per-Episode samples."""

    normalized = _horizon(horizon)
    target = observer_schedule(normalized)["target_samples"]
    by_timestamp: dict[int, dict[str, Any]] = {}
    for source in samples:
        if not isinstance(source, Mapping):
            continue
        timestamp = _canonical_bucket_end(source, normalized)
        if timestamp is None:
            continue
        by_timestamp[timestamp] = dict(source)
    ordered = [by_timestamp[key] for key in sorted(by_timestamp)]
    # A few spare rows make a delayed/duplicate poll recoverable while keeping
    # the Signal payload small on low-memory web instances.
    return ordered[-max(target + 4, 16) :]


def summarize_continuation_samples(
    samples: Iterable[Mapping[str, Any]],
    horizon: str,
    direction: str,
) -> dict[str, Any]:
    """Summarize fixed-cadence averages without creating a price Trigger.

    OI uses contracts (or underlying currency amount), never ``oiUsd``. Taker
    flow is aggregated from non-overlapping interval volumes, so it is volume
    weighted rather than an average of isolated ratios. Price and volume come
    from complete closed micro-candle windows prepared by the market client.
    """

    normalized_horizon = _horizon(horizon)
    normalized_direction = str(direction or "").strip().upper()
    all_samples = bounded_observer_samples(samples, normalized_horizon)
    prepared = _continuous_tail(all_samples, normalized_horizon)
    continuity_reset = len(prepared) < len(all_samples)
    schedule = observer_schedule(normalized_horizon)
    windows: dict[str, dict[str, Any]] = {}
    for key, minutes, required_samples in _WINDOW_SPECS[normalized_horizon]:
        windows[key] = _summarize_window(
            prepared,
            normalized_direction,
            key,
            minutes,
            required_samples,
        )

    completed = windows[schedule["primary_window"]]["ready"]
    early_ready = windows[schedule["early_window"]]["ready"]
    bucket_count = max(0, len(prepared) - 1)
    if completed:
        primary = windows[schedule["primary_window"]]
        if primary["known_count"] == 0:
            status = "INSUFFICIENT"
            label = f"{schedule['primary_window']} 時窗完成・有效資料不足"
        elif primary["unknown_count"]:
            status = "PARTIAL"
            label = f"{schedule['primary_window']} 平均部分完成"
        else:
            status = "READY"
            label = f"{schedule['primary_window']} 平均走向已建立"
        selected_window = schedule["primary_window"]
    elif early_ready:
        status = "EARLY_READY"
        label = f"{schedule['early_window']} 平均已形成・長窗持續採樣"
        selected_window = schedule["early_window"]
    else:
        status = "COLLECTING"
        prefix = "採樣曾中斷・重新累積" if continuity_reset else "平均走向採樣中"
        label = (
            f"{prefix} {min(bucket_count, schedule['target_buckets'])}"
            f"/{schedule['target_buckets']}"
        )
        selected_window = schedule["early_window"]

    selected = windows[selected_window]
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "source_mode": "POST_SIGNAL_OBSERVER",
        "status": status,
        "label": label,
        "horizon": normalized_horizon,
        "direction": normalized_direction,
        "interval_seconds": schedule["interval_seconds"],
        "cadence_label": (
            "每 1 分鐘採樣"
            if normalized_horizon == "SHORT"
            else "每 5 分鐘採樣"
        ),
        "sample_count": len(prepared),
        "bucket_count": bucket_count,
        "target_samples": schedule["target_samples"],
        "target_buckets": schedule["target_buckets"],
        "continuity_reset": continuity_reset,
        "early_window": schedule["early_window"],
        "primary_window": schedule["primary_window"],
        "selected_window": selected_window,
        "averaging_ready": bool(selected.get("ready")),
        "updated_at_ms": (
            _integer(prepared[-1].get("observed_at_ms")) if prepared else None
        ),
        "as_of_close_ms": (
            _integer(prepared[-1].get("bucket_end_ms")) if prepared else None
        ),
        "windows": windows,
        "selected": selected,
        "meaning": (
            "以訊號後多筆固定節奏樣本判斷平均走向；這是同向延續證據，"
            "不是勝率，也不會建立、取消或翻轉正式 Trigger。"
        ),
        "permission": "ADVISORY_ONLY_NEVER_CHANGES_TRIGGER_OR_PLAN",
    }


def summarize_closed_lookback_samples(
    samples: Iterable[Mapping[str, Any]],
    horizon: str,
    direction: str,
) -> dict[str, Any]:
    """Summarize completed historical 5m bars available at scan time.

    This deliberately uses exact exchange candle boundaries.  Rows are sorted
    and de-duplicated, but missing intervals are never filled or interpolated:
    only the newest continuous 5m tail can satisfy a fixed window.
    """

    normalized_horizon = _horizon(horizon)
    normalized_direction = str(direction or "").strip().upper()
    specs = _LOOKBACK_WINDOW_SPECS[normalized_horizon]
    target_samples = specs[-1][2] + 1
    all_samples = _bounded_closed_lookback_samples(samples, target_samples)
    prepared = _continuous_tail_ms(
        all_samples,
        _LOOKBACK_INTERVAL_SECONDS * 1000,
    )
    continuity_reset = len(prepared) < len(all_samples)
    early_window = specs[0][0]
    primary_window = specs[-1][0]
    windows: dict[str, dict[str, Any]] = {}
    for key, minutes, required_buckets in specs:
        windows[key] = _summarize_window(
            prepared,
            normalized_direction,
            key,
            minutes,
            required_buckets,
        )

    completed = windows[primary_window]["ready"]
    early_ready = windows[early_window]["ready"]
    bucket_count = max(0, len(prepared) - 1)
    target_buckets = specs[-1][2]
    if completed:
        primary = windows[primary_window]
        if primary["known_count"] == 0:
            status = "INSUFFICIENT"
            label = f"{primary_window} 已收線時窗完成・有效資料不足"
        elif primary["unknown_count"]:
            status = "PARTIAL"
            label = f"{primary_window} 已收線平均部分完成"
        else:
            status = "READY"
            label = f"{primary_window} 已收線平均走向已建立"
        selected_window = primary_window
    elif early_ready:
        status = "EARLY_READY"
        label = f"{early_window} 已收線平均已形成・長窗資料不足"
        selected_window = early_window
    else:
        status = "INSUFFICIENT"
        prefix = "已收線資料有缺口" if continuity_reset else "已收線平均資料不足"
        label = f"{prefix} {min(bucket_count, target_buckets)}/{target_buckets}"
        selected_window = early_window

    selected = windows[selected_window]
    return {
        "algorithm_version": LOOKBACK_ALGORITHM_VERSION,
        "source_mode": "HISTORICAL_CLOSED_BARS",
        "status": status,
        "label": label,
        "horizon": normalized_horizon,
        "direction": normalized_direction,
        "interval_seconds": _LOOKBACK_INTERVAL_SECONDS,
        "cadence_label": "每 5 分鐘完整收線",
        "sample_count": len(prepared),
        "bucket_count": bucket_count,
        "target_samples": target_samples,
        "target_buckets": target_buckets,
        "continuity_reset": continuity_reset,
        "early_window": early_window,
        "primary_window": primary_window,
        "selected_window": selected_window,
        "averaging_ready": bool(selected.get("ready")),
        "as_of_close_ms": (
            _integer(prepared[-1].get("bucket_end_ms")) if prepared else None
        ),
        "updated_at_ms": (
            _integer(prepared[-1].get("observed_at_ms")) if prepared else None
        ),
        "windows": windows,
        "selected": selected,
        "meaning": (
            "以掃描當下最新完整收線與前幾根 5 分鐘棒判斷平均走向；"
            "這是同向延續證據，不是勝率，也不會建立、取消或翻轉正式 Trigger。"
        ),
        "permission": "ADVISORY_ONLY_NEVER_CHANGES_TRIGGER_OR_PLAN",
    }


def _bounded_closed_lookback_samples(
    samples: Iterable[Mapping[str, Any]],
    target_samples: int,
) -> list[dict[str, Any]]:
    """Return a small, ordered set of exact completed 5m samples."""

    by_timestamp: dict[int, dict[str, Any]] = {}
    conflicted_timestamps: set[int] = set()
    for source in samples:
        if not isinstance(source, Mapping):
            continue
        timestamp = _canonical_closed_lookback_end(source)
        if timestamp is None:
            continue
        if timestamp in conflicted_timestamps:
            continue
        normalized = dict(source)
        existing = by_timestamp.get(timestamp)
        if existing is not None:
            conflicts = any(
                left is not None
                and right is not None
                and left != right
                for left, right in (
                    (
                        _number(existing.get("open_interest_contracts")),
                        _number(normalized.get("open_interest_contracts")),
                    ),
                    (
                        _number(existing.get("open_interest_ccy")),
                        _number(normalized.get("open_interest_ccy")),
                    ),
                )
            )
            if conflicts:
                by_timestamp.pop(timestamp, None)
                conflicted_timestamps.add(timestamp)
                continue
        by_timestamp[timestamp] = normalized
    ordered = [by_timestamp[key] for key in sorted(by_timestamp)]
    return ordered[-max(target_samples + 4, 17) :]


def _canonical_closed_lookback_end(sample: Mapping[str, Any]) -> int | None:
    interval_ms = _LOOKBACK_INTERVAL_SECONDS * 1000
    bucket_end = _integer(sample.get("bucket_end_ms"))
    bucket_start = _integer(sample.get("bucket_start_ms"))
    observed_at = _integer(sample.get("observed_at_ms"))
    if (
        bucket_end is None
        or bucket_end <= 0
        or bucket_end % interval_ms != 0
        or bucket_start != bucket_end - interval_ms
        or observed_at is None
        or observed_at < bucket_end
    ):
        return None
    return bucket_end


def _continuous_tail_ms(
    samples: list[dict[str, Any]],
    interval_ms: int,
) -> list[dict[str, Any]]:
    if len(samples) < 2:
        return samples
    start = len(samples) - 1
    while start > 0:
        newer = _integer(samples[start].get("bucket_end_ms"))
        older = _integer(samples[start - 1].get("bucket_end_ms"))
        if newer is None or older is None or newer - older != interval_ms:
            break
        start -= 1
    return samples[start:]


def _summarize_window(
    samples: list[dict[str, Any]],
    direction: str,
    key: str,
    minutes: int,
    required_buckets: int,
) -> dict[str, Any]:
    selected = samples[-(required_buckets + 1) :]
    sample_count = len(selected)
    bucket_count = max(0, sample_count - 1)
    first_timestamp = _integer(selected[0].get("bucket_end_ms")) if selected else None
    last_timestamp = _integer(selected[-1].get("bucket_end_ms")) if selected else None
    elapsed_minutes = (
        (last_timestamp - first_timestamp) / 60_000.0
        if first_timestamp is not None
        and last_timestamp is not None
        and last_timestamp >= first_timestamp
        else None
    )
    ready = bool(
        direction in {"LONG", "SHORT"}
        and bucket_count >= required_buckets
        and elapsed_minutes is not None
        # Canonical exchange buckets make a 10m/60m label exact; polling
        # jitter belongs to observed_at_ms and cannot stretch the window.
        and last_timestamp - first_timestamp == minutes * 60_000
    )
    expected_interval_ms = int(minutes * 60_000 / required_buckets)
    price_return = _price_return(selected, expected_interval_ms)
    directional_return = (
        price_return if direction == "LONG" else -price_return
        if direction == "SHORT" and price_return is not None
        else None
    )

    consistency = _directional_consistency(
        selected,
        direction,
        expected_interval_ms,
    )
    oi = _oi_domain(
        selected,
        directional_return,
        consistency,
        minutes,
        ready,
        expected_interval_ms,
    )
    taker = _taker_domain(
        selected[1:],
        directional_return,
        consistency,
        direction,
        ready,
        expected_interval_ms,
    )
    volume = _volume_domain(
        selected[1:],
        directional_return,
        consistency,
        ready,
        required_buckets,
    )
    domains = {"OI": oi, "TAKER_CVD": taker, "VOLUME": volume}
    support_count = sum(item["state"] == "SUPPORT" for item in domains.values())
    conflict_count = sum(item["state"] == "CONFLICT" for item in domains.values())
    known_count = sum(item["state"] != "UNKNOWN" for item in domains.values())
    unknown_count = len(domains) - known_count
    severe = any(item.get("severe") is True for item in domains.values())

    if not ready:
        state = "COLLECTING"
        label = f"採樣中 {bucket_count}/{required_buckets}"
    elif known_count == 0:
        state = "UNKNOWN"
        label = "資料不足"
    elif severe or conflict_count >= 2:
        state = "CONFLICT"
        label = "平均走向出現反證"
    elif (
        support_count >= 2
        and oi["state"] == "SUPPORT"
        and conflict_count == 0
    ):
        state = "ALIGNED"
        label = "平均走向同向"
    elif conflict_count:
        state = "MIXED"
        label = "平均走向分歧"
    elif support_count:
        state = "FORMING"
        label = "平均走向形成中"
    else:
        state = "NEUTRAL"
        label = "平均走向尚未確認"

    return {
        "key": key,
        "minutes": minutes,
        "ready": ready,
        "sample_count": sample_count,
        "bucket_count": bucket_count,
        "required_buckets": required_buckets,
        "progress_pct": round(min(100.0, bucket_count / required_buckets * 100.0), 1),
        "elapsed_minutes": _round(elapsed_minutes, 2),
        "state": state,
        "label": label,
        "price_return_pct": _round(price_return, 4),
        "directional_price_return_pct": _round(directional_return, 4),
        "directional_consistency_pct": _round(consistency, 1),
        "support_count": support_count,
        "conflict_count": conflict_count,
        "known_count": known_count,
        "unknown_count": unknown_count,
        "domains": domains,
    }


def _oi_domain(
    samples: list[dict[str, Any]],
    directional_return: float | None,
    directional_consistency: float | None,
    minutes: int,
    ready: bool,
    expected_interval_ms: int,
) -> dict[str, Any]:
    points: list[tuple[float, float]] = []
    unit_label = "合約數"
    if samples:
        source_timestamps = [
            _source_timestamp(sample, "open_interest") for sample in samples
        ]
        valid_source_clock = bool(
            all(timestamp is not None for timestamp in source_timestamps)
            and all(
                _oi_source_matches_bucket(sample, timestamp)
                for sample, timestamp in zip(samples, source_timestamps)
            )
            and all(
                expected_interval_ms * 0.80
                <= right - left
                <= expected_interval_ms * 1.20
                for left, right in zip(
                    source_timestamps,
                    source_timestamps[1:],
                )
            )
        )
        if not valid_source_clock:
            return _domain(
                "UNKNOWN",
                "OI 來源時間與固定收線窗不一致",
                missing="OI 新鮮且連續的來源時間",
            )
        origin = int(source_timestamps[0])
        contracts = [_number(sample.get("open_interest_contracts")) for sample in samples]
        currency = [_number(sample.get("open_interest_ccy")) for sample in samples]
        # Never switch units within a window.  A per-row contracts→oiCcy
        # fallback could manufacture a huge trend when one endpoint field is
        # temporarily absent.
        if all(value is not None and value > 0 for value in contracts):
            series = contracts
        elif all(value is not None and value > 0 for value in currency):
            series = currency
            unit_label = "標的幣數量"
        else:
            series = []
        for sample, value, timestamp in zip(samples, series, source_timestamps):
            if timestamp is not None and value is not None:
                points.append(((timestamp - origin) / 60_000.0, value))
    if not ready or len(points) != len(samples):
        return _domain("UNKNOWN", "OI 平均樣本仍不足", missing="OI 多筆合約量樣本")
    if directional_return is None or directional_consistency is None:
        return _domain(
            "UNKNOWN",
            "OI 已取得，但同一平均窗的已收盤價格資料不足",
            missing="OI 同窗價格反應",
        )

    first = points[0][1]
    latest = points[-1][1]
    prior_average = sum(value for _, value in points[:-1]) / (len(points) - 1)
    latest_vs_prior_average_pct = (latest - prior_average) / prior_average * 100.0
    window_change_pct = (latest - first) / first * 100.0
    normalized = [(x, (value - first) / first * 100.0) for x, value in points]
    slope = _linear_slope(normalized)
    deltas = [right[1] - left[1] for left, right in zip(points, points[1:])]
    persistence = (
        sum(delta > 0 for delta in deltas) / len(deltas) * 100.0
        if deltas
        else None
    )
    threshold = max(0.10, 0.50 * minutes / 60.0)
    detail = (
        f"{unit_label}・最新相較前 {len(points) - 1} 點均值 "
        f"{latest_vs_prior_average_pct:+.2f}%・"
        f"上升持續度 {persistence:.0f}%"
    )
    domain_metrics = {
        # Keep change_pct as the primary public number for compatibility, but
        # it now means exactly what the radar promises: newest completed OI
        # versus the mean of the preceding completed endpoints.
        "change_pct": latest_vs_prior_average_pct,
        "latest_vs_prior_average_pct": latest_vs_prior_average_pct,
        "window_change_pct": window_change_pct,
        "prior_average": prior_average,
        "latest_value": latest,
        "slope_pct_per_min": slope,
        "persistence_pct": persistence,
    }
    if (
        latest_vs_prior_average_pct >= threshold
        and window_change_pct > 0.0
        and (slope or 0.0) > 0.0
        and (persistence or 0.0) >= 60.0
    ):
        if directional_return > 0.02 and directional_consistency >= 55.0:
            return _domain(
                "SUPPORT",
                f"最新完整 OI 高於前段均值且價格同向（{detail}）",
                detail=detail,
                **domain_metrics,
            )
        if directional_return < -0.02 and directional_consistency <= 45.0:
            return _domain(
                "CONFLICT",
                f"最新完整 OI 高於前段均值但價格反向（{detail}）",
                detail=detail,
                severe=True,
                **domain_metrics,
            )
        return _domain(
            "NEUTRAL",
            f"最新完整 OI 高於前段均值，但價格尚未同向回應（{detail}）",
            detail=detail,
            **domain_metrics,
        )
    if latest_vs_prior_average_pct <= -threshold:
        return _domain(
            "NEUTRAL",
            f"最新完整 OI 低於前段均值，較像平倉／回補（{detail}）",
            detail=detail,
            **domain_metrics,
        )
    return _domain(
        "NEUTRAL",
        f"最新完整 OI 相較前段均值變化不明顯（{detail}）",
        detail=detail,
        **domain_metrics,
    )


def _taker_domain(
    samples: list[dict[str, Any]],
    directional_return: float | None,
    directional_consistency: float | None,
    direction: str,
    ready: bool,
    expected_interval_ms: int,
) -> dict[str, Any]:
    intervals = [
        sample
        for sample in samples
        if str(sample.get("trades_coverage") or "").upper() == "COMPLETE"
        and _trade_bucket_matches(sample, expected_interval_ms)
        and _number(sample.get("taker_buy_volume")) is not None
        and _number(sample.get("taker_sell_volume")) is not None
    ]
    if not ready or len(intervals) < len(samples):
        return _domain(
            "UNKNOWN",
            "Taker／CVD 平均窗有缺口，不能用部分成交冒充完整方向",
            missing="Taker／CVD 完整區間樣本",
        )
    if directional_return is None or directional_consistency is None:
        return _domain(
            "UNKNOWN",
            "Taker／CVD 已取得，但同一平均窗的已收盤價格資料不足",
            missing="Taker／CVD 同窗價格反應",
        )
    interval_totals = [
        (_number(item.get("taker_buy_volume")) or 0.0)
        + (_number(item.get("taker_sell_volume")) or 0.0)
        for item in intervals
    ]
    if any(total <= 0 for total in interval_totals):
        return _domain(
            "UNKNOWN",
            "Taker／CVD 平均窗含無成交區間",
            missing="每個 Taker 成交區間",
        )
    buy = sum(_number(item.get("taker_buy_volume")) or 0.0 for item in intervals)
    sell = sum(_number(item.get("taker_sell_volume")) or 0.0 for item in intervals)
    total = buy + sell
    if total <= 0:
        return _domain("UNKNOWN", "Taker／CVD 區間沒有可比較成交", missing="Taker 成交")
    buy_share = buy / total
    directional_share = buy_share if direction == "LONG" else 1.0 - buy_share
    directional_cvd = (buy - sell) if direction == "LONG" else (sell - buy)
    aligned_buckets = sum(
        (
            (_number(item.get("taker_buy_volume")) or 0.0)
            > (_number(item.get("taker_sell_volume")) or 0.0)
        )
        if direction == "LONG"
        else (
            (_number(item.get("taker_sell_volume")) or 0.0)
            > (_number(item.get("taker_buy_volume")) or 0.0)
        )
        for item in intervals
    )
    bucket_persistence = aligned_buckets / len(intervals) * 100.0
    detail = (
        f"量加權同向占比 {directional_share * 100.0:.1f}%・"
        f"同向區間 {aligned_buckets}/{len(intervals)}"
    )
    if (
        directional_share >= 0.62
        and bucket_persistence >= 60.0
        and directional_return <= 0.0
    ):
        return _domain(
            "CONFLICT",
            f"Taker／CVD 平均很強但價格推不動，可能遭吸收（{detail}）",
            detail=detail,
            severe=True,
            directional_share_pct=directional_share * 100.0,
            directional_cvd=directional_cvd,
            bucket_persistence_pct=bucket_persistence,
        )
    if (
        directional_share >= 0.60
        and bucket_persistence >= 60.0
        and directional_return > 0.02
        and directional_consistency >= 55.0
    ):
        return _domain(
            "SUPPORT",
            f"Taker／CVD 量加權平均與價格同向（{detail}）",
            detail=detail,
            directional_share_pct=directional_share * 100.0,
            directional_cvd=directional_cvd,
            bucket_persistence_pct=bucket_persistence,
        )
    if (
        directional_share <= 0.35
        and bucket_persistence <= 40.0
        and directional_return < -0.02
        and directional_consistency <= 45.0
    ):
        return _domain(
            "CONFLICT",
            f"Taker／CVD 量加權平均與 Trigger 反向（{detail}）",
            detail=detail,
            directional_share_pct=directional_share * 100.0,
            directional_cvd=directional_cvd,
            bucket_persistence_pct=bucket_persistence,
        )
    return _domain(
        "NEUTRAL",
        f"Taker／CVD 量加權平均尚未形成同向主導（{detail}）",
        detail=detail,
        directional_share_pct=directional_share * 100.0,
        directional_cvd=directional_cvd,
        bucket_persistence_pct=bucket_persistence,
    )


def _volume_domain(
    samples: list[dict[str, Any]],
    directional_return: float | None,
    directional_consistency: float | None,
    ready: bool,
    required_buckets: int,
) -> dict[str, Any]:
    by_candle: dict[int, tuple[float, float]] = {}
    for sample in samples:
        timestamp = _candle_timestamp(sample)
        bucket_end = _integer(sample.get("bucket_end_ms"))
        bucket_start = _integer(sample.get("bucket_start_ms"))
        candle_start = _integer(sample.get("candle_ts"))
        volume = _number(sample.get("quote_volume"))
        baseline = _number(sample.get("volume_baseline"))
        if (
            timestamp is not None
            and timestamp == bucket_end
            and candle_start == bucket_start
            and volume is not None
            and volume >= 0
            and baseline is not None
            and baseline > 0
        ):
            by_candle[timestamp] = (volume, baseline)
    values = [by_candle[key] for key in sorted(by_candle)]
    ratio = (
        (sum(value[0] for value in values) / len(values))
        / (sum(value[1] for value in values) / len(values))
        if values
        else None
    )
    if not ready or len(values) < required_buckets or ratio is None:
        return _domain("UNKNOWN", "平均成交量窗仍不足", missing="完整平均成交量窗")
    if directional_return is None or directional_consistency is None:
        return _domain(
            "UNKNOWN",
            "成交量已取得，但同一平均窗的已收盤價格資料不足",
            missing="成交量同窗價格反應",
        )
    expanded_buckets = sum(volume / baseline >= 1.20 for volume, baseline in values)
    expansion_persistence = expanded_buckets / len(values) * 100.0
    detail = (
        f"區間平均量比 {ratio:.2f} 倍・"
        f"放量區間 {expanded_buckets}/{len(values)}"
    )
    persistent_expansion = ratio >= 1.20 and expansion_persistence >= 50.0
    if (
        persistent_expansion
        and directional_return > 0.02
        and directional_consistency >= 55.0
    ):
        return _domain(
            "SUPPORT",
            f"平均成交量持續放大且價格同向（{detail}）",
            detail=detail,
            ratio=ratio,
            expansion_persistence_pct=expansion_persistence,
        )
    if (
        persistent_expansion
        and directional_return < -0.02
        and directional_consistency <= 45.0
    ):
        return _domain(
            "CONFLICT",
            f"平均成交量持續放大但價格反向（{detail}）",
            detail=detail,
            ratio=ratio,
            expansion_persistence_pct=expansion_persistence,
        )
    return _domain(
        "NEUTRAL",
        f"平均成交量尚未形成持續同向放量（{detail}）",
        detail=detail,
        ratio=ratio,
        expansion_persistence_pct=expansion_persistence,
    )


def _price_return(
    samples: list[dict[str, Any]],
    expected_interval_ms: int,
) -> float | None:
    points: list[float] = []
    candle_timestamps: list[int] = []
    seen_timestamps: set[int] = set()
    for sample in samples:
        value = _number(sample.get("price"))
        timestamp = _candle_timestamp(sample)
        bucket_end = _integer(sample.get("bucket_end_ms"))
        bucket_start = _integer(sample.get("bucket_start_ms"))
        candle_start = _integer(sample.get("candle_ts"))
        if (
            value is None
            or value <= 0
            or timestamp is None
            or timestamp != bucket_end
            or candle_start != bucket_start
            or timestamp in seen_timestamps
        ):
            return None
        seen_timestamps.add(timestamp)
        candle_timestamps.append(timestamp)
        points.append(value)
    if len(points) < 2 or points[0] <= 0:
        return None
    if any(
        right - left != expected_interval_ms
        for left, right in zip(candle_timestamps, candle_timestamps[1:])
    ):
        return None
    return (points[-1] - points[0]) / points[0] * 100.0


def _continuous_tail(
    samples: list[dict[str, Any]],
    horizon: str,
) -> list[dict[str, Any]]:
    if len(samples) < 2:
        return samples
    interval_ms = _INTERVAL_SECONDS[horizon] * 1000
    start = len(samples) - 1
    while start > 0:
        newer = _integer(samples[start].get("bucket_end_ms"))
        older = _integer(samples[start - 1].get("bucket_end_ms"))
        if newer is None or older is None:
            break
        elapsed = newer - older
        if elapsed != interval_ms:
            break
        start -= 1
    return samples[start:]


def _directional_consistency(
    samples: list[dict[str, Any]],
    direction: str,
    expected_interval_ms: int,
) -> float | None:
    prices: list[float] = []
    candle_timestamps: list[int] = []
    seen_timestamps: set[int] = set()
    for sample in samples:
        timestamp = _candle_timestamp(sample)
        bucket_end = _integer(sample.get("bucket_end_ms"))
        bucket_start = _integer(sample.get("bucket_start_ms"))
        candle_start = _integer(sample.get("candle_ts"))
        close = _number(sample.get("price"))
        if (
            timestamp is None
            or timestamp != bucket_end
            or candle_start != bucket_start
            or close is None
            or timestamp in seen_timestamps
        ):
            return None
        seen_timestamps.add(timestamp)
        candle_timestamps.append(timestamp)
        prices.append(close)
    if (
        len(prices) != len(samples)
        or len(prices) < 2
        or direction not in {"LONG", "SHORT"}
    ):
        return None
    if any(
        right - left != expected_interval_ms
        for left, right in zip(candle_timestamps, candle_timestamps[1:])
    ):
        return None
    sign = 1.0 if direction == "LONG" else -1.0
    deltas = [(right - left) * sign for left, right in zip(prices, prices[1:])]
    return sum(delta > 0 for delta in deltas) / len(deltas) * 100.0


def _domain(
    state: str,
    reason: str,
    *,
    detail: str | None = None,
    missing: str | None = None,
    severe: bool = False,
    **values: Any,
) -> dict[str, Any]:
    payload = {
        "state": state,
        "reason": reason,
        "detail": detail or reason,
        "missing": missing,
        "severe": severe,
    }
    for key, value in values.items():
        payload[key] = _round(value, 6) if isinstance(value, float) else value
    return payload


def _candle_timestamp(sample: Mapping[str, Any]) -> int | None:
    return _integer(sample.get("candle_close_ts"))


def _canonical_bucket_end(
    sample: Mapping[str, Any],
    horizon: str,
) -> int | None:
    interval_ms = _INTERVAL_SECONDS[horizon] * 1000
    bucket_end = _integer(sample.get("bucket_end_ms"))
    bucket_start = _integer(sample.get("bucket_start_ms"))
    observed_at = _integer(sample.get("observed_at_ms"))
    if (
        bucket_end is None
        or bucket_end <= 0
        or bucket_end % interval_ms != 0
        or observed_at is None
        or observed_at < bucket_end
        or bucket_start is None
        or bucket_start != bucket_end - interval_ms
    ):
        return None
    return bucket_end


def _source_timestamp(sample: Mapping[str, Any], key: str) -> int | None:
    sources = sample.get("source_timestamps")
    if not isinstance(sources, Mapping):
        return None
    return _integer(sources.get(key))


def _oi_source_matches_bucket(
    sample: Mapping[str, Any],
    source_timestamp: int | None,
) -> bool:
    bucket_end = _integer(sample.get("bucket_end_ms"))
    bucket_start = _integer(sample.get("bucket_start_ms"))
    observed_at = _integer(sample.get("observed_at_ms"))
    interval_ms = (
        bucket_end - bucket_start
        if bucket_end is not None
        and bucket_start is not None
        and bucket_end > bucket_start
        else None
    )
    historical_generation_time = (
        str(sample.get("open_interest_alignment") or "").upper()
        == "PRECEDING_COMPLETED_5M_CLOSE"
    )
    return bool(
        source_timestamp is not None
        and bucket_end is not None
        and interval_ms is not None
        and observed_at is not None
        and (
            (
                # Rubik history exposes a data-generation timestamp, not a
                # candle confirm bit.  The scanner maps it to the immediately
                # preceding completed boundary, so it can occur anywhere in
                # that next interval.
                historical_generation_time
                and bucket_end <= source_timestamp < bucket_end + interval_ms
            )
            or (
                not historical_generation_time
                and abs(source_timestamp - bucket_end) <= _OI_BUCKET_GRACE_MS
            )
        )
        and source_timestamp <= observed_at + _SOURCE_FUTURE_SKEW_MS
    )


def _trade_bucket_matches(
    sample: Mapping[str, Any],
    expected_interval_ms: int,
) -> bool:
    bucket_end = _integer(sample.get("bucket_end_ms"))
    bucket_start = _integer(sample.get("bucket_start_ms"))
    return bool(
        bucket_end is not None
        and bucket_start is not None
        and bucket_start == bucket_end - expected_interval_ms
    )


def _linear_slope(points: list[tuple[float, float]]) -> float | None:
    if len(points) < 2:
        return None
    mean_x = sum(point[0] for point in points) / len(points)
    mean_y = sum(point[1] for point in points) / len(points)
    denominator = sum((point[0] - mean_x) ** 2 for point in points)
    if denominator <= 0:
        return None
    return sum(
        (point[0] - mean_x) * (point[1] - mean_y)
        for point in points
    ) / denominator


def _horizon(value: str) -> str:
    normalized = str(value or "SHORT").strip().upper()
    if normalized not in _WINDOW_SPECS:
        raise ValueError("observer horizon must be SHORT or LONG")
    return normalized


def _number(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _integer(value: Any) -> int | None:
    numeric = _number(value)
    return int(numeric) if numeric is not None else None


def _round(value: Any, digits: int) -> float | None:
    numeric = _number(value)
    return round(numeric, digits) if numeric is not None else None
