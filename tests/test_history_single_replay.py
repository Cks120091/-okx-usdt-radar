"""Offline tests for the single-coin 15m history model."""
import tempfile
import threading
import unittest
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
        'outcome': outcome,
    }


class SingleCoinAggregateTests(unittest.TestCase):
    def test_even_one_resolved_sample_is_visible_but_marked_extremely_low(self):
        result = aggregate([{'inst_id':'BTC-USDT-SWAP','samples':[sample()]}], complete=True, days=7)
        self.assertEqual(result['overall']['rate_pct'], 100.0)
        self.assertEqual(result['overall']['tier'], '極低樣本')
        self.assertEqual(result['overall']['total'], 1)
        self.assertEqual(result['overall']['resolved'], 1)

    def test_all_actionable_outcomes_are_counted_without_hiding_timeout_unknown(self):
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

    def test_incomplete_history_never_releases_rate(self):
        result = aggregate([{'inst_id':'BTC-USDT-SWAP','samples':[sample()]}], complete=False, days=7)
        self.assertIsNone(result['overall']['rate_pct'])
        self.assertEqual(result['overall']['status'], 'PARTIAL')


class FirstActionableEpisodeTests(unittest.TestCase):
    def test_same_episode_is_sampled_once_at_first_actionable_15m_close(self):
        start, end = BASE, BASE + 2 * CORE
        signal = ready_signal('LONG')
        signal.trigger_id = 'episode-one'
        signal.stop_loss = '95'
        signal.take_profit_1 = '110'
        signal.entry_low = '99'
        signal.entry_high = '101'
        signal.radar_horizon = 'SHORT'
        signal.actionable = True
        signal.lifecycle = {**signal.lifecycle, 'terminal': False}

        class Repo:
            def load_active_signal(self, *_): return None
            def load_story(self, *_): return {}
            def excursion_profile(self, *_): return None
            def reconcile(self, signals, *_): return list(signals)
            def close(self): pass

        class Engine:
            def analyze(self, instrument, ticker, *args, **kwargs):
                return SimpleNamespace(market_state=None, signal=signal if ticker.ts >= start else None)

        fake = SimpleNamespace(
            config=SimpleNamespace(
                candle_limit_4h=60, candle_limit_1h=60, candle_limit_15m=60,
                candle_limit_5m=60, min_quote_volume_24h=0,
                quote_volume_buffer_24h=0,
            ),
            repository=Repo(),
            engine=Engine(),
            _record_entry_window=lambda item: item,
        )
        histories = {tf:[candle(BASE)] for tf in ('5m','15m','1H','4H')}
        window = [candle(BASE + i * STEP) for i in range(288)]
        instrument = Instrument('BTC-USDT-SWAP','live','USDT','linear',0.01)
        with patch('radar.history_single_replay.MarketScanner', return_value=fake), \
             patch('radar.history_single_replay.past_window', return_value=window), \
             patch('radar.history_single_replay._price_projection', side_effect=lambda scanner,item,asof,price: replace(item, actionable=True)), \
             patch('radar.history_single_replay.classify_path', return_value={'outcome':'TP1_FIRST','r':2.0}):
            result = replay_symbol(instrument, histories, start, end, {'min_quote_volume_24h':0})
        self.assertEqual(result['actionable_signals'], 1)
        self.assertEqual(len(result['samples']), 1)
        self.assertEqual(result['samples'][0]['episode'], 'episode-one')
        self.assertEqual(result['samples'][0]['entry_ms'], start)


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

    def test_new_request_selects_one_coin_and_three_or_seven_days_only(self):
        with patch.object(self.manager, '_spawn') as spawn:
            result = self.manager.command(
                'start',
                days={'days':3, 'inst_id':'sol-usdt-swap'},
                token=self.manager.token,
            )
        spawn.assert_called_once()
        self.assertEqual(result['schema_version'], VERSION)
        self.assertEqual(result['inst_id'], 'SOL-USDT-SWAP')
        self.assertEqual(result['days'], 3)
        self.assertIn('SOL-USDT-SWAP', result['coins'])
        self.assertEqual(result['total'], 1)

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


if __name__ == '__main__':
    unittest.main()
