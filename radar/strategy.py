from __future__ import annotations

import inspect as _inspect
import math as _math

from . import _strategy_core as _core

# The core blob SHA is deliberately embedded so research/statistics fingerprints
# that hash radar/strategy.py still change when the retained core implementation
# changes.  This facade only changes entry policy; formal Trigger and plan geometry
# remain implemented by the retained core.
CORE_SOURCE_SHA = "ab9a259359bed02319b750aa759f0c2c191458b6"

for _name in dir(_core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_core, _name)

_original_entry_eligibility = _core._entry_eligibility
_original_feature_metrics = _core.AdaptiveStrategyEngine._feature_metrics


def _metric(value: object, digits: int = 4):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return round(number, digits) if _math.isfinite(number) else None


def _feature_metrics(tf):
    """Expose the existing hidden fusion as read-only core context.

    These fields consolidate overlapping EMA / RSI / MACD observations.  They
    do not create or cancel formal price Triggers and do not alter Entry/SL/TP
    geometry.
    """

    data = dict(_original_feature_metrics(tf))
    data.update(
        {
            "fusion_long_score": _metric(getattr(tf, "fusion_long_score", None), 2),
            "fusion_trend_score": _metric(getattr(tf, "fusion_trend_score", None), 2),
            "fusion_retest_score": _metric(getattr(tf, "fusion_retest_score", None), 2),
            "fusion_momentum_score": _metric(getattr(tf, "fusion_momentum_score", None), 2),
            "fusion_fast_score": _metric(getattr(tf, "fusion_fast_score", None), 2),
            "ema7": _metric(getattr(tf, "ema7", None), 10),
            "ema12": _metric(getattr(tf, "ema12", None), 10),
            "ema144": _metric(getattr(tf, "ema144", None), 10),
            "ema169": _metric(getattr(tf, "ema169", None), 10),
            "ema576": _metric(getattr(tf, "ema576", None), 10),
            "ema676": _metric(getattr(tf, "ema676", None), 10),
            "fusion_medium_tunnel_available": bool(
                getattr(tf, "fusion_medium_tunnel_available", False)
            ),
            "fusion_deep_tunnel_available": bool(
                getattr(tf, "fusion_deep_tunnel_available", False)
            ),
        }
    )
    return data


def _entry_eligibility(*args, **kwargs):
    """Keep Entry Zone authoritative; make a second closed retest advisory.

    Once a formal Trigger still exists and price is literally back inside the
    immutable Entry Zone, an older Episode no longer needs a *second* closed
    retest merely to regain entry permission.  The retest/reclaim remains
    visible as a quality confirmation.  Invalidation, target completion,
    adverse-side logic and chase limits remain owned by the original function.
    """

    result = dict(_original_entry_eligibility(*args, **kwargs))
    try:
        bound = _inspect.signature(_original_entry_eligibility).bind_partial(
            *args, **kwargs
        )
        bound.apply_defaults()
        values = bound.arguments
        current_price = float(values.get("current_price"))
        entry_low = float(values.get("entry_low"))
        entry_high = float(values.get("entry_high"))
    except (TypeError, ValueError, OverflowError):
        return result

    confirmation_only_wait = bool(
        result.get("status") == "WAIT_RETEST"
        and result.get("reentry_confirmation_required") is True
        and result.get("closed_retest_confirmed") is not True
        and entry_low <= current_price <= entry_high
    )
    if confirmation_only_wait:
        result.update(
            {
                "status": "ENTRY_READY",
                "label": "目前可進｜回踩確認列為加分",
                "reason": (
                    "價格已回到原 Entry Zone，正式 Trigger 與原 SL／TP 仍有效；"
                    "新的收線 retest／reclaim 改列品質確認，不再單獨封鎖新進場。"
                ),
                "actionable": True,
                "reentry_confirmation_advisory": True,
                "entry_policy": "FUSION_BALANCED_V1",
            }
        )
    else:
        result.setdefault("reentry_confirmation_advisory", False)
        result.setdefault("entry_policy", "FUSION_BALANCED_V1")
    return result


# Patch globals used by classes/functions defined in the retained core module,
# then re-export the patched class/function through this public facade.
_core._entry_eligibility = _entry_eligibility
_core.AdaptiveStrategyEngine._feature_metrics = staticmethod(_feature_metrics)
AdaptiveStrategyEngine = _core.AdaptiveStrategyEngine
