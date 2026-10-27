"""Shadow-only early-strength tracking for attested launchpad mints.

This research pool never places orders, changes the paper decision/council,
or interprets a creation event as a price. All market observations originate
from existing bounded batch quotes; no extra HTTP polling or paid APIs.
"""
from __future__ import annotations

import threading
import time
from collections import Counter

FRESH_SECONDS = 420
TRACK_SECONDS = 14400
MAX_TRACKED = 120
SHADOW_QUOTE_LIMIT = 10


class EarlyStrengthWatch:
    def __init__(self, clock=None):
        self.clock = clock or time.time
        self.lock = threading.RLock()
        self.records = {}
        self.total_quotes = 0
        self.last_observed_at = 0.0
        self.last_core = ()

    def register(self, items, now=None):
        """Items: strictly launcher-verified (mint, platform, event-time)."""
        now = self.clock() if now is None else now
        with self.lock:
            for mint, launchpad, event_at in items[:350]:
                if not (isinstance(event_at, (float, int))
                        and 0 <= now - event_at < TRACK_SECONDS):
                    continue
                entry = self.records.get(mint)
                if entry is None:
                    self.records[mint] = {
                        "mint": mint, "launchpad": launchpad,
                        "first_seen_at": event_at, "last_event_at": event_at,
                        "symbol": "—", "state": "EARLY_WATCH", "reason": "僅有新幣事件，等待可信行情",
                        "last_quote_at": 0.0, "quote_samples": 0,
                        "positive_windows": 0, "last_price": None,
                        "previous_price": None, "volume_h1": None,
                        "liquidity_usd": None, "h1_buys": None,
                        "h1_sells": None, "m5_change": None,
                        "h1_change": None, "recent_core": False,
                    }
                else:
                    entry["last_event_at"] = max(event_at, entry["last_event_at"])
            # bounded memory; newer data wins, but retain observed strength when fresh.
            if len(self.records) > MAX_TRACKED:
                keep = sorted(self.records.values(), key=lambda r: (
                    r["state"] == "STRONG_PREP" and now-r["last_quote_at"]<=FRESH_SECONDS,
                    r["last_quote_at"], r["last_event_at"]), reverse=True)[:MAX_TRACKED]
                self.records = {r["mint"]: r for r in keep}

    def choose_shadow(self, latest_core, count=SHADOW_QUOTE_LIMIT, now=None):
        """Pick older followed mints, never substitute for core 20 candidates."""
        now = self.clock() if now is None else now
        excluded = set(latest_core)
        with self.lock:
            options = [r for r in self.records.values()
                       if r["mint"] not in excluded
                       and 0 <= now - r["last_event_at"] < TRACK_SECONDS]
            # Reserve half for previously promising candidates; rotate remaining
            # early leads to avoid following only the oldest/newest indefinitely.
            options.sort(key=lambda r: (
                r["state"] == "STRONG_PREP" and 0 <= now-r["last_quote_at"]<=FRESH_SECONDS,
                r["state"] == "BUILDING" and 0 <= now-r["last_quote_at"]<=FRESH_SECONDS,
                -r["last_quote_at"], r["last_event_at"]), reverse=True)
            return [r["mint"] for r in options[:max(0, min(SHADOW_QUOTE_LIMIT,int(count)))]]

    def observe(self, quotes, core_mints, now=None):
        """Use only validated normalized snapshots; at most one per scan interval."""
        now = self.clock() if now is None else now
        self.last_core = tuple(core_mints)
        with self.lock:
            for row in quotes.values():
                rec = self.records.get(row.get("mint"))
                if rec is None:
                    continue
                # Do not call observing two timestamps of same scan "confirmation".
                if rec["last_quote_at"] and now - rec["last_quote_at"] < 90:
                    continue
                liq = float(row.get("liquidity_usd") or 0)
                price = float(row.get("price") or 0)
                vol = float(row.get("volume_h1") or 0)
                buys = max(0, int(row.get("h1_buys") or 0))
                sells = max(0, int(row.get("h1_sells") or 0))
                m5 = float(row.get("m5_change") or 0)
                h1 = float(row.get("h1_change") or 0)
                if price <= 0 or liq <= 0:
                    continue
                momentum = (vol >= 5000 and liq >= 25000 and
                            buys+sells >= 20 and buys > sells*1.1 and
                            0 < m5 <= 15 and 0 < h1 <= 60)
                prev_good = rec["positive_windows"]
                # Two independent scans, NOT transaction-level 5m evidence.
                positive = min(5, prev_good+1) if momentum else 0
                reason = ("連續兩輪有正向價格／交易活躍度；非正式進場訊號"
                          if positive >= 2 else
                          "單輪活躍轉強，等待下一輪驗證" if positive == 1 else
                          "資料可用，但暫未達早期觀察動能門檻")
                rec.update({
                    "state": "STRONG_PREP" if positive >= 2 else
                             "BUILDING" if positive == 1 else "EARLY_WATCH",
                    "reason": reason, "symbol": str(row.get("symbol") or "—")[:22],
                    "quote_samples": rec["quote_samples"]+1,
                    "positive_windows": positive,
                    "previous_price": rec["last_price"],
                    "last_price": price,
                    "last_quote_at": now, "liquidity_usd": liq,
                    "volume_h1": vol, "h1_buys": buys, "h1_sells": sells,
                    "m5_change": m5, "h1_change": h1,
                })
                self.total_quotes += 1
            self.last_observed_at = now

    def snapshot(self, now=None):
        now = self.clock() if now is None else now
        with self.lock:
            valid = [dict(r) for r in self.records.values()
                     if 0 <= now-r["last_event_at"] < TRACK_SECONDS]
            core = set(self.last_core)
            result = []
            totals = Counter()
            for r in valid:
                age = now-r["last_quote_at"] if r["last_quote_at"] else None
                if age is None:
                    state = "AWAITING_QUOTE"
                elif age > FRESH_SECONDS or age < 0:
                    state = "STALE_DATA"
                else:
                    state = r["state"]
                totals[state] += 1
                r["state"] = state
                r["recent_core"] = r["mint"] in core
                r["quote_age_seconds"] = round(age) if age is not None else None
                result.append(r)
            order={"STRONG_PREP":0, "BUILDING":1, "EARLY_WATCH":2,
                   "AWAITING_QUOTE":3, "STALE_DATA":4}
            result.sort(key=lambda r: (order.get(r["state"], 5),
                                      -r["last_quote_at"], -r["last_event_at"]))
            return {
                "mode": "OBSERVE_ONLY",
                "trade_decisions_affected": False,
                "extra_market_requests": 0,
                "max_tracked": MAX_TRACKED,
                "extra_batch_mints_limit": SHADOW_QUOTE_LIMIT,
                "registered": len(valid),
                "outside_latest_20": sum(r["mint"] not in core for r in valid),
                "states": dict(totals),
                "last_observed_at": self.last_observed_at,
                "valid_quote_samples": self.total_quotes,
                "items": result[:18],
                "note": "強勢預備只表示兩次可用市場快照皆符合觀察條件；非勝率、保證上漲、鏈上買入或模擬進場批准。Render 重啟會清空觀察史。",
            }
