import unittest

from radar.continuation import summarize_continuation_samples


def observer_samples(
    count,
    *,
    step_ms=60_000,
    oi_values=None,
    prices=None,
    buy=70.0,
    sell=30.0,
    coverage="COMPLETE",
    quote_volume=150.0,
    baseline=100.0,
):
    oi_values = oi_values or [1_000.0 + index * 2.0 for index in range(count)]
    prices = prices or [100.0 + index * 0.05 for index in range(count)]
    output = []
    for index in range(count):
        bucket_end_ms = 1_800_000_000_000 + index * step_ms
        observed_at_ms = bucket_end_ms + 3_000
        output.append(
            {
                "observed_at_ms": observed_at_ms,
                "bucket_start_ms": bucket_end_ms - step_ms,
                "bucket_end_ms": bucket_end_ms,
                "open_interest_contracts": oi_values[index],
                # Deliberately unrelated: USD notional must never drive OI trend.
                "open_interest_usd": 5_000_000.0 + index * 100_000.0,
                "price": prices[index],
                "trades_coverage": "BASELINE" if index == 0 else coverage,
                "taker_buy_volume": 0.0 if index == 0 else buy,
                "taker_sell_volume": 0.0 if index == 0 else sell,
                "cvd": 0.0 if index == 0 else buy - sell,
                "candle_ts": bucket_end_ms - step_ms,
                "candle_close_ts": bucket_end_ms,
                "quote_volume": quote_volume,
                "volume_baseline": baseline,
                "source_timestamps": {
                    "open_interest": bucket_end_ms + 2_000,
                },
            }
        )
    return output


