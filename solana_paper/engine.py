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

from .intelligence import MintRiskObserver
from .council import PaperCouncil
from .feed_control import SHARED_GATE, MarketTemporarilyUnavailable
from .openai_agent import OpenAICouncil
from .shadow_judge import ShadowJudge
from .realtime import StateSignal
from .strong_watch import EarlyStrengthWatch
from .chain_stream import LiveChainObserver

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
    trade_usd: float = 40.0
    max_positions: int = 20  # simultaneous open positions, not trades per day
    max_allocated_pct: float = 0.90
    cash_reserve_usd: float = 50.0
    simulated_network_fee_usd: float = 0.02
    daily_loss_limit_usd: float = 50.0
    min_liquidity_usd: float = 25000.0
    min_volume_h1_usd: float = 5000.0
    stop_loss_pct: float = 12.0
    take_profit_pct: float = 20.0
    max_hold_minutes: int = 360
    fee_pct_per_side: float = 0.30
    slippage_pct_per_side: float = 0.80
    scan_seconds: int = 150
    monitor_seconds: int = 30  # held-position quote checks, separate from universe scans
    monitor_stale_seconds: int = 120
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
            trade_usd=get("PAPER_TRADE_USD", 40, 1, 1000),
            max_positions=get("PAPER_MAX_POSITIONS", 20, 3, 20, int),
            max_allocated_pct=get("PAPER_MAX_ALLOCATION_PCT", 90, 1, 100) / 100,
            cash_reserve_usd=get("PAPER_CASH_RESERVE_USD", 50, 0, 10000),
            simulated_network_fee_usd=get("PAPER_NETWORK_FEE_USD", .02, 0, 2),
            daily_loss_limit_usd=get("PAPER_DAILY_LOSS_USD", 50, 1, 10000),
            min_liquidity_usd=get("PAPER_MIN_LIQUIDITY_USD", 25000, 1000, 10000000),
            min_volume_h1_usd=get("PAPER_MIN_VOLUME_H1_USD", 5000, 100, 10000000),
            stop_loss_pct=get("PAPER_STOP_LOSS_PCT", 12, 1, 90),
            take_profit_pct=get("PAPER_TAKE_PROFIT_PCT", 20, 1, 500),
            max_hold_minutes=get("PAPER_MAX_HOLD_MINUTES", 360, 10, 10080, int),
            fee_pct_per_side=get("PAPER_FEE_PCT", .30, 0, 5),
            slippage_pct_per_side=get("PAPER_SLIPPAGE_PCT", .80, 0, 10),
            scan_seconds=get("PAPER_SCAN_SECONDS", 150, 60, 3600, int),
            monitor_seconds=get("PAPER_MONITOR_SECONDS", 30, 15, 300, int),
            monitor_stale_seconds=get("PAPER_MONITOR_STALE_SECONDS", 120, 60, 900, int),
            db_path=os.getenv("PAPER_DB_PATH", "data/solana_paper.sqlite3"),
        )


class DexFeed:
    """DEX Screener official public REST. Discovery is NOT exhaustive."""
    BASE = "https://api.dexscreener.com"

    def __init__(self, gate=None):
        self.gate = gate if gate is not None else SHARED_GATE

    def _get(self, path: str) -> Any:
        # Profile/boost lists change slowly; prices have a shorter fresh TTL.
        ttl = 90 if path.startswith(("/token-profiles/", "/token-boosts/")) else 25
        return self.gate.get("DEX_SCREENER", path, {
            "Accept": "application/json",
            "User-Agent": "Solana-Paper-Research/1.1",
        }, ttl=ttl)

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



