"""Pure entry-location presentation; never grants or revokes a price Trigger."""
from __future__ import annotations
import math
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

_UNSET = object()

POLICY_VERSION = "SIGNAL_LOCATION_SEPARATION_V1"
POSITION_STATUSES = frozenset({"ENTRY_READY", "WAIT_RETEST", "MISSED_ENTRY", "NO_CHASE"})
POSITION_CODES = frozenset({"CHASE", "PRICE_TOO_FAR", "ENTRY_RETEST", "WAIT_RETEST", "FAVORABLE_AWAY", "FAVORABLE_MISSED", "ADVERSE_TOLERANCE", "NEAR_INVALIDATION", "ENTRY_WINDOW_CLOSED"})
FORMAL_STAGES = frozenset({"EARLY_SIGNAL", "CONFIRMED", "REENTRY"})


def read(item: Any, key: str, default=None):
    return item.get(key, default) if isinstance(item, Mapping) else getattr(item, key, default)


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value=float(value)
    except (TypeError,ValueError,OverflowError):
        return None
    return value if math.isfinite(value) else None


def describe_position(item: Any, *, current_price=_UNSET, source=None, original_status=None) -> dict:
    metrics=read(item,'market_metrics',{}) or {}
    entry=read(item,'entry_eligibility',{}) or {}
    low,high=number(read(item,'entry_low')),number(read(item,'entry_high'))
    if current_price is _UNSET:
        current_price=number(metrics.get('entry_execution_price'))
        if current_price is None:
            current_price=number(entry.get('current_price'))
        if current_price is None:
            current_price=number(metrics.get('last_price'))
    value=number(current_price)
    result={'policy_version':POLICY_VERSION,'advisory_only':True,'state':'UNKNOWN',
            'entry_low':low,'entry_high':high,'current_price':value,
            'price_source':str(source or metrics.get('entry_execution_price_source') or entry.get('current_price_source') or 'LAST'),
            'source_status':str(original_status or entry.get('status') or 'UNKNOWN'),
            'gap_pct':None,'label':'位置資料待更新','advice':'位置尚無法核對；不以舊值或零補算。'}
    if any(v is None or v <= 0 for v in (low,high,value)) or low > high:
        return result
    price_d,low_d,high_d=map(lambda v:Decimal(str(v)),(value,low,high))
    above=price_d>high_d; below=price_d<low_d
    state='ABOVE' if above else 'BELOW' if below else 'IN_ZONE'
    edge=high_d if above else low_d
    gap=float(abs(price_d-edge)/edge*100) if above or below else 0.0
    direction=str(read(item,'direction','NEUTRAL')).upper()
    label='目前位於可進位置' if state=='IN_ZONE' else '目前高於可進位置' if above else '目前低於可進位置'
    if state=='IN_ZONE':
        advice='價格位於原參考區間；訊號狀態另行判定。'
    elif (above and direction=='LONG') or (below and direction=='SHORT'):
        advice='可等回落再評估，不改變訊號狀態。' if above else '可等反彈再評估，不改變訊號狀態。'
    else:
        advice='價格往原計畫不利方向移動，留意原止損；位置本身不取消訊號。'
    return {**result,'state':state,'gap_pct':round(gap,6),'label':label,'advice':advice}
