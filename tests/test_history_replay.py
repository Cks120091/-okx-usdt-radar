"""No market calls: causality, state/permission separation and resource tests."""
import copy
import json
import math
import tempfile
import threading
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from radar.config import AppConfig
from radar.history_replay import (CORE, STEP, DAY, HISTORY_SYMBOLS, INTERVALS, Interrupted, aggregate,
    classify_path, config_for_replay, delayed_attempt, fetch_history, fingerprint,
    minimum_sample_days, past_window, replay_symbol, valid_bar, _price_projection)
from radar.history_jobs import HistoryManager, _connect, _update, _latest, _rebuild
from radar.models import Candle, Instrument
from radar.scanner import MarketScanner, ScannerConfig
from tests.test_short_entry_window import ready_signal

BASE = 1_800_000_000_000 // DAY * DAY


def bar(ts=BASE, open=100, high=101, low=99, close=100, interval=STEP):
    return Candle(ts, open, high, low, close, 100, 100_000, True)


def rows(start=BASE, count=288):
    return {start+i*STEP:bar(start+i*STEP) for i in range(count)}


def sample(i=0, outcome='TP1_FIRST', key='["SHORT","LONG"]'):
    return {'cohort':key, 'label':'測試情境', 'entry_ms':BASE+i*DAY,
            'outcome':outcome}


class HistoryPathTests(unittest.TestCase):
    def test_long_target_first(self):
        data=rows();data[BASE]=bar(high=111)
        self.assertEqual(classify_path('LONG',100,95,110,BASE,data)['outcome'],'TP1_FIRST')

    def test_long_stop_first(self):
        data=rows();data[BASE]=bar(low=94)
        self.assertEqual(classify_path('LONG',100,95,110,BASE,data)['outcome'],'SL_FIRST')

    def test_short_target_first(self):
        data=rows();data[BASE]=bar(low=89)
        self.assertEqual(classify_path('SHORT',100,105,90,BASE,data)['outcome'],'TP1_FIRST')

    def test_short_stop_first(self):
        data=rows();data[BASE]=bar(high=106)
        self.assertEqual(classify_path('SHORT',100,105,90,BASE,data)['outcome'],'SL_FIRST')

    def test_both_barriers_unknown_not_best_case(self):
        data=rows();data[BASE]=bar(high=111,low=94)
        self.assertEqual(classify_path('LONG',100,95,110,BASE,data)['outcome'],'UNKNOWN')

    def test_missing_early_path_not_inferred_from_later_target(self):
        data=rows();del data[BASE];data[BASE+STEP]=bar(BASE+STEP,high=120)
        self.assertEqual(classify_path('LONG',100,95,110,BASE,data)['outcome'],'UNKNOWN')

    def test_timeout_separate_and_no_post_horizon_target(self):
        data=rows();data[BASE+DAY]=bar(BASE+DAY,high=120)
        result=classify_path('LONG',100,95,110,BASE,data)
        self.assertEqual(result['outcome'],'TIMEOUT');self.assertEqual(result['r'],0)

    def test_prior_to_entry_target_not_counted(self):
        data=rows();data[BASE-STEP]=bar(BASE-STEP,high=120)
        self.assertEqual(classify_path('LONG',100,95,110,BASE,data)['outcome'],'TIMEOUT')

    def test_invalid_geometry_and_unaligned_entries_rejected(self):
        for direction,stop,target,ts in [('LONG',101,110,BASE),('SHORT',95,90,BASE),('NONE',95,110,BASE),('LONG',95,110,BASE+1)]:
            with self.assertRaises(ValueError):classify_path(direction,100,stop,target,ts,rows())

    def test_unconfirmed_and_nonfinite_not_used(self):
        for candle in [replace(bar(),confirmed=False),replace(bar(),high=math.inf),replace(bar(),open=105),replace(bar(),ts=BASE+1)]:
            self.assertFalse(valid_bar(candle,STEP))


