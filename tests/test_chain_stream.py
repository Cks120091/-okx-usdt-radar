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
        created = interpret_pump_event({"mint": MINT, "txType": "create",
                                        "symbol": "NEW"}, now)
        self.assertEqual(created["state"], "AWAITING_MARKET_VERIFICATION")
        self.assertFalse(created["trade_eligible"])
        self.assertEqual(created["risk"], "UNKNOWN")
        self.assertEqual("MIGRATED", interpret_pump_event(
            {"mint": MINT, "txType": "migration"}, now)["kind"])
        self.assertIsNone(interpret_pump_event({"mint": MINT, "txType": "buy"}, now))
        self.assertIsNone(interpret_pump_event({"mint": MINT, "price": 1}, now))
        self.assertIsNone(interpret_pump_event({"mint": "bad", "txType": "create"}, now))

    def test_one_second_screen_dedup_status_and_no_prices(self):
        c = LiveChainObserver(env={"PAPER_PUMP_FREE_EVENTS": "1"}, clock=lambda: 1000)
        self.assertTrue(c.ingest_pump({"mint": MINT, "txType": "create"}, 1000))
        self.assertTrue(c.ingest_pump({"mint": MINT, "txType": "create"}, 1000))
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
        self.assertTrue(c.ingest_pump({"mint": MINT, "txType": "migrate"},1002))
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
        socket=FakeSocket([json.dumps({"txType": "create","mint":MINT, "symbol":"ABC"})])
        created=[]
        c=LiveChainObserver(env={"PAPER_PUMP_FREE_EVENTS":"1"},
                            clock=lambda:1000,connection_factory=lambda url:(created.append(url) or socket))
        with patch("solana_paper.chain_stream.time.sleep", side_effect=StopThread()):
            with self.assertRaises(StopThread):
                c._pump_loop()
        self.assertEqual([PUMP_URL],created)
        self.assertEqual([x["method"] for x in socket.sent],list(FREE_PUMP_METHODS))
        self.assertNotIn("subscribeAccountTrade",repr(socket.sent))
        self.assertNotIn("subscribeTokenTrade",repr(socket.sent))
        self.assertTrue(socket.closed)
        self.assertTrue(c.tick(1000))
        self.assertEqual(c.snapshot()["created_total"],1)

    def test_helius_requires_optin_key_and_only_observes_activity(self):
        absent=LiveChainObserver(env={"PAPER_HELIUS_WALLET_EVENTS":"1"})
        self.assertFalse(absent.helius_enabled)
        self.assertEqual("NOT_CONFIGURED", absent.snapshot()["helius"]["state"])
        key="testSecret111"
        fake=FakeSocket([json.dumps({"method":"logsNotification", "params":{
            "result":{"value":{"signature":"a"*88,"logs":["swap"]}}}})])
        urls=[]
        enabled=LiveChainObserver(env={"PAPER_HELIUS_WALLET_EVENTS":"1",
                                        "HELIUS_API_KEY":key,
                                        "PAPER_PUMP_FREE_EVENTS":"0"},
                                  connection_factory=lambda url:(urls.append(url) or fake))
        self.assertTrue(enabled.helius_enabled)
        with patch("solana_paper.chain_stream.time.sleep", side_effect=StopThread()):
            with self.assertRaises(StopThread):
                enabled._helius_loop()
        self.assertEqual(3,len(fake.sent))
        self.assertTrue(all(s["method"]=="logsSubscribe" for s in fake.sent))
        self.assertEqual(1,enabled.snapshot()["wallet_activity_total"])
        self.assertIn("NOT_PROVEN_BUY",enabled.snapshot()["recent_wallet_events"][0]["kind"])
        self.assertNotIn(key,str(enabled.snapshot()))

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
            self.assertTrue(stream.ingest_pump({"mint":MINT,"txType":"create"},1000))
            stream.tick(1000)
            self.assertEqual([],rt.book.trades())
            self.assertEqual([],rt.run_once(1000)["buys"])
            self.assertEqual([],rt.book.trades())
            self.assertEqual(1,rt.status()["chain_stream"]["created_total"])
            self.assertEqual([],rt.status()["candidates"])

if __name__=="__main__":
    unittest.main()
