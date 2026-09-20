"""Reviewed branch-local migration; no network calls or test suppression."""
from pathlib import Path
import ast
import textwrap


def edit(path, old, new, count=1):
    p=Path(path); text=p.read_text()
    if text.count(old) != count:
        raise RuntimeError(f'{path}: expected {count}, got {text.count(old)}: {old[:120]!r}')
    p.write_text(text.replace(old,new))


def put(path, value):
    p=Path(path); p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(textwrap.dedent(value).lstrip('\n'))


put('radar/entry_position.py', r'''
"""Pure entry-location presentation; never grants or revokes a price Trigger."""
from __future__ import annotations
import math
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

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


def describe_position(item: Any, *, current_price=None, source=None, original_status=None) -> dict:
    metrics=read(item,'market_metrics',{}) or {}
    entry=read(item,'entry_eligibility',{}) or {}
    low,high=number(read(item,'entry_low')),number(read(item,'entry_high'))
    if current_price is None:
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
''')

edit('radar/_decision_core.py',
     '            hard=False,\n        )\n\n    quote_volume =',
     '            hard=not (str(entry.get("status") or "").upper() in {"WAIT_RETEST", "MISSED_ENTRY", "NO_CHASE"}\n                      and _stage(item) in _FORMAL_STAGES\n                      and entry.get("direction_still_valid") is not False),\n        )\n\n    quote_volume =')
edit('radar/_decision_core.py', '''    elif missed_entry_no_chase and active_trigger:
        status, label = "ENTER", "訊號已觸發｜目前價格已離可進位置"
        wait_code, wait_label = "NONE", ""
    elif entry_status == "MISSED_ENTRY" and active_trigger:
        status, label = "ENTER", "訊號已觸發｜原可進位置僅供參考"
        wait_code, wait_label = "NONE", ""
''', '')
edit('radar/_decision_core.py','''    elif (
        entry_status == "ENTRY_READY"
        and active_trigger
        and conflict["blocks_entry"]
    ):''','''    elif active_trigger and conflict["blocks_entry"]:''')
edit('radar/_decision_core.py','''    elif entry_status == "ENTRY_READY" and active_trigger:
        status, label = (
            "ENTER",
            "目前可進｜附風險提醒" if has_risk_warnings else "目前可進",
        )
        wait_code, wait_label = "NONE", ""
    elif entry_status == "WAIT_RETEST" and active_trigger:
        status, label = "ENTER", "訊號已觸發｜等待回到較佳可進位置"
        wait_code, wait_label = "NONE", ""
''','''    elif entry_status in {"ENTRY_READY", "WAIT_RETEST", "MISSED_ENTRY", "NO_CHASE"} and active_trigger:
        status, label = "ENTER", "訊號已觸發｜位置獨立參考"
        wait_code, wait_label = "NONE", ""
    elif entry_status == "MISSED_ENTRY":
        status, label = "WAIT", "目前階段未提供有效新訊號"
        wait_code, wait_label = "ENTRY_WINDOW_CLOSED", "等待正式訊號重新確認"
''')
edit('radar/_decision_core.py','entry_label or "價格仍在合理進場區",','"訊號成立；可進位置另列參考，不代表目前位於區間內",')
edit('radar/_decision_core.py', '    active_trigger = plan_present and stage in _FORMAL_STAGES and not target_completed', '''    source_trigger = _mapping(_read(item, "trigger", None) or _mapping(_read(item, "market_story", {})).get("trigger", {}))
    trigger_explicitly_absent = source_trigger.get("triggered") is False and source_trigger.get("type") != "ACTIVE_EPISODE"
    active_trigger = plan_present and stage in _FORMAL_STAGES and not target_completed and not trigger_explicitly_absent''')
