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
    if str(getattr(signal, "signal_stage", "") or "").upper() != "REENTRY":
        return False
    story = getattr(signal, "market_story", None)
    trigger = story.get("trigger") if isinstance(story, dict) else None
    if not (
        isinstance(trigger, dict)
        and trigger.get("triggered") is True
        and str(trigger.get("type") or "").upper() == "CONTINUATION"
    ):
        return False
    # REENTRY must carry a newer, independently confirmed candle event.
    try:
        confirmation = int(trigger.get("confirmation_ts") or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    return confirmation > 0


def is_continuation_promotion(signal: Any) -> bool:
    """An older non-continuation Episode gained a newly confirmed REENTRY."""
    return bool(
        str(getattr(signal, "trigger_type", "") or "").upper() != "CONTINUATION"
        and is_continuation_focus(signal)
    )
