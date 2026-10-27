"""Seventh JUDGE shadow lab: fact-only dossier, closed answers, and A/B paper log.

No LLM calls, no Jev API, no orders. Stage B uses a separate SQLite paper
ledger, while stage A continues exactly as before. Missing is UNKNOWN, never
guessed, and "confidence" is not invented. All outcomes require later quotes.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path

LOG = logging.getLogger("solana_paper.shadow_judge")
HORIZON = 86400.0
RECHECK = 1800.0
FIRST_HOUR = 3600.0

FIELDS = (
    "chain", "age_minutes", "market_cap_usd", "liquidity_usd",
    "holders_now", "holders_1h_ago", "price_change_1h_pct",
    "buys_last_5m", "sells_last_5m", "buys_prev_5m", "sells_prev_5m",
    "top10_share_pct", "dev_share_pct", "risk_result",
    "risk_checks", "whale_buy_wallets", "whale_buy_timestamps",
    "social_mentions_now", "social_mentions_1h_ago",
    "insider_net_sell_pct",
)


def fact(value):
    if value is None or value == "" or value == "UNKNOWN":
        return "missing"
    if isinstance(value, (int, float)) and not math.isfinite(value):
        return "missing"
    return value


def dossier(token, now):
    agents = token.get("agents") or {}
    safety = agents.get("safety") or {}
    smart = agents.get("smart_money") or {}
    chain = safety.get("onchain") or {"state": "UNKNOWN"}
    report = {
        "chain": "solana",
        "age_minutes": fact(token.get("age_minutes")),
        "market_cap_usd": fact(token.get("market_cap")),
        "fdv_usd": fact(token.get("fdv")),
        "liquidity_usd": fact(token.get("liquidity_usd")),
        "holders_now": "missing",
        "holders_1h_ago": "missing",
        "price_change_1h_pct": fact(token.get("h1_change")),
        "buys_last_5m": "missing",
        "sells_last_5m": "missing",
        "buys_prev_5m": "missing",
        "sells_prev_5m": "missing",
        "top10_share_pct": "missing",
        "dev_share_pct": "missing",
        "risk_result": "REJECT" if not safety.get("passed") else "PARTIALLY_SCREENED",
        "risk_checks": {
            "market_liquidity": "CHECKED",
            "pool_age": "CHECKED",
            "mint_freeze": chain.get("state", "UNKNOWN"),
            "lp_rug": "missing", "holders": "missing", "dev_associates": "missing",
        },
        # Holding is NOT evidence of a recent buy or winning trader.
        "whale_buy_wallets": "missing",
        "whale_buy_timestamps": "missing",
        "social_mentions_now": "missing",
        "social_mentions_1h_ago": "missing",
        "insider_net_sell_pct": "missing",
        "watchlist_holding_count": int(smart.get("matched_wallets", 0)),
        "price_usd": fact(token.get("price")),
        "observed_at": now,
    }
    report["missing_fields"] = [key for key in FIELDS if report[key] == "missing"]
    return report


def judge_four_plus_one(d, token):
    """Closed questions with explicit UNKNOWN where observations are absent.

    This is a conservative deterministic prototype, NOT Jev/LLM output. Values
    are not probability estimates; gate requires verified evidence.
    """
    answers = {
        "organic": {"answer": "UNKNOWN", "probability": None,
                    "why": "缺少持有人變化及兩個獨立 5m 買賣窗口"},
        "late": {"answer": "UNKNOWN", "probability": None,
                 "why": "缺少早於進場訊號的價格路徑與可比較的時間序列"},
        "exit_risk": {"answer": "UNKNOWN", "score": None,
                      "why": "尚未取得指定買賣數量的可執行滑價與退出深度"},
        "insider_exit": {"answer": "UNKNOWN", "probability": None,
                         "why": "沒有開發者、狙擊與關聯錢包的已驗證淨賣出"},
        "action": {"answer": "WATCH", "probability": None,
                   "why": "核心籌碼與可退出性資料不足；僅做影子觀察"},
    }
    risk = token.get("agents", {}).get("safety") or {}
    if risk.get("state") == "REJECT" or (risk.get("onchain") or {}).get("state") == "RISK":
        answers["action"] = {"answer": "PASS", "probability": None,
                             "why": "原有風險代理已否決，不進入有效裁判候選"}
    elif token.get("h1_change", 0) > 60 or token.get("m5_change", 0) > 15:
        answers["late"] = {"answer": "YES", "probability": None,
                           "why": "短時間暴衝，僅為防追價規則，不是機率預測"}
        answers["action"] = {"answer": "PASS", "probability": None,
                             "why": "價格短時間暴衝，影子測試暫不追價"}
    return answers


def gate(answers, dossier_data, original_allowed):
    """The article's 0.70/0.65 are NOT imitated with uncalibrated numbers."""
    if not original_allowed:
        return "PASS", "原始規則已否決"
    if answers["action"]["answer"] == "PASS":
        return "PASS", answers["action"]["why"]
    organic, late, exit_risk, insider = (
        answers["organic"], answers["late"], answers["exit_risk"], answers["insider_exit"])
    action = answers["action"]
    if (action["answer"] == "BUY"
        and isinstance(action.get("probability"), (int, float))
        and action["probability"] >= .70
        and organic["answer"] == "YES"
        and isinstance(organic.get("probability"), (int, float))
        and organic["probability"] >= .65
        and late["answer"] == "NO"
        and isinstance(exit_risk.get("score"), int)
        and exit_risk["score"] <= 3
        and insider["answer"] == "NO"
        and dossier_data["risk_result"] == "PARTIALLY_SCREENED"
        and dossier_data["whale_buy_wallets"] != "missing"
        and dossier_data["whale_buy_timestamps"] != "missing"
        and dossier_data["holders_now"] != "missing"
        and dossier_data["top10_share_pct"] != "missing"
    ):
        return "BUY", "五題皆有足夠證據，符合固定 Gate（僅 B 組模擬）"
    return "WATCH", "缺少可核驗證據或裁判信心尚未校準，30 分鐘後只複審一次"


