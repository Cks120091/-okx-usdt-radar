"""Read-only free launch websocket and independent Solana public-RPC wallet activity.

Strict provider allowlists. Never subscribes to metered PumpPortal trade
feeds or constructs any trade/signature/private-key transaction.
Every second: process queued launch events locally, NOT HTTP polling.
Launchs are unverified leads only; a websocket event is never a price quote.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import logging
import os
import queue
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from .intelligence import valid_address
from .wallet_roster import roster, selected
from .public_wallet_rpc import PublicWalletRpcMonitor
from .launchpad_scope import ALLOWED_LAUNCHPADS, identify_stream_launchpad, identify_bags_indexer_launchpad

LOG = logging.getLogger("solana_paper.stream")
# Provider explicitly marks launch pool/platform; stream includes other events
# which are dropped BEFORE entering the queue. One socket, no subscriptions.
PUMP_URL = "wss://stream.pumpapi.io/"
BAGS_LAUNCHES_URL = "https://data-api.bags.fm/v2/launches?launchpads=Bags%2CBagsProtocol&limit=30"
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
        # Helius is not used. Solana Labs PUBLIC RPC is the sole wallet
        # activity source; opt-out and caps control its shared-IP footprint.
        self.wallet_roster = roster()
        self.rpc_wallet_enabled = env.get("PAPER_RPC_WALLET_EVENTS", "1") == "1"
        try:
            maximum = int(env.get("PAPER_RPC_MAX_WALLETS", "10"))
        except (TypeError, ValueError):
            maximum = 10
        self.active_wallets = selected(max(1, min(13, maximum)))
        self.wallet_monitor = PublicWalletRpcMonitor(
            wallets=self.active_wallets, env=env,
            connection_factory=connection_factory, clock=self.clock)
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
        self.state = {
            "pump": {"state": "DISABLED" if not self.enabled else "CONNECTING",
                     "last_error": "", "last_connected": 0.0,
                     "last_message": 0.0, "reconnects": 0},
            "bags": {"state": "CONNECTING" if self.bags_indexer_enabled else "DISABLED",
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
        """Only recently observed named-launchpad mints, never market-wide."""
        now = self.clock()
        with self.lock:
            fresh = [(mint, meta) for mint, meta in self.provenance.items()
                     if meta[0] in ALLOWED_LAUNCHPADS and 0 <= now - meta[1] < 14400]
            return [mint for mint, _ in sorted(
                fresh, key=lambda entry: entry[1][1], reverse=True
            )[:max(0, min(25, int(limit)))]]

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
                    # Historical launches from a re-polled indexer must never
                    # masquerade as fresh new-token notifications.
                    launched = row.get("launchedAt") or row.get("createdAt")
                    if not isinstance(launched, str):
                        continue
                    try:
                        launched_at = datetime.fromisoformat(
                            launched.replace("Z", "+00:00")).timestamp()
                    except (ValueError, OverflowError):
                        continue
                    if not 0 <= self.clock() - launched_at <= 3600:
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

    def start(self, on_update=None):
        if self.started:
            return
        self.started = True
        if self.enabled:
            threading.Thread(target=self._pump_loop, name="solana-free-new-token-ws", daemon=True).start()
        self.wallet_monitor.start(on_update)
        if self.bags_indexer_enabled:
            threading.Thread(target=self._bags_indexer_loop, name="solana-bags-provenance", daemon=True).start()

        def one_second():
            while True:
                if self.tick() and on_update:
                    on_update()
                time.sleep(1.0)
        threading.Thread(target=one_second, name="solana-one-second-event-screen", daemon=True).start()

    def snapshot(self):
        wallet = self.wallet_monitor.snapshot()
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
                "wallet_rpc": wallet["status"],
                "pending_count": self.pending.qsize(),
                "screened_total": self.screened,
                "created_total": self.created_count,
                "migrated_total": self.migrated_count,
                "dropped_total": self.dropped,
                "wallet_activity_total": wallet["wallet_activity_total"],
                "wallet_roster_total": len(self.wallet_roster),
                "wallet_roster_research_candidates": sum(
                    w["category"] == "ACTIVE_RESEARCH_CANDIDATE" for w in self.wallet_roster),
                "wallet_subscriptions_selected": len(self.active_wallets) if self.rpc_wallet_enabled else 0,
                "wallet_roster": [{
                    "address": w["address"], "label": w["label"],
                    "category": w["category"], "source": w["source"],
                    "observed_at": w["observed_at"],
                    "pnl_verified": w["pnl_verified"],
                    "currently_selected": self.rpc_wallet_enabled and w in self.active_wallets,
                } for w in self.wallet_roster],
                "last_event_at": self.last_seen_at,
                "last_screen_at": self.last_screen_at,
                "leads": list(self.leads)[:25],
                "recent_wallet_events": wallet["recent_wallet_events"],
                "limitations": "只收有可信來源標記的Pump/BONK/Bags發射事件；其他Raydium/Meteora及不明來源直接丟棄。新幣並非可成交報價；Bags公用索引可能延遲或限流。",
            }
