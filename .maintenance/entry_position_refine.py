from pathlib import Path
import ast
import textwrap


def change(path, old, new, expected=1):
    p=Path(path);s=p.read_text()
    if s.count(old)!=expected:raise RuntimeError((path,old[:120],s.count(old)))
    p.write_text(s.replace(old,new))


def method(path, name, replacements):
    p=Path(path);s=p.read_text();lines=s.splitlines(True)
    nodes=[n for n in ast.walk(ast.parse(s)) if isinstance(n,ast.FunctionDef) and n.name==name]
    if len(nodes)!=1:raise RuntimeError((path,name,len(nodes)))
    n=nodes[0];seg=''.join(lines[n.lineno-1:n.end_lineno])
    for old,new in replacements:
        if old not in seg:raise RuntimeError((path,name,old))
        seg=seg.replace(old,new)
    p.write_text(''.join(lines[:n.lineno-1])+seg+''.join(lines[n.end_lineno:]))

# Actual data gaps may not fall back to a saved quote when a caller supplied None.
change('radar/entry_position.py','POLICY_VERSION =', '_UNSET = object()\n\nPOLICY_VERSION =')
change('radar/entry_position.py','current_price=None, source=None','current_price=_UNSET, source=None')
change('radar/entry_position.py','    if current_price is None:\n        current_price=number(metrics.get', '    if current_price is _UNSET:\n        current_price=number(metrics.get')

# Keep inactive stages distinct from active triggers. No bypass of core blockers.
change('radar/_decision_core.py','''        elif blockers == {"risk_reward"}:''','''        elif blockers == {"entry_permission"} and entry_status == "MISSED_ENTRY" and not active_trigger:
            status, label = "WAIT", "目前階段未提供有效新訊號"
            wait_code, wait_label = "ENTRY_WINDOW_CLOSED", "等待正式訊號重新確認"
        elif blockers == {"risk_reward"}:''')

# A structurally invalid or missing plan is not a positional warning.
change('radar/decision.py','''        if not formal_plan_present:
            kwargs = {**kwargs, "plan_present": False}''','''        if formal_plan_present:
            low, high, stop, target = (_core._number(_core._read(item, key, None))
                                      for key in ("entry_low", "entry_high", "stop_loss", "take_profit_1"))
            side = str(_core._read(item, "direction", "")).upper()
            formal_plan_present = all(value > 0 for value in (low, high, stop, target)) and low <= high
            formal_plan_present = formal_plan_present and (stop < low <= high < target if side == "LONG" else target < low <= high < stop if side == "SHORT" else False)
        if not formal_plan_present:
            kwargs = {**kwargs, "plan_present": False}''')

