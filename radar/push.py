from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from typing import Any, Callable
from urllib.parse import urlparse


LOGGER = logging.getLogger("okx_radar.push")

try:  # Keep the radar available even if the optional push dependency is broken.
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid
    from pywebpush import webpush
except Exception:  # pragma: no cover - exercised only by a broken deployment image.
    serialization = None
    Vapid = None
    webpush = None


_ALLOWED_PUSH_HOSTS = {
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
}
_ALLOWED_PUSH_HOST_SUFFIXES = (
    ".push.apple.com",
    ".notify.windows.com",
)
_MAX_ENDPOINT_LENGTH = 4096


class PushSubscriptionError(ValueError):
    """A browser supplied an invalid or unsafe Web Push subscription."""


class DisabledPushNotifier:
    available = False

    def __init__(self, reason: str = "掃描通知服務目前無法啟用"):
        self.reason = reason

    def public_config(self) -> dict[str, Any]:
        return {
            "available": False,
            "public_key": None,
            "key_id": None,
            "temporary_key": True,
            "note": self.reason,
        }

    def normalize_subscription(self, payload: Any) -> dict[str, Any]:
        raise PushSubscriptionError(self.reason)

    def subscription_key(self, subscription: dict[str, Any]) -> str:
        return ""

    def send(self, subscription: dict[str, Any], payload: dict[str, Any]) -> None:
        return


class WebPushNotifier:
    """Send one-scan completion notices without persisting browser endpoints."""

    available = True

    def __init__(
        self,
        *,
        vapid: Any | None = None,
        sender: Callable[..., Any] | None = None,
        subject: str | None = None,
        temporary_key: bool | None = None,
    ):
        if Vapid is None or serialization is None or webpush is None:
            raise RuntimeError("Web Push dependencies are unavailable")

        configured_key = os.environ.get("RADAR_VAPID_PRIVATE_KEY", "").strip()
        if vapid is None and configured_key:
            vapid = (
                Vapid.from_pem(configured_key.encode("utf-8"))
                if "-----BEGIN" in configured_key
                else Vapid.from_string(configured_key)
            )
        if vapid is None:
            vapid = Vapid()
            vapid.generate_keys()

        self._vapid = vapid
        self._sender = sender or webpush
        self._subject = (
            subject
            or os.environ.get("RADAR_VAPID_SUBJECT", "").strip()
            or "https://okx-radar-v2-live.onrender.com"
        )
        self._temporary_key = (
            not bool(configured_key) if temporary_key is None else temporary_key
        )
        public_bytes = self._vapid.public_key.public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
        self._public_key = _base64url(public_bytes)
        self._key_id = hashlib.sha256(public_bytes).hexdigest()[:16]

    def public_config(self) -> dict[str, Any]:
        return {
            "available": True,
            "public_key": self._public_key,
            "key_id": self._key_id,
            "temporary_key": self._temporary_key,
            "note": (
                "通知只綁定本輪掃描；主機重啟後裝置會安全地重新訂閱。"
                if self._temporary_key
                else "掃描完成與失敗時可在頁面關閉後通知。"
            ),
        }

    def normalize_subscription(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise PushSubscriptionError("通知訂閱格式不正確")

        endpoint = str(payload.get("endpoint") or "").strip()
        if not endpoint or len(endpoint) > _MAX_ENDPOINT_LENGTH:
            raise PushSubscriptionError("通知端點不正確")
        parsed = urlparse(endpoint)
        hostname = (parsed.hostname or "").lower()
        try:
            port = parsed.port
        except ValueError as exc:
            raise PushSubscriptionError("通知端點不正確") from exc
        if (
            parsed.scheme != "https"
            or not hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or port not in (None, 443)
            or not _allowed_push_host(hostname)
        ):
            raise PushSubscriptionError("通知端點不是受信任的瀏覽器推播服務")

        keys = payload.get("keys")
        if not isinstance(keys, dict):
            raise PushSubscriptionError("通知加密金鑰缺失")
        p256dh = str(keys.get("p256dh") or "").strip()
        auth = str(keys.get("auth") or "").strip()
        try:
            decoded_p256dh = _decode_base64url(p256dh)
            decoded_auth = _decode_base64url(auth)
        except (TypeError, ValueError) as exc:
            raise PushSubscriptionError("通知加密金鑰格式不正確") from exc
        if len(decoded_p256dh) != 65 or decoded_p256dh[0] != 4:
            raise PushSubscriptionError("通知裝置公鑰不正確")
        if len(decoded_auth) != 16:
            raise PushSubscriptionError("通知驗證金鑰不正確")

        return {
            "endpoint": endpoint,
            "keys": {"p256dh": p256dh, "auth": auth},
        }

    def subscription_key(self, subscription: dict[str, Any]) -> str:
        endpoint = str(subscription.get("endpoint") or "")
        return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()

    def send(self, subscription: dict[str, Any], payload: dict[str, Any]) -> None:
        normalized = self.normalize_subscription(subscription)
        self._sender(
            subscription_info=normalized,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            vapid_private_key=self._vapid,
            vapid_claims={"sub": self._subject},
            content_encoding="aes128gcm",
            ttl=300,
            timeout=10,
        )


def build_push_notifier() -> WebPushNotifier | DisabledPushNotifier:
    try:
        return WebPushNotifier()
    except Exception:
        LOGGER.exception("Unable to initialize Web Push; radar remains available")
        return DisabledPushNotifier()


def _allowed_push_host(hostname: str) -> bool:
    return hostname in _ALLOWED_PUSH_HOSTS or any(
        hostname.endswith(suffix) for suffix in _ALLOWED_PUSH_HOST_SUFFIXES
    )


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64url(value: str) -> bytes:
    if not value or len(value) > 256:
        raise ValueError("invalid base64url length")
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(
        (value + padding).encode("ascii"),
        altchars=b"-_",
        validate=True,
    )
