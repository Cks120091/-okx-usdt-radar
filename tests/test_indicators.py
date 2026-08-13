import math
import unittest

from radar.indicators import adx, atr, ema_series, features, macd, rsi
from radar.models import Candle


def rising_candles(count: int = 100) -> list[Candle]:
    output = []
    for index in range(count):
        close = 100 + (index * 0.4)
        output.append(Candle(index, close - 0.2, close + 0.8, close - 0.8, close, 10, 1000, True))
    return output


class IndicatorTests(unittest.TestCase):
    def test_ema_tracks_rising_prices(self):
        values = [float(item) for item in range(1, 101)]
        line = ema_series(values, 21)
        self.assertEqual(len(line), len(values))
        self.assertGreater(line[-1], line[-10])
        self.assertLess(line[-1], values[-1])

    def test_rsi_adx_and_atr_are_finite(self):
        candles = rising_candles()
        closes = [item.close for item in candles]
        self.assertGreater(rsi(closes), 70)
        self.assertTrue(math.isfinite(adx(candles)))
        self.assertGreater(adx(candles), 20)
        self.assertGreater(atr(candles), 0)

    def test_macd_returns_histogram(self):
        values = [100 + index * 0.1 + (index / 100) ** 3 for index in range(100)]
        line, signal, hist, previous = macd(values)
        self.assertTrue(all(math.isfinite(item) for item in (line, signal, hist, previous)))

    def test_comprehensive_features_are_finite(self):
        values = features(rising_candles())
        self.assertTrue(math.isfinite(values.vwap20))
        self.assertTrue(math.isfinite(values.bollinger_width_pct))
        self.assertGreaterEqual(values.directional_volume_ratio, 0)
        self.assertLessEqual(values.directional_volume_ratio, 1)
        self.assertGreater(values.atr_pct, 0)

    def test_structure_windows_keep_older_obstacles(self):
        candles = rising_candles(120)
        older = candles[30]
        candles[30] = Candle(
            older.ts,
            older.open,
            250.0,
            older.low,
            older.close,
            older.volume,
            older.quote_volume,
            True,
        )
        values = features(candles)
        self.assertGreater(values.prior_high100, values.prior_high50)
        self.assertGreaterEqual(values.prior_high50, values.prior_high20)


if __name__ == "__main__":
    unittest.main()
