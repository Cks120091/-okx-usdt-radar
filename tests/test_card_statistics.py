import copy
import json
import sqlite3
import unittest

from radar.card_statistics import (
    DAY, MIN_RESOLVED, VERSION, advance, enroll, initialize, public_summary,
    setup, summarize, timestamp, wilson,
)

BAR = 900_000
BASE = 1_780_012_800_000 // BAR * BAR


def signal(identity='s1', now=BASE, direction='LONG', horizon='SHORT'):
    return {'trigger_id': identity, 'inst_id': 'AAA-USDT-SWAP', 'radar_horizon': horizon,
            'direction': direction, 'trigger_type': 'CONTINUATION',
            'signal_stage': 'EARLY_SIGNAL', 'actionable': True,
            'entry_low': 99, 'entry_high': 101, 'stop_loss': 95 if direction == 'LONG' else 105,
            'take_profit_1': 110 if direction == 'LONG' else 90,
            'take_profit_2': 115 if direction == 'LONG' else 85,
            'timeframe_states': {'4H': {'direction': direction}, '1D': {'direction': direction}},
            'market_metrics': {'entry_execution_price': 100, 'ticker_sampled_at': now,
                               'entry_execution_price_source': 'ASK' if direction == 'LONG' else 'BID'},
            'decision_context': {'final': {'status': 'ENTER', 'new_entry_allowed': True},
                                 'hard_gate': {'blocked': False, 'unknown': False}},
            'entry_eligibility': {'status': 'ENTRY_READY', 'new_entry_allowed': True},
            'lifecycle': {}}


def state(path, horizon='SHORT', inst_id='AAA-USDT-SWAP'):
    return {'inst_id': inst_id, 'radar_horizon': horizon, 'market_metrics': {'_core_path': path}}


class CardStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        initialize(self.db)

    def tearDown(self):
        self.db.close()

    def row(self, identity='s1'):
        return dict(self.db.execute('SELECT * FROM card_statistics_v1 WHERE signal_id=?', (identity,)).fetchone())

    def test_additive_schema_and_repeat_init(self):
        self.db.execute('CREATE TABLE signals(secret TEXT)')
        self.db.execute("INSERT INTO signals VALUES ('preserve')")
        initialize(self.db)
        self.assertEqual(self.db.execute('SELECT secret FROM signals').fetchone()[0], 'preserve')

    def test_first_ready_freezes_quote_context_and_plan(self):
        s = signal(); original = copy.deepcopy(s)
        self.assertTrue(enroll(self.db, s, 'v', BASE))
        s['market_metrics']['entry_execution_price'] = 101
        s['timeframe_states']['4H']['direction'] = 'SHORT'
        self.assertFalse(enroll(self.db, s, 'v', BASE+100))
        row = self.row()
        self.assertEqual(row['entry'], 100)
        self.assertIn('同向背景', row['label'])
        self.assertEqual(row['observed_ms'], BASE)
        self.assertEqual(original['stop_loss'], row['stop'])

    def test_observing_does_not_mutate_signal(self):
        s = signal(); before = copy.deepcopy(s)
        enroll(self.db, s, 'v', BASE)
        summarize(self.db, s, 'v', BASE)
        self.assertEqual(s, before)

    def test_wait_blocked_preview_terminal_never_enrolled(self):
        for variant in ('wait', 'blocked', 'unknown', 'preview', 'terminal', 'denied', 'actionable'):
            s = signal(variant)
            if variant == 'wait': s['entry_eligibility']['status'] = 'WAIT_RETEST'
            if variant in ('blocked', 'unknown'): s['decision_context']['hard_gate'][variant] = True
            if variant == 'preview': s['signal_stage'] = 'NEAR_TRIGGER'
            if variant == 'terminal': s['lifecycle']['terminal'] = True
            if variant == 'denied': s['decision_context']['final']['new_entry_allowed'] = False
            if variant == 'actionable': s['actionable'] = False
            self.assertFalse(enroll(self.db, s, 'v', BASE), variant)

    def test_bad_missing_or_stale_executable_quote_not_enrolled(self):
        for price in (None, float('nan'), float('inf'), True, -1, 0):
            s=signal(); s['market_metrics']['entry_execution_price']=price
            self.assertFalse(enroll(self.db, s, 'v', BASE))
        for ts in (0, BASE-120001, BASE+1):
            s=signal(); s['market_metrics']['ticker_sampled_at']=ts
            self.assertFalse(enroll(self.db, s, 'v', BASE))
        s=signal(); s['market_metrics']['entry_execution_price_source']='BID'
        self.assertFalse(enroll(self.db,s,'v',BASE))

    def test_each_direction_uses_correct_hit_side(self):
        for d in ('LONG','SHORT'):
            s=signal(d,direction=d); enroll(self.db,s,'v',BASE)
            p=[BASE,111,99,110] if d=='LONG' else [BASE,101,89,90]
            advance(self.db,[state([p])],BASE+BAR)
            self.assertEqual(self.row(d)['outcome'],'TP1_FIRST')
            self.db.execute('DELETE FROM card_statistics_v1')

    def test_stop_first_cannot_be_rewritten_by_later_target(self):
        enroll(self.db,signal(),'v',BASE)
        advance(self.db,[state([[BASE,102,94,96],[BASE+BAR,111,99,110]])],BASE+2*BAR)
        self.assertEqual(self.row()['outcome'],'SL_FIRST')
        advance(self.db,[state([[BASE+2*BAR,120,99,115]])],BASE+3*BAR)
        self.assertEqual(self.row()['outcome'],'SL_FIRST')

    def test_same_bar_both_hits_are_unknown(self):
        enroll(self.db,signal(),'v',BASE)
        advance(self.db,[state([[BASE,111,94,100]])],BASE+BAR)
        self.assertEqual(self.row()['reason'],'AMBIGUOUS_SAME_BAR')

    def test_entry_partial_bar_hit_cannot_be_used_as_win(self):
        enroll(self.db,signal(now=BASE+1000),'v',BASE+1000)
        advance(self.db,[state([[BASE,111,99,100]])],BASE+BAR)
        self.assertEqual(self.row()['outcome'],'UNKNOWN')
        self.assertEqual(self.row()['reason'],'PARTIAL_BOUNDARY_BAR')

    def test_safe_partial_entry_bar_then_next_bar_target(self):
        enroll(self.db,signal(now=BASE+1000),'v',BASE+1000)
        advance(self.db,[state([[BASE,102,99,100],[BASE+BAR,111,99,110]])],BASE+2*BAR)
        self.assertEqual(self.row()['outcome'],'TP1_FIRST')

    def test_future_unclosed_bar_not_used(self):
        enroll(self.db,signal(),'v',BASE)
        advance(self.db,[state([[BASE,111,99,110]])],BASE+BAR-1)
        self.assertEqual(self.row()['outcome'],'PENDING')

    def test_duplicate_conflict_is_missing_not_cherry_picked(self):
        enroll(self.db,signal(),'v',BASE)
        advance(self.db,[state([[BASE,111,99,110],[BASE,101,99,100]])],BASE+BAR)
        self.assertEqual(self.row()['outcome'],'PENDING')

    def test_missing_path_never_uses_latest_ticker(self):
        enroll(self.db,signal(),'v',BASE)
        s=state([]); s['market_metrics'].update(last_price=111,core_high=111,core_low=99)
        advance(self.db,[s],BASE+DAY+BAR)
        self.assertEqual(self.row()['outcome'],'UNKNOWN')

    def test_unrelated_coin_or_horizon_does_not_close_samples(self):
        enroll(self.db,signal(),'v',BASE)
        advance(self.db,[state([],inst_id='BBB-USDT-SWAP'),state([],horizon='LONG')],BASE+DAY+BAR)
        self.assertEqual(self.row()['outcome'],'PENDING')

    def test_no_scan_keeps_unresolved_not_assumed_loss(self):
        enroll(self.db,signal(),'v',BASE)
        advance(self.db,[],BASE+2*DAY)
        self.assertEqual(self.row()['outcome'],'PENDING')
        result=summarize(self.db,signal('current'),'v',BASE+2*DAY)
        self.assertEqual(result['pending'],1)
        self.assertEqual(result['losses'],0)

    def test_gap_can_be_recovered_before_deadline(self):
        enroll(self.db,signal(),'v',BASE)
        advance(self.db,[state([[BASE+BAR,111,99,110]])],BASE+2*BAR)
        self.assertEqual(self.row()['outcome'],'PENDING')
        advance(self.db,[state([[BASE,102,99,100],[BASE+BAR,111,99,110]])],BASE+3*BAR)
        self.assertEqual(self.row()['outcome'],'TP1_FIRST')

    def test_timeout_and_final_partial_bar_ambiguity(self):
        for offset in (0,1000):
            self.db.execute('DELETE FROM card_statistics_v1')
            enroll(self.db,signal(now=BASE+offset),'v',BASE+offset)
            path=[[BASE+i*BAR,102,99,100] for i in range(97)]
            if offset: path[-1]=[BASE+96*BAR,111,99,110]
            advance(self.db,[state(path)],BASE+DAY+BAR)
            self.assertEqual(self.row()['outcome'],'TIMEOUT' if not offset else 'UNKNOWN')

    def seed(self, count=50, days=5, wins=31, extra=None):
        now=BASE+20*DAY
        for i in range(count):
            observed=BASE+(i%days)*DAY
            s=signal(str(i),now=observed)
            enroll(self.db,s,'v',observed)
            outcome='TP1_FIRST' if i<wins else 'SL_FIRST'
            if extra and i>=count-extra[1]: outcome=extra[0]
            self.db.execute('UPDATE card_statistics_v1 SET outcome=?,resolved_ms=? WHERE signal_id=?',
                            (outcome,observed+2*BAR,str(i)))
        return now

    def test_zero_samples_does_not_mean_zero_win_rate(self):
        out=summarize(self.db,signal(),'v',BASE)
        self.assertIsNone(out['rate_pct'])
        self.assertEqual(out['status'],'INSUFFICIENT')

    def test_small_sample_suppression(self):
        now=self.seed(count=49)
        out=summarize(self.db,signal('current'),'v',now)
        self.assertIsNone(out['rate_pct'])
        self.assertEqual(out['resolved'],49)

    def test_sufficient_mature_history_produces_actual_ratio(self):
        now=self.seed()
        out=summarize(self.db,signal('current'),'v',now)
        self.assertEqual(out['rate_pct'],62)
        self.assertEqual(out['wins'],31)
        self.assertEqual(out['losses'],19)
        self.assertEqual(out['interval_pct'],wilson(31,50))
        self.assertNotIn('entry',out)

    def test_market_burst_not_fifty_independent_days(self):
        now=self.seed(days=1)
        self.assertIsNone(summarize(self.db,signal('current'),'v',now)['rate_pct'])

    def test_own_sample_excluded_even_if_completed(self):
        now=self.seed()
        out=summarize(self.db,signal('0'),'v',now)
        self.assertEqual(out['resolved'],49)
        self.assertIsNone(out['rate_pct'])

    def test_immature_resolved_winners_do_not_bias_rate(self):
        now=self.seed()
        out=summarize(self.db,signal('current'),'v',BASE+BAR)
        self.assertEqual(out['resolved'],0)
        self.assertIsNone(out['rate_pct'])
        self.assertEqual(out['total'],10)
        self.assertEqual(out['immature'],10)

    def test_version_and_cohort_never_fall_back_to_global_rate(self):
        now=self.seed()
        self.assertIsNone(summarize(self.db,signal('x'),'other',now)['rate_pct'])
        variants=[]
        s=signal('x',direction='SHORT'); variants.append(s)
        s=signal('x',horizon='LONG'); variants.append(s)
        s=signal('x'); s['trigger_type']='BREAKOUT'; variants.append(s)
        s=signal('x'); s['signal_stage']='CONFIRMED'; variants.append(s)
        s=signal('x'); s['timeframe_states']['4H']['direction']='SHORT'; variants.append(s)
        s=signal('x'); s['take_profit_1']=135; variants.append(s)
        for s in variants:
            self.assertEqual(summarize(self.db,s,'v',now)['total'],0)

    def test_unresolved_unknown_and_timeout_are_visible(self):
        now=self.seed(count=70,wins=40,extra=('UNKNOWN',15))
        self.db.execute("UPDATE card_statistics_v1 SET outcome='TIMEOUT' WHERE signal_id='54'")
        self.db.execute("UPDATE card_statistics_v1 SET outcome='PENDING',resolved_ms=NULL WHERE signal_id='53'")
        out=summarize(self.db,signal('current'),'v',now)
        self.assertEqual(out['unknown'],15)
        self.assertEqual(out['timeout'],1)
        self.assertEqual(out['pending'],1)
        self.assertEqual(out['status'],'LOW_COVERAGE')
        self.assertIsNone(out['rate_pct'])

    def test_no_future_result_or_old_history_leak(self):
        now=self.seed()
        self.db.execute('UPDATE card_statistics_v1 SET resolved_ms=?',(now+1,))
        self.assertEqual(summarize(self.db,signal('x'),'v',now)['resolved'],0)
        self.assertEqual(summarize(self.db,signal('x'),'v',now+100*DAY)['total'],0)

    def test_public_payload_rejects_legacy_quality_or_fake_rate(self):
        self.assertIsNone(public_summary({'score':99,'win_rate':99})['rate_pct'])
        self.assertIsNone(public_summary({'schema_version':VERSION,'status':'AVAILABLE','rate_pct':99})['rate_pct'])
        now=self.seed(); stats=summarize(self.db,signal('x'),'v',now)
        stats.update(rate_pct=99,private_rows=['secret'])
        public=public_summary(stats)
        self.assertEqual(public['rate_pct'],62)
        self.assertNotIn('private_rows',public)

    def test_public_candidate_exposes_only_guarded_summary(self):
        from radar.public_payload import public_candidate_payload
        s=signal(); s['historical_performance']={'score':99}
        self.assertIsNone(public_candidate_payload(s,signal=True)['historical_performance']['rate_pct'])

    def test_reference_wilson_and_clock(self):
        self.assertEqual(wilson(15,50),[19.1,43.8])
        self.assertIsNone(wilson(0,0))
        self.assertEqual(timestamp('2026-09-11T01:00:00+00:00'),1789088400000)
        self.assertEqual(timestamp('2026-09-11T01:00:00'),0)