change('radar/preflight_position.py',"    payload['entry_position']=position", "    if str(verdict.get('situation') or '').upper()=='NEAR_INVALIDATION':\n        position['risk_note']='接近原止損，風險較高；不放寬止損。'\n    payload['entry_position']=position")
change('radar/preflight_position.py',"        verdict['actionable']=False\n", "        if life.get('terminal') is not True:\n            saved_values={str(value or '').upper() for value in (saved_life.get('status'),saved_life.get('current_stage'),saved_life.get('outcome'),getattr(signal,'signal_stage',''))}\n            profit=bool(saved_values & {'COMPLETED','TARGET_REACHED','TP1_FIRST'})\n            stopped=bool(saved_values & {'INVALIDATED','PLAN_INVALIDATED','STOP_HIT','SL_HIT','SL_FIRST'})\n            state='COMPLETED' if profit else 'INVALIDATED' if stopped else 'CLOSED_UNKNOWN'\n            life.update(status=state,terminal=True,label='原交易計畫已結束')\n            verdict.update(status='MISSED_ENTRY' if profit else 'PLAN_INVALIDATED' if stopped else 'DATA_UNAVAILABLE',\n                           situation='TARGET_REACHED' if profit else 'INVALIDATED' if stopped else 'CLOSED_UNKNOWN',label='原交易計畫已結束')\n            plan.update(status=state,new_trigger_required=True,old_plan_reusable=False)\n        verdict['actionable']=False\n")
change('radar/preflight_position.py',"            reason='／'.join(blockers),actionable=False,new_entry_allowed=False)", "            reason='／'.join({'OPPOSITE_SIGNAL':'已出現正式反向訊號', 'TIMEFRAME_DIRECTION_ALIGNMENT':'方向週期與觸發週期不同向', 'NO_FORMAL_TRIGGER':'正式訊號尚未成立', 'CORE_DATA_UNAVAILABLE':'核心資料不足', 'REQUIRED_DATA_UNAVAILABLE':'必要資料不足', 'STORED_PLAN_DATA_UNAVAILABLE':'原交易計畫資料不完整', 'EVIDENCE_CONFLICT':'方向證據有衝突'}.get(value,value) for value in blockers),actionable=False,new_entry_allowed=False)")
change('radar/preflight_position.py',"note='原訊號與原價位保留；可進位置僅供參考，不是訊號成立條件。'", "note='原訊號與原價位保留；可進位置僅供參考，不是訊號成立條件。若已持倉，仍按原止損與止盈管理。'")

# The repository's CAS guards stay binding. Lack of a new retest is only a
# historical position observation under this policy, not a signal cancellation.
change('radar/repository.py','''                    return _entry_window_changed(signal)
            window = entry_window_snapshot(signal, observed_ms, "OPEN" if ready else "SUSPENDED")''','''                    if signal.decision_context.get("final", {}).get("position_policy") != "SIGNAL_LOCATION_SEPARATION_V1":
                        return _entry_window_changed(signal)
                    ready = False  # retain the positional history; do not invent a new retest
            window = entry_window_snapshot(signal, observed_ms, "OPEN" if ready else "SUSPENDED")''')
change('radar/service.py','''    projected = replace(signal, actionable=allowed,
        entry_eligibility={**signal.entry_eligibility, "status": verdict.get("status"),
                           "actionable": allowed, "new_entry_allowed": allowed},
        decision_context={**signal.decision_context, "final": {"status": "ENTER" if allowed else "WAIT",
                                                               "new_entry_allowed": allowed}})''','''    separated = payload.get("entry_policy_version") == "SIGNAL_LOCATION_SEPARATION_V1"
    source_status = payload.get("entry_position", {}).get("source_status") if separated else verdict.get("status")
    final = {"status": "ENTER" if allowed else "WAIT", "new_entry_allowed": allowed}
    if separated:
        final["position_policy"] = "SIGNAL_LOCATION_SEPARATION_V1"
    projected = replace(signal, actionable=allowed,
        entry_eligibility={**signal.entry_eligibility, "status": source_status,
                           "actionable": allowed, "new_entry_allowed": allowed},
        decision_context={**signal.decision_context, "final": final})''')
change('radar/card_statistics.py','''    quote_ts = timestamp(metrics.get("ticker_sampled_at"))''','''    if (final.get("position_policy") == "SIGNAL_LOCATION_SEPARATION_V1"
            and mapping(final.get("entry_position")).get("state") != "IN_ZONE"):
        return False  # an alert away from Entry is not an observed entry-price sample
    quote_ts = timestamp(metrics.get("ticker_sampled_at"))''')

# Public and preflight prose preserve the near-SL caution without treating it as
# a positional veto. The original SL still determines actual invalidation.
change('radar/public_payload.py','"gap_pct", "label", "advice"))','"gap_pct", "label", "advice", "risk_note"))')
change('radar/static/pages.html',"positionAdviceText(data.entry_position)+'；位置僅供參考，不是進場命令。'", "positionAdviceText(data.entry_position)+'；'+(data.entry_position?.risk_note||'位置僅供參考，不是進場命令。')")