class ShadowJudge:
    def __init__(self, settings):
        # No real order is possible. B has separate cash, positions, fees, exits.
        from .engine import PaperBook, ExecutionAgent
        self.s = settings
        self.db_path = str(Path(settings.db_path).with_suffix(".shadow_judge.sqlite3"))
        self.book = PaperBook(replace(settings, db_path=self.db_path))
        self.execution = ExecutionAgent(self.book, settings)
        self.lock = threading.RLock()
        self._ensure()

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=8)
        db.row_factory = sqlite3.Row
        return db

    def _ensure(self):
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS judge_leads (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              mint TEXT NOT NULL UNIQUE,
              symbol TEXT NOT NULL,
              observed_at REAL NOT NULL,
              first_price REAL NOT NULL,
              last_seen_at REAL NOT NULL,
              last_price REAL NOT NULL,
              min_price_24h REAL NOT NULL,
              max_price_24h REAL NOT NULL,
              action TEXT NOT NULL,
              final INTEGER NOT NULL,
              reviews INTEGER NOT NULL,
              review_at REAL NOT NULL,
              dossier_json TEXT NOT NULL,
              answers_json TEXT NOT NULL,
              reason TEXT NOT NULL,
              outcome_24h TEXT NOT NULL DEFAULT 'PENDING',
              price_at_24h REAL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_judge_lead_at ON judge_leads(observed_at)")

    def _review_lead(self, db, token, now, allowed):
        mint, price = str(token.get("mint", "")), token.get("price", 0)
        if not mint or not isinstance(price, (float, int)) or price <= 0:
            return None
        row = db.execute("SELECT * FROM judge_leads WHERE mint=?", (mint,)).fetchone()
        d = dossier(token, now)
        answers = judge_four_plus_one(d, token)
        result, reason = gate(answers, d, allowed)
        if not row:
            final = result != "WATCH"
            db.execute(
                """INSERT INTO judge_leads
                (mint,symbol,observed_at,first_price,last_seen_at,last_price,
                 min_price_24h,max_price_24h,action,final,reviews,review_at,
                 dossier_json,answers_json,reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mint,str(token.get("symbol","?"))[:24],now,price,now,price,price,price,
                 result,int(final),1,now,json.dumps(d,ensure_ascii=False),
                 json.dumps(answers,ensure_ascii=False),reason))
            return {"state": result, "reason": reason, "review_count": 1, "lead_id": db.execute("SELECT last_insert_rowid()").fetchone()[0]}
        if row["outcome_24h"] == "PENDING" and 0 <= now - row["observed_at"] <= HORIZON:
            db.execute("""UPDATE judge_leads
                          SET last_seen_at=?,last_price=?,
                              min_price_24h=MIN(min_price_24h, ?),
                              max_price_24h=MAX(max_price_24h, ?)
                          WHERE id=?""", (now,price,price,price,row["id"]))
        if row["outcome_24h"] == "PENDING" and HORIZON <= now - row["observed_at"] <= HORIZON + FIRST_HOUR:
            # A quote *near* the 24h boundary is required for comparison.
            db.execute("UPDATE judge_leads SET outcome_24h=?,price_at_24h=?,last_seen_at=?,last_price=? WHERE id=?",
                       ("24H_QUOTE_OBSERVED",price,now,price,row["id"]))
        if row["final"] or row["reviews"] >= 2:
            return {"state": row["action"], "reason": row["reason"], "review_count": row["reviews"], "lead_id": row["id"]}
        if now - row["review_at"] < RECHECK:
            return {"state": "WATCH", "reason": row["reason"], "review_count": 1, "lead_id": row["id"]}
        # WATCH may be revisited only once, never looped hoping for YES.
        result, reason = gate(answers,d,allowed)
        if result == "WATCH":
            result,reason = "PASS","第二次審查仍缺乏資料，按規則 PASS"
        db.execute("""UPDATE judge_leads SET action=?,final=1,reviews=2,review_at=?,
                   dossier_json=?,answers_json=?,reason=? WHERE id=?""",
                   (result,now,json.dumps(d,ensure_ascii=False),
                    json.dumps(answers,ensure_ascii=False),reason,row["id"]))
        return {"state": result, "reason": reason, "review_count": 2, "lead_id": row["id"]}

    def process(self, tokens, quotes, now):
        """Called from the same fresh scanner snapshot. No extra API calls."""
        with self.lock:
            try:
                self.execution.review_positions(quotes, now)
                events = []
                with self._connect() as db:
                    for token in tokens[:30]:
                        original_allowed = token.get("state") == "PAPER_BUY"
                        verdict = self._review_lead(db, token, now, original_allowed)
                        if not verdict:
                            continue
                        token["shadow_judge"] = verdict
                        events.append(verdict)
                        # This cannot affect A: completely separate paper ledger.
                        if (verdict["state"] == "BUY" and
                            original_allowed and
                            self.execution.approve(token, now)[0]):
                            self.book.buy(token, now)
                return events
            except Exception:
                LOG.exception("shadow judge unavailable; baseline trading unaffected")
                return []

    def monitor(self, quotes, now):
        with self.lock:
            try:
                return self.execution.review_positions(quotes, now)
            except Exception:
                LOG.exception("shadow paper position monitoring failed")
                return []

    def positions(self):
        return self.book.positions()

    def summary(self):
        with self.lock:
            with self._connect() as db:
                tally = {row["action"]: row["n"] for row in db.execute(
                    "SELECT action,COUNT(*) n FROM judge_leads GROUP BY action")}
                mature = db.execute(
                    "SELECT COUNT(*) FROM judge_leads WHERE outcome_24h='24H_QUOTE_OBSERVED'"
                ).fetchone()[0]
                eligible = db.execute(
                    "SELECT COUNT(*) FROM judge_leads WHERE observed_at<=?",
                    (time.time() - HORIZON,)).fetchone()[0]
                records = [
                    {"lead_id": row["id"], "symbol": row["symbol"],
                     "mint": row["mint"], "at": row["observed_at"],
                     "action": row["action"], "reviews": row["reviews"],
                     "reason": row["reason"],
                     "outcome_24h": row["outcome_24h"],
                     "change_24h_pct": (round((row["price_at_24h"] / row["first_price"] - 1) * 100, 2)
                         if row["price_at_24h"] and row["first_price"] > 0 else None)}
                    for row in db.execute(
                        "SELECT * FROM judge_leads ORDER BY id DESC LIMIT 10")
                ]
                return {
                    "mode": "DETERMINISTIC_SHADOW_ONLY",
                    "is_ai": False, "is_jev": False,
                    "uses_paid_api": False, "affects_baseline": False,
                    "questions": ["organic","late","exit_risk","action","insider_exit"],
                    "totals": tally,
                    "24h_matured_leads": eligible,
                    "24h_verified_quotes": mature,
                    "24h_missing_quotes": max(0,eligible-mature),
                    "open_positions_b": len(self.book.positions()),
                    "bank_b_usd": round(self.book.equity(),2),
                    "bank_b_cash_usd": round(self.book.cash(),2),
                    "recent": records,
                    "note": "A組原策略不變；B組獨立帳本只在證據齊備的BUY才模擬。未驗證欄位全部 missing。沒有24小時真實報價不宣稱RUG或獲利。SQLite在Render Free可能重置。",
                }
