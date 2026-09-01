from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any


def signal_plan_display_fields(signal: Any) -> dict[str, float | int | None]:
    """Return the frozen plan values the browser may display verbatim.

    Entry is a deliberately asymmetric zone, so deriving R from its midpoint
    does not reproduce the strategy's canonical plan.  The Signal already
    stores TP1 R and the adaptive management plan stores TP2 R; expose those
    values together with the OKX tick size used to format every plan price.
    """

    metrics = _read(signal, "market_metrics", {})
    management = _read(signal, "management_plan", {})
    tick_size = _positive_number(_read(metrics, "instrument_tick_size"))
    return {
        "instrument_tick_size": tick_size,
        "display_precision": display_precision_from_tick_size(tick_size),
        "tp1_r": _positive_number(_read(signal, "risk_reward")),
        "tp2_r": _positive_number(_read(management, "tp2_rr_model")),
    }


def display_precision_from_tick_size(tick_size: Any) -> int | None:
    value = _positive_number(tick_size)
    if value is None:
        return None
    try:
        exponent = Decimal(str(value)).normalize().as_tuple().exponent
    except (InvalidOperation, ValueError):
        return None
    return max(0, -int(exponent))


def _positive_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _read(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)
