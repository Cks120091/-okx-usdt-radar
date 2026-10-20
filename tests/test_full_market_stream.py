"""Offline all-contract fanout tests; no OKX network or trading permissions."""
import json
import threading
import time
import unittest
from types import SimpleNamespace

from radar.full_market_stream import FullMarketStream


class FakeClient:
    def __init__(self, count):
        self.ids = [f"COIN{i:03d}-USDT-SWAP" for i in range(count)]

    def get_usdt_swap_instruments(self):
        return [SimpleNamespace(inst_id=name, state="live") for name in self.ids]

    def get_swap_tickers(self):
        return {
            name: SimpleNamespace(quote_volume_24h=5_000_000.0 if index else 100_000.0)
            for index, name in enumerate(self.ids)
        }


class FakeSocket:
    def __init__(self, uri, **kwargs):
        self.uri = uri
        self.callbacks = kwargs
        self.requests = []
        self.done = threading.Event()

    def send(self, message):
        body = json.loads(message)
        self.requests.append(body)
        for arg in body["args"]:
            self.callbacks["on_message"](
                self, json.dumps({"event": "subscribe", "arg": arg})
            )

    def run_forever(self, **kwargs):
        self.callbacks["on_open"](self)
        self.done.wait(5)
        self.callbacks["on_close"](self, None, None)

    def close(self):
        self.done.set()


def wait_until(predicate, timeout=3):
    start = time.monotonic()
    while not predicate():
        if time.monotonic() - start > timeout:
            return False
        time.sleep(.01)
    return True


class FullMarketStreamTests(unittest.TestCase):
    def setUp(self):
        self.sockets = []
        self.client = FakeClient(205)

        def factory(uri, **kwargs):
            sock = FakeSocket(uri, **kwargs)
            self.sockets.append(sock)
            return sock

        self.stream = FullMarketStream(
            self.client, "wss://ws.okx.com/ws/v5/public", lambda _: None,
            ws_factory=factory, shard_size=80, subscribe_batch=40,
        )
        self.addCleanup(self.stream.stop)

    def test_every_contract_subscribed_even_illiquid(self):
        self.stream.refresh_universe()
        self.assertTrue(wait_until(lambda: self.stream.status()["coverage_complete"]))
        status = self.stream.status()
        self.assertEqual(status["listed_symbols"], 205)
        self.assertEqual(status["expected_shards"], 3)
        self.assertEqual(status["connected_shards"], 3)
        self.assertEqual(status["acknowledged_symbols"], 205)
        all_subscribed = [
            item["instId"]
            for sock in self.sockets
            for request in sock.requests
            for item in request["args"]
        ]
        self.assertCountEqual(all_subscribed, self.client.ids)
        self.assertEqual(self.stream.quote_volume(self.client.ids[0]), 100_000)
        self.assertTrue(all(len(r["args"]) <= 40 for s in self.sockets for r in s.requests))

    def test_listing_changes_without_reconnecting_unchanged_universe(self):
        self.stream.refresh_universe()
        self.assertTrue(wait_until(lambda: self.stream.status()["coverage_complete"]))
        current_sockets = len(self.sockets)
        self.stream.refresh_universe()
        self.assertEqual(len(self.sockets), current_sockets)
        self.client.ids = [*self.client.ids, "LATEST-USDT-SWAP"]
        self.stream.refresh_universe()
        self.assertTrue(wait_until(lambda: self.stream.status()["coverage_complete"]))
        self.assertEqual(self.stream.status()["listed_symbols"], 206)
        self.assertTrue(all(sock.done.is_set() for sock in self.sockets[:current_sockets]))

    def test_discovery_error_preserves_last_good_universe(self):
        self.stream.refresh_universe()
        self.assertTrue(wait_until(lambda: self.stream.status()["coverage_complete"]))
        original = self.stream.symbols
        self.client.get_usdt_swap_instruments = lambda: (_ for _ in ()).throw(
            TimeoutError("offline")
        )
        with self.assertRaises(TimeoutError):
            self.stream.refresh_universe()
        self.assertEqual(self.stream.symbols, original)

    def test_invalid_capacity_and_empty_universe_fail_closed(self):
        with self.assertRaises(ValueError):
            FullMarketStream(self.client, "wss://ws.okx.com/ws/v5/public",
                             lambda _: None, shard_size=200)
        self.client.ids = []
        with self.assertRaises(RuntimeError):
            self.stream.refresh_universe()
        self.assertEqual(self.stream.status()["listed_symbols"], 0)


if __name__ == "__main__":
    unittest.main()
