"""Synthetic regressions for intraday context and same-opportunity refreshes."""
import copy
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from radar.config import AppConfig
from radar.decision import build_decision_context
from radar.entry_window import can_continue, snapshot, plan_key, CORE_MS, MAX_AGE_MS
from radar.models import Signal, Ticker, MarketContext
from radar.preflight import build_preflight_payload
from radar.repository import SignalRepository
from radar.scanner import MarketScanner, ScannerConfig
from radar.strategy import _entry_eligibility
from radar.service import _with_durable_entry_window, _record_preflight_entry_window
from tests.test_decision import complete_signal
from tests.test_repository import state_fixture
from tests.test_market_story import valid_breakout_frames
from radar.market_story import MarketStoryEngine

TS = 1_800_000_000_000
NOW = TS + CORE_MS + 120_000


def ready_signal(direction='SHORT'):
    x = complete_signal()
    x.update(radar_horizon='SHORT', strategy='test', score=80, evidence=[],
             direction=direction, closed_candle_ts=TS, data_timestamp=TS,
             entry_low='0.3338', entry_high='0.3343',
             stop_loss='0.3393' if direction=='SHORT' else '0.3288',
             take_profit_1='0.3199' if direction=='SHORT' else '0.3480',
             take_profit_2='0.3128' if direction=='SHORT' else '0.3550',
             generated_at=datetime.fromtimestamp(NOW/1000,timezone.utc).isoformat())
    x['market_metrics'].update(last_price=.3341, entry_execution_price=.3341,
                              entry_execution_price_source='BID' if direction=='SHORT' else 'ASK',
                              ticker_sampled_at=NOW, core_timestamp=TS)
    x['market_story']['trigger'].update(event_atr=.002, event_ts=TS,
        triggered=True, direction=direction, trigger_event_key='test-event')
    x['market_story']['raw']['core_atr']=.002
    x['entry_eligibility'].update(ready_max_chase_atr=.18,new_entry_allowed=True,
                                 existing_episode=True,entry_ready_once=True)
    x['lifecycle'].update(status='ACTIVE',first_seen_at=x['generated_at'],
                          last_evaluated_core_ts=TS, entry_ready_once=True)
    x['decision_context']=build_decision_context(x)
    x['actionable']=True
    signal=Signal.from_dict(x)
    signal.lifecycle['entry_window']=snapshot(signal,NOW,'OPEN')
    return signal


class ShortContextPolicyTests(unittest.TestCase):
    def test_strong_background_is_advisory_both_directions(self):
        for direction in ('LONG','SHORT'):
            item=complete_signal();item['direction']=direction;item['radar_horizon']='SHORT'
            item['conflicts']=['1H 背景反向，屬逆勢 Trigger','更高週期背景明顯反向；只列 Conflict，不取消核心 Trigger']
            if direction=='SHORT':
                item.update(stop_loss='102', take_profit_1='96', take_profit_2='94')
            result=build_decision_context(item)
            self.assertTrue(result['final']['new_entry_allowed'])
            self.assertFalse(result['conflict']['blocks_entry'])
            self.assertTrue(result['conflict']['countertrend'])

    def test_single_core_warning_plus_background_not_extra_veto(self):
        item=complete_signal();item['conflicts']=['4H 背景明顯反向']
        item['evidence_groups']['trend_momentum'].update(stance='CONFLICT',conflicts=['15m MACD轉弱'])
        self.assertTrue(build_decision_context(item)['final']['new_entry_allowed'])

    def test_two_actual_core_domains_still_block(self):
        item=complete_signal()
        for k in ('position_structure','trend_momentum'):
            item['evidence_groups'][k].update(stance='CONFLICT',source_timeframe='15m',evidence_scope='CORE')
        self.assertFalse(build_decision_context(item)['final']['new_entry_allowed'])

    def test_htf_provenance_not_disguised_as_core(self):
        item=complete_signal()
        for k in ('position_structure','trend_momentum'):
            item['evidence_groups'][k].update(stance='CONFLICT',source_timeframe='4H',evidence_scope='CONTEXT')
        result=build_decision_context(item)
        self.assertFalse(result['conflict']['blocks_entry'])
        self.assertTrue(result['final']['new_entry_allowed'])

    def test_legacy_htf_only_group_prose_not_two_core_votes(self):
        item=complete_signal()
        for k in ('position_structure','trend_momentum'):
            item['evidence_groups'][k].update(stance='CONFLICT',conflicts=['4H 背景明顯反向'])
        self.assertFalse(build_decision_context(item)['conflict']['blocks_entry'])

    def test_swing_policy_unchanged(self):
        item=complete_signal();item['radar_horizon']='LONG';item['conflicts']=['更高週期背景明顯反向']
        self.assertFalse(build_decision_context(item)['final']['new_entry_allowed'])

    def test_risk_gates_still_block_against_background(self):
        for field,value in [('spread_pct',.3),('risk_reward',.5)]:
            item=complete_signal();item['conflicts']=['4H 背景明顯反向'];item[field]=value
            if field=='risk_reward':item['entry_eligibility']['remaining_rr']=.5
            self.assertFalse(build_decision_context(item)['final']['new_entry_allowed'])
        item=complete_signal();item['data_quality']['core']='MISSING'
        self.assertFalse(build_decision_context(item)['final']['new_entry_allowed'])
        item=complete_signal();item['market_story']['trigger']['new_entry_suspended']=True
        self.assertFalse(build_decision_context(item)['final']['new_entry_allowed'])

    def test_core_groups_independent_of_changed_higher_frames(self):
        a,b,c=valid_breakout_frames();engine=MarketStoryEngine()
        first=engine.analyze_short(a,b,c)
        def flip(rows):
            return [replace(x,open=300-x.open, high=300-x.low, low=300-x.high, close=300-x.close) for x in rows]
        other=engine.analyze_short(flip(a),flip(b),c)
        for key in ('position_structure','trend_momentum'):
            self.assertEqual(first.groups[key]['score'],other.groups[key]['score'])
            self.assertEqual(other.groups[key]['source_timeframe'],'15m')
        self.assertEqual(first.trigger_direction,other.trigger_direction)
        self.assertEqual(first.triggered,other.triggered)


