#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys

from radar.api import OKXPublicClient
from radar.config import AppConfig
from radar.reporting import report_markdown, save_report
from radar.scanner import MarketScanner, ScannerConfig
from radar.service import RadarRuntime, serve


def build_runtime(config: AppConfig) -> RadarRuntime:
    client = OKXPublicClient(
        base_url=config.okx_base_url,
        timeout_seconds=config.request_timeout_seconds,
        retries=config.request_retries,
        rate_limit_requests=config.rate_limit_requests_per_2s,
    )
    scanner = MarketScanner(
        client,
        ScannerConfig(
            max_signals=config.max_signals,
            workers=config.workers,
            candle_limit=config.candle_limit,
            min_quote_volume_24h=config.min_quote_volume_24h,
            max_spread_pct=config.max_spread_pct,
            min_open_interest_usd=config.min_open_interest_usd,
            require_micro_volume_anomaly=config.require_micro_volume_anomaly,
            minimum_rr=config.minimum_rr,
            context_candidates=config.context_candidates,
        ),
    )
    return RadarRuntime(scanner, config)


def main() -> int:
    parser = argparse.ArgumentParser(description="OKX 全 USDT 永續分析雷達（公開資料、無下單）")
    parser.add_argument("--config", default="config.json", help="JSON 設定檔路徑")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="完整掃描一次後結束")
    mode.add_argument("--serve", action="store_true", help="啟動網頁與整點排程（預設）")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = AppConfig.load(args.config)
        runtime = build_runtime(config)
        if args.once:
            report = runtime.scanner.scan_once()
            save_report(report, config.data_dir)
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
