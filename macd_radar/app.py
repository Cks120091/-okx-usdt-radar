"""Independent zero-key, read-only MACD radar prototype using OKX public endpoints.

Run from repository root: python -m macd_radar.app
The previous OKX strategy and Solana application are never imported or modified.
"""
from __future__ import annotations
import json
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

API_BASE = "https://www.okx.com"
TIMEFRAMES = {"15m": 900, "1H": 3600, "4H": 14400, "1D": 86400}
_CACHE = {}
_LOCK = threading.Lock()


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


def ranked_usdt_swaps(limit: int):
    # USD notionals are approximate: the public SWAP ticker exposes 24h base-volume
    # and a last price, NOT an authoritative USDT turnover or liquidity figure.
    items = okx_get("/api/v5/market/tickers", instType="SWAP")
    items = [r for r in items if r.get("instId", "").endswith("-USDT-SWAP")
             and safe_number(r.get("last")) > 0]
    items.sort(key=lambda r: safe_number(r.get("volCcy24h")) * safe_number(r.get("last")), reverse=True)
    return items[:limit]


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
    closes = [confirmed[t][2] for t in times]
    highs = [confirmed[t][0] for t in times]
    lows = [confirmed[t][1] for t in times]
    metrics = calculate(closes).as_dict()
    technical = combined_analysis(closes, highs, lows, times, ma_type="SMA")
    bar_start = times[-1]
    bar_end = bar_start + 1000 * TIMEFRAMES[timeframe]
    priority = (
        0 if technical["ma"]["structure_trigger"] != "無新結構確認" and metrics["event"] != "無新交叉" else
        1 if technical["ma"]["structure_trigger"] != "無新結構確認" else
        2 if metrics["event"] != "無新交叉" else
        3 if technical["divergences"] else
        4 if metrics["phase"] in ("金叉預備", "死叉預備") else 5
    )
    return {
        "instId": inst, "last": safe_number(ticker.get("last")),
        "confirmed_close": closes[-1],
        "confirmed_bar_start_ms": bar_start, "confirmed_bar_end_ms": bar_end,
        "rank_priority": priority, **metrics, **technical,
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
        "count": len(signals), "items": signals, "errors": errors[:12],
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "seconds": round(time.monotonic() - start, 2),
        "note": "僅依已收盤K線確認交叉及樞紐背離；4MA暫以SMA計算，待核對原TradingView腳本；候選不代表勝率或交易許可。"
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