# One authoritative status is used by the list, the main card, and its preflight
# button. We do not turn raw historical position statuses into fake in-zone facts.
change('radar/static/pages.html',"      if(status==='HARD_GATE_BLOCKED')return 'HARD_GATE_BLOCKED';", "      if(final.position_policy==='SIGNAL_LOCATION_SEPARATION_V1'){if(itemHardGateBlocked(item))return 'HARD_GATE_BLOCKED';return finalStatus==='ENTER'&&final.new_entry_allowed===true?'ENTRY_READY':finalStatus||'WAIT'}\n      if(status==='HARD_GATE_BLOCKED')return 'HARD_GATE_BLOCKED';")
change('radar/static/pages.html',"if(itemCurrentEntryReady(item))return {key:'VERIFY',label:'掃描條件通過'", "if(itemCurrentEntryReady(item))return {key:'VERIFY',label:separatedSignalView(item)?'訊號已觸發':'掃描條件通過'")

# Correct a regression-test invocation; require real public serialization.
change('tests/test_entry_position_policy.py','public_candidate_payload(item)','public_candidate_payload(item, signal=True)')

# Update ONLY tests whose expected position veto was explicitly removed. All
# independent data, opposite, terminal, geometry, identity and price assertions
# stay in force. Source-specific tests still verify the unchanged raw distances.
for name in ('test_no_chase_preserves_trigger_and_is_not_invalidation',
             'test_missed_entry_below_severe_threshold_is_no_chase_not_severe_gate',
             'test_legacy_ready_status_is_vetoed_by_severe_live_chase',
             'test_legacy_episode_uses_numeric_quality_extension_as_chase_fallback'):
    pairs=[('self.assertEqual(result["final"]["status"], "NO_CHASE")','self.assertEqual(result["final"]["status"], "ENTER")'),
           ('self.assertFalse(result["final"]["new_entry_allowed"])','self.assertTrue(result["final"]["new_entry_allowed"])')]
    if name=='test_missed_entry_below_severe_threshold_is_no_chase_not_severe_gate':
        pairs.append(('self.assertEqual(result["hard_gate"]["blockers"], ["entry_permission"])','self.assertEqual(result["hard_gate"]["blockers"], [])'))
    method('tests/legacy_decision_cases.py',name,pairs)
method('tests/legacy_decision_cases.py','test_missing_live_chase_with_nonsevere_legacy_value_is_unknown',[
    ('self.assertIn("chase", result["hard_gate"]["unknowns"])','self.assertNotIn("chase", result["hard_gate"]["unknowns"])\n        self.assertFalse(chase["hard"])'),
    ('self.assertEqual(result["final"]["status"], "DATA_UNAVAILABLE")','self.assertEqual(result["final"]["status"], "ENTER")'),
    ('self.assertFalse(result["final"]["new_entry_allowed"])','self.assertTrue(result["final"]["new_entry_allowed"])')])
method('tests/legacy_decision_cases.py','test_wait_retest_and_missed_entry_are_not_terminal_states',[
    ('("WAIT_RETEST", "WAIT", "ENTRY_RETEST")','("WAIT_RETEST", "ENTER", None)'),
    ('("MISSED_ENTRY", "WAIT", "ENTRY_WINDOW_CLOSED")','("MISSED_ENTRY", "ENTER", None)'),
    ('result["final"]["wait_reason"]["code"]','result["final"]["wait_reason"]')])
method('tests/test_decision.py','test_epsilon_beyond_each_hard_gate_limit_vetoes_entry',[
    ('self.assertIn("chase", result["hard_gate"]["blockers"])','self.assertNotIn("chase", result["hard_gate"]["blockers"])'),
    ('self.assertFalse(result["final"]["new_entry_allowed"])','self.assertTrue(result["final"]["new_entry_allowed"])')])
