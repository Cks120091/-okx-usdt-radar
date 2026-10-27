"""Paper-only Solana meme-token research and simulated fills.

No private keys, transaction construction, signing or order submission.
All prices are indicative DexScreener USD quotes, not executable quotes.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

LOG = logging.getLogger("solana_paper")
SOLANA_MINT = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
KNOWN_QUOTES = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCER2wy3f1HyJgAxU8K8k7hPwWNhbKaZbN9D",
}


def num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError, OverflowError):
        return default


def fmt(value: float) -> float:
    return round(float(value), 7)


@dataclass(frozen=True)
class Settings:
    initial_cash: float = 1000.0
    trade_usd: float = 50.0
    max_positions: int = 3
    max_allocated_pct: float = 0.20
    daily_loss_limit_usd: float = 50.0
    min_liquidity_usd: float = 25000.0
    min_volume_h1_usd: float = 5000.0
    stop_loss_pct: float = 12.0
    take_profit_pct: float = 20.0
    max_hold_minutes: int = 360
    fee_pct_per_side: float = 0.30
    slippage_pct_per_side: float = 0.80
    scan_seconds: int = 150
    db_path: str = "data/solana_paper.sqlite3"

    @classmethod
    def load(cls) -> "Settings":
        # Non-secret, strictly clamped paper-only parameters.
        def get(name: str, default: Any, minimum: float, maximum: float, kind=float):
            try:
                return kind(max(minimum, min(maximum, float(os.getenv(name, default)))))
            except (ValueError, TypeError):
                return kind(default)
        return cls(
            initial_cash=get("PAPER_INITIAL_CASH", 1000, 100, 100000),
            trade_usd=get("PAPER_TRADE_USD", 50, 1, 1000),
            max_positions=get("PAPER_MAX_POSITIONS", 3, 1, 10, int),
            max_allocated_pct=get("PAPER_MAX_ALLOCATION_PCT", 20, 1, 100) / 100,
            daily_loss_limit_usd=get("PAPER_DAILY_LOSS_USD", 50, 1, 10000),
            min_liquidity_usd=get("PAPER_MIN_LIQUIDITY_USD", 25000, 1000, 10000000),
            min_volume_h1_usd=get("PAPER_MIN_VOLUME_H1_USD", 5000, 100, 10000000),
            stop_loss_pct=get("PAPER_STOP_LOSS_PCT", 12, 1, 90),
            take_profit_pct=get("PAPER_TAKE_PROFIT_PCT", 20, 1, 500),
            max_hold_minutes=get("PAPER_MAX_HOLD_MINUTES", 360, 10, 10080, int),
            fee_pct_per_side=get("PAPER_FEE_PCT", .30, 0, 5),
            slippage_pct_per_side=get("PAPER_SLIPPAGE_PCT", .80, 0, 10),
            scan_seconds=get("PAPER_SCAN_SECONDS", 150, 60, 3600, int),
            db_path=os.getenv("PAPER_DB_PATH", "data/solana_paper.sqlite3"),
        )


class DexFeed:
    """DEX Screener official public REST. Discovery is NOT exhaustive."""
    BASE = "https://api.dexscreener.com"

    def _get(self, path: str) -> Any:
        request = Request(self.BASE + path, headers={
            "Accept": "application/json",
            "User-Agent": "Solana-Paper-Research/1.0",
        })
        with urlopen(request, timeout=12) as response:
            if int(response.status) != 200:
                raise RuntimeError("DEX HTTP status not 200")
            raw = response.read(2000000)
            if len(raw) >= 2000000:
                raise ValueError("API payload too large")
            return json.loads(raw)

    @staticmethod
    def valid(mint: Any) -> bool:
        return isinstance(mint, str) and SOLANA_MINT.fullmatch(mint) is not None and mint not in KNOWN_QUOTES

    def discover(self) -> dict:
        profiles = self._get("/token-profiles/latest/v1")
        boosts = self._get("/token-boosts/latest/v1")
        if not isinstance(profiles, list) or not isinstance(boosts, list):
            raise ValueError("Unexpected discovery API response")
        mints = []
        paid = {}
        sources = {}
        for label, items in (("profile", profiles), ("paid_boost", boosts)):
            for item in items:
                if not isinstance(item, dict) or str(item.get("chainId", "")).lower() != "solana":
                    continue
                mint = item.get("tokenAddress")
                if not self.valid(mint):
                    continue
                if mint not in mints and len(mints) < 30:
                    mints.append(mint)
                sources.setdefault(mint, set()).add(label)
                if label == "paid_boost":
                    paid[mint] = num(item.get("totalAmount"))
        if not mints:
            return {"pairs": [], "mints": [], "paid": {}, "sources": {}}
        addresses = ",".join(quote(mint, safe="") for mint in mints)
        pairs = self._get("/tokens/v1/solana/" + addresses)
        if not isinstance(pairs, list):
            raise ValueError("Unexpected pool API response")
        return {
            "pairs": pairs,
            "mints": mints,
            "paid": paid,
            "sources": {k: sorted(v) for k, v in sources.items()},
        }

    def quotes(self, mints: list[str]) -> list[dict]:
        if not mints:
            return []
        items = [mint for mint in mints if self.valid(mint)]
        if not items:
            return []
        output = []
        for i in range(0, len(items), 30):
            path = "/tokens/v1/solana/" + ",".join(quote(mint, safe="") for mint in items[i:i+30])
            data = self._get(path)
            if not isinstance(data, list):
                raise ValueError("Unexpected quote API response")
            output.extend(item for item in data if isinstance(item, dict))
        return output


def normalize_pairs(raw: list[dict], allowed: list[str], now: float) -> dict[str, dict]:
    """Choose highest-liquidity BASE pool per mint (avoid quote-token misidentification)."""
    allowed_mints = set(allowed)
    chosen = {}
    for pair in raw:
        if not isinstance(pair, dict) or str(pair.get("chainId", "")).lower() != "solana":
            continue
        base = pair.get("baseToken") or {}
        mint = base.get("address") if isinstance(base, dict) else None
        if mint not in allowed_mints:
            continue
        liquidity = num((pair.get("liquidity") or {}).get("usd"))
        price = num(pair.get("priceUsd"))
        created = num(pair.get("pairCreatedAt"))
        if not liquidity > 0 or not price > 0 or created <= 0:
            continue
        age_mins = max(0, (now * 1000 - created) / 60000)
        volume = pair.get("volume") or {}
        txns = pair.get("txns") or {}
        price_change = pair.get("priceChange") or {}
        candidate = {
            "mint": mint,
            "symbol": str(base.get("symbol") or "?")[:22],
            "name": str(base.get("name") or "")[:50],
            "pair": str(pair.get("pairAddress") or "")[:80],
            "dex": str(pair.get("dexId") or "")[:35],
            "price": price,
            "liquidity_usd": liquidity,
            "volume_h1": num(volume.get("h1")),
            "volume_h24": num(volume.get("h24")),
            "m5_change": num(price_change.get("m5")),
            "h1_change": num(price_change.get("h1")),
            "h1_buys": max(0, int(num((txns.get("h1") or {}).get("buys")))),
            "h1_sells": max(0, int(num((txns.get("h1") or {}).get("sells")))),
            "age_minutes": round(age_mins, 1),
            "fdv": num(pair.get("fdv")),
            "market_cap": num(pair.get("marketCap")),
        }
        if mint not in chosen or liquidity > chosen[mint]["liquidity_usd"]:
            chosen[mint] = candidate
    return chosen


class DiscoveryAgent:
    name = "01｜獵幣"
    def evaluate(self, token: dict, sources: list[str]) -> dict:
        return {"state": "OBSERVED", "sources": sources, "message": "來自最新代幣資料／付費 Boost 清單，不代表全鏈新幣"}


class SafetyAgent:
    name = "02｜風險審核"
    def __init__(self, settings: Settings):
        self.s = settings

    def evaluate(self, t: dict) -> dict:
        failures = []
        if t["liquidity_usd"] < self.s.min_liquidity_usd:
            failures.append("流動性不足")
        if t["age_minutes"] < 10:
            failures.append("池子建立未滿10分鐘")
        if t["age_minutes"] > 365 * 24 * 60:
            failures.append("池齡資料異常")
        if t["fdv"] > 0 and t["fdv"] / max(t["liquidity_usd"], 1) > 100:
            failures.append("FDV／流動性比過高")
        return {
            "state": "REJECT" if failures else "MARKET_SCREENED",
            "passed": not failures,
            "reasons": failures,
            "contract_security": "UNVERIFIED",
            "message": "只檢查市場可見風險；合約權限、持幣集中度與 Rug 風險尚未經鏈上驗證",
        }


class SmartMoneyAgent:
    name = "03｜聰明錢"
    def evaluate(self, t: dict) -> dict:
        return {"state": "NO_DATA", "score_used": False, "message": "尚未串接錢包獲利與地址關聯資料，不推測巨鯨動向"}


class SocialAgent:
    name = "04｜社群"
    def evaluate(self, t: dict, boost: float) -> dict:
        return {
            "state": "PAID_MARKETING_ONLY" if boost > 0 else "NO_DATA",
            "boost_amount": boost,
            "score_used": False,
            "message": "Boost 是付費曝光，不代表真實社群人氣；尚未接入 X 貼文語意分析",
        }


class DecisionAgent:
    name = "05｜決策"
    def __init__(self, settings: Settings):
        self.s = settings

    def evaluate(self, t: dict, safety: dict) -> dict:
        if not safety["passed"]:
            return {"state": "REJECT", "score": 0, "reasons": safety["reasons"]}
        score, reasons = 0, []
        liq = t["liquidity_usd"]
        if liq >= 100000:
            score += 25
            reasons.append("流動性達10萬美元")
        else:
            score += 15
        if t["volume_h1"] >= 25000:
            score += 20
            reasons.append("1H成交量放大")
        elif t["volume_h1"] >= self.s.min_volume_h1_usd:
            score += 10
        else:
            reasons.append("1H成交量不足")
        buys, sells = t["h1_buys"], t["h1_sells"]
        ratio = (buys / max(sells, 1)) if buys + sells >= 20 else 0
        if ratio >= 1.35:
            score += 20
            reasons.append("1H買方筆數明顯較多（非實際成交額）")
        elif ratio > 1.1:
            score += 10
        if 1 <= t["m5_change"] <= 15:
            score += 15
            reasons.append("5分鐘正向動能")
        elif 0 < t["m5_change"] < 1:
            score += 5
        elif t["m5_change"] > 15:
            score -= 20
            reasons.append("5分鐘過度暴衝，防追價")
        if 0 < t["h1_change"] <= 60:
            score += 10
        elif t["h1_change"] > 60:
            score -= 10
            reasons.append("1小時漲幅過大")
        if t["age_minutes"] >= 30:
            score += 10
        score = max(0, min(100, score))
        passed = (
            score >= 65 and t["volume_h1"] >= self.s.min_volume_h1_usd
            and 0 < t["m5_change"] <= 15 and 0 < t["h1_change"] <= 60
            and buys + sells >= 20 and ratio > 1.1
        )
        return {
            "state": "PAPER_BUY" if passed else "WATCH",
            "score": score,
            "reasons": reasons,
            "message": "規則評分，非 AI 預測勝率；鏈上安全未驗證，僅允許模擬",
        }


class PaperBook:
    """SQLite auditable paper fills. Never sends a trade to any chain or CEX."""
    def __init__(self, settings: Settings):
        self.s = settings
        Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS positions(
                mint TEXT PRIMARY KEY, symbol TEXT NOT NULL, qty REAL NOT NULL,
                entry REAL NOT NULL, cost REAL NOT NULL, opened_at REAL NOT NULL,
                mark REAL NOT NULL, mark_at REAL NOT NULL
              );
              CREATE TABLE IF NOT EXISTS trades(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
                mint TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
                price REAL NOT NULL, qty REAL NOT NULL, gross REAL NOT NULL,
                fee REAL NOT NULL, pnl REAL, reason TEXT NOT NULL
              );
              CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('cash',?)", (str(settings.initial_cash),))
            db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('start_cash',?)", (str(settings.initial_cash),))

    def connect(self):
        db = sqlite3.connect(self.s.db_path, timeout=8)
        db.row_factory = sqlite3.Row
        return db

    def cash(self) -> float:
        with self.connect() as db:
            return float(db.execute("SELECT value FROM meta WHERE key='cash'").fetchone()[0])

    def positions(self) -> list[dict]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM positions ORDER BY opened_at DESC")]

    def trades(self, limit=40) -> list[dict]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))]

    def equity(self) -> float:
        return self.cash() + sum(
            p["qty"] * p["mark"] * (1 - self.s.slippage_pct_per_side / 100)
            * (1 - self.s.fee_pct_per_side / 100) for p in self.positions()
        )

    def bought_recently(self, mint: str, now: float) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT MAX(ts) FROM trades WHERE mint=?", (mint,)).fetchone()
            return row[0] is not None and now - row[0] < 3600

    def mark(self, mint: str, price: float, now: float) -> None:
        if price <= 0:
            return
        with self.connect() as db:
            db.execute("UPDATE positions SET mark=?,mark_at=? WHERE mint=?", (price, now, mint))

    def buy(self, t: dict, now: float) -> bool:
        """Accounting uses gross quote notional plus fee. All USD is simulated."""
        size = self.s.trade_usd
        fill = t["price"] * (1 + self.s.slippage_pct_per_side / 100)
        fee = size * self.s.fee_pct_per_side / 100
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cash = float(db.execute("SELECT value FROM meta WHERE key='cash'").fetchone()[0])
            if cash < size + fee or db.execute("SELECT 1 FROM positions WHERE mint=?", (t["mint"],)).fetchone():
                db.rollback()
                return False
            qty = size / fill
            db.execute("UPDATE meta SET value=? WHERE key='cash'", (str(cash - size - fee),))
            db.execute(
                "INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)",
                (t["mint"], t["symbol"], qty, fill, size + fee, now, t["price"], now)
            )
            db.execute("INSERT INTO trades(ts,mint,symbol,side,price,qty,gross,fee,pnl,reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, t["mint"], t["symbol"], "BUY", fill, qty, size, fee, None, "RULE_SIGNAL"))
        return True

    def sell(self, pos: dict, quote_price: float, reason: str, now: float) -> float | None:
        if quote_price <= 0:
            return None
        fill = quote_price * (1 - self.s.slippage_pct_per_side / 100)
        gross = pos["qty"] * fill
        fee = gross * self.s.fee_pct_per_side / 100
        pnl = gross - fee - pos["cost"]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            present = db.execute("SELECT 1 FROM positions WHERE mint=?", (pos["mint"],)).fetchone()
            if not present:
                db.rollback()
                return None
            cash = float(db.execute("SELECT value FROM meta WHERE key='cash'").fetchone()[0])
            db.execute("UPDATE meta SET value=? WHERE key='cash'", (str(cash + gross - fee),))
            db.execute("DELETE FROM positions WHERE mint=?", (pos["mint"],))
            db.execute("INSERT INTO trades(ts,mint,symbol,side,price,qty,gross,fee,pnl,reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now, pos["mint"], pos["symbol"], "SELL", fill, pos["qty"], gross, fee, pnl, reason))
        return pnl

    def realized_today(self, now: float) -> float:
        # UTC day boundary (clearly labelled in UI).
        today = dt.datetime.fromtimestamp(now, dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        with self.connect() as db:
            row = db.execute("SELECT SUM(pnl) FROM trades WHERE side='SELL' AND ts>=?", (today,)).fetchone()
            return float(row[0] or 0)


class ExecutionAgent:
    name = "06｜紙上交易"
    def __init__(self, book: PaperBook, settings: Settings):
        self.book, self.s = book, settings

    def approve(self, token: dict, now: float) -> tuple[bool, str]:
        positions = self.book.positions()
        if len(positions) >= self.s.max_positions:
            return False, "持倉數上限"
        if self.book.bought_recently(token["mint"], now):
            return False, "同幣1小時冷卻"
        if self.book.realized_today(now) <= -self.s.daily_loss_limit_usd:
            return False, "每日已實現虧損熔斷"
        if self.book.equity() <= self.s.initial_cash - self.s.daily_loss_limit_usd:
            return False, "總淨值回撤熔斷"
        invested = sum(p["cost"] for p in positions)
        if invested + self.s.trade_usd > self.book.equity() * self.s.max_allocated_pct:
            return False, "總模擬倉位上限"
        if self.book.cash() < self.s.trade_usd * (1 + self.s.fee_pct_per_side / 100):
            return False, "模擬餘額不足"
        return True, "可模擬買入"

    def review_positions(self, quotes: dict[str, dict], now: float) -> list[dict]:
        exits = []
        for pos in self.book.positions():
            q = quotes.get(pos["mint"])
            if not q:
                continue   # Missing quote -> stale, never invented fill.
            self.book.mark(pos["mint"], q["price"], now)
            unrealized = (q["price"] * (1 - self.s.slippage_pct_per_side / 100)
                          * (1 - self.s.fee_pct_per_side / 100) / pos["entry"]) - 1
            age_minutes = (now - pos["opened_at"]) / 60
            reason = ("STOP_LOSS" if unrealized <= -self.s.stop_loss_pct / 100
                      else "TAKE_PROFIT" if unrealized >= self.s.take_profit_pct / 100
                      else "MAX_HOLD" if age_minutes >= self.s.max_hold_minutes
                      else "")
            if reason:
                pnl = self.book.sell(pos, q["price"], reason, now)
                if pnl is not None:
                    exits.append({"symbol": pos["symbol"], "reason": reason, "pnl": fmt(pnl)})
        return exits


class PaperRuntime:
    def __init__(self, settings: Settings | None = None, feed: Any | None = None):
        self.s = settings or Settings.load()
        self.feed = feed or DexFeed()
        self.book = PaperBook(self.s)
        self.hunters = DiscoveryAgent()
        self.safety = SafetyAgent(self.s)
        self.wallet = SmartMoneyAgent()
        self.social = SocialAgent()
        self.decision = DecisionAgent(self.s)
        self.execution = ExecutionAgent(self.book, self.s)
        self.lock = threading.Lock()
        self.last_scan = 0.0
        self.last_attempt = 0.0
        self.last_error = ""
        self.scanning = False
        self.candidates: list[dict] = []
        self.events: list[dict] = []
        self._started = False

    def run_once(self, now: float | None = None) -> dict:
        now = now if now is not None else time.time()
        with self.lock:
            self.scanning, self.last_attempt = True, now
        try:
            discovery = self.feed.discover()
            mints = discovery["mints"]
            candidate_quotes = normalize_pairs(discovery["pairs"], mints, now)
            held_mints = [p["mint"] for p in self.book.positions()]
            missing = [mint for mint in held_mints if mint not in candidate_quotes]
            if missing:
                held_quotes = normalize_pairs(self.feed.quotes(missing), missing, now)
                candidate_quotes.update(held_quotes)
            exits = self.execution.review_positions(candidate_quotes, now)
            ranked = []
            for mint in mints:
                t = candidate_quotes.get(mint)
                if not t:
                    continue
                discovery_report = self.hunters.evaluate(t, discovery["sources"].get(mint, []))
                safety = self.safety.evaluate(t)
                money = self.wallet.evaluate(t)
                social = self.social.evaluate(t, discovery["paid"].get(mint, 0))
                decision = self.decision.evaluate(t, safety)
                ranked.append({**t, "score": decision["score"], "state": decision["state"],
                    "agents": {"discovery": discovery_report, "safety": safety,
                               "smart_money": money, "social": social, "decision": decision}})
            ranked.sort(key=lambda x: (x["score"], x["liquidity_usd"]), reverse=True)
            buys = []
            for t in ranked:
                if t["state"] != "PAPER_BUY":
                    continue
                allowed, why = self.execution.approve(t, now)
                if not allowed:
                    t["paper_order"] = why
                    continue
                if self.book.buy(t, now):
                    buys.append({"symbol": t["symbol"], "price": fmt(t["price"]), "usd": self.s.trade_usd})
                    t["paper_order"] = "已模擬買入"
            with self.lock:
                self.candidates = ranked[:30]
                self.events = (buys + exits + self.events)[:20]
                self.last_scan, self.last_error = now, ""
            return {"buys": buys, "exits": exits, "candidates": len(ranked)}
        except Exception as exc:
            LOG.exception("paper scan failed")
            with self.lock:
                self.last_error = "%s: %s" % (type(exc).__name__, str(exc)[:250])
            return {"error": str(exc)}
        finally:
            with self.lock:
                self.scanning = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True

        def loop():
            while True:
                self.run_once()
                time.sleep(self.s.scan_seconds)
        threading.Thread(target=loop, name="solana-paper-scanner", daemon=True).start()

    def status(self) -> dict:
        with self.lock:
            snapshot = {
                "last_scan": self.last_scan,
                "last_attempt": self.last_attempt,
                "last_error": self.last_error,
                "scanning": self.scanning,
                "candidates": self.candidates,
                "events": self.events,
            }
        positions = self.book.positions()
        cash = self.book.cash()
        equity = self.book.equity()
        unrealized = equity - cash - sum(p["cost"] for p in positions)
        return {
            "mode": "PAPER_ONLY",
            "phase": "RULE_BASED_AGENTS_V1",
            "solana_only": True,
            "execution_enabled_on_chain": False,
            "public_dashboard": True,
            "persistent_storage_guaranteed": False,
            "notes": [
                "六代理第一版是規則代理，尚未串接 LLM 或鏈上聰明錢；不是 AI 預測勝率",
                "DexScreener 最新 Profile／Boost 不是全鏈新幣清單，Boost 是付費曝光",
                "合約權限與 Rug 風險未完成鏈上驗證，僅可模擬，不可實盤",
                "成交為延遲行情估算，含固定滑價／費率，不等於真實可成交價",
                "Render 免費服務檔案系統可能重置；當前 SQLite 僅供第一階段測試",
                "本模擬網站為公開可讀狀態；未配置私人存取限制",
            ],
            "agents": [
                {"id": "01", "name": self.hunters.name, "status": "ACTIVE", "detail": "DEX 最新 Profile + 付費 Boost"},
                {"id": "02", "name": self.safety.name, "status": "PARTIAL", "detail": "流動性／池齡／FDV，合約安全未知"},
                {"id": "03", "name": self.wallet.name, "status": "NO_DATA", "detail": "未接錢包地址與鏈上獲利"},
                {"id": "04", "name": self.social.name, "status": "PARTIAL", "detail": "付費曝光指示，未接 X 語意分析"},
                {"id": "05", "name": self.decision.name, "status": "ACTIVE", "detail": "多因子條件與防追價"},
                {"id": "06", "name": self.execution.name, "status": "PAPER_ONLY", "detail": "只寫 SQLite，絕不送單"},
            ],
            "config": asdict(self.s) | {"db_path": "(server local sqlite)"},
            "account": {
                "cash": fmt(cash), "equity": fmt(equity),
                "open_positions": len(positions),
                "unrealized_pnl": fmt(unrealized),
                "realized_today_utc": fmt(self.book.realized_today(time.time())),
                "pnl_since_start": fmt(equity - self.s.initial_cash),
            },
            "positions": [
                {**p, "unrealized_pct": round((p["mark"] / p["entry"] - 1) * 100, 2)}
                for p in positions
            ],
            "trades": self.book.trades(40),
            **snapshot,
        }
