"""Percentage-based discretionary take-profit planning.

These computations never overwrite the scanner's canonical Entry/SL/TP/R:R,
never place orders, and never imply a guaranteed or fee-net return.
"""
from __future__ import annotations

import math


def percent_target_price(
    entry_price: float,
    direction: str,
    target_pct: float,
    *,
    basis: str,
    leverage: float = 1.0,
) -> float:
    """Return gross TP price for a LONG/SHORT.

    basis='PRICE_MOVE': target_pct is the underlying instrument move.
    basis='MARGIN_ROI': target_pct is gross estimated initial-margin ROI,
                        requiring the actual chosen leverage.
    Neither mode includes fees, funding, slippage or position adjustments.
    """
    numbers = (entry_price, target_pct, leverage)
    if not all(math.isfinite(float(value)) for value in numbers):
        raise ValueError("All inputs must be finite")
    if entry_price <= 0 or target_pct <= 0 or target_pct > 100:
        raise ValueError("Entry and profit target must be positive and bounded")
    if leverage < 1 or leverage > 125:
        raise ValueError("Leverage must be in range 1 to 125")
    side = str(direction).upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("Direction must be LONG or SHORT")
    mode = str(basis).upper()
    if mode not in {"PRICE_MOVE", "MARGIN_ROI"}:
        raise ValueError("basis must be PRICE_MOVE or MARGIN_ROI")
    move_pct = target_pct if mode == "PRICE_MOVE" else target_pct / leverage
    factor = 1 + move_pct / 100 if side == "LONG" else 1 - move_pct / 100
    if factor <= 0:
        raise ValueError("Target price is not valid")
    return entry_price * factor
