"""Fresh-quote signal/location bridge; terminal trades are never revived."""
from __future__ import annotations
from .entry_position import POLICY_VERSION, FORMAL_STAGES, POSITION_STATUSES, POSITION_CODES, describe_position, number

AVAILABLE={'AVAILABLE','COMPLETE','COMPLETED','FRESH','OK'}
TERMINAL={'INVALIDATED','PLAN_INVALIDATED','TARGET_REACHED','COMPLETED','CLOSED','CLOSED_UNKNOWN','SUPERSEDED','STOP_HIT','SL_HIT','TP1_FIRST','SL_FIRST'}


def apply_position_policy(payload, signal):
    if signal is None:
        return payload
    verdict=payload.setdefault('verdict',{})
    life=payload.setdefault('signal_lifecycle',{})
    plan=payload.setdefault('plan_state',{})
    original=payload.get('original',{})
    live=payload.get('live',{})
    saved_life=getattr(signal,'lifecycle',{}) or {}
    saved_decision=getattr(signal,'decision_context',{}) or {}
    saved_final=saved_decision.get('final',{}) or {}
    original_status=str(verdict.get('status') or 'UNKNOWN').upper()
    position=describe_position(signal,current_price=live.get('price'),source=live.get('price_source'),original_status=original_status)
    if str(verdict.get('situation') or '').upper()=='NEAR_INVALIDATION':
        position['risk_note']='接近原止損，風險較高；不放寬止損。'
    payload['entry_position']=position
    payload['entry_policy_version']=POLICY_VERSION
    verdict['position_policy']=POLICY_VERSION
    verdict['signal_active']=False
    terminal_values={str(value or '').upper() for value in (
        getattr(signal,'signal_stage',''),saved_life.get('status'),saved_life.get('current_stage'),saved_life.get('outcome'),
        life.get('status'),verdict.get('situation'),verdict.get('status'),plan.get('status'))}
    if terminal_values & TERMINAL or saved_life.get('terminal') is True or life.get('terminal') is True:
        if life.get('terminal') is not True:
            saved_values={str(value or '').upper() for value in (saved_life.get('status'),saved_life.get('current_stage'),saved_life.get('outcome'),getattr(signal,'signal_stage',''))}
            profit=bool(saved_values & {'COMPLETED','TARGET_REACHED','TP1_FIRST'})
            stopped=bool(saved_values & {'INVALIDATED','PLAN_INVALIDATED','STOP_HIT','SL_HIT','SL_FIRST'})
            state='COMPLETED' if profit else 'INVALIDATED' if stopped else 'CLOSED_UNKNOWN'
            life.update(status=state,terminal=True,label='原交易計畫已結束')
            verdict.update(status='MISSED_ENTRY' if profit else 'PLAN_INVALIDATED' if stopped else 'DATA_UNAVAILABLE',
                           situation='TARGET_REACHED' if profit else 'INVALIDATED' if stopped else 'CLOSED_UNKNOWN',label='原交易計畫已結束')
            plan.update(status=state,new_trigger_required=True,old_plan_reusable=False)
        verdict['actionable']=False
        verdict['new_entry_allowed']=False
        plan['new_entry_allowed']=False
        plan['old_plan_reusable_for_new_entry']=False
        return payload
    blockers=list(dict.fromkeys(str(v).upper() for v in verdict.get('hard_blockers',[])))
    blockers=[v for v in blockers if v not in POSITION_CODES]
    stage=str(getattr(signal,'signal_stage','')).upper()
    story=getattr(signal,'market_story',{}) or {}
    trigger=story.get('trigger',{}) or {}
    if stage not in FORMAL_STAGES or (trigger.get('triggered') is False and trigger.get('type') != 'ACTIVE_EPISODE'):
        blockers.append('NO_FORMAL_TRIGGER')
    if trigger.get('new_entry_suspended') is True or trigger.get('opposite_warning_only') is True:
        blockers.append('OPPOSITE_SIGNAL')
    from .decision import _timeframe_direction_alignment
    alignment=_timeframe_direction_alignment(signal,str(getattr(signal,'direction','')).upper())
    stored_alignment=saved_final.get('timeframe_alignment',{}) or {}
    if alignment.get('passed') is False or (alignment.get('passed') is None and stored_alignment.get('passed') is False):
        blockers.append('TIMEFRAME_DIRECTION_ALIGNMENT')
    data=getattr(signal,'data_quality',{}) or {}
    core=str(data.get('core') or data.get('core_status') or '').upper()
    if core and core not in AVAILABLE:
        blockers.append('CORE_DATA_UNAVAILABLE')
    required=list(payload.get('data_quality',{}).get('required_missing_sources',[]) or [])
    if required:
        blockers.append('REQUIRED_DATA_UNAVAILABLE')
    low,high,stop,target=(number(original.get(k)) for k in ('entry_low','entry_high','stop_loss','take_profit_1'))
    direction=str(getattr(signal,'direction','')).upper()
    geometry=all(v is not None and v>0 for v in (low,high,stop,target))
    if geometry:
        geometry=low<=high and (stop<low<=high<target if direction=='LONG' else target<low<=high<stop if direction=='SHORT' else False)
    if not geometry or position['current_price'] is None or position['current_price']<=0:
        blockers.append('STORED_PLAN_DATA_UNAVAILABLE')
    saved_wait=saved_final.get('wait_reason',{}) or {}
    if saved_wait.get('code') in {'TIMEFRAME_DIRECTION_ALIGNMENT','EVIDENCE_CONFLICT'}:
        blockers.append(saved_wait['code'])
    blockers=list(dict.fromkeys(blockers))
    verdict['hard_blockers']=blockers
    if blockers:
        missing=any('DATA' in v or 'MISSING' in v for v in blockers)
        verdict.update(status='DATA_UNAVAILABLE' if missing else 'HARD_GATE_BLOCKED',
            label='核心資料待更新' if missing else '訊號條件待確認',
            reason='／'.join({'OPPOSITE_SIGNAL':'已出現正式反向訊號', 'TIMEFRAME_DIRECTION_ALIGNMENT':'方向週期與觸發週期不同向', 'NO_FORMAL_TRIGGER':'正式訊號尚未成立', 'CORE_DATA_UNAVAILABLE':'核心資料不足', 'REQUIRED_DATA_UNAVAILABLE':'必要資料不足', 'STORED_PLAN_DATA_UNAVAILABLE':'原交易計畫資料不完整', 'EVIDENCE_CONFLICT':'方向證據有衝突'}.get(value,value) for value in blockers),actionable=False,new_entry_allowed=False)
        plan.update(status='ACTIVE_ENTRY_BLOCKED',new_entry_status='WAIT',new_entry_allowed=False,old_plan_reusable_for_new_entry=False)
        return payload
    positional_situations={'IN_ENTRY_AREA','FAVORABLE_AWAY','FAVORABLE_MISSED','ADVERSE_TOLERANCE','NEAR_INVALIDATION','WAIT_RETEST','ENTRY_WINDOW_CLOSED'}
    if original_status not in POSITION_STATUSES or str(verdict.get('situation') or '').upper() not in positional_situations:
        return payload
    verdict.update(status='ENTRY_READY',label='訊號已觸發',reason=position['label']+'；'+position['advice'],
                   actionable=True,new_entry_allowed=True,signal_active=True)
    plan.update(status='ACTIVE',old_plan_reusable=True,old_plan_reusable_for_new_entry=True,
                new_entry_status='READY',new_entry_allowed=True,new_trigger_required=False,
                note='原訊號與原價位保留；可進位置僅供參考，不是訊號成立條件。若已持倉，仍按原止損與止盈管理。')
    life['note']='訊號仍有效；進場位置獨立顯示，不代表此刻必須進場。'
    return payload