class GeckoFeed:
    """Secondary public price/pool feed. GeckoTerminal API v2 (beta)."""
    BASE = "https://api.geckoterminal.com/api/v2"

    def __init__(self, gate=None):
        self.gate = gate if gate is not None else SHARED_GATE
        self.known_pool: dict[str, str] = {}

    def _get(self, path: str) -> dict:
        ttl = 180 if "/new_pools" in path or "/trending_pools" in path else 30
        data = self.gate.get("GECKOTERMINAL", path, {
            "Accept": "application/json;version=20230203",
            "User-Agent": "Solana-Paper-Research/1.1",
        }, ttl=ttl)
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ValueError("Gecko response missing pools")
        return data

    @staticmethod
    def translate(payload: dict, now: float) -> list[dict]:
        """Map Gecko JSONAPI pool shape to the shared DEX pair contract."""
        included = {}
        for entry in payload.get("included") or []:
            if isinstance(entry, dict):
                included[entry.get("id")] = entry.get("attributes") or {}
        result = []
        for pool in payload.get("data") or []:
            if not isinstance(pool, dict):
                continue
            attrs, relationships = pool.get("attributes") or {}, pool.get("relationships") or {}
            base_ref = ((relationships.get("base_token") or {}).get("data") or {})
            base_id = str(base_ref.get("id") or "")
            if not base_id.startswith("solana_"):
                continue
            mint = base_id[len("solana_"):]
            if not DexFeed.valid(mint):
                continue
            base_attrs = included.get(base_id, {})
            created = attrs.get("pool_created_at")
            try:
                created_ms = int(dt.datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp() * 1000)
            except (ValueError, TypeError, OverflowError):
                continue
            if created_ms <= 0 or created_ms > (now + 300) * 1000:
                continue
            price = num(attrs.get("base_token_price_usd"))
            liq = num(attrs.get("reserve_in_usd"))
            if price <= 0 or liq <= 0:
                continue
            txns = attrs.get("transactions") or {}
            volume = attrs.get("volume_usd") or {}
            change = attrs.get("price_change_percentage") or {}
            pool_name = str(attrs.get("name") or "")
            symbol = str(base_attrs.get("symbol") or pool_name.split("/")[0].strip() or "?")[:22]
            result.append({
                "chainId": "solana",
                "baseToken": {"address": mint, "symbol": symbol,
                              "name": str(base_attrs.get("name") or pool_name)[:50]},
                "pairAddress": str(attrs.get("address") or "")[:80],
                "dexId": str(((relationships.get("dex") or {}).get("data") or {}).get("id") or "gecko")[:35],
                "priceUsd": str(price),
                "liquidity": {"usd": liq},
                "fdv": num(attrs.get("fdv_usd")),
                "marketCap": num(attrs.get("market_cap_usd")),
                "pairCreatedAt": created_ms,
                "volume": {"h1": num(volume.get("h1")), "h24": num(volume.get("h24"))},
                "priceChange": {"m5": num(change.get("m5")), "h1": num(change.get("h1"))},
                "txns": {"h1": {
                    "buys": max(0, int(num((txns.get("h1") or {}).get("buys")))),
                    "sells": max(0, int(num((txns.get("h1") or {}).get("sells")))),
                }},
            })
        return result

    def discover(self) -> dict:
        now = time.time()
        # Two discovery baskets; both constrained and not a census of all Solana launches.
        data_new = self._get("/networks/solana/new_pools?include=base_token")
        try:
            data_trending = self._get("/networks/solana/trending_pools?include=base_token")
        except Exception as exc:
            LOG.warning("Gecko trending discovery unavailable, use new pools: %s", exc)
            data_trending = {"data": []}
        raw = []
        sources = {}
        mints = []
        for label, payload in (("gecko_new_pool", data_new), ("gecko_trending", data_trending)):
            for item in self.translate(payload, now):
                mint = item["baseToken"]["address"]
                if mint not in mints and len(mints) >= 30:
                    continue
                if mint not in mints:
                    mints.append(mint)
                sources.setdefault(mint, set()).add(label)
                raw.append(item)
        # Track the known pool addresses to batch refresh held positions with
        # /pools/multi rather than N token/pools calls (N can be 20).
        best = normalize_pairs(raw, mints, now)
        for mint, entry in best.items():
            if DexFeed.valid(entry["pair"]):
                self.known_pool[mint] = entry["pair"]
        if len(self.known_pool) > 200:
            self.known_pool = {k: v for k, v in self.known_pool.items() if k in set(mints)}
        return {"mints": mints, "pairs": raw, "sources": {
            key: sorted(value) for key, value in sources.items()}, "paid": {}, "source": "GECKOTERMINAL"}

    def quotes(self, mints: list[str]) -> list[dict]:
        # A *single* batch for up to 20 known pools, not 20 individual API hits.
        # Unknown pairs remain stale/UNKNOWN: never fabricate an indicative quote.
        addresses = list(dict.fromkeys(self.known_pool[mint] for mint in mints[:20]
                                       if mint in self.known_pool))
        if not addresses:
            return []
        raw = []
        for start in range(0, len(addresses), 20):
            path = "/networks/solana/pools/multi/" + ",".join(
                quote(x, safe="") for x in addresses[start:start+20]) + "?include=base_token"
            raw.extend(self.translate(self._get(path), time.time()))
        return [p for p in raw if p["baseToken"]["address"] in set(mints)]


