"""Aligned, read-only price/OI/taker windows for the single-coin screen.

No function here creates a Trigger, changes a plan, or grants entry permission.
CVD means interval taker-buy minus taker-sell quote volume, NOT a reset-dependent
chart level. Missing data is never converted into a directional vote.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .models import Candle

INTERVAL_MS = 300_000
VERSION = "INTRADAY_FLOW_V1"
WINDOWS = (("15m", 3), ("1H", 12), ("4H", 48))


def _number(value: Any, *, positive: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return value if math.isfinite(value) and (value > 0 if positive else value >= 0) else None


def _points(rows: Sequence[Mapping[str, Any]], fields: tuple[str, ...]) -> dict[int, tuple[float | None, ...]]:
    """Only exact exchange bucket timestamps; reject contradictory duplicates."""
    output: dict[int, tuple[float | None, ...]] = {}
    conflicts: set[int] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        ts = _number(row.get("ts"), positive=True)
        if ts is None or ts != int(ts) or int(ts) % INTERVAL_MS:
            continue
        key = int(ts)
        values = tuple(_number(row.get(field)) for field in fields)
        if key in output and output[key] != values:
            conflicts.add(key)
        output[key] = values
    return {key: values for key, values in output.items() if key not in conflicts}


def _window(end: int, bars: int, candles: dict[int, Candle], oi: dict, taker: dict) -> dict[str, Any]:
    start = end - bars * INTERVAL_MS
    stamps = list(range(start, end, INTERVAL_MS))
    result: dict[str, Any] = {"start_ms": start, "end_ms": end,
        "price": {"status": "MISSING"}, "oi": {"status": "MISSING", "unit": "contracts"},
        "cvd": {"status": "MISSING", "unit": "USDT"}, "interpretation": "價格資料不足"}
    if not all(ts in candles for ts in stamps) or start - INTERVAL_MS not in candles:
        return result
    first = candles[start - INTERVAL_MS].close
    last = candles[stamps[-1]].close
    if _number(first, positive=True) is None or _number(last, positive=True) is None:
        return result
    change = (last / first - 1) * 100
    result["price"] = {"status": "OK", "start": first, "end": last, "change_pct": change}
    oi_stamps = [*stamps, end]
    # Require a continuous raw-quantity series, not just two interpolated endpoints.
    if all(ts in oi for ts in oi_stamps):
        index = next((i for i in (0, 1) if all(_number(oi[ts][i], positive=True) is not None for ts in oi_stamps)), None)
        if index is not None:
            initial, final = oi[start][index], oi[end][index]
            result["oi"] = {"status": "OK", "unit": "contracts" if index == 0 else "base",
                "start": initial, "end": final, "change": final - initial,
                "change_pct": (final / initial - 1) * 100}
    if all(ts in taker and all(value is not None for value in taker[ts]) for ts in stamps):
        # Verify that exchange volume buckets really match these price buckets.
        # A shifted timestamp convention or incomplete payload stays unavailable.
        matched = all(abs(sum(taker[ts]) - candles[ts].quote_volume) <= max(1e-6, candles[ts].quote_volume * .02)
                      for ts in stamps)
        positive_total = any(candles[ts].quote_volume > 0 for ts in stamps)
        if matched and positive_total:
            sell = sum(taker[ts][0] for ts in stamps)
            buy = sum(taker[ts][1] for ts in stamps)
            total = buy + sell
            result["cvd"] = {"status": "OK", "unit": "USDT", "buy": buy, "sell": sell,
                "delta": buy - sell, "imbalance_pct": (buy - sell) / total * 100 if total else None,
                "method": "SUM_TAKER_BUY_MINUS_SELL", "volume_alignment": "VERIFIED"}
        else:
            result["cvd"]["reason"] = "成交窗口與K線成交額無法核對"
    quantity = result["oi"].get("change_pct")
    direction = "上漲" if change > .05 else "下跌" if change < -.05 else "價格持平"
    participation = "OI數量資料不足" if quantity is None else "增倉" if quantity > .1 else "減倉" if quantity < -.1 else "持倉大致持平"
    text = f"{participation}｜{direction}"
    imbalance = result["cvd"].get("imbalance_pct")
    if imbalance is None:
        text += "；主動成交資料不足"
    elif imbalance < -10 and change >= -.05:
        text += "；主動賣出占優但未有效推低，可能有承接，需價格驗證"
    elif imbalance > 10 and change <= .05:
        text += "；主動買入占優但未有效推高，反推成果有限"
    elif imbalance < -10:
        text += "；主動賣出與下壓價格互相支持"
    elif imbalance > 10:
        text += "；主動買入與上漲價格互相支持"
    else:
        text += "；主動成交較均衡"
    result["interpretation"] = text
    return result


def summarize_intraday_flow(inst_id: str, candles: Sequence[Candle], oi_history: Sequence[Mapping[str, Any]],
                            taker_history: Sequence[Mapping[str, Any]], *, observed_at_ms: int) -> dict[str, Any]:
    """One fixed as-of for all rolling windows. Late/gapped sources stay missing."""
    end = observed_at_ms // INTERVAL_MS * INTERVAL_MS
    closed: dict[int, Candle] = {}
    conflicts: set[int] = set()
    for candle in candles:
        if not candle.confirmed or candle.ts % INTERVAL_MS or candle.ts + INTERVAL_MS > end:
            continue
        if candle.ts in closed and closed[candle.ts] != candle:
            conflicts.add(candle.ts)
        closed[candle.ts] = candle
    for ts in conflicts:
        closed.pop(ts, None)
    oi = _points(oi_history, ("oi", "oiCcy"))
    # The adapter explicitly requests unit=2 for a USDT linear contract.
    taker = _points([row for row in taker_history if isinstance(row, Mapping)
                    and row.get("unit") == "USDT" and row.get("inst_id") == inst_id], ("sell", "buy"))
    # Freeze a recent common watermark instead of mixing a delayed flow bucket
    # with current price. Never silently rewind farther than one 5m interval.
    latest_points = [end]
    if closed:
        latest_points.append(max(closed) + INTERVAL_MS)
    available_oi = [ts for ts in oi if ts <= end]
    if available_oi:
        latest_points.append(max(available_oi))
    available_flow = [ts + INTERVAL_MS for ts in taker if ts + INTERVAL_MS <= end]
    if available_flow:
        latest_points.append(max(available_flow))
    common_end = min(latest_points)
    if end - common_end <= INTERVAL_MS:
        end = common_end
    rows = {name: _window(end, count, closed, oi, taker) for name, count in WINDOWS}
    previous = _window(end - 3 * INTERVAL_MS, 3, closed, oi, taker)
    latest, hourly = rows["15m"], rows["1H"]
    summary = latest["interpretation"]
    p15, p1h = latest["price"].get("change_pct"), hourly["price"].get("change_pct")
    if p15 is not None and p1h is not None:
        if p1h < -.05 and p15 > .05:
            summary = "1H回落中，最近15m已有買方反推；是否收復需看價格。" + summary
        elif p1h > .05 and p15 < -.05:
            summary = "1H上漲背景中，最近15m出現下壓；不是等待4H翻空才觀察。" + summary
    now_delta = latest["cvd"].get("imbalance_pct")
    prior_delta = previous["cvd"].get("imbalance_pct")
    comparison: dict[str, Any] = {"status": "MISSING", "label": "前後15m成交資料不足"}
    if now_delta is not None and prior_delta is not None:
        shift = now_delta - prior_delta
        comparison = {"status": "OK", "imbalance_change_pp": shift,
            "label": "主動成交向買方移動" if shift > 5 else "主動成交向賣方移動" if shift < -5 else "主動成交比例大致持平"}
    structure = {}
    structure_stamps = list(range(end - 15 * INTERVAL_MS, end, INTERVAL_MS))
    if all(ts in closed for ts in structure_stamps):
        tail = [closed[ts] for ts in structure_stamps]
        if all(0 < bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
               and all(math.isfinite(v) for v in (bar.low, bar.high, bar.open, bar.close)) for bar in tail):
            ranges = [max(bar.high - bar.low, abs(bar.high - prior.close), abs(bar.low - prior.close))
                      for prior, bar in zip(tail, tail[1:])]
            structure = {"as_of_ms": end, "low_15m": min(bar.low for bar in tail[-3:]),
                         "high_15m": max(bar.high for bar in tail[-3:]),
                         "atr_5m": sum(ranges) / len(ranges),
                         "method": "CLOSED_5M_TRUE_RANGE_MEAN_14"}
    return {"closed_structure": structure, "schema_version": VERSION, "inst_id": inst_id, "source": "OKX_CONTRACT_HISTORY",
        "as_of_ms": end, "observed_at_ms": observed_at_ms, "windows": rows, "previous_15m": previous,
        "change_vs_previous_15m": comparison, "summary": summary,
        "permission": "CONTEXT_ONLY_NEVER_CREATES_OR_CANCELS_TRIGGER",
        "notes": ["最近15m／1H／4H是重疊回看窗口，不是三張獨立多空票。",
                  "OI比較張數或幣數，不把美元估值變動當淨資金流入。",
                  "CVD為區間主動成交差額，不是App累積指標絕對值；缺少完整歷史不補值。"]}
