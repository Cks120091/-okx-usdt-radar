"""Independent zero-key, read-only MACD radar prototype using OKX public endpoints.

Run from repository root: python -m macd_radar.app
The previous OKX strategy and Solana application are never imported or modified.
"""
from __future__ import annotations
import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from .core import calculate
from .technical import combined_analysis
from .dual import dual_confirm

API_BASE = "https://www.okx.com"
TIMEFRAMES = {"15m": 900, "1H": 3600, "4H": 14400, "1D": 86400}
# Allow 2m exchange-finalization/network delay before calling a bar stale.
# Freshness is based on the *last confirmed K close time*, NOT the ticker
# timestamp or the wall-clock time when the API scan was requested.
CANDLE_FRESHNESS_GRACE_MS = 2 * 60 * 1000
# USDT-denominated, *estimated* trailing 24h traded notional.
# OKX's derivative SWAP volCcy24h reports base-currency quantity, not
# actual accumulated USDT trade turnover. Converting at latest trade price
# is a snapshot estimate; borderline markets may differ from exact turnover.
MIN_24H_TURNOVER_USDT_EST = 1_500_000.0
# OKX instruments: 1=crypto, 3=stocks, 4=commodities.
# Only the official spot-gold contract XAU-USDT-SWAP from category 4
# is explicitly allowed. All other commodities are excluded.
CRYPTO_INST_CATEGORY = "1"
GOLD_INST_CATEGORY = "4"
GOLD_USDT_SWAP_INST_ID = "XAU-USDT-SWAP"
INSTRUMENT_CACHE_SECONDS = 300
_CACHE = {}
_LOCK = threading.Lock()
_INSTRUMENT_CACHE = None
_INSTRUMENT_LOCK = threading.Lock()


def okx_get(endpoint: str, **params):
    url = API_BASE + endpoint + "?" + urlencode(params)
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "macd-readonly-radar/1.0"})
    with urlopen(request, timeout=12) as response:
        body = json.load(response)
    if body.get("code") != "0" or not isinstance(body.get("data"), list):
        raise RuntimeError("OKX API error: " + str(body.get("msg") or body.get("code")))
    return body["data"]


