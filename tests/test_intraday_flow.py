import copy
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from radar.api import OKXPublicClient
from radar.intraday_flow import INTERVAL_MS, summarize_intraday_flow
from radar.market_story import MarketStoryEngine
from radar.models import Candle
from radar.repository import SignalRepository
from radar.scanner import MarketScanner, ScannerConfig
from tests.test_market_story import valid_breakout_frames
from tests.test_repository import signal_fixture, state_fixture

SYMBOL = 'AAA-USDT-SWAP'
END = 1_800_000_000_000


def flow_fixture():
    candles, oi, flow = [], [], []
    for i in range(61):
        ts = END - (60-i) * INTERVAL_MS
        p = 110 - i * .1
        candles.append(Candle(ts, p+.1, p+.2, p-.2, p, 10, 1000, True))
        oi.append({'ts': ts, 'oi': 1000+i, 'oiCcy': 100+i/10, 'oiUsd': 100000-i*50})
        flow.append({'ts': ts, 'sell': 700, 'buy': 300, 'unit': 'USDT', 'inst_id': SYMBOL})
    return candles, oi, flow


class IntradayFlowTests(unittest.TestCase):
    def run_flow(self, candles=None, oi=None, flow=None):
        c, o, f = flow_fixture()
        return summarize_intraday_flow(SYMBOL, c if candles is None else candles,
            o if oi is None else oi, f if flow is None else flow, observed_at_ms=END+1000)

    def test_all_windows_aligned_and_quantity_not_usd(self):
        result = self.run_flow()
        for key, n in [('15m', 3), ('1H', 12), ('4H', 48)]:
            row = result['windows'][key]
            self.assertEqual(row['end_ms'], END)
            self.assertEqual(row['end_ms']-row['start_ms'], n*INTERVAL_MS)
            self.assertEqual(row['cvd']['delta'], -400*n)
            self.assertEqual(row['cvd']['imbalance_pct'], -40)
            self.assertGreater(row['oi']['change_pct'], 0)
            self.assertLess(row['price']['change_pct'], 0)
            self.assertIn('增倉', row['interpretation'])
        self.assertEqual(result['change_vs_previous_15m']['status'], 'OK')
        self.assertIn('CONTEXT_ONLY', result['permission'])

    def test_no_quantity_does_not_fall_back_to_usd(self):
        _, oi, _ = flow_fixture()
        for p in oi:
            p.pop('oi'); p.pop('oiCcy')
        r = self.run_flow(oi=oi)['windows']['15m']
        self.assertEqual(r['oi']['status'], 'MISSING')
        self.assertEqual(r['price']['status'], 'OK')
        self.assertEqual(r['cvd']['status'], 'OK')

    def test_base_unit_fallback_consistent_over_whole_window(self):
        _, oi, _ = flow_fixture()
        for p in oi:p.pop('oi')
        r = self.run_flow(oi=oi)['windows']['4H']['oi']
        self.assertEqual(r['status'], 'OK'); self.assertEqual(r['unit'], 'base')

    def test_oi_gap_is_not_interpolated(self):
        _, oi, _ = flow_fixture(); oi.pop(-2)
        self.assertEqual(self.run_flow(oi=oi)['windows']['15m']['oi']['status'], 'MISSING')

    def test_conflicting_duplicate_rejected(self):
        _, oi, _ = flow_fixture(); oi.append({**oi[-2], 'oi': 99})
        self.assertEqual(self.run_flow(oi=oi)['windows']['15m']['oi']['status'], 'MISSING')

    def test_taker_gap_never_filled_with_candle_color(self):
        _, _, f = flow_fixture(); f.pop(-3)
        self.assertEqual(self.run_flow(flow=f)['windows']['15m']['cvd']['status'], 'MISSING')

    def test_wrong_instrument_and_unit_rejected(self):
        _, _, f = flow_fixture()
        for key, value in [('inst_id', 'OTHER-USDT-SWAP'), ('unit', 'contracts')]:
            bad = [{**r, key: value} for r in f]
            self.assertEqual(self.run_flow(flow=bad)['windows']['15m']['cvd']['status'], 'MISSING')

    def test_mismatched_volume_is_missing_not_false_cvd(self):
        _, _, f = flow_fixture(); f[-2]['buy'] = 99999
        self.assertEqual(self.run_flow(flow=f)['windows']['15m']['cvd']['status'], 'MISSING')

    def test_unconfirmed_and_future_price_not_used(self):
        c, o, f = flow_fixture(); c[-1] = replace(c[-1], close=999999, confirmed=False)
        result=self.run_flow(c,o,f)
        self.assertLess(result['windows']['15m']['price']['end'], 110)

    def test_gap_in_prices_missing(self):
        c, o, f=flow_fixture();c.pop(-3)
        self.assertEqual(self.run_flow(c,o,f)['windows']['15m']['price']['status'], 'MISSING')

    def test_inputs_unchanged(self):
        c,o,f=flow_fixture();before=copy.deepcopy((c,o,f));self.run_flow(c,o,f)
        self.assertEqual((c,o,f),before)

    def test_latest_rebound_not_hidden_by_hourly_selloff(self):
        c,o,f=flow_fixture()
        c[-2]=replace(c[-2], close=c[-5].close+.2)
        r=self.run_flow(c,o,f)
        self.assertIn('反推',r['summary'])

    def test_taker_adapter_requests_contract_quote_unit(self):
        client=OKXPublicClient()
        with patch.object(client, '_get', return_value=[[str(END), '700', '300']]) as getter:
            out=client.get_contract_taker_history(SYMBOL, '5m', 60, request_retries=0, request_timeout_seconds=3)
        self.assertEqual(getter.call_args.args[0], '/api/v5/rubik/stat/taker-volume-contract')
        self.assertEqual(getter.call_args.args[1]['unit'], '2')
        self.assertEqual(out[0]['sell'],700);self.assertEqual(out[0]['buy'],300)
        self.assertEqual(out[0]['inst_id'],SYMBOL)

    def test_optional_flow_failure_does_not_raise_or_fake(self):
        class Client:
            def get_contract_taker_history(self,*args,**kwargs):raise TimeoutError('unavailable')
        scanner=MarketScanner(Client(),ScannerConfig())
        c,o,_=flow_fixture()
        with patch('radar.scanner.time.time',return_value=(END+1000)/1000):
            r=scanner._intraday_flow(SYMBOL,c,o)
        self.assertEqual(r['windows']['15m']['price']['status'],'OK')
        self.assertEqual(r['windows']['15m']['cvd']['status'],'MISSING')
        scanner.repository.close()


