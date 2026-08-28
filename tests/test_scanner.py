import unittest
from dataclasses import replace

from radar.models import Candle, Instrument, MarketContext, MarketState, Signal, Ticker
from radar.scanner import MarketScanner, ScannerConfig, _compact_market_map_state
from radar.strategy import AnalysisResult


def candles(count=100, micro_anomaly=False):
    return [
        Candle(
            index,
            100 + index * 0.1 - (0.05 if micro_anomaly and index >= count - 12 else 0),
            101 + index * 0.1,
            99 + index * 0.1,
            100 + index * 0.1,
            10,
            1_000_000 * (2 if micro_anomaly and index == count - 1 else 1),
            True,
        )
        for index in range(count)
    ]


class FakeClient:
    def __init__(self, fail_id=None):
        self.fail_id = fail_id
        self.candle_requests = []
        self.instruments = [
            Instrument("AAA-USDT-SWAP", "live", "USDT", "linear", 0.01),
            Instrument("BBB-USDT-SWAP", "live", "USDT", "linear", 0.01),
        ]

    def get_usdt_swap_instruments(self):
        return self.instruments

    def get_swap_tickers(self):
        return {
            item.inst_id: Ticker(item.inst_id, 110, 109.99, 110.01, 1)
            for item in self.instruments
        }

    def get_candles(self, inst_id, bar, limit=100):
        self.candle_requests.append((inst_id, bar, limit))
        if inst_id == self.fail_id and bar == "1H":
            raise RuntimeError("fixture failure")
        return candles(limit)


class ScannerMarketPulseTests(unittest.TestCase):
    def test_oi_and_price_are_classified_by_participation_type(self):
        classify = MarketScanner._classify_oi_flow
        self.assertEqual(classify(1.2, 0.8), "LONG_BUILD")
        self.assertEqual(classify(1.2, -0.8), "SHORT_BUILD")
        self.assertEqual(classify(-1.2, 0.8), "SHORT_COVER")
        self.assertEqual(classify(-1.2, -0.8), "LONG_EXIT")
        self.assertEqual(classify(0.1, 0.8), "STABLE")
        self.assertIsNone(classify(None, 0.8))


class ManyFakeClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.instruments = [
            Instrument(f"T{index:02d}-USDT-SWAP", "live", "USDT", "linear", 0.01)
            for index in range(25)
        ]


class ContextFakeClient(FakeClient):
    def get_open_interest_usd(self):
        return {item.inst_id: 5_000_000 for item in self.instruments}

    def get_market_context(self, inst_id, open_interest_usd=None):
        return MarketContext(inst_id, open_interest_usd, 0.0001, 0.12, 0.56, 1)

    def get_candles(self, inst_id, bar, limit=100):
        self.candle_requests.append((inst_id, bar, limit))
        return candles(limit, micro_anomaly=bar == "5m")


class LowOpenInterestClient(ContextFakeClient):
    def get_open_interest_usd(self):
        return {item.inst_id: 500_000 for item in self.instruments}


class FailedOpenInterestClient(ContextFakeClient):
    def get_open_interest_usd(self):
        raise RuntimeError("fixture OI failure")


class LongHistoryGapClient(FakeClient):
    def get_candles(self, inst_id, bar, limit=100):
        self.candle_requests.append((inst_id, bar, limit))
        if inst_id == "BBB-USDT-SWAP" and bar == "1D":
            return candles(59)
        return candles(limit)


class AlwaysSignalEngine:
    def analyze(self, instrument, ticker, candles_4h, candles_1h, candles_15m, candles_5m=None):
        rank = instrument.inst_id[1:3]
        score = float(rank) if rank.isdigit() else 50.0
        return AnalysisResult(
            Signal(
                inst_id=instrument.inst_id,
                direction="LONG",
                strategy="fixture",
                score=score,
                evidence=["a", "b"],
                entry_low="1",
                entry_high="1",
                stop_loss="0.9",
                take_profit_1="1.2",
                take_profit_2="1.3",
                risk_reward=2.0,
                invalidation="fixture",
                spread_pct=0.01,
                quote_volume_24h=1_000_000,
                closed_candle_ts=1,
                regime="fixture",
            ),
            "qualified",
        )


