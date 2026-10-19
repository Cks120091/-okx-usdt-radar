"""Offline regression tests: confirm-only alerts, safe dedup and opt-in Telegram."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from radar.formal_alerts import FormalSignalTracker, TelegramFormalSender


class Sender:
    enabled = True

    def __init__(self):
        self.events = []
        self.fail = False

    def send(self, event):
        if self.fail:
            raise RuntimeError("offline")
        self.events.append(event)


class FormalAlertsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = tmp.name
        self.now = 1800000000.0
        self.sender = Sender()
        self.tracker = FormalSignalTracker(
            self.directory, sender=self.sender, clock=lambda: self.now
        )

    def signal(self, **changes):
        payload = dict(
            inst_id="BTC-USDT-SWAP", direction="LONG", actionable=True,
            signal_stage="CONFIRMED", closed_candle_ts=int((self.now-900) * 1000),
            trigger_id="btc-short-001", entry_low="100", entry_high="101",
            stop_loss="98", lifecycle={"terminal": False, "status": "ACTIVE"},
        )
        payload.update(changes)
        return SimpleNamespace(**payload)

    def report(self, **changes):
        payload = dict(status="OK", actionable=True, signals=[self.signal()],
                       long_signals=[])
        payload.update(changes)
        return SimpleNamespace(**payload)

    def test_confirmed_signal_delivered_once_and_persisted_across_restart(self):
        report = self.report()
        events = self.tracker.process(report)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "FORMAL_TRIGGER_CONFIRMED")
        self.assertFalse(events[0]["auto_ordering"])
        self.assertEqual(self.sender.events[0]["symbol"], "BTC-USDT-SWAP")
        self.assertFalse(self.tracker.process(report))
        restarted = FormalSignalTracker(self.directory, sender=self.sender,
                                        clock=lambda: self.now)
        self.assertFalse(restarted.process(report))
        self.assertEqual(len(self.sender.events), 1)
        self.assertEqual(len((Path(self.directory) / "formal_alerts.jsonl")
                             .read_text(encoding="utf-8").splitlines()), 1)

    def test_early_incomplete_terminal_stale_are_never_formal(self):
        cases = [
            self.report(status="DATA_INCOMPLETE"),
            self.report(actionable=False),
            self.report(signals=[self.signal(signal_stage="EARLY")]),
            self.report(signals=[self.signal(actionable=False)]),
            self.report(signals=[self.signal(lifecycle={"terminal": True})]),
            self.report(signals=[self.signal(closed_candle_ts=1)]),
            self.report(signals=[self.signal(direction="NEUTRAL")]),
            self.report(signals=[self.signal(closed_candle_ts=int((self.now+90)*1000))]),
        ]
        for report in cases:
            self.assertFalse(self.tracker.process(report))
        self.assertFalse(self.sender.events)

    def test_new_episode_and_long_signal_each_get_own_notice(self):
        short = self.signal()
        long = self.signal(trigger_id="btc-long-001",
                           closed_candle_ts=int((self.now-14400)*1000))
        self.tracker.process(self.report(signals=[short], long_signals=[long]))
        self.assertEqual(len(self.sender.events), 2)
        self.assertEqual({x["horizon"] for x in self.sender.events}, {"SHORT","LONG"})
        self.tracker.process(self.report(signals=[self.signal(trigger_id="new-episode")],
                                         long_signals=[]))
        self.assertEqual(len(self.sender.events), 3)

    def test_delivery_failure_retries_without_marking_seen(self):
        self.sender.fail = True
        self.assertFalse(self.tracker.process(self.report()))
        self.assertFalse((Path(self.directory) / "formal_alert_seen.json").exists())
        self.sender.fail = False
        self.assertEqual(len(self.tracker.process(self.report())), 1)

    def test_unconfigured_telegram_keeps_sensitive_fields_private(self):
        sender = TelegramFormalSender(token="", chat_id="")
        self.assertFalse(sender.enabled)
        self.assertEqual(sender.send({}), None)
        with self.assertRaises(ValueError):
            TelegramFormalSender(token="invalid", chat_id="123")

    def test_telegram_sender_posts_expected_payload_no_orders(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): return b'{"ok":true}'
        requests = []
        def opener(request, timeout):
            requests.append(request)
            self.assertEqual(timeout, 10)
            return Response()
        sender = TelegramFormalSender(token="123:abc", chat_id="456", opener=opener)
        sender.send(self.tracker.process(self.report())[0])
        self.assertEqual(len(requests), 1)
        payload = json.loads(requests[0].data)
        self.assertIn("正式訊號", payload["text"])
        self.assertNotIn("自動下單：已啟動", payload["text"])


if __name__ == "__main__":
    unittest.main()
