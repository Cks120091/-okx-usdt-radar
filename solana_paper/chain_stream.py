"""Read-only free creation/migration websocket and optional Helius wallet activity.

Strict provider allowlists. Never subscribes to metered PumpPortal trade
feeds or constructs any trade/signature/private-key transaction.
Every second: process queued launch events locally, NOT HTTP polling.
Launchs are unverified leads only; a websocket event is never a price quote.
"""
from __future__ import annotations

from collections import deque
import json
import logging
import os
import queue
import threading
import time
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from .intelligence import PUBLIC_WATCHLIST, valid_address
from .launchpad_scope import ALLOWED_LAUNCHPADS, identify_stream_launchpad, identify_bags_indexer_launchpad

LOG = logging.getLogger("solana_paper.stream")
# Provider explicitly marks launch pool/platform; stream includes other events
# which are dropped BEFORE entering the queue. One socket, no subscriptions.
PUMP_URL = "wss://stream.pumpapi.io/"
BAGS_LAUNCHES_URL = "https://data-api.bags.fm/v2/launches?launchpads=Bags%2CBagsProtocol&limit=30"
HELIUS_URL = "wss://mainnet.helius-rpc.com/?api-key="
FREE_PUMP_METHODS = ()


def interpret_pump_event(packet, received_at):
    if not isinstance(packet, dict):
        return None
    mint = packet.get("mint")
    if not valid_address(mint):
        return None
    kind = str(packet.get("action") or packet.get("txType") or packet.get("type") or "").lower()
    if kind in ("create", "newtoken", "new_token", "new_token_creation"):
        kind = "CREATED"
    elif kind in ("migration", "migrate", "migrated"):
        kind = "MIGRATED"
    else:
        return None
    launchpad = identify_stream_launchpad(packet)
    if launchpad not in ALLOWED_LAUNCHPADS:
        return None  # Generic Raydium/Meteora is not a BONK/BAGS launch.
    return {
        "mint": mint, "symbol": str(packet.get("symbol") or "未知")[:24],
        "name": str(packet.get("name") or "")[:52],
        "kind": kind, "received_at": received_at,
        "launchpad": launchpad,
        "state": "AWAITING_MARKET_VERIFICATION",
        "risk": "UNKNOWN", "trade_eligible": False,
        "source": str(packet.get("_verified_source") or "PUMPAPI_FREE_STREAM")[:48],
    }


