"""Trend-continuation focus, leaving other price patterns as reserve.

This is a presentation/notification selection policy, not a new market signal.
Core candles, original Episode identity, Entry/SL/TP and R:R stay immutable.
"""
from __future__ import annotations

from typing import Any


def is_continuation_focus(signal: Any) -> bool:
    """True only for an original continuation or independently confirmed reentry.

    An initial BREAKOUT or REVERSAL must *not* be upgraded because a newer
    price move merely resembles continuation. The core strategy explicitly
    labels REENTRY only after a distinct, confirmed continuation event.
    """
    if str(getattr(signal, "trigger_type", "") or "").upper() == "CONTINUATION":
        return True
    lifecycle = getattr(signal, "lifecycle", None)
    if not isinstance(lifecycle, dict):
        return False
    # Only the core repository can set this persistent marker after a true
    # newer, independently confirmed continuation REENTRY.
    try:
        accepted_confirmation = int(
            lifecycle.get("continuation_promotion_confirmation_ms") or 0
        )
    except (TypeError, ValueError, OverflowError):
        return False
    return accepted_confirmation > 0


def is_continuation_promotion(signal: Any) -> bool:
    """An older non-continuation Episode gained a newly confirmed REENTRY."""
    return bool(
        str(getattr(signal, "trigger_type", "") or "").upper() != "CONTINUATION"
        and is_continuation_focus(signal)
    )
