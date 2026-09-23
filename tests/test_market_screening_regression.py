import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from radar.scanner import MarketScanner, ScannerConfig
from radar.models import MarketContext, Ticker
from radar.strategy import AnalysisResult
from radar.public_payload import public_candidate_payload
from tests.legacy_scanner_cases import (
    FakeClient, candles, aligned_hourly_candles, qualified_signal, qualified_state,
)


class MarketScreeningRegressionTests(unittest.TestCase):
    def setUp(self):
        self.scanner = MarketScanner(FakeClient(), ScannerConfig(state_db_path=':memory:'))

    def test_directional_continuation_survives_without_new_cross(self):
        for sign in (1, -1):
            tf = SimpleNamespace(macd_line=sign * 2, macd_signal=sign,
                                 sma5=100 + sign * 3, sma10=100 + sign * 2, sma20=100)
            with self.subTest(sign=sign), patch('radar.scanner.features', return_value=tf):
                result = self.scanner._macd_ma_prefilter(candles())
                self.assertTrue(result['passed'])
                self.assertEqual(result['state'], 'CONTINUING')
                self.assertEqual(result['macd_cross'], 'NONE')

    def test_opposed_ma_and_macd_still_rejected(self):
        tf = SimpleNamespace(macd_line=-2, macd_signal=-1, sma5=103, sma10=102, sma20=100)
        with patch('radar.scanner.features', return_value=tf):
            self.assertFalse(self.scanner._macd_ma_prefilter(candles())['passed'])

    def test_market_population_independent_of_candidate_population(self):
        bundle = {'15m': candles(), '1H': aligned_hourly_candles()}
        bundles = {key: bundle for key in ('BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'AAA-USDT-SWAP')}
        tickers = {key: Ticker(key, 110, 109.99, 110.01, 1, 3_000_000) for key in bundles}
        bias = self.scanner._calculate_short_market_bias(bundles, tickers, {})
        self.assertEqual(bias['market_average_rsi_sample_count'], 3)
        self.assertEqual(bias['market_rsi_24h_sample_count'], 3)
        self.assertIsNotNone(bias['btc']['core_rsi'])
        self.assertEqual(bias['resonance']['formal_count'], 0)

    def test_resonance_survives_deep_context_and_public_payload(self):
        signal = qualified_signal()
        state = qualified_state(signal)
        result = AnalysisResult(signal=signal, reason='', market_state=state, candidate_signal=signal)
        bias = {'score': 80, 'resonance': {'active': True, 'direction': 'LONG', 'formal_count': 8}}
        result = self.scanner._apply_market_resonance(result, bias)
        context = MarketContext(signal.inst_id, None, None, None, None, 1)
        result = self.scanner._apply_professional_context(result, context, None, bias, 100)
        for item, is_signal in ((result.signal, True), (result.market_state, False)):
            meta = public_candidate_payload(item, signal=is_signal)['market_metrics']['market_resonance']
            self.assertEqual(meta['state'], 'ALIGNED')
            self.assertEqual(meta['priority'], 2)
            self.assertFalse(meta['affects_trigger'])
        self.assertEqual(result.signal.entry_low, signal.entry_low)
        self.assertEqual(result.signal.stop_loss, signal.stop_loss)
        self.assertEqual(result.signal.direction, signal.direction)

    def test_missing_market_data_is_unknown_not_neutral(self):
        meta = self.scanner._market_resonance_meta('LONG', {})
        self.assertEqual(meta['state'], 'UNKNOWN')
        self.assertEqual(meta['priority'], 0)

    def test_no_candidates_is_not_a_data_failure(self):
        with patch.object(self.scanner, '_macd_ma_prefilter', return_value={'passed': False}):
            report = self.scanner.scan_once(scan_mode='SHORT')
        self.assertNotEqual(report.status, 'DATA_INCOMPLETE')
        self.assertGreater(report.market_bias['market_average_rsi_sample_count'], 0)
        self.assertEqual(report.signals, [])

    def test_active_episode_is_reanalyzed_even_when_prefilter_rejects(self):
        with patch.object(self.scanner, '_macd_ma_prefilter', return_value={'passed': False}), \
             patch.object(self.scanner.repository, 'load_active_signal', return_value=qualified_signal()), \
             patch.object(self.scanner, '_analyze_short_v33', wraps=self.scanner._analyze_short_v33) as analyze:
            report = self.scanner.scan_once(scan_mode='SHORT')
        self.assertGreater(analyze.call_count, 0)
        self.assertEqual(analyze.call_count, report.fetched_count)