class EntryWindowTests(unittest.TestCase):
    def test_same_core_and_in_zone_no_new_retest_both_directions(self):
        for direction in ('SHORT','LONG'):
            s=ready_signal(direction);before=copy.deepcopy(s.to_dict())
            self.assertTrue(can_continue(s,NOW+180000))
            out=_entry_eligibility(direction=direction,current_price=.3339,
                entry_low=.3338,entry_high=.3343,stop=float(s.stop_loss),target=float(s.take_profit_1),atr=.002,
                stage='CONFIRMED',minimum_rr=1.8,ready_max_chase_atr=.18,missed_chase_atr=.5,
                existing_episode=True,entry_ready_once=True,
                continuing_entry_window=can_continue(s,NOW+180000))
            self.assertEqual(out['status'],'ENTRY_READY');self.assertFalse(out['reentry_confirmation_required'])
            self.assertEqual(before,s.to_dict())

    def test_real_departure_still_waits_despite_open_flag(self):
        out=_entry_eligibility(direction='SHORT',current_price=.336,entry_low=.3338,entry_high=.3343,
            stop=.3393,target=.3199,atr=.002,stage='CONFIRMED',minimum_rr=1.8,
            ready_max_chase_atr=.18,missed_chase_atr=.5,existing_episode=True,continuing_entry_window=True)
        self.assertEqual(out['status'],'WAIT_RETEST')

    def test_closed_or_stale_window_never_reopens_from_quote(self):
        for change in ('SUSPENDED','STALE','FUTURE','PLAN','TERMINAL','LONG'):
            s=ready_signal();now=NOW+1000
            if change=='SUSPENDED':s.lifecycle['entry_window']['state']='SUSPENDED'
            elif change=='STALE':now=NOW+MAX_AGE_MS+1
            elif change=='FUTURE':now=NOW-1
            elif change=='PLAN':s.stop_loss='0.34'
            elif change=='TERMINAL':s.lifecycle['terminal']=True
            elif change=='LONG':s.radar_horizon='LONG'
            self.assertFalse(can_continue(s,now),change)

    def test_new_core_requires_complete_in_envelope_closed_path(self):
        s=ready_signal();s.market_metrics['core_timestamp']=TS+CORE_MS;s.data_timestamp=TS+CORE_MS
        now=TS+2*CORE_MS+1000
        self.assertFalse(can_continue(s,now))
        s.market_metrics['_core_path']=[[TS+CORE_MS,.33425,.3339,.3340]]
        self.assertTrue(can_continue(s,now))
        s.market_metrics['_core_path']=[[TS+CORE_MS,.335,.3339,.3340]]
        self.assertFalse(can_continue(s,now))
        s.market_metrics['_core_path']=[[TS+CORE_MS,.33425,.3339,.3340],[TS+CORE_MS,.33425,.330,.3340]]
        self.assertFalse(can_continue(s,now))

    def test_legacy_ready_can_migrate_but_ready_once_alone_cannot(self):
        s=ready_signal();s.lifecycle.pop('entry_window')
        self.assertTrue(can_continue(s,NOW+1000))
        s.entry_eligibility.update(status='WAIT_RETEST',actionable=False)
        self.assertFalse(can_continue(s,NOW+1000))

    def test_scanner_in_zone_refresh_remains_ready(self):
        scanner=MarketScanner(object(),ScannerConfig())
        try:
            s=ready_signal();s.market_metrics.update(entry_execution_price=.3339,ticker_sampled_at=NOW+180000)
            out=scanner._attach_decision_context(scanner._refresh_entry_eligibility(s))
            self.assertTrue(out.actionable)
            self.assertEqual(out.entry_low,s.entry_low);self.assertEqual(out.stop_loss,s.stop_loss)
        finally:scanner.repository.close()

    def test_wait_reason_is_not_misreported_as_price_outside_or_risk(self):
        item=complete_signal();item['entry_eligibility'].update(status='WAIT_RETEST',new_entry_allowed=False,
            actionable=False,reentry_confirmation_required=True,closed_retest_confirmed=False,
            label='已回到進場區｜等待收線重新確認',reason='價格在區間內；前一進場窗口已關閉。')
        out=build_decision_context(item)
        self.assertEqual(out['final']['status'],'WAIT')
        self.assertIn('區間內',out['final']['wait_reason']['label'])
        self.assertFalse(out['hard_gate']['blocked'])

    def test_preflight_continues_same_window_without_changing_plan(self):
        s=ready_signal();config=AppConfig();t=Ticker(s.inst_id,.3339,.3339,.3340,NOW+1000,30_000_000)
        # Keep test independent of the optional book estimates.
        c=MarketContext(s.inst_id,None,None,None,None,NOW+1000)
        out=build_preflight_payload(s,t,c,config,report_generated_at=s.generated_at,now_ms=NOW+1000)
        self.assertEqual(out['verdict']['status'],'ENTRY_READY',out['verdict'])
        self.assertEqual(s.stop_loss,'0.3393')


