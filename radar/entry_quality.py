"""Price-location scoring shared by scans and preflight; no lifecycle gates."""
from __future__ import annotations

from .position_advisory import number


def entry_location_quality(eligibility, *, invalidated=False, target_reached=False):
    price = number(eligibility.get("current_price"))
    low = number(eligibility.get("entry_low"))
    high = number(eligibility.get("entry_high"))
    chase = number(eligibility.get("chase_atr"))
    ready = max(number(eligibility.get("ready_max_chase_atr")) or .15, 1e-9)
    missed = max(number(eligibility.get("missed_chase_atr")) or .5, ready)
    if invalidated or target_reached:
        key, label, score = "PLAN_ENDED", "原交易計畫已結束", 0.0
    elif any(v is None for v in (price, low, high, chase)) or not 0 < low <= high or price <= 0:
        key, label, score = "UNKNOWN", "位置資料不足", 0.0
    elif low <= price <= high:
        key, label, score = "LIVE_ACCEPTABLE", "位於原進場區間", 95.0
    elif eligibility.get("remaining_rr_applicable") is False or (number(eligibility.get("adverse_atr")) or 0) > 0:
        key, label, score = "ADVERSE_OUTSIDE", "價格位於進場區不利側", 55.0
    elif chase <= ready:
        key, label, score = "LIVE_ACCEPTABLE", "價格略超出原進場區", max(75.0, 95.0 - chase / ready * 20)
    elif chase <= missed:
        key, label, score = "EXTENDED", "價格偏離原進場區", 55.0
    else:
        key, label, score = "SEVERE_CHASE", "價格明顯偏離原進場區", 10.0
    return {"key": key, "label": label, "score": round(score, 1),
            "extension_atr": chase, "basis": "ENTRY_ZONE_V2"}
