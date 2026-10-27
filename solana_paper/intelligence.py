"""Read-only Solana wallet and mint intelligence for paper-only research.

A public address list is not proof of trader identity, profitability or future
signal quality. Data outages explicitly return UNKNOWN, never safe/zero risk.
No private keys, signatures, orders or on-chain transactions are constructed.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

LOG = logging.getLogger("solana_paper.intelligence")
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPF7CWaHr9gqPNbP93"
SOURCE_URL = "https://blog.bananagun.io/blog/fomo-copy-trading-top-wallets"
SOURCE_CHECKED = "2026-09-30"
PUBLIC_WATCHLIST = (
    {"label": "Unipcs (public attribution)", "address": "2heJbC32Tpfcb3nbUb5ER61K11FGZVfVGtVnDm6LDogF"},
    {"label": "frankdegods (public attribution)", "address": "498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ"},
    {"label": "pointfarmcap (public attribution)", "address": "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"},
)

def valid_address(value):
    import re
    return isinstance(value, str) and re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", value) is not None


class PublicRpc:
    """Standard Solana JSON RPC; operator can configure a trusted HTTPS endpoint.

    Public RPC has strict rate limits. Requests are read-only; no auth required.
    """
    def __init__(self, url=None):
        self.url = url or os.getenv("SOLANA_READ_RPC_URL", "https://api.mainnet-beta.solana.com")
        if not self.url.startswith("https://"):
            raise ValueError("Only HTTPS RPC endpoints are supported")

    def call(self, method: str, params: list):
        if method not in {"getTokenAccountsByOwner", "getAccountInfo"}:
            raise ValueError("Unsupported read-only RPC method")
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        request = Request(self.url, data=payload, headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "Solana-Paper-Research/1.1",
        }, method="POST")
        with urlopen(request, timeout=10) as response:
            raw = response.read(1_500_001)
            if len(raw) > 1_500_000:
                raise ValueError("RPC response too large")
        data = json.loads(raw)
        if not isinstance(data, dict) or "error" in data or "result" not in data:
            raise RuntimeError("RPC returned an error or missing result")
        return data["result"]


def extract_wallet_mints(response):
    values = response.get("value") if isinstance(response, dict) else None
    if not isinstance(values, list):
        raise ValueError("Missing token accounts")
    result = set()
    for row in values[:1500]:
        try:
            info = row["account"]["data"]["parsed"]["info"]
            if int(info["tokenAmount"]["amount"]) > 0 and valid_address(info["mint"]):
                result.add(info["mint"])
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
    return result


def mint_risk_from_rpc(result):
    """Return checks only when account was parsed as an SPL mint.

    Token-2022 extensions not on a complete allowlist remain UNKNOWN, with
    an explicit caution. Mint/freeze authorities known-present veto new paper.
    """
    value = result.get("value") if isinstance(result, dict) else None
    if value is None:
        return {"state": "UNKNOWN", "flags": ["mint_account_unavailable"]}
    try:
        data = value["data"]["parsed"]
        if data["type"] != "mint":
            return {"state": "UNKNOWN", "flags": ["mint_parse_unsupported"]}
        info = data["info"]
        owner_program = value.get("owner")
        if owner_program not in (TOKEN_PROGRAM, TOKEN_2022):
            return {"state": "UNKNOWN", "flags": ["unknown_token_program"]}
        risks = []
        if info.get("mintAuthority") is not None:
            risks.append("mint_authority_active")
        if info.get("freezeAuthority") is not None:
            risks.append("freeze_authority_active")
        extensions = info.get("extensions") or []
        if owner_program == TOKEN_2022:
            # New extensions may alter transfer/sale behavior, treat as unknown.
            return {"state": "RISK" if risks else "UNKNOWN",
                    "flags": risks or ["token_2022_extensions_not_verified"],
                    "program": owner_program}
        return {"state": "RISK" if risks else "MINT_FREEZE_REVOKED",
                "flags": risks, "program": owner_program}
    except (KeyError, TypeError, AttributeError, ValueError):
        return {"state": "UNKNOWN", "flags": ["mint_parse_incomplete"]}


class WalletObserver:
    """Low-frequency, non-copy-trading holding observer for public addresses."""
    def __init__(self, rpc=None, wallets=None):
        self.rpc = rpc or PublicRpc()
        self.wallets = tuple(wallets) if wallets is not None else PUBLIC_WATCHLIST
        self.lock = threading.Lock()
        self.data = {}
        self.updated_at = 0.0
        self.last_error = ""
        self.backoff_until = 0.0

    def refresh(self, now=None):
        now = now if now is not None else time.time()
        with self.lock:
            if now < self.backoff_until:
                return False
            if self.updated_at and now - self.updated_at < 600:
                return True
        observed = {}
        try:
            for wallet in self.wallets[:5]:
                if not valid_address(wallet.get("address")):
                    continue
                accounts = self.rpc.call("getTokenAccountsByOwner", [
                    wallet["address"], {"programId": TOKEN_PROGRAM},
                    {"encoding": "jsonParsed", "commitment": "confirmed"}])
                observed[wallet["address"]] = extract_wallet_mints(accounts)
            if not observed:
                raise RuntimeError("No public wallet records obtained")
            with self.lock:
                self.data, self.updated_at, self.last_error = observed, now, ""
                self.backoff_until = 0.0
            return True
        except Exception as exc:
            error = "%s: %s" % (type(exc).__name__, str(exc)[:100])
            delay = 1800 if isinstance(exc, HTTPError) and exc.code in (403, 429) else 600
            LOG.warning("public watchlist unavailable; cached data is not a live trade: %s", error)
            with self.lock:
                self.last_error = error
                self.backoff_until = now + delay
            return False

    def status(self, mint: str, now=None):
        now = now if now is not None else time.time()
        with self.lock:
            age = now - self.updated_at if self.updated_at else None
            # Cached observation is stale rather than a fresh confirmation.
            valid = age is not None and 0 <= age <= 1200
            matches = [a for a, mints in self.data.items() if mint in mints] if valid else []
            return {
                "state": "OBSERVED_HOLDING" if matches else ("NOT_HELD_IN_WATCHLIST" if valid else "NO_DATA"),
                "matched_wallets": len(matches), "observed_addresses": matches[:5],
                "wallets_sampled": len(self.data) if valid else 0,
                "source": SOURCE_URL, "checked_at_source": SOURCE_CHECKED,
                "snapshot_age_seconds": round(age) if age is not None else None,
                "score_used": False, "note": "公開歸屬未獨立驗證；持倉快照不是新買入，也不是獲利證明",
            }

    def summary(self):
        with self.lock:
            return {"tracked": len(self.wallets), "updated_at": self.updated_at,
                    "last_error": self.last_error, "backoff_until": self.backoff_until,
                    "source": SOURCE_URL}


class MintRiskObserver:
    """Small, rate-limited risk lookup for candidate SPL mint authority."""
    def __init__(self, rpc=None):
        self.rpc = rpc or PublicRpc()
        self.lock = threading.Lock()
        self.reports = {}
        self.backoff_until = 0.0
        self.last_error = ""

    def refresh(self, mints, now=None):
        now = now if now is not None else time.time()
        if now < self.backoff_until:
            return 0
        updated = 0
        for mint in list(dict.fromkeys(mints))[:5]:
            if not valid_address(mint):
                continue
            with self.lock:
                prior = self.reports.get(mint)
            if prior and now - prior["at"] < 900:
                continue
            try:
                result = self.rpc.call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
                report = mint_risk_from_rpc(result)
                with self.lock:
                    self.reports[mint] = {"at": now, "report": report}
                    self.last_error = ""
                updated += 1
            except Exception as exc:
                delay = 1800 if isinstance(exc, HTTPError) and exc.code in (403, 429) else 600
                with self.lock:
                    self.backoff_until = now + delay
                    self.last_error = "%s: %s" % (type(exc).__name__, str(exc)[:100])
                break
        return updated

    def status(self, mint, now=None):
        now = now if now is not None else time.time()
        with self.lock:
            record = self.reports.get(mint)
            if not record or now - record["at"] > 1800:
                return {"state": "UNKNOWN", "flags": ["unavailable_or_stale"]}
            return dict(record["report"]) | {"observed_at": record["at"]}

    def summary(self):
        with self.lock:
            return {"checked": len(self.reports), "last_error": self.last_error,
                    "backoff_until": self.backoff_until}
