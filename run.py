#!/usr/bin/env python3
"""Production entrypoint: OKX USDT-SWAP MA × MACD read-only radar.

Replaces the previous Solana paper-trading runtime. No trading keys or
automatic execution. Previous code remains in dated git backup branches.
"""
from macd_radar.app import main

if __name__ == "__main__":
    main()