class LiveChainObserver:
    def __init__(self, env=None, connection_factory=None, clock=None):
        env = env if env is not None else os.environ
        self.clock = clock or time.time
        self.enabled = env.get("PAPER_PUMP_FREE_EVENTS", "1") == "1"
        self.bags_indexer_enabled = env.get("PAPER_BAGS_INDEXER_ENABLED", "1") == "1"
        key = env.get("HELIUS_API_KEY", "").strip()
        self.helius_enabled = (env.get("PAPER_HELIUS_WALLET_EVENTS", "0") == "1"
                               and bool(key))
        self.helius_key = key  # private only, never in public status/logs
        self.connection_factory = connection_factory
        self.pending = queue.Queue(maxsize=1200)
        self.lock = threading.RLock()
        self.started = False
        self.unique = {}
        self.leads = deque(maxlen=50)
        self.provenance = {}
        self.launchpad_counts = {k: 0 for k in ALLOWED_LAUNCHPADS}
        self.created_count = 0
        self.migrated_count = 0
        self.dropped = 0
        self.screened = 0
        self.last_seen_at = 0.0
        self.last_screen_at = 0.0
        self.wallet_activities = deque(maxlen=25)
        self.wallet_count = 0
        self.state = {
            "pump": {"state": "DISABLED" if not self.enabled else "CONNECTING",
                     "last_error": "", "last_connected": 0.0,
                     "last_message": 0.0, "reconnects": 0},
            "bags": {"state": "CONNECTING" if self.bags_indexer_enabled else "DISABLED",
                     "last_error": "", "last_connected": 0.0,
                     "last_message": 0.0, "reconnects": 0},
            "helius": {"state": "CONNECTING" if self.helius_enabled else "NOT_CONFIGURED",
                       "last_error": "", "last_connected": 0.0,
                       "last_message": 0.0, "reconnects": 0},
        }

    def ingest_pump(self, packet, timestamp=None):
        at = timestamp if timestamp is not None else self.clock()
        lead = interpret_pump_event(packet, at)
        if lead is None:
            return False
        try:
            self.pending.put_nowait(lead)
            return True
        except queue.Full:
            with self.lock:
                self.dropped += 1
            return False

    def tick(self, now=None):
        """One-second local dispatch. Does not call any pricing provider."""
        at = now if now is not None else self.clock()
        batch = []
        for _ in range(200):
            try:
                batch.append(self.pending.get_nowait())
            except queue.Empty:
                break
        changed = False
        with self.lock:
            for lead in batch:
                mint = lead["mint"]
                dedup = (mint, lead["kind"])
                if dedup in self.unique and self.unique[dedup] > at - 3600:
                    continue
                self.unique[dedup] = at
                self.leads.appendleft(lead)
                self.provenance[mint] = (lead["launchpad"], at)
                self.launchpad_counts[lead["launchpad"]] += 1
                self.screened += 1
                if self.screened == 1:
                    LOG.info("first free WebSocket Solana launch event accepted for local screening")
                self.last_seen_at = at
                if lead["kind"] == "CREATED":
                    self.created_count += 1
                else:
                    self.migrated_count += 1
                changed = True
            self.last_screen_at = at
            if len(self.unique) > 3000:
                self.unique = {k:v for k,v in self.unique.items() if v > at - 3600}
            if len(self.provenance) > 600:
                newest = sorted(self.provenance.items(), key=lambda x: x[1][1], reverse=True)[:350]
                self.provenance = dict(newest)
        return changed

    def scoped_mints(self, limit=20):
        """Trusted launchpad attribution, *not* permission to trade."""
        with self.lock:
            return [mint for mint, meta in sorted(
                self.provenance.items(), key=lambda entry: entry[1][1], reverse=True
            )[:max(0, min(25, int(limit)))] if meta[0] in ALLOWED_LAUNCHPADS]

    def launchpad_for(self, mint):
        with self.lock:
            return self.provenance.get(mint, ("UNKNOWN", 0))[0]

    def _connect(self, url):
        if self.connection_factory is not None:
            return self.connection_factory(url)
        from websocket import create_connection
        return create_connection(url, timeout=12, enable_multithread=True)

    def _set_state(self, name, state, error=""):
        with self.lock:
            self.state[name]["state"] = state
            self.state[name]["last_error"] = error
            if state == "CONNECTED":
                self.state[name]["last_connected"] = self.clock()

    def _receive(self, ws, name, on_message):
        ws.settimeout(25)
        last_ping = time.monotonic()
        while True:
            try:
                raw = ws.recv()
                if not raw:
                    raise OSError("WebSocket disconnected")
                if isinstance(raw, bytes):
                    if len(raw) > 300000:
                        continue
                    raw = raw.decode("utf-8", errors="replace")
                if len(raw) > 300000:
                    continue
                packet = json.loads(raw)
                on_message(packet)
                with self.lock:
                    self.state[name]["last_message"] = self.clock()
                # Periodic control ping; ws library does not auto-ping create_connection.
                if time.monotonic() - last_ping > 18:
                    ws.ping()
                    last_ping = time.monotonic()
            except TimeoutError:
                ws.ping()
                last_ping = time.monotonic()
            except Exception as exc:
                if exc.__class__.__name__ in ("WebSocketTimeoutException",):
                    ws.ping()
                    last_ping = time.monotonic()
                else:
                    raise

    def _handle_pump_packet(self, packet):
        if isinstance(packet, dict):
            message = str(packet.get("message") or packet.get("error") or "")[:120].lower()
            if packet.get("status") == "error" or packet.get("error") or any(
                phrase in message for phrase in ("unauthorized", "invalid api key", "not authorized", "api key required")
            ):
                raise RuntimeError("free launch event subscription rejected")
        self.ingest_pump(packet)

    def _pump_loop(self):
        pause = 5
        while True:
            ws = None
            try:
                # No API key and no metered account/token trade subscriptions.
                ws = self._connect(PUMP_URL)
                # PumpApi publishes without a subscribe command or API key.
                # Never call fee-bearing trade endpoints or send private keys.
                self._set_state("pump", "CONNECTED")
                LOG.info("Multi-launchpad free stream connected; strict Pump/BONK/Bags launch-only filter active")
                pause = 5
                self._receive(ws, "pump", self._handle_pump_packet)
            except Exception as exc:
                # Never log endpoint URLs or any response secret.
                self._set_state("pump", "BACKOFF", type(exc).__name__)
                with self.lock:
                    self.state["pump"]["reconnects"] += 1
                LOG.warning("free launchpad stream disconnected: %s; retry in %ds",
                            type(exc).__name__, pause)
                time.sleep(pause)
                pause = min(pause * 2, 300)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass

    def _bags_indexer_loop(self):
        """Slow public, launchpad-filtered provenance backup; never price/trade."""
        pause = 120
        while True:
            try:
                req = Request(BAGS_LAUNCHES_URL, headers={
                    "Accept": "application/json",
                    "User-Agent": "Solana-Paper-Launchpad-Observer/1.0"})
                with urlopen(req, timeout=8) as response:
                    if response.status != 200:
                        raise OSError("Indexer HTTP status not OK")
                    raw = response.read(800001)
                if len(raw) > 800000:
                    raise ValueError("Indexer response oversized")
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    rows = payload.get("launches", payload.get("data", payload.get("items", [])))
                    if isinstance(rows, dict):
                        rows = rows.get("launches", rows.get("items", []))
                else:
                    rows = payload
                if not isinstance(rows, list):
                    raise ValueError("Indexer payload has no launch list")
                accepted = 0
                for row in rows[:50]:
                    if identify_bags_indexer_launchpad(row) != "BAGS":
                        continue
                    mint = row.get("mint") or row.get("tokenMint")
                    if not valid_address(mint):
                        continue
                    event = {
                        "action": "create", "mint": mint, "symbol": row.get("symbol"),
                        "name": row.get("name"), "pool": "meteora-launchpad",
                        "platform": "bags", "_verified_source": "BAGS_INDEXER_LAUNCHES"}
                    accepted += int(self.ingest_pump(event))
                self._set_state("bags", "CONNECTED")
                with self.lock:
                    self.state["bags"]["last_message"] = self.clock()
                pause = 120
                if accepted and self.screened == 0:
                    LOG.info("public Bags-only indexer returned launchpad-attributed candidates")
            except Exception as exc:
                self._set_state("bags", "BACKOFF", type(exc).__name__)
                with self.lock:
                    self.state["bags"]["reconnects"] += 1
                pause = max(600 if isinstance(exc, HTTPError) and exc.code == 429 else 120, pause)
                LOG.warning("Bags public launch indexer unavailable: %s; retry in %ds",
                            type(exc).__name__, pause)
            time.sleep(pause)

    def _handle_helius(self, packet):
        if not isinstance(packet, dict) or packet.get("method") != "logsNotification":
            return
        value = (packet.get("params") or {}).get("result") or {}
        details = value.get("value") or {}
        signature = details.get("signature")
        if not isinstance(signature, str) or len(signature) < 60:
            return
        # logsSubscribe mentions show address activity only, NOT wallet buys.
        with self.lock:
            self.wallet_count += 1
            self.wallet_activities.appendleft({
                "signature": signature[:100], "at": self.clock(),
                "kind": "ADDRESS_ACTIVITY_NOT_PROVEN_BUY"
            })

    def _helius_loop(self):
        pause = 5
        while True:
            ws = None
            try:
                ws = self._connect(HELIUS_URL + quote(self.helius_key, safe=""))
                # Standard logsSubscribe, one address per subscription; single
                # WS. "mentions" is not equivalent to a verified smart-money buy.
                for n, wallet in enumerate(PUBLIC_WATCHLIST[:3], 1):
                    ws.send(json.dumps({"jsonrpc": "2.0", "id": n,
                        "method": "logsSubscribe",
                        "params": [{"mentions": [wallet["address"]]},
                                   {"commitment": "confirmed"}]}))
                self._set_state("helius", "CONNECTED")
                pause = 5
                self._receive(ws, "helius", self._handle_helius)
            except Exception as exc:
                # Do not expose API key through exception text.
                self._set_state("helius", "BACKOFF", type(exc).__name__)
                with self.lock:
                    self.state["helius"]["reconnects"] += 1
                LOG.warning("Helius standard wallet activity stream unavailable: %s",
                            type(exc).__name__)
                time.sleep(pause)
                pause = min(pause * 2, 300)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass

    def start(self, on_update=None):
        if self.started:
            return
        self.started = True
        if self.enabled:
            threading.Thread(target=self._pump_loop, name="solana-free-new-token-ws", daemon=True).start()
        if self.helius_enabled:
            threading.Thread(target=self._helius_loop, name="solana-helius-wallet-ws", daemon=True).start()
        if self.bags_indexer_enabled:
            threading.Thread(target=self._bags_indexer_loop, name="solana-bags-provenance", daemon=True).start()

        def one_second():
            while True:
                if self.tick() and on_update:
                    on_update()
                time.sleep(1.0)
        threading.Thread(target=one_second, name="solana-one-second-event-screen", daemon=True).start()

    def snapshot(self):
        with self.lock:
            return {
                "mode": "THREE_LAUNCHPAD_ONLY",
                "allowed_launchpads": list(ALLOWED_LAUNCHPADS),
                "platform_counts": dict(self.launchpad_counts),
                "every_second_local_screen": True,
                "price_websocket": False,
                "fills_from_stream": False,
                "metered_streams_enabled": False,
                "pump": dict(self.state["pump"]),
                "bags_indexer": dict(self.state["bags"]),
                "helius": dict(self.state["helius"]),
                "pending_count": self.pending.qsize(),
                "screened_total": self.screened,
                "created_total": self.created_count,
                "migrated_total": self.migrated_count,
                "dropped_total": self.dropped,
                "wallet_activity_total": self.wallet_count,
                "last_event_at": self.last_seen_at,
                "last_screen_at": self.last_screen_at,
                "leads": list(self.leads)[:25],
                "recent_wallet_events": list(self.wallet_activities)[:6],
                "limitations": "只收有可信來源標記的Pump/BONK/Bags發射事件；其他Raydium/Meteora及不明來源直接丟棄。新幣並非可成交報價；Bags公用索引可能延遲或限流。",
            }
