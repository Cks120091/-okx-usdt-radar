import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from solana_paper.engine import DecisionAgent, DexFeed, PaperBook, PaperRuntime, SafetyAgent, Settings, normalize_pairs

MINT = "A" * 44
NOW = 1793100000.0

def pair(price=1.0, liquidity=150000.0, mint=MINT):
    return {
        "chainId": "solana",
        "baseToken": {"address": mint, "symbol": "MOON", "name": "Moon Test"},
        "quoteToken": {"address": "So11111111111111111111111111111111111111112", "symbol": "SOL"},
        "priceUsd": str(price), "liquidity": {"usd": liquidity}, "fdv": 1000000,
        "pairCreatedAt": int((NOW - 3600) * 1000),
        "volume": {"h1": 40000, "h24": 120000},
        "priceChange": {"m5": 5.0, "h1": 25.0},
        "txns": {"h1": {"buys": 70, "sells": 30}},
        "pairAddress": "B" * 44, "dexId": "raydium",
    }

class FakeFeed:
    def __init__(self):
        self.price, self.liquidity = 1.0, 150000.0
    def discover(self):
        return {"mints": [MINT], "pairs": [pair(self.price, self.liquidity)],
                "paid": {MINT: 5}, "sources": {MINT: ["profile", "paid_boost"]}}
    def quotes(self, mints):
        return [pair(self.price, self.liquidity)]

class BadFeed:
    def discover(self):
        raise RuntimeError("upstream offline")

class PaperSystemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = replace(Settings(), db_path=str(Path(self.tmp.name) / "ledger.sqlite3"))
        self.feed = FakeFeed()
        self.rt = PaperRuntime(self.s, self.feed)

    def tearDown(self):
        self.tmp.cleanup()

    def test_six_agents_and_paper_only(self):
        state = self.rt.status()
        self.assertEqual(6, len(state["agents"]))
        self.assertFalse(state["execution_enabled_on_chain"])
        self.assertEqual("PAPER_ONLY", state["mode"])

    def test_buy_and_simulated_take_profit(self):
        self.rt.run_once(NOW)
        self.assertEqual(1, len(self.rt.book.positions()))
        self.assertEqual("BUY", self.rt.book.trades()[0]["side"])
        self.feed.price = 1.5
        self.rt.run_once(NOW + 150)
        self.assertEqual(0, len(self.rt.book.positions()))
        trade = self.rt.book.trades()[0]
        self.assertEqual("SELL", trade["side"])
        self.assertEqual("TAKE_PROFIT", trade["reason"])
        self.assertGreater(trade["pnl"], 0)

    def test_illiquid_pool_cannot_buy(self):
        self.feed.liquidity = 200
        self.rt.run_once(NOW)
        self.assertEqual([], self.rt.book.positions())
        self.assertEqual("REJECT", self.rt.candidates[0]["state"])

    def test_buys_include_slippage_and_fees(self):
        self.rt.run_once(NOW)
        pos = self.rt.book.positions()[0]
        self.assertGreater(pos["entry"], 1)
        self.assertLess(self.rt.book.cash(), 950)
        self.assertLess(self.rt.book.equity(), 1000)

    def test_error_never_fabricates_quotes_or_orders(self):
        self.rt.feed = BadFeed()
        result = self.rt.run_once(NOW)
        self.assertIn("error", result)
        self.assertEqual([], self.rt.book.trades())
        self.assertIn("upstream offline", self.rt.last_error)

    def test_ledger_survives_process_object_recreation(self):
        self.rt.run_once(NOW)
        new_book = PaperBook(self.s)
        self.assertEqual(1, len(new_book.positions()))
        self.assertEqual(1, len(new_book.trades()))

    def test_quote_filter_does_not_accept_quote_token(self):
        raw = pair()
        wrong = pair()
        wrong["baseToken"] = {"address": "B" * 44, "symbol": "OTHER"}
        chosen = normalize_pairs([wrong, raw], [MINT], NOW)
        self.assertEqual([MINT], list(chosen.keys()))

    def test_score_not_probability_and_risk_is_unverified(self):
        token = normalize_pairs([pair()], [MINT], NOW)[MINT]
        safety = SafetyAgent(self.s).evaluate(token)
        decision = DecisionAgent(self.s).evaluate(token, safety)
        self.assertEqual("UNVERIFIED", safety["contract_security"])
        self.assertEqual("PAPER_BUY", decision["state"])
        self.assertEqual(100, decision["score"])

    def test_mint_validation(self):
        self.assertTrue(DexFeed.valid(MINT))
        self.assertFalse(DexFeed.valid("../secret"))
        self.assertFalse(DexFeed.valid("ethereum"))

    def test_no_reentry_before_cooldown(self):
        self.rt.run_once(NOW)
        self.feed.price = 1.5
        self.rt.run_once(NOW + 150)
        self.rt.run_once(NOW + 300)
        self.assertEqual(2, len(self.rt.book.trades()))
        self.assertEqual(0, len(self.rt.book.positions()))

if __name__ == "__main__":
    unittest.main()
