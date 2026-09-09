import copy
import json
import unittest
from dataclasses import replace

from radar.exit_review import review_exit_plan
from radar.intraday_flow import summarize_intraday_flow
from tests.test_intraday_flow import flow_fixture, END


def review_fixture(short=False):
    sign = -1 if short else 1
    plan = {"inst_id": "AAA-USDT-SWAP", "direction": "SHORT" if short else "LONG",
            "radar_horizon": "SHORT", "entry_low": "99.8", "entry_high": "100.2",
            "plan_entry_price": 100, "stop_loss": 100-sign*2,
            "take_profit_1": 100+sign*10, "take_profit_2": 100+sign*20,
            "instrument_tick_size": .05, "management_plan": {"structural_target_price": 100+sign*8}}
    flow = {"inst_id": plan["inst_id"], "schema_version": "INTRADAY_FLOW_V1", "source": "OKX_CONTRACT_HISTORY", "as_of_ms": END,
            "windows": {"15m": {"start_ms": END-900000,"end_ms": END,
                "price": {"status": "OK", "change_pct": sign*2},
                "oi": {"status": "OK", "unit": "contracts", "change_pct": 3},
                "cvd": {"status": "OK", "volume_alignment": "VERIFIED", "imbalance_pct": sign*40}}},
            "closed_structure": {"as_of_ms": END, "low_15m": 95 if short else 103.5,
                                 "high_15m": 96.5 if short else 105, "atr_5m": 1}}
    return plan, flow, 100+sign*4


