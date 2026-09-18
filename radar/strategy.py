from __future__ import annotations

import inspect as _inspect
import math as _math

from . import _strategy_core as _core

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

    These fields consolidate overlapping EMA / RSI / MACD observations. They
    never create/cancel formal price Triggers and never rewrite Entry/SL/TP.
    """
    data = dict(_original_feature_metrics(tf))
    data.update({
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
        "fusion_medium_tunnel_available": bool(getattr(tf, "fusion_medium_tunnel_available", False)),
        "fusion_deep_tunnel_available": bool(getattr(tf, "fusion_deep_tunnel_available", False)),
    })
    return data


def _entry_eligibility(*args, **kwargs):
    """Make duplicate retest confirmation advisory without reviving departed windows.

    A still-valid Episode that has never reached ENTRY_READY may regain entry
    permission when price is literally back inside its immutable Entry Zone;
    the extra closed retest becomes quality confirmation. If that Episode was
    already ENTRY_READY once and subsequently lost its durable entry window,
    the original closed-retest rule remains binding so a transient quote cannot
    resurrect a previously departed opportunity.
    """
    result = dict(_original_entry_eligibility(*args, **kwargs))
    try:
        bound = _inspect.signature(_original_entry_eligibility).bind_partial(*args, **kwargs)
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
        and result.get("entry_ready_once") is not True
        and entry_low <= current_price <= entry_high
    )
    if confirmation_only_wait:
        result.update({
            "status": "ENTRY_READY",
            "label": "掃描條件通過｜回踩確認列為加分",
            "reason": (
                "價格已回到原 Entry Zone，正式 Trigger 與原 SL／TP 仍有效；"
                "新的收線 retest／reclaim 改列品質確認，不再單獨封鎖新進場。"
            ),
            "actionable": True,
            "reentry_confirmation_advisory": True,
            "entry_policy": "FUSION_BALANCED_V1",
        })
    else:
        result.setdefault("reentry_confirmation_advisory", False)
        result.setdefault("entry_policy", "FUSION_BALANCED_V1")
    return result


_core._entry_eligibility = _entry_eligibility
_core.AdaptiveStrategyEngine._feature_metrics = staticmethod(_feature_metrics)
AdaptiveStrategyEngine = _core.AdaptiveStrategyEngine
