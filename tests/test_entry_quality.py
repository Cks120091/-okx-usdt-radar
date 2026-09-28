"""Regressions for the in-zone HUMA preflight deduction and scoring migration."""
import copy
import unittest
from dataclasses import replace

from radar.entry_quality import entry_location_quality
from radar.strategy import _entry_eligibility
from tests.test_signal_position_policy import preflight_signal, preflight
from tests.legacy_strategy_cases import valid_breakout_frames
from radar.models import Instrument, Ticker, MarketContext
from radar.strategy import AdaptiveStrategyEngine, StrategyConfig


class EntryQualityTests(unittest.TestCase):
    def test_scan_and_enrichment_use_zone_basis_instead_of_ema_score(self):
        frames = valid_breakout_frames()
        price = frames[-1][-1].close
        engine = AdaptiveStrategyEngine(StrategyConfig(min_quote_volume_24h=1_000_000))
        result = engine.analyze(Instrument("TEST-USDT-SWAP", "live", "USDT", "linear", .01),
                                Ticker("TEST-USDT-SWAP", price, price, price, 1), *frames)
        self.assertIsNotNone(result.signal)
        self.assertEqual(result.signal.execution_quality["score_components_version"], 2)
        scores = []
        for legacy_score in (0, 95):
            sample = copy.deepcopy(result)
            sample.assessment.execution_quality["entry_location"]["score"] = legacy_score
            enriched = engine.apply_market_context(sample, MarketContext(
                "TEST-USDT-SWAP", 20_000_000, .0001, .2, .62, 2), "LONG", [],
                {"score": 72., "label": "偏多"})
            self.assertIsNotNone(enriched.signal)
            quality = enriched.signal.execution_quality
            self.assertEqual(quality["score_components_version"], 2)
            self.assertEqual(quality["entry_location"]["basis"], "ENTRY_ZONE_V2")
            scores.append(quality["score_components"]["entry_location"])
        self.assertEqual(scores[0], scores[1])

    def test_history_flags_do_not_change_in_zone_quality_or_permission(self):
        for horizon in ("SHORT", "LONG"):
            for direction in ("LONG", "SHORT"):
                base = preflight_signal(direction, horizon)
                baseline = preflight(base, 100)
                for confirmed in (False, True):
                    signal = replace(base, lifecycle={**base.lifecycle, "entry_ready_once": True},
                                     entry_eligibility={**base.entry_eligibility,
                                                        "closed_retest_confirmed": confirmed})
                    before = copy.deepcopy(signal)
                    result = preflight(signal, 100)
                    self.assertEqual(result["live"]["quality_score"], baseline["live"]["quality_score"])
                    self.assertEqual(result["live"]["quality_components"]["entry_location"], 28.5)
                    self.assertEqual(result["live"]["quality_components_version"], 2)
                    self.assertEqual(result["verdict"]["new_entry_allowed"], baseline["verdict"]["new_entry_allowed"])
                    self.assertEqual(result["confirmation_assessment"]["pending"], not confirmed)
                    self.assertEqual(signal, before)

    def test_huma_in_zone_price_no_longer_gets_legacy_twelve_point_penalty(self):
        # Original HUMA zone, quote and plan. ATR does not affect in-zone distance.
        for atr in (.0005, .001, .01):
            eligibility = _entry_eligibility(
                direction="LONG", current_price=.02783, entry_low=.02771,
                entry_high=.02786, stop=.02463, target=.03807, atr=atr,
                stage="EARLY_SIGNAL", minimum_rr=1.8, ready_max_chase_atr=.15,
                missed_chase_atr=.5, entry_ready_once=True,
                closed_retest_confirmed=False, continuing_entry_window=False,
            )
            self.assertEqual(eligibility["status"], "WAIT_RETEST")
            self.assertEqual(entry_location_quality(eligibility)["score"], 95)

    def test_price_location_still_penalizes_outside_zone_in_both_directions(self):
        for direction in ("LONG", "SHORT"):
            sign = 1 if direction == "LONG" else -1
            scores = []
            for price in (100, 100 + sign * .4, 100 + sign * .8, 100 + sign * 2):
                eligibility = _entry_eligibility(
                    direction=direction, current_price=price, entry_low=99.8,
                    entry_high=100.2, stop=90 if sign == 1 else 110,
                    target=120 if sign == 1 else 80, atr=2, stage="CONFIRMED",
                    minimum_rr=1.8, ready_max_chase_atr=.15, missed_chase_atr=.5)
                scores.append(entry_location_quality(eligibility)["score"])
            self.assertEqual(scores[0], 95)
            self.assertTrue(all(a > b for a, b in zip(scores, scores[1:])))

    def test_legacy_components_are_not_explained_as_market_change(self):
        base = preflight_signal(horizon="LONG")
        current = preflight(base, 100)["live"]
        signal = replace(base, execution_quality={"score": current["quality_score"],
                         "score_components": current["quality_components"], "score_components_version": 1})
        result = preflight(signal, 100)
        self.assertEqual(result["quality_explanation"]["mode"], "CURRENT_ONLY")
        self.assertIn("舊評分方式", result["quality_explanation"]["note"])

    def test_terminal_plan_has_no_confirmation_reminder(self):
        signal = preflight_signal()
        signal = replace(signal, lifecycle={**signal.lifecycle, "entry_ready_once": True})
        for price in (89, 121):
            result = preflight(signal, price)
            self.assertFalse(result["verdict"]["new_entry_allowed"])
            self.assertFalse(result["confirmation_assessment"]["pending"])
            self.assertEqual(result["live"]["quality_components"]["entry_location"], 0)
