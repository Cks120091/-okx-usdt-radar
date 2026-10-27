"""Offline paper-only engine smoke and accounting tests (no external network)."""
from __future__ import annotations

import os
import http.client
import threading
from http.server import ThreadingHTTPServer
import tempfile
import unittest
from dataclasses import replace

from solana_paper.app import make_handler
from solana_paper.engine import (
    DecisionAgent,
    DexFeed,
    PaperRuntime,
    SafetyAgent,
    Settings,
    normalize_pairs,
)

MINT = "A" * 32
NOW = 2_000_000_000.0


def pool(price=0.01, liquidity=100_000, mint=MINT):
    return {
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": "B" * 32,
        "baseToken": {"address": mint, "symbol": "TEST", "name": "Test Meme"},
        "quoteToken": {"address": "So11111111111111111111111111111111111111112"},
        "priceUsd": str(price),
        "pairCreatedAt": int((NOW - 7200) * 1000),
        "liquidity": {"usd": liquidity},
        "volume": {"h1": 32000, "h24": 250000},
        "txns": {"h1": {"buys": 190, "sells": 90}},
        "priceChange": {"m5": 4.2, "h1": 25.0},
        "fdv": 600000,
        "marketCap": 500000,
    }


class FakeFeed:
    def __init__(self, price=0.01):
        self.price = price

    def discover(self):
        return {"mints": [MINT], "pairs": [pool(self.price)], "paid": {},
                "sources": {MINT: ["profile"]}}

    def quotes(self, mints):
        return [pool(self.price)] if MINT in mints else []


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = replace(Settings(), db_path=os.path.join(self.temp.name, "book.db"))
        self.feed = FakeFeed()
        self.runtime = PaperRuntime(self.settings, self.feed)

    def tearDown(self):
        self.temp.cleanup()

    def test_mint_validation_and_pair_filter(self):
        self.assertTrue(DexFeed.valid(MINT))
        self.assertFalse(DexFeed.valid("fake"))
        self.assertFalse(DexFeed.valid("So11111111111111111111111111111111111111112"))
        data = normalize_pairs([pool(), {**pool(), "chainId": "ethereum"}], [MINT], NOW)
        self.assertEqual(list(data), [MINT])

    def test_safety_unverified_and_rules_only(self):
        token = normalize_pairs([pool()], [MINT], NOW)[MINT]
        safety = SafetyAgent(self.settings).evaluate(token)
        self.assertTrue(safety["passed"])
        self.assertEqual(safety["contract_security"], "UNVERIFIED")
        self.assertEqual(DecisionAgent(self.settings).evaluate(token, safety)["state"], "PAPER_BUY")

    def test_buy_then_take_profit_in_simulated_db(self):
        result = self.runtime.run_once(NOW)
        self.assertEqual(len(result["buys"]), 1)
        self.assertEqual(len(self.runtime.book.positions()), 1)
        self.assertLess(self.runtime.book.cash(), 1000)
        self.feed.price = 0.015
        result = self.runtime.run_once(NOW + 150)
        self.assertEqual(len(result["exits"]), 1)
        self.assertEqual(self.runtime.book.positions(), [])
        self.assertGreater(self.runtime.book.cash(), 1000)
        self.assertEqual([x["side"] for x in self.runtime.book.trades()], ["SELL", "BUY"])

    def test_rebuy_cooldown_and_max_positions(self):
        self.runtime.run_once(NOW)
        result = self.runtime.run_once(NOW + 150)
        self.assertEqual(result["buys"], [])
        self.assertEqual(len(self.runtime.book.positions()), 1)

    def test_reject_weak_liquidity(self):
        token = normalize_pairs([pool(liquidity=2500)], [MINT], NOW)[MINT]
        self.assertEqual(SafetyAgent(self.settings).evaluate(token)["state"], "REJECT")

    def test_independent_monitor_closes_without_discovery_scan(self):
        first = self.runtime.run_once(NOW)
        self.assertEqual(len(first["buys"]), 1)
        self.assertEqual(len(self.runtime.book.positions()), 1)
        self.runtime.monitor_feed = self.feed
        self.feed.price = 0.015
        outcome = self.runtime.monitor_once(NOW + 30)
        self.assertEqual("OK", outcome["status"])
        self.assertEqual(len(outcome["exits"]), 1)
        self.assertEqual(self.runtime.book.positions(), [])
        self.assertEqual(self.runtime.monitor_last_success, NOW + 30)
        self.assertEqual([t["side"] for t in self.runtime.book.trades()], ["SELL", "BUY"])

    def test_monitor_failure_never_creates_fake_exit(self):
        self.runtime.run_once(NOW)
        class EmptyQuotes:
            def quotes(self, mints):
                return []
        self.runtime.monitor_feed = EmptyQuotes()
        failed = self.runtime.monitor_once(NOW + 30)
        self.assertEqual("ERROR", failed["status"])
        self.assertEqual(1, len(self.runtime.book.positions()))
        self.assertEqual(1, len(self.runtime.book.trades()))
        self.assertEqual("RATE_LIMIT_BACKOFF", self.runtime.monitor_once(NOW + 40)["status"])

    def test_head_homepage_returns_headers_without_body(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.runtime))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
        try:
            conn.request("HEAD", "/")
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            self.assertGreater(int(res.getheader("Content-Length", "0")), 1000)
            self.assertEqual(res.read(), b"")
            conn.request("GET", "/health")
            health = conn.getresponse()
            self.assertEqual(health.status, 200)
            self.assertIn(b"PAPER_ONLY", health.read())
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_reports_read_only_paper_mode(self):
        s = self.runtime.status()
        self.assertEqual(s["mode"], "PAPER_ONLY")
        self.assertFalse(s["execution_enabled_on_chain"])
        self.assertEqual(len(s["agents"]), 6)
        self.assertFalse(s["persistent_storage_guaranteed"])


if __name__ == "__main__":
    unittest.main()
