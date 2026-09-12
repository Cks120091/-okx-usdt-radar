import json
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from radar.config import AppConfig
from radar.history_jobs import HistoryManager, _update


class FakePushNotifier:
    available = True

    def __init__(self):
        self.sent = []

    def normalize_subscription(self, payload):
        if not isinstance(payload, dict) or not payload.get("endpoint"):
            raise ValueError("bad subscription")
        return dict(payload)

    def subscription_key(self, subscription):
        return str(subscription.get("endpoint") or "")

    def send(self, subscription, payload):
        self.sent.append((dict(subscription), dict(payload)))


class HistoryCompletionPushTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.notifier = FakePushNotifier()
        runtime = SimpleNamespace(
            config=AppConfig(data_dir=self.temp.name),
            _running=False,
            _scan_lock=threading.RLock(),
            push_notifier=self.notifier,
        )
        self.manager = HistoryManager(runtime)
        self.addCleanup(self.manager.close)

    def _start(self):
        subscription = {"endpoint": "https://push.example/device", "keys": {"p256dh": "x", "auth": "y"}}
        with patch.object(self.manager, "_spawn"):
            result = self.manager.command(
                "start",
                days={"days": 7, "inst_id": "BTC-USDT-SWAP", "push_subscription": subscription},
                token=self.manager.token,
            )
        return result["id"]

    def test_complete_job_sends_one_server_push_with_result_summary(self):
        job = self._start()
        summary = {
            "trigger_signals": 18,
            "overall": {
                "total": 18,
                "resolved": 15,
                "wins": 10,
                "losses": 5,
                "rate_pct": 66.7,
            },
        }
        _update(
            self.manager.path,
            job,
            status="COMPLETE",
            summary=json.dumps(summary),
        )

        self.manager._send_job_push_if_terminal(job)
        self.manager._send_job_push_if_terminal(job)

        self.assertEqual(len(self.notifier.sent), 1)
        _, payload = self.notifier.sent[0]
        self.assertEqual(payload["title"], "BTC 勝率更新完成")
        self.assertIn("7日｜Trigger 18｜TP1 10 / SL 5｜先達率 66.7%", payload["body"])
        self.assertEqual(payload["url"], "/history-scan?inst_id=BTC-USDT-SWAP")
        self.assertEqual(payload["kind"], "HISTORY_COMPLETION")

    def test_paused_job_does_not_notify_but_later_completion_does(self):
        job = self._start()
        _update(self.manager.path, job, status="PAUSED", error="使用者暫停")
        self.manager._send_job_push_if_terminal(job)
        self.assertEqual(self.notifier.sent, [])
        self.assertIn(job, self.manager._job_push_subscriptions)

        _update(
            self.manager.path,
            job,
            status="PARTIAL_COMPLETE",
            summary=json.dumps({
                "overall": {"total": 4, "wins": 2, "losses": 1, "rate_pct": 66.7}
            }),
        )
        self.manager._send_job_push_if_terminal(job)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("部分資料", self.notifier.sent[0][1]["title"])

    def test_error_job_sends_failure_notice_without_persisting_subscription(self):
        job = self._start()
        _update(self.manager.path, job, status="ERROR", error="歷史資料取得失敗")
        self.manager._send_job_push_if_terminal(job)

        self.assertEqual(len(self.notifier.sent), 1)
        _, payload = self.notifier.sent[0]
        self.assertEqual(payload["title"], "BTC 勝率更新未完成")
        self.assertIn("歷史資料取得失敗", payload["body"])
        status = self.manager.status()
        self.assertNotIn("push_subscription", status)


if __name__ == "__main__":
    unittest.main()
