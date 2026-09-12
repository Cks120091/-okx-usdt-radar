"""Single-instrument 15m historical replay for first formal Trigger samples.

This module is intentionally separate from the live scanner. It replays one
requested USDT perpetual through the existing price engine and records exactly
one sample at the first formal Trigger of each Signal Episode. Later retests,
entry-readiness changes and same-Episode re-entry states are confirmation /
execution states only; they never create another historical sample.

Each Trigger sample freezes the trigger-time 15m close together with the SL and
TP1 generated at that moment, then checks later confirmed 5m candles to see
whether TP1 or SL is reached first within the existing 24-hour outcome horizon.

It never changes the live strategy, never places orders, and never fabricates
historical OI/CVD/order-book data.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import fields
from pathlib import Path
from typing import Any, Callable

from .card_statistics import setup, wilson
from .history_replay import (
    CORE,
    DAY,
    INTERVALS,
    STEP,
    Interrupted,
    classify_path,
    config_for_replay,
    fetch_history,
    iso,
    past_window,
)
from .models import Candle, Instrument, Ticker
from .scanner import MarketScanner

VERSION = "HISTORY_SINGLE_15M_V1"
ALLOWED_DAYS = (3, 7, 14, 30)
MIN_RESOLVED = 1
MIN_RESOLVED_COVERAGE = 0.8
FORMAL_TRIGGER_STAGES = {"EARLY_SIGNAL", "CONFIRMED", "REENTRY"}
NOTE = (
    "單幣15m價格核心歷史回放；每個Signal Episode只在第一次正式Trigger成立的15m收線點取1筆樣本。"
    "後續回踩、可進場、再進或持續確認都只算同一Trigger的後續狀態，不另加樣本。"
    "每筆固定Trigger當下價格與當時SL／TP1，再用後續已收線5m判定24小時內TP1或SL誰先到。"
    "非實盤成交勝率，也不含完整歷史OI／CVD、Bid／Ask、深度或訂單簿。"
)


def minimum_sample_days(days: int) -> int:
    # Retained for status compatibility only. Single-coin rates expose the
    # actual sample count instead of hiding short windows behind a day gate.
    return 0


def fingerprint(settings: dict[str, Any]) -> str:
    root = Path(__file__).parent
    digest = hashlib.sha256(VERSION.encode())
    for name in (
        "history_single_replay.py",
        "strategy.py",
        "market_story.py",
        "indicators.py",
        "entry_window.py",
        "decision.py",
        "repository.py",
        "scanner.py",
        "card_statistics.py",
    ):
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    cfg = config_for_replay(settings)
    relevant = {
        field.name: getattr(cfg, field.name)
        for field in fields(cfg)
        if field.name not in {"workers", "state_db_path", "previous_open_interest_usd"}
    }
    digest.update(json.dumps(relevant, sort_keys=True, allow_nan=False).encode())
    return digest.hexdigest()


def replay_symbol(
    instrument: Instrument,
    histories: dict[str, list[Candle]],
    start: int,
    end: int,
    settings: dict[str, Any],
    checkpoint: Callable[[], None] = lambda: None,
) -> dict[str, Any]:
    """Replay every 15m close and sample the first formal Trigger of each Episode."""
    scanner = MarketScanner(None, config_for_replay(settings))
    cfg = scanner.config
    limits = {
        "4H": cfg.candle_limit_4h,
        "1H": cfg.candle_limit_1h,
        "15m": cfg.candle_limit_15m,
        "5m": max(288, cfg.candle_limit_5m),
    }
    closes = {
        tf: [bar.ts + INTERVALS[tf] for bar in rows]
        for tf, rows in histories.items()
    }
    prices = {bar.ts: bar for bar in histories["5m"]}
    seen_episodes: set[str] = set()
    sampled_episodes: set[str] = set()
    samples: list[dict[str, Any]] = []
    checked = 0
    missing = 0
    eligible_count = 0
    membership = False
    replay_start = start - DAY

    try:
        for iteration, asof in enumerate(range(replay_start, end, CORE)):
            if iteration % 8 == 0:
                checkpoint()
                time.sleep(0.002)
            if instrument.list_time and asof < instrument.list_time:
                continue

            bundle = {
                tf: past_window(
                    histories[tf], closes[tf], asof, INTERVALS[tf], limit
                )
                for tf, limit in limits.items()
            }
            if not all(bundle.values()):
                if asof >= start:
                    missing += 1
                continue
            if asof >= start:
                checked += 1

            volume_bars = bundle["5m"][-288:]
            if len(volume_bars) < 288:
                continue
            quote_volume_24h = sum(bar.quote_volume for bar in volume_bars)
            threshold = (
                max(0, cfg.min_quote_volume_24h - cfg.quote_volume_buffer_24h)
                if membership
                else cfg.min_quote_volume_24h
            )
            membership = quote_volume_24h >= threshold
            if membership and asof >= start:
                eligible_count += 1

            active = scanner.repository.load_active_signal(instrument.inst_id, "SHORT")
            if not membership and active is None:
                continue

            previous = dict(scanner.repository.load_story(instrument.inst_id, "SHORT") or {})
            previous["allow_opposite_episode"] = True
            close = bundle["15m"][-1].close
            ticker = Ticker(
                instrument.inst_id,
                close,
                close,
                close,
                asof,
                quote_volume_24h,
            )
            analysis = scanner.engine.analyze(
                instrument,
                ticker,
                bundle["4H"],
                bundle["1H"],
                bundle["15m"],
                bundle["5m"][-cfg.candle_limit_5m :],
                previous_story=previous,
                excursion_profile_loader=lambda direction, kind: scanner.repository.excursion_profile(
                    instrument.inst_id, "SHORT", direction, kind
                ),
            )
            states = [analysis.market_state] if analysis.market_state is not None else []
            reconciled = scanner.repository.reconcile(
                [analysis.signal] if analysis.signal is not None and membership else [],
                states,
                iso(asof),
                "SHORT",
            )

            for signal in reconciled:
                if signal.lifecycle.get("terminal") or not signal.trigger_id:
                    continue
                trigger_id = str(signal.trigger_id)
                if trigger_id in seen_episodes:
                    continue

                # Mark it seen even during the one-day warm-up. If an Episode
                # began before the visible range, later retests inside the range
                # must not be miscounted as a fresh Trigger sample.
                seen_episodes.add(trigger_id)

                if asof < start or not membership:
                    continue
                if str(getattr(signal, "signal_stage", "")) not in FORMAL_TRIGGER_STAGES:
                    continue

                spec = setup(signal, close)
                if spec is None:
                    # The Trigger existed, but the trigger-time SL/TP1 geometry
                    # was not usable. Never shift this Episode to a later retest
                    # just to manufacture a valid sample.
                    continue

                sampled_episodes.add(trigger_id)
                samples.append(
                    {
                        "cohort": spec["cohort"],
                        "label": spec["label"],
                        "entry_ms": asof,
                        "trigger_ms": asof,
                        "trigger_event_ms": int(
                            signal.market_metrics.get("trigger_event_ts") or 0
                        ),
                        "direction": signal.direction,
                        "entry": close,
                        "trigger_price": close,
                        "stop": spec["stop"],
                        "target": spec["target"],
                        "episode": trigger_id,
                        "opportunity_kind": "TRIGGER",
                        "opportunity_index": 1,
                        "trigger_type": str(
                            getattr(signal, "trigger_type", "UNKNOWN")
                        ),
                        "signal_stage": str(
                            getattr(signal, "signal_stage", "UNKNOWN")
                        ),
                        "outcome": None,
                    }
                )

        # Future candles are read only after chronological Trigger generation.
        for sample in samples:
            checkpoint()
            sample.update(
                classify_path(
                    sample["direction"],
                    sample["entry"],
                    sample["stop"],
                    sample["target"],
                    sample["entry_ms"],
                    prices,
                )
            )

        trigger_signals = len(samples)
        return {
            "inst_id": instrument.inst_id,
            "status": "OK",
            "evaluated": checked,
            "missing_windows": missing,
            "eligible_windows": eligible_count,
            "episodes": len(sampled_episodes),
            "trigger_signals": trigger_signals,
            # Backward-compatible aggregate keys. Historical sampling no longer
            # has a separate re-entry sample class.
            "initial_signals": trigger_signals,
            "reentry_signals": 0,
            "actionable_signals": trigger_signals,
            "entry_attempts": trigger_signals,
            "samples": samples,
            "timeframes": {tf: len(rows) for tf, rows in histories.items()},
        }
    finally:
        scanner.repository.close()


def _new_bucket(label: str) -> dict[str, Any]:
    return {
        "label": label,
        "wins": 0,
        "losses": 0,
        "timeout": 0,
        "unknown": 0,
        "days": set(),
        "total": 0,
    }


def _add(bucket: dict[str, Any], sample: dict[str, Any]) -> None:
    bucket["total"] += 1
    bucket["days"].add(sample["entry_ms"] // DAY)
    key = {
        "TP1_FIRST": "wins",
        "SL_FIRST": "losses",
        "TIMEOUT": "timeout",
    }.get(sample.get("outcome"), "unknown")
    bucket[key] += 1


def _finish(bucket: dict[str, Any], complete: bool) -> dict[str, Any]:
    bucket = dict(bucket)
    bucket["days"] = len(bucket["days"])
    resolved = bucket["wins"] + bucket["losses"]
    bucket["resolved"] = resolved
    bucket["coverage_pct"] = (
        round(100 * resolved / bucket["total"], 1) if bucket["total"] else 0.0
    )
    bucket["coverage_ok"] = bucket["coverage_pct"] >= MIN_RESOLVED_COVERAGE * 100
    if resolved >= 50:
        tier = "樣本充足"
    elif resolved >= 20:
        tier = "中等樣本"
    elif resolved >= 10:
        tier = "低樣本參考"
    elif resolved > 0:
        tier = "極低樣本"
    else:
        tier = "無已判定樣本"
    bucket["tier"] = tier
    available = complete and resolved > 0
    bucket["status"] = (
        "AVAILABLE" if available else "INSUFFICIENT" if complete else "PARTIAL"
    )
    bucket["rate_pct"] = (
        round(100 * bucket["wins"] / resolved, 1) if available else None
    )
    bucket["interval_pct"] = wilson(bucket["wins"], resolved) if available else None
    return bucket


def aggregate(
    results: list[dict[str, Any]], *, complete: bool, days: int = 7
) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    overall = _new_bucket("全部15m Trigger樣本")
    trigger_overall = _new_bucket("Episode首次正式Trigger")
    confirmation_only = _new_bucket("回踩確認不另算樣本")
    symbol_groups: dict[str, dict[str, Any]] = {}

    for result in results:
        inst_id = str(result.get("inst_id") or "")
        own = symbol_groups.setdefault(inst_id, {}) if inst_id else None
        for sample in result.get("samples", []):
            _add(overall, sample)
            _add(trigger_overall, sample)
            group = groups.setdefault(sample["cohort"], _new_bucket(sample["label"]))
            _add(group, sample)
            if own is not None:
                own_group = own.setdefault(
                    sample["cohort"], _new_bucket(sample["label"])
                )
                _add(own_group, sample)

    finished_groups = {
        key: _finish(value, complete) for key, value in groups.items()
    }
    finished_symbols = {
        inst: {key: _finish(value, complete) for key, value in bucket.items()}
        for inst, bucket in symbol_groups.items()
    }
    total_triggers = sum(
        int(result.get("trigger_signals", len(result.get("samples", []))))
        for result in results
    )
    return {
        "days": int(days),
        "overall": _finish(overall, complete),
        # Keep these legacy keys so old readers remain safe, while reentry is
        # intentionally empty under Trigger-time sampling.
        "initial_overall": _finish(trigger_overall, complete),
        "reentry_overall": _finish(confirmation_only, complete),
        "groups": finished_groups,
        "symbol_groups": finished_symbols,
        "samples": overall["total"],
        "minimum_days": 0,
        "episodes": sum(result.get("episodes", 0) for result in results),
        "trigger_signals": total_triggers,
        "initial_signals": total_triggers,
        "reentry_signals": 0,
        "actionable_signals": total_triggers,
        "entry_attempts": total_triggers,
        "missing_windows": sum(
            result.get("missing_windows", 0) for result in results
        ),
    }