class HistoricalDataTests(unittest.TestCase):
    def test_pagination_stops_at_target_range(self):
        client=Mock();client._get.side_effect=[[[BASE+STEP,'100','101','99','100','5','5','500','1']],[[BASE,'100','101','99','100','5','5','500','1']]]
        found=fetch_history(client,'AAA-USDT-SWAP','5m',BASE,BASE+2*STEP)
        self.assertEqual([x.ts for x in found],[BASE,BASE+STEP])
        self.assertEqual(client._get.call_count,2)

    def test_repeated_page_raises_instead_of_looping(self):
        client=Mock();client._get.return_value=[[BASE+STEP,'100','101','99','100','5','5','500','1']]
        with self.assertRaises(ValueError):fetch_history(client,'AAA','5m',BASE,BASE+3*STEP)

    def test_future_and_unconfirmed_excluded(self):
        client=Mock();client._get.return_value=[[BASE,'100','101','99','100','5','5','500','0'],[BASE+STEP,'100','101','99','100','5','5','500','1']]
        self.assertEqual(fetch_history(client,'AAA','5m',BASE,BASE+STEP),[])

    def test_cancellation_does_not_look_like_success(self):
        def stop():raise Interrupted('pause')
        client=Mock()
        with self.assertRaises(Interrupted):fetch_history(client,'AAA','5m',BASE,BASE+STEP,stop)
        client._get.assert_not_called()

    def test_future_higher_timeframe_close_never_visible(self):
        interval=INTERVALS['4H'];data=[bar(BASE+i*interval) for i in range(65)]
        asof=BASE+60*interval+CORE
        found=past_window(data,[x.ts+interval for x in data],asof,interval,200)
        self.assertEqual(len(found),60)
        self.assertLessEqual(found[-1].ts+interval,asof)

    def test_gap_blocks_entire_indicator_window(self):
        data=[bar(BASE+i*STEP) for i in range(70)];del data[30]
        self.assertEqual(past_window(data,[x.ts+STEP for x in data],BASE+70*STEP,STEP,200),[])

    def test_too_short_or_old_is_unavailable(self):
        data=[bar(BASE+i*STEP) for i in range(60)]
        self.assertEqual(past_window(data,[x.ts+STEP for x in data],BASE+61*STEP,STEP,200),[])
        self.assertEqual(past_window(data[:10],[x.ts+STEP for x in data[:10]],BASE+10*STEP,STEP,200),[])

    def test_config_memory_store_and_single_worker(self):
        cfg=config_for_replay({'state_db_path':'/live.sqlite','workers':99,'minimum_rr':2.1})
        self.assertEqual(cfg.state_db_path,':memory:');self.assertEqual(cfg.workers,1);self.assertEqual(cfg.minimum_rr,2.1)

    def test_config_change_separates_results(self):
        self.assertNotEqual(fingerprint({'minimum_rr':1.8}),fingerprint({'minimum_rr':2.0}))

    def test_short_history_scope_is_fixed_to_eight_major_tokens(self):
        self.assertEqual(HISTORY_SYMBOLS, ('BTC-USDT-SWAP','ETH-USDT-SWAP','SOL-USDT-SWAP','XRP-USDT-SWAP','DOGE-USDT-SWAP','ADA-USDT-SWAP','LINK-USDT-SWAP','AVAX-USDT-SWAP'))
        self.assertEqual(minimum_sample_days(3),3);self.assertEqual(minimum_sample_days(7),5)