method('tests/test_decision.py','test_missed_entry_position_keeps_priority_over_risk_warning',[
    ('self.assertIn("entry_permission", result["hard_gate"]["blockers"])','self.assertNotIn("entry_permission", result["hard_gate"]["blockers"])'),
    ('self.assertEqual(result["final"]["status"], "NO_CHASE")','self.assertEqual(result["final"]["status"], "ENTER")'),
    ('self.assertFalse(result["final"]["new_entry_allowed"])','self.assertTrue(result["final"]["new_entry_allowed"])')])
method('tests/test_card_statistics.py','test_final_publication_enrolls_once_preview_and_blocked_do_not',[
    ('self.assertFalse(report.signals[0].actionable)','self.assertTrue(report.signals[0].actionable)\n            self.assertNotEqual(report.signals[0].entry_eligibility["status"], "ENTRY_READY")')])
method('tests/legacy_scanner_cases.py','test_attach_decision_applies_canonical_chase_hard_gate',[
    ('"NO_CHASE",','"ENTER",'),('self.assertFalse(attached.actionable)','self.assertTrue(attached.actionable)'),
    ('self.assertFalse(attached.entry_eligibility["new_entry_allowed"])','self.assertTrue(attached.entry_eligibility["new_entry_allowed"])'),
    ('self.assertIn("chase", attached.entry_eligibility["hard_blockers"])','self.assertNotIn("chase", attached.entry_eligibility["hard_blockers"])')])
method('tests/legacy_scanner_cases.py','test_market_scan_rechecks_entry_with_publication_ticker',[
    ('self.assertFalse(signal.entry_eligibility["new_entry_allowed"])','self.assertTrue(signal.entry_eligibility["new_entry_allowed"])')])
method('tests/legacy_scanner_cases.py','test_single_scan_reuses_one_closed_oi_history_for_both_horizons',[
    ('# Missing 1H history provides no new closed retest proof, so a live\n        # quote back inside Entry must not reopen it.','# Missing auxiliary OI history does not erase a valid price signal.\n        # The raw positional retest status remains available as advice.'),
    ('self.assertFalse(degraded.short_result.signal.actionable)','self.assertTrue(degraded.short_result.signal.actionable)')])

method('tests/test_preflight.py','test_adverse_side_hides_artificial_live_rr_without_mutating_trigger',[
    ('self.assertEqual(payload["verdict"]["status"], "WAIT_RETEST")','self.assertEqual(payload["verdict"]["status"], "ENTRY_READY")'),
    ('self.assertFalse(payload["verdict"]["actionable"])','self.assertTrue(payload["verdict"]["actionable"])'),
    ('self.assertIn("接近失效", payload["verdict"]["label"])','self.assertIn("接近原止損", payload["entry_position"]["risk_note"])'),
    ('self.assertFalse(payload["plan_state"]["old_plan_reusable_for_new_entry"])','self.assertTrue(payload["plan_state"]["old_plan_reusable_for_new_entry"])')])
method('tests/legacy_preflight_cases.py','test_either_opposite_flag_remains_binding_while_price_already_waits',[
    ('self.assertEqual(payload["verdict"]["status"], "WAIT_RETEST")','self.assertEqual(payload["verdict"]["status"], "HARD_GATE_BLOCKED")')])
method('tests/legacy_preflight_cases.py','test_existing_episode_needs_closed_retest_before_live_price_can_reopen_it',[
    ('self.assertEqual(waiting["verdict"]["status"], "WAIT_RETEST")','self.assertEqual(waiting["verdict"]["status"], "ENTRY_READY")'),
    ('self.assertFalse(waiting["verdict"]["actionable"])','self.assertTrue(waiting["verdict"]["actionable"])'),
    ('self.assertFalse(\n            waiting["plan_state"]["old_plan_reusable_for_new_entry"]','self.assertTrue(\n            waiting["plan_state"]["old_plan_reusable_for_new_entry"]')])
