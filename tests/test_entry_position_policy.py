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
        out=public_candidate_payload(item, signal=True)
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
