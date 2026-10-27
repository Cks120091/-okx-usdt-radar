"""Offline/read-only validation for the free creation/migration WebSocket lane."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from solana_paper.chain_stream import (
    LiveChainObserver, FREE_PUMP_METHODS, interpret_pump_event, PUMP_URL
)
from solana_paper.engine import PaperRuntime, Settings

MINT = "A" * 44


class StopThread(BaseException):
    pass


class FakeSocket:
    def __init__(self, frames=()):
        self.frames = iter(frames)
        self.sent = []
        self.closed = False
    def send(self, data):
        self.sent.append(json.loads(data))
    def settimeout(self, value):
        self.timeout = value
    def recv(self):
        try:
            return next(self.frames)
        except StopIteration:
            raise OSError("closed")
    def close(self):
        self.closed = True
    def ping(self):
        pass


class LiveStreamsTest(unittest.TestCase):
    def test_creation_migration_parsing_and_unpriced_fail_closed(self):
        now = 2000.0
        created = interpret_pump_event({"mint": MINT, "action": "create", "pool": "pump",
                                        "symbol": "NEW"}, now)
        self.assertEqual(created["state"], "AWAITING_MARKET_VERIFICATION")
        self.assertFalse(created["trade_eligible"])
        self.assertEqual(created["risk"], "UNKNOWN")
        self.assertEqual("MIGRATED", interpret_pump_event(
            {"mint": MINT, "action": "migrate", "pool": "pump"}, now)["kind"])
        self.assertIsNone(interpret_pump_event({"mint": MINT, "action": "buy", "pool": "pump"}, now))
        self.assertIsNone(interpret_pump_event({"mint": MINT, "price": 1}, now))
        self.assertIsNone(interpret_pump_event({"mint": "bad", "action": "create", "pool": "pump"}, now))

    def test_one_second_screen_dedup_status_and_no_prices(self):
        c = LiveChainObserver(env={"PAPER_PUMP_FREE_EVENTS": "1"}, clock=lambda: 1000)
        self.assertTrue(c.ingest_pump({"mint": MINT, "action": "create", "pool": "pump"}, 1000))
        self.assertTrue(c.ingest_pump({"mint": MINT, "action": "create", "pool": "pump"}, 1000))
        self.assertTrue(c.tick(1000))
        data=c.snapshot()
        self.assertEqual(1, data["created_total"])
        self.assertEqual(1, data["screened_total"])
        self.assertEqual(0, data["migrated_total"])
        self.assertEqual(0, data["pending_count"])
        self.assertTrue(data["every_second_local_screen"])
        self.assertFalse(data["price_websocket"])
        self.assertFalse(data["metered_streams_enabled"])
        self.assertFalse(data["leads"][0]["trade_eligible"])
        self.assertFalse(c.tick(1001))
        self.assertTrue(c.ingest_pump({"mint": MINT, "action": "migrate", "pool": "pump"},1002))
        self.assertTrue(c.tick(1002))
        self.assertEqual(1,c.snapshot()["migrated_total"])

    def test_free_stream_api_rejection_is_not_treated_as_new_token(self):
        c=LiveChainObserver(env={"PAPER_PUMP_FREE_EVENTS":"1"})
        with self.assertRaises(RuntimeError):
            c._handle_pump_packet({"status":"error","message":"Invalid API key"})
        self.assertEqual(0,c.snapshot()["screened_total"])
        self.assertEqual(0,c.snapshot()["pending_count"])
        c._handle_pump_packet({"message":"Successfully subscribed"})
        self.assertEqual(0,c.snapshot()["pending_count"])

    def test_free_pump_subscription_only_one_ws_and_no_metered_methods(self):
        socket=FakeSocket([json.dumps({"action": "create", "pool": "pump","mint":MINT, "symbol":"ABC"})])
        created=[]
        c=LiveChainObserver(env={"PAPER_PUMP_FREE_EVENTS":"1"},
                            clock=lambda:1000,connection_factory=lambda url:(created.append(url) or socket))
        with patch("solana_paper.chain_stream.time.sleep", side_effect=StopThread()):
            with self.assertRaises(StopThread):
                c._pump_loop()
        self.assertEqual([PUMP_URL],created)
        self.assertEqual([], socket.sent)  # Public source streams without commands
        self.assertEqual((), FREE_PUMP_METHODS)
        self.assertNotIn("subscribeAccountTrade",repr(socket.sent))
        self.assertNotIn("subscribeTokenTrade",repr(socket.sent))
        self.assertTrue(socket.closed)
        self.assertTrue(c.tick(1000))
        self.assertEqual(c.snapshot()["created_total"],1)


    def test_strict_platform_filter_excludes_generic_raydium_meteora_unknown(self):
        c=LiveChainObserver(env={"PAPER_PUMP_FREE_EVENTS":"1",
            "PAPER_BAGS_INDEXER_ENABLED":"0"},clock=lambda:1021)
        for i,(pool,platform,label) in enumerate([
            ("pump",None,"PUMP"),
            ("raydium-launchpad","bonk","BONK"),
            ("meteora-launchpad","bags","BAGS")]):
            self.assertTrue(c.ingest_pump({"action":"create","pool":pool,
                "platform":platform,"mint":str(i+1)*44,"symbol":label},1000+i))
        for event in [
            {"action":"create","pool":"raydium-launchpad","platform":"custom"},
            {"action":"create","pool":"meteora-launchpad"},
            {"action":"create","pool":"meteora-launchpad","platform":"moonshot"},
            {"action":"create","pool":"raydium-cpmm","platform":"bonk"},
            {"action":"create","pool":"pump","platform":"custom"},
            {"action":"create","symbol":"BONK"},
            {"action":"buy","pool":"pump"},
        ]:
            self.assertFalse(c.ingest_pump({"mint":"8"*44,**event},1010))
        self.assertTrue(c.tick(1020))
        v=c.snapshot()
        self.assertEqual({"PUMP":1,"BONK":1,"BAGS":1},v["platform_counts"])
        self.assertEqual({"PUMP","BONK","BAGS"},set(x["launchpad"] for x in v["leads"]))
        self.assertEqual(3,len(c.scoped_mints()))
        self.assertFalse(v["metered_streams_enabled"])

    def test_bags_indexer_requires_exact_launchpad_identity(self):
        from solana_paper.launchpad_scope import identify_bags_indexer_launchpad as cls
        self.assertEqual("BAGS",cls({"launchpad":"Bags"}))
        self.assertEqual("BAGS",cls({"launchpad":"BagsProtocol"}))
        self.assertEqual("BAGS",cls({"launch":{"launchpad":"Bags"}}))
        self.assertIsNone(cls({"launchpad":"Moonshot","symbol":"BAGS"}))
        self.assertIsNone(cls({"protocol":"meteora_dbc"}))

    def test_production_scan_queries_only_attested_launchpad_mints(self):
        import os
        class ScopedFeed:
            def __init__(self):
                self.discovers=0
                self.queries=[]
            def discover(self):
                self.discovers+=1
                raise AssertionError("Must NOT scan whole market")
            def quotes(self,mints):
                self.queries.append(list(mints))
                return []
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"PAPER_LAUNCHPAD_ONLY":"1"}):
            settings=replace(Settings(),db_path=str(Path(tmp)/"paper.db"))
            feed=ScopedFeed()
            rt=PaperRuntime(settings,feed)
            rt.chain_stream.clock=lambda:1001
            no_leads=rt.run_once(1000)
            self.assertEqual("WAITING_LAUNCHPAD_EVENTS",no_leads["status"])
            self.assertEqual([],feed.queries)
            self.assertEqual(0,feed.discovers)
            self.assertTrue(rt.chain_stream.ingest_pump(
                {"action":"create","pool":"raydium-launchpad",
                 "platform":"bonk","mint":MINT},1000))
            rt.chain_stream.tick(1000)
            rt.run_once(1001)
            self.assertEqual([[MINT]],feed.queries)
            self.assertEqual(0,feed.discovers)
            self.assertEqual([],rt.book.trades())

    def test_public_rpc_requires_no_key_and_has_bounded_ws_subscriptions(self):
        from solana_paper.public_wallet_rpc import (
            PublicWalletRpcMonitor, PUBLIC_WS_URL)
        fake=FakeSocket([
            *(json.dumps({"id":i,"result":100+i}) for i in range(1,11)),
            json.dumps({"method":"logsNotification","params":{
                "subscription":101,"result":{"value":{
                    "signature":"a"*88,"err":None}}}})
        ])
        observer=LiveChainObserver(env={
            "PAPER_RPC_WALLET_EVENTS":"1",
            "PAPER_RPC_MAX_WALLETS":"10",
            "HELIUS_API_KEY":"unused-old-key",
            "PAPER_HELIUS_WALLET_EVENTS":"1",
            "PAPER_PUMP_FREE_EVENTS":"0",
        },connection_factory=lambda url:fake)
        self.assertTrue(observer.rpc_wallet_enabled)
        self.assertEqual(13,observer.snapshot()["wallet_roster_total"])
        self.assertEqual(10,observer.snapshot()["wallet_subscriptions_selected"])
        self.assertEqual("SOLANA_PUBLIC_RPC",observer.snapshot()["wallet_rpc"]["provider"])
        with self.assertRaises(OSError):
            observer.wallet_monitor._ws_once()
        self.assertEqual(10,len(fake.sent))
        self.assertEqual(10,observer.snapshot()["wallet_rpc"]["subscriptions_confirmed"])
        self.assertTrue(all(x["method"]=="logsSubscribe" for x in fake.sent))
        self.assertTrue(all(len(x["params"][0]["mentions"])==1 for x in fake.sent))
        self.assertEqual(1,observer.snapshot()["wallet_activity_total"])
        self.assertEqual("ADDRESS_ACTIVITY_NOT_PROVEN_BUY",
                         observer.snapshot()["recent_wallet_events"][0]["kind"])
        self.assertEqual(PUBLIC_WS_URL,"wss://api.mainnet.solana.com/")
        self.assertNotIn("unused-old-key",str(observer.snapshot()))

    def test_public_rpc_disabled_without_touching_old_helius_settings(self):
        observer=LiveChainObserver(env={
            "PAPER_RPC_WALLET_EVENTS":"0", "PAPER_HELIUS_WALLET_EVENTS":"1",
            "HELIUS_API_KEY":"unused"})
        self.assertEqual("DISABLED", observer.snapshot()["wallet_rpc"]["state"])
        self.assertEqual(0,observer.snapshot()["wallet_subscriptions_selected"])

    def test_sourced_wallet_roster_not_fake_profit_ranking(self):
        from solana_paper.wallet_roster import roster,selected,ACTIVITY_LEADERBOARD
        from solana_paper.intelligence import valid_address
        wallets=roster()
        self.assertEqual(13,len(wallets))
        self.assertEqual(13,len({w["address"] for w in wallets}))
        self.assertTrue(all(valid_address(w["address"]) for w in wallets))
        self.assertTrue(all(not w["pnl_verified"] and not w["win_rate_verified"] for w in wallets))
        self.assertTrue(all(w["source"]==ACTIVITY_LEADERBOARD
            for w in wallets if w["category"]=="ACTIVE_RESEARCH_CANDIDATE"))
        self.assertEqual(10,len(selected()))
        self.assertEqual(13,len(selected(100)))
        self.assertEqual(0,len(selected(0)))

    def test_public_rpc_confirmed_activity_only_not_copytrading(self):
        from solana_paper.public_wallet_rpc import PublicWalletRpcMonitor
        from solana_paper.wallet_roster import selected
        monitor=PublicWalletRpcMonitor(wallets=selected(3),env={
            "PAPER_RPC_WALLET_EVENTS":"1"},clock=lambda:1000)
        sig="b"*88
        def event(sub,error=None):
            return {"method":"logsNotification","params":{
                "subscription":sub,"result":{"value":{"signature":sig,"err":error}}}}
        monitor.handle_ws_message(event(7))
        self.assertEqual(0,monitor.snapshot()["wallet_activity_total"])
        monitor.handle_ws_message({"id":1,"result":7})
        monitor.handle_ws_message(event(7,{"InstructionError":[0,"Custom"]}))
        self.assertEqual(0,monitor.snapshot()["wallet_activity_total"])
        monitor.handle_ws_message(event(7))
        monitor.handle_ws_message(event(7))
        self.assertEqual(1,monitor.snapshot()["wallet_activity_total"])
        self.assertEqual("SOLANA_PUBLIC_WS",
                         monitor.snapshot()["recent_wallet_events"][0]["source"])
        with self.assertRaises(RuntimeError):
            monitor.handle_ws_message({"id":2,"error":{"message":"rejected"}})

    def test_public_rpc_http_fallback_baseline_and_429_backoff(self):
        from solana_paper.public_wallet_rpc import (
            PublicWalletRpcMonitor, PUBLIC_HTTP_URL)
        from solana_paper.wallet_roster import selected
        from urllib.error import HTTPError
        from unittest.mock import Mock
        clock=[1000.]
        monitor=PublicWalletRpcMonitor(wallets=selected(3),clock=lambda:clock[0],
                env={"PAPER_RPC_FALLBACK_WALLETS":"1","PAPER_RPC_POLL_SECONDS":"10"})
        self.assertEqual(120,monitor.poll_seconds)
        self.assertEqual(1,monitor.poll_limit)
        initial="a"*88
        new="b"*88
        results=[[{"signature":initial,"err":None}],
                 [{"signature":new,"err":None},{"signature":initial,"err":None}]]
        monitor._signatures=lambda addr: results.pop(0)
        self.assertTrue(monitor.poll_once())
        self.assertEqual(0,monitor.snapshot()["wallet_activity_total"])
        clock[0]=1180
        self.assertTrue(monitor.poll_once())
        self.assertEqual(1,monitor.snapshot()["wallet_activity_total"])
        self.assertEqual("SOLANA_PUBLIC_HTTP_FALLBACK",
                         monitor.snapshot()["recent_wallet_events"][0]["source"])
        def rate_limit(addr):
            raise HTTPError(PUBLIC_HTTP_URL,429,"Rate limited",{"Retry-After":"900"},None)
        monitor._signatures=rate_limit
        clock[0]=1350
        self.assertFalse(monitor.poll_once())
        self.assertEqual("BACKOFF",monitor.snapshot()["status"]["state"])
        self.assertGreaterEqual(monitor.snapshot()["status"]["http_backoff_until"],2250)
        self.assertFalse(monitor.poll_once())
        self.assertEqual(1,monitor.snapshot()["wallet_activity_total"])

    def test_no_event_can_place_paper_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=replace(Settings(),db_path=str(Path(tmp)/"paper.db"))
            class EmptyFeed:
                def discover(self):
                    return {"mints":[],"pairs":[],"sources":{},"paid":{}}
                def quotes(self,mints):
                    return []
            rt=PaperRuntime(s,EmptyFeed())
            stream=rt.chain_stream
            self.assertTrue(stream.ingest_pump({"mint":MINT,"action":"create","pool":"pump"},1000))
            stream.tick(1000)
            self.assertEqual([],rt.book.trades())
            self.assertEqual([],rt.run_once(1000)["buys"])
            self.assertEqual([],rt.book.trades())
            self.assertEqual(1,rt.status()["chain_stream"]["created_total"])
            self.assertEqual([],rt.status()["candidates"])

if __name__=="__main__":
    unittest.main()