class IntradayEpisodeTests(unittest.TestCase):
    def test_market_scope_accepts_new_opposite_but_card_scope_keeps_original(self):
        engine=MarketStoryEngine();a,b,c=valid_breakout_frames()
        previous={'active_trigger_direction':'SHORT', 'active_stage':'CONFIRMED',
            'invalidation_price':max(x.high for x in c)+1,'last_evaluated_core_ts':c[-5].ts,
            'invalidated':False,'trigger':{'direction':'SHORT','triggered':True,'type':'BREAKOUT',
            'stage':'CONFIRMED','event_ts':c[-10].ts,'trigger_event_key':'old-short'}}
        locked=engine.analyze_short(a,b,c,previous_story=previous)
        self.assertFalse(locked.triggered);self.assertEqual(locked.trigger_direction,'SHORT')
        result=engine.analyze_short(a,b,c,previous_story={**previous,'allow_opposite_episode':True})
        self.assertTrue(result.triggered);self.assertEqual(result.trigger_direction,'LONG')
        self.assertEqual(result.trigger['opposite_episode_transition']['prior_event_key'],'old-short')
        self.assertFalse(result.trigger['previous_plan_invalidated'])

    def test_bullish_4h_can_report_closed_15m_short_during_pullback(self):
        engine=MarketStoryEngine();a,b,c=valid_breakout_frames()
        c=[replace(x,open=200-x.open,high=200-x.low,low=200-x.high,close=200-x.close) for x in c]
        previous={'allow_opposite_episode':True,'active_trigger_direction':'LONG','active_stage':'CONFIRMED',
            'invalidation_price':min(x.low for x in c)-1,'last_evaluated_core_ts':c[-5].ts,
            'trigger':{'event_ts':c[-10].ts,'trigger_event_key':'oldlong','direction':'LONG',
                       'triggered':True,'stage':'CONFIRMED'}}
        result=engine.analyze_short(a,b,c,previous_story=previous)
        self.assertTrue(result.triggered);self.assertEqual(result.trigger_direction,'SHORT')
        self.assertEqual(result.timeframe_states['4H']['direction'],'LONG')
        self.assertIn('短線回落',result.timeframe_states['4H']['label'])
        self.assertFalse(result.timeframe_states['4H']['can_block_trigger'])
        c[-1]=replace(c[-1],confirmed=False)
        result=engine.analyze_short(a,b,c,previous_story=previous)
        self.assertNotIn('opposite_episode_transition',result.trigger)

    def test_bearish_4h_does_not_block_15m_long_rebound(self):
        engine=MarketStoryEngine();a,b,c=valid_breakout_frames()
        a=[replace(x,open=200-x.open,high=200-x.low,low=200-x.high,close=200-x.close) for x in a]
        result=engine.analyze_short(a,b,c)
        self.assertTrue(result.triggered);self.assertEqual(result.trigger_direction,'LONG')
        self.assertEqual(result.timeframe_states['4H']['direction'],'SHORT')
        self.assertIn('短線反彈',result.timeframe_states['4H']['label'])

    def _reversal(self, repository, authorized=True):
        raw=signal_fixture(event_ts=END-900000)
        original=repository.reconcile([raw],[state_fixture(raw,raw.data_timestamp)],'2026-09-09T12:00:00+00:00','SHORT')[0]
        candidate=replace(signal_fixture(event_ts=END,core_timestamp=END),direction='SHORT',
            stop_loss='110',take_profit_1='80',take_profit_2='70',market_story={'trigger':{
                'event_ts':END, 'triggered':True,'trigger_event_key':'new-short','confirmation_ts':END}})
        if authorized:
            candidate.market_story['trigger']['opposite_episode_transition']={
                'authorized_scope':'MARKET_SCAN','prior_event_key':original.lifecycle['event_key'],
                'prior_direction':'LONG','confirmation_ts':END}
        return original,candidate

    def test_new_opposite_archives_original_without_false_stop(self):
        repo=SignalRepository(':memory:');old,raw=self._reversal(repo)
        new=repo.reconcile([raw],[state_fixture(raw,END)],'2026-09-09T12:15:00+00:00','SHORT')[0]
        self.assertEqual(new.direction,'SHORT');self.assertNotEqual(new.trigger_id,old.trigger_id)
        row=repo._connection.execute('SELECT * FROM signals WHERE signal_id=?',(old.trigger_id,)).fetchone()
        self.assertEqual(row['outcome'],'SUPERSEDED');self.assertIsNone(row['final_r']);self.assertIsNone(row['tp_sl_order'])
        archive=repo._terminal_projection(row)
        self.assertEqual(archive.stop_loss,old.stop_loss);self.assertIn('不是已觸發止損',archive.entry_eligibility['reason'])
        again=repo.reconcile([raw],[state_fixture(raw,END)],'2026-09-09T12:16:00+00:00','SHORT')[0]
        self.assertEqual(again.trigger_id,new.trigger_id)
        active=repo._connection.execute("SELECT COUNT(*) FROM signals WHERE status='ACTIVE'").fetchone()[0]
        self.assertEqual(active,1);repo.close()

    def test_default_reconcile_does_not_flip_card(self):
        repo=SignalRepository(':memory:');old,raw=self._reversal(repo,False)
        result=repo.reconcile_instrument(raw,state_fixture(raw,END),'2026-09-09T12:15:00+00:00','SHORT')
        self.assertEqual(result.trigger_id,old.trigger_id);self.assertEqual(result.direction,'LONG');repo.close()

    def test_single_card_observation_cannot_consume_market_reversal(self):
        repo=SignalRepository(':memory:');old,raw=self._reversal(repo)
        # A card observation sees the same closed bar first, but is not allowed
        # to publish the opposite direction. A subsequent market scan still can.
        locked=replace(raw,market_story={'trigger':{k:v for k,v in raw.market_story['trigger'].items()
                                                   if k!='opposite_episode_transition'}})
        first=repo.reconcile_instrument(locked,state_fixture(locked,END),'2026-09-09T12:15:00+00:00','SHORT')
        self.assertEqual(first.trigger_id,old.trigger_id)
        result=repo.reconcile([raw],[state_fixture(raw,END)],'2026-09-09T12:16:00+00:00','SHORT')[0]
        self.assertEqual(result.direction,'SHORT');self.assertNotEqual(result.trigger_id,old.trigger_id)
        repo.close()

    def test_market_reversal_already_observed_on_card_keeps_event_time(self):
        engine=MarketStoryEngine();a,b,c=valid_breakout_frames()
        previous={'active_trigger_direction':'SHORT','active_stage':'CONFIRMED',
            'invalidation_price':max(x.high for x in c)+1,'last_evaluated_core_ts':c[-1].ts,
            'trigger':{'direction':'SHORT','triggered':True,'type':'BREAKOUT',
            'stage':'CONFIRMED','event_ts':c[-10].ts,'trigger_event_key':'old-short'}}
        locked=engine.analyze_short(a,b,c,previous_story=previous)
        self.assertFalse(locked.triggered)
        result=engine.analyze_short(a,b,c,previous_story={**previous,'allow_opposite_episode':True})
        self.assertTrue(result.triggered);self.assertEqual(result.trigger_direction,'LONG')
        self.assertLessEqual(result.trigger['confirmation_ts'],c[-1].ts)

    def test_supersession_is_transactional(self):
        repo=SignalRepository(':memory:');old,raw=self._reversal(repo)
        with patch.object(repo,'_insert_signal',side_effect=RuntimeError('test insert failure')):
            with self.assertRaises(RuntimeError):repo._reconcile_raw_signal(raw,'2026-09-09T12:15:00+00:00')
        self.assertEqual(repo._active_episode_row(SYMBOL,'SHORT')['signal_id'],old.trigger_id)
        repo.close()
