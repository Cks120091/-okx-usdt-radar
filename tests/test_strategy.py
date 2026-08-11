import unittest

from radar.models import Candle, Instrument, Ticker
from radar.strategy import AdaptiveStrategyEngine, StrategyConfig, _format_price


def trend_candles(start, step, count=100, quote_volume=120_000, breakout=False, accelerate=False):
    closes = [start + (step * index) for index in range(count)]
    if accelerate:
        for index in range(count - 6, count):
            closes[index] += ((index - (count - 7)) ** 2) * abs(step) * (1 if step > 0 else -1) * 0.25
    if breakout:
        closes[-1] += 1.10 if step > 0 else -1.10
    candles = []
    for index, close in enumerate(closes):
        padding = 1.0
        candles.append(
            Candle(
                ts=1_700_000_000_000 + index * 60_000,
                open=close - (step * 0.25),
                high=close + padding,
                low=close - padding,
                close=close,
                volume=1000,
                quote_volume=quote_volume * (3.0 if breakout and index == count - 1 else 1.0),
                confirmed=True,
            )
        )
    return candles


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.instrument = Instrument("TEST-USDT-SWAP", "live", "USDT", "linear", 0.01)

    def test_clear_breakout_can_qualify(self):
        candles_4h = trend_candles(80, 0.4)
        candles_1h = trend_candles(100, 0.18, breakout=True)
        candles_15m = trend_candles(110, 0.09, accelerate=True)
        ticker = Ticker("TEST-USDT-SWAP", candles_15m[-1].close, candles_15m[-1].close - 0.03, candles_15m[-1].close + 0.03, 1)
        engine = AdaptiveStrategyEngine(StrategyConfig(min_quote_volume_24h=1_000_000))
        result = engine.analyze(self.instrument, ticker, candles_4h, candles_1h, candles_15m)
        self.assertIsNotNone(result.signal, result.reason)
        self.assertEqual(result.signal.direction, "LONG")
        self.assertGreaterEqual(result.signal.risk_reward, 1.8)
        self.assertGreaterEqual(len(result.signal.evidence), 2)

    def test_low_liquidity_is_rejected(self):
        data = trend_candles(100, 0.1, quote_volume=100)
        ticker = Ticker("TEST-USDT-SWAP", 110, 109.99, 110.01, 1)
        result = AdaptiveStrategyEngine().analyze(self.instrument, ticker, data, data, data)
        self.assertEqual(result.reason, "liquidity_too_low")

    def test_unconfirmed_or_short_history_is_rejected(self):
        data = trend_candles(100, 0.1, count=59)
        ticker = Ticker("TEST-USDT-SWAP", 106, 105.99, 106.01, 1)
        result = AdaptiveStrategyEngine().analyze(self.instrument, ticker, data, data, data)
        self.assertEqual(result.reason, "insufficient_history")

    def test_price_is_rounded_to_instrument_tick_size(self):
        self.assertEqual(_format_price(123.456, 0.01), "123.46")
        self.assertEqual(_format_price(1.024, 0.05), "1.00")
        self.assertEqual(_format_price(1.026, 0.05), "1.05")


if __name__ == "__main__":
    unittest.main()
