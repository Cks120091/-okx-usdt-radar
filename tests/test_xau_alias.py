import unittest

from radar.market_scope import XAU_INST_ID
from radar.service import PreflightError, _normalize_usdt_swap_id


class XAUAliasTests(unittest.TestCase):
    def test_supported_gold_aliases_resolve_to_one_okx_instrument(self):
        for value in (
            "XAU", "XAUUSDT", "XAU/USDT", "XAU-USDT-SWAP",
            "XAUUSDT.P", "OKX:XAUUSDT.P", " okx:xauusdt.p ",
        ):
            with self.subTest(value=value):
                self.assertEqual(_normalize_usdt_swap_id(value), XAU_INST_ID)

    def test_other_venues_and_commodity_aliases_are_not_rewritten(self):
        for value in (
            "BINANCE:XAUUSDT.P", "BYBIT:XAUUSDT.P", "XAGUSDT.P",
            "XAUTUSDT.P", "XAUUSDT.P.EXTRA", "OKX:XAGUSDT.P",
        ):
            with self.subTest(value=value), self.assertRaises(PreflightError):
                _normalize_usdt_swap_id(value)

    def test_existing_crypto_names_remain_compatible(self):
        for value in ("BTC", "BTCUSDT", "BTC/USDT", "BTC-USDT-SWAP"):
            with self.subTest(value=value):
                self.assertEqual(_normalize_usdt_swap_id(value), "BTC-USDT-SWAP")
