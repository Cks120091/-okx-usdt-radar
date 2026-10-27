"""Offline tests for publicly sourced read-only wallet research and paper risk."""
from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.error import HTTPError

from solana_paper.council import PaperCouncil
from solana_paper.engine import PaperBook, ExecutionAgent, Settings, SafetyAgent
from solana_paper.intelligence import (
    WalletObserver, MintRiskObserver, PUBLIC_WATCHLIST, extract_wallet_mints, mint_risk_from_rpc,
)

MINT = "A" * 44


class StubRpc:
    def __init__(self, amount="1000"):
        self.amount = amount
        self.calls = 0

    def call(self, method, params):
        self.calls += 1
        if method == "getTokenAccountsByOwner":
            return {"value": [{"account": {"data": {"parsed": {"info": {
                "mint": MINT, "tokenAmount": {"amount": self.amount}}}}}}]}
        if method == "getAccountInfo":
            return {"value": {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                              "data": {"parsed": {"type": "mint", "info": {
                                  "mintAuthority": "some_authority", "freezeAuthority": None}}}}}
        raise AssertionError(method)


class FailingRpc:
    def call(self, method, params):
        raise HTTPError("https://example.invalid", 429, "Rate limited", {}, None)


class IntelligenceTests(unittest.TestCase):
    def test_public_source_address_is_valid_and_not_claimed_proven(self):
        self.assertEqual(len(PUBLIC_WATCHLIST), 3)
        self.assertEqual(len({w["address"] for w in PUBLIC_WATCHLIST}), 3)
        self.assertTrue(all(len(w["address"]) >= 32 for w in PUBLIC_WATCHLIST))

    def test_owner_token_accounts_are_holdings_not_trades(self):
        rpc = StubRpc()
        wallet = WalletObserver(rpc=rpc, wallets=[PUBLIC_WATCHLIST[0]])
        self.assertTrue(wallet.refresh(1000))
        state = wallet.status(MINT, 1005)
        self.assertEqual("OBSERVED_HOLDING", state["state"])
        self.assertEqual(1, state["matched_wallets"])
        self.assertFalse(state["score_used"])
        self.assertEqual(1, rpc.calls)
        self.assertTrue(wallet.refresh(1020))  # cache, do not hammer API
        self.assertEqual(1, rpc.calls)
        self.assertEqual("NO_DATA", wallet.status(MINT, 2300)["state"])

    def test_rpc_rate_limit_never_means_no_smart_wallets(self):
        wallet = WalletObserver(rpc=FailingRpc(), wallets=[PUBLIC_WATCHLIST[0]])
        self.assertFalse(wallet.refresh(1000))
        self.assertEqual("NO_DATA", wallet.status(MINT, 1001)["state"])
        self.assertGreater(wallet.backoff_until, 1000)

    def test_mint_authority_flags_veto_and_unknown_does_not_approve(self):
        report = mint_risk_from_rpc(StubRpc().call("getAccountInfo", []))
        self.assertEqual("RISK", report["state"])
        safe = SafetyAgent(Settings()).evaluate({
            "liquidity_usd": 150000.0, "age_minutes": 60.0, "fdv": 1_000_000.0,
            "onchain_risk": report,
        })
        self.assertFalse(safe["passed"])
        self.assertEqual("RISK", safe["contract_security"])
        self.assertEqual("UNKNOWN", mint_risk_from_rpc({"value": None})["state"])

    def test_council_abstention_and_veto(self):
        a = {"sources": ["profile"]}
        market = {"passed": True, "onchain": {"state": "UNKNOWN"}}
        unknown = {"state": "NO_DATA"}
        social = {"state": "PAID_MARKETING_ONLY"}
        decision = {"state": "PAPER_BUY"}
        c = PaperCouncil.consider(a, market, unknown, social, decision, True, "ok")
        self.assertTrue(c["paper_allowed"])
        self.assertEqual("CAUTION", c["votes"]["safety"]["vote"])
        self.assertEqual("ABSTAIN", c["votes"]["smart_money"]["vote"])
        self.assertEqual("ABSTAIN", c["votes"]["social"]["vote"])
        market["onchain"] = {"state": "RISK"}
        veto = PaperCouncil.consider(a, market, unknown, social, decision, True, "ok")
        self.assertFalse(veto["paper_allowed"])
        self.assertEqual("VETO", veto["outcome"])

    def test_simulated_network_fees_and_cash_buffer_enforced_in_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = replace(Settings(), db_path=str(Path(tmp) / "paper.sqlite"),
                        initial_cash=100, trade_usd=51, max_allocated_pct=.90,
                        cash_reserve_usd=50, simulated_network_fee_usd=.02)
            book = PaperBook(s)
            token = {"mint": MINT, "symbol": "TEST", "price": 1}
            self.assertFalse(ExecutionAgent(book, s).approve(token, 1000)[0])
            self.assertFalse(book.buy(token, 1000))
            self.assertEqual([], book.trades())
            self.assertEqual(100, book.cash())
            s2 = replace(s, trade_usd=40)
            book2 = PaperBook(s2)
            self.assertTrue(ExecutionAgent(book2, s2).approve(token, 1000)[0])
            self.assertTrue(book2.buy(token, 1000))
            self.assertGreaterEqual(book2.cash(), 50)
            self.assertAlmostEqual(book2.trades()[0]["fee"], 40 * .003 + .02, places=6)
            pos = book2.positions()[0]
            self.assertIsNotNone(book2.sell(pos, 1.2, "TEST_EXIT", 1001))
            self.assertGreater(book2.trades()[0]["fee"], .02)


