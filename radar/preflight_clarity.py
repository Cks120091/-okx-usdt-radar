"""Explain existing preflight decisions and score arithmetic without granting entry."""
from __future__ import annotations

from .position_advisory import ACTIVE_SIGNAL_STAGES, mapping, number
from .early_warning import preflight_early_warning

COMPONENTS = {
    "entry_location": "進場位置",
    "spread": "買賣價差",
    "risk_reward": "剩餘風險報酬比",
    "stop_distance": "止損距離",
    "execution_cost": "成交成本",
}


def _quality_explanation(signal, payload):
    live = mapping(payload.get("live"))
    old = mapping(getattr(signal, "execution_quality", {}))
    original = number(mapping(payload.get("original")).get("quality_score"))
    current = number(live.get("quality_score"))
    previous = mapping(old.get("score_components"))
    latest = mapping(live.get("quality_components"))
    delta = round(current - original, 1) if original is not None and current is not None else None
    result = {"delta": delta, "mode": "CURRENT_ONLY", "reasons": [],
              "note": "原品質缺少可比對的明細；以下僅列本次評分，無法精確歸因分數變化。"}
    if mapping(payload.get("signal_lifecycle")).get("terminal") is True:
        result.update(mode="TERMINAL", reasons=["原計畫已結束，品質分數不再代表新進場機會。"], note="")
        return result
    comparable = (old.get("score_components_version") == live.get("quality_components_version") == 1
                  and all(number(previous.get(k)) is not None and number(latest.get(k)) is not None for k in COMPONENTS)
                  and original is not None and current is not None
                  and abs(sum(number(previous[k]) for k in COMPONENTS) - original) <= 0.11
                  and abs(sum(number(latest[k]) for k in COMPONENTS) - current) <= 0.11)
    if comparable:
        changes = sorted(((k, number(latest[k]) - number(previous[k])) for k in COMPONENTS),
                         key=lambda row: abs(row[1]), reverse=True)
        reasons = [f"{COMPONENTS[k]}評分{'提高' if change > 0 else '降低'} {abs(change):.1f} 分。"
                   for k, change in changes if abs(change) >= 0.05][:2]
        result.update(mode="COMPARISON", reasons=reasons or ["各項評分大致持平。"], note="列出影響最大的兩項；分數不代表勝率。")
    else:
        # Show actual current weighted points, never invent historical inputs.
        rows = [(k, number(latest.get(k))) for k in COMPONENTS]
        maxima = {"entry_location": 30, "spread": 10, "risk_reward": 25, "stop_distance": 13.5, "execution_cost": 5}
        rows = sorted((row for row in rows if row[1] is not None),
                      key=lambda row: maxima[row[0]] - row[1], reverse=True)
        result["reasons"] = [f"本次{COMPONENTS[k]}評分 {value:.1f} 分。" for k, value in rows[:2]]
    if not live.get("quality_cost_estimated"):
        result["note"] += " 本次深度資料不足，成交成本分項使用中性估值。"
    return result


def explain_preflight(signal, payload):
    """Attach factual explanations; preserve verdict, plan, direction and signal."""
    result = dict(payload)
    verdict = mapping(payload.get("verdict"))
    lifecycle = mapping(payload.get("signal_lifecycle"))
    original = mapping(payload.get("original"))
    live = mapping(payload.get("live"))
    trigger = mapping(mapping(getattr(signal, "market_story", {})).get("trigger"))
    stage = str(getattr(signal, "signal_stage", "") or "").upper()
    labels = {
        "EARLY_SIGNAL": "早期", "CONFIRMED": "完整確認", "REENTRY": "回踩再發動",
        "TRENDING": "趨勢進行中", "EXTENDED": "已延伸", "NO_FOLLOW_THROUGH": "未獲延續",
        "WATCH": "觀望", "NEAR_TRIGGER": "接近觸發", "PRE_TRIGGER": "快觸發",
        "PRE_CONTINUATION": "趨勢延續預備",
    }
    blockers = list(dict.fromkeys(str(x).upper() for x in verdict.get("hard_blockers", [])))
    reasons = []
    for code in blockers:
        if code == "NO_FORMAL_TRIGGER":
            if stage == "NO_FOLLOW_THROUGH":
                detail = "原訊號曾觸發，但最近掃描記錄為「未獲延續」；目前暫停新進場，原計畫仍保留。"
            elif stage not in ACTIVE_SIGNAL_STAGES:
                detail = f"最近掃描記錄為「{labels.get(stage, '待確認')}」，未符合可新進場的正式訊號階段。"
            else:
                detail = "最近掃描的觸發標記未成立，且沒有保留有效訊號的標記；原計畫保留，暫停新進場。"
        else:
            detail = {
                "DIRECTION_UNAVAILABLE": "多空方向資料不足。",
                "TIMEFRAME_DIRECTION_ALIGNMENT": mapping(payload.get("timeframe_alignment")).get("reason") or "大週期方向與原訊號方向未通過確認。",
                "OPPOSITE_SIGNAL": "已記錄正式反向訊號，原方向暫停新進場。",
                "EVIDENCE_CONFLICT": "核心證據互相衝突，等待重新掃描確認。",
                "CORE_DATA_UNAVAILABLE": "必要的核心資料不足，請重新掃描取得資料。",
                "CORE_CANDLE_UNCONFIRMED": "必要的 K 線尚未確認收盤。",
            }.get(code, "仍有必要條件未通過；請查看判定理由或重新掃描確認。")
        reasons.append({"code": code, "detail": detail})
    if lifecycle.get("terminal"):
        state, label = "PLAN_ENDED", "原交易計畫已結束"
    elif (verdict.get("status") in {"DATA_UNAVAILABLE", "UPDATE_FAILED"}
          or set(blockers) & {"CORE_DATA_UNAVAILABLE", "CORE_CANDLE_UNCONFIRMED", "DIRECTION_UNAVAILABLE"}
          or verdict.get("new_entry_allowed") is None):
        state, label = "DATA_UNAVAILABLE", "資料不足，暫時無法確認"
    elif verdict.get("new_entry_allowed") is False:
        state, label = "ENTRY_SUSPENDED", "原計畫保留，暫停新進場"
    else:
        state, label = "SIGNAL_ACTIVE", "原訊號仍有效"
    result["core_assessment"] = {
        "state": state, "label": label, "reasons": reasons,
        "source": "STORED_SCAN", "stage": stage,
        "trigger_marked": trigger.get("triggered"),
        "active_episode_preserved": trigger.get("active_episode_preserved") is True,
        "note": "核心判讀沿用最近掃描；本次更新報價、成交條件與續走資料，沒有重新計算多週期 K 線。",
    }
    result["data_times"] = {
        "core_scan_at": original.get("report_generated_at"),
        "quote_at": live.get("quote_sampled_at"),
        "execution_at": live.get("execution_sampled_at"),
    }
    result["quality_explanation"] = _quality_explanation(signal, payload)
    result["early_warning"] = preflight_early_warning(signal, payload)
    return result
