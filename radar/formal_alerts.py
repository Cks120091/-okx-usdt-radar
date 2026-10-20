"""Formal, closed-candle trigger notices. Analysis-only; never places orders.

Notices are distinct from live ticker momentum advisory warnings. Persistent
identity deduplication prevents repeated alerts for the same Signal Episode.
Telegram delivery is opt-in and uses only secrets held in server environment.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

from .continuation_focus import is_continuation_focus, is_continuation_promotion

LOG = logging.getLogger("okx_radar.formal_alerts")
_TELEGRAM_TOKEN_RE = re.compile(r"^[0-9]+:[A-Za-z0-9_-]+$")
_TELEGRAM_CHAT_RE = re.compile(r"^-?[0-9]+$")


def _value(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    return text[:80] if text else "—"


class TelegramFormalSender:
    """Optional Telegram transport; bot token is never exposed to reports."""

    def __init__(self, token: str | None = None, chat_id: str | None = None,
                 opener: Callable[..., Any] = urlopen):
        self._token = (token if token is not None else os.getenv("RADAR_TELEGRAM_BOT_TOKEN", "")).strip()
        self._chat_id = (chat_id if chat_id is not None else os.getenv("RADAR_TELEGRAM_CHAT_ID", "")).strip()
        self._opener = opener
        if bool(self._token) != bool(self._chat_id):
            raise ValueError("Telegram bot token and chat ID must both be set")
        if self._token and (not _TELEGRAM_TOKEN_RE.fullmatch(self._token)
                            or not _TELEGRAM_CHAT_RE.fullmatch(self._chat_id)):
            raise ValueError("Invalid Telegram credentials format")

    @property
    def enabled(self) -> bool:
        return bool(self._token)

    def send(self, event: dict[str, Any]) -> None:
        if not self.enabled:
            return
        direction = "做多" if event["direction"] == "LONG" else "做空"
        text = ("🔔 OKX 雷達｜趨勢延續已觸發\n"
                f"{event['symbol']} · {direction} · {event['horizon']}\n"
                f"Entry：{_value(event['entry_low'])} ～ {_value(event['entry_high'])}\n"
                f"SL：{_value(event['stop_loss'])}\n"
                "這是已收線正式掃描訊號，非自動下單。\n"
                "下單前仍需更新現價、檢查可進場／禁止追價與交易成本。\n"
                + ("⚠️ 原始突破／反轉交易計畫價位並未重算；請以進場前更新為準。" if event.get("original_plan_retained") else ""))
        request = Request(
            f"https://api.telegram.org/bot{self._token}/sendMessage",
            data=json.dumps({"chat_id": self._chat_id, "text": text},
                            ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self._opener(request, timeout=10) as response:
            payload = json.loads(response.read(4096))
        if payload.get("ok") is not True:
            raise RuntimeError("Telegram rejected the notification")


class FormalSignalTracker:
    """Dedupe scanner-confirmed Signal Episodes across service restarts."""

    def __init__(self, data_dir: str, *, sender: Any = None,
                 clock: Callable[[], float] = time.time):
        self._directory = Path(data_dir)
        self._seen_path = self._directory / "formal_alert_seen.json"
        self._event_path = self._directory / "formal_alerts.jsonl"
        self._clock = clock
        self._delivery_lock = threading.Lock()
        self.sender = sender if sender is not None else TelegramFormalSender()
        self._seen = self._load_seen()
        self._recent: deque[dict[str, Any]] = deque(maxlen=30)

    def _load_seen(self) -> dict[str, float]:
        try:
            loaded = json.loads(self._seen_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return {k: float(v) for k, v in loaded.items()
                        if isinstance(k, str) and isinstance(v, (float, int))}
        except (OSError, ValueError, TypeError):
            pass
        return {}

    def _save_seen(self) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        tmp_path = self._seen_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(self._seen, separators=(",", ":")),
                            encoding="utf-8")
        tmp_path.replace(self._seen_path)

    def status(self) -> dict[str, Any]:
        return {
            "telegram_configured": bool(getattr(self.sender, "enabled", False)),
            "recent_formal_alerts": list(self._recent)[-10:],
            "analysis_only": True,
            "auto_ordering": False,
        }

    def process(self, report: Any) -> list[dict[str, Any]]:
        """Serialize overlapping candidate and scheduled notification passes."""
        with self._delivery_lock:
            return self._process_locked(report)

    def _process_locked(self, report: Any) -> list[dict[str, Any]]:
        """Report is the output of the existing formal scanner, not live tickers."""
        # An unconfigured sender is not a delivered notification. Do not
        # mark episodes seen during installation/dry-run before Telegram setup.
        if not getattr(self.sender, "enabled", False):
            return []
        if (str(getattr(report, "status", "")).upper() == "DATA_INCOMPLETE"
                or getattr(report, "actionable", True) is False):
            return []
        now = self._clock()
        alerts = []
        for horizon, field in (("SHORT", "signals"), ("LONG", "long_signals")):
            for signal in getattr(report, field, []) or []:
                # An old BREAKOUT plan can be marked EXTENDED even when a
                # distinct new continuation is genuinely confirmed. Notify
                # about that *event*, not about permission to reuse old Entry.
                promoted = is_continuation_promotion(signal)
                if not is_continuation_focus(signal):
                    continue
                if not promoted:
                    if getattr(signal, "actionable", True) is False:
                        continue
                    if str(getattr(signal, "signal_stage", "")).upper() not in {"CONFIRMED", "REENTRY"}:
                        continue
                lifecycle = getattr(signal, "lifecycle", None) or {}
                if not isinstance(lifecycle, dict) or lifecycle.get("terminal") is True:
                    continue
                if str(lifecycle.get("status", "")).upper() in {
                    "CLOSED", "INVALIDATED", "COMPLETED", "EXPIRED", "MISSED"}:
                    continue
                direction = str(getattr(signal, "direction", "")).upper()
                symbol = str(getattr(signal, "inst_id", ""))
                timestamp = getattr(signal, "closed_candle_ts", 0)
                trigger_id = str(getattr(signal, "trigger_id", "") or "")
                if not symbol or direction not in {"LONG", "SHORT"}:
                    continue
                try:
                    candle_ms = int(timestamp)
                except (ValueError, TypeError, OverflowError):
                    continue
                # Candle timestamp can represent its open: allow up to two bars.
                max_age = 2 * (900 if horizon == "SHORT" else 3600)
                reference_ms = candle_ms
                if promoted:
                    try:
                        reference_ms = int(
                            lifecycle.get("continuation_promotion_confirmation_ms") or 0
                        )
                    except (TypeError, ValueError, OverflowError):
                        continue
                age = now - reference_ms / 1000
                if reference_ms <= 0 or not math.isfinite(age) or age < -60 or age > max_age:
                    continue
                # Fall back to closed candle timestamp only when older Episodes
                # have no explicit trigger ID.
                story = getattr(signal, "market_story", {}) or {}
                trigger = story.get("trigger", {}) if isinstance(story, dict) else {}
                try:
                    confirmation_ms = int(trigger.get("confirmation_ts") or 0) if isinstance(trigger, dict) else 0
                except (TypeError, ValueError, OverflowError):
                    confirmation_ms = 0
                # A reserve Episode is promoted ONLY by the independently
                # confirmed, durable core reentry event marker.
                lifecycle_promotion = 0
                if is_continuation_promotion(signal):
                    try:
                        lifecycle_promotion = int(
                            lifecycle.get("continuation_promotion_confirmation_ms") or 0
                        )
                    except (ValueError, TypeError, OverflowError):
                        continue
                confirmed_reentry = str(getattr(signal, "signal_stage", "")).upper() == "REENTRY"
                event_marker = (
                    f"{trigger_id or candle_ms}:PROMOTION:{lifecycle_promotion}"
                    if lifecycle_promotion > 0
                    else f"{trigger_id or candle_ms}:REENTRY:{confirmation_ms}"
                    if confirmed_reentry and confirmation_ms > 0
                    else (trigger_id or candle_ms)
                )
                identity = f"{horizon}|{symbol}|{direction}|{event_marker}"
                if identity in self._seen:
                    continue
                event = {
                    "type": "CONTINUATION_PROMOTION_CONFIRMED" if promoted else "FORMAL_TRIGGER_CONFIRMED",
                    "symbol": symbol,
                    "direction": direction,
                    "horizon": horizon,
                    "trigger_id": trigger_id or None,
                    "closed_candle_ts": candle_ms,
                    "observed_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                    "entry_low": getattr(signal, "entry_low", None),
                    "entry_high": getattr(signal, "entry_high", None),
                    "stop_loss": getattr(signal, "stop_loss", None),
                    "analysis_only": True,
                    "auto_ordering": False,
                    "signal_type": "CONTINUATION",
                    "promoted_from_backup": promoted,
                    "original_plan_retained": promoted,
                    "entry_permitted": False,  # notice is never permission to open an order
                    "message": (
                        "備用型態後續已獨立確認趨勢延續；原 Entry/SL/TP 為舊交易計畫，必須重新確認現價與可進場資格。"
                        if is_continuation_promotion(signal)
                        else "趨勢延續正式收線訊號已確認；入場仍須更新現價與風控。"
                    ),
                }
                try:
                    self.sender.send(event)
                except Exception as exc:
                    # Never log the Telegram bot token or a secret-bearing URL.
                    LOG.warning("Formal alert delivery failed: %s", type(exc).__name__)
                    continue  # Retry on next complete scan.
                self._seen[identity] = now
                # Keep at most 2000 recent identities. No history changes to
                # Signal Episodes, and no private trading credentials.
                if len(self._seen) > 2000:
                    self._seen = dict(sorted(self._seen.items(),
                                             key=lambda item: item[1])[-1500:])
                try:
                    self._save_seen()
                    with self._event_path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(event, ensure_ascii=False,
                                           separators=(",", ":")) + "\n")
                except OSError:
                    LOG.warning("Could not persist formal alert state")
                self._recent.append(event)
                alerts.append(event)
        return alerts
