"""No-order regression tests for gross percentage-based TP targets."""
import unittest
from radar.profit_target import percent_target_price


class ProfitTargetTests(unittest.TestCase):
    def test_price_move_long_short(self):
        self.assertAlmostEqual(percent_target_price(100, "LONG", 2, basis="PRICE_MOVE"), 102)
        self.assertAlmostEqual(percent_target_price(100, "SHORT", 2, basis="PRICE_MOVE"), 98)

    def test_margin_roi_with_leverage(self):
        self.assertAlmostEqual(percent_target_price(100, "LONG", 2, basis="MARGIN_ROI", leverage=10), 100.2)
        self.assertAlmostEqual(percent_target_price(100, "SHORT", 2, basis="MARGIN_ROI", leverage=10), 99.8)

    def test_invalid_inputs_fail_closed(self):
        cases = [
            (0, "LONG", 2, "PRICE_MOVE", 1),
            (100, "LONG", -1, "PRICE_MOVE", 1),
            (100, "LONG", 2, "MARGIN_ROI", 0),
            (100, "LONG", 2, "MARGIN_ROI", 200),
            (100, "NEUTRAL", 2, "PRICE_MOVE", 1),
            (100, "LONG", 2, "UNKNOWN", 1),
            (float("nan"), "LONG", 2, "PRICE_MOVE", 1),
        ]
        for price, direction, pct, basis, leverage in cases:
            with self.assertRaises(ValueError):
                percent_target_price(price, direction, pct,
                                     basis=basis, leverage=leverage)


if __name__ == "__main__":
    unittest.main()
