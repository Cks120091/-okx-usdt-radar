"""Seventh JUDGE shadow-only workflow, without network or paid AI."""
from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from solana_paper.engine import Settings, PaperRuntime, normalize_pairs, SafetyAgent
from solana_paper.shadow_judge import ShadowJudge, dossier, judge_four_plus_one, gate

MINT = "A" * 44
NOW = 1793100000.0


def token(price=1.0, mint=MINT):
    t = {
        "mint": mint, "symbol": "TEST", "price": price,
        "liquidity_usd": 150000.0, "market_cap": 800000,
        "fdv": 1000000.0, "volume_h1": 30000,
        "age_minutes": 45.0, "m5_change": 5.0, "h1_change": 20.0,
        "h1_buys": 70, "h1_sells": 30,
        "state": "PAPER_BUY",
    }
    t["agents"] = {
        "safety": {"state": "MARKET_SCREENED", "passed": True,
                   "onchain": {"state": "UNKNOWN"}},
        "market_activity": {"state": "OBSERVED", "h1_buys": 70,
                            "h1_sells": 30, "h1_volume_usd": 30000},
        "social": {"state": "PAID_MARKETING_ONLY"},
    }
    return t


class Feed:
    def discover(self):
        return {"mints": [MINT], "pairs": [{
            "chainId": "solana",
            "baseToken": {"address": MINT, "symbol": "TEST"},
            "pairAddress": "B" * 44, "dexId": "raydium",
            "priceUsd": "1", "liquidity": {"usd": 150000},
            "marketCap": 800000, "fdv": 1000000,
            "pairCreatedAt": int((NOW - 3600) * 1000),
            "volume": {"h1": 40000, "h24": 120000},
            "priceChange": {"m5": 5.0, "h1": 25.0},
            "txns": {"h1": {"buys": 70, "sells": 30}},
        }], "sources": {MINT: ["profile"]}, "paid": {}}
    def quotes(self, mints):
        return []


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = replace(Settings(), db_path=str(Path(self.tmp.name) / "main.db"))
        self.judge = ShadowJudge(self.s)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fact_only_dossier_never_invents_holders_whale_buy_or_rug(self):
        d = dossier(token(), NOW)
        self.assertEqual("missing", d["holders_now"])
        self.assertEqual("missing", d["top10_share_pct"])
        self.assertEqual("missing", d["dev_share_pct"])
        self.assertEqual("OBSERVED", d["market_activity_state"])
        self.assertEqual(70, d["market_buys_h1"])
        self.assertEqual(30, d["market_sells_h1"])
        self.assertEqual(30000, d["market_volume_h1_usd"])
        self.assertEqual("missing", d["insider_net_sell_pct"])
        a = judge_four_plus_one(d,token())
        self.assertEqual("UNKNOWN", a["organic"]["answer"])
        self.assertIsNone(a["organic"]["probability"])
        self.assertIsNone(a["exit_risk"]["score"])
        self.assertEqual("UNKNOWN", a["insider_exit"]["answer"])
        self.assertEqual("WATCH", gate(a,d,True)[0])

    def test_watch_once_after_30_minutes_then_pass_and_remains_final(self):
        t=token()
        self.judge.process([t], {MINT:t}, NOW)
        self.assertEqual("WATCH", t["shadow_judge"]["state"])
        self.assertEqual(1,t["shadow_judge"]["review_count"])
        self.judge.process([token()], {MINT:t}, NOW+1799)
        t2=token()
        self.judge.process([t2], {MINT:t2}, NOW+1800)
        self.assertEqual("PASS", t2["shadow_judge"]["state"])
        self.assertEqual(2,t2["shadow_judge"]["review_count"])
        t3=token()
        self.judge.process([t3], {MINT:t3}, NOW+3600)
        self.assertEqual("PASS", t3["shadow_judge"]["state"])
        self.assertEqual(2,t3["shadow_judge"]["review_count"])
        self.assertEqual([], self.judge.book.trades())

    def test_definitive_risk_pass_and_no_shadow_trade(self):
        t=token()
        t["agents"]["safety"]["onchain"]["state"]="RISK"
        t["agents"]["safety"]["passed"]=False
        t["agents"]["safety"]["state"]="REJECT"
        self.judge.process([t], {MINT:t}, NOW)
        self.assertEqual("PASS", t["shadow_judge"]["state"])
        self.assertEqual(0,self.judge.summary()["open_positions_b"])
        self.assertFalse(self.judge.summary()["is_jev"])

    def test_shadow_24h_truth_requires_observed_quote(self):
        t=token()
        self.judge.process([t],{MINT:t},NOW)
        p=token(price=.65)
        self.judge.process([p],{MINT:p},NOW+86410)
        info=self.judge.summary()
        self.assertEqual(1,info["24h_verified_quotes"])
        self.assertEqual(-35.0,info["recent"][0]["change_24h_pct"])
        self.assertEqual("24H_QUOTE_OBSERVED",info["recent"][0]["outcome_24h"])
        self.assertNotIn("rug",info["recent"][0])

    def test_missing_24h_quote_not_labeled_green_or_rug(self):
        t=token()
        self.judge.process([t],{MINT:t},NOW)
        p=token(price=.5)
        self.judge.process([p],{MINT:p},NOW+86400+3700)
        info=self.judge.summary()
        self.assertEqual(0,info["24h_verified_quotes"])
        self.assertIsNone(info["recent"][0]["change_24h_pct"])

    def test_b_ledger_isolated_from_a_and_requires_verified_gate(self):
        from solana_paper.engine import PaperBook
        a=PaperBook(self.s)
        t=token()
        d=dossier(t,NOW)
        d.update({
            "risk_result":"PARTIALLY_SCREENED",
            "holders_now":450,
            "top10_share_pct":13,
        })
        answers={
            "organic":{"answer":"YES","probability":.8},
            "late":{"answer":"NO","probability":.9},
            "exit_risk":{"answer":"LOW","score":2},
            "insider_exit":{"answer":"NO","probability":.8},
            "action":{"answer":"BUY","probability":.75}
        }
        self.assertEqual("BUY",gate(answers,d,True)[0])
        self.assertEqual("PASS",gate(answers,d,False)[0])
        with patch("solana_paper.shadow_judge.dossier",return_value=d), \
             patch("solana_paper.shadow_judge.judge_four_plus_one",return_value=answers):
            self.judge.process([t],{MINT:t},NOW)
        self.assertEqual(1,len(self.judge.book.trades()))
        self.assertEqual([],a.trades())
        self.assertEqual(1000,a.cash())
        self.assertLess(self.judge.book.cash(),1000)
        self.assertGreaterEqual(self.judge.book.cash(),self.s.cash_reserve_usd)

    def test_real_runtime_a_trades_unchanged_and_b_doesnt_trade_on_missing_fields(self):
        rt=PaperRuntime(self.s, Feed())
        self.assertEqual(1,len(rt.run_once(NOW)["buys"]))
        self.assertEqual(1,len(rt.book.trades()))
        self.assertEqual([],rt.shadow_judge.book.trades())
        state=rt.status()
        self.assertEqual(6,len(state["agents"]))
        self.assertEqual("DETERMINISTIC_SHADOW_ONLY",state["shadow_judge"]["mode"])
        self.assertFalse(state["shadow_judge"]["uses_paid_api"])
        self.assertFalse(state["shadow_judge"]["affects_baseline"])
        self.assertEqual(0,state["shadow_judge"]["open_positions_b"])

if __name__=="__main__":
    unittest.main()
