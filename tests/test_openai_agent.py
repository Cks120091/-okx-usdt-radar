"""No-network tests for opt-in OpenAI six-role paper council."""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from solana_paper.openai_agent import (
    OpenAICouncil, OpenAIResponses, ROLES, role_response,
)
from solana_paper.engine import PaperRuntime, Settings

MINT = "A" * 44
NOW = 1793100000.0


def paper_pair():
    return {
        "chainId": "solana", "baseToken": {"address": MINT, "symbol": "TEST"},
        "pairAddress": "B" * 44, "dexId": "raydium",
        "priceUsd": "1.0", "liquidity": {"usd": 150000.0}, "fdv": 1000000,
        "pairCreatedAt": int((NOW - 3600) * 1000),
        "volume": {"h1": 40000, "h24": 120000},
        "priceChange": {"m5": 5.0, "h1": 25.0},
        "txns": {"h1": {"buys": 70, "sells": 30}},
    }


class FakeFeed:
    def discover(self):
        return {
            "mints": [MINT], "pairs": [paper_pair()],
            "paid": {}, "sources": {MINT: ["profile"]},
        }
    def quotes(self, mints):
        return [paper_pair()]


class FakeTransport:
    def __init__(self, veto=None):
        self.calls = []
        self.veto = veto

    def ask(self, role, token, prior):
        self.calls.append((role, dict(prior)))
        choice = ("VETO" if role == self.veto else
                  "ABSTAIN" if role in ("smart_money", "social") else "APPROVE")
        return {"vote": choice, "reason": "僅供模擬研究，無已驗證獲利資料"}, {
            "input_tokens": 200, "output_tokens": 100,
        }


class FakeModelReply:
    def __init__(self, content):
        self.content = content
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, size): return self.content[:size]


class OpenAITests(unittest.TestCase):
    def test_no_key_and_disabled_never_call_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "paper.sqlite3")
            off = OpenAICouncil(path, env={"PAPER_OPENAI_ENABLED": "0", "OPENAI_API_KEY": "sk-fake"})
            self.assertFalse(off.enabled)
            self.assertEqual("DISABLED", off.status()["state"])
            self.assertEqual("DISABLED", off.submit({"mint": MINT}))
            missing = OpenAICouncil(path, env={"PAPER_OPENAI_ENABLED": "1"})
            self.assertFalse(missing.enabled)
            self.assertEqual("WAITING_API_KEY", missing.status()["state"])

    def test_response_parser_does_not_assume_first_output_item(self):
        payload = {"status": "completed", "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "content": [{"type": "output_text",
                "text": '{"vote":"CAUTION","reason":"資料不足"}'}]},
        ]}
        self.assertEqual(role_response(payload)["vote"], "CAUTION")
        with self.assertRaises(ValueError):
            role_response({"output": []})

    def test_responses_request_has_json_schema_and_does_not_store(self):
        called = []
        payload = {"status": "completed", "output": [{
            "type": "message", "content": [{"type": "output_text",
                "text": '{"vote":"APPROVE","reason":"僅模擬"}'}]}
        ]}
        def opener(req, timeout):
            called.append((req, timeout))
            return FakeModelReply(json.dumps(payload).encode())
        transport = OpenAIResponses("sk-test-only", "gpt-6-luna", opener)
        result, usage = transport.ask("safety", {"mint": MINT}, {})
        self.assertEqual(result["vote"], "APPROVE")
        req, timeout = called[0]
        sent = json.loads(req.data)
        self.assertEqual(req.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(req.get_method(), "POST")
        self.assertFalse(sent["store"])
        self.assertEqual(sent["text"]["format"]["type"], "json_schema")
        self.assertEqual(sent["max_output_tokens"], 250)
        self.assertNotIn("sk-test-only", str(sent))

    def test_six_roles_share_prior_votes_and_keep_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport()
            council = OpenAICouncil(str(Path(tmp) / "paper.db"),
                                    {"PAPER_OPENAI_ENABLED": "1",
                                     "OPENAI_API_KEY": "sk-test-not-real",
                                     "PAPER_OPENAI_MAX_DAILY_CALLS": "6"}, transport)
            result = council.analyze({"mint": MINT, "symbol": "TEST", "state": "PAPER_BUY"})
            self.assertEqual("APPROVED", result["state"])
            self.assertTrue(result["allowed"])
            self.assertEqual([r for r, _ in transport.calls], list(ROLES))
            self.assertEqual([len(p) for _, p in transport.calls], list(range(6)))
            self.assertEqual(council.status()["calls_today"], 6)
            self.assertEqual("ERROR", council.analyze({"mint": "B" * 44})["state"])
            self.assertEqual(len(transport.calls), 6)

    def test_ai_veto_stops_simulated_entry_and_pending_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = replace(Settings(), db_path=str(Path(tmp) / "ledger.db"))
            runtime = PaperRuntime(settings, FakeFeed())
            model = FakeTransport(veto="safety")
            council = OpenAICouncil(settings.db_path,
                                    {"PAPER_OPENAI_ENABLED": "1", "OPENAI_API_KEY": "sk-fake"},
                                    transport=model)
            runtime.openai = council
            first = runtime.run_once(NOW)
            self.assertEqual(first["buys"], [])
            self.assertEqual(council.review(MINT)["state"], "PENDING")
            veto = council.analyze({"mint": MINT, "symbol": "TEST", "state": "PAPER_BUY"})
            self.assertEqual("REJECTED", veto["state"])
            second = runtime.run_once(NOW + 150)
            self.assertEqual(second["buys"], [])
            self.assertEqual(runtime.book.trades(), [])

    def test_approved_ai_still_cannot_override_rule_risk_veto(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = replace(Settings(), db_path=str(Path(tmp) / "ledger.db"),
                               min_liquidity_usd=250000)
            runtime = PaperRuntime(settings, FakeFeed())
            council = OpenAICouncil(settings.db_path,
                                    {"PAPER_OPENAI_ENABLED": "1", "OPENAI_API_KEY": "sk-fake"},
                                    transport=FakeTransport())
            runtime.openai = council
            council.analyze({"mint": MINT, "symbol": "TEST"})
            res = runtime.run_once(NOW)
            self.assertEqual(res["buys"], [])
            self.assertEqual(runtime.book.trades(), [])

if __name__ == "__main__":
    unittest.main()