method('tests/legacy_preflight_cases.py','test_small_adverse_move_keeps_trigger_active_with_retest_tolerance',[
    ('self.assertEqual(payload["verdict"]["status"], "WAIT_RETEST")','self.assertEqual(payload["verdict"]["status"], "ENTRY_READY")'),
    ('self.assertIn("容許回測中", payload["verdict"]["label"])','self.assertEqual(payload["entry_position"]["state"], "BELOW")'),
    ('self.assertEqual(payload["plan_state"]["new_entry_status"], "WAIT")','self.assertEqual(payload["plan_state"]["new_entry_status"], "READY")')])
method('tests/legacy_preflight_cases.py','test_favorable_move_shows_active_trigger_and_waits_without_chasing',[
    ('self.assertEqual(payload["verdict"]["status"], "WAIT_RETEST")','self.assertEqual(payload["verdict"]["status"], "ENTRY_READY")'),
    ('self.assertIn("已離開最佳進場點", payload["verdict"]["label"])','self.assertEqual(payload["entry_position"]["state"], "BELOW")'),
    ('self.assertFalse(payload["verdict"]["actionable"])','self.assertTrue(payload["verdict"]["actionable"])')])
method('tests/legacy_preflight_cases.py','test_favorable_move_beyond_entry_window_closes_only_new_entry',[
    ('self.assertEqual(payload["verdict"]["status"], "MISSED_ENTRY")','self.assertEqual(payload["verdict"]["status"], "ENTRY_READY")'),
    ('self.assertFalse(payload["plan_state"]["old_plan_reusable_for_new_entry"])','self.assertTrue(payload["plan_state"]["old_plan_reusable_for_new_entry"])')])
method('tests/legacy_short_entry_window_cases.py','test_wait_reason_is_not_misreported_as_price_outside_or_risk',[
    ("self.assertEqual(out['final']['status'],'WAIT')","self.assertEqual(out['final']['status'],'ENTER')"),
    ("self.assertIn('區間內',out['final']['wait_reason']['label'])","self.assertIsNone(out['final']['wait_reason'])\n        self.assertEqual(out['final']['entry_position']['source_status'],'WAIT_RETEST')")])
method('tests/legacy_short_entry_window_cases.py','test_preflight_departure_is_durable_then_return_stays_wait',[
    ("self.assertEqual(result['verdict']['status'],'WAIT_RETEST')","self.assertEqual(result['verdict']['status'],'ENTRY_READY')\n        self.assertEqual(old_report.lifecycle['entry_window']['state'],'SUSPENDED')\n        self.assertTrue(result['verdict']['signal_active'])")])
method('tests/test_ui_compact.py','test_trigger_lifecycle_is_separate_from_snapshot_entry_state',[
    ('ENTRY｜可進參考區間','ENTRY｜可進位置（參考）')])
print('REVIEWED REFINEMENTS APPLIED; independent safety and geometry tests retained')
method('tests/legacy_decision_cases.py','test_short_direction_uses_directional_volume_and_taker',[
    ('item["direction"] = "SHORT"','item["direction"] = "SHORT"\n        item.update(stop_loss="102", take_profit_1="96", take_profit_2="94")')])
method('tests/legacy_scanner_cases.py','test_market_scan_rechecks_entry_with_publication_ticker',[
    ('self.assertFalse(signal.actionable)','self.assertTrue(signal.actionable)'),('"NO_CHASE",','"ENTER",')])
method('tests/legacy_short_entry_window_cases.py','test_same_old_retest_does_not_reopen_suspended_window',[
    ('self.assertFalse(self.repo.record_entry_window(closed,NOW+2000).actionable)',
     "updated=self.repo.record_entry_window(closed,NOW+2000)\n        self.assertTrue(updated.actionable)\n        self.assertEqual(updated.lifecycle['entry_window']['state'],'SUSPENDED')")])
