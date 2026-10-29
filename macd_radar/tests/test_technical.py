import unittest

from macd_radar.technical import averages, ma_structure, detect_divergences, combined_analysis


class TestTechnical(unittest.TestCase):
    def test_sma_and_ema(self):
        self.assertEqual(averages([1, 2, 3, 4, 5], 3, "SMA"), [None, None, 2, 3, 4])
        self.assertEqual(averages([1, 2, 3, 4, 5], 3, "EMA")[-1], 4.0625)

    def test_bearish_5_10_20_alignment_slope(self):
        prices = [200 - i * .35 for i in range(150)]
        state = ma_structure(prices)
        self.assertEqual(state["structure"], "空方完整排列")
        self.assertLess(state["ma5"], state["ma10"])
        self.assertLess(state["ma10"], state["ma20"])
        self.assertLess(state["ma20_slope_2bars"], 0)

    def test_bullish_5_10_20_alignment(self):
        prices = [100 + i * .45 for i in range(150)]
        state = ma_structure(prices)
        self.assertEqual(state["structure"], "多方完整排列")
        self.assertGreater(state["ma20_slope_2bars"], 0)

    def test_pivot_requires_right_side_confirmation(self):
        highs = [100.] * 80
        lows = [99.] * 80
        oscill = [0.] * 80
        highs[60], highs[78] = 104, 108
        oscill[60], oscill[78] = 2, 1
        # Pivot at 78 has only one confirmed candle to its right; no signal.
        self.assertEqual(detect_divergences(highs, lows, oscill), [])

    def test_confirmed_bearish_regular_divergence(self):
        highs = [100.] * 80
        lows = [99.] * 80
        oscill = [0.] * 80
        highs[50], highs[67] = 104, 108
        oscill[50], oscill[67] = 2, 1
        found = detect_divergences(highs, lows, oscill, list(range(80)))
        self.assertTrue(found)
        self.assertEqual(found[0]["pattern"], "頂背離")
        self.assertEqual(found[0]["level"], "大背離候選")
        self.assertEqual(found[0]["confirmation_ts"], 69)

    def test_no_divergence_if_price_makes_lower_high(self):
        highs = [100.] * 80
        lows = [99.] * 80
        oscill = [0.] * 80
        highs[50], highs[67] = 108, 104
        oscill[50], oscill[67] = 2, 1
        self.assertEqual(detect_divergences(highs, lows, oscill), [])

    def test_zero_cross_not_signal_line_cross(self):
        closes = [100] * 120
        highs = [x+1 for x in closes]
        lows = [x-1 for x in closes]
        data = combined_analysis(closes, highs, lows, list(range(120)))
        self.assertEqual(data["ema12_26_cross"], "無新零軸交叉")
        self.assertEqual(data["divergences"], [])


if __name__ == "__main__":
    unittest.main()
