"""Run: python -m unittest discover -s macd_radar/tests -v"""
import unittest
from macd_radar.core import calculate, ema, MIN_BARS


class TestMacd(unittest.TestCase):
    def test_constant_prices_are_neutral(self):
        s = calculate([100.0] * 120)
        self.assertEqual(s.event, "無新交叉")
        self.assertEqual(s.side, "中性")
        self.assertEqual(s.histogram, 0.0)

    def test_invalid_series_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate([100.0] * (MIN_BARS - 1))
        with self.assertRaises(ValueError):
            calculate([100.0] * 100 + [0.0])

    def test_ema_returns_expected_simple_case(self):
        self.assertEqual(ema([10, 20, 30], 3), [10, 15, 22.5])

    def test_rising_then_falling_produces_confirmed_death_cross(self):
        prices = [100.0] * 100 + [100 + 2 * i for i in range(1, 35)]
        for i in range(1, 40):
            prices.append(prices[-1] - 2)
        crosses = [calculate(prices[:i]).event for i in range(MIN_BARS, len(prices)+1)]
        self.assertIn("死叉", crosses)

    def test_histogram_is_not_win_probability(self):
        s = calculate([100 + i * 0.3 for i in range(120)])
        self.assertGreaterEqual(s.normalized_histogram, 0.0)
        self.assertLess(s.normalized_histogram, 1.0)


if __name__ == "__main__":
    unittest.main()