class MarketFeedGateTests(unittest.TestCase):
    def test_rate_limit_retry_after_and_no_expired_price_reuse(self):
        import io
        from solana_paper.feed_control import MarketHttpGate, MarketTemporarilyUnavailable
        class Clock:
            t = 1000.0
            def now(self):
                return self.t
            def sleep(self, seconds):
                self.t += seconds
        class Response:
            def __init__(self, blob):
                self.blob = blob
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, count):
                return self.blob[:count]
        clock = Clock()
        requests = []
        def fetch(request, timeout):
            from urllib.error import HTTPError
            requests.append(request.full_url)
            if len(requests) > 1:
                raise HTTPError(request.full_url, 429, "Too Many Requests",
                                {"Retry-After": "1200"}, None)
            return Response(b'{"data":[1]}')
        gate = MarketHttpGate(clock=clock.now, fetcher=fetch, sleeper=clock.sleep)
        path = "/networks/solana/new_pools"
        self.assertEqual(gate.get("GECKOTERMINAL", path, {}, ttl=30), {"data": [1]})
        clock.t += 10
        self.assertEqual(gate.get("GECKOTERMINAL", path, {}, ttl=30), {"data": [1]})
        self.assertEqual(len(requests), 1)
        clock.t += 40
        with self.assertRaises(MarketTemporarilyUnavailable) as blocked:
            gate.get("GECKOTERMINAL", path, {}, ttl=30)
        self.assertGreaterEqual(blocked.exception.retry_at - clock.t, 1200)
        with self.assertRaises(MarketTemporarilyUnavailable):
            gate.get("GECKOTERMINAL", path, {}, ttl=30)
        self.assertEqual(len(requests), 2)
        stat = gate.snapshot()["GECKOTERMINAL"]
        self.assertEqual(stat["http_calls"], 2)
        self.assertEqual(stat["cache_hits"], 1)
        self.assertEqual(stat["blocked_calls"], 1)
        self.assertEqual(stat["state"], "BACKOFF")

    def test_gecko_quote_refreshes_known_pools_in_one_request(self):
        from solana_paper.engine import GeckoFeed
        class FakeGate:
            calls = []
            def get(self, source, path, headers, ttl):
                self.calls.append((source, path))
                return {"data": [], "included": []}
        gateway = FakeGate()
        gecko = GeckoFeed(gateway)
        gecko.known_pool = {("A" * 43) + str(i): ("B" * 43) + str(i)
                            for i in range(10)}
        results = gecko.quotes(list(gecko.known_pool))
        self.assertEqual(results, [])
        self.assertEqual(len(gateway.calls), 1)
        self.assertTrue(gateway.calls[0][1].startswith("/networks/solana/pools/multi/"))

    def test_both_blocked_sources_dont_fake_paper_fill(self):
        from solana_paper.engine import ResilientFeed, PaperRuntime
        from solana_paper.feed_control import MarketTemporarilyUnavailable
        class FailedProvider:
            def discover(self):
                raise MarketTemporarilyUnavailable("UPSTREAM", "HTTP 429", 9999999999)
        with tempfile.TemporaryDirectory() as tmp:
            settings = replace(Settings(), db_path=str(Path(tmp) / "paper.db"))
            feed = ResilientFeed()
            feed.dex, feed.gecko = FailedProvider(), FailedProvider()
            runtime = PaperRuntime(settings, feed)
            result = runtime.run_once(1000)
            self.assertEqual(result["buys"], [])
            self.assertEqual(result["status"], "UPSTREAM_BACKOFF")
            self.assertEqual(runtime.book.trades(), [])
            self.assertTrue(runtime.status()["market_snapshot_stale"])


if __name__ == "__main__":
    unittest.main()