class DurableWindowTests(unittest.TestCase):
    def setUp(self):
        self.repo=SignalRepository(':memory:')
        raw=ready_signal();raw.trigger_id='';raw.lifecycle={};raw.entry_eligibility['existing_episode']=False
        self.s=self.repo.reconcile([raw],[],datetime.fromtimestamp(NOW/1000,timezone.utc).isoformat(),'SHORT')[0]
        self.s=self.repo.record_entry_window(self.s,NOW)
        self.assertTrue(self.s.actionable)

    def tearDown(self):self.repo.close()

    def test_suspend_survives_reload_and_in_zone_price(self):
        closed=replace(self.s,actionable=False,entry_eligibility={**self.s.entry_eligibility,'status':'WAIT_RETEST','actionable':False},
            decision_context={'final':{'status':'WAIT','new_entry_allowed':False}})
        closed=self.repo.record_entry_window(closed,NOW+1000)
        loaded=self.repo.load_active_signal(self.s.inst_id,'SHORT')
        self.assertFalse(can_continue(loaded,NOW+2000))
        self.assertEqual(loaded.entry_low,self.s.entry_low)
        self.assertEqual(loaded.trigger_id,self.s.trigger_id)

    def test_concurrent_old_ready_response_cannot_overwrite_suspension(self):
        closed=replace(self.s,actionable=False)
        self.repo.record_entry_window(closed,NOW+1000)
        rejected=self.repo.record_entry_window(self.s,NOW+2000)
        self.assertFalse(rejected.actionable)
        self.assertEqual(self.repo.load_active_signal(self.s.inst_id,'SHORT').lifecycle['entry_window']['state'],'SUSPENDED')

    def test_stale_quote_cannot_rollback_latest_window(self):
        latest=self.repo.record_entry_window(self.s,NOW+1000)
        rejected=self.repo.record_entry_window(latest,NOW-1)
        self.assertFalse(rejected.actionable)
        self.assertEqual(self.repo.load_active_signal(self.s.inst_id,'SHORT').lifecycle['entry_window']['observed_ms'],NOW+1000)

    def test_service_loads_durable_suspension_not_old_report_ready(self):
        self.repo.record_entry_window(replace(self.s,actionable=False),NOW+1000)
        loaded=_with_durable_entry_window(self.repo,self.s)
        self.assertFalse(can_continue(loaded,NOW+2000))

    def test_same_window_repeated_record_not_new_episode(self):
        latest=self.repo.record_entry_window(self.s,NOW+1000)
        self.assertTrue(can_continue(latest,NOW+2000))
        self.assertEqual(latest.trigger_id,self.s.trigger_id)
        self.assertEqual(latest.lifecycle['triggered_at'],self.s.lifecycle['triggered_at'])


