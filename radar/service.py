from __future__ import annotations

import inspect
import json
import logging
import re
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from .api import OKXAPIError
from .config import AppConfig
from .models import RadarReport
from .preflight import build_preflight_payload
from .public_payload import public_candidate_payload, public_report_payload
from .push import PushSubscriptionError, build_push_notifier
from .repository import terminal_card_retention_until
from .reporting import (
    load_latest_report,
    load_runtime_state,
    report_markdown,
    save_report,
    save_runtime_state,
)
from .scanner import MarketScanner


LOGGER = logging.getLogger("okx_radar")


def _normalize_scan_mode(value: Any) -> str:
    mode = str(value or "FULL").strip().upper()
    aliases = {
        "15M": "SHORT",
        "SHORT": "SHORT",
        "4H": "LONG",
        "LONG": "LONG",
        "ALL": "FULL",
        "FULL": "FULL",
    }
    normalized = aliases.get(mode)
    if normalized is None:
        raise ValueError("scan_mode must be SHORT, LONG, or FULL")
    return normalized


_SCAN_MODE_LABELS = {
    "SHORT": "15m 掃描",
    "LONG": "4H 掃描",
    "FULL": "全市場掃描（15m＋4H）",
}

_SCAN_MODE_HORIZONS = {
    "SHORT": frozenset({"SHORT"}),
    "LONG": frozenset({"LONG"}),
    "FULL": frozenset({"SHORT", "LONG"}),
}


def _single_scan_error_chain(exc: BaseException) -> list[BaseException]:
    errors: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(errors) < 8:
        errors.append(current)
        current = current.__cause__
    return errors


def _single_scan_error_detail(exc: BaseException) -> str:
    return " ".join(str(error) for error in _single_scan_error_chain(exc)).lower()


def _single_scan_failure_is_retryable(exc: BaseException) -> bool:
    errors = _single_scan_error_chain(exc)
    if any(
        isinstance(error, (OKXAPIError, HTTPError, URLError, TimeoutError, ConnectionError))
        for error in errors
    ):
        return True
    detail = _single_scan_error_detail(exc)
    return any(
        marker in detail
        for marker in (
            "timed out",
            "timeout",
            "connection refused",
            "connection reset",
            "remote end closed",
            "temporary failure",
            "name resolution",
            "rate limit",
            "code=50004",
            "code=50011",
            " 429",
            " 502",
            " 503",
            " 504",
        )
    )


def _single_scan_retry_delay(exc: BaseException) -> float:
    detail = _single_scan_error_detail(exc)
    if "429" in detail or "code=50011" in detail or "rate limit" in detail:
        return 2.25
    return 0.75


def _single_scan_failure_message(exc: Exception) -> str:
    detail = _single_scan_error_detail(exc)
    if any(
        marker in detail
        for marker in (
            "timed out",
            "timeout",
            "connection refused",
            "connection reset",
            "remote end closed",
            "temporary failure",
            "name resolution",
            "502",
            "503",
            "504",
        )
    ):
        return (
            "OKX 公開行情目前連線失敗；系統已自動重試官方主端點與備援端點。"
            "這不是幣種或訊號失效，請稍後再試。"
        )
    if "429" in detail or "code=50011" in detail or "rate limit" in detail:
        return (
            "OKX 暫時限制請求頻率；系統已自動等待並重試。"
            "這不是幣種失效，請約 10 秒後再試。"
        )
    if "k 線" in detail:
        return (
            "OKX 最新 K 線自動重試後仍無法完整取得；不是訊號失效。"
            "系統沒有拿缺漏週期硬算，請稍後再試。"
        )
    return (
        "OKX 最新單幣資料自動重試後仍無法完成分析；"
        "這不是幣種失效，請稍後再試。"
    )


def _normalize_horizon(value: Any) -> str | None:
    return {
        "15M": "SHORT",
        "SHORT": "SHORT",
        "4H": "LONG",
        "LONG": "LONG",
    }.get(str(value or "").strip().upper())


def _latest_confirmation(result: Any, original_direction: str) -> dict[str, Any]:
    """Describe the newest closed-candle evidence without rewriting a plan."""

    if result is None:
        return {
            "status": "DATA_UNAVAILABLE",
            "label": "最新確認資料不足",
            "message": "最新多週期資料不完整；禁止用舊現價判定補算進場資格。",
            "new_entry_allowed": False,
        }
    state = getattr(result, "market_state", None)
    signal = getattr(result, "signal", None)
    direction = str(
        getattr(signal, "direction", "")
        or getattr(state, "direction", "")
        or "NEUTRAL"
    )
    stage = str(
        getattr(signal, "signal_stage", "")
        or getattr(state, "status", "")
        or "WATCH"
    )
    trigger = dict(getattr(state, "trigger", {}) or {})
    if signal is not None:
        signal_story = getattr(signal, "market_story", {}) or {}
        if isinstance(signal_story, dict):
            trigger = dict(signal_story.get("trigger", {}) or trigger)
    noise = dict(trigger.get("noise", {}) or {})
    opposite = direction in ("LONG", "SHORT") and direction != original_direction
    formal = signal is not None and stage in ("EARLY_SIGNAL", "CONFIRMED", "REENTRY")
    item = signal or state
    decision = dict(getattr(item, "decision_context", {}) or {}) if item else {}
    hard_gate = dict(decision.get("hard_gate", {}) or {})
    final = dict(decision.get("final", {}) or {})
    failed_risk_checks = [
        str(check.get("key") or check.get("label") or "risk_warning")
        for check in list(getattr(item, "safety_checks", []) or [])
        if check.get("passed") is False
    ] if item is not None else []
    hard_gate_blockers = [
        str(value) for value in list(hard_gate.get("blockers", []) or [])
    ]
    hard_gate_unknowns = [
        str(value) for value in list(hard_gate.get("unknowns", []) or [])
    ]
    risk_warning_codes = list(
        dict.fromkeys(
            [
                *hard_gate_blockers,
                *hard_gate_unknowns,
                *failed_risk_checks,
            ]
        )
    )

    if opposite:
        # The fresh opposite candidate is not the stored plan.  Its
        # direction-dependent R:R, SL or entry checks must never veto an
        # otherwise valid original Episode. Risk review is advisory, so this
        # direction comparison remains informational only.
        status = "ORIGINAL_DIRECTION_NOT_RECONFIRMED"
        label = "方向比較只供參考"
        message = (
            "最新掃描沒有延續原方向；這項方向比較只供參考，不建立反向判定、"
            "不改寫進場資格，也不終止舊 Episode。"
        )
    elif noise.get("high"):
        status = "HIGH_NOISE"
        label = "疑似假突破・雜訊提醒"
        message = "最新核心週期來回交叉、雜訊偏高；只顯示提醒，不改寫進場資格。"
    elif (
        formal
        and direction == original_direction
        and final.get("new_entry_allowed") is True
    ):
        status = "REVALIDATED"
        label = "原方向重新確認"
        message = "最新已收盤多週期資料仍形成同方向正式 Trigger。"
    elif formal and direction == original_direction:
        status = "SAME_DIRECTION_WAIT"
        label = "原方向仍有效・目前等待"
        message = "最新仍是同方向正式 Trigger，但目前價格位置尚未符合進場條件。"
    elif direction == original_direction:
        status = "ORIGINAL_DIRECTION_STABLE"
        label = "原方向結構仍穩定"
        message = (
            "最新收盤沒有反轉，但也沒有新的正式 Trigger；"
            "原訊號只作生命週期追蹤，尚未進場者先等待。"
        )
    else:
        status = "NO_FORMAL_TRIGGER"
        label = "最新確認不足・只供參考"
        message = (
            "舊訊號仍保留作生命週期追蹤，但最新核心週期已無同方向正式 Trigger；"
            "這項比較不改寫舊 Episode 的現價位置判定。"
        )

    groups = dict(getattr(state, "evidence_groups", {}) or {})
    return {
        "status": status,
        "label": label,
        "message": message,
        "direction": direction,
        "stage": stage,
        "formal_trigger": formal,
        "two_step_reversal_confirmed": False,
        "hard_blockers": [],
        "risk_warnings": risk_warning_codes,
        "closed_candle_ts": getattr(state, "closed_candle_ts", None),
        "group_stances": {
            key: str((value or {}).get("stance", "NEUTRAL"))
            for key, value in groups.items()
            if isinstance(value, dict)
        },
        "new_entry_allowed": status == "REVALIDATED",
    }


def _merge_preflight_confirmation(
    payload: dict[str, Any],
    confirmation: dict[str, Any],
) -> dict[str, Any]:
    merged = deepcopy(payload)
    confirmation = deepcopy(confirmation)
    merged["latest_confirmation"] = deepcopy(confirmation)
    verdict = merged.setdefault("verdict", {})
    lifecycle = merged.setdefault("signal_lifecycle", {})
    merged.setdefault("plan_state", {})
    status = str(confirmation.get("status") or "UNKNOWN").upper()
    legacy_risk_codes = list(
        dict.fromkeys(
            [
                *list(confirmation.get("risk_warnings", []) or []),
                *list(confirmation.get("hard_blockers", []) or []),
            ]
        )
    )
    if status in {
        "OPPOSITE_WARNING",
        "CONFIRMED_REVERSAL",
        "HARD_GATE_BLOCKED",
    }:
        # Backward compatibility for an in-memory/cached V3.4 response.  A
        # direction comparison is no longer allowed to create a reversal
        # verdict or terminate a Signal Episode; only the original SL/TP
        # terminal checks own that transition.
        status = (
            "ORIGINAL_DIRECTION_NOT_RECONFIRMED"
            if status in {"OPPOSITE_WARNING", "CONFIRMED_REVERSAL"}
            else "RISK_WARNING"
        )
        confirmation.update(
            {
                "status": status,
                "label": (
                    "方向比較只供參考"
                    if status == "ORIGINAL_DIRECTION_NOT_RECONFIRMED"
                    else "風險條件只供提醒"
                ),
                "message": (
                    "最新掃描沒有延續原方向；這項方向比較只供參考，不建立"
                    "反向判定、不改寫進場資格，也不終止舊 Episode。"
                    if status == "ORIGINAL_DIRECTION_NOT_RECONFIRMED"
                    else "流動性、Spread、Slippage、R:R 與成交成本只作風險提醒，"
                    "不再改寫目前進場資格或隱藏卡片。"
                ),
                "two_step_reversal_confirmed": False,
                "hard_blockers": [],
                "risk_warnings": legacy_risk_codes,
            }
        )

    confirmation["hard_blockers"] = []
    confirmation["risk_warnings"] = legacy_risk_codes

    # Direction/noise/formal-Trigger comparisons are context only. They do
    # not overwrite the current Entry/SL/TP preflight verdict.  Risk reviews
    # are advisory as well; only the positional/lifecycle verdict decides.

    confirmation["new_entry_allowed"] = bool(verdict.get("actionable"))
    merged["latest_confirmation"] = deepcopy(confirmation)
    merged.setdefault("safety", {})["unified_single_scan"] = True
    merged["safety"]["note"] = (
        "本頁已使用同一次單幣完整掃描，同時核對原訊號生命週期、"
        "最新多週期收盤、現價與成交條件；不再使用兩套互相獨立的判定。"
    )
    return merged


