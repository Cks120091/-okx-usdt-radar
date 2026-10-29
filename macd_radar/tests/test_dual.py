"""Safety and chronology tests for strict MA + MACD event gating."""
import unittest
from unittest.mock import patch
from macd_radar.dual import dual_confirm


class TestDualConfirm(unittest.TestCase):
    @staticmethod
    def sequences(ma_turn=118, macd_turn=119, n=120, signal_direction="bull"):
        # MA20 increases continuously; a/b only become bullish at ma_turn.
        c = [10 + i * .1 for i in range(n)]
        b = [v - 1 if i < ma_turn else v + 1 for i, v in enumerate(c)]
        a = [v - 1 if i < ma_turn else v + 1 for i, v in enumerate(b)]
        # MACD line is 2.0. Before cross signal is 3.0 (negative
        # histogram); at cross becomes 1.0 (positive histogram).
        fast = [2.0] * n
        slow = [0.0] * n
        signal = [3.0 if i < macd_turn else 1.0 for i in range(n)]
        if signal_direction == "bear":
            signal = [1.0 if i < macd_turn else 3.0 for i in range(n)]
        return [a, b, c], [fast, slow, signal]

    def run_pattern(self, ma_turn=118, macd_turn=119, n=120, signal_direction="bull"):
        ma, ema = self.sequences(ma_turn, macd_turn, n, signal_direction)
        with patch("macd_radar.dual.averages", side_effect=ma), patch(
            "macd_radar.dual.ema", side_effect=ema
        ):
            return dual_confirm([100.0] * n)

    def test_simultaneous_confirm(self):
        actual = self.run_pattern(ma_turn=119, macd_turn=119)
        self.assertEqual(actual["trigger"], "正式雙確認做多")
        self.assertEqual(actual["ma_confirm_bars_ago"], 0)
        self.assertEqual(actual["macd_confirm_bars_ago"], 0)

    def test_ma_first_macd_last(self):
        actual = self.run_pattern(ma_turn=117, macd_turn=119)
        self.assertTrue(actual["is_new"])
        self.assertEqual(actual["ma_confirm_bars_ago"], 2)
        self.assertEqual(actual["macd_confirm_bars_ago"], 0)

    def test_macd_first_ma_last(self):
        actual = self.run_pattern(ma_turn=119, macd_turn=117)
        self.assertTrue(actual["is_new"])
        self.assertEqual(actual["macd_confirm_bars_ago"], 2)
        self.assertEqual(actual["ma_confirm_bars_ago"], 0)

    def test_old_pair_is_not_falsely_retriggered(self):
        actual = self.run_pattern(ma_turn=116, macd_turn=117)
        self.assertEqual(actual["trigger"], "無新雙確認")
        self.assertTrue(actual["confirmed"])
        self.assertFalse(actual["is_new"])
        self.assertEqual(actual["state"], "雙確認做多維持")

    def test_old_ma_confirmation_expires(self):
        actual = self.run_pattern(ma_turn=112, macd_turn=119)
        self.assertEqual(actual["trigger"], "無新雙確認")
        self.assertFalse(actual["confirmed"])

    def test_opposite_macd_cross_cannot_form_bull_signal(self):
        actual = self.run_pattern(ma_turn=119, macd_turn=119, signal_direction="bear")
        self.assertEqual(actual["trigger"], "無新雙確認")
        self.assertFalse(actual["confirmed"])

    def test_insufficient_history_rejected(self):
        with self.assertRaises(ValueError):
            dual_confirm([100.0] * 99)


if __name__ == "__main__":
    unittest.main()
