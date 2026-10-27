#!/usr/bin/env python3
"""Solana paper-only six-agent scanner; OKX radar is not imported."""
from __future__ import annotations
import argparse
import logging
import os
import sys
from solana_paper.engine import PaperRuntime, Settings
from solana_paper.web import serve

def main() -> int:
    parser = argparse.ArgumentParser(description="Solana 六代理土狗模擬交易台（絕不實盤）")
    parser.add_argument("--once", action="store_true", help="只掃描一次並模擬記帳")
    parser.add_argument("--serve", action="store_true", help="啟動網站與週期掃描（預設）")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        runtime = PaperRuntime(Settings.load())
        if args.once:
            print(runtime.run_once())
        else:
            serve(runtime, os.getenv("HOST", "0.0.0.0"), int(os.getenv("PORT", "8000")))
        return 0
    except Exception:
        logging.exception("Solana paper app startup error")
        print("Solana paper startup failed", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