def _canonical_single_decision(
    item: Any | None,
    preflight: dict[str, Any] | None,
    confirmation: dict[str, Any] | None,
) -> dict[str, Any]:
    """Collapse the episode refresh into one user-facing conclusion."""

    decision = deepcopy(getattr(item, "decision_context", {}) or {})
    final = deepcopy(decision.get("final", {}) or {})
    if preflight is None:
        if final:
            decision["final"] = final
        return decision

    verdict = dict(preflight.get("verdict", {}) or {})
    lifecycle = dict(preflight.get("signal_lifecycle", {}) or {})
    plan = dict(preflight.get("plan_state", {}) or {})
    status = str(verdict.get("status", "DATA_UNAVAILABLE")).upper()
    situation = str(verdict.get("situation", "")).upper()
    if status in {"HARD_GATE_BLOCKED", "ANOMALY"}:
        # Normalize cached responses produced before risk checks became
        # advisory. The positional status now owns entry permission; if the
        # old payload did not preserve it, keep the card visible in WAIT
        # instead of fabricating an actionable Entry.
        item_entry = dict(getattr(item, "entry_eligibility", {}) or {})
        status = str(
            verdict.get("position_status")
            or item_entry.get("position_status")
            or item_entry.get("status")
            or "WAIT_RETEST"
        ).upper()
        if status in {"HARD_GATE_BLOCKED", "ANOMALY"}:
            status = "WAIT_RETEST"
        situation = status
        verdict["actionable"] = status == "ENTRY_READY"
        verdict["hard_blockers"] = []
    lifecycle_status = str(lifecycle.get("status", "")).upper()
    plan_status = str(plan.get("status", "")).upper()
    direction = str(preflight.get("direction") or final.get("direction") or "NEUTRAL")
    explicitly_invalidated = (
        status in {"PLAN_INVALIDATED", "INVALIDATED"}
        or situation in {"PLAN_INVALIDATED", "INVALIDATED"}
        or lifecycle_status == "INVALIDATED"
        or plan_status == "INVALIDATED"
    )
    target_completed = not explicitly_invalidated and (
        status in {"TARGET_REACHED", "COMPLETED"}
        or situation in {"TARGET_REACHED", "COMPLETED"}
        or lifecycle_status in {"TARGET_REACHED", "COMPLETED"}
        or plan_status in {"TARGET_REACHED", "COMPLETED"}
    )
    closed_unknown = not explicitly_invalidated and not target_completed and (
        status == "CLOSED_UNKNOWN"
        or situation == "CLOSED_UNKNOWN"
        or lifecycle_status == "CLOSED_UNKNOWN"
        or plan_status == "CLOSED_UNKNOWN"
    )
    # A generic terminal flag remains fail-closed, except when the repository
    # explicitly says the TP/SL order is unknown. Reaching TP is completion,
    # while unknown closure is data-unavailable; neither can reuse the plan.
    terminal_invalidation = explicitly_invalidated or (
        bool(lifecycle.get("terminal"))
        and not target_completed
        and not closed_unknown
    )
    mapped_status = (
        "INVALIDATED"
        if terminal_invalidation
        else "COMPLETED"
        if target_completed
        else "DATA_UNAVAILABLE"
        if closed_unknown
        else "ENTER"
        if status == "ENTRY_READY" and verdict.get("actionable") is True
        else "DATA_UNAVAILABLE"
        if status == "DATA_UNAVAILABLE" or situation == "DATA_UNAVAILABLE"
        else "NO_CHASE"
    if status == "MISSED_ENTRY"
        and situation in {"FAVORABLE_MISSED", "PRICE_TOO_FAR"}
        else "WAIT"
    )
    labels = {
        "INVALIDATED": "交易計畫已失效｜等待全新 Trigger",
        "COMPLETED": "目標已達｜本次交易計畫完成",
        "ENTER": "目前可進｜附風險提醒",
        "DATA_UNAVAILABLE": "資料不足｜禁止新進場",
        "NO_CHASE": "方向仍可追蹤｜禁止追價",
        "NO_EDGE": "風險報酬不值得",
        "WAIT": str(verdict.get("label") or "目前等待確認"),
    }
    reason = str(verdict.get("reason") or "等待下一次完整資料確認")
    confirmation_message = str((confirmation or {}).get("message") or "")
    reasons = [reason]
    if confirmation_message and confirmation_message != reason:
        reasons.append(confirmation_message)
    existing_reasons = list(final.get("reasons", []) or [])
    reasons.extend(existing_reasons)
    wait_codes = {
        "INVALIDATED": ("NEW_TRIGGER_REQUIRED", "等待新的 Trigger／REENTRY"),
        "COMPLETED": ("TARGET_REACHED", "本次機會已完成｜等待全新 Trigger"),
        "DATA_UNAVAILABLE": ("DATA_MISSING", "等待最新完整資料"),
        "NO_CHASE": ("PRICE_TOO_FAR", "等待回到合理進場區或新事件"),
        "NO_EDGE": ("RISK_REWARD", "等待新的合理交易計畫"),
        "WAIT": (str(situation or "ENTRY_CONFIRMATION"), labels["WAIT"]),
    }
    final.update(
        {
            "status": mapped_status,
            "label": labels[mapped_status],
            "direction": direction,
            "direction_label": (
                "做多" if direction == "LONG" else "做空" if direction == "SHORT" else "中性"
            ),
            "new_entry_allowed": mapped_status == "ENTER",
            "trigger_preserved": mapped_status != "INVALIDATED",
            "reasons": list(dict.fromkeys(item for item in reasons if item))[:3],
            "wait_reason": (
                None
                if mapped_status == "ENTER"
                else {
                    "code": (
                        "CLOSED_UNKNOWN"
                        if closed_unknown
                        else wait_codes[mapped_status][0]
                    ),
                    "label": (
                        "舊計畫已關閉｜等待最新完整資料與全新 Trigger"
                        if closed_unknown
                        else wait_codes[mapped_status][1]
                    ),
                }
            ),
            "invalidation_condition": (
                str(preflight.get("original", {}).get("stop_loss"))
                if not final.get("invalidation_condition")
                else final.get("invalidation_condition")
            ),
        }
    )
    decision["final"] = final
    decision["episode_plan_state"] = {
        "status": plan.get("status"),
        "terminal": terminal_invalidation or target_completed or closed_unknown,
        "completed": target_completed,
        "invalidated": terminal_invalidation,
        "closed_unknown": closed_unknown,
        "old_plan_reusable_for_new_entry": bool(
            plan.get("old_plan_reusable_for_new_entry")
            and not terminal_invalidation
            and not target_completed
            and not closed_unknown
        ),
    }
    return decision


def _preflight_terminal_kind(payload: dict[str, Any] | None) -> str | None:
    """Return the durable terminal outcome represented by one preflight."""

    if not isinstance(payload, dict):
        return None
    verdict = dict(payload.get("verdict", {}) or {})
    lifecycle = dict(payload.get("signal_lifecycle", {}) or {})
    plan = dict(payload.get("plan_state", {}) or {})
    values = {
        str(verdict.get("status") or "").upper(),
        str(verdict.get("situation") or "").upper(),
        str(lifecycle.get("status") or "").upper(),
        str(plan.get("status") or "").upper(),
    }
    if values & {"PLAN_INVALIDATED", "INVALIDATED"}:
        return "INVALIDATED"
    if values & {"TARGET_REACHED", "COMPLETED"}:
        return "COMPLETED"
    if values & {"CLOSED_UNKNOWN"}:
        return "CLOSED_UNKNOWN"
    return None


def _project_persisted_preflight_terminal(
    payload: dict[str, Any],
    terminal_kind: str,
    *,
    observed_at: str = "",
    horizon: str = "",
) -> None:
    """Make the response agree with the terminal outcome that won the CAS.

    A closed-candle repository update can race the live ticker check.  The
    durable exact-episode outcome always wins; mutating this request-local
    payload prevents a TP-looking response from masking an already persisted
    stop invalidation (and vice versa).
    """

    verdict = payload.setdefault("verdict", {})
    lifecycle = payload.setdefault("signal_lifecycle", {})
    plan = payload.setdefault("plan_state", {})
    if terminal_kind == "COMPLETED":
        verdict.update(
            {
                "status": "COMPLETED",
                "situation": "TARGET_REACHED",
                "label": "已達止盈｜本次交易計畫完成",
                "reason": "原始 TP1 已到達；舊計畫不可重新進場，請等待新的 Trigger。",
                "actionable": False,
            }
        )
        lifecycle.update(
            {
                "status": "TARGET_REACHED",
                "label": "已觸發・已達止盈",
                "active": False,
                "terminal": True,
                "note": "本次交易計畫已完成；價格回到原 Entry 也不會復活舊訊號。",
            }
        )
        plan_status = "TARGET_REACHED"
        direction_still_valid = True
    elif terminal_kind == "INVALIDATED":
        verdict.update(
            {
                "status": "PLAN_INVALIDATED",
                "situation": "INVALIDATED",
                "label": "已達止損｜本次交易計畫結束",
                "reason": "原始 SL／失效位已被突破；同一筆訊號永久結束。",
                "actionable": False,
            }
        )
        lifecycle.update(
            {
                "status": "INVALIDATED",
                "label": "已觸發・已達止損",
                "active": False,
                "terminal": True,
                "note": "原交易計畫永久失效；必須等待新的 Trigger／REENTRY。",
            }
        )
        plan_status = "INVALIDATED"
        direction_still_valid = False
    else:
        verdict.update(
            {
                "status": "DATA_UNAVAILABLE",
                "situation": "CLOSED_UNKNOWN",
                "label": "資料狀態未知｜舊計畫已關閉",
                "reason": (
                    "訊號資料庫已終止舊計畫，但現有資料無法證明 TP／SL 先後；"
                    "不可沿用舊價位。"
                ),
                "actionable": False,
            }
        )
        lifecycle.update(
            {
                "status": "CLOSED_UNKNOWN",
                "label": "已觸發・終局未知",
                "active": False,
                "terminal": True,
                "note": "結果未知；保留歷史紀錄，但禁止重用舊交易計畫。",
            }
        )
        plan_status = "CLOSED_UNKNOWN"
        direction_still_valid = False
    if observed_at:
        lifecycle["closed_at"] = observed_at
        lifecycle["retention_until"] = terminal_card_retention_until(
            observed_at,
            horizon,
        )
    plan.update(
        {
            "status": plan_status,
            "old_plan_reusable": False,
            "old_plan_reusable_for_new_entry": False,
            "existing_position_plan_active": False,
            "new_entry_status": "CLOSED",
            "new_entry_allowed": False,
            "direction_still_valid": direction_still_valid,
            "new_trigger_required": True,
        }
    )


_HORIZON_CANDIDATE_ARRAYS = {
    "SHORT": ("signals", "watchlist"),
    "LONG": ("long_signals", "long_watchlist"),
}


def _project_horizon_read_only(
    payload: dict[str, Any],
    horizon: str,
    reason: str,
) -> None:
    """Disable entry permission in an API projection, never in the saved model."""

    for field_name in _HORIZON_CANDIDATE_ARRAYS[horizon]:
        for item in payload.get(field_name, []) or []:
            if not isinstance(item, dict):
                continue
            item["actionable"] = False
            item["read_only_reason"] = reason
            decision = item.setdefault("decision_context", {})
            if not isinstance(decision, dict):
                decision = {}
                item["decision_context"] = decision
            final = decision.setdefault("final", {})
            if not isinstance(final, dict):
                final = {}
                decision["final"] = final
            original_status = str(final.get("status") or "UNKNOWN")
            final.setdefault("original_final_status", original_status)
            read_only_status = {
                "STALE": "EXPIRED",
                "ERROR": "UPDATE_FAILED",
                "SCANNING": "WAIT",
                "CORE_PREVIEW": "WAIT",
            }.get(reason, "WAIT")
            read_only_label = {
                "STALE": "資料已過期｜禁止依此進場",
                "ERROR": "更新失敗｜顯示上一輪資料",
                "SCANNING": "掃描中｜顯示上一輪資料",
                "CORE_PREVIEW": "核心預覽｜等待完整風控",
            }.get(reason, "唯讀資料｜禁止進場")
            final["status"] = read_only_status
            final["label"] = read_only_label
            final["new_entry_allowed"] = False
            final["read_only_reason"] = reason
            eligibility = item.get("entry_eligibility")
            if isinstance(eligibility, dict):
                eligibility.setdefault(
                    "original_status",
                    str(eligibility.get("status") or "UNKNOWN"),
                )
                eligibility["status"] = (
                    "EXPIRED" if reason == "STALE" else "WAIT_RETEST"
                )
                eligibility["label"] = read_only_label
                eligibility["reason"] = reason
                eligibility["actionable"] = False
                eligibility["new_entry_allowed"] = False


def _suppress_horizon_projection(
    payload: dict[str, Any],
    horizon: str,
) -> None:
    """Hide a requested horizon's previous snapshot from one API response.

    The saved report and Signal Repository remain untouched.  This only keeps
    an in-flight or failed scan from presenting last round's candidates and
    market ranking as if they belonged to the newly requested round.
    """

    for field_name in _HORIZON_CANDIDATE_ARRAYS[horizon]:
        payload[field_name] = []
    if horizon == "SHORT":
        payload["market_map"] = []
        payload["market_regime_counts"] = {}
        payload["market_bias"] = {}


def _read_only_reasons(
    system_status: str,
    freshness: dict[str, dict[str, Any]],
    requested_horizons: frozenset[str] | set[str],
) -> dict[str, str | None]:
    reasons: dict[str, str | None] = {"SHORT": None, "LONG": None}
    for horizon, item in freshness.items():
        if not item.get("available"):
            continue
        if system_status in {"SCANNING", "ERROR", "CORE_PREVIEW"} and (
            horizon in requested_horizons
        ):
            reasons[horizon] = system_status
        elif item.get("expired"):
            reasons[horizon] = "STALE"
    return reasons


class PreflightError(RuntimeError):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


