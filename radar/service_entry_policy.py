from __future__ import annotations

from typing import Any


POLICY_VERSION = "STABLE_ENTRY_REUSE_V1"


def _stable_episode_can_reuse_entry(
    payload: dict[str, Any],
    confirmation: dict[str, Any],
) -> bool:
    """Return whether a stable active Episode may keep ENTRY_READY.

    This never creates a new formal Trigger. It only removes the redundant
    requirement that an on-demand full refresh must emit another same-direction
    formal Trigger after the stored Episode and the live Entry contract have
    already passed. Durable Entry Window departure, invalidation, opposite
    signals and required core/live-price failures remain binding. Execution
    quality and risk metrics are advisory and do not veto ENTRY_READY.
    """

    if str(confirmation.get("status") or "").upper() != "ORIGINAL_DIRECTION_STABLE":
        return False
    if list(confirmation.get("hard_blockers", []) or []):
        return False

    verdict = dict(payload.get("verdict", {}) or {})
    lifecycle = dict(payload.get("signal_lifecycle", {}) or {})
    plan = dict(payload.get("plan_state", {}) or {})

    if str(verdict.get("status") or "").upper() != "ENTRY_READY":
        return False
    if verdict.get("actionable") is not True:
        return False
    if verdict.get("new_entry_allowed") is False:
        return False
    if list(verdict.get("hard_blockers", []) or []):
        return False

    if lifecycle.get("terminal") is True or lifecycle.get("active") is False:
        return False
    if str(lifecycle.get("status") or "ACTIVE").upper() in {
        "INVALIDATED",
        "COMPLETED",
        "TARGET_REACHED",
        "CLOSED_UNKNOWN",
    }:
        return False

    if plan.get("new_trigger_required") is True:
        return False
    if plan.get("direction_still_valid") is False:
        return False
    if plan.get("new_entry_allowed") is not True:
        return False
    if plan.get("old_plan_reusable_for_new_entry") is False:
        return False
    if str(plan.get("new_entry_status") or "READY").upper() not in {
        "READY",
        "ENTRY_READY",
    }:
        return False
    return True


def apply_service_entry_policy(service_module: Any) -> None:
    """Patch only the service confirmation merge used by the live web app."""

    if getattr(service_module, "_stable_entry_reuse_policy_applied", False):
        return

    original_merge = service_module._merge_preflight_confirmation

    def merge_preflight_confirmation(
        payload: dict[str, Any],
        confirmation: dict[str, Any],
    ) -> dict[str, Any]:
        # The same request-local confirmation object is later passed into the
        # canonical single-coin decision. Mutating it here keeps both views on
        # one contract without fabricating a new Trigger or changing Entry/SL/TP.
        if _stable_episode_can_reuse_entry(payload, confirmation):
            confirmation.update(
                {
                    "source_status": "ORIGINAL_DIRECTION_STABLE",
                    "status": "REVALIDATED",
                    "label": "原方向穩定｜沿用既有 Trigger",
                    "message": (
                        "最新已收盤資料沒有形成正式反向或失效；原 Trigger 仍有效，"
                        "而目前 Entry Zone 與必要成交條件已通過。這不是新的 Trigger，"
                        "不再要求重複出現同方向 formal Trigger 才能進場。"
                    ),
                    "new_entry_allowed": True,
                    "stable_episode_entry_reuse": True,
                    "entry_confirmation_policy": POLICY_VERSION,
                }
            )
        return original_merge(payload, confirmation)

    service_module._merge_preflight_confirmation = merge_preflight_confirmation
    service_module.STABLE_ENTRY_CONFIRMATION_POLICY_VERSION = POLICY_VERSION
    service_module._stable_entry_reuse_policy_applied = True