class ResilientFeed:
    """Use fresh DEX or Gecko snapshots with a shared provider circuit breaker."""

    def __init__(self, gate=None):
        self.gate = gate if gate is not None else SHARED_GATE
        self.dex = DexFeed(self.gate)
        self.gecko = GeckoFeed(self.gate)
        self.source = "NOT_FETCHED"

    def discover(self) -> dict:
        try:
            result = self.dex.discover()
            self.source = "DEX_SCREENER"
            result["source"] = self.source
            return result
        except Exception as dex_exc:
            try:
                result = self.gecko.discover()
                self.source = "GECKOTERMINAL"
                return result
            except Exception as gecko_exc:
                self.source = "ALL_FEEDS_UNAVAILABLE"
                # Do not invent prices or use an expired cached snapshot.
                raise MarketTemporarilyUnavailable(
                    "ALL_MARKET_FEEDS",
                    "DEX %s; Gecko %s" % (str(dex_exc)[:95], str(gecko_exc)[:95]),
                    max(getattr(dex_exc, "retry_at", 0),
                        getattr(gecko_exc, "retry_at", 0),
                        time.time() + 30)
                ) from gecko_exc

    def quotes(self, mints: list[str]) -> list[dict]:
        if not mints:
            return []
        try:
            return self.dex.quotes(mints)
        except Exception:
            return self.gecko.quotes(mints)

    def health(self) -> dict:
        return self.gate.snapshot()


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
        chain = t.get("onchain_risk") or {"state": "UNKNOWN", "flags": ["not_checked"]}
        if chain.get("state") == "RISK":
            failures.append("已偵測 Mint／Freeze 或其他鏈上重大權限風險")
        return {
            "state": "REJECT" if failures else "MARKET_SCREENED",
            "passed": not failures,
            "reasons": failures,
            "contract_security": "RISK" if chain.get("state") == "RISK" else "UNVERIFIED",
            "onchain": chain,
            "message": "僅局部檢查 Mint／Freeze；持幣集中度、DEV 關聯、夾擊、交易稅與 Rug 無法完整驗證",
        }