class RadarRuntime:
    """Single-scan runtime with persisted reports and an optional core preview."""

    def __init__(
        self,
        scanner: MarketScanner,
        config: AppConfig,
        *,
        push_notifier: Any | None = None,
    ):
        self.scanner = scanner
        self.config = config
        self.push_notifier = push_notifier or build_push_notifier()
        # Re-entrant because the public single-scan guard holds this lock for
        # the whole analyze -> reconcile -> preflight -> response transaction,
        # while the legacy inner path also enters it defensively.
        self._scan_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._preflight_lock = threading.Lock()
        self._preflight_cache: dict[
            tuple[str, str, str], tuple[float, dict[str, Any]]
        ] = {}
        self._invalidated_preflight_signals: dict[
            tuple[str, str, str], Any
        ] = {}
        self._terminal_preflight_outcomes: dict[
            tuple[str, str, str], str
        ] = {}
        self._preflight_cache_ttl_seconds = 12.0
        self._latest: RadarReport | None = load_latest_report(config.data_dir)
        self._prune_restored_terminal_cards()
        restored_runtime = load_runtime_state(config.data_dir)
        self._preview: RadarReport | None = None
        self._running = False
        restored_status = str(restored_runtime.get("last_attempt_status") or "").upper()
        if restored_status == "SCANNING":
            restored_status = "ERROR"
            restored_runtime["last_error"] = "上一次掃描在完成前中斷；舊資料不可冒充最新。"
        self._last_error: str | None = (
            str(restored_runtime.get("last_error"))
            if restored_runtime.get("last_error")
            else None
        )
        self._last_attempt_status = (
            restored_status
            if restored_status in {"SUCCESS", "ERROR"}
            else "RESTORED"
            if self._latest is not None
            else "IDLE"
        )
        self._scan_id: str | None = None
        self._scan_started_at: str | None = None
        try:
            self._scan_mode = _normalize_scan_mode(
                restored_runtime.get("scan_mode", "FULL")
            )
        except ValueError:
            self._scan_mode = "FULL"
        self._scan_push_subscriptions: dict[str, dict[str, Any]] = {}
        self._max_scan_push_subscriptions = 8
        self._single_inflight: set[tuple[str, str, str]] = set()
        self._progress: dict[str, Any] = self._idle_progress()

    def _prune_restored_terminal_cards(self) -> None:
        """Move exact CLOSED episodes into their bounded review collections.

        SQLite owns the Signal Episode lifecycle.  A prior report-file write
        can fail after the terminal CAS succeeds, so startup reconciles exact
        trigger identities, restores recent TP/SL cards, and never removes a
        newer active Episode or the unrelated horizon.
        """

        report = self._latest
        repository = getattr(self.scanner, "repository", None)
        terminal_loader = getattr(repository, "preflight_terminal_kind", None)
        recent_loader = getattr(repository, "recent_terminal_signals", None)
        if report is None or not callable(terminal_loader):
            return

        changed = False

        def retained(items: list[Any]) -> list[Any]:
            nonlocal changed
            output: list[Any] = []
            for item in items:
                try:
                    terminal_kind = str(terminal_loader(item) or "").upper()
                except Exception:
                    LOGGER.exception(
                        "Failed to reconcile restored terminal episode %s",
                        getattr(item, "trigger_id", ""),
                    )
                    output.append(item)
                    continue
                if terminal_kind in {
                    "COMPLETED",
                    "INVALIDATED",
                    "CLOSED_UNKNOWN",
                }:
                    changed = True
                    continue
                output.append(item)
            return output

        short_closed = list(report.closed_signals)
        long_closed = list(report.long_closed_signals)
        if callable(recent_loader):
            try:
                short_closed = recent_loader("SHORT")
                long_closed = recent_loader("LONG")
            except Exception:
                LOGGER.exception("Failed to restore recent terminal cards")
        updated_report = replace(
            report,
            signals=retained(list(report.signals)),
            closed_signals=short_closed,
            long_signals=retained(list(report.long_signals)),
            long_closed_signals=long_closed,
        )
        previous_terminal_ids = {
            item.trigger_id
            for item in [*report.closed_signals, *report.long_closed_signals]
        }
        restored_terminal_ids = {
            item.trigger_id for item in [*short_closed, *long_closed]
        }
        if not changed and previous_terminal_ids == restored_terminal_ids:
            return
        self._latest = updated_report
        try:
            save_report(updated_report, self.config.data_dir)
        except Exception:
            # Keep the authoritative in-memory projection clean.  The next
            # restart repeats the exact repository reconciliation.
            LOGGER.exception("Failed to persist restored terminal-card cleanup")

    def stop(self) -> None:
        return

    def trigger_scan(
        self,
        push_subscription: Any | None = None,
        scan_mode: str = "FULL",
    ) -> bool:
        normalized_mode = _normalize_scan_mode(scan_mode)
        normalized_subscription = (
            self.push_notifier.normalize_subscription(push_subscription)
            if push_subscription is not None
            else None
        )
        with self._state_lock:
            if self._single_inflight:
                raise PreflightError(
                    HTTPStatus.CONFLICT,
                    "單幣掃描正在執行；完成後再啟動全市場掃描",
                )
            if self._running:
                if normalized_subscription is not None:
                    self._register_scan_push_locked(normalized_subscription)
                return False
            self._begin_scan_locked(normalized_mode)
            if normalized_subscription is not None:
                self._register_scan_push_locked(normalized_subscription)
        thread = threading.Thread(
            target=self._scan_worker,
            name="radar-on-demand-scan",
            daemon=True,
        )
        thread.start()
        return True

    def push_config(self) -> dict[str, Any]:
        return self.push_notifier.public_config()

    def scan_blocking(self, scan_mode: str = "FULL") -> RadarReport:
        normalized_mode = _normalize_scan_mode(scan_mode)
        with self._state_lock:
            if self._running or self._single_inflight:
                raise RuntimeError("scan already running")
            self._begin_scan_locked(normalized_mode)
        try:
            return self._perform_scan()
        finally:
            with self._state_lock:
                self._running = False

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            system_status, age_seconds, stale = self._system_status_locked()
            horizon_freshness = {
                horizon: self._horizon_freshness_locked(horizon)
                for horizon in ("SHORT", "LONG")
            }
            return {
                "running": self._running,
                "system_status": system_status,
                "runtime_status": system_status,
                "data_status": "STALE" if stale else "FRESH" if self._latest else "NONE",
                "actionable": system_status == "FRESH",
                "snapshot_expired": system_status == "STALE",
                "last_error": self._last_error,
                "last_attempt_status": self._last_attempt_status,
                "has_report": self._latest is not None,
                "has_preview": self._preview is not None and self._running,
                "latest_status": self._latest.status if self._latest else None,
                "latest_generated_at": self._latest.generated_at if self._latest else None,
                "latest_age_seconds": age_seconds,
                "stale_after_seconds": self.config.stale_after_seconds,
                "scan_id": self._scan_id,
                "scan_started_at": self._scan_started_at,
                "scan_mode": self._scan_mode,
                "scan_mode_label": _SCAN_MODE_LABELS[self._scan_mode],
                "horizon_freshness": horizon_freshness,
                "progress": dict(self._progress),
                "analysis_only": True,
                "auto_ordering": False,
            }

    def latest_dict(self) -> dict[str, Any] | None:
        with self._state_lock:
            if self._latest is None:
                return None
            payload = public_report_payload(self._latest)
            system_status, age_seconds, _ = self._system_status_locked()
            horizon_freshness = {
                horizon: self._horizon_freshness_locked(horizon)
                for horizon in ("SHORT", "LONG")
            }
            actionable = system_status == "FRESH" and self._latest.status != "DATA_INCOMPLETE"
            snapshot_expired = system_status == "STALE"
            unavailable_horizons = (
                _SCAN_MODE_HORIZONS[self._scan_mode]
                if system_status == "SCANNING"
                or (
                    system_status == "ERROR"
                    and self._last_attempt_status == "ERROR"
                )
                else frozenset()
            )
            horizon_actionable = {
                horizon: (
                    self._latest.status != "DATA_INCOMPLETE"
                    and item["available"]
                    and not item["expired"]
                    and (
                        system_status == "FRESH"
                        or (
                            system_status in {"SCANNING", "ERROR"}
                            and horizon not in unavailable_horizons
                        )
                    )
                )
                for horizon, item in horizon_freshness.items()
            }
            read_only_reasons = _read_only_reasons(
                system_status,
                horizon_freshness,
                unavailable_horizons,
            )
            payload["runtime_status"] = system_status
            payload["actionable"] = actionable
            payload["snapshot_expired"] = snapshot_expired
            payload["latest_age_seconds"] = age_seconds
            payload["horizon_freshness"] = horizon_freshness
            payload["max_signals"] = self.config.max_signals
            payload["safety"]["horizon_actionable"] = horizon_actionable
            payload["horizon_read_only_reasons"] = read_only_reasons
            payload["safety"]["horizon_read_only_reasons"] = deepcopy(
                read_only_reasons
            )
            suppressed_reasons: dict[str, str | None] = {
                horizon: (
                    system_status
                    if system_status in {"SCANNING", "ERROR"}
                    and horizon in unavailable_horizons
                    else None
                )
                for horizon in ("SHORT", "LONG")
            }
            payload["horizon_suppressed_reasons"] = suppressed_reasons
            payload["safety"]["horizon_suppressed_reasons"] = deepcopy(
                suppressed_reasons
            )
            payload["scan_in_progress_horizons"] = (
                sorted(unavailable_horizons) if system_status == "SCANNING" else []
            )
            payload["scan_unavailable_horizons"] = (
                sorted(unavailable_horizons) if system_status == "ERROR" else []
            )
            # Availability is the hard boundary. Even a malformed legacy partial
            # report cannot leak an array from a horizon that has no completion
            # timestamp.
            if not horizon_freshness["SHORT"]["available"]:
                payload["signals"] = []
                payload["watchlist"] = []
                payload["market_map"] = []
                payload["market_regime_counts"] = {}
                payload["market_bias"] = {}
            if not horizon_freshness["LONG"]["available"]:
                payload["long_signals"] = []
                payload["long_watchlist"] = []
            for horizon, reason in read_only_reasons.items():
                if reason:
                    _project_horizon_read_only(payload, horizon, reason)
            for horizon, reason in suppressed_reasons.items():
                if reason:
                    _suppress_horizon_projection(payload, horizon)
            if not actionable:
                payload["historical_signal_count"] = len(payload.get("signals", []))
                payload["historical_long_signal_count"] = len(
                    payload.get("long_signals", [])
                )
                payload["safety"]["actionable"] = False
                if snapshot_expired:
                    # Keep the last completed snapshot visible for reference, but
                    # expose an explicit read-only state so no client can mistake it
                    # for a current entry opportunity.
                    payload["signals_suppressed_reason"] = None
                    payload["signals_read_only_reason"] = "STALE"
                elif system_status == "SCANNING":
                    # The explicitly requested horizon has no current result yet,
                    # so its previous cards stay hidden.  An untouched horizon may
                    # remain visible and actionable at its own completion time.
                    payload["signals_suppressed_reason"] = suppressed_reasons[
                        "SHORT"
                    ]
                    payload["signals_read_only_reason"] = None
                elif system_status == "ERROR" and unavailable_horizons:
                    # A failed attempt does not delete the completed snapshot, but
                    # the requested slot is withheld from this public projection.
                    payload["signals_suppressed_reason"] = suppressed_reasons[
                        "SHORT"
                    ]
                    payload["signals_read_only_reason"] = None
                else:
                    payload["signals"] = []
                    payload["watchlist"] = []
                    payload["market_map"] = []
                    payload["long_signals"] = []
                    payload["long_watchlist"] = []
                    payload["signals_suppressed_reason"] = system_status
                    payload["signals_read_only_reason"] = None
            else:
                payload["signals_suppressed_reason"] = None
                payload["signals_read_only_reason"] = None
            payload["long_signals_suppressed_reason"] = suppressed_reasons["LONG"]
            return payload

    def preview_dict(self) -> dict[str, Any] | None:
        with self._state_lock:
            if not self._running or self._preview is None:
                return None
            payload = public_report_payload(self._preview)
            horizon_freshness = {
                horizon: self._report_horizon_freshness(self._preview, horizon)
                for horizon in ("SHORT", "LONG")
            }
            # A FULL core preview is the current round's 15m core result.  The
            # current 4H pass has not completed yet and must not inherit a
            # timestamp merely because the report itself is marked FULL.
            if self._scan_mode == "FULL" and not self._preview.long_completed_at:
                horizon_freshness["LONG"] = {
                    "available": False,
                    "completed_at": None,
                    "age_seconds": None,
                    "expired": False,
                }
            payload["runtime_status"] = "CORE_PREVIEW"
            payload["actionable"] = False
            payload["preliminary"] = True
            payload["deep_data_pending"] = True
            payload["scan_request_mode"] = self._scan_mode
            payload["horizon_freshness"] = horizon_freshness
            payload["signals_suppressed_reason"] = None
            payload["safety"]["actionable"] = False
            requested = (
                {"SHORT", "LONG"}
                if self._scan_mode == "FULL"
                else {self._scan_mode}
            )
            payload["safety"]["horizon_actionable"] = {
                horizon: bool(
                    horizon not in requested
                    and item.get("available")
                    and not item.get("expired")
                )
                for horizon, item in horizon_freshness.items()
            }
            read_only_reasons = _read_only_reasons(
                "CORE_PREVIEW",
                horizon_freshness,
                requested,
            )
            payload["horizon_read_only_reasons"] = read_only_reasons
            payload["safety"]["horizon_read_only_reasons"] = deepcopy(
                read_only_reasons
            )
            suppressed_reasons = {
                horizon: (
                    "CORE_PREVIEW"
                    if horizon in requested and not item.get("available")
                    else None
                )
                for horizon, item in horizon_freshness.items()
            }
            payload["horizon_suppressed_reasons"] = suppressed_reasons
            payload["safety"]["horizon_suppressed_reasons"] = deepcopy(
                suppressed_reasons
            )
            for horizon, reason in read_only_reasons.items():
                if reason:
                    _project_horizon_read_only(payload, horizon, reason)
            for horizon, reason in suppressed_reasons.items():
                if reason:
                    _suppress_horizon_projection(payload, horizon)
            payload["signals_suppressed_reason"] = suppressed_reasons["SHORT"]
            payload["long_signals_suppressed_reason"] = suppressed_reasons["LONG"]
            payload["signals_read_only_reason"] = read_only_reasons["SHORT"]
            return payload

    def statistics(self) -> dict[str, Any]:
        repository = getattr(self.scanner, "repository", None)
        if repository is None:
            return {
                "available": False,
                "note": "Signal Repository 尚未啟用；禁止顯示假勝率。",
            }
        return repository.performance()

    def signal_history(self, limit: int = 60) -> dict[str, Any]:
        repository = getattr(self.scanner, "repository", None)
        if repository is None or not hasattr(repository, "recent_history"):
            return {
                "available": False,
                "items": [],
                "note": "Signal Repository 尚未啟用。",
            }
        short_items = repository.recent_history(
            limit,
            horizon="SHORT",
            max_age_hours=24,
        )
        long_items = repository.recent_history(
            limit,
            horizon="LONG",
            max_age_hours=24 * 7,
        )
        items = short_items + long_items
        return {
            "available": bool(items),
            "items": items,
            "short_items": short_items,
            "long_items": long_items,
            "retention": {
                "SHORT": {"hours": 24, "limit": min(max(1, int(limit)), 100)},
                "LONG": {"hours": 24 * 7, "limit": min(max(1, int(limit)), 100)},
            },
            "note": (
                "15m 保留 24 小時、4H 保留 7 天；各自最多 60 筆，只按原始觸發時間輪替。"
                if items
                else "尚無訊號生命週期紀錄。"
            ),
        }

    def scan_instrument_dict(
        self,
        inst_id: str,
        horizon: str = "BOTH",
        direction_lock: str | None = None,
    ) -> dict[str, Any]:
        """Run one deduplicated single-symbol transaction end to end."""

        normalized_id = _normalize_usdt_swap_id(inst_id)
        requested_horizon = str(horizon or "BOTH").strip().upper()
        requested_horizon = {
            "15M": "SHORT",
            "4H": "LONG",
            "FULL": "BOTH",
            "ALL": "BOTH",
        }.get(requested_horizon, requested_horizon)
        if requested_horizon not in {"SHORT", "LONG", "BOTH"}:
            raise PreflightError(
                HTTPStatus.BAD_REQUEST,
                "單幣掃描週期必須是 15m、4H 或 15m＋4H",
            )
        requested_direction_lock = (
            str(direction_lock or "").strip().upper() or None
        )
        if requested_direction_lock not in {None, "LONG", "SHORT"}:
            raise PreflightError(
                HTTPStatus.BAD_REQUEST,
                "卡片原方向必須是做多或做空",
            )
        if requested_direction_lock is not None and requested_horizon == "BOTH":
            raise PreflightError(
                HTTPStatus.BAD_REQUEST,
                "卡片方向鎖定必須指定 15m 或 4H",
            )
        request_key = (
            normalized_id,
            requested_horizon,
            requested_direction_lock or "UNLOCKED",
        )
        with self._state_lock:
            if self._running:
                raise PreflightError(
                    HTTPStatus.CONFLICT,
                    "全市場掃描正在執行，完成後才能掃描單一幣種",
                )
            if self._single_inflight:
                raise PreflightError(
                    HTTPStatus.CONFLICT,
                    "另一個單幣掃描正在執行，請等待本輪完成",
                )
            self._single_inflight.add(request_key)
        try:
            with self._scan_lock:
                return self._scan_instrument_dict_locked(
                    normalized_id,
                    requested_horizon,
                    requested_direction_lock,
                )
        finally:
            with self._state_lock:
                self._single_inflight.discard(request_key)

    def _scan_instrument_dict_locked(
        self,
        inst_id: str,
        horizon: str = "BOTH",
        direction_lock: str | None = None,
    ) -> dict[str, Any]:
        """Refresh one symbol for the explicitly requested radar horizon."""

        normalized_id = _normalize_usdt_swap_id(inst_id)
        requested_horizon = str(horizon or "BOTH").strip().upper()
        requested_horizon = {
            "15M": "SHORT",
            "4H": "LONG",
            "FULL": "BOTH",
            "ALL": "BOTH",
        }.get(requested_horizon, requested_horizon)
        if requested_horizon not in {"SHORT", "LONG", "BOTH"}:
            raise PreflightError(
                HTTPStatus.BAD_REQUEST,
                "單幣掃描週期必須是 15m、4H 或 15m＋4H",
            )
        requested_direction_lock = (
            str(direction_lock or "").strip().upper() or None
        )
        if requested_direction_lock not in {None, "LONG", "SHORT"}:
            raise PreflightError(
                HTTPStatus.BAD_REQUEST,
                "卡片原方向必須是做多或做空",
            )
        analyzer = getattr(self.scanner, "scan_instrument", None)
        if not callable(analyzer):
            raise PreflightError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "單幣掃描服務尚未啟用",
            )

        with self._scan_lock:
            with self._state_lock:
                if self._running:
                    raise PreflightError(
                        HTTPStatus.CONFLICT,
                        "全市場掃描正在執行，完成後才能掃描單一幣種",
                    )
                market_bias = deepcopy(
                    self._latest.market_bias if self._latest is not None else {}
                )
                long_market_bias = deepcopy(
                    self._latest.long_market_bias
                    if self._latest is not None
                    else {}
                )
                stored_signals: dict[str, Any | None] = {"SHORT": None, "LONG": None}
                horizon_timestamps: dict[str, str] = {"SHORT": "", "LONG": ""}
                if self._latest is not None:
                    stored_signals["SHORT"] = next(
                        (
                            item
                            for item in self._latest.signals
                            if item.inst_id == normalized_id
                        ),
                        None,
                    )
                    stored_signals["LONG"] = next(
                        (
                            item
                            for item in self._latest.long_signals
                            if item.inst_id == normalized_id
                        ),
                        None,
                    )
                    horizon_timestamps = {
                        horizon: self._previous_horizon_timestamp(
                            self._latest,
                            horizon,
                        )
                        for horizon in ("SHORT", "LONG")
                    }
                btc_bias = "NEUTRAL"
                long_btc_bias = "NEUTRAL"
                if self._latest is not None:
                    btc_state = next(
                        (
                            item
                            for item in self._latest.market_map
                            if item.inst_id == "BTC-USDT-SWAP"
                        ),
                        None,
                    )
                    if btc_state is not None:
                        btc_bias = btc_state.direction
                    long_btc_state = next(
                        (
                            item
                            for item in self._latest.long_market_map
                            if item.inst_id == "BTC-USDT-SWAP"
                        ),
                        None,
                    )
                    if long_btc_state is not None:
                        long_btc_bias = long_btc_state.direction
                btc_bias = str(
                    dict(market_bias.get("btc", {}) or {}).get("direction")
                    or btc_bias
                )
                long_btc_bias = str(
                    dict(long_market_bias.get("btc", {}) or {}).get("direction")
                    or long_btc_bias
                )
            repository = getattr(self.scanner, "repository", None)
            active_loader = getattr(repository, "load_active_signal", None)
            if callable(active_loader):
                for stored_horizon in ("SHORT", "LONG"):
                    if (
                        requested_horizon != "BOTH"
                        and requested_horizon != stored_horizon
                    ):
                        continue
                    active_signal = active_loader(normalized_id, stored_horizon)
                    if active_signal is not None:
                        stored_signals[stored_horizon] = active_signal
            analyzer_parameters = inspect.signature(analyzer).parameters
            analyzer_args: dict[str, Any] = {}
            if "market_bias" in analyzer_parameters:
                analyzer_args["market_bias"] = market_bias
            if "long_market_bias" in analyzer_parameters:
                analyzer_args["long_market_bias"] = long_market_bias
            if "btc_bias" in analyzer_parameters:
                analyzer_args["btc_bias"] = btc_bias
            if "long_btc_bias" in analyzer_parameters:
                analyzer_args["long_btc_bias"] = long_btc_bias
            if "requested_horizon" in analyzer_parameters:
                analyzer_args["requested_horizon"] = requested_horizon
            if "direction_lock" in analyzer_parameters:
                analyzer_args["direction_lock"] = requested_direction_lock

            analysis = None
            for attempt in range(2):
                try:
                    analysis = analyzer(normalized_id, **analyzer_args)
                    break
                except ValueError as exc:
                    raise PreflightError(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        str(exc),
                    ) from exc
                except Exception as exc:
                    if attempt == 0 and _single_scan_failure_is_retryable(exc):
                        delay = _single_scan_retry_delay(exc)
                        LOGGER.warning(
                            "Transient single-instrument scan failure for %s; "
                            "retrying once after %.2fs: %s",
                            normalized_id,
                            delay,
                            exc,
                        )
                        time.sleep(delay)
                        continue
                    LOGGER.exception(
                        "Single-instrument scan failed for %s",
                        normalized_id,
                    )
                    raise PreflightError(
                        HTTPStatus.BAD_GATEWAY,
                        _single_scan_failure_message(exc),
                    ) from exc
                finally:
                    self._release_scanner_transient_data()

            if analysis is None:  # pragma: no cover - loop exits by return or exception
                raise PreflightError(
                    HTTPStatus.BAD_GATEWAY,
                    "OKX 最新單幣資料暫時無法完成分析，請稍後再試。",
                )

        def horizon_payload(result: Any, horizon: str) -> dict[str, Any]:
            if requested_horizon != "BOTH" and requested_horizon != horizon:
                return {
                    "horizon": horizon,
                    "horizon_label": "4H 長線" if horizon == "LONG" else "15m 短線",
                    "kind": "NOT_REQUESTED",
                    "reason_code": "horizon_not_requested",
                    "message": "本次沒有執行這個週期，既有另一週期結果不受影響。",
                    "item": None,
                    "preflight": None,
                    "latest_confirmation": None,
                }
            stored_signal = stored_signals[horizon]
            confirmation = (
                _latest_confirmation(result, stored_signal.direction)
                if stored_signal is not None
                else None
            )
            preflight = None
            persisted_terminal_kind = None
            if stored_signal is not None:
                try:
                    preflight = build_preflight_payload(
                        stored_signal,
                        analysis.ticker,
                        analysis.context,
                        self.config,
                        report_generated_at=(
                            horizon_timestamps[horizon]
                            or analysis.analyzed_at
                        ),
                    )
                    preflight = _merge_preflight_confirmation(
                        preflight,
                        confirmation or {},
                    )
                    preflight["cached"] = False
                    preflight["cache_age_seconds"] = 0.0
                    preflight["cache_ttl_seconds"] = 0.0
                    terminal_kind = _preflight_terminal_kind(preflight)
                    if terminal_kind is not None:
                        cache_key = (
                            horizon_timestamps[horizon] or analysis.analyzed_at,
                            horizon,
                            normalized_id,
                        )
                        persisted_terminal_kind = self._persist_preflight_terminal(
                            repository=getattr(self.scanner, "repository", None),
                            signal=stored_signal,
                            payload=preflight,
                            observed_at=analysis.analyzed_at,
                            horizon=horizon,
                            cache_key=cache_key,
                        )
                except (TypeError, ValueError) as exc:
                    failure_message = f"無法安全核對舊交易計畫：{exc}"
                    confirmation = {
                        "status": "DATA_UNAVAILABLE",
                        "label": "舊計畫資料不足",
                        "message": failure_message,
                        "new_entry_allowed": False,
                    }
                    # Keep the response fail-closed even when malformed legacy
                    # plan values prevent a normal Preflight build.  Without a
                    # structured DATA_UNAVAILABLE verdict the fresh candidate
                    # (including an opposite-direction candidate) could leak
                    # through as ENTER in the single-coin page.
                    stored_lifecycle = dict(
                        getattr(stored_signal, "lifecycle", {}) or {}
                    )
                    lifecycle_status = str(
                        stored_lifecycle.get("status") or "ACTIVE"
                    ).upper()
                    lifecycle_terminal = lifecycle_status in {
                        "INVALIDATED",
                        "COMPLETED",
                        "TARGET_REACHED",
                    }
                    preflight = _merge_preflight_confirmation(
                        {
                            "inst_id": normalized_id,
                            "horizon": horizon,
                            "horizon_label": (
                                "4H 長線" if horizon == "LONG" else "15m 短線"
                            ),
                            "direction": str(
                                getattr(stored_signal, "direction", "NEUTRAL")
                            ),
                            "verdict": {
                                "status": "DATA_UNAVAILABLE",
                                "situation": "DATA_UNAVAILABLE",
                                "label": "舊計畫資料不足｜禁止新進場",
                                "reason": failure_message,
                                "actionable": False,
                                "hard_blockers": ["STORED_PLAN_DATA_UNAVAILABLE"],
                            },
                            "signal_lifecycle": {
                                "status": lifecycle_status,
                                "label": "已觸發・核對資料不足",
                                "triggered": True,
                                "active": not lifecycle_terminal,
                                "terminal": lifecycle_terminal,
                                "note": "舊 Episode 保留，但資料恢復前禁止新進場。",
                            },
                            "plan_state": {
                                "status": "ACTIVE_ENTRY_BLOCKED",
                                "old_plan_reusable": False,
                                "old_plan_reusable_for_new_entry": False,
                                "existing_position_plan_active": not lifecycle_terminal,
                                "new_entry_status": "WAIT",
                                "new_entry_allowed": False,
                                "new_trigger_required": False,
                            },
                            "original": {},
                            "live": {},
                            "execution": {},
                            "data_quality": {
                                "status": "UNAVAILABLE",
                                "missing_sources": ["stored_signal_plan"],
                            },
                        },
                        confirmation,
                    )
            if result is None:
                canonical_decision = _canonical_single_decision(
                    stored_signal,
                    preflight,
                    confirmation,
                )
                return {
                    "horizon": horizon,
                    "horizon_label": "4H 長線" if horizon == "LONG" else "15m 短線",
                    "kind": "UNAVAILABLE",
                    "reason_code": "data_unavailable",
                    "message": "這個週期的資料目前不足，沒有使用替代值硬算。",
                    "item": None,
                    "preflight": preflight,
                    "latest_confirmation": confirmation,
                    "decision_context": canonical_decision,
                }
            fresh_signal = result.signal
            fresh_state = result.market_state
            locked_opposite_reason = (
                str(getattr(result, "reason", ""))
                == "card_direction_locked_opposite"
            )
            candidate_source = (
                fresh_state if locked_opposite_reason else fresh_signal or fresh_state
            )
            candidate_direction = str(
                getattr(candidate_source, "direction", "") or "NEUTRAL"
            )
            opposite_warning = bool(
                requested_direction_lock is not None
                and candidate_direction in {"LONG", "SHORT"}
                and candidate_direction != requested_direction_lock
            )
            if (
                fresh_signal is not None
                and requested_direction_lock is not None
                and fresh_signal.direction != requested_direction_lock
            ):
                # Defense in depth for scanner adapters that do not implement
                # direction_lock themselves. Never expose an opposite Trigger
                # as the result of a card-scoped scan.
                fresh_signal = None
            fresh_item = fresh_signal or fresh_state
            if opposite_warning and stored_signal is None and fresh_item is not None:
                # With no active Episode to display (for example, a retained
                # TP/SL card), show a neutral reversal warning instead of an
                # opposite-direction pseudo-card.
                fresh_item = deepcopy(fresh_item)
                fresh_item.direction = "NEUTRAL"
                fresh_item.summary = (
                    f"原{('做多' if requested_direction_lock == 'LONG' else '做空')}方向"
                    f"目前轉弱，偵測到{('做多' if candidate_direction == 'LONG' else '做空')}候選；"
                    "本次卡片掃描只提示可能反轉，不建立反向卡。"
                )
            item = fresh_item
            is_signal_item = fresh_signal is not None
            closed_item_payload = None
            stored_trigger_id = str(
                getattr(stored_signal, "trigger_id", "") or ""
            )
            fresh_trigger_id = str(
                getattr(fresh_signal, "trigger_id", "") or ""
            )
            independent_new_episode = bool(
                stored_signal is not None
                and fresh_signal is not None
                and stored_trigger_id
                and fresh_trigger_id
                and stored_trigger_id != fresh_trigger_id
                and persisted_terminal_kind in {"COMPLETED", "INVALIDATED"}
            )
            if independent_new_episode:
                # The old Episode reached TP/SL on the same refresh that the
                # repository accepted a genuinely newer Trigger.  Keep the
                # terminal card as a separate payload and let the main item be
                # the new plan; attaching the old preflight to it would replace
                # its new Entry/SL/TP with the closed plan in the UI.
                terminal_signal = None
                terminal_loader = getattr(
                    getattr(self.scanner, "repository", None),
                    "load_terminal_signal",
                    None,
                )
                if callable(terminal_loader):
                    terminal_signal = terminal_loader(stored_signal)
                if terminal_signal is None:
                    with self._state_lock:
                        latest = self._latest
                        terminal_collection = (
                            list(latest.long_closed_signals)
                            if latest is not None and horizon == "LONG"
                            else list(latest.closed_signals)
                            if latest is not None
                            else []
                        )
                    terminal_signal = next(
                        (
                            candidate
                            for candidate in terminal_collection
                            if candidate.trigger_id == stored_trigger_id
                        ),
                        None,
                    )
                if terminal_signal is not None:
                    closed_item_payload = public_candidate_payload(
                        terminal_signal,
                        signal=True,
                    )
                preflight = None
            if stored_signal is not None and preflight is not None:
                # A live Signal Episode owns its direction and original
                # Entry/SL/TP until terminal invalidation/completion.  A fresh
                # opposite candidate is reference evidence only; never attach
                # the original plan's verdict to that candidate's direction
                # or prices in the response.
                stored_direction = str(
                    getattr(stored_signal, "direction", "") or "NEUTRAL"
                )
                fresh_direction = str(
                    getattr(fresh_item, "direction", "") or "NEUTRAL"
                )
                same_direction = (
                    fresh_item is not None
                    and fresh_direction == stored_direction
                )

                if same_direction and fresh_signal is not None:
                    # A same-direction Trigger is the newest reading of the
                    # existing Episode, not a second trade plan.  Keep its
                    # fresh evidence/summary while restoring the immutable
                    # Entry, SL, TP and Trigger identity from the Episode.
                    item = deepcopy(fresh_signal)
                    for field in (
                        "direction",
                        "strategy",
                        "entry_low",
                        "entry_high",
                        "stop_loss",
                        "take_profit_1",
                        "take_profit_2",
                        "risk_reward",
                        "invalidation",
                        "radar_horizon",
                        "trigger_id",
                        "trigger_type",
                        "signal_stage",
                        "freshness",
                        "generated_at",
                    ):
                        setattr(item, field, deepcopy(getattr(stored_signal, field)))
                    fresh_story = dict(getattr(item, "market_story", {}) or {})
                    stored_story = dict(
                        getattr(stored_signal, "market_story", {}) or {}
                    )
                    fresh_story["trigger"] = deepcopy(
                        stored_story.get("trigger", {})
                    )
                    item.market_story = fresh_story
                elif same_direction:
                    # No new formal Trigger was created, but the newest
                    # same-direction Market State still refreshes the Episode
                    # explanation and the latest advisory risk evidence.
                    item = deepcopy(stored_signal)
                    for field in (
                        "spread_pct",
                        "quote_volume_24h",
                        "closed_candle_ts",
                        "regime",
                        "readiness_score",
                        "factor_scores",
                        "evidence_groups",
                        "timeframe_states",
                        "supporting_evidence",
                        "conflicts",
                        "neutral_evidence",
                        "safety_checks",
                        "entry_quality",
                        "summary",
                        "direction_state",
                        "market_participation",
                        "execution_quality",
                        "data_quality",
                    ):
                        if hasattr(fresh_item, field):
                            setattr(item, field, deepcopy(getattr(fresh_item, field)))
                    fresh_story = dict(
                        getattr(fresh_item, "market_story", {}) or {}
                    )
                    stored_story = dict(
                        getattr(stored_signal, "market_story", {}) or {}
                    )
                    fresh_story["trigger"] = deepcopy(
                        stored_story.get("trigger", {})
                    )
                    item.market_story = fresh_story
                else:
                    # Opposite-direction output is not a replacement plan.
                    # Keep the active Episode intact and update only neutral
                    # live-market/execution observations below.
                    item = deepcopy(stored_signal)
                is_signal_item = True
                fresh_metrics = dict(
                    getattr(fresh_item, "market_metrics", {}) or {}
                )
                merged_metrics = dict(getattr(item, "market_metrics", {}) or {})
                if same_direction:
                    merged_metrics.update(fresh_metrics)
                else:
                    neutral_metric_keys = {
                        "last_price",
                        "price_change_5m_pct",
                        "price_change_15m_pct",
                        "price_change_1h_pct",
                        "price_change_4h_pct",
                        "price_change_24h_pct",
                        "quote_volume_24h",
                        "volume_ratio_5m",
                        "volume_ratio_15m",
                        "open_interest_usd",
                        "open_interest_change_pct",
                        "oi_flow_state",
                        "funding_rate_pct",
                        "order_book_imbalance_pct",
                        "taker_buy_pct",
                        "taker_buy_volume",
                        "taker_sell_volume",
                        "cvd",
                        "bid_depth_usd",
                        "ask_depth_usd",
                        "buy_slippage_pct",
                        "sell_slippage_pct",
                        "best_bid",
                        "best_ask",
                        "spread_pct",
                        "execution_quality_complete",
                        "context_sampled_at",
                    }
                    merged_metrics.update(
                        {
                            key: value
                            for key, value in fresh_metrics.items()
                            if key in neutral_metric_keys
                        }
                    )
                live = dict(preflight.get("live", {}) or {})
                live_price = live.get("price")
                merged_metrics["last_price"] = (
                    live_price if live_price is not None else analysis.ticker.last
                )
                item.market_metrics = merged_metrics
                if fresh_item is not None:
                    for field in ("spread_pct", "quote_volume_24h"):
                        value = getattr(fresh_item, field, None)
                        if value is not None:
                            setattr(item, field, value)
                item.data_timestamp = int(
                    getattr(fresh_item, "data_timestamp", 0)
                    or getattr(analysis.ticker, "ts", 0)
                    or getattr(item, "data_timestamp", 0)
                )
                fresh_closed_candle = getattr(fresh_item, "closed_candle_ts", 0)
                if fresh_closed_candle:
                    item.closed_candle_ts = int(fresh_closed_candle)

                verdict = dict(preflight.get("verdict", {}) or {})
                eligibility = dict(getattr(item, "entry_eligibility", {}) or {})
                eligibility.update(
                    {
                        "status": verdict.get("status", "DATA_UNAVAILABLE"),
                        "label": verdict.get("label", "進場資格待確認"),
                        "reason": verdict.get("reason", "等待最新完整資料"),
                        "actionable": verdict.get("actionable") is True,
                        "new_entry_allowed": verdict.get("actionable") is True,
                        "hard_blockers": list(
                            verdict.get("hard_blockers", []) or []
                        ),
                        "risk_warnings": list(
                            verdict.get("risk_warnings", []) or []
                        ),
                    }
                )
                for key in (
                    "chase_atr",
                    "adverse_atr",
                    "invalidation_progress_pct",
                    "remaining_rr",
                    "remaining_rr_applicable",
                ):
                    if key in live:
                        eligibility[key] = live[key]
                item.entry_eligibility = eligibility
                item.actionable = verdict.get("actionable") is True

                lifecycle = dict(preflight.get("signal_lifecycle", {}) or {})
                item.lifecycle = {
                    **dict(getattr(stored_signal, "lifecycle", {}) or {}),
                    **{
                        key: value
                        for key, value in lifecycle.items()
                        if value is not None
                    },
                }
                if "STORED_PLAN_DATA_UNAVAILABLE" in set(
                    verdict.get("hard_blockers", []) or []
                ):
                    for field in (
                        "entry_low",
                        "entry_high",
                        "stop_loss",
                        "take_profit_1",
                        "take_profit_2",
                    ):
                        setattr(item, field, None)
                    item.invalidation = "舊交易計畫資料不足，禁止沿用舊價位。"

            canonical_decision = _canonical_single_decision(
                item,
                preflight,
                confirmation,
            )
            if is_signal_item:
                message = str(
                    canonical_decision.get("final", {}).get("label")
                    or "已使用最新資料更新同一個 Signal Episode。"
                )
                kind = "SIGNAL"
            elif item is not None:
                message = str(
                    canonical_decision.get("final", {}).get("label")
                    or "目前尚未形成正式 Trigger；顯示最新方向與等待原因。"
                )
                kind = "STATE"
            else:
                message = "核心資料無法形成可判讀的 Market Story，沒有使用假資料。"
                kind = "UNAVAILABLE"
            return {
                "horizon": horizon,
                "horizon_label": "4H 長線" if horizon == "LONG" else "15m 短線",
                "kind": kind,
                "reason_code": result.reason,
                "message": message,
                "item": (
                    {
                        **public_candidate_payload(item, signal=is_signal_item),
                        "decision_context": canonical_decision,
                    }
                    if item is not None
                    else None
                ),
                "closed_item": closed_item_payload,
                "preflight": preflight,
                "latest_confirmation": confirmation,
                "decision_context": canonical_decision,
                "direction_lock": requested_direction_lock,
                "opposite_warning": (
                    {
                        "detected": True,
                        "candidate_direction": candidate_direction,
                        "message": (
                            f"原{('做多' if requested_direction_lock == 'LONG' else '做空')}方向轉弱，"
                            f"偵測到{('做多' if candidate_direction == 'LONG' else '做空')}候選；"
                            "卡片不翻向，真正反向新卡只由 15m／4H／全市場大掃描建立。"
                        ),
                    }
                    if opposite_warning
                    else None
                ),
            }

        return {
            "inst_id": analysis.inst_id,
            "analyzed_at": analysis.analyzed_at,
            "source": "ON_DEMAND_SINGLE_INSTRUMENT",
            "requested_horizon": requested_horizon,
            "direction_lock": requested_direction_lock,
            "current_price": analysis.ticker.last,
            "short": horizon_payload(analysis.short_result, "SHORT"),
            "long": horizon_payload(analysis.long_result, "LONG"),
            "warnings": list(analysis.errors),
            "safety": {
                "analysis_only": True,
                "auto_ordering": False,
                "full_market_scan": False,
                "persisted_signal_episode": True,
                "persisted_to_market_report": False,
                "persisted_to_report": False,
                "card_direction_locked": requested_direction_lock is not None,
                "note": (
                    "只掃描這一個幣；Signal Episode 會安全延續，但結果不加入"
                    "全市場排行，也不在伺服器記憶體保留完整單幣分析。"
                    "卡片方向鎖定時，反向候選只提示；真正反向卡只由大掃描建立。"
                ),
            },
        }

    def preflight_dict(self, inst_id: str, horizon: str) -> dict[str, Any]:
        """Run the canonical single-symbol refresh for one stored signal."""

        normalized_id = str(inst_id or "").strip().upper()
        normalized_horizon = _normalize_horizon(horizon)
        if not normalized_id.endswith("-USDT-SWAP") or normalized_horizon is None:
            raise PreflightError(HTTPStatus.BAD_REQUEST, "幣種或長短線參數不正確")

        # Production uses one canonical source for both the card's
        # "更新現狀" action and the dedicated coin scan page.  Lightweight
        # scanner fixtures without scan_instrument keep the compatibility path
        # below for deterministic unit tests and offline integrations.
        if callable(getattr(self.scanner, "scan_instrument", None)):
            direction_lock = None
            with self._state_lock:
                if self._latest is not None:
                    collection = (
                        self._latest.long_signals
                        if normalized_horizon == "LONG"
                        else self._latest.signals
                    )
                    stored = next(
                        (item for item in collection if item.inst_id == normalized_id),
                        None,
                    )
                    if stored is not None:
                        direction_lock = stored.direction
            repository = getattr(self.scanner, "repository", None)
            active_loader = getattr(repository, "load_active_signal", None)
            if callable(active_loader):
                stored = active_loader(normalized_id, normalized_horizon)
                if stored is not None:
                    direction_lock = stored.direction
            refreshed = self.scan_instrument_dict(
                normalized_id,
                normalized_horizon,
                direction_lock,
            )
            side = refreshed["long" if normalized_horizon == "LONG" else "short"]
            payload = side.get("preflight")
            if payload is None:
                raise PreflightError(
                    HTTPStatus.NOT_FOUND,
                    "目前沒有可核對的舊正式 Trigger；請以單幣最新分析結果為準",
                )
            return deepcopy(payload)

        with self._state_lock:
            system_status, _, _ = self._system_status_locked()
            if self._running:
                raise PreflightError(
                    HTTPStatus.CONFLICT,
                    "全市場掃描正在執行，完成後才能進行單幣進場檢查",
                )
            if self._latest is None:
                raise PreflightError(HTTPStatus.NOT_FOUND, "尚未完成第一輪市場掃描")
            if system_status != "FRESH":
                raise PreflightError(
                    HTTPStatus.CONFLICT,
                    "市場報告已過期或異常，請先重新掃描全市場",
                )
            horizon_freshness = self._horizon_freshness_locked(normalized_horizon)
            if not horizon_freshness["available"] or horizon_freshness["expired"]:
                label = "4H" if normalized_horizon == "LONG" else "15m"
                raise PreflightError(
                    HTTPStatus.CONFLICT,
                    f"{label} 資料已過期，請先執行對應週期掃描或全市場掃描",
                )
            report_generated_at = str(horizon_freshness["completed_at"])
            collection = (
                self._latest.long_signals
                if normalized_horizon == "LONG"
                else self._latest.signals
            )
            signal = next(
                (item for item in collection if item.inst_id == normalized_id),
                None,
            )
            if signal is None:
                raise PreflightError(
                    HTTPStatus.NOT_FOUND,
                    "最新報告中沒有這個週期的正式 Trigger；候選尚不能進行進場檢查",
                )
            repository = getattr(self.scanner, "repository", None)
            cache_key = (report_generated_at, normalized_horizon, normalized_id)
            cached = self._cached_preflight_locked(cache_key)
            if cached is not None:
                return cached

        client = getattr(self.scanner, "client", None)
        if client is None:
            raise PreflightError(HTTPStatus.SERVICE_UNAVAILABLE, "即時公開資料服務尚未啟用")

        with self._preflight_lock:
            with self._state_lock:
                cached = self._cached_preflight_locked(cache_key)
                if cached is not None:
                    return cached
            try:
                ticker = client.get_ticker(normalized_id)
                context = client.get_execution_context(normalized_id)
                payload = build_preflight_payload(
                    signal,
                    ticker,
                    context,
                    self.config,
                    report_generated_at=report_generated_at,
                )
            except PreflightError:
                raise
            except ValueError as exc:
                raise PreflightError(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc
            except Exception as exc:
                LOGGER.exception("Preflight market-data refresh failed for %s", normalized_id)
                raise PreflightError(
                    HTTPStatus.BAD_GATEWAY,
                    "OKX 即時公開資料暫時無法取得，請稍後再按一次",
                ) from exc

            with self._state_lock:
                if (
                    self._latest is None
                    or self._horizon_completed_at_locked(normalized_horizon)
                    != report_generated_at
                ):
                    raise PreflightError(
                        HTTPStatus.CONFLICT,
                        "市場報告已更新，請回到訊號頁重新選擇",
                    )
            terminal_kind = _preflight_terminal_kind(payload)
            persisted_terminal = self._persist_preflight_terminal(
                repository=repository,
                signal=signal,
                payload=payload,
                observed_at=(
                    str(payload.get("live", {}).get("sampled_at") or "")
                    or datetime.now(timezone.utc).isoformat()
                ),
                horizon=normalized_horizon,
                cache_key=cache_key,
            )
            with self._state_lock:
                if (
                    self._latest is None
                    or self._horizon_completed_at_locked(normalized_horizon)
                    != report_generated_at
                ):
                    raise PreflightError(
                        HTTPStatus.CONFLICT,
                        "市場報告已更新，請回到訊號頁重新選擇",
                    )
                cached_payload = deepcopy(payload)
                cached_payload["cached"] = False
                cached_payload["cache_age_seconds"] = 0.0
                cached_payload["cache_ttl_seconds"] = self._preflight_cache_ttl_seconds
                self._preflight_cache[cache_key] = (
                    time.monotonic(),
                    cached_payload,
                )
                if (
                    terminal_kind == "INVALIDATED"
                    and persisted_terminal is None
                    and repository is None
                ):
                    # Compatibility-only scanners have no durable repository;
                    # retain the legacy explicit reanalysis hand-off.
                    self._invalidated_preflight_signals[cache_key] = deepcopy(signal)
                return deepcopy(cached_payload)

    def reanalyze_preflight_dict(self, inst_id: str, horizon: str) -> dict[str, Any]:
        """Re-run one invalidated symbol through the V3.4 context pipeline."""

        normalized_id = str(inst_id or "").strip().upper()
        normalized_horizon = {
            "15M": "SHORT",
            "SHORT": "SHORT",
            "4H": "LONG",
            "LONG": "LONG",
        }.get(str(horizon or "").strip().upper())
        if not normalized_id.endswith("-USDT-SWAP") or normalized_horizon is None:
            raise PreflightError(HTTPStatus.BAD_REQUEST, "幣種或長短線參數不正確")

        analyzer = getattr(self.scanner, "reanalyze_instrument", None)
        committer = getattr(self.scanner, "commit_single_reanalysis", None)
        if not callable(analyzer) or not callable(committer):
            raise PreflightError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "單幣重新分析服務尚未啟用",
            )

        with self._scan_lock:
            with self._state_lock:
                system_status, _, _ = self._system_status_locked()
                if self._running:
                    raise PreflightError(
                        HTTPStatus.CONFLICT,
                        "全市場掃描正在執行，完成後才能重新分析這一個幣",
                    )
                if self._latest is None:
                    raise PreflightError(HTTPStatus.NOT_FOUND, "尚未完成第一輪市場掃描")
                if system_status != "FRESH":
                    raise PreflightError(
                        HTTPStatus.CONFLICT,
                        "市場報告已過期或異常，請先重新掃描全市場",
                    )
                horizon_freshness = self._horizon_freshness_locked(normalized_horizon)
                if not horizon_freshness["available"] or horizon_freshness["expired"]:
                    label = "4H" if normalized_horizon == "LONG" else "15m"
                    raise PreflightError(
                        HTTPStatus.CONFLICT,
                        f"{label} 資料已過期，請先執行對應週期掃描或全市場掃描",
                    )
                report_generated_at = str(horizon_freshness["completed_at"])
                cache_key = (
                    report_generated_at,
                    normalized_horizon,
                    normalized_id,
                )
                previous_signal = self._invalidated_preflight_signals.get(cache_key)
                if previous_signal is None:
                    raise PreflightError(
                        HTTPStatus.CONFLICT,
                        "必須先由進場檢查確認原交易計畫失效，才能重新分析這一個幣",
                    )
                market_bias = deepcopy(self._latest.market_bias)

            try:
                analysis = analyzer(previous_signal, market_bias)
                new_signal = committer(analysis)
            except PreflightError:
                raise
            except ValueError as exc:
                raise PreflightError(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc
            except Exception as exc:
                LOGGER.exception(
                    "Single-instrument reanalysis failed for %s",
                    normalized_id,
                )
                raise PreflightError(
                    HTTPStatus.BAD_GATEWAY,
                    _single_scan_failure_message(exc),
                ) from exc
            finally:
                self._release_scanner_transient_data()

            with self._state_lock:
                if (
                    self._running
                    or self._latest is None
                    or self._horizon_completed_at_locked(normalized_horizon)
                    != report_generated_at
                ):
                    raise PreflightError(
                        HTTPStatus.CONFLICT,
                        "市場報告已更新，請回到訊號頁重新選擇",
                    )
                current_report = self._latest
                collection = (
                    current_report.long_signals
                    if normalized_horizon == "LONG"
                    else current_report.signals
                )
                updated_collection = [
                    item
                    for item in collection
                    if item.inst_id != normalized_id
                ]
                if new_signal is not None:
                    updated_collection.append(new_signal)
                    sorter = getattr(self.scanner, "_signal_sort_key", None)
                    if callable(sorter):
                        updated_collection.sort(key=sorter, reverse=True)
                    updated_collection = updated_collection[: self.config.max_signals]
                repository = getattr(self.scanner, "repository", None)
                historical = (
                    repository.performance()
                    if repository is not None and hasattr(repository, "performance")
                    else current_report.historical_performance
                )
                updated_report = replace(
                    current_report,
                    signals=(
                        updated_collection
                        if normalized_horizon == "SHORT"
                        else current_report.signals
                    ),
                    long_signals=(
                        updated_collection
                        if normalized_horizon == "LONG"
                        else current_report.long_signals
                    ),
                    historical_performance=historical,
                )
                self._latest = updated_report
                self._preflight_cache.pop(cache_key, None)
                if new_signal is not None:
                    self._invalidated_preflight_signals.pop(cache_key, None)
                    self._terminal_preflight_outcomes.pop(cache_key, None)

            save_report(updated_report, self.config.data_dir)

            if new_signal is None:
                return {
                    "inst_id": normalized_id,
                    "horizon": normalized_horizon,
                    "horizon_label": (
                        "4H 長線" if normalized_horizon == "LONG" else "15m 短線"
                    ),
                    "reanalysis": {
                        "performed": True,
                        "status": "NO_NEW_ENTRY_OPPORTUNITY",
                        "message": (
                            "已用最新多週期資料重新分析這一個幣，"
                            "目前沒有新的正式 Trigger。"
                        ),
                        "old_plan_closed": True,
                        "reason_code": analysis.reason,
                        "analyzed_at": analysis.analyzed_at,
                    },
                    "safety": {
                        "analysis_only": True,
                        "auto_ordering": False,
                        "old_plan_reused": False,
                        "core_strategy_unchanged": True,
                    },
                }

            payload = build_preflight_payload(
                new_signal,
                analysis.ticker,
                analysis.context,
                self.config,
                report_generated_at=analysis.analyzed_at,
            )
            payload["reanalysis"] = {
                "performed": True,
                "status": "NEW_ENTRY_OPPORTUNITY",
                "message": "已產生全新的正式 Trigger 與交易計畫。",
                "old_plan_closed": True,
                "old_trigger_id": previous_signal.trigger_id,
                "new_trigger_id": new_signal.trigger_id,
                "analyzed_at": analysis.analyzed_at,
            }
            payload["cached"] = False
            payload["cache_age_seconds"] = 0.0
            payload["cache_ttl_seconds"] = self._preflight_cache_ttl_seconds
            return payload

    def latest_markdown(self) -> str | None:
        with self._state_lock:
            if self._latest is None:
                return None
            system_status, _, _ = self._system_status_locked()
            if system_status == "STALE":
                markdown = report_markdown(self._latest)
                heading = "# OKX USDT 永續雷達\n\n"
                warning = (
                    "> ⚠️ 資料已過期：這是超過 30 分鐘的保留快照，"
                    "禁止依此進場。請重新掃描全市場或只更新單一幣種。\n\n"
                )
                if markdown.startswith(heading):
                    return heading + warning + markdown[len(heading) :]
                return warning + markdown
            if system_status != "FRESH":
                labels = {
                    "SCANNING": "掃描中，舊正式訊號已暫停使用。",
                    "ERROR": "最新掃描失敗，舊正式訊號已暫停使用。",
                    "BOOTING": "雷達啟動中，尚無可用訊號。",
                }
                return "# OKX USDT 永續雷達\n\n" + labels.get(
                    system_status,
                    "目前沒有可使用的正式訊號。",
                )
            return report_markdown(self._latest)

    def _begin_scan_locked(self, scan_mode: str = "FULL") -> None:
        self._scan_mode = _normalize_scan_mode(scan_mode)
        self._running = True
        self._last_attempt_status = "SCANNING"
        self._scan_id = str(uuid.uuid4())
        self._scan_started_at = datetime.now(timezone.utc).isoformat()
        self._preview = None
        self._preflight_cache.clear()
        self._invalidated_preflight_signals.clear()
        self._terminal_preflight_outcomes.clear()
        self._scan_push_subscriptions.clear()
        self._progress = {
            "phase": "STARTING",
            "completed": 0,
            "total": None,
            "percent": None,
            "message": f"正在啟動{_SCAN_MODE_LABELS[self._scan_mode]}",
        }
        self._persist_runtime_state_locked()

    def _scan_worker(self) -> None:
        report: RadarReport | None = None
        error: Exception | None = None
        try:
            report = self._perform_scan()
        except Exception as exc:
            error = exc
        finally:
            with self._state_lock:
                self._running = False
                scan_id = self._scan_id or "unknown"
                subscriptions = list(self._scan_push_subscriptions.values())
                self._scan_push_subscriptions.clear()
        if subscriptions:
            self._deliver_scan_notifications(subscriptions, scan_id, report, error)

    def _perform_scan(self) -> RadarReport:
        with self._scan_lock:
            with self._state_lock:
                scan_id = self._scan_id or str(uuid.uuid4())
                started_at = self._scan_started_at or datetime.now(timezone.utc).isoformat()
                scan_mode = self._scan_mode
                previous_report = self._latest
            LOGGER.info(
                "Starting on-demand OKX USDT perpetual scan id=%s mode=%s",
                scan_id,
                scan_mode,
            )
            try:
                scan_kwargs: dict[str, Any] = {
                    "progress": self._update_progress,
                    "scan_id": scan_id,
                }
                parameters = inspect.signature(self.scanner.scan_once).parameters
                if "preview" in parameters:
                    scan_kwargs["preview"] = self._publish_preview
                if "scan_mode" in parameters:
                    scan_kwargs["scan_mode"] = scan_mode
                try:
                    report = self.scanner.scan_once(**scan_kwargs)
                finally:
                    self._release_scanner_transient_data()
                completed_at = datetime.now(timezone.utc).isoformat()
                if report.status != "DATA_INCOMPLETE":
                    report = self._merge_partial_report(
                        report,
                        previous_report,
                        scan_mode,
                        completed_at,
                    )
                report = replace(
                    report,
                    scan_id=scan_id,
                    scan_started_at=started_at,
                    generated_at=completed_at,
                    completed_at=completed_at,
                    runtime_status=(
                        "ERROR" if report.status == "DATA_INCOMPLETE" else "FRESH"
                    ),
                    actionable=report.status != "DATA_INCOMPLETE",
                    signals=([] if report.status == "DATA_INCOMPLETE" else report.signals),
                    long_signals=(
                        []
                        if report.status == "DATA_INCOMPLETE"
                        else report.long_signals
                    ),
                    max_signals=self.config.max_signals,
                    scan_mode=scan_mode,
                )
                if report.status == "DATA_INCOMPLETE" and previous_report is not None:
                    # A failed refresh is an attempt state, not a new market
                    # snapshot. Keep the last completed horizon slots on disk and
                    # in memory; the requested slot is disabled by latest_dict(),
                    # while an untouched slot remains available at its own age.
                    with self._state_lock:
                        self._preview = None
                        self._last_error = report.message
                        self._last_attempt_status = "ERROR"
                        self._progress = {
                            "phase": "COMPLETED",
                            "completed": 1,
                            "total": 1,
                            "percent": 100.0,
                            "message": f"{_SCAN_MODE_LABELS[scan_mode]}失敗",
                        }
                        self._persist_runtime_state_locked()
                    LOGGER.info(
                        "Scan attempt failed without replacing completed snapshot: "
                        "id=%s mode=%s status=%s",
                        scan_id,
                        scan_mode,
                        report.status,
                    )
                    return report
                save_report(report, self.config.data_dir)
                with self._state_lock:
                    self._latest = report
                    self._preview = None
                    if report.status == "DATA_INCOMPLETE":
                        self._last_error = report.message
                        self._last_attempt_status = "ERROR"
                    else:
                        self._last_error = None
                        self._last_attempt_status = "SUCCESS"
                    self._progress = {
                        "phase": "COMPLETED",
                        "completed": 1,
                        "total": 1,
                        "percent": 100.0,
                        "message": (
                            f"{_SCAN_MODE_LABELS[scan_mode]}完成"
                            if report.status != "DATA_INCOMPLETE"
                            else f"{_SCAN_MODE_LABELS[scan_mode]}失敗"
                        ),
                    }
                    self._persist_runtime_state_locked()
                LOGGER.info(
                    "Scan finished: id=%s mode=%s status=%s coverage=%.2f signals=%d",
                    scan_id,
                    scan_mode,
                    report.status,
                    report.coverage_pct,
                    len(report.signals),
                )
                return report
            except Exception as exc:
                LOGGER.exception("Unexpected scanner failure")
                with self._state_lock:
                    self._preview = None
                    self._last_error = str(exc)
                    self._last_attempt_status = "ERROR"
                    self._progress = {
                        "phase": "ERROR",
                        "completed": None,
                        "total": None,
                        "percent": None,
                        "message": "最新掃描失敗",
                    }
                    self._persist_runtime_state_locked()
                raise

    @staticmethod
    def _previous_horizon_timestamp(
        report: RadarReport,
        horizon: str,
    ) -> str:
        field_name = "long_completed_at" if horizon == "LONG" else "short_completed_at"
        timestamp = str(getattr(report, field_name, "") or "").strip()
        if timestamp:
            return timestamp
        mode = str(getattr(report, "scan_mode", "FULL") or "FULL").upper()
        if mode == "FULL" or mode == horizon:
            return report.completed_at or report.generated_at
        return ""

    def _merge_partial_report(
        self,
        report: RadarReport,
        previous: RadarReport | None,
        scan_mode: str,
        completed_at: str,
    ) -> RadarReport:
        """Keep the unrequested radar intact after a successful partial scan."""

        if scan_mode == "FULL":
            return replace(
                report,
                short_completed_at=completed_at,
                long_completed_at=completed_at,
            )
        quality = deepcopy(report.data_quality)
        if scan_mode == "SHORT":
            previous_long_completed_at = (
                self._previous_horizon_timestamp(previous, "LONG")
                if previous is not None
                else ""
            )
            if not previous_long_completed_at:
                return replace(
                    report,
                    long_signals=[],
                    long_closed_signals=[],
                    long_watchlist=[],
                    long_market_map=[],
                    long_market_bias={},
                    short_completed_at=completed_at,
                    long_completed_at="",
                )
            previous_quality = previous.data_quality or {}
            for key, value in previous_quality.items():
                if key.startswith("long_"):
                    quality[key] = deepcopy(value)
            return replace(
                report,
                long_signals=previous.long_signals,
                long_closed_signals=previous.long_closed_signals,
                long_watchlist=previous.long_watchlist,
                long_market_map=previous.long_market_map,
                long_market_bias=previous.long_market_bias,
                long_completed_at=previous_long_completed_at,
                short_completed_at=completed_at,
                data_quality=quality,
                message=f"{report.message} 4H 沿用上一輪資料。",
            )
        previous_short_completed_at = (
            self._previous_horizon_timestamp(previous, "SHORT")
            if previous is not None
            else ""
        )
        if not previous_short_completed_at:
            return replace(
                report,
                signals=[],
                closed_signals=[],
                watchlist=[],
                market_map=[],
                market_regime_counts={},
                market_bias={},
                short_completed_at="",
                long_completed_at=completed_at,
            )
        previous_quality = previous.data_quality or {}
        for key, value in previous_quality.items():
            if key.startswith("core_"):
                quality[key] = deepcopy(value)
        return replace(
            report,
            signals=previous.signals,
            closed_signals=previous.closed_signals,
            watchlist=previous.watchlist,
            market_map=previous.market_map,
            market_regime_counts=previous.market_regime_counts,
            market_bias=previous.market_bias,
            short_completed_at=previous_short_completed_at,
            long_completed_at=completed_at,
            data_quality=quality,
            message=f"{report.message} 15m 沿用上一輪資料。",
        )

    def _release_scanner_transient_data(self) -> None:
        release = getattr(self.scanner, "release_transient_data", None)
        if not callable(release):
            return
        try:
            released = release()
            LOGGER.info("Released %s cached candle series", released)
        except Exception:
            LOGGER.exception("Unable to release transient scanner data")

    def _register_scan_push_locked(self, subscription: dict[str, Any]) -> None:
        key = self.push_notifier.subscription_key(subscription)
        if not key or key in self._scan_push_subscriptions:
            return
        if len(self._scan_push_subscriptions) >= self._max_scan_push_subscriptions:
            raise PushSubscriptionError("本輪掃描通知裝置已達安全上限")
        self._scan_push_subscriptions[key] = subscription

    def _deliver_scan_notifications(
        self,
        subscriptions: list[dict[str, Any]],
        scan_id: str,
        report: RadarReport | None,
        error: Exception | None,
    ) -> None:
        success = (
            error is None
            and report is not None
            and report.status != "DATA_INCOMPLETE"
        )
        payload = {
            "title": "OKX 雷達掃描完成" if success else "OKX 雷達掃描未完成",
            "body": (
                "最新市場報告已完成，點擊查看結果。"
                if success
                else "本輪掃描未能完成，點擊查看目前狀態。"
            ),
            "url": "/",
            "tag": f"okx-radar-scan-{scan_id}",
            "status": "SUCCESS" if success else "ERROR",
            "scan_id": scan_id,
        }
        for subscription in subscriptions:
            try:
                self.push_notifier.send(subscription, payload)
            except Exception as exc:
                # Browser push endpoints are capability URLs. Never log them.
                LOGGER.warning(
                    "Unable to deliver one scan completion notification error=%s",
                    type(exc).__name__,
                )

    def _publish_preview(self, report: RadarReport) -> None:
        with self._state_lock:
            if not self._running or report.scan_id != self._scan_id:
                return
            if self._scan_mode == "SHORT":
                report = self._merge_partial_report(
                    report,
                    self._latest,
                    "SHORT",
                    report.short_completed_at or report.generated_at,
                )
            elif self._scan_mode == "FULL" and not report.long_completed_at:
                # The scanner publishes 15m core before the full 4H pass.  Never
                # inject the previous round's 4H cards into this round's preview.
                report = replace(
                    report,
                    long_signals=[],
                    long_watchlist=[],
                    long_market_map=[],
                    long_market_bias={},
                    long_completed_at="",
                )
            self._preview = report
            self._progress = {
                "phase": "CORE_PREVIEW",
                "completed": 1,
                "total": 1,
                "percent": 100.0,
                "message": (
                    "15m 核心結果已發布；既有 4H 結果保持不變，正在補深度資料"
                    if self._scan_mode == "SHORT"
                    else "15m 核心結果已發布，正在補深度資料與 4H 長線雷達"
                ),
            }

    def _update_progress(
        self,
        phase: str,
        completed: int | None,
        total: int | None,
        message: str,
    ) -> None:
        percent = (
            round(completed / total * 100.0, 1)
            if completed is not None and total not in (None, 0)
            else None
        )
        with self._state_lock:
            self._progress = {
                "phase": phase,
                "completed": completed,
                "total": total,
                "percent": percent,
                "message": message,
            }

    def _system_status_locked(self) -> tuple[str, float | None, bool]:
        age_seconds = self._report_age_seconds_locked()
        stale = age_seconds is not None and age_seconds > self.config.stale_after_seconds
        if self._running:
            return "SCANNING", age_seconds, stale
        # If a new attempt failed while the retained report is already old, STALE is
        # the primary safety state. ``last_error`` still explains the failed attempt.
        if self._latest is not None and stale:
            return "STALE", age_seconds, True
        if self._last_attempt_status == "ERROR" or self._last_error:
            return "ERROR", age_seconds, stale
        if self._latest is None:
            return "BOOTING", None, False
        if self._latest.status == "DATA_INCOMPLETE":
            return "ERROR", age_seconds, stale
        return "FRESH", age_seconds, False

    def _report_age_seconds_locked(self) -> float | None:
        if self._latest is None:
            return None
        try:
            completed = datetime.fromisoformat(
                self._latest.completed_at or self._latest.generated_at
            )
            if completed.tzinfo is None:
                completed = completed.replace(tzinfo=timezone.utc)
            return max(0.0, time.time() - completed.timestamp())
        except (TypeError, ValueError):
            return float("inf")

    def _horizon_completed_at_locked(self, horizon: str) -> str | None:
        if self._latest is None:
            return None
        return self._previous_horizon_timestamp(self._latest, horizon) or None

    def _horizon_freshness_locked(self, horizon: str) -> dict[str, Any]:
        if self._latest is None:
            return {
                "available": False,
                "completed_at": None,
                "age_seconds": None,
                "expired": False,
            }
        return self._report_horizon_freshness(self._latest, horizon)

    def _report_horizon_freshness(
        self,
        report: RadarReport,
        horizon: str,
    ) -> dict[str, Any]:
        completed_at = self._previous_horizon_timestamp(report, horizon)
        if not completed_at:
            return {
                "available": False,
                "completed_at": None,
                "age_seconds": None,
                "expired": False,
            }
        try:
            completed = datetime.fromisoformat(completed_at)
            if completed.tzinfo is None:
                completed = completed.replace(tzinfo=timezone.utc)
            age_seconds = max(0.0, time.time() - completed.timestamp())
        except (TypeError, ValueError):
            age_seconds = float("inf")
        return {
            "available": True,
            "completed_at": completed_at,
            "age_seconds": age_seconds,
            "expired": age_seconds > self.config.stale_after_seconds,
        }

    def _cached_preflight_locked(
        self,
        key: tuple[str, str, str],
    ) -> dict[str, Any] | None:
        cached = self._preflight_cache.get(key)
        if cached is None:
            return None
        created_at, payload = cached
        age = max(0.0, time.monotonic() - created_at)
        if age >= self._preflight_cache_ttl_seconds:
            self._preflight_cache.pop(key, None)
            return None
        result = deepcopy(payload)
        result["cached"] = True
        result["cache_age_seconds"] = round(age, 3)
        return result

    def _persist_preflight_terminal(
        self,
        *,
        repository: Any,
        signal: Any,
        payload: dict[str, Any],
        observed_at: str,
        horizon: str,
        cache_key: tuple[str, str, str],
    ) -> str | None:
        """Persist and project one exact terminal preflight outcome.

        Repository compare-and-set owns the durable lifecycle boundary.  The
        in-memory/latest.json removal happens only after that exact episode was
        closed (or was observed closed by the same in-flight transaction).
        """

        terminal_kind = _preflight_terminal_kind(payload)
        if terminal_kind is None or repository is None:
            return None
        persisted_kind = None
        if terminal_kind in {"COMPLETED", "INVALIDATED"}:
            closer_name = (
                "complete_preflight_plan"
                if terminal_kind == "COMPLETED"
                else "invalidate_preflight_plan"
            )
            closer = getattr(repository, closer_name, None)
            if callable(closer) and bool(closer(signal, observed_at)):
                persisted_kind = terminal_kind
        if persisted_kind is None:
            # The scanner may have advanced the same episode while the live
            # preflight was running.  Absence of an ACTIVE row is not enough:
            # query the exact CLOSED outcome so SL and TP races cannot be
            # mislabeled as whichever live price happened to be observed last.
            terminal_loader = getattr(repository, "preflight_terminal_kind", None)
            if callable(terminal_loader):
                observed_kind = str(terminal_loader(signal) or "").upper()
                if observed_kind in {
                    "COMPLETED",
                    "INVALIDATED",
                    "CLOSED_UNKNOWN",
                }:
                    persisted_kind = observed_kind
        if persisted_kind is None:
            return None

        _project_persisted_preflight_terminal(
            payload,
            persisted_kind,
            observed_at=observed_at,
            horizon=horizon,
        )
        exact_terminal_loader = getattr(repository, "load_terminal_signal", None)
        terminal_signal = (
            exact_terminal_loader(signal)
            if callable(exact_terminal_loader)
            else None
        )
        if terminal_signal is None:
            stage = (
                "COMPLETED"
                if persisted_kind == "COMPLETED"
                else "INVALIDATED"
                if persisted_kind == "INVALIDATED"
                else "CLOSED_UNKNOWN"
            )
            verdict = dict(payload.get("verdict", {}) or {})
            lifecycle = {
                **dict(getattr(signal, "lifecycle", {}) or {}),
                **dict(payload.get("signal_lifecycle", {}) or {}),
            }
            eligibility = dict(getattr(signal, "entry_eligibility", {}) or {})
            eligibility.update(
                {
                    "status": stage,
                    "label": verdict.get("label"),
                    "reason": verdict.get("reason"),
                    "actionable": False,
                    "new_entry_allowed": False,
                }
            )
            terminal_signal = replace(
                signal,
                signal_stage=stage,
                freshness=(
                    "COMPLETED" if stage == "COMPLETED" else "INVALIDATED"
                ),
                lifecycle=lifecycle,
                entry_eligibility=eligibility,
                actionable=False,
            )

        with self._state_lock:
            self._terminal_preflight_outcomes[cache_key] = persisted_kind
            if persisted_kind == "INVALIDATED":
                self._invalidated_preflight_signals[cache_key] = deepcopy(signal)
            else:
                self._invalidated_preflight_signals.pop(cache_key, None)
            current_report = self._latest
            if current_report is None:
                return persisted_kind
            field_name = "long_signals" if horizon == "LONG" else "signals"
            closed_field_name = (
                "long_closed_signals" if horizon == "LONG" else "closed_signals"
            )
            current_items = list(getattr(current_report, field_name))
            retained_items = [
                item
                for item in current_items
                if item.trigger_id != signal.trigger_id
            ]
            closed_items = [
                item
                for item in list(getattr(current_report, closed_field_name))
                if item.trigger_id != signal.trigger_id
            ]
            if persisted_kind in {"COMPLETED", "INVALIDATED"}:
                closed_items.append(terminal_signal)
                closed_items.sort(
                    key=lambda item: str(
                        getattr(item, "lifecycle", {}).get("closed_at") or ""
                    ),
                    reverse=True,
                )
                closed_items = closed_items[:100]
            updated_report = replace(
                current_report,
                **{
                    field_name: retained_items,
                    closed_field_name: closed_items,
                },
            )
            # SQLite has already committed the terminal CAS, so the public
            # in-memory view must move this card out of ACTIVE even if the
            # report-file write fails. Startup rebuilds the bounded terminal
            # collections from SQLite before publishing a restored latest.json.
            self._latest = updated_report
            try:
                save_report(updated_report, self.config.data_dir)
            except Exception:
                LOGGER.exception(
                    "Failed to persist terminal-card transition for %s %s",
                    signal.inst_id,
                    horizon,
                )
        return persisted_kind

    @staticmethod
    def _idle_progress() -> dict[str, Any]:
        return {
            "phase": "IDLE",
            "completed": None,
            "total": None,
            "percent": None,
            "message": "等待排程或使用者要求最新市場掃描",
        }

    def _persist_runtime_state_locked(self) -> None:
        try:
            save_runtime_state(
                self.config.data_dir,
                {
                    "last_attempt_status": self._last_attempt_status,
                    "last_error": self._last_error,
                    "scan_mode": self._scan_mode,
                    "scan_id": self._scan_id,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            LOGGER.exception("Unable to persist scan attempt state")


def _normalize_usdt_swap_id(value: str) -> str:
    raw = str(value or "").strip().upper().replace(" ", "")
    if raw.endswith("-USDT-SWAP"):
        base = raw[: -len("-USDT-SWAP")]
    else:
        base = raw
        for suffix in ("/USDT", "-USDT", "USDT", "-SWAP"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
    if not re.fullmatch(r"[A-Z0-9]{1,24}", base):
        raise PreflightError(
            HTTPStatus.BAD_REQUEST,
            "請輸入正確幣種，例如 BTC 或 BTC-USDT-SWAP",
        )
    return f"{base}-USDT-SWAP"


def serve(runtime: RadarRuntime, host: str, port: int) -> None:
    static_dir = Path(__file__).parent / "static"
    dashboard_path = static_dir / "pages.html"
    dashboard = dashboard_path.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "OKXRadar/3.4"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/":
                self._send_bytes(HTTPStatus.OK, dashboard, "text/html; charset=utf-8")
            elif path == "/manifest.webmanifest":
                self._send_bytes(
                    HTTPStatus.OK,
                    (static_dir / "manifest.webmanifest").read_bytes(),
                    "application/manifest+json; charset=utf-8",
                )
            elif path == "/service-worker.js":
                self._send_bytes(
                    HTTPStatus.OK,
                    (static_dir / "service-worker.js").read_bytes(),
                    "application/javascript; charset=utf-8",
                )
            elif path == "/radar-icon.svg":
                self._send_bytes(
                    HTTPStatus.OK,
                    (static_dir / "radar-icon.svg").read_bytes(),
                    "image/svg+xml; charset=utf-8",
                )
            elif path == "/health":
                self._send_json(HTTPStatus.OK, {"ok": True, **runtime.status()})
            elif path == "/api/status":
                self._send_json(HTTPStatus.OK, runtime.status())
            elif path == "/api/push/config":
                self._send_json(HTTPStatus.OK, runtime.push_config())
            elif path == "/api/report/latest":
                payload = runtime.latest_dict()
                if payload is None:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {
                            "error": "尚未完成第一輪掃描",
                            "runtime_status": runtime.status()["system_status"],
                        },
                    )
                else:
                    self._send_json(HTTPStatus.OK, payload)
            elif path == "/api/report/preview":
                payload = runtime.preview_dict()
                if payload is None:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "本輪 15m 核心結果尚未完成"},
                    )
                else:
                    self._send_json(HTTPStatus.OK, payload)
            elif path == "/api/report/latest.md":
                markdown = runtime.latest_markdown()
                if markdown is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "尚未完成第一輪掃描"})
                else:
                    self._send_bytes(
                        HTTPStatus.OK,
                        markdown.encode("utf-8"),
                        "text/markdown; charset=utf-8",
                    )
            elif path == "/api/stats":
                self._send_json(HTTPStatus.OK, runtime.statistics())
            elif path == "/api/history":
                query = parse_qs(parsed.query)
                try:
                    limit = int(query.get("limit", ["60"])[0])
                except (TypeError, ValueError):
                    limit = 60
                self._send_json(HTTPStatus.OK, runtime.signal_history(limit))
            elif path == "/api/preflight":
                query = parse_qs(parsed.query)
                try:
                    payload = runtime.preflight_dict(
                        query.get("inst_id", [""])[0],
                        query.get("horizon", [""])[0],
                    )
                except PreflightError as exc:
                    self._send_json(exc.status, {"error": str(exc)})
                else:
                    self._send_json(HTTPStatus.OK, payload)
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/instrument/scan":
                try:
                    payload = self._read_json_body()
                    result = runtime.scan_instrument_dict(
                        payload.get("inst_id", ""),
                        payload.get("horizon", "BOTH"),
                        payload.get("direction_lock"),
                    )
                except PreflightError as exc:
                    self._send_json(exc.status, {"error": str(exc)})
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                else:
                    self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/preflight/reanalyze":
                try:
                    payload = self._read_json_body()
                    # Backward-compatible alias for older installed PWA shells.
                    # It must use the same canonical scan as every current
                    # single-symbol refresh and must not mutate the report.
                    result = runtime.preflight_dict(
                        payload.get("inst_id", ""),
                        payload.get("horizon", ""),
                    )
                except PreflightError as exc:
                    self._send_json(exc.status, {"error": str(exc)})
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                else:
                    self._send_json(HTTPStatus.OK, result)
                return
            if path != "/api/scan":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                payload = self._read_json_body()
                push_subscription = payload.get("push_subscription")
                scan_mode = _normalize_scan_mode(payload.get("scan_mode", "FULL"))
                started = runtime.trigger_scan(
                    push_subscription,
                    scan_mode=scan_mode,
                )
            except PreflightError as exc:
                self._send_json(exc.status, {"error": str(exc)})
                return
            except PushSubscriptionError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            status = runtime.status()
            self._send_json(
                HTTPStatus.ACCEPTED,
                {
                    "accepted": started,
                    "joined_existing_scan": not started,
                    "scan_id": status["scan_id"],
                    "scan_mode": status["scan_mode"],
                    "scan_mode_label": status["scan_mode_label"],
                    "runtime_status": "SCANNING",
                    "notification_registered": push_subscription is not None,
                    "message": (
                        f"已開始{_SCAN_MODE_LABELS[scan_mode]}"
                        if started
                        else f"{status['scan_mode_label']}正在執行，已加入目前進度"
                    ),
                },
            )

        def _read_json_body(self) -> dict[str, Any]:
            raw_length = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_length)
            except (TypeError, ValueError) as exc:
                raise ValueError("請求長度不正確") from exc
            if length < 0 or length > 16_384:
                raise ValueError("通知請求內容過大")
            if length == 0:
                return {}
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("請求內容必須是正確的 JSON") from exc
            if not isinstance(payload, dict):
                raise ValueError("請求內容格式不正確")
            return payload

        def log_message(self, format_string: str, *args: Any) -> None:
            LOGGER.info("HTTP %s - %s", self.address_string(), format_string % args)

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self._send_bytes(status, body, "application/json; charset=utf-8")

        def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status.value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((host, port), Handler)
    LOGGER.info("Radar dashboard listening on http://%s:%d", host, port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        runtime.stop()
        server.server_close()
