#!/usr/bin/env python3
"""Solana six-agent paper-only entrypoint; the old OKX radar is not imported."""
from __future__ import annotations
import argparse
import logging
import sys
from solana_paper.app import main as serve_main
from solana_paper.engine import PaperRuntime, Settings

def main() -> int:
    parser = argparse.ArgumentParser(description="Solana 六代理土狗模擬交易台（絕不實盤）")
    parser.add_argument("--once", action="store_true", help="只掃描一次並模擬記帳")
    parser.add_argument("--serve", action="store_true", help="啟動網站與週期掃描（預設）")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.once:
            result = PaperRuntime(Settings.load()).run_once()
            print(result)
        else:
            serve_main()
        return 0
    except Exception:
        logging.exception("Solana paper startup error")
        print("Solana paper startup failed", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