class MarketActivityAgent:
    """03: market participation evidence, distinct from entry/exit strategy.

    Records only observed hourly transaction counts and volume; these counts
    do not prove buyer notional, genuine wallets, or future price direction.
    Does not place orders, score entries, or bypass safety.
    """
    name = "03｜交易活躍度"

    def evaluate(self, t: dict, now: float | None = None) -> dict:
        buys = max(0, int(t.get("h1_buys") or 0))
        sells = max(0, int(t.get("h1_sells") or 0))
        volume = max(0.0, float(t.get("volume_h1") or 0))
        total = buys + sells
        observed = total >= 20 and volume > 0
        return {
            "state": "OBSERVED" if observed else "INSUFFICIENT_DATA",
            "h1_buys": buys, "h1_sells": sells,
            "h1_transactions": total, "h1_volume_usd": volume,
            "buy_sell_count_ratio": round(buys / max(sells, 1), 3) if total else None,
            "score_used": False,
            "message": "獨立回報1H交易筆數與成交量；買賣筆數不是資金淨流入，也不代表可實盤",
        }


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
        fee = size * self.s.fee_pct_per_side / 100 + self.s.simulated_network_fee_usd
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cash = float(db.execute("SELECT value FROM meta WHERE key='cash'").fetchone()[0])
            if cash < size + fee + self.s.cash_reserve_usd or db.execute("SELECT 1 FROM positions WHERE mint=?", (t["mint"],)).fetchone():
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
        fee = gross * self.s.fee_pct_per_side / 100 + self.s.simulated_network_fee_usd
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
            return False, "同時持倉數已達上限（非每日交易次數）"
        if self.book.bought_recently(token["mint"], now):
            return False, "同幣1小時冷卻"
        if self.book.realized_today(now) <= -self.s.daily_loss_limit_usd:
            return False, "每日已實現虧損熔斷"
        if self.book.equity() <= self.s.initial_cash - self.s.daily_loss_limit_usd:
            return False, "總淨值回撤熔斷"
        invested = sum(p["cost"] for p in positions)
        if invested + self.s.trade_usd > self.book.equity() * self.s.max_allocated_pct:
            return False, "總模擬倉位上限"
        required = (self.s.trade_usd * (1 + self.s.fee_pct_per_side / 100)
                    + self.s.simulated_network_fee_usd + self.s.cash_reserve_usd)
        if self.book.cash() < required:
            return False, "扣除手續費／網路費與預留緩衝後，模擬餘額不足"
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
        self.feed = feed or ResilientFeed()
        # Keep a separate price path: one batched request for <= 20 held mints.
        # The discoverer can fail independently.  This is polling, NOT WebSocket.
        self.monitor_feed = DexFeed()
        self.book = PaperBook(self.s)
        self.hunters = DiscoveryAgent()
        self.safety = SafetyAgent(self.s)
        self.mint_observer = MintRiskObserver()
        self.activity = MarketActivityAgent()
        self.council = PaperCouncil()
        # Explicit opt-in only. OPENAI_API_KEY is never returned to the browser.
        self.openai = OpenAICouncil(self.s.db_path)
        self.shadow_judge = ShadowJudge(self.s)
        self.live_signal = StateSignal()
        self.chain_stream = LiveChainObserver()
        self.early_watch = EarlyStrengthWatch()
        # Default production scanner ONLY considers attested launches of the
        # three named launchpads. Injected fake feeds opt out only for legacy
        # deterministic tests; Render sets PAPER_LAUNCHPAD_ONLY=1 explicitly.
        self.launchpad_only = os.getenv("PAPER_LAUNCHPAD_ONLY",
                                       "1" if feed is None else "0") == "1"
        self.council_reviews = 0
        self.council_approved = 0
        self.council_recent: list[dict] = []
        self.social = SocialAgent()
        self.decision = DecisionAgent(self.s)
        self.execution = ExecutionAgent(self.book, self.s)
        self.lock = threading.Lock()
        self.monitor_lock = threading.Lock()
        self.monitor_last_attempt = 0.0
        self.monitor_last_success = 0.0
        self.monitor_last_error = ""
        self.monitor_quotes_seen = 0
        self.monitor_next_allowed = 0.0
        self.monitor_events: list[dict] = []
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
            shadow_mints = []
            if self.launchpad_only:
                mints = self.chain_stream.scoped_mints(limit=20)
                # Shadow-only observation of older named-launchpad events.
                # This does not expand mints processed by six agents/JUDGE.
                try:
                    verified = self.chain_stream.research_candidates(now=now)
                    self.early_watch.register(verified, now)
                    shadow_mints = self.early_watch.choose_shadow(mints, now=now)
                except Exception as exc:
                    shadow_mints = []
                    LOG.warning("research pool unavailable; keep core paper scan: %s",
                                type(exc).__name__)
                if not mints:
                    with self.lock:
                        self.candidates = []
                        self.last_error = "等待 Pump.fun／BONK.fun／Bags.fm 的已識別新幣；不抓全市場候選"
                    return {"buys": [], "exits": [], "candidates": 0, "status": "WAITING_LAUNCHPAD_EVENTS"}
                # No DEX/Gecko all-market discovery calls. Only batch quotes
                # for launchpad-attested mints; all risk, council and original
                # paper-account checks below remain in force.
                # DEX token batch supports 30: 20 original + up to 10 shadow.
                # One normal market quote call, not a second polling loop.
                discovery = {"mints": mints,
                             "pairs": self.feed.quotes(mints + shadow_mints),
                             "paid": {}, "sources": {
                                 mint: ["launchpad:" + self.chain_stream.launchpad_for(mint)]
                                 for mint in mints}}
            else:
                discovery = self.feed.discover()
            mints = discovery["mints"]
            candidate_quotes = normalize_pairs(discovery["pairs"], mints, now)
            if self.launchpad_only:
                try:
                    extra_quotes = normalize_pairs(discovery["pairs"], shadow_mints, now)
                    self.early_watch.observe({**candidate_quotes, **extra_quotes}, mints, now)
                except Exception as exc:
                    LOG.warning("research quote observation unavailable, paper unchanged: %s",
                                type(exc).__name__)
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
                t["onchain_risk"] = self.mint_observer.status(mint, now)
                discovery_report = self.hunters.evaluate(t, discovery["sources"].get(mint, []))
                safety = self.safety.evaluate(t)
                activity = self.activity.evaluate(t, now)
                social = self.social.evaluate(t, discovery["paid"].get(mint, 0))
                decision = self.decision.evaluate(t, safety)
                ranked.append({**t, "launchpad": self.chain_stream.launchpad_for(mint) if self.launchpad_only else "LEGACY_TEST",
                    "score": decision["score"], "state": decision["state"],
                    "agents": {"discovery": discovery_report, "safety": safety,
                               "market_activity": activity, "social": social, "decision": decision}})
            ranked.sort(key=lambda x: (x["score"], x["liquidity_usd"]), reverse=True)
            buys = []
            council_records = []
            for t in ranked:
                # Private background council asks all six roles for an explicit
                # vote before any simulated order. Unknown is never 'safe'.
                allowed, why = self.execution.approve(t, now)
                council = self.council.consider(
                    t["agents"]["discovery"], t["agents"]["safety"],
                    t["agents"]["market_activity"], t["agents"]["social"],
                    t["agents"]["decision"], allowed, why)
                council_records.append({"symbol": t["symbol"], "mint": t["mint"],
                                        "outcome": council["outcome"],
                                        "votes": council["votes"], "at": now})
                if not council["paper_allowed"]:
                    t["paper_order"] = "六代理未通過模擬： " + (
                        why if not allowed else council["outcome"])
                    continue
                if self.openai.enabled:
                    # Only rule-eligible + freshly quoted candidates reach paid AI.
                    # AI is asynchronous, cost-limited and may only deny/delay.
                    ai_vote = self.openai.review(t["mint"])
                    if ai_vote["state"] != "APPROVED" or not ai_vote["allowed"]:
                        state = self.openai.submit(t) if ai_vote["state"] in ("UNREVIEWED", "PENDING") else ai_vote["state"]
                        t["paper_order"] = "等待 ChatGPT 六角色審查／未批准（" + state + "）"
                        t["ai_state"] = state
                        continue
                    t["ai_state"] = "APPROVED"
                if self.book.buy(t, now):
                    buys.append({"symbol": t["symbol"], "price": fmt(t["price"]), "usd": self.s.trade_usd})
                    t["paper_order"] = "六代理協商通過；鏈上風險未完整驗證；已模擬買入"
            # The seventh JUDGE is observational: a separate B ledger, no API
            # spend and no veto/hold applied to baseline A.
            # Errors in the lab never stop or modify original paper fills.
            self.shadow_judge.process(ranked, candidate_quotes, now)
            with self.lock:
                self.council_reviews += len(council_records)
                self.council_approved += len(buys)
                self.council_recent = (council_records + self.council_recent)[:20]
                self.candidates = ranked[:30]
                self.events = (buys + exits + self.events)[:20]
                self.last_scan, self.last_error = now, ""
            return {"buys": buys, "exits": exits, "candidates": len(ranked)}
        except MarketTemporarilyUnavailable as exc:
            # Display the last good candidates *as stale* for observation only.
            # No new paper orders, no fake PnL, no repeated stacktrace storm.
            with self.lock:
                self.last_error = "行情來源限流／退避中：%s" % str(exc)[:220]
            return {"error": str(exc), "status": "UPSTREAM_BACKOFF", "buys": [], "exits": []}
        except Exception as exc:
            LOG.exception("paper scan failed")
            with self.lock:
                self.last_error = "%s: %s" % (type(exc).__name__, str(exc)[:250])
            return {"error": str(exc)}
        finally:
            with self.lock:
                self.scanning = False
            self.live_signal.notify()

    def monitor_once(self, now: float | None = None) -> dict:
        """Refresh open positions and apply simulated TP/SL without a universe scan.

        No orders and no fabricated fills. A failed batch does not re-use a stale
        mark for an exit. Quote fetching is bulked via DEX Screener (max 30 mints).
        This method is synchronous so it can be tested with a fake market feed.
        """
        now = now if now is not None else time.time()
        if not self.monitor_lock.acquire(blocking=False):
            return {"status": "BUSY"}
        try:
            with self.lock:
                self.monitor_last_attempt = now
                next_allowed = self.monitor_next_allowed
            held = self.book.positions()
            if not held:
                with self.lock:
                    self.monitor_last_error = ""
                    self.monitor_quotes_seen = 0
                return {"status": "NO_POSITIONS", "monitored": 0, "exits": []}
            if now < next_allowed:
                return {"status": "RATE_LIMIT_BACKOFF", "next_allowed": next_allowed}
            mints = [pos["mint"] for pos in held]
            try:
                raw_quotes = self.monitor_feed.quotes(mints)
                quotes = normalize_pairs(raw_quotes, mints, now)
                if not quotes:
                    raise RuntimeError("沒有取得任何可驗證的持倉交易池報價")
                exits = self.execution.review_positions(quotes, now)
                # Update B exits only when the baseline batch already contains
                # their quote; never make A depend on extra shadow HTTP calls.
                self.shadow_judge.monitor(quotes, now)
                with self.lock:
                    self.monitor_last_success = now
                    self.monitor_last_error = ""
                    self.monitor_quotes_seen = len(quotes)
                    self.monitor_next_allowed = 0.0
                    self.monitor_events = (exits + self.monitor_events)[:20]
                return {
                    "status": "OK" if len(quotes) == len(mints) else "PARTIAL",
                    "monitored": len(quotes),
                    "missing": len(mints) - len(quotes),
                    "exits": exits,
                }
            except Exception as exc:
                from urllib.error import HTTPError
                backoff = (max(30, int(exc.retry_at - now))
                           if isinstance(exc, MarketTemporarilyUnavailable)
                           else 300 if isinstance(exc, HTTPError) and exc.code == 429 else 60)
                message = f"{type(exc).__name__}: {str(exc)[:160]}"
                LOG.warning("held-position quote monitor unavailable: %s", message)
                with self.lock:
                    self.monitor_last_error = message
                    self.monitor_quotes_seen = 0
                    self.monitor_next_allowed = now + backoff
                return {"status": "ERROR", "error": message, "retry_after_seconds": backoff}
        finally:
            self.monitor_lock.release()
            self.live_signal.notify()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.openai.start()
        self.chain_stream.start(on_update=self.live_signal.notify)

        def loop():
            while True:
                self.run_once()
                time.sleep(self.s.scan_seconds)
        threading.Thread(target=loop, name="solana-paper-scanner", daemon=True).start()

        def monitor_loop():
            while True:
                self.monitor_once()
                time.sleep(self.s.monitor_seconds)
        threading.Thread(target=monitor_loop, name="solana-paper-position-monitor", daemon=True).start()

        def intelligence_loop():
            # Refresh mint permissions off the hot scan path; no wallet tracking.
            time.sleep(5)
            while True:
                with self.lock:
                    mints = [t["mint"] for t in self.candidates[:5]]
                self.mint_observer.refresh(mints)
                self.live_signal.notify()
                time.sleep(600)
        threading.Thread(target=intelligence_loop, name="solana-paper-intelligence", daemon=True).start()

    def status(self) -> dict:
        with self.lock:
            snapshot = {
                "last_scan": self.last_scan,
                "last_attempt": self.last_attempt,
                "last_error": self.last_error,
                "market_feed": str(getattr(self.feed, "source", "TEST_FEED")),
                "market_feed_health": self.feed.health() if hasattr(self.feed, "health") else {},
                "market_snapshot_stale": not self.last_scan or
                    (time.time() - self.last_scan > self.s.scan_seconds * 2),
                "scanning": self.scanning,
                "candidates": self.candidates,
                "events": self.events,
                "monitor_last_attempt": self.monitor_last_attempt,
                "monitor_last_success": self.monitor_last_success,
                "monitor_last_error": self.monitor_last_error,
                "monitor_quotes_seen": self.monitor_quotes_seen,
                "monitor_backoff_until": self.monitor_next_allowed,
                "monitor_type": "BATCHED_HTTP_POLLING_NOT_WEBSOCKET",
                "monitor_seconds": self.s.monitor_seconds,
                "council_reviewed_total": self.council_reviews,
                "council_paper_fills_total": self.council_approved,
                "council_recent": self.council_recent[:10],
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
                "六代理分工是規則引擎，非六個獨立 LLM；不追蹤聰明錢錢包，也不是預測勝率",
                "DexScreener 最新 Profile／Boost 不是全鏈新幣清單，Boost 是付費曝光",
                "合約權限與 Rug 風險未完成鏈上驗證，僅可模擬，不可實盤",
                "成交為延遲行情估算，含固定滑價／費率，不等於真實可成交價",
                "Render 免費服務檔案系統可能重置；當前 SQLite 僅供第一階段測試",
                "本模擬網站為公開可讀狀態；未配置私人存取限制",
                "持倉數是同時持有的開倉數（可設定 3～20），平倉即釋放名額，沒有每日筆數上限",
                "預留 USD 模擬現金給手續費與退出緩衝，計入每次模擬進出費用",
                "第三代理僅觀察市場交易活躍度，未驗證真實買方資金或投資者身分",
                "六代理在後台交換規則投票；不是六個會互相對話的獨立 LLM",
                "ChatGPT 六角色分析是 API 選用功能；停用時完全不呼叫付費 API，啟用後仍受原風控否決",
                "第七位JUDGE目前僅規則型影子測試：五題閉合答案與缺失資料、30分鐘最多再問一次、獨立B帳本，不影響A原策略",
                "新幣掃描僅 Pump.fun／BONK.fun／Bags.fm：需來源辨識才加入，DEX與Gecko僅對三平台清單查報價，歷史持倉續受監控",

                "持倉監控獨立於全市場掃描：每30秒左右批次檢查報價；這是HTTP輪詢而非WebSocket串流",
                "若報價來源限流或斷線，不會假裝出場；該持倉會顯示報價過期警告",
                "免費WebSocket新幣與遷移僅作早期情報，後台每秒整理；無可信行情與必要風控證據不模擬買入",

            ],
            "agents": [
                {"id": "01", "name": self.hunters.name, "status": "ACTIVE", "detail": "DEX 最新 Profile + 付費 Boost"},
                {"id": "02", "name": self.safety.name, "status": "PARTIAL", "detail": "流動性／池齡／FDV，合約安全未知"},
                {"id": "03", "name": self.activity.name, "status": "ACTIVE", "detail": "獨立核對每小時買賣筆數與成交量，不推測資金方向"},
                {"id": "04", "name": self.social.name, "status": "PARTIAL", "detail": "付費曝光指示，未接 X 語意分析"},
                {"id": "05", "name": self.decision.name, "status": "ACTIVE", "detail": "多因子條件與防追價"},
                {"id": "06", "name": self.execution.name, "status": "PAPER_ONLY", "detail": "只寫 SQLite，絕不送單"},
            ],
            "config": asdict(self.s) | {"db_path": "(server local sqlite)"},
            "mint_risk_observer": self.mint_observer.summary(),
            "openai_council": self.openai.status(),
            "shadow_judge": self.shadow_judge.summary(),
            "chain_stream": self.chain_stream.snapshot(),
            "early_strength_watch": self.early_watch.snapshot(),
            "launchpad_only": self.launchpad_only,
            "realtime": {
                "transport": "SSE",
                "monitoring": "SERVER_HTTP_POLLING",
                "live_market_tick": False,
                "last_scan_success_at": self.last_scan,
                "last_monitor_quote_success_at": self.monitor_last_success,
                **self.live_signal.snapshot(),
            },
            "coordination": {"model": "SIX_RULE_AGENT_COUNCIL", "requires": "discovery + non-veto safety + decision + execution",
                             "market_activity": "independent observations only; decision and execution remain separate",
                             "social": "abstains until independently verified",
                             "llm_enabled": self.openai.enabled, "paper_only": True},
            "account": {
                "cash": fmt(cash), "equity": fmt(equity),
                "open_positions": len(positions),
                "unrealized_pnl": fmt(unrealized),
                "realized_today_utc": fmt(self.book.realized_today(time.time())),
                "pnl_since_start": fmt(equity - self.s.initial_cash),
            },
            "positions": [
                {**p,
                 "unrealized_pct": round((p["mark"] / p["entry"] - 1) * 100, 2),
                 "quote_age_seconds": max(0, int(time.time() - p["mark_at"])),
                 "quote_stale": time.time() - p["mark_at"] > self.s.monitor_stale_seconds}
                for p in positions
            ],
            "trades": self.book.trades(40),
            **snapshot,
        }
