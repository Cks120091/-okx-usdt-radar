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

    def test_stream_does_not_start_wallet_monitor_even_with_old_settings(self):
        observer = LiveChainObserver(env={
            "PAPER_RPC_WALLET_EVENTS":"1",
            "PAPER_RPC_MAX_WALLETS":"13",
            "PAPER_HELIUS_WALLET_EVENTS":"1",
            "HELIUS_API_KEY":"old-key-not-used",
            "PAPER_PUMP_FREE_EVENTS":"0",
            "PAPER_BAGS_INDEXER_ENABLED":"0",
        })
        snap = observer.snapshot()
        self.assertNotIn("wallet_rpc", snap)
        self.assertNotIn("wallet_roster", snap)
        self.assertNotIn("recent_wallet_events", snap)
        self.assertFalse(hasattr(observer, "wallet_monitor"))
        self.assertEqual(0, len([x for x in snap if "wallet" in x.lower()]))
        self.assertNotIn("old-key-not-used", str(snap))

    def test_shadow_early_strength_requires_two_real_quote_snapshots(self):
        from solana_paper.strong_watch import EarlyStrengthWatch
        mint="7"*44
        p=EarlyStrengthWatch(clock=lambda:1000)
        p.register([(mint,"PUMP",1000)],1000)
        self.assertEqual("AWAITING_QUOTE",p.snapshot(1000)["items"][0]["state"])
        row={"mint":mint,"symbol":"EARLY","price":.0002,
             "liquidity_usd":35000,"volume_h1":10000,
             "h1_buys":31,"h1_sells":20,
             "m5_change":3,"h1_change":12}
        p.observe({mint:row},[mint],1050)
        self.assertEqual("BUILDING",p.snapshot(1050)["items"][0]["state"])
        p.observe({mint:row},[mint],1100)  # ignore repeat within 90s
        self.assertEqual(1,p.snapshot(1100)["items"][0]["quote_samples"])
        p.observe({mint:row},[mint],1200)
        snap=p.snapshot(1200)
        self.assertEqual("STRONG_PREP",snap["items"][0]["state"])
        self.assertEqual(2,snap["items"][0]["quote_samples"])
        self.assertFalse(snap["trade_decisions_affected"])
        self.assertEqual(0,snap["extra_market_requests"])
        self.assertEqual("STALE_DATA",p.snapshot(1800)["items"][0]["state"])
        # no hidden trading even with strong snapshot; class only observes.
        self.assertFalse(hasattr(p,"buy"))

    def test_shadow_pool_retains_older_leads_without_trading_them(self):
        from solana_paper.strong_watch import EarlyStrengthWatch
        import os
        abc="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        class Feed:
            def __init__(self):
                self.queries=[]
            def quotes(self,mints):
                self.queries.append(list(mints))
                return []
            def discover(self):
                raise AssertionError("Never run full-market discovery")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                os.environ,{"PAPER_LAUNCHPAD_ONLY":"1"}):
            settings=replace(Settings(),db_path=str(Path(tmp)/"watch.db"))
            feed=Feed()
            rt=PaperRuntime(settings,feed)
            rt.chain_stream.clock=lambda:1100
            rt.early_watch.clock=lambda:1100
            for i in range(35):
                rt.chain_stream.ingest_pump({
                    "mint":abc[i]*44, "action":"create","pool":"pump",
                    "symbol":"RESEARCH"},1000+i)
            self.assertTrue(rt.chain_stream.tick(1100))
            core=rt.chain_stream.scoped_mints(limit=20)
            self.assertEqual(20,len(core))
            before_trades=rt.book.trades()
            rt.run_once(1100)
            self.assertEqual(1,len(feed.queries))
            self.assertEqual(30,len(feed.queries[0]))
            self.assertEqual(core,feed.queries[0][:20])
            self.assertTrue(set(feed.queries[0][20:]).isdisjoint(core))
            self.assertEqual(before_trades,rt.book.trades())
            obs=rt.status()["early_strength_watch"]
            self.assertEqual(35,obs["registered"])
            self.assertEqual(15,obs["outside_latest_20"])
            self.assertEqual(0,obs["valid_quote_samples"])
            self.assertEqual("OBSERVE_ONLY",obs["mode"])
            self.assertEqual(0,obs["extra_market_requests"])

    def test_older_shadow_strong_candidate_cannot_create_paper_buy(self):
        import os
        alphabet="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        oldest=alphabet[0]*44
        now=[1100.]
        class Feed:
            def __init__(self): self.calls=0
            def discover(self): raise AssertionError("No whole-market discovery")
            def quotes(self,mints):
                self.calls+=1
                if oldest not in mints:
                    return []
                return [{
                    "chainId":"solana",
                    "baseToken":{"address":oldest,"symbol":"OLD"},
                    "pairAddress":"B"*44,"dexId":"raydium",
                    "priceUsd":"0.0002","pairCreatedAt":900000,
                    "liquidity":{"usd":100000},
                    "volume":{"h1":32000,"h24":100000},
                    "txns":{"h1":{"buys":60,"sells":30}},
                    "priceChange":{"m5":4.0,"h1":12.0},
                    "fdv":600000,"marketCap":500000,
                }]
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,{"PAPER_LAUNCHPAD_ONLY":"1"}):
            rt=PaperRuntime(replace(Settings(),db_path=str(Path(tmp)/"shadow.db")),Feed())
            rt.chain_stream.clock=lambda:now[0]
            rt.early_watch.clock=lambda:now[0]
            for i in range(21):
                rt.chain_stream.ingest_pump({
                    "mint":alphabet[i]*44,"action":"create",
                    "pool":"pump","symbol":"TEST"},1000+i)
                rt.chain_stream.tick(1000+i)  # distinct actual dispatch times
            self.assertNotIn(oldest,rt.chain_stream.scoped_mints(limit=20))
            rt.run_once(1100)
            self.assertEqual([],rt.book.trades())
            self.assertEqual([],rt.status()["candidates"])
            self.assertEqual("BUILDING",next(t for t in rt.status()[
                "early_strength_watch"]["items"] if t["mint"]==oldest)["state"])
            now[0]=1250
            rt.run_once(1250)
            self.assertEqual([],rt.book.trades())
            self.assertEqual([],rt.shadow_judge.book.trades())
            self.assertEqual("STRONG_PREP",next(t for t in rt.status()[
                "early_strength_watch"]["items"] if t["mint"]==oldest)["state"])

    def test_shadow_pool_caps_records_and_does_not_use_unknown_launchpads(self):
        from solana_paper.strong_watch import EarlyStrengthWatch
        abc="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        p=EarlyStrengthWatch(clock=lambda:1000)
        p.register([(abc[i%len(abc)]*43+abc[i//len(abc)],"PUMP",1000)
                    for i in range(135)],1000)
        self.assertEqual(120,p.snapshot(1000)["registered"])
        self.assertEqual(10,len(p.choose_shadow([],100,1000)))
        self.assertEqual(0,p.snapshot(20000)["registered"])

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