class ExitReviewTests(unittest.TestCase):
    def review(self, p=None, f=None, current=None, **kw):
        a,b,c=review_fixture()
        return review_exit_plan(a if p is None else p,b if f is None else f,
                                current_price=c if current is None else current, now_ms=END+1000, **kw)

    def test_inputs_and_frozen_plan_unchanged(self):
        p,f,c=review_fixture();before=copy.deepcopy((p,f));result=self.review(p,f,c)
        self.assertEqual((p,f),before);self.assertTrue(result['plan_unchanged']);self.assertFalse(result['auto_ordering'])
        json.dumps(result,allow_nan=False)

    def test_protective_stops_only_tighten_both_directions(self):
        for short in (False,True):
            p,f,c=review_fixture(short);r=self.review(p,f,c);stop=r['protective_stop_reference'];self.assertIsNotNone(stop)
            if short:self.assertLess(stop,p['stop_loss']);self.assertGreater(stop,c);self.assertLess(stop,100)
            else:self.assertGreater(stop,p['stop_loss']);self.assertLess(stop,c);self.assertGreater(stop,100)
            self.assertAlmostEqual(stop/.05,round(stop/.05))

    def test_no_stop_reference_below_one_r(self):
        self.assertIsNone(self.review(current=101)['protective_stop_reference'])

    def test_no_stop_reference_without_tick_size(self):
        p,f,c=review_fixture();p.pop('instrument_tick_size');self.assertIsNone(self.review(p,f,c)['protective_stop_reference'])

    def test_flow_cannot_expand_profit_targets(self):
        p,f,c=review_fixture();r=self.review(p,f,c)
        self.assertEqual(r['status'],'FOLLOW');self.assertEqual(r['original']['tp1'],110);self.assertEqual(r['original']['tp2'],120)
        self.assertIsNone(r['partial_take_profit_reference'])

    def test_adverse_price_can_suggest_real_nearer_structure(self):
        p,f,c=review_fixture();f['windows']['15m']['price']['change_pct']=-1
        r=self.review(p,f,c);self.assertEqual(r['status'],'DEFENSIVE');self.assertEqual(r['partial_take_profit_reference'],108)

    def test_obstacle_behind_current_is_never_new_target(self):
        p,f,c=review_fixture();p['management_plan']['structural_target_price']=103;f['windows']['15m']['price']['change_pct']=-1
        self.assertIsNone(self.review(p,f,c)['partial_take_profit_reference'])

    def test_missing_or_usd_oi_not_treated_as_quantity(self):
        p,f,c=review_fixture();f['windows']['15m']['oi']={'status':'OK','unit':'USD','change_pct':100}
        r=self.review(p,f,c);self.assertEqual(r['status'],'OBSERVE');self.assertNotIn('OI 原始數量',r['sources'])

    def test_decreasing_oi_does_not_invalidate_following_price(self):
        p,f,c=review_fixture();f['windows']['15m']['oi']['change_pct']=-3
        self.assertEqual(self.review(p,f,c)['status'],'FOLLOW')

    def test_stale_future_wrong_contract_rejected(self):
        for patch in ({'as_of_ms':END-900000},{'as_of_ms':END+900000},{'inst_id':'BTC-USDT-SWAP'}):
            p,f,c=review_fixture();f.update(patch);r=self.review(p,f,c)
            self.assertEqual(r['status'],'INSUFFICIENT');self.assertIsNone(r['protective_stop_reference'])

    def test_mismatched_window_does_not_produce_references(self):
        p,f,c=review_fixture();f['windows']['15m']['end_ms']-=300000
        r=self.review(p,f,c);self.assertEqual(r['status'],'INSUFFICIENT')

    def test_terminal_and_superseded_never_get_live_suggestions(self):
        self.assertEqual(self.review(terminal=True)['status'],'CLOSED')
        p,f,c=review_fixture();p['lifecycle']={'outcome':'SUPERSEDED'}
        self.assertEqual(self.review(p,f,c)['status'],'CLOSED')

    def test_current_crossing_original_stop_or_target(self):
        self.assertEqual(self.review(current=98)['status'],'STOP_REACHED')
        self.assertEqual(self.review(current=110)['status'],'TARGET_REACHED')

    def test_no_plan_does_not_generate_fake_prices(self):
        self.assertEqual(self.review(p={})['status'],'NO_PLAN')
        p,f,c=review_fixture();p['take_profit_1']='NaN';r=self.review(p,f,c)
        self.assertEqual(r['status'],'NO_PLAN');json.dumps(r,allow_nan=False)

    def test_swing_radar_never_uses_15m_to_move_stop(self):
        p,f,c=review_fixture();p['radar_horizon']='LONG';f['windows']['4H']=copy.deepcopy(f['windows']['15m']);f['windows']['4H']['start_ms']=END-14400000
        r=self.review(p,f,c);self.assertEqual(r['horizon'],'4H');self.assertIsNone(r['protective_stop_reference'])

    def test_unverified_cvd_does_not_become_support(self):
        p,f,c=review_fixture();f['windows']['15m']['cvd']['volume_alignment']='UNKNOWN'
        r=self.review(p,f,c);self.assertEqual(r['status'],'OBSERVE')

    def test_closed_structure_has_no_future_or_unclosed_bars(self):
        c,o,t=flow_fixture();normal=summarize_intraday_flow('AAA-USDT-SWAP',c,o,t,observed_at_ms=END+1000)
        c[-1]=replace(c[-1],high=9999,low=.001,confirmed=False)
        future=summarize_intraday_flow('AAA-USDT-SWAP',c,o,t,observed_at_ms=END+1000)
        self.assertEqual(normal['closed_structure'],future['closed_structure'])
        self.assertLess(future['closed_structure']['high_15m'],110)

    def test_terminal_aliases_and_flag_never_get_management_references(self):
        for flags in ({"terminal":True},{"status":"INVALIDATED"},{"outcome":"SL_FIRST"},{"terminal_status":"CLOSED_UNKNOWN"}):
            p,f,c=review_fixture();p["lifecycle"]=flags;r=self.review(p,f,c)
            self.assertEqual(r["status"],"CLOSED");self.assertIsNone(r["protective_stop_reference"])

    def test_unrecognized_flow_schema_or_source_is_not_a_valid_reference(self):
        for flags in ({"schema_version":"OLD"},{"source":"OTHER_EXCHANGE"}):
            p,f,c=review_fixture();f.update(flags);r=self.review(p,f,c)
            self.assertEqual(r["status"],"INSUFFICIENT");self.assertIsNone(r["protective_stop_reference"])