# Cached old positional blockers must not reappear as an upstream hard gate.
change('radar/_decision_core.py', '''            f"上游已標記新進場阻擋條件：{blocker}。",
        )''', '''            f"上游已標記新進場阻擋條件：{blocker}。",
            hard=not (blocker == "ENTRY_PERMISSION"
                      and str(entry.get("status") or "").upper() in {"WAIT_RETEST", "MISSED_ENTRY", "NO_CHASE"}
                      and _stage(item) in _FORMAL_STAGES
                      and entry.get("direction_still_valid") is not False),
        )''')
change('radar/decision.py','_ADVISORY_SAFETY_KEYS = {','_ADVISORY_SAFETY_KEYS = {\n    "ENTRY_ELIGIBILITY", "ENTRY_POSITION", "CHASE", "ENTRY_PERMISSION",')
# Add discovered tests, not standalone pytest-style functions ignored by CI.
p=Path('tests/test_entry_position_policy.py')
p.write_text(p.read_text()+r'''

    def test_cached_position_codes_do_not_hide_independent_blockers(self):
        item=self.case()
        item['entry_eligibility'].update(status='MISSED_ENTRY',new_entry_allowed=False,hard_blockers=['ENTRY_PERMISSION'])
        item['safety_checks'].append({'key':'entry_eligibility','hard':True,'passed':False})
        out=build_decision_context(item)
        self.assertTrue(out['final']['new_entry_allowed'])
        item['entry_eligibility']['hard_blockers'].append('CORE_DATA_UNAVAILABLE')
        self.assertFalse(build_decision_context(item)['final']['new_entry_allowed'])

    def test_unknown_quote_and_invalid_plan_cannot_be_called_in_zone(self):
        self.assertEqual(describe_position(self.case(),current_price=None)['state'],'UNKNOWN')
        for key,value in (('entry_low','0'),('entry_high','99'),('stop_loss','101'),('take_profit_1','99')):
            item=self.case();item[key]=value
            self.assertFalse(build_decision_context(item)['final']['new_entry_allowed'],(key,value))

    def test_preflight_short_bid_is_used_not_ask_or_scan_price(self):
        for horizon in ('SHORT','LONG'):
            signal=replace(make_signal(),direction='SHORT',radar_horizon=horizon,
                           stop_loss='102',take_profit_1='96',take_profit_2='94')
            for current,expected in ((100,'IN_ZONE'),(98.5,'BELOW'),(101,'ABOVE')):
                client=PreflightClient(current)
                result=build_preflight_payload(signal,client.get_ticker(signal.inst_id),client.get_execution_context(signal.inst_id),AppConfig(),report_generated_at='2026-09-20T00:00:00+00:00')
                self.assertTrue(result['verdict']['new_entry_allowed'])
                self.assertAlmostEqual(result['live']['price'],current-.01)
                self.assertEqual(result['live']['price_source'],'BEST_BID')
                self.assertEqual(result['entry_position']['state'],expected)
                self.assertEqual(result['original']['stop_loss'],102)
                self.assertEqual(result['original']['take_profit_1'],96)

    def test_old_terminal_preflight_never_reports_active_on_price_return(self):
        for status in ('COMPLETED','INVALIDATED','CLOSED_UNKNOWN'):
            signal=replace(make_signal(),lifecycle={'terminal':True,'status':status})
            client=PreflightClient(100)
            result=build_preflight_payload(signal,client.get_ticker(signal.inst_id),client.get_execution_context(signal.inst_id),AppConfig(),report_generated_at='2026-09-20T00:00:00+00:00')
            self.assertFalse(result['verdict']['new_entry_allowed'])
            self.assertTrue(result['signal_lifecycle']['terminal'])
            self.assertEqual(result['signal_lifecycle']['status'],status)

    def test_single_scan_projection_keeps_signal_and_position_separate(self):
        from radar.service import _canonical_single_decision
        from types import SimpleNamespace
        signal=make_signal();client=PreflightClient(101.5)
        payload=build_preflight_payload(signal,client.get_ticker(signal.inst_id),client.get_execution_context(signal.inst_id),AppConfig(),report_generated_at='2026-09-20T00:00:00+00:00')
        item=self.case();item['decision_context']=build_decision_context(item)
        out=_canonical_single_decision(SimpleNamespace(**item),payload,None)
        self.assertTrue(out['final']['new_entry_allowed'])
        self.assertEqual(out['final']['entry_position']['state'],'ABOVE')
        self.assertEqual(out['final']['label'],'訊號已觸發')
        payload['verdict']['hard_blockers']=['OPPOSITE_SIGNAL']
        out=_canonical_single_decision(SimpleNamespace(**item),payload,None)
        self.assertFalse(out['final']['new_entry_allowed'])
''')
change('radar/static/pages.html',"const allowed=!terminal&&!readonly&&!expired&&!preview&&final.new_entry_allowed===true;", "const gate=itemHardGate(item),allowed=!terminal&&!readonly&&!expired&&!preview&&final.status==='ENTER'&&final.new_entry_allowed===true&&gate.status==='PASSED'&&gate.passed===true&&gate.blocked!==true&&gate.unknown!==true;")
change('radar/static/pages.html',"if(data?.entry_policy_version==='SIGNAL_LOCATION_SEPARATION_V1'&&data?.verdict?.signal_active===true", "if(status==='ENTRY_READY'&&data?.entry_policy_version==='SIGNAL_LOCATION_SEPARATION_V1'&&data?.verdict?.signal_active===true")
# The UI must not claim an active signal from a contradictory partial payload.
change('radar/static/pages.html',"      if(status==='ENTRY_READY')return {label:'最近更新時條件通過'", "      if(data?.entry_policy_version==='SIGNAL_LOCATION_SEPARATION_V1'&&status==='ENTRY_READY')return {label:'訊號尚待確認',detail:'本次更新尚未取得完整訊號確認。',lifecycle:'等待更新'};\n      if(status==='ENTRY_READY')return {label:'最近更新時條件通過'")

