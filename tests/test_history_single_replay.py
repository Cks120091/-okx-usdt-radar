"""Offline tests for the single-coin 15m Trigger-time history model."""
import tempfile
import threading
import unittest
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from radar.config import AppConfig
from radar.history_jobs import HistoryManager
from radar.history_replay import CORE, STEP
from radar.history_single_replay import VERSION, aggregate, replay_symbol
from radar.models import Candle, Instrument
from tests.test_short_entry_window import ready_signal

BASE = 1_800_000_000_000 // 86_400_000 * 86_400_000


def candle(ts=BASE, price=100.0):
    return Candle(ts, price, price + 1, price - 1, price, 100, 100_000, True)


def sample(outcome='TP1_FIRST', i=0, key='same'):
    return {
        'cohort': key,
        'label': '15m測試情境',
        'entry_ms': BASE + i * CORE,
        'trigger_ms': BASE + i * CORE,
        'opportunity_kind': 'TRIGGER',
        'outcome': outcome,
    }


class SingleCoinAggregateTests(unittest.TestCase):
    def test_even_one_resolved_sample_is_visible_but_marked_extremely_low(self):
        result = aggregate([{'inst_id':'BTC-USDT-SWAP','samples':[sample()]}], complete=True, days=7)
        self.assertEqual(result['overall']['rate_pct'], 100.0)
        self.assertEqual(result['overall']['tier'], '極低樣本')
        self.assertEqual(result['overall']['total'], 1)
        self.assertEqual(result['overall']['resolved'], 1)

    def test_all_trigger_outcomes_are_counted_without_hiding_timeout_unknown(self):
        samples = [sample('TP1_FIRST', i) for i in range(12)]
        samples += [sample('SL_FIRST', 20+i) for i in range(8)]
        samples += [sample('TIMEOUT', 40), sample('UNKNOWN', 41)]
        result = aggregate([{'inst_id':'SOL-USDT-SWAP','samples':samples}], complete=True, days=3)
        overall = result['overall']
        self.assertEqual(overall['total'], 22)
        self.assertEqual(overall['resolved'], 20)
        self.assertEqual(overall['rate_pct'], 60.0)
        self.assertEqual(overall['tier'], '中等樣本')
        self.assertEqual(overall['timeout'], 1)
        self.assertEqual(overall['unknown'], 1)

    def test_trigger_samples_have_no_reentry_sample_class(self):
        samples = [sample('TP1_FIRST', 0), sample('SL_FIRST', 1)]
        result = aggregate([{'inst_id':'BTC-USDT-SWAP','samples':samples}], complete=True, days=7)
        self.assertEqual(result['overall']['total'], 2)
        self.assertEqual(result['initial_overall']['total'], 2)
        self.assertEqual(result['reentry_overall']['total'], 0)
        self.assertEqual(result['trigger_signals'], 2)
        self.assertEqual(result['initial_signals'], 2)
        self.assertEqual(result['reentry_signals'], 0)
        self.assertEqual(result['overall']['rate_pct'], 50.0)

    def test_incomplete_history_never_releases_rate(self):
        result = aggregate([{'inst_id':'BTC-USDT-SWAP','samples':[sample()]}], complete=False, days=7)
        self.assertIsNone(result['overall']['rate_pct'])
        self.assertEqual(result['overall']['status'], 'PARTIAL')


class FirstTriggerEpisodeTests(unittest.TestCase):
    def _run_trigger_sequence(self, trigger_ids, actionable=None):
        start, end = BASE, BASE + len(trigger_ids) * CORE
        base_signal = ready_signal('LONG')
        base_signal.stop_loss = '95'
        base_signal.take_profit_1 = '110'
        base_signal.entry_low = '99'
        base_signal.entry_high = '101'
        base_signal.radar_horizon = 'SHORT'
        base_signal.signal_stage = 'EARLY_SIGNAL'
        base_signal.lifecycle = {**base_signal.lifecycle, 'terminal': False}
        actionable = actionable or [False] * len(trigger_ids)

        class Repo:
            def load_active_signal(self, *_): return None
            def load_story(self, *_): return {}
            def excursion_profile(self, *_): return None
            def reconcile(self, signals, *_): return list(signals)
            def close(self): pass

        class Engine:
            def analyze(self, instrument, ticker, *args, **kwargs):
                if ticker.ts < start:
                    return SimpleNamespace(market_state=None, signal=None)
                index = (ticker.ts - start) // CORE
                if not 0 <= index < len(trigger_ids):
                    return SimpleNamespace(market_state=None, signal=None)
                trigger_id = trigger_ids[index]
                if not trigger_id:
                    return SimpleNamespace(market_state=None, signal=None)
                signal = replace(
                    base_signal,
                    trigger_id=trigger_id,
                    actionable=bool(actionable[index]),
                    entry_eligibility={
                        **base_signal.entry_eligibility,
                        'actionable': bool(actionable[index]),
                        'new_entry_allowed': bool(actionable[index]),
                    },
                    market_metrics={
                        **base_signal.market_metrics,
                        'trigger_event_ts': ticker.ts - CORE,
                    },
                )
                return SimpleNamespace(market_state=None, signal=signal)

        fake = SimpleNamespace(
            config=SimpleNamespace(
                candle_limit_4h=60, candle_limit_1h=60, candle_limit_15m=60,
                candle_limit_5m=60, min_quote_volume_24h=0,
                quote_volume_buffer_24h=0,
            ),
            repository=Repo(),
            engine=Engine(),
        )
        histories = {tf:[candle(BASE)] for tf in ('5m','15m','1H','4H')}
        window = [candle(BASE + i * STEP) for i in range(288)]
        instrument = Instrument('BTC-USDT-SWAP','live','USDT','linear',0.01)

        with patch('radar.history_single_replay.MarketScanner', return_value=fake), \
             patch('radar.history_single_replay.past_window', return_value=window), \
             patch('radar.history_single_replay.classify_path', return_value={'outcome':'TP1_FIRST','r':2.0}):
            return replay_symbol(instrument, histories, start, end, {'min_quote_volume_24h':0})

    def test_trigger_is_sampled_immediately_even_before_entry_permission(self):
        result = self._run_trigger_sequence(['episode-one'], actionable=[False])
        self.assertEqual(result['trigger_signals'], 1)
        self.assertEqual(len(result['samples']), 1)
        self.assertEqual(result['samples'][0]['episode'], 'episode-one')
        self.assertEqual(result['samples'][0]['trigger_ms'], BASE)
        self.assertEqual(result['samples'][0]['entry_ms'], BASE)
        self.assertEqual(result['samples'][0]['opportunity_kind'], 'TRIGGER')

    def test_same_episode_retest_and_entry_confirmation_do_not_add_samples(self):
        result = self._run_trigger_sequence(
            ['episode-one'] * 6,
            actionable=[False, False, True, False, False, True],
        )
        self.assertEqual(result['trigger_signals'], 1)
        self.assertEqual(result['initial_signals'], 1)
        self.assertEqual(result['reentry_signals'], 0)
        self.assertEqual(len(result['samples']), 1)

    def test_distinct_episode_gets_its_own_trigger_time_sample(self):
        result = self._run_trigger_sequence(
            ['episode-one', 'episode-one', 'episode-two'],
            actionable=[False, True, False],
        )
        self.assertEqual(result['trigger_signals'], 2)
        self.assertEqual(
            [row['episode'] for row in result['samples']],
            ['episode-one', 'episode-two'],
        )
        self.assertEqual(
            [row['trigger_ms'] for row in result['samples']],
            [BASE, BASE + 2 * CORE],
        )


class SingleCoinManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        runtime = SimpleNamespace(
            config=AppConfig(data_dir=self.temp.name),
            _running=False,
            _scan_lock=threading.RLock(),
        )
        self.manager = HistoryManager(runtime)
        self.addCleanup(self.manager.close)

    def test_new_request_accepts_only_supported_short_history_ranges(self):
        supported = (3, 7, 14, 30)
        for days in supported:
            with self.subTest(days=days):
                with patch.object(self.manager, '_spawn') as spawn:
                    result = self.manager.command(
                        'start',
                        days={'days':days, 'inst_id':'sol-usdt-swap'},
                        token=self.manager.token,
                    )
                spawn.assert_called_once()
                self.assertEqual(result['schema_version'], VERSION)
                self.assertEqual(result['inst_id'], 'SOL-USDT-SWAP')
                self.assertEqual(result['days'], days)
                self.assertIn('SOL-USDT-SWAP', result['coins'])
                self.manager.command('delete', days={'days':days, 'inst_id':'SOL-USDT-SWAP'}, token=self.manager.token)

        for invalid in (0, 60, 90, 180, 270, 365, 366):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.manager.command('start', days={'days':invalid, 'inst_id':'SOL-USDT-SWAP'}, token=self.manager.token)

    def test_history_page_only_exposes_3_7_14_30_days(self):
        root = Path(__file__).parents[1]
        html = (root / 'radar/static/history-scan.html').read_text(encoding='utf-8')
        for value in (3, 7, 14, 30):
            self.assertIn(f'value="{value}"', html)
        for value in (90, 180, 270, 365):
            self.assertNotIn(f'value="{value}"', html)

    def test_different_coin_does_not_inherit_cached_result(self):
        with patch.object(self.manager, '_spawn'):
            self.manager.command(
                'start',
                days={'days':7, 'inst_id':'BTC-USDT-SWAP'},
                token=self.manager.token,
            )
        status = self.manager.status()
        self.assertIn('BTC-USDT-SWAP', status['coins'])
        self.assertNotIn('MINA-USDT-SWAP', status['coins'])

    def test_delete_all_clears_every_coin_without_touching_schema(self):
        with patch.object(self.manager, '_spawn'):
            self.manager.command('start', days={'days':3, 'inst_id':'BTC-USDT-SWAP'}, token=self.manager.token)
            self.manager.command('start', days={'days':7, 'inst_id':'SOL-USDT-SWAP'}, token=self.manager.token)
        self.assertEqual(set(self.manager.status()['coins']), {'BTC-USDT-SWAP', 'SOL-USDT-SWAP'})
        cleared = self.manager.command('delete_all', token=self.manager.token)
        self.assertEqual(cleared['status'], 'IDLE')
        self.assertEqual(cleared['coins'], {})
        with patch.object(self.manager, '_spawn') as spawn:
            restarted = self.manager.command('start', days={'days':3, 'inst_id':'ETH-USDT-SWAP'}, token=self.manager.token)
        spawn.assert_called_once()
        self.assertIn('ETH-USDT-SWAP', restarted['coins'])

    def test_history_page_places_clear_all_in_compact_data_tools(self):
        root = Path(__file__).parents[1]
        html = (root / 'radar/static/history-scan.html').read_text(encoding='utf-8')
        js = (root / 'radar/static/history-scan.js').read_text(encoding='utf-8')
        self.assertIn('id="deleteAll"', html)
        self.assertIn('說明、完整度與資料管理', html)
        self.assertIn("const clearAll = action === 'delete_all';", js)
        self.assertIn("command('delete_all')", js)
        self.assertIn("clearAll ? {csrf:latest.csrf}", js)


if __name__ == '__main__':
    unittest.main()