class LowReadinessContextEngine:
    def analyze(self, instrument, ticker, candles_4h, candles_1h, candles_15m, candles_5m=None):
        return AnalysisResult(
            None,
            "fixture",
            MarketState(
                inst_id=instrument.inst_id,
                regime="DISORDER",
                direction="NEUTRAL",
                preferred_strategy="等待",
                readiness_score=0.0,
                status="WATCH",
                missing_conditions=["等待方向清楚"],
                spread_pct=0.01,
                quote_volume_24h=20_000_000,
                closed_candle_ts=1,
            ),
        )

    def apply_market_context(
        self,
        result,
        context,
        btc_bias="NEUTRAL",
        candles_5m=None,
        market_bias=None,
    ):
        return result


class ScannerTests(unittest.TestCase):
    def test_single_reanalysis_uses_latest_multiframe_data_and_rejects_missed_plan(self):
        previous = Signal(
            inst_id="AAA-USDT-SWAP",
            direction="LONG",
            strategy="fixture",
            score=80.0,
            evidence=[],
            entry_low="100",
            entry_high="102",
            stop_loss="98",
            take_profit_1="108",
            take_profit_2="112",
            risk_reward=2.0,
            invalidation="fixture",
            spread_pct=0.01,
            quote_volume_24h=20_000_000,
            closed_candle_ts=1_000,
            regime="TREND",
            radar_horizon="SHORT",
            trigger_id="old-id",
            lifecycle={"event_key": "old-event"},
            market_story={
                "trigger": {"event_ts": 1_000, "trigger_event_key": "old-event"}
            },
            data_timestamp=1_000,
        )
        candidate = replace(
            previous,
            trigger_id="",
            closed_candle_ts=2_000,
            lifecycle={},
            market_story={
                "raw": {"core_atr": 2.0},
                "trigger": {
                    "event_atr": 2.0,
                    "event_ts": 2_000,
                    "trigger_event_key": "new-event",
                },
            },
            market_metrics={"last_price": 101.0},
            data_timestamp=2_000,
        )
        state = MarketState(
            inst_id="AAA-USDT-SWAP",
            regime="TREND",
            direction="LONG",
            preferred_strategy="fixture",
            readiness_score=80.0,
            status="CONFIRMED",
            missing_conditions=[],
            spread_pct=0.01,
            quote_volume_24h=20_000_000,
            closed_candle_ts=2_000,
            market_metrics={"last_price": 101.0, "price_change_core_pct": 0.5},
            radar_horizon="SHORT",
        )

        class ReanalysisClient(ContextFakeClient):
            price = 101.0

            def get_ticker(self, inst_id):
                return Ticker(inst_id, self.price, self.price - 0.01, self.price + 0.01, 2_000)

        class ReanalysisEngine:
            def analyze(self, *args, previous_story=None):
                return AnalysisResult(candidate, "qualified", state)

            def apply_market_context(self, result, *args):
                return result

        client = ReanalysisClient()
        scanner = MarketScanner(
            client,
            ScannerConfig(min_quote_volume_24h=0, universe_max_spread_pct=1.0),
        )
        scanner.engine = ReanalysisEngine()

        ready = scanner.reanalyze_instrument(previous)

        self.assertIsNotNone(ready.raw_signal)
        self.assertEqual(ready.raw_signal.entry_eligibility["status"], "ENTRY_READY")
        self.assertEqual(
            {bar for inst_id, bar, _ in client.candle_requests if inst_id == previous.inst_id},
            {"4H", "1H", "15m", "5m"},
        )

        client.price = 109.0
        missed = scanner.reanalyze_instrument(previous)

        self.assertIsNone(missed.raw_signal)
        self.assertEqual(missed.reason, "new_trigger_not_an_entry_opportunity")

    def test_single_reanalysis_never_repackages_the_same_trigger_event(self):
        previous = Signal(
            inst_id="AAA-USDT-SWAP",
            direction="LONG",
            strategy="fixture",
            score=80.0,
            evidence=[],
            entry_low="100",
            entry_high="101",
            stop_loss="98",
            take_profit_1="105",
            take_profit_2="108",
            risk_reward=2.0,
            invalidation="fixture",
            spread_pct=0.01,
            quote_volume_24h=20_000_000,
            closed_candle_ts=1_000,
            regime="TREND",
            radar_horizon="SHORT",
            trigger_id="old-id",
            lifecycle={"event_key": "same-event"},
            market_story={
                "trigger": {"event_ts": 1_000, "trigger_event_key": "same-event"}
            },
            data_timestamp=1_000,
        )
        same = replace(
            previous,
            trigger_id="",
            entry_low="101",
            entry_high="102",
        )
        newer = replace(
            same,
            lifecycle={"event_key": "new-event"},
            market_story={
                "trigger": {"event_ts": 2_000, "trigger_event_key": "new-event"}
            },
            data_timestamp=2_000,
        )
        older_with_different_key = replace(
            same,
            lifecycle={"event_key": "older-event"},
            market_story={
                "trigger": {"event_ts": 900, "trigger_event_key": "older-event"}
            },
            data_timestamp=900,
        )

        self.assertFalse(MarketScanner._is_new_trigger_event(previous, same))
        self.assertFalse(
            MarketScanner._is_new_trigger_event(previous, older_with_different_key)
        )
        self.assertTrue(MarketScanner._is_new_trigger_event(previous, newer))

    def test_single_reanalysis_live_stop_check_is_directional(self):
        long_signal = Signal(
            inst_id="AAA-USDT-SWAP",
            direction="LONG",
            strategy="fixture",
            score=80.0,
            evidence=[],
            entry_low="100",
            entry_high="101",
            stop_loss="98",
            take_profit_1="105",
            take_profit_2="108",
            risk_reward=2.0,
            invalidation="fixture",
            spread_pct=0.01,
            quote_volume_24h=20_000_000,
            closed_candle_ts=1_000,
            regime="TREND",
        )
        short_signal = replace(long_signal, direction="SHORT", stop_loss="102")

        self.assertTrue(MarketScanner._plan_crossed_live_stop(long_signal, 97.9))
        self.assertFalse(MarketScanner._plan_crossed_live_stop(long_signal, 98.1))
        self.assertTrue(MarketScanner._plan_crossed_live_stop(short_signal, 102.1))
        self.assertFalse(MarketScanner._plan_crossed_live_stop(short_signal, 101.9))

    def test_market_map_projection_drops_heavy_analysis_payloads(self):
        state = MarketState(
            inst_id="AAA-USDT-SWAP",
            regime="TREND",
            direction="LONG",
            preferred_strategy="fixture",
            readiness_score=75.0,
            status="WATCH",
            missing_conditions=["等待突破"],
            spread_pct=0.01,
            quote_volume_24h=10_000_000,
            closed_candle_ts=1,
            market_metrics={
                "last_price": 100.0,
                "price_change_1h_pct": 1.2,
                "open_interest_usd": 5_000_000,
                "raw_indicators": {"oversized": "x" * 20_000},
                "order_book_sequence": {"oversized": "x" * 20_000},
            },
            data_quality={"core": "AVAILABLE", "missing_sources": []},
            market_story={"oversized": "x" * 20_000},
            trigger={"oversized": "x" * 20_000},
            timeframe_states={"15m": {"oversized": "x" * 20_000}},
        )

        compact = _compact_market_map_state(state)

        self.assertEqual(compact.market_metrics["last_price"], 100.0)
        self.assertEqual(compact.market_metrics["open_interest_usd"], 5_000_000)
        self.assertNotIn("raw_indicators", compact.market_metrics)
        self.assertNotIn("order_book_sequence", compact.market_metrics)
        self.assertEqual(compact.market_story, {})
        self.assertEqual(compact.trigger, {})
        self.assertEqual(compact.timeframe_states, {})
        self.assertEqual(compact.data_quality["core"], "AVAILABLE")

    def test_release_transient_data_clears_candle_reuse_cache(self):
        scanner = MarketScanner(FakeClient(), ScannerConfig(workers=2))
        scanner._candle_cache[("AAA-USDT-SWAP", "1H")] = candles(60)

        self.assertEqual(scanner.release_transient_data(), 1)
        self.assertEqual(scanner._candle_cache, {})

    def test_one_symbol_failure_is_isolated_and_surviving_signal_remains(self):
        scanner = MarketScanner(
            FakeClient("BBB-USDT-SWAP"),
            ScannerConfig(workers=2, min_quote_volume_24h=0),
        )
        scanner.engine = AlwaysSignalEngine()
        report = scanner.scan_once()
        self.assertEqual(report.status, "PARTIAL_DATA")
        self.assertLess(report.coverage_pct, 100)
        self.assertEqual([item.inst_id for item in report.signals], ["AAA-USDT-SWAP"])
        self.assertIn("BBB-USDT-SWAP", report.failed_instruments)
        self.assertEqual(report.data_quality["core_failed_count"], 1)
        self.assertEqual(report.data_quality["long_failed_count"], 0)

    def test_long_history_gap_does_not_masquerade_as_core_failure(self):
        report = MarketScanner(
            LongHistoryGapClient(),
            ScannerConfig(workers=2, min_quote_volume_24h=0),
        ).scan_once()

        self.assertEqual(report.status, "PARTIAL_DATA")
        self.assertEqual(report.coverage_pct, 100.0)
        self.assertEqual(report.data_quality["core_status"], "AVAILABLE")
        self.assertEqual(report.data_quality["core_failed_count"], 0)
        self.assertEqual(report.data_quality["long_status"], "PARTIAL")
        self.assertEqual(report.data_quality["long_failed_count"], 1)
        self.assertIn("BBB-USDT-SWAP:LONG", report.failed_instruments)
        self.assertIn("不影響其短線判定", report.message)

    def test_core_preview_is_emitted_before_final_deep_report(self):
        previews = []
        request_snapshots = []
        client = ContextFakeClient()
        scanner = MarketScanner(
            client,
            ScannerConfig(workers=2, min_quote_volume_24h=0),
        )

        def publish(report):
            previews.append(report)
            request_snapshots.append(list(client.candle_requests))

        final = scanner.scan_once(preview=publish)

        self.assertEqual(len(previews), 1)
        self.assertEqual(previews[0].runtime_status, "CORE_PREVIEW")
        self.assertEqual(previews[0].data_quality["deep_status"], "PENDING")
        self.assertEqual(previews[0].long_signals, [])
        self.assertEqual(final.runtime_status, "FRESH")
        self.assertFalse(
            any(bar == "1D" for _, bar, _ in request_snapshots[0])
        )
        self.assertTrue(any(bar == "1D" for _, bar, _ in client.candle_requests))

    def test_unchanged_higher_timeframes_are_reused_between_scans(self):
        client = FakeClient()
        scanner = MarketScanner(
            client,
            ScannerConfig(workers=2, min_quote_volume_24h=0),
        )
        scanner._cache_covers_current_bar = lambda candles, bar: True

        scanner.scan_once()
        scanner.scan_once()

        counts = {
            bar: sum(requested_bar == bar for _, requested_bar, _ in client.candle_requests)
            for bar in ("1D", "4H", "1H", "15m")
        }
        self.assertEqual(counts["15m"], 4)
        self.assertEqual(counts["1H"], 2)
        self.assertEqual(counts["4H"], 2)
        self.assertEqual(counts["1D"], 2)

    def test_full_fetch_reports_one_hundred_percent_coverage(self):
        client = FakeClient()
        report = MarketScanner(
            client,
            ScannerConfig(
                workers=2,
                min_open_interest_usd=0,
                require_micro_volume_anomaly=False,
            ),
        ).scan_once()
        self.assertEqual(report.coverage_pct, 100)
        self.assertEqual(report.target_count, 2)
        self.assertEqual(report.fetched_count, 2)
        self.assertNotEqual(report.status, "DATA_INCOMPLETE")
        self.assertEqual(len(report.market_map), 2)
        self.assertEqual(sum(report.market_regime_counts.values()), 2)
        self.assertTrue(report.watchlist)
        self.assertTrue(report.watchlist[0].missing_conditions)
        self.assertTrue(
            all(
                not any(key.startswith("_") for key in item.market_metrics)
                for item in report.market_map
            )
        )
        requested = {(bar, limit) for _, bar, limit in client.candle_requests}
        self.assertEqual(
            requested,
            {("1D", 200), ("4H", 200), ("1H", 240), ("15m", 200)},
        )

    def test_output_has_hard_limit_of_twenty_and_is_quality_sorted(self):
        scanner = MarketScanner(
            ManyFakeClient(),
            ScannerConfig(
                workers=3,
                max_signals=99,
                min_quote_volume_24h=0,
                max_spread_pct=1,
                min_open_interest_usd=0,
                require_micro_volume_anomaly=False,
            ),
        )
        scanner.engine = AlwaysSignalEngine()
        report = scanner.scan_once()
        self.assertEqual(len(report.signals), 20)
        self.assertEqual(report.signals[0].inst_id, "T24-USDT-SWAP")
        self.assertEqual(report.signals[-1].inst_id, "T05-USDT-SWAP")

    def test_top_candidates_receive_public_market_context(self):
        client = ContextFakeClient()
        report = MarketScanner(
            client,
            ScannerConfig(
                workers=2,
                previous_open_interest_usd={
                    "AAA-USDT-SWAP": 4_000_000,
                    "BBB-USDT-SWAP": 4_000_000,
                },
            ),
        ).scan_once()
        self.assertEqual(report.context_target_count, 2)
        self.assertEqual(report.context_enriched_count, 2)
        self.assertEqual(report.context_failures, {})
        self.assertEqual(report.data_quality["deep_completeness_pct"], 100.0)
        self.assertTrue(report.watchlist[0].market_metrics["context_complete"])
        self.assertGreater(
            report.watchlist[0].market_metrics["micro_acceleration_5m"],
            50,
        )
        self.assertNotEqual(
            report.watchlist[0].timeframe_states["5m"]["label"],
            "資料暫缺",
        )
        self.assertEqual(
            report.watchlist[0].market_metrics["open_interest_change_pct"],
            25.0,
        )
        self.assertIn(report.market_bias["label"], ("偏多", "中性", "偏空"))
        self.assertIn(("AAA-USDT-SWAP", "5m", 120), client.candle_requests)
        self.assertIn(("BBB-USDT-SWAP", "5m", 120), client.candle_requests)

    def test_context_coverage_is_not_limited_to_near_trigger_candidates(self):
        client = ContextFakeClient()
        scanner = MarketScanner(
            client,
            ScannerConfig(workers=2, context_candidates=999),
        )
        scanner.engine = LowReadinessContextEngine()
        report = scanner.scan_once()
        self.assertEqual(report.context_target_count, 2)
        self.assertEqual(report.context_enriched_count, 2)
        self.assertEqual(report.data_quality["deep_candidate_limit"], 100)
        self.assertIn(("AAA-USDT-SWAP", "5m", 120), client.candle_requests)
        self.assertIn(("BBB-USDT-SWAP", "5m", 120), client.candle_requests)

    def test_low_open_interest_is_visible_context_not_a_universe_gate(self):
        report = MarketScanner(LowOpenInterestClient(), ScannerConfig(workers=2)).scan_once()
        self.assertEqual(report.context_target_count, 2)
        self.assertEqual(len(report.watchlist), 2)
        self.assertTrue(all(item.status != "FILTERED" for item in report.market_map))
        self.assertTrue(
            all(
                item.market_metrics["open_interest_usd"] == 500_000
                for item in report.market_map
            )
        )

    def test_open_interest_endpoint_failure_is_nonfatal_and_explicit(self):
        report = MarketScanner(
            FailedOpenInterestClient(),
            ScannerConfig(workers=2),
        ).scan_once()
        self.assertNotEqual(report.status, "DATA_INCOMPLETE")
        self.assertTrue(report.actionable)
        self.assertEqual(report.signals, [])
        self.assertIn("_OPEN_INTEREST_", report.context_failures)
        self.assertTrue(
            all(
                "open_interest" in item.data_quality["missing_sources"]
                for item in report.market_map
            )
        )

    def test_market_bias_turns_bullish_when_breadth_and_anchors_align(self):
        scanner = MarketScanner(FakeClient())

        def bullish(inst_id):
            return AnalysisResult(
                None,
                "fixture",
                MarketState(
                    inst_id=inst_id,
                    regime="TREND",
                    direction="LONG",
                    preferred_strategy="趨勢回踩續行",
                    readiness_score=80.0,
                    status="WATCH",
                    missing_conditions=[],
                    spread_pct=0.01,
                    quote_volume_24h=20_000_000,
                    closed_candle_ts=1,
                ),
            )

        bias = scanner._calculate_market_bias(
            {
                "BTC-USDT-SWAP": bullish("BTC-USDT-SWAP"),
                "ETH-USDT-SWAP": bullish("ETH-USDT-SWAP"),
                "AAA-USDT-SWAP": bullish("AAA-USDT-SWAP"),
            }
        )
        self.assertEqual(bias["label"], "偏多")
        self.assertGreaterEqual(bias["score"], 65.0)
        self.assertEqual(bias["market_breadth_long_pct"], 100.0)


if __name__ == "__main__":
    unittest.main()