class WindowIntegrationTests(unittest.TestCase):
    setUp = DurableWindowTests.setUp
    tearDown = DurableWindowTests.tearDown

    def test_repository_scanner_and_preflight_same_core_agree(self):
        scanner=MarketScanner(object(),ScannerConfig())
        scanner.repository.close();scanner.repository=self.repo
        old=copy.deepcopy(self.s)
        raw=replace(self.s,trigger_id='',lifecycle={},decision_context={},actionable=False,
            entry_eligibility={**self.s.entry_eligibility,'status':'WAIT_RETEST','actionable':False,
                'existing_episode':True,'new_entry_allowed':False,'closed_retest_confirmed':False})
        raw.market_metrics={**raw.market_metrics,'ticker_sampled_at':NOW+180000,
            'last_price':.3339,'entry_execution_price':.3339}
        updated=self.repo.reconcile_instrument(raw,replace(state_fixture(raw,TS,.3342,.3338),market_metrics=dict(raw.market_metrics)),datetime.fromtimestamp((NOW+180000)/1000,timezone.utc).isoformat(),'SHORT')
        projected=scanner._episode_with_latest_signal_context(updated,raw)
        final=scanner._record_entry_window(scanner._attach_decision_context(scanner._refresh_entry_eligibility(projected)))
        self.assertTrue(final.actionable,final.decision_context['final'])
        ticker=Ticker(final.inst_id,.3339,.3339,.3340,NOW+180000,30_000_000)
        payload=build_preflight_payload(final,ticker,MarketContext(final.inst_id,None,None,None,None,NOW+180000),
            AppConfig(),report_generated_at=final.generated_at,now_ms=NOW+180000)
        self.assertEqual(payload['verdict']['status'],'ENTRY_READY')
        for key in ('trigger_id','entry_low','entry_high','stop_loss','take_profit_1','take_profit_2'):
            self.assertEqual(getattr(final,key),getattr(old,key))

    def test_preflight_departure_is_durable_then_return_stays_wait(self):
        payload={'verdict':{'status':'WAIT_RETEST','actionable':False},
                 'plan_state':{'new_entry_allowed':False},'signal_lifecycle':{'terminal':False}}
        _record_preflight_entry_window(self.repo,self.s,payload,NOW+1000)
        old_report=_with_durable_entry_window(self.repo,self.s)
        ticker=Ticker(self.s.inst_id,.3339,.3339,.3340,NOW+2000,30_000_000)
        result=build_preflight_payload(old_report,ticker,MarketContext(self.s.inst_id,None,None,None,None,NOW+2000),
            AppConfig(),report_generated_at=self.s.generated_at,now_ms=NOW+2000)
        self.assertEqual(result['verdict']['status'],'WAIT_RETEST')

    def test_real_new_closed_retest_can_reopen_suspended_window(self):
        closed=self.repo.record_entry_window(replace(self.s,actionable=False),NOW+1000)
        # Reconcile an actually newer closed price event before re-opening.
        raw=copy.deepcopy(self.s)
        raw.market_metrics.update(core_timestamp=TS+CORE_MS,core_high=.3342,core_low=.3338,core_close=.334,
            _core_path=[[TS+CORE_MS,.3342,.3338,.334]],ticker_sampled_at=TS+2*CORE_MS+1000)
        raw.closed_candle_ts=raw.data_timestamp=TS+CORE_MS
        raw.market_story['trigger']['confirmation_ts']=TS+CORE_MS
        raw.entry_eligibility.update(closed_retest_confirmed=True,status='ENTRY_READY',actionable=True,new_entry_allowed=True)
        raw.signal_stage='REENTRY'
        later=self.repo.reconcile_instrument(raw,replace(state_fixture(raw,TS+CORE_MS,.3342,.3338),market_metrics=dict(raw.market_metrics)),datetime.fromtimestamp((TS+2*CORE_MS+1000)/1000,timezone.utc).isoformat(),'SHORT')
        later.entry_eligibility=raw.entry_eligibility
        later.market_story['trigger']['confirmation_ts']=TS+CORE_MS
        later.actionable=True
        later.decision_context={'final':{'status':'ENTER','new_entry_allowed':True}}
        reopened=self.repo.record_entry_window(later,TS+2*CORE_MS+1000)
        self.assertTrue(reopened.actionable,reopened.entry_eligibility)

    def test_same_old_retest_does_not_reopen_suspended_window(self):
        closed=self.repo.record_entry_window(replace(self.s,actionable=False),NOW+1000)
        closed.entry_eligibility={**closed.entry_eligibility,'closed_retest_confirmed':True}
        closed.market_story=copy.deepcopy(closed.market_story)
        closed.market_story['trigger']['confirmation_ts']=TS
        closed.actionable=True
        self.assertFalse(self.repo.record_entry_window(closed,NOW+2000).actionable)


if __name__=='__main__':unittest.main()