class ContinuationAverageTests(unittest.TestCase):
    def test_short_requires_ten_real_one_minute_buckets(self):
        collecting = summarize_continuation_samples(
            observer_samples(10),
            "SHORT",
            "LONG",
        )
        ready = summarize_continuation_samples(
            observer_samples(11),
            "SHORT",
            "LONG",
        )

        self.assertEqual(collecting["bucket_count"], 9)
        self.assertFalse(collecting["windows"]["10m"]["ready"])
        self.assertEqual(collecting["status"], "EARLY_READY")
        self.assertEqual(ready["bucket_count"], 10)
        self.assertTrue(ready["windows"]["10m"]["ready"])
        self.assertEqual(ready["selected_window"], "10m")
        self.assertEqual(ready["status"], "READY")

    def test_seconds_apart_cannot_masquerade_as_ten_minutes(self):
        result = summarize_continuation_samples(
            observer_samples(11, step_ms=1_000),
            "SHORT",
            "LONG",
        )

        self.assertEqual(result["status"], "COLLECTING")
        self.assertFalse(result["windows"]["10m"]["ready"])
        self.assertLess(result["bucket_count"], 10)

    def test_ten_samples_polled_too_early_are_not_called_ten_minutes(self):
        result = summarize_continuation_samples(
            observer_samples(11, step_ms=54_000),
            "SHORT",
            "LONG",
        )

        self.assertFalse(result["windows"]["10m"]["ready"])
        self.assertEqual(result["bucket_count"], 0)

    def test_gap_discards_old_tail_but_can_recover_with_a_fresh_full_window(self):
        old = observer_samples(3)
        fresh = observer_samples(11)
        for sample in fresh:
            sample["observed_at_ms"] += 3_600_000
            sample["candle_ts"] += 3_600_000
            sample["candle_close_ts"] += 3_600_000
            sample["bucket_start_ms"] += 3_600_000
            sample["bucket_end_ms"] += 3_600_000
            sample["source_timestamps"]["open_interest"] += 3_600_000

        result = summarize_continuation_samples([*old, *fresh], "SHORT", "LONG")

        self.assertTrue(result["continuity_reset"])
        self.assertEqual(result["bucket_count"], 10)
        self.assertTrue(result["windows"]["10m"]["ready"])
        self.assertEqual(result["status"], "READY")

    def test_oi_usd_rise_is_ignored_when_contracts_are_flat(self):
        samples = observer_samples(11, oi_values=[1_000.0] * 11)
        result = summarize_continuation_samples(samples, "SHORT", "LONG")
        oi = result["windows"]["10m"]["domains"]["OI"]

        self.assertEqual(oi["state"], "NEUTRAL")
        self.assertAlmostEqual(oi["change_pct"], 0.0)
        self.assertNotIn("新增且價格同向", oi["reason"])

    def test_one_last_oi_spike_does_not_flip_average_persistence(self):
        levels = [1_000.0] * 10 + [1_020.0]
        result = summarize_continuation_samples(
            observer_samples(11, oi_values=levels),
            "SHORT",
            "LONG",
        )
        oi = result["windows"]["10m"]["domains"]["OI"]

        self.assertEqual(oi["state"], "NEUTRAL")
        self.assertLess(oi["persistence_pct"], 60.0)

    def test_oi_units_are_never_mixed_within_one_window(self):
        samples = observer_samples(11)
        for sample in samples:
            sample["open_interest_ccy"] = 10.0
        samples[5]["open_interest_contracts"] = None

        result = summarize_continuation_samples(samples, "SHORT", "LONG")
        oi = result["windows"]["10m"]["domains"]["OI"]

        self.assertEqual(oi["state"], "NEUTRAL")
        self.assertAlmostEqual(oi["change_pct"], 0.0)
        self.assertIn("標的幣數量", oi["detail"])

    def test_volume_weighted_taker_with_no_price_response_is_absorption(self):
        result = summarize_continuation_samples(
            observer_samples(11, prices=[100.0] * 11, buy=72.0, sell=28.0),
            "SHORT",
            "LONG",
        )
        taker = result["windows"]["10m"]["domains"]["TAKER_CVD"]

        self.assertEqual(taker["state"], "CONFLICT")
        self.assertTrue(taker["severe"])
        self.assertIn("吸收", taker["reason"])

    def test_partial_trade_bucket_stays_unknown(self):
        samples = observer_samples(11)
        samples[6]["trades_coverage"] = "PARTIAL"
        result = summarize_continuation_samples(samples, "SHORT", "LONG")

        self.assertEqual(
            result["windows"]["10m"]["domains"]["TAKER_CVD"]["state"],
            "UNKNOWN",
        )

    def test_one_last_taker_spike_cannot_flip_the_average(self):
        samples = observer_samples(11, buy=40.0, sell=60.0)
        samples[-1]["taker_buy_volume"] = 1_000.0
        samples[-1]["taker_sell_volume"] = 1.0
        result = summarize_continuation_samples(samples, "SHORT", "LONG")
        taker = result["windows"]["10m"]["domains"]["TAKER_CVD"]

        self.assertEqual(taker["state"], "NEUTRAL")
        self.assertLess(taker["bucket_persistence_pct"], 60.0)

    def test_one_last_volume_spike_cannot_flip_the_average(self):
        samples = observer_samples(11, quote_volume=100.0, baseline=100.0)
        samples[-1]["quote_volume"] = 1_000.0
        result = summarize_continuation_samples(samples, "SHORT", "LONG")
        volume = result["windows"]["10m"]["domains"]["VOLUME"]

        self.assertEqual(volume["state"], "NEUTRAL")
        self.assertLess(volume["expansion_persistence_pct"], 50.0)

    def test_missing_same_window_price_is_unknown_not_absorption(self):
        samples = observer_samples(11, buy=75.0, sell=25.0)
        samples[4]["price"] = None
        result = summarize_continuation_samples(samples, "SHORT", "LONG")

        self.assertEqual(
            result["windows"]["10m"]["domains"]["TAKER_CVD"]["state"],
            "UNKNOWN",
        )

    def test_skipped_closed_candle_breaks_same_window_price_coverage(self):
        samples = observer_samples(11)
        samples[5]["candle_close_ts"] += 60_000
        result = summarize_continuation_samples(samples, "SHORT", "LONG")

        self.assertIsNone(result["windows"]["10m"]["price_return_pct"])
        self.assertTrue(
            all(
                domain["state"] == "UNKNOWN"
                for domain in result["windows"]["10m"]["domains"].values()
            )
        )

    def test_out_of_order_closed_candles_cannot_pass_average_coverage(self):
        samples = observer_samples(11)
        samples[5]["candle_close_ts"], samples[6]["candle_close_ts"] = (
            samples[6]["candle_close_ts"],
            samples[5]["candle_close_ts"],
        )
        result = summarize_continuation_samples(samples, "SHORT", "LONG")

        self.assertIsNone(result["windows"]["10m"]["price_return_pct"])
        self.assertIsNone(
            result["windows"]["10m"]["directional_consistency_pct"]
        )

    def test_final_price_spike_does_not_masquerade_as_average_direction(self):
        prices = [100.0 - index * 0.05 for index in range(10)] + [101.0]
        result = summarize_continuation_samples(
            observer_samples(11, prices=prices),
            "SHORT",
            "LONG",
        )

        self.assertLess(
            result["windows"]["10m"]["directional_consistency_pct"],
            55.0,
        )
        self.assertNotEqual(
            result["windows"]["10m"]["domains"]["OI"]["state"],
            "SUPPORT",
        )

    def test_long_uses_thirty_and_sixty_minute_windows(self):
        result = summarize_continuation_samples(
            observer_samples(13, step_ms=300_000),
            "LONG",
            "LONG",
        )

        self.assertTrue(result["windows"]["30m"]["ready"])
        self.assertTrue(result["windows"]["60m"]["ready"])
        self.assertEqual(result["primary_window"], "60m")
        self.assertEqual(result["bucket_count"], 12)
        self.assertGreaterEqual(result["windows"]["60m"]["elapsed_minutes"], 60.0)

    def test_missing_oi_points_are_unknown_not_neutral(self):
        samples = observer_samples(11)
        samples[4]["open_interest_contracts"] = None
        samples[4]["open_interest_ccy"] = None
        result = summarize_continuation_samples(samples, "SHORT", "LONG")

        self.assertEqual(
            result["windows"]["10m"]["domains"]["OI"]["state"],
            "UNKNOWN",
        )
        self.assertEqual(result["windows"]["10m"]["state"], "FORMING")

    def test_polling_at_second_fifty_nine_cannot_make_oi_support(self):
        samples = observer_samples(11)
        for sample in samples:
            sample["observed_at_ms"] = sample["bucket_end_ms"] + 59_000
            sample["source_timestamps"]["open_interest"] = sample[
                "observed_at_ms"
            ]

        result = summarize_continuation_samples(samples, "SHORT", "LONG")
        window = result["windows"]["10m"]

        self.assertTrue(window["ready"])
        self.assertEqual(window["domains"]["OI"]["state"], "UNKNOWN")
        self.assertNotEqual(window["state"], "ALIGNED")

    def test_repeated_oi_source_timestamp_is_unknown(self):
        samples = observer_samples(11)
        repeated = samples[4]["source_timestamps"]["open_interest"]
        samples[5]["source_timestamps"]["open_interest"] = repeated

        result = summarize_continuation_samples(samples, "SHORT", "LONG")

        self.assertEqual(
            result["windows"]["10m"]["domains"]["OI"]["state"],
            "UNKNOWN",
        )

    def test_observation_jitter_does_not_change_exact_canonical_window(self):
        samples = observer_samples(11)
        jitters = [2_000, 7_000, 4_000, 6_000, 3_000, 8_000, 2_500, 5_500, 4_500, 7_500, 3_500]
        for sample, jitter in zip(samples, jitters):
            sample["observed_at_ms"] = sample["bucket_end_ms"] + jitter

        result = summarize_continuation_samples(samples, "SHORT", "LONG")

        self.assertTrue(result["windows"]["10m"]["ready"])
        self.assertEqual(result["windows"]["10m"]["elapsed_minutes"], 10.0)

    def test_missing_bucket_start_or_wrong_candle_start_fails_closed(self):
        missing_start = observer_samples(11)
        missing_start[5].pop("bucket_start_ms")
        missing_result = summarize_continuation_samples(
            missing_start,
            "SHORT",
            "LONG",
        )
        self.assertFalse(missing_result["windows"]["10m"]["ready"])

        wrong_candle = observer_samples(11)
        wrong_candle[5]["candle_ts"] += 1_000
        wrong_result = summarize_continuation_samples(
            wrong_candle,
            "SHORT",
            "LONG",
        )
        self.assertIsNone(wrong_result["windows"]["10m"]["price_return_pct"])
        self.assertTrue(
            all(
                domain["state"] == "UNKNOWN"
                for domain in wrong_result["windows"]["10m"]["domains"].values()
            )
        )


if __name__ == "__main__":
    unittest.main()