Path('scripts/check_entry_position_ui.js').write_text(r'''// Execute production renderers, not text-presence assertions.
'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('radar/static/pages.html','utf8');
const code=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m=>m[1]).join('\n');
new vm.Script(code); // Validate the whole inline application, including untouched paths.
function declaration(name){
 const re=new RegExp('(?:^|\\n)    function '+name+'\\('),m=re.exec(code);
 assert(m,`Missing ${name}`);
 const start=m.index+(code[m.index]==='\n'?1:0),tail=code.slice(start+5);
 const next=/\n    (?:async )?function /.exec(tail);
 return code.slice(start,next?start+5+next.index:code.length);
}
const context=vm.createContext({console,JSON,Number,Math,String,Boolean,Array,Object,Set,Date,Intl,
 state:{report:{runtime_status:'FRESH'},scanStarting:false,favorites:new Set()},
 itemSnapshotEntryState:i=>({label:i.lifecycle?.terminal?'交易已結束':'等待確認'}),
 terminalSignalOutcome:i=>i.lifecycle?.terminal?'CLOSED_UNKNOWN':null,
 isTerminalSignal:i=>i.lifecycle?.terminal===true,
 itemReadOnlyReason:i=>i.read_only_reason||null,
 isExpiredSnapshot:i=>i.expired===true,
 isPreviewItem:i=>i.preview===true,
 normalizedHorizon:h=>h==='LONG'?'LONG':'SHORT',
 activePreflightSignal:()=>null,
 signalDataUnavailable:i=>i.data_quality?.core==='UNAVAILABLE',
 preflightDisabledLabel:()=> '資料待更新',
});
for(const name of ['isRecord','itemDecisionContext','itemHardGate','metricNumber','num','esc','tickPrecision','pricePrecision','price','planTargetR','signalTradeGrid','preflightSignalUsable','preflightButton','singleScanButton','preflightActions','separatedSignalView','positionAdviceText','separatedSignalCard','preflightTerminalKind','preflightPresentation']){
 vm.runInContext(declaration(name),context);
}
const fixtures=JSON.parse(fs.readFileSync(0,'utf8'));
for(const row of fixtures.cards){
 const before=JSON.stringify(row.item),result=context.separatedSignalCard(row.item);
 assert.equal(JSON.stringify(row.item),before,'renderer mutated input');
 assert(result.includes('可進位置（參考）'));
 assert(!result.includes('<details'));
 assert(!result.includes('data-detail-key'));
 if(row.expectedActive){
  assert(result.includes(`${row.item.direction==='LONG'?'做多':'做空'}訊號已觸發`));
  assert(result.includes('data-preflight-id='));
  assert(!result.includes('disabled title'));
  assert(!result.includes('必要條件未成立'));
  assert(result.includes(row.item.decision_context.final.entry_position.label));
 }else{
  assert(!result.includes('⚡ 做多訊號已觸發')&&!result.includes('⚡ 做空訊號已觸發'));
 }
}
for(const row of fixtures.preflight){
 const result=context.preflightPresentation(row.payload);
 if(row.expectedActive){
  assert(result.label.includes('訊號已觸發'));
  assert(result.detail.includes(row.payload.entry_position.label));
 }else{
  assert(!result.label.includes('訊號已觸發'));
 }
}
console.log(`UI behavior PASS: ${fixtures.cards.length} cards, ${fixtures.preflight.length} preflight states; complete JS syntax valid`);
''')
p=Path('tests/test_entry_position_policy.py')
p.write_text(p.read_text()+r'''

    def test_real_js_renderers_for_active_wait_terminal_and_missing_data(self):
        import json, subprocess
        cards=[]
        for direction in ('LONG','SHORT'):
            for horizon in ('SHORT','LONG'):
                for status,current in (('ENTRY_READY',100),('WAIT_RETEST',99),('MISSED_ENTRY',101.5)):
                    item=self.case(direction,horizon)
                    item['entry_eligibility'].update(status=status,new_entry_allowed=status=='ENTRY_READY',chase_atr=2.5)
                    item['market_metrics']['entry_execution_price']=current
                    item['decision_context']=build_decision_context(item)
                    item['actionable']=item['decision_context']['final']['new_entry_allowed']
                    item['entry_eligibility'].update(actionable=item['actionable'],new_entry_allowed=item['actionable'])
                    self.assertTrue(item['actionable'])
                    cards.append({'item':public_candidate_payload(item,signal=True),'expectedActive':True})
        for failure in ('DATA','OPPOSITE','TERMINAL','READ_ONLY'):
            item=self.case();item['entry_eligibility'].update(status='MISSED_ENTRY',new_entry_allowed=False)
            if failure=='DATA':item['data_quality']['core']='UNAVAILABLE'
            elif failure=='OPPOSITE':item['market_story']['trigger']['new_entry_suspended']=True
            elif failure=='TERMINAL':item['lifecycle'].update(terminal=True,status='INVALIDATED')
            elif failure=='READ_ONLY':item['read_only_reason']='STALE'
            item['decision_context']=build_decision_context(item)
            cards.append({'item':item,'expectedActive':False})
        preflight=[]
        for current in (100,101.5,99,97,105):
            signal=make_signal();client=PreflightClient(current)
            result=build_preflight_payload(signal,client.get_ticker(signal.inst_id),client.get_execution_context(signal.inst_id),AppConfig(),report_generated_at='2026-09-20T00:00:00+00:00')
            preflight.append({'payload':result,'expectedActive':current not in (97,105)})
        result=subprocess.run(['node','scripts/check_entry_position_ui.js'],input=json.dumps({'cards':cards,'preflight':preflight}),text=True,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        self.assertIn('UI behavior PASS',result.stdout)
''')