edit('radar/decision.py','from . import _decision_core as _core','from . import _decision_core as _core\nfrom .entry_position import POLICY_VERSION as POSITION_POLICY, describe_position')
edit('radar/decision.py','''    payload["final"] = final
    payload["policy"] = "MTF_DIRECTION_ALIGNMENT_V1"
''','''    final["position_policy"] = POSITION_POLICY
    final["entry_position"] = describe_position(item)
    final["signal_active"] = bool(final.get("new_entry_allowed"))
    if final.get("status") == "ENTER":
        final["label"] = "訊號已觸發"
    payload["final"] = final
    payload["policy"] = "MTF_DIRECTION_ALIGNMENT_V1"
''')
edit('radar/decision.py','行情已走一段，不建立新的追價型進場；等待回踩／反彈後重新形成 Trigger。','行情已走一段，追價風險較高，可等回踩／反彈後評估；僅為建議。')
edit('radar/decision.py','_SOFT_GATE_KEYS = {','_SOFT_GATE_KEYS = {\n    "CHASE", "PRICE_TOO_FAR", "ENTRY_RETEST", "ENTRY_WINDOW_CLOSED",')

put('radar/preflight_position.py', r'''
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
    payload['entry_position']=position
    payload['entry_policy_version']=POLICY_VERSION
    verdict['position_policy']=POLICY_VERSION
    verdict['signal_active']=False
    terminal_values={str(value or '').upper() for value in (
        getattr(signal,'signal_stage',''),saved_life.get('status'),saved_life.get('current_stage'),saved_life.get('outcome'),
        life.get('status'),verdict.get('situation'),verdict.get('status'),plan.get('status'))}
    if terminal_values & TERMINAL or saved_life.get('terminal') is True or life.get('terminal') is True:
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
            reason='／'.join(blockers),actionable=False,new_entry_allowed=False)
        plan.update(status='ACTIVE_ENTRY_BLOCKED',new_entry_status='WAIT',new_entry_allowed=False,old_plan_reusable_for_new_entry=False)
        return payload
    positional_situations={'IN_ENTRY_AREA','FAVORABLE_AWAY','FAVORABLE_MISSED','ADVERSE_TOLERANCE','NEAR_INVALIDATION','WAIT_RETEST','ENTRY_WINDOW_CLOSED'}
    if original_status not in POSITION_STATUSES or str(verdict.get('situation') or '').upper() not in positional_situations:
        return payload
    verdict.update(status='ENTRY_READY',label='訊號已觸發',reason=position['label']+'；'+position['advice'],
                   actionable=True,new_entry_allowed=True,signal_active=True)
    plan.update(status='ACTIVE',old_plan_reusable=True,old_plan_reusable_for_new_entry=True,
                new_entry_status='READY',new_entry_allowed=True,new_trigger_required=False,
                note='原訊號與原價位保留；可進位置僅供參考，不是訊號成立條件。')
    life['note']='訊號仍有效；進場位置獨立顯示，不代表此刻必須進場。'
    return payload
''')
edit('radar/preflight.py','    return payload\n','    from .preflight_position import apply_position_policy\n    return apply_position_policy(payload, signal)\n')
edit('radar/public_payload.py','''    return payload


def _public_continuation_observer''','''    final_source = _read(decision, "final", {})
    payload["final"].update(_select(final_source, ("position_policy", "signal_active")))
    position = _read(final_source, "entry_position", {})
    payload["final"]["entry_position"] = _select(position, ("policy_version", "advisory_only", "state", "entry_low", "entry_high", "current_price", "price_source", "source_status", "gap_pct", "label", "advice"))
    alignment = _read(final_source, "timeframe_alignment", {})
    payload["final"]["timeframe_alignment"] = _select(alignment, ("required", "passed", "state", "timeframe", "trigger_timeframe", "timeframe_direction", "trigger_direction", "long_score", "reason"))
    maturity = _read(final_source, "swing_maturity", {})
    payload["final"]["swing_maturity"] = _select(maturity, ("required", "passed", "state", "extension_atr", "limit_atr", "anchor_price", "fresh_retest", "reason"))
    return payload


def _public_continuation_observer''')
edit('radar/service.py','''    decision["final"] = final
    decision["episode_plan_state"] = {''','''    if preflight.get("entry_policy_version") == "SIGNAL_LOCATION_SEPARATION_V1":
        final["position_policy"] = "SIGNAL_LOCATION_SEPARATION_V1"
        final["entry_position"] = deepcopy(preflight.get("entry_position", {}))
        final["signal_active"] = final.get("new_entry_allowed") is True
        if final["signal_active"]:
            final["label"] = "訊號已觸發"
    decision["final"] = final
    decision["episode_plan_state"] = {''')
