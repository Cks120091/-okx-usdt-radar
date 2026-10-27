"""Read-only, conservative Solana PUBLIC RPC wallet watcher for paper research.

No third-party RPC vendor/key, no submit/sendTransaction, no wallet signing.
Mainnet public RPC is rate-limited and NOT a guaranteed production feed.
WS gets live address *activity*; HTTP is a limited fallback with a historical
baseline, never a retroactive live-buy alert. Does not influence paper orders.
"""
from __future__ import annotations

from collections import deque
import json
import logging
import os
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

LOG = logging.getLogger("solana_paper.wallet_rpc")
PUBLIC_WS_URL = "wss://api.mainnet.solana.com/"
PUBLIC_HTTP_URL = "https://api.mainnet.solana.com"


def bounded_int(raw, default, lo, hi):
    try:
        number = int(raw)
    except (TypeError, ValueError):
        number = default
    return max(lo, min(hi, number))


class PublicWalletRpcMonitor:
    def __init__(self, wallets=(), env=None, connection_factory=None,
                 http_fetcher=None, clock=None):
        env = os.environ if env is None else env
        self.clock = clock or time.time
        self.enabled = env.get("PAPER_RPC_WALLET_EVENTS", "1") == "1"
        self.wallets = tuple(wallets)
        self.poll_limit = min(len(self.wallets), bounded_int(
            env.get("PAPER_RPC_FALLBACK_WALLETS"), 3, 1, 3))
        self.poll_seconds = bounded_int(env.get("PAPER_RPC_POLL_SECONDS"), 180, 120, 600)
        self.connection_factory = connection_factory
        self.http_fetcher = http_fetcher or urlopen
        self.lock = threading.RLock()
        self.confirmations = {}
        self.known_signatures = {}  # per address, seed first to avoid historical false alarms
        self.unique = set()
        self.unique_queue = deque()
        self.activities = deque(maxlen=25)
        self.activity_total = 0
        self.on_update = None
        self.started = False
        self.backoff_until = 0.0
        self.state = {
            "provider": "SOLANA_PUBLIC_RPC",
            "state": "CONNECTING" if self.enabled else "DISABLED",
            "last_error": "", "last_connected": 0.0, "last_message": 0.0,
            "last_poll": 0.0, "reconnects": 0,
            "subscriptions_requested": 0, "subscriptions_confirmed": 0,
            "fallback_wallets": self.poll_limit,
            "poll_interval_seconds": self.poll_seconds,
            "http_backoff_until": 0.0,
            "guaranteed_realtime": False,
        }

    def _set(self, state, error=""):
        with self.lock:
            self.state["state"], self.state["last_error"] = state, error
            if state == "CONNECTED":
                self.state["last_connected"] = self.clock()

    def _record(self, address, signature, source):
        if not (isinstance(signature, str) and 60 <= len(signature) <= 110):
            return False
        with self.lock:
            key = (address, signature)
            if key in self.unique:
                return False
            self.unique.add(key)
            self.unique_queue.append(key)
            while len(self.unique_queue) > 500:
                self.unique.discard(self.unique_queue.popleft())
            self.activity_total += 1
            self.activities.appendleft({
                "signature": signature, "address": address, "at": self.clock(),
                "kind": "ADDRESS_ACTIVITY_NOT_PROVEN_BUY", "source": source,
            })
        if self.on_update:
            self.on_update()
        return True

    def handle_ws_message(self, packet):
        """Reject failed transactions and unknown subscriptions; do not infer buys."""
        if not isinstance(packet, dict):
            return
        if "error" in packet:
            raise RuntimeError("Public Solana RPC rejected wallet subscription")
        if packet.get("method") != "logsNotification":
            request_id, sub_id = packet.get("id"), packet.get("result")
            if (type(request_id) is int and 1 <= request_id <= len(self.wallets)
                    and type(sub_id) is int):
                with self.lock:
                    self.confirmations[sub_id] = self.wallets[request_id - 1]["address"]
                    confirmed = len(self.confirmations)
                    self.state["subscriptions_confirmed"] = confirmed
                    self.state["last_message"] = self.clock()
                self._set("CONNECTED")
                if confirmed in (1, len(self.wallets)):
                    LOG.info("Solana public RPC wallet logsSubscribe confirmed %d/%d",
                             confirmed, len(self.wallets))
            return
        params = packet.get("params") or {}
        details = (params.get("result") or {}).get("value") or {}
        signature = details.get("signature")
        if details.get("err") is not None:
            return
        with self.lock:
            address = self.confirmations.get(params.get("subscription"))
            self.state["last_message"] = self.clock()
        if address:
            self._record(address, signature, "SOLANA_PUBLIC_WS")

    def _connect(self):
        if self.connection_factory is not None:
            return self.connection_factory(PUBLIC_WS_URL)
        from websocket import create_connection
        return create_connection(PUBLIC_WS_URL, timeout=12, enable_multithread=True)

    def _ws_once(self):
        ws = self._connect()
        try:
            with self.lock:
                self.confirmations.clear()
                self.state["subscriptions_confirmed"] = 0
                self.state["subscriptions_requested"] = len(self.wallets)
            self._set("SUBSCRIBING")
            for i, wallet in enumerate(self.wallets, 1):
                ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": i, "method": "logsSubscribe",
                    "params": [{"mentions": [wallet["address"]]},
                               {"commitment": "confirmed"}],
                }))
            LOG.info("Solana public RPC read-only WS opened; requested %d wallet subscriptions",
                     len(self.wallets))
            ws.settimeout(30)
            while True:
                try:
                    frame = ws.recv()
                except Exception as exc:
                    if exc.__class__.__name__ == "WebSocketTimeoutException":
                        ws.ping()
                        continue
                    raise
                if not frame:
                    raise OSError("Wallet RPC socket disconnected")
                if isinstance(frame, bytes):
                    if len(frame) > 300000:
                        continue
                    frame = frame.decode("utf-8", "replace")
                if len(frame) > 300000:
                    continue
                self.handle_ws_message(json.loads(frame))
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def _ws_loop(self):
        delay = 10
        while True:
            try:
                self._ws_once()
            except Exception as exc:
                # Never print potentially sensitive provider responses.
                with self.lock:
                    self.confirmations.clear()
                    self.state["subscriptions_confirmed"] = 0
                    self.state["reconnects"] += 1
                self._set("BACKOFF", type(exc).__name__)
                LOG.warning("Solana public RPC wallet WS unavailable (%s); retry in %ds",
                            type(exc).__name__, delay)
                time.sleep(delay)
                delay = min(delay * 2, 600)

    def _signatures(self, address):
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress",
            "params": [address, {"limit": 3, "commitment": "confirmed"}],
        }).encode()
        req = Request(PUBLIC_HTTP_URL, data=payload, method="POST", headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Solana-Paper-ReadOnly-WalletResearch/1.0",
        })
        with self.http_fetcher(req, timeout=10) as response:
            raw = response.read(120001)
        if len(raw) > 120000:
            raise ValueError("Public RPC response too large")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or "error" in parsed:
            raise RuntimeError("Public RPC returned error for wallet polling")
        entries = parsed.get("result")
        if not isinstance(entries, list):
            raise RuntimeError("Public RPC response missing signatures")
        return entries[:3]

    def poll_once(self):
        """WS preferred; at most three HTTP wallet calls per 2-10 min fallback."""
        if not self.enabled:
            return False
        with self.lock:
            if (self.state["state"] == "CONNECTED"
                    and self.state["subscriptions_confirmed"] == len(self.wallets)):
                return False
            if self.clock() < self.backoff_until:
                return False
        did_poll = False
        for wallet in self.wallets[:self.poll_limit]:
            address = wallet["address"]
            try:
                entries = self._signatures(address)
                valid = [x for x in entries if isinstance(x, dict)
                         and isinstance(x.get("signature"), str)]
                signatures = {x["signature"] for x in valid}
                with self.lock:
                    previous = self.known_signatures.get(address)
                    # Baseline is not treated as fresh activity.
                    fresh = [x for x in reversed(valid)
                             if previous is not None and
                             x["signature"] not in previous and x.get("err") is None]
                    self.known_signatures[address] = signatures
                    self.state["last_poll"] = self.clock()
                for entry in fresh:
                    self._record(address, entry["signature"], "SOLANA_PUBLIC_HTTP_FALLBACK")
                did_poll = True
            except Exception as exc:
                cooldown = 600 if isinstance(exc, HTTPError) and exc.code in (403, 429) else 180
                if isinstance(exc, HTTPError) and exc.headers is not None:
                    cooldown = max(cooldown, bounded_int(
                        exc.headers.get("Retry-After"), cooldown, 0, 3600))
                with self.lock:
                    self.backoff_until = self.clock() + cooldown
                    self.state["http_backoff_until"] = self.backoff_until
                # The WS can still reconnect while HTTP is backing off.
                if self.state["state"] != "CONNECTED":
                    self._set("BACKOFF", type(exc).__name__)
                LOG.warning("Solana public RPC wallet HTTP fallback unavailable (%s), backoff %ds",
                            type(exc).__name__, cooldown)
                break
        if did_poll:
            with self.lock:
                ws_live = self.state["subscriptions_confirmed"] == len(self.wallets)
            if not ws_live:
                self._set("POLLING")  # only low-frequency, partial coverage
        return did_poll

    def _poll_loop(self):
        while True:
            self.poll_once()
            time.sleep(self.poll_seconds)

    def start(self, on_update=None):
        if not self.enabled or self.started:
            return
        self.started = True
        self.on_update = on_update
        threading.Thread(target=self._ws_loop, name="solana-public-wallet-ws", daemon=True).start()
        threading.Thread(target=self._poll_loop, name="solana-public-wallet-http-fallback", daemon=True).start()

    def snapshot(self):
        with self.lock:
            return {
                "status": dict(self.state), "wallet_activity_total": self.activity_total,
                "recent_wallet_events": list(self.activities)[:6],
            }
