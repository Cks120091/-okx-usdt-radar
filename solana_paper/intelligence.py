"""Read-only Solana Mint/Freeze risk intelligence for paper-only research.

Data outages explicitly return UNKNOWN, never safe/zero risk.
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
        if method != "getAccountInfo":
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