edit('radar/service.py','_BINDING_DIRECTION_CODES = {','_BINDING_DIRECTION_CODES = {\n    "TIMEFRAME_DIRECTION_ALIGNMENT", "EVIDENCE_CONFLICT", "NO_FORMAL_TRIGGER",')
put('docs/ENTRY_POSITION_POLICY.md', '''
# Signal and entry location separation

`SIGNAL_LOCATION_SEPARATION_V1` keeps Entry/SL/TP immutable. The canonical final
status `ENTER` / preflight compatibility status `ENTRY_READY` means an active
signal passed the independent core checks. It does NOT assert the quote is inside
Entry and does not represent an order or fill. Browser labels say 訊號已觸發.

`final.entry_position` (preflight: `entry_position`) carries original bounds,
current request quote, source, above/inside/below relation and gap. It is advice
only. Raw positional `entry_eligibility.status` remains available for history and
statistics. Do not enroll an entry-price cohort merely because signal_active=true.

Binding checks run BEFORE signal permission: actual terminal status, complete
plan/core data, formal opposite trigger, current core evidence conflict and known
1H/15m or 1D/4H disagreement. Unknown live quotes are never fabricated. A closed
trade cannot be resurrected by a quote returning to the old entry range.

Preflight obtains fresh Bid/Ask and reevaluates original plan validity; it does
not manufacture a fresh multi-timeframe candle analysis. Saved direction remains
as-of its original core analysis, unless a separate full scan re-confirms it.
''')
html=Path('radar/static/pages.html'); text=html.read_text()
marker='    function compactCoreCard(item){'; assert text.count(marker)==1
new_js=r'''    function separatedSignalView(item){
      const final=itemDecisionContext(item)?.final||{};
      return final.position_policy==='SIGNAL_LOCATION_SEPARATION_V1'?final:null;
    }
    function positionAdviceText(position){
      if(!position||position.state==='UNKNOWN')return '位置待更新';
      const gap=metricNumber(position.gap_pct),suffix=gap!==null&&gap>0?'（差 '+num(gap,2)+'%）':'';
      return String(position.label||'位置僅供參考')+suffix;
    }
    function separatedSignalCard(item){
      const final=separatedSignalView(item),snapshot=itemSnapshotEntryState(item),position=final.entry_position||{},terminal=terminalSignalOutcome(item),readonly=itemReadOnlyReason(item),expired=isExpiredSnapshot(item),preview=isPreviewItem(item);
      const allowed=!terminal&&!readonly&&!expired&&!preview&&final.new_entry_allowed===true;
      const side=item.direction==='LONG'?'做多':item.direction==='SHORT'?'做空':'方向待確認';
      const label=terminal?snapshot.label:preview?'初步訊號｜掃描中':expired?'資料已過期':readonly?snapshot.label:allowed?'⚡ '+side+'訊號已觸發':final.label||'訊號條件待確認';
      const advice=terminal?'原計畫已結束，不可沿用':readonly||expired?'保留快照，請更新':positionAdviceText(position);
      const source=String(position.price_source||''),quote=source.includes('ASK')?'掃描 Ask':source.includes('BID')?'掃描 Bid':'掃描價格',priceValue=position.current_price;
      const warning=final.swing_maturity?.state==='MATURE'?'行情已走一段，追價風險較高。':'';
      return '<div class="decision-panel '+(terminal?'closed':allowed?'ready':'wait')+'" data-signal-position-policy="v1"><div class="decision-top"><div class="decision-state">'+esc(label)+'</div><div class="decision-price"><span>'+esc(quote)+'</span><b>'+price(priceValue,item)+'</b></div></div><p class="decision-reason">'+esc(advice)+(warning?' · '+esc(warning):'')+'</p>'+signalTradeGrid(item,{original:Boolean(terminal)})+(terminal?'':'<div class="decision-primary-action">'+preflightActions(item.inst_id,item.radar_horizon==='LONG'?'LONG':'SHORT',item,false)+'</div>')+'</div>';
    }
'''
text=text.replace(marker,new_js+marker)
text=text.replace(marker,marker+"\n      if(separatedSignalView(item))return '';",1)
text=text.replace('    function decisionPanelBody(item){','    function decisionPanelBody(item){\n      if(separatedSignalView(item))return separatedSignalCard(item);',1)
text=text.replace('${itemDataPage(item,`<section class="data-section"><h3>資料／執行狀態</h3>${list(safetyItems(item),\'狀態待確認\')}</section>`)}', '${separatedSignalView(item)?\'\':itemDataPage(item,`<section class="data-section"><h3>資料／執行狀態</h3>${list(safetyItems(item),\'狀態待確認\')}</section>`)}')
needle="      if(status==='ENTRY_READY')return {label:'最近更新時條件通過'"; assert text.count(needle)==1
addition=r'''      if(data?.entry_policy_version==='SIGNAL_LOCATION_SEPARATION_V1'&&data?.verdict?.signal_active===true&&data?.verdict?.new_entry_allowed===true)return {label:'⚡ '+(data.direction==='SHORT'?'做空':'做多')+'訊號已觸發',detail:positionAdviceText(data.entry_position)+'；位置僅供參考，不是進場命令。',lifecycle:'⚡ 訊號已觸發｜有效中'};
'''
text=text.replace(needle,addition+needle)
text=text.replace("prefix+'ENTRY｜可進參考區間'","prefix+'ENTRY｜可進位置（參考）'")
text=text.replace(" /* compatibility: intradayFlowPanel(flow) remains the baseline renderer */",'')
html.write_text(text)