class HistoricalStatisticsTests(unittest.TestCase):
    def test_release_only_complete_adequate_cohort(self):
        samples=[sample(i%5,'TP1_FIRST' if i<31 else 'SL_FIRST') for i in range(50)]
        result=aggregate([{'samples':samples}],complete=True)
        group=next(iter(result['groups'].values()))
        self.assertEqual(group['rate_pct'],62);self.assertEqual(group['days'],5)
        self.assertEqual(len(group['interval_pct']),2)

    def test_three_day_mode_releases_after_three_sample_dates(self):
        samples=[sample(i%3) for i in range(50)]
        g3=next(iter(aggregate([{'samples':samples}],complete=True,days=3)['groups'].values()))
        g7=next(iter(aggregate([{'samples':samples}],complete=True,days=7)['groups'].values()))
        self.assertEqual(g3['rate_pct'],100);self.assertEqual(g3['minimum_days'],3);self.assertIsNone(g7['rate_pct'])

    def test_symbol_groups_are_separate_and_pooled_group_is_preserved(self):
        btc=[sample(i%5,'TP1_FIRST') for i in range(50)]
        eth=[sample(i%5,'SL_FIRST') for i in range(50)]
        result=aggregate([{'inst_id':'BTC-USDT-SWAP','samples':btc},{'inst_id':'ETH-USDT-SWAP','samples':eth}],complete=True,days=7)
        key='[\"SHORT\",\"LONG\"]'
        self.assertEqual(result['symbol_groups']['BTC-USDT-SWAP'][key]['rate_pct'],100)
        self.assertEqual(result['symbol_groups']['ETH-USDT-SWAP'][key]['rate_pct'],0)
        self.assertEqual(result['groups'][key]['rate_pct'],50)

    def test_partial_never_advertises_full_market_percentage(self):
        g=next(iter(aggregate([{'samples':[sample(i%5) for i in range(60)]}],complete=False)['groups'].values()))
        self.assertIsNone(g['rate_pct']);self.assertEqual(g['status'],'PARTIAL')

    def test_unknown_timeout_lower_coverage_and_are_counted(self):
        samples=[sample(i%5) for i in range(50)]+[sample(0,'UNKNOWN') for i in range(15)]+[sample(0,'TIMEOUT') for i in range(10)]
        g=next(iter(aggregate([{'samples':samples}],complete=True)['groups'].values()))
        self.assertEqual(g['unknown'],15);self.assertEqual(g['timeout'],10);self.assertIsNone(g['rate_pct'])

    def test_small_samples_and_same_day_not_released(self):
        for samples in [[sample(i%5) for i in range(49)],[sample(0) for i in range(60)]]:
            self.assertIsNone(next(iter(aggregate([{'samples':samples}],complete=True)['groups'].values()))['rate_pct'])

    def test_cohorts_never_fallback_to_unrelated_ones(self):
        result=aggregate([{'samples':[sample(i%5,key='A') for i in range(60)]+[sample(key='B')]}],complete=True)
        self.assertEqual(result['groups']['A']['rate_pct'],100);self.assertIsNone(result['groups']['B']['rate_pct'])


class EntryModelTests(unittest.TestCase):
    def test_delay_touching_barrier_does_not_fake_fill(self):
        item=ready_signal('LONG');item.stop_loss='95';item.take_profit_1='110'
        result,reason=delayed_attempt(Mock(),item,BASE,{BASE:bar(low=94),BASE+STEP:bar(BASE+STEP)})
        self.assertIsNone(result);self.assertEqual(reason,'BARRIER_BEFORE_ENTRY')

    def test_later_retest_attempt_not_globally_suppressed(self):
        item=ready_signal('LONG');item.stop_loss='95';item.take_profit_1='110';data=rows()
        with patch('radar.history_replay._price_projection',side_effect=[SimpleNamespace(actionable=False),SimpleNamespace(actionable=True)]):
            first,_=delayed_attempt(Mock(),item,BASE,data)
            later,_=delayed_attempt(Mock(),item,BASE+CORE,data)
        self.assertIsNone(first);self.assertIsNotNone(later)

    def test_projection_does_not_mutate_source_direction_plan(self):
        item=ready_signal('LONG');before=copy.deepcopy(item.to_dict())
        scanner=MarketScanner(None,ScannerConfig())
        try:
            _price_projection(scanner,item,item.market_metrics['ticker_sampled_at'],.3341)
            self.assertEqual(before,item.to_dict())
        finally:scanner.repository.close()

    def test_raw_rr_below_threshold_not_rounded_into_permission(self):
        item=ready_signal('LONG');item.stop_loss='90';item.take_profit_1='117.999';item.entry_low='99';item.entry_high='101'
        item.market_metrics['last_price']=100
        item.lifecycle.pop('entry_window',None);item.lifecycle['transition']='NEW'
        scanner=MarketScanner(None,ScannerConfig())
        try:self.assertFalse(_price_projection(scanner,item,item.market_metrics['ticker_sampled_at'],100).actionable)
        finally:scanner.repository.close()

    def test_closed_loop_uses_real_engine_and_returns_zero_not_fake_samples(self):
        histories={tf:[bar(ts) for ts in range(BASE-50*DAY,BASE+3*DAY,interval)] for tf,interval in INTERVALS.items()}
        instrument=Instrument('FLAT-USDT-SWAP','live','USDT','linear',.01)
        original=copy.deepcopy(histories)
        result=replay_symbol(instrument,histories,BASE,BASE+CORE*4,{'min_quote_volume_24h':0})
        self.assertEqual(result['status'],'OK');self.assertEqual(result['evaluated'],4)
        self.assertEqual(result['samples'],[]);self.assertEqual(histories,original)


class HistoryManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.runtime=SimpleNamespace(config=AppConfig(data_dir=self.temp.name),_running=False,_scan_lock=threading.RLock())
        self.manager=HistoryManager(self.runtime);self.addCleanup(self.manager.close)

    def test_initial_status_never_starts_worker_or_market_call(self):
        with patch('subprocess.Popen') as spawn:
            result=self.manager.status()
            self.assertEqual(result['status'],'IDLE');spawn.assert_not_called()

    def test_csrf_and_invalid_operations_rejected(self):
        with self.assertRaises(PermissionError):self.manager.command('start',token='bad')
        with self.assertRaises(ValueError):self.manager.command('oops',token=self.manager.token)
        for value in [True,'7',0,30,90]:
            with self.assertRaises(ValueError):self.manager.command('start',days=value,token=self.manager.token)

    def test_job_starts_with_past_full_outcome_period(self):
        with patch.object(self.manager,'_spawn') as spawn:
            result=self.manager.command('start',token=self.manager.token)
            spawn.assert_called_once()
        self.assertEqual(result['end_ms']-result['start_ms'],7*DAY)
        self.assertEqual(result['status'],'QUEUED')
        row=_latest(self.manager.path)
        self.assertLessEqual(result['end_ms']+DAY+STEP,row['created_ms'])

    def test_same_job_not_duplicated_while_process_running(self):
        with patch.object(self.manager,'_spawn'):
            first=self.manager.command('start',token=self.manager.token)
        self.manager.process=Mock();self.manager.process.poll.return_value=None
        second=self.manager.command('start',token=self.manager.token)
        self.assertEqual(first['id'],second['id']);self.manager.process.poll.return_value=0

    def test_spawn_error_is_visible_not_stuck_queued(self):
        with patch('subprocess.Popen',side_effect=OSError('cannot start')):
            with self.assertRaises(OSError):self.manager.command('start',token=self.manager.token)
        self.assertEqual(self.manager.status()['status'],'ERROR')

    def test_restart_marks_interrupted_but_does_not_auto_resume(self):
        with patch.object(self.manager,'_spawn'):
            self.manager.command('start',token=self.manager.token)
        another=HistoryManager(self.runtime)
        self.assertEqual(another.status()['status'],'INTERRUPTED');self.assertIsNone(another.process)
        another.close()

    def test_version_change_never_reuses_old_percent(self):
        with patch.object(self.manager,'_spawn'):
            result=self.manager.command('start',token=self.manager.token)
        self.manager.fingerprint='changed'
        status=self.manager.status();self.assertFalse(status['compatible']);self.assertEqual(status['groups'],{})
        self.assertEqual(status['status'],'VERSION_CHANGED')

    def test_delete_keeps_live_database_intact(self):
        live=Path(self.temp.name)/'radar_state.sqlite3';live.write_bytes(b'preserve original')
        with patch.object(self.manager,'_spawn'):
            self.manager.command('start',token=self.manager.token)
        self.manager.command('delete',token=self.manager.token)
        self.assertEqual(live.read_bytes(),b'preserve original');self.assertEqual(self.manager.status()['status'],'IDLE')

    def test_no_original_tables_in_research_database(self):
        with _connect(self.manager.path) as connection:
            names={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('signals',names);self.assertNotIn('card_statistics_v1',names)

    def test_resume_preserves_range_and_done_rows(self):
        with patch.object(self.manager,'_spawn'):
            result=self.manager.command('start',token=self.manager.token)
            _update(self.manager.path,result['id'],status='PAUSED',done=2)
            resumed=self.manager.command('resume',token=self.manager.token)
        self.assertEqual(result['id'],resumed['id']);self.assertEqual(resumed['done'],2)

    def test_aggregate_no_rate_when_scope_missing(self):
        with patch.object(self.manager,'_spawn'):
            result=self.manager.command('start',token=self.manager.token)
        _update(self.manager.path,result['id'],total=5)
        raw={'inst_id':'AAA','status':'OK','evaluated':7*96,'samples':[sample(i%5) for i in range(60)]}
        with _connect(self.manager.path) as connection:
            connection.execute('INSERT INTO history_symbols_v1 VALUES(?,?,?,?)',(result['id'],'AAA','OK',json.dumps(raw)))
        _rebuild(self.manager.path,result['id'],complete=True)
        stat=self.manager.status();self.assertEqual(stat['scope_coverage_pct'],20)
        self.assertIsNone(next(iter(stat['groups'].values()))['rate_pct'])
        self.assertNotIn('settings',stat);self.assertNotIn('samples',next(iter(stat['groups'].values())))

class HistoryHTTPIsolationTests(unittest.TestCase):
    def exercise(self, call):
        from radar.service import serve
        runtime=SimpleNamespace(status=lambda:{'system_status':'READY'},stop=Mock())
        def server_factory(address,handler_type):
            server=Mock()
            def run(**kwargs):
                handler=object.__new__(handler_type)
                handler.headers={'Host':'localhost'}
                handler._send_json=Mock();handler._send_bytes=Mock()
                call(handler)
            server.serve_forever.side_effect=run
            return server
        with patch('radar.service.ThreadingHTTPServer',side_effect=server_factory),patch('radar.history_jobs.HistoryManager',side_effect=OSError('no history store')) as manager:
            serve(runtime,'localhost',0)
            return manager.call_count

    def test_home_and_health_never_initialize_history_or_start_job(self):
        def calls(handler):
            handler.path='/';handler.do_GET()
            self.assertEqual(handler._send_bytes.call_args.args[0],200)
            handler.path='/health';handler.do_GET()
            self.assertTrue(handler._send_json.call_args.args[1]['ok'])
        self.assertEqual(self.exercise(calls),0)

    def test_history_failure_does_not_break_health(self):
        def calls(handler):
            with self.assertLogs('okx_radar',level='ERROR'):
                handler.path='/api/history-scan/status';handler.do_GET()
            self.assertEqual(handler._send_json.call_args.args[0],503)
            handler.path='/health';handler.do_GET()
            self.assertEqual(handler._send_json.call_args.args[0],200)
        self.assertEqual(self.exercise(calls),1)

    def test_cross_origin_post_never_launches_history(self):
        def calls(handler):
            handler.path='/api/history-scan/start'
            handler.headers={'Host':'localhost','Origin':'https://other.example','X-History-Intent':'user'}
            handler.do_POST()
            self.assertEqual(handler._send_json.call_args.args[0],403)
        self.assertEqual(self.exercise(calls),0)


if __name__=='__main__':unittest.main()
