import unittest
from dataclasses import replace
from unittest.mock import patch

from radar.market_scope import XAU_INST_ID
from radar.models import Instrument, MarketContext, Ticker
from radar.public_payload import public_candidate_payload
from radar.scanner import MarketScanner, ScannerConfig
from radar.strategy import AnalysisResult
from tests.legacy_scanner_cases import (
    ContextFakeClient,
    FakeClient,
    aligned_hourly_candles,
    candles,
    qualified_signal,
    qualified_state,
)


def market_result(inst_id, *, direction="LONG", rsi=60.0):
    signal = replace(qualified_signal(inst_id), direction=direction)
    state = qualified_state(signal)
    state = replace(
        state,
        market_metrics={
            **state.market_metrics,
            "rsi_core": rsi,
            "rsi_24h": rsi,
            "price_change_core_pct": 2.0 if direction == "LONG" else -2.0,
        },
    )
    return AnalysisResult(
        signal=signal, reason="", market_state=state, candidate_signal=signal
    )


class XauMarketScopeTests(unittest.TestCase):
    def setUp(self):
        self.scanner = MarketScanner(FakeClient(), ScannerConfig())
        self.addCleanup(self.scanner.repository.close)

    def test_gold_does_not_change_crypto_rsi_breadth_resonance_or_exposure(self):
        crypto = {
            inst_id: market_result(inst_id)
            for inst_id in (
                "BTC-USDT-SWAP", "ETH-USDT-SWAP", "AAA-USDT-SWAP", "BBB-USDT-SWAP"
            )
        }
        expected = self.scanner._calculate_market_bias(crypto)
        self.assertEqual(expected["resonance"]["formal_count"], 4)
        self.assertFalse(expected["resonance"]["active"])
        for direction, rsi in (("LONG", 100.0), ("SHORT", 0.0)):
            with self.subTest(direction=direction):
                mixed = {**crypto, XAU_INST_ID: market_result(XAU_INST_ID, direction=direction, rsi=rsi)}
                self.assertEqual(self.scanner._calculate_market_bias(mixed), expected)
                self.assertIn(XAU_INST_ID, mixed)

    def test_short_population_excludes_gold_without_losing_crypto_samples(self):
        bundle = {"15m": candles(), "1H": aligned_hourly_candles()}
        bundles = {key: bundle for key in ("BTC-USDT-SWAP", "ETH-USDT-SWAP")}
        tickers = {key: Ticker(key, 110, 109.99, 110.01, 1, 20_000_000) for key in bundles}
        expected = self.scanner._calculate_short_market_bias(bundles, tickers, {})
        mixed = {**bundles, XAU_INST_ID: bundle}
        mixed_tickers = {**tickers, XAU_INST_ID: Ticker(XAU_INST_ID, 4000, 3999.9, 4000.1, 1, 1_000_000_000)}
        actual = self.scanner._calculate_short_market_bias(
            mixed, mixed_tickers, {XAU_INST_ID: market_result(XAU_INST_ID)}
        )
        self.assertEqual(actual, expected)
        self.assertEqual(actual["market_rsi_24h_sample_count"], 2)

    def test_gold_only_does_not_claim_neutral_crypto_market(self):
        bundle = {"15m": candles(), "1H": aligned_hourly_candles()}
        for bias in (
            self.scanner._calculate_market_bias({XAU_INST_ID: market_result(XAU_INST_ID)}),
            self.scanner._calculate_short_market_bias({XAU_INST_ID: bundle}, {}, {}),
        ):
            with self.subTest(bias=bias):
                self.assertIsNone(bias["score"])
                self.assertEqual(bias["label"], "資料不足")
                self.assertIsNone(bias["market_average_rsi"])
                self.assertIsNone(bias["market_rsi_24h"])
                self.assertEqual(bias["market_average_rsi_sample_count"], 0)
                self.assertEqual(bias["market_rsi_24h_sample_count"], 0)
                self.assertEqual(bias["resonance"]["formal_count"], 0)

    def test_gold_resonance_is_not_applicable_and_plan_is_unchanged(self):
        original = market_result(XAU_INST_ID)
        bias = {
            "score": 90,
            "btc": {"core_change_pct": 2.0},
            "market_breadth_long_pct": 90,
            "resonance": {"active": True, "direction": "LONG", "formal_count": 8},
        }
        result = self.scanner._apply_market_resonance(original, bias)
        result = self.scanner._apply_professional_context(
            result, MarketContext(XAU_INST_ID, None, None, None, None, 1), None, bias, 100
        )
        for item, is_signal in ((result.signal, True), (result.market_state, False)):
            meta = public_candidate_payload(item, signal=is_signal)["market_metrics"]["market_resonance"]
            self.assertEqual(meta["state"], "NOT_APPLICABLE")
            self.assertEqual(meta["priority"], 0)
            self.assertIsNone(meta["market_bias_score"])
            self.assertFalse(meta["affects_trigger"])
            driver = item.market_metrics["market_driver"]
            self.assertEqual(driver["key"], "UNKNOWN")
            self.assertEqual(driver["label"], "黃金獨立觀察")
            self.assertIsNone(driver["relative_strength_pct"])
        for key in ("direction", "entry_low", "entry_high", "stop_loss", "take_profit_1", "trigger_id"):
            self.assertEqual(getattr(result.signal, key), getattr(original.signal, key))
        self.assertNotIn("market_resonance", original.signal.market_metrics)

    def test_full_scan_keeps_gold_in_both_horizons_and_market_maps(self):
        client = ContextFakeClient()
        client.instruments.append(Instrument(XAU_INST_ID, "live", "USDT", "linear", 0.1))
        scanner = MarketScanner(client, ScannerConfig(workers=2))
        self.addCleanup(scanner.repository.close)
        with patch.object(scanner.engine, "apply_market_context", wraps=scanner.engine.apply_market_context) as apply_context:
            report = scanner.scan_once(scan_mode="FULL")
        self.assertIn(XAU_INST_ID, report.target_instruments)
        self.assertIn(XAU_INST_ID, {item.inst_id for item in report.market_map})
        self.assertIn(XAU_INST_ID, {item.inst_id for item in report.long_market_map})
        self.assertIn(XAU_INST_ID, {item.inst_id for item in report.watchlist})
        self.assertIn(XAU_INST_ID, {item.inst_id for item in report.long_watchlist})
        self.assertIn("僅限 XAU-USDT-SWAP 黃金例外", report.scope)
        gold_calls = [call for call in apply_context.call_args_list if call.args[0].market_state.inst_id == XAU_INST_ID]
        self.assertEqual(len(gold_calls), 2)
        for call in gold_calls:
            self.assertEqual(call.args[2], "NEUTRAL")
            self.assertEqual(call.args[4], {})
        self.assertEqual(report.market_bias["market_average_rsi_sample_count"], 2)
        self.assertEqual(report.long_market_bias["market_average_rsi_sample_count"], 2)

    def test_single_gold_scan_ignores_crypto_bias_in_both_horizons(self):
        client = ContextFakeClient()
        client.instruments.append(Instrument(XAU_INST_ID, "live", "USDT", "linear", 0.1))
        scanner = MarketScanner(client, ScannerConfig(workers=2))
        self.addCleanup(scanner.repository.close)
        with patch.object(scanner.engine, "apply_market_context", wraps=scanner.engine.apply_market_context) as apply_context:
            analysis = scanner.scan_instrument(
                XAU_INST_ID,
                market_bias={"score": 95},
                long_market_bias={"score": 5},
                btc_bias="LONG",
                long_btc_bias="SHORT",
                requested_horizon="BOTH",
            )
        self.assertIsNotNone(analysis.short_result.market_state)
        self.assertIsNotNone(analysis.long_result.market_state)
        self.assertEqual(apply_context.call_count, 2)
        for call in apply_context.call_args_list:
            self.assertEqual(call.args[2], "NEUTRAL")
            self.assertEqual(call.args[4], {})


if __name__ == "__main__":
    unittest.main()
