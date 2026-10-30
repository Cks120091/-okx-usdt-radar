"""No confirmed-but-old candle may issue a formal MA + MACD signal."""
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from macd_radar.app import (
    CANDLE_FRESHNESS_GRACE_MS,
    scan_one,
    validate_confirmed_bar_freshness,
)

TPE = ZoneInfo("Asia/Taipei")


def taipei_ms(hour, minute):
    return int(datetime(2026, 10, 30, hour, minute, tzinfo=TPE).timestamp() * 1000)


class TestCandleFreshness(unittest.TestCase):
    def test_15m_at_0803_latest_0800_is_fresh(self):
        info = validate_confirmed_bar_freshness(
            taipei_ms(8, 0), "15m", now_ms=taipei_ms(8, 3)
        )
        self.assertTrue(info["fresh"])
        self.assertEqual(info["bar_age_seconds"], 180)
        self.assertEqual(info["freshness_max_seconds"], 1020)

    def test_screenshot_15m_0700_at_0803_must_not_signal(self):
        with self.assertRaisesRegex(ValueError, "行情資料落後"):
            validate_confirmed_bar_freshness(
                taipei_ms(7, 0), "15m", now_ms=taipei_ms(8, 3)
            )

    def test_stale_by_one_full_bar_plus_grace_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "行情資料落後"):
            validate_confirmed_bar_freshness(
                taipei_ms(7, 45), "15m", now_ms=taipei_ms(8, 3)
            )

    def test_one_bar_grace_boundary_is_accepted(self):
        info = validate_confirmed_bar_freshness(
            taipei_ms(7, 45), "15m",
            now_ms=taipei_ms(8, 2)
        )
        self.assertTrue(info["fresh"])
        self.assertEqual(CANDLE_FRESHNESS_GRACE_MS, 120_000)

    def test_4h_closed_0400_at_0703_is_valid(self):
        info = validate_confirmed_bar_freshness(
            taipei_ms(4, 0), "4H", now_ms=taipei_ms(7, 3)
        )
        self.assertTrue(info["fresh"])

    def test_exchange_future_timestamp_rejected(self):
        with self.assertRaisesRegex(ValueError, "時間超前"):
            validate_confirmed_bar_freshness(
                taipei_ms(8, 15), "15m", now_ms=taipei_ms(8, 3)
            )

    def test_stale_gold_blocks_indicators_before_any_signal_generated(self):
        # Build 120 consecutive confirmed 15m candles; latest close = 07:00.
        newest_end_ms = taipei_ms(7, 0)
        width_ms = 15 * 60 * 1000
        candles = [
            [str(newest_end_ms - width_ms * (i+1)),
             "4032", "4035", "4020", "4031.5", "1", "1", "1", "1"]
            for i in range(120)
        ]
        gold_ticker = {
            "instId": "XAU-USDT-SWAP",
            "last": "4026.7",
            "turnover_24h_usdt_est": 262_914_103.01,
        }
        with patch("macd_radar.app.okx_get", return_value=candles), \
             patch("macd_radar.app.time.time", return_value=taipei_ms(8, 3) / 1000), \
             patch("macd_radar.app.calculate") as signal_calc:
            with self.assertRaisesRegex(ValueError, "行情資料落後"):
                scan_one(gold_ticker, "15m")
        signal_calc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
