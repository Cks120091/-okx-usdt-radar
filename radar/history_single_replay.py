"""Single-instrument 15m historical replay for actionable Episodes only.

This module is intentionally separate from the old pooled eight-major replay.
It replays one requested USDT perpetual through the existing price engine,
records the first 15m close where each Episode is actionable, and then checks
whether TP1 or SL is reached first using later confirmed 5m candles.

It never changes the live strategy, never places orders, and never fabricates
historical OI/CVD/order-book data.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import fields, replace
from pathlib import Path
from typing import Any, Callable

from .card_statistics import setup, wilson
from .history_replay import (
    CORE,
    DAY,
    INTERVALS,
    STEP,
    Interrupted,
    _price_projection,
    classify_path,
    config_for_replay,
    fetch_history,
    iso,
    past_window,
)
from .models import Candle, Instrument, Ticker
from .scanner import MarketScanner

VERSION = "HISTORY_SINGLE_15M_V1"
ALLOWED_DAYS = (3, 7)
MIN_RESOLVED = 1
MIN_RESOLVED_COVERAGE = 0.8
NOTE = (
    "單幣15m價格核心歷史回放；只統計每個Episode第一次達到可進場的15m收線點，"
    "再用後續已收線5m判定TP1或SL誰先到。非實盤成交勝率，也不含完整歷史OI／CVD、"
    "Bid／Ask、深度或訂單簿。"
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
    """Replay every 15m close and record the first actionable state per Episode."""
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
    sampled: set[str] = set()
    episodes: set[str] = set()
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
                if asof >= start:
                    episodes.add(signal.trigger_id)
                signal = _price_projection(scanner, signal, asof, close)
                if not membership:
                    signal = replace(
                        signal,
                        actionable=False,
                        entry_eligibility={
                            **signal.entry_eligibility,
                            "actionable": False,
                            "new_entry_allowed": False,
                        },
                    )
                signal = scanner._record_entry_window(signal)
                if not signal.actionable or signal.trigger_id in sampled:
                    continue

                # The user's requested sample definition is the first 15m close
                # where the Episode is already actionable. Do not require another
                # five-minute revalidation before admitting the sample.
                sampled.add(signal.trigger_id)
                if asof < start:
                    continue
                spec = setup(signal, close)
                if spec is None:
                    continue
                samples.append(
                    {
                        "cohort": spec["cohort"],
                        "label": spec["label"],
                        "entry_ms": asof,
                        "direction": signal.direction,
                        "entry": close,
                        "stop": spec["stop"],
                        "target": spec["target"],
                        "episode": signal.trigger_id,
                        "trigger_type": str(getattr(signal, "trigger_type", "UNKNOWN")),
                        "signal_stage": str(getattr(signal, "signal_stage", "UNKNOWN")),
                        "outcome": None,
                    }
                )

        # Future candles are only read after chronological signal generation.
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
        return {
            "inst_id": instrument.inst_id,
            "status": "OK",
            "evaluated": checked,
            "missing_windows": missing,
            "eligible_windows": eligible_count,
            "episodes": len(episodes),
            "actionable_signals": len(samples),
            "entry_attempts": len(samples),
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
    bucket["status"] = "AVAILABLE" if available else "INSUFFICIENT" if complete else "PARTIAL"
    bucket["rate_pct"] = round(100 * bucket["wins"] / resolved, 1) if available else None
    bucket["interval_pct"] = wilson(bucket["wins"], resolved) if available else None
    return bucket


def aggregate(
    results: list[dict[str, Any]], *, complete: bool, days: int = 7
) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    overall = _new_bucket("全部15m可進訊號")
    symbol_groups: dict[str, dict[str, Any]] = {}
    for result in results:
        inst_id = str(result.get("inst_id") or "")
        own = symbol_groups.setdefault(inst_id, {}) if inst_id else None
        for sample in result.get("samples", []):
            _add(overall, sample)
            group = groups.setdefault(sample["cohort"], _new_bucket(sample["label"]))
            _add(group, sample)
            if own is not None:
                own_group = own.setdefault(sample["cohort"], _new_bucket(sample["label"]))
                _add(own_group, sample)

    finished_groups = {key: _finish(value, complete) for key, value in groups.items()}
    finished_symbols = {
        inst: {key: _finish(value, complete) for key, value in bucket.items()}
        for inst, bucket in symbol_groups.items()
    }
    return {
        "days": int(days),
        "overall": _finish(overall, complete),
        "groups": finished_groups,
        "symbol_groups": finished_symbols,
        "samples": overall["total"],
        "minimum_days": 0,
        "episodes": sum(result.get("episodes", 0) for result in results),
        "actionable_signals": sum(result.get("actionable_signals", 0) for result in results),
        "entry_attempts": sum(result.get("entry_attempts", 0) for result in results),
        "missing_windows": sum(result.get("missing_windows", 0) for result in results),
    }
