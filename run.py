#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import sys
from urllib.request import Request, urlopen

from radar.api import OKXPublicClient
from radar.config import AppConfig
from radar.reporting import report_markdown
from radar.scanner import MarketScanner, ScannerConfig
from radar.service import RadarRuntime, serve


def build_runtime(config: AppConfig) -> RadarRuntime:
    previous_open_interest = _load_previous_open_interest(config.previous_report_url)
    client = OKXPublicClient(
        base_url=config.okx_base_url,
        timeout_seconds=config.request_timeout_seconds,
        retries=config.request_retries,
        rate_limit_requests=config.rate_limit_requests_per_2s,
        candle_rate_limit_requests=config.candle_rate_limit_requests_per_2s,
        rate_limit_backoff_seconds=config.rate_limit_backoff_seconds,
        rate_limit_max_backoff_seconds=config.rate_limit_max_backoff_seconds,
        execution_notional_usdt=config.execution_notional_usdt,
    )
    scanner = MarketScanner(
        client,
        ScannerConfig(
            max_signals=config.max_signals,
            max_watchlist=config.max_watchlist,
            workers=config.workers,
            candle_limit=config.candle_limit,
            candle_limit_4h=config.candle_limit_4h,
            candle_limit_1h=config.candle_limit_1h,
            candle_limit_15m=config.candle_limit_15m,
            candle_limit_5m=config.candle_limit_5m,
            min_quote_volume_24h=config.min_quote_volume_24h,
            max_spread_pct=config.max_spread_pct,
            min_open_interest_usd=config.min_open_interest_usd,
            require_micro_volume_anomaly=config.require_micro_volume_anomaly,
            minimum_rr=config.minimum_rr,
            context_candidates=config.context_candidates,
            estimated_taker_fee_pct=config.estimated_taker_fee_pct,
            max_execution_cost_to_risk_pct=config.max_execution_cost_to_risk_pct,
            max_slippage_pct=config.max_slippage_pct,
            max_entry_extension_atr=config.max_entry_extension_atr,
            severe_entry_extension_atr=config.severe_entry_extension_atr,
            previous_open_interest_usd=previous_open_interest,
            core_recovery_attempts=config.core_recovery_attempts,
            context_recovery_attempts=config.context_recovery_attempts,
            recovery_workers=config.recovery_workers,
        ),
    )
    return RadarRuntime(scanner, config)


def _load_previous_open_interest(url: str) -> dict[str, float]:
    if not url:
        return {}
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "okx-usdt-perp-radar/0.3 (public-data-only)",
        },
    )
    try:
        with urlopen(request, timeout=15.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        logging.getLogger("okx_radar").warning(
            "Previous report unavailable; OI change will be blank: %s",
            exc,
        )
        return {}
    return _extract_previous_open_interest(payload)


def _extract_previous_open_interest(payload: object) -> dict[str, float]:
    if not isinstance(payload, dict):
        return {}
    output: dict[str, float] = {}
    for section in ("market_map", "watchlist", "signals"):
        rows = payload.get(section, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            inst_id = row.get("inst_id")
            metrics = row.get("market_metrics", {})
            oi = metrics.get("open_interest_usd") if isinstance(metrics, dict) else None
            if isinstance(inst_id, str) and isinstance(oi, (int, float)) and oi > 0:
                output[inst_id] = float(oi)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="OKX 全 USDT 永續分析雷達（公開資料、無下單）")
    parser.add_argument("--config", default="config.json", help="JSON 設定檔路徑")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="完整掃描一次後結束")
    mode.add_argument("--serve", action="store_true", help="啟動按需掃描網頁服務（預設）")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = AppConfig.load(args.config)
        runtime = build_runtime(config)
        if args.once:
            report = runtime.scan_blocking()
            print(report_markdown(report))
            return 2 if report.status == "DATA_INCOMPLETE" else 0
        serve(runtime, config.host, config.port)
        return 0
    except Exception as exc:
        logging.exception("Radar failed")
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