put('tests/test_entry_position_policy.py', r'''
import copy
import unittest
from dataclasses import replace
from radar.decision import build_decision_context
from radar.entry_position import describe_position, POLICY_VERSION
from radar.public_payload import public_candidate_payload
from tests.legacy_decision_cases import complete_signal
from tests.legacy_preflight_cases import make_signal, PreflightClient
from radar.preflight import build_preflight_payload
from radar.config import AppConfig

class EntryPositionPolicyTests(unittest.TestCase):
    def case(self, direction='LONG', horizon='SHORT'):
        item=complete_signal()
        item['direction']=direction; item['radar_horizon']=horizon
        if direction=='SHORT':
            item.update(stop_loss='102',take_profit_1='96',take_profit_2='94')
        item['market_metrics']['last_price']=100
        item['market_metrics']['raw_indicators']={('1H' if horizon=='SHORT' else '1D'):{'fusion_long_score':65 if direction=='LONG' else 35}}
        return item

    def test_positions_cannot_override_signal_or_fixed_prices(self):
        for direction in ('LONG','SHORT'):
            for horizon in ('SHORT','LONG'):
                for status,price_value in (('ENTRY_READY',100),('WAIT_RETEST',100.5),('MISSED_ENTRY',101.5),('NO_CHASE',99)):
                    with self.subTest(direction=direction,horizon=horizon,status=status):
                        item=self.case(direction,horizon)
                        item['entry_eligibility'].update(status=status,new_entry_allowed=status=='ENTRY_READY',chase_atr=2.5)
                        item['market_metrics']['entry_execution_price']=price_value
                        before=copy.deepcopy(item)
                        result=build_decision_context(item)
                        self.assertEqual(item,before)
                        self.assertEqual(result['final']['status'],'ENTER')
                        self.assertTrue(result['final']['new_entry_allowed'])
                        self.assertEqual(result['final']['position_policy'],POLICY_VERSION)
                        self.assertEqual(result['final']['entry_position']['current_price'],price_value)
                        self.assertNotIn('chase',result['hard_gate']['blockers'])

    def test_real_blockers_win_even_when_price_has_missed(self):
        mutations=[lambda i:i['data_quality'].update(core='UNAVAILABLE'),
                   lambda i:i['data_quality'].update(publication_ticker_status='UNAVAILABLE'),
                   lambda i:i['market_story']['trigger'].update(new_entry_suspended=True),
                   lambda i:i['market_story']['trigger'].update(triggered=False),
                   lambda i:i['market_metrics']['raw_indicators']['1H'].update(fusion_long_score=30),
                   lambda i:i.pop('stop_loss')]
        for mutate in mutations:
            item=self.case();item['entry_eligibility'].update(status='MISSED_ENTRY',new_entry_allowed=False,chase_atr=5)
            mutate(item)
            result=build_decision_context(item)
            self.assertFalse(result['final']['new_entry_allowed'],result['final'])
            self.assertNotEqual(result['final']['status'],'ENTER')

    def test_terminal_does_not_revive_at_old_entry(self):
        for terminal in ('INVALIDATED','COMPLETED','CLOSED_UNKNOWN'):
            item=self.case();item['lifecycle'].update(terminal=True,status=terminal)
            item['entry_eligibility'].update(status='MISSED_ENTRY')
            result=build_decision_context(item)
            self.assertFalse(result['final']['new_entry_allowed'])

    def test_position_boundary_unknown_and_units(self):
        item=self.case()
        for value,state in ((99.8,'IN_ZONE'),(100.2,'IN_ZONE'),(100.21,'ABOVE'),(99.79,'BELOW')):
            self.assertEqual(describe_position(item,current_price=value)['state'],state)
        self.assertEqual(describe_position(item,current_price=float('nan'))['state'],'UNKNOWN')
        self.assertEqual(describe_position(item,current_price=True)['state'],'UNKNOWN')

    def test_public_projection_preserves_safe_position_no_raw_history(self):
        item=self.case();item['entry_eligibility'].update(status='WAIT_RETEST')
        item['decision_context']=build_decision_context(item)
        item['decision_context']['final']['entry_position']['raw_points']=['private']
        out=public_candidate_payload(item)
        position=out['decision_context']['final']['entry_position']
        self.assertEqual(position['policy_version'],POLICY_VERSION)
        self.assertNotIn('raw_points',position)

    def test_preflight_fresh_quotes_and_stop_target_preserved(self):
        signal=make_signal(); before=copy.deepcopy(signal)
        for value,state in ((100,'IN_ZONE'),(100.5,'ABOVE'),(101.6,'ABOVE'),(99,'BELOW')):
            client=PreflightClient(value)
            result=build_preflight_payload(signal,client.get_ticker(signal.inst_id),client.get_execution_context(signal.inst_id),AppConfig(),report_generated_at='2026-09-20T00:00:00+00:00')
            self.assertTrue(result['verdict']['new_entry_allowed'],result['verdict'])
            self.assertEqual(result['entry_position']['state'],state)
            self.assertAlmostEqual(result['live']['price'],value+.01)
            self.assertEqual(result['original']['stop_loss'],98)
            self.assertEqual(result['original']['entry_high'],100.2)
        self.assertEqual(signal,before)
        for value in (97,105):
            client=PreflightClient(value)
            out=build_preflight_payload(signal,client.get_ticker(signal.inst_id),client.get_execution_context(signal.inst_id),AppConfig(),report_generated_at='2026-09-20T00:00:00+00:00')
            self.assertFalse(out['verdict'].get('new_entry_allowed',False))
            self.assertTrue(out['signal_lifecycle']['terminal'])

    def test_preflight_known_direction_and_core_errors_remain_binding(self):
        for raw,quality in (({'1H':{'fusion_long_score':30}},{}),({}, {'core':'UNAVAILABLE'})):
            signal=make_signal()
            signal=replace(signal,market_metrics={**signal.market_metrics,'raw_indicators':raw},data_quality=quality)
            client=PreflightClient(101.5)
            result=build_preflight_payload(signal,client.get_ticker(signal.inst_id),client.get_execution_context(signal.inst_id),AppConfig(),report_generated_at='2026-09-20T00:00:00+00:00')
            self.assertFalse(result['verdict']['new_entry_allowed'])
''')
print('PATCH_APPLIED: core precedence, preflight, public position data and compact renderer')
for path in ('radar/_decision_core.py','radar/decision.py','radar/preflight_position.py','radar/entry_position.py','radar/preflight.py','radar/service.py','radar/public_payload.py','tests/test_entry_position_policy.py'):
    ast.parse(Path(path).read_text(),filename=path)