def safe_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def estimated_24h_turnover_usdt(ticker):
    """Approximate 24h USDT notional for a linear USDT SWAP.

    SWAP volCcy24h is underlying/base-coin quantity; *not* quote USDT.
    It cannot be compared directly to the threshold. Convert via last
    trade price and discard absent, zero, infinite or NaN inputs.
    """
    if not ticker.get("instId", "").endswith("-USDT-SWAP"):
        return 0.0
    try:
        base_qty = float(ticker["volCcy24h"])
        last = float(ticker["last"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return 0.0
    if not (math.isfinite(base_qty) and math.isfinite(last)) or base_qty <= 0 or last <= 0:
        return 0.0
    amount = base_qty * last
    return amount if math.isfinite(amount) else 0.0


def eligible_usdt_swaps(tickers, minimum=MIN_24H_TURNOVER_USDT_EST):
    """Hard gate applied BEFORE market candles, MA/MACD, or signal ranking."""
    if not math.isfinite(minimum) or minimum < 0:
        raise ValueError("Invalid 24h turnover minimum")
    eligible = []
    for ticker in tickers:
        notional = estimated_24h_turnover_usdt(ticker)
        if notional < minimum:
            continue
        eligible.append({**ticker, "turnover_24h_usdt_est": notional})
    eligible.sort(key=lambda row: row["turnover_24h_usdt_est"], reverse=True)
    return eligible


def eligible_asset_usdt_swap_ids(instruments):
    """Allow live crypto + EXACTLY ONE gold USDT perpetual.

    Normal crypto is allowed only with exchange instCategory=1.
    Gold is XAU-USDT-SWAP with instCategory=4, confirmed by OKX
    exchange API documentation and its 2026-04-22 reclassification log.
    Stocks/indices, silver, crude oil, forex and unknown classifications
    fail closed. Category 4 alone never qualifies a contract.
    """
    return frozenset(
        item["instId"] for item in instruments
        if item.get("instType") == "SWAP"
        and item.get("instId", "").endswith("-USDT-SWAP")
        and (
            str(item.get("instCategory", "")) == CRYPTO_INST_CATEGORY
            or (
                str(item.get("instCategory", "")) == GOLD_INST_CATEGORY
                and item["instId"] == GOLD_USDT_SWAP_INST_ID
            )
        )
        and item.get("state", "live") == "live"
    )


def active_eligible_asset_usdt_swap_ids():
    """Cache positive exchange metadata for five minutes to limit API load."""
    global _INSTRUMENT_CACHE
    now = time.monotonic()
    with _INSTRUMENT_LOCK:
        if _INSTRUMENT_CACHE is not None and now - _INSTRUMENT_CACHE[0] < INSTRUMENT_CACHE_SECONDS:
            return _INSTRUMENT_CACHE[1]
        instruments = okx_get("/api/v5/public/instruments", instType="SWAP")
        # On category metadata outage, do NOT fall back to scanning all SWAPs.
        eligible_ids = eligible_asset_usdt_swap_ids(instruments)
        if not eligible_ids:
            raise RuntimeError("OKX 合約類別查詢無有效加密貨幣／黃金標的，已停止掃描以防誤納股票合約")
        _INSTRUMENT_CACHE = (time.monotonic(), eligible_ids)
        return eligible_ids


def ranked_usdt_swaps(limit: int):
    # Exchange asset-category allowlist executes BEFORE volume and K candles.
    valid_ids = active_eligible_asset_usdt_swap_ids()
    items = okx_get("/api/v5/market/tickers", instType="SWAP")
    ranked = eligible_usdt_swaps(
        item for item in items if item.get("instId") in valid_ids
    )
    selected = ranked[:limit]
    # Keep one slot for active, above-threshold gold, even when crypto volume
    # dominates the top-N. This does NOT bypass the 1.5M USDT volume gate.
    gold = next((row for row in ranked if row["instId"] == GOLD_USDT_SWAP_INST_ID), None)
    if gold is not None and gold not in selected and limit > 0:
        selected = ranked[:limit-1] + [gold]
        selected.sort(key=lambda row: row["turnover_24h_usdt_est"], reverse=True)
    return selected


def validate_confirmed_bar_freshness(bar_end_ms: int, timeframe: str, now_ms: int | None = None) -> dict:
    """Refuse outdated/future K data even when OKX marks a candle confirm=1.

    At any moment the latest completed candle should have ended within
    one full bar length plus a 2-minute API-finalization grace. During market
    pauses (e.g. some commodity contracts), old K bars are NOT signals.
    """
    if timeframe not in TIMEFRAMES:
        raise ValueError("未知 K 線週期")
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    age_ms = current_ms - int(bar_end_ms)
    if age_ms < -CANDLE_FRESHNESS_GRACE_MS:
        raise ValueError("K 線時間超前伺服器時鐘：拒絕產生訊號")
    allowed_ms = TIMEFRAMES[timeframe] * 1000 + CANDLE_FRESHNESS_GRACE_MS
    if age_ms > allowed_ms:
        age_min = round(age_ms / 60000, 1)
        raise ValueError(
            f"行情資料落後：最新已收盤 K 線是 {age_min} 分鐘前（{timeframe}）；"
            "禁止產生正式雙確認，請核對 OKX 市場資料與合約來源"
        )
    return {
        "bar_age_seconds": max(0, round(age_ms / 1000, 1)),
        "freshness_max_seconds": round(allowed_ms / 1000, 1),
        "fresh": True,
    }


def scan_one(ticker, timeframe):
    inst = ticker["instId"]
    candles = okx_get("/api/v5/market/candles", instId=inst, bar=timeframe, limit=300)
    confirmed = {}
    for row in candles:
        if len(row) >= 9 and str(row[8]) == "1" and safe_number(row[4]) > 0:
            confirmed[int(row[0])] = (float(row[2]), float(row[3]), float(row[4]))
    if len(confirmed) < 100:
        raise ValueError("不足 100 根已收盤 K 線")
    times = sorted(confirmed)
    last_bar_end = times[-1] + 1000 * TIMEFRAMES[timeframe]
    freshness = validate_confirmed_bar_freshness(last_bar_end, timeframe)
    closes = [confirmed[t][2] for t in times]
    highs = [confirmed[t][0] for t in times]
    lows = [confirmed[t][1] for t in times]
    metrics = calculate(closes).as_dict()
    technical = combined_analysis(closes, highs, lows, times, ma_type="SMA")
    dual = dual_confirm(closes, ma_type="SMA")
    bar_start = times[-1]
    bar_end = last_bar_end
    priority = (
        0 if dual["is_new"] else
        1 if dual["confirmed"] else
        2 if technical["ma"]["structure_trigger"] != "無新結構確認" else
        3 if metrics["event"] != "無新交叉" else
        4 if technical["divergences"] else 5
    )
    return {
        "instId": inst, "last": safe_number(ticker.get("last")),
        "turnover_24h_usdt_est": ticker["turnover_24h_usdt_est"],
        "confirmed_close": closes[-1],
        "confirmed_bar_start_ms": bar_start, "confirmed_bar_end_ms": bar_end,
        "data_quality": freshness,
        "rank_priority": priority, **metrics, **technical, "dual": dual,
    }


def do_scan(timeframe, limit):
    start = time.monotonic()
    rows = ranked_usdt_swaps(limit)
    signals, errors = [], []
    with ThreadPoolExecutor(max_workers=3) as workers:
        futures = {workers.submit(scan_one, row, timeframe): row["instId"] for row in rows}
        for job in as_completed(futures):
            inst_id = futures[job]
            try:
                signals.append(job.result())
            except Exception as exc:
                errors.append(f"{inst_id}: {str(exc)[:130]}")
    signals.sort(key=lambda r: (r["rank_priority"], -r["normalized_histogram"]))
    return {
        "mode": "READ_ONLY", "timeframe": timeframe, "requested": len(rows),
        "asset_filter": "OKX category=1 crypto plus category=4 XAU-USDT-SWAP gold only; exclude stocks/indices/other commodities/forex",
        "turnover_24h_min_usdt_est": MIN_24H_TURNOVER_USDT_EST,
        "turnover_basis": "SWAP 24h underlying/base volume × latest price; estimate only",
        "universe": "OKX crypto plus XAU gold USDT perpetual; estimated 24h turnover >= 1,500,000 USDT",
        "count": len(signals), "items": signals, "errors": errors[:12],
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "seconds": round(time.monotonic() - start, 2),
        "note": "掃描OKX類別1加密貨幣及類別4的XAU-USDT-SWAP黃金永續；排除股票/股指/白銀/原油/外匯，24h估算成交額>=150萬USDT；估算非精確成交額。MA與MACD實際同向交叉，先後不限且無固定5根截止；兩者仍有效才算確認；其他僅觀察。4MA暫用SMA。"
    }


def cached_scan(timeframe, limit):
    key = (timeframe, limit)
    now = time.monotonic()
    with _LOCK:
        old = _CACHE.get(key)
        if old and now - old[0] < 50:
            return old[1]
    data = do_scan(timeframe, limit)
    with _LOCK:
        _CACHE[key] = (time.monotonic(), data)
    return data


class Handler(BaseHTTPRequestHandler):
    def send_json(self, data, status=200):
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path in ("/", "/index.html"):
            payload = (Path(__file__).parent / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/health":
            self.send_json({"status": "ok", "application": "macd-radar", "trading": False})
            return
        if parsed.path != "/api/scan":
            self.send_json({"error": "not_found"}, status=404)
            return
        q = parse_qs(parsed.query)
        tf = q.get("tf", ["15m"])[0]
        if tf not in TIMEFRAMES:
            self.send_json({"error": "unsupported timeframe"}, status=400)
            return
        try:
            limit = max(5, min(40, int(q.get("limit", ["20"])[0])))
            self.send_json(cached_scan(tf, limit))
        except Exception as exc:
            self.send_json({"error": str(exc)[:180], "items": [], "timeframe": tf}, status=502)


def main() -> None:
    port = int(os.getenv("PORT", "8000"))
    print(f"MA × MACD read-only radar: http://localhost:{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