class CardStatisticsIntegrationTests(unittest.TestCase):
    def ready(self, repository):
        from dataclasses import replace
        from datetime import datetime, timezone
        from tests.test_repository import signal_fixture
        raw=signal_fixture(event_ts=BASE-BAR,core_timestamp=BASE-BAR)
        at=datetime.fromtimestamp(BASE/1000,tz=timezone.utc).isoformat()
        created=repository.reconcile([raw],[],at,'SHORT')[0]
        values=signal(now=BASE)
        return replace(created,actionable=True,entry_eligibility=values['entry_eligibility'],
            decision_context=values['decision_context'],market_metrics=values['market_metrics'],
            timeframe_states=values['timeframe_states']),at

    def test_repository_records_string_prices_without_mutating_original_history(self):
        from radar.repository import SignalRepository
        repo=SignalRepository(':memory:')
        try:
            s,at=self.ready(repo)
            before=[tuple(row) for row in repo._connection.execute('SELECT * FROM signals')]
            events=repo._connection.execute('SELECT COUNT(*) FROM signal_events').fetchone()[0]
            output=repo.observe_card_statistics([s],[],at,'v')
            self.assertEqual(repo._connection.execute('SELECT COUNT(*) FROM card_statistics_v1').fetchone()[0],1)
            self.assertEqual(before,[tuple(row) for row in repo._connection.execute('SELECT * FROM signals')])
            self.assertEqual(events,repo._connection.execute('SELECT COUNT(*) FROM signal_events').fetchone()[0])
            self.assertEqual(output[0].direction,s.direction)
            self.assertEqual(output[0].entry_eligibility,s.entry_eligibility)
            self.assertEqual(output[0].decision_context,s.decision_context)
            self.assertEqual(output[0].stop_loss,s.stop_loss)
            self.assertIsNone(output[0].historical_performance['rate_pct'])
        finally: repo.close()

    def test_unregistered_or_changed_plan_not_recorded(self):
        from dataclasses import replace
        from radar.repository import SignalRepository
        repo=SignalRepository(':memory:')
        try:
            s,at=self.ready(repo)
            repo.observe_card_statistics([replace(s,trigger_id='not-stored'),replace(s,stop_loss='99')],[],at,'v')
            self.assertEqual(repo._connection.execute('SELECT COUNT(*) FROM card_statistics_v1').fetchone()[0],0)
        finally: repo.close()

    def test_stats_failure_never_changes_permission_or_plan(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from radar.repository import SignalRepository
        from radar.scanner import MarketScanner,ScannerConfig
        repo=SignalRepository(':memory:')
        try:
            s,at=self.ready(repo)
            def fail(*args,**kwargs): raise sqlite3.OperationalError('fixture failure')
            scanner=MarketScanner.__new__(MarketScanner)
            scanner.repository=SimpleNamespace(observe_card_statistics=fail)
            scanner.config=ScannerConfig()
            with patch('radar.scanner.card_statistics_fingerprint',return_value='v'):
                with self.assertLogs('radar.scanner',level='WARNING'):
                    result=scanner._card_statistics([s],[],at)
            self.assertEqual(result[0].decision_context,s.decision_context)
            self.assertEqual(result[0].entry_eligibility,s.entry_eligibility)
            self.assertEqual(result[0].stop_loss,s.stop_loss)
        finally: repo.close()

    def test_final_html_and_public_projection_have_stats_not_quality_conversion(self):
        from pathlib import Path
        html=(Path(__file__).resolve().parents[1]/'radar/static/pages.html').read_text()
        self.assertIn("decisionPanelBody(item)+quickLookPanel(item)+(window.HistoryReplay?.card(item,isPreviewItem(item))||'')+historicalStatsPanel(item)",html)
        self.assertIn('return [item.historical_performance,item.timeframe_states',html)




class PublishedScannerStatisticsTests(unittest.TestCase):
    def test_final_publication_enrolls_once_preview_and_blocked_do_not(self):
        import time
        from dataclasses import replace
        from tests.test_scanner import ContextFakeClient, qualified_signal, qualified_state
        from radar.models import Ticker
        from radar.scanner import MarketScanner, ScannerConfig
        from radar.strategy import AnalysisResult
        class Client(ContextFakeClient):
            def __init__(self):
                super().__init__()
                self.instruments=self.instruments[:1]
                self.last=100.0
            def get_swap_tickers(self):
                return {x.inst_id:Ticker(x.inst_id,self.last,self.last-.01,self.last+.01,
                        int(time.time()*1000),20_000_000) for x in self.instruments}
        class Engine:
            def analyze(self,instrument,ticker,*args,**kwargs):
                s=qualified_signal(instrument.inst_id)
                s=replace(s,trigger_id='',lifecycle={'current_stage':'CONFIRMED','transition':'TECHNICAL_EVENT'},
                          market_metrics={**s.market_metrics,'last_price':ticker.last})
                return AnalysisResult(s,'qualified',qualified_state(s))
        client=Client()
        scanner=MarketScanner(client,ScannerConfig(workers=1,minimum_rr=1.5))
        scanner.engine=Engine()
        try:
            def preview(report):
                self.assertEqual(scanner.repository._connection.execute('SELECT COUNT(*) FROM card_statistics_v1').fetchone()[0],0)
            report=scanner.scan_once(scan_mode='SHORT',preview=preview)
            self.assertTrue(report.signals[0].actionable)
            rows=scanner.repository._connection.execute('SELECT * FROM card_statistics_v1').fetchall()
            self.assertEqual(len(rows),1)
            self.assertAlmostEqual(rows[0]['entry'],100.01)
            self.assertEqual(report.signals[0].historical_performance['schema_version'],'CARD_STATISTICS_V1')
            self.assertIsNone(report.signals[0].historical_performance['rate_pct'])
            client.last=104.0
            report=scanner.scan_once(scan_mode='SHORT')
            self.assertFalse(report.signals[0].actionable)
            self.assertEqual(scanner.repository._connection.execute('SELECT COUNT(*) FROM card_statistics_v1').fetchone()[0],1)
            self.assertAlmostEqual(scanner.repository._connection.execute('SELECT entry FROM card_statistics_v1').fetchone()[0],100.01)
        finally:
            scanner.repository.close()

if __name__ == '__main__':
    unittest.main()
