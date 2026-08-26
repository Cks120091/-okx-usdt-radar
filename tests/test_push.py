import base64
import json
import unittest

from radar.push import PushSubscriptionError, WebPushNotifier


def encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def subscription(host: str = "web.push.apple.com") -> dict:
    return {
        "endpoint": f"https://{host}/Qfixture-capability-token",
        "expirationTime": None,
        "keys": {
            "p256dh": encoded(b"\x04" + b"p" * 64),
            "auth": encoded(b"a" * 16),
        },
    }


class WebPushTests(unittest.TestCase):
    def test_public_config_contains_a_browser_compatible_vapid_key(self):
        notifier = WebPushNotifier()

        config = notifier.public_config()
        public_key = config["public_key"]
        decoded = base64.urlsafe_b64decode(public_key + "=" * (-len(public_key) % 4))

        self.assertTrue(config["available"])
        self.assertTrue(config["temporary_key"])
        self.assertEqual(len(config["key_id"]), 16)
        self.assertEqual(len(decoded), 65)
        self.assertEqual(decoded[0], 4)

    def test_normalizes_supported_browser_push_hosts_and_strips_extra_fields(self):
        notifier = WebPushNotifier()

        for host in (
            "web.push.apple.com",
            "fcm.googleapis.com",
            "updates.push.services.mozilla.com",
            "wns2-par02p.notify.windows.com",
        ):
            with self.subTest(host=host):
                normalized = notifier.normalize_subscription(subscription(host))
                self.assertEqual(normalized["endpoint"], subscription(host)["endpoint"])
                self.assertEqual(set(normalized), {"endpoint", "keys"})
                self.assertEqual(set(normalized["keys"]), {"p256dh", "auth"})

    def test_rejects_untrusted_or_malformed_endpoints(self):
        notifier = WebPushNotifier()
        invalid_endpoints = (
            "http://web.push.apple.com/Qfixture",
            "https://127.0.0.1/Qfixture",
            "https://evil.push.apple.com.attacker.example/Qfixture",
            "https://web.push.apple.com:8443/Qfixture",
            "https://web.push.apple.com:bad/Qfixture",
            "https://user@web.push.apple.com/Qfixture",
        )

        for endpoint in invalid_endpoints:
            with self.subTest(endpoint=endpoint):
                payload = subscription()
                payload["endpoint"] = endpoint
                with self.assertRaises(PushSubscriptionError):
                    notifier.normalize_subscription(payload)

    def test_rejects_missing_or_malformed_encryption_keys(self):
        notifier = WebPushNotifier()
        fixtures = (
            {"endpoint": subscription()["endpoint"]},
            {**subscription(), "keys": {"p256dh": "bad", "auth": "bad"}},
            {
                **subscription(),
                "keys": {"p256dh": encoded(b"\x04" + b"p" * 63), "auth": encoded(b"a" * 16)},
            },
            {
                **subscription(),
                "keys": {"p256dh": encoded(b"\x04" + b"p" * 64), "auth": encoded(b"a" * 15)},
            },
        )

        for payload in fixtures:
            with self.subTest(payload=payload):
                with self.assertRaises(PushSubscriptionError):
                    notifier.normalize_subscription(payload)

    def test_send_uses_encrypted_web_push_settings_and_minimal_json(self):
        calls = []

        def sender(**kwargs):
            calls.append(kwargs)

        notifier = WebPushNotifier(sender=sender, subject="mailto:radar@example.com")
        payload = {
            "title": "OKX 雷達掃描完成",
            "body": "最新市場報告已完成，點擊查看結果。",
            "url": "/",
            "status": "SUCCESS",
        }

        notifier.send(subscription(), payload)

        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["subscription_info"], notifier.normalize_subscription(subscription()))
        self.assertEqual(json.loads(call["data"]), payload)
        self.assertEqual(call["vapid_claims"], {"sub": "mailto:radar@example.com"})
        self.assertEqual(call["content_encoding"], "aes128gcm")
        self.assertEqual(call["ttl"], 300)
        self.assertEqual(call["timeout"], 10)


if __name__ == "__main__":
    unittest.main()
