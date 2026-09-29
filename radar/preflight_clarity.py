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


def _quality_reason(key, payload):
    """Explain the actual scoring inputs, never infer market strength from a score."""
    live = mapping(payload.get("live"))
    inputs = mapping(live.get("quality_inputs"))
    position = mapping(payload.get("position_advisory"))
    thresholds = mapping(inputs.get("quality_thresholds"))
    if key == "entry_location":
        state = position.get("state")
        if state == "WITHIN":
            return "現價位於原進場區間，位置分不因訊號階段或等待重新確認而扣分"
        location = mapping(inputs.get("entry_location"))
        if location.get("key") == "ADVERSE_OUTSIDE":
            return "現價已離開原進場區，移向止損一側，因此位置分較低"
        chase = number(live.get("chase_atr"))
        if state in {"ABOVE", "BELOW"} and chase is not None and chase > 0:
            return f"現價已{'高' if state == 'ABOVE' else '低'}於原進場區，順向偏離 {chase:.2f} 倍近期波幅，位置分按偏離程度計算"
        return "位置資料不足，無法確認價格與原進場區的距離"
    if key == "stop_distance":
        risk = number(inputs.get("risk_pct"))
        if risk is not None:
            band = "超過 5%，因此此項分數較低" if risk > 5 else "介於 2.5%～5%，此項採中間分數" if risk > 2.5 else "在 2.5% 以內，此項採較高分數"
            return f"現價到原止損距離為 {risk:.2f}%，{band}"
    if key == "risk_reward":
        rr = number(inputs.get("risk_reward"))
        target = number(thresholds.get("target_rr"))
        if live.get("remaining_rr_applicable") is False:
            return "現價位於進場區不利側，剩餘風報暫不適用，此項不給分"
        if rr is not None and target is not None:
            return f"依現價到原止損與第一目標計算，剩餘風報為 {rr:.2f}R（評分基準 {target:.2f}R）"
    if key == "spread":
        spread = number(inputs.get("spread_pct"))
        limit = number(thresholds.get("max_spread_pct"))
        if spread is not None and limit is not None:
            return f"目前買賣價差 {spread:.4f}%，{'超過' if spread > limit else '未超過'} {limit:.4f}% 的扣分門檻"
    if key == "execution_cost":
        if not live.get("quality_cost_estimated"):
            return "本次委託簿深度不足，成本採中性估值；不代表實際成交成本改善"
        cost = number(inputs.get("execution_cost_to_risk_pct"))
        limit = number(thresholds.get("max_cost_to_risk_pct"))
        if cost is not None and limit is not None:
            return f"價差、預估滑價與手續費合計占止損風險 {cost:.1f}%，{'超過' if cost > limit else '未超過'} {limit:.1f}% 的扣分門檻"
    return "本次計分依據不足，無法說明實際原因"


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
    old_version = old.get("score_components_version")
    live_version = live.get("quality_components_version")
    if old_version != live_version and live_version == 2:
        result["basis_changed"] = True
        result["delta"] = None
        result["note"] = "原品質使用舊評分方式，與本次無法直接比較；以下僅列本次評分，無法精確歸因分數變化。重新掃描後將採用相同評分方式。"
    comparable = (old_version == live_version == 2
                  and all(number(previous.get(k)) is not None and number(latest.get(k)) is not None for k in COMPONENTS)
                  and original is not None and current is not None
                  and abs(sum(number(previous[k]) for k in COMPONENTS) - original) <= 0.11
                  and abs(sum(number(latest[k]) for k in COMPONENTS) - current) <= 0.11)
    if comparable:
        changes = sorted(((k, number(latest[k]) - number(previous[k])) for k in COMPONENTS),
                         key=lambda row: abs(row[1]), reverse=True)
        reasons = [f"{COMPONENTS[k]}評分{'提高' if change > 0 else '降低'} {abs(change):.1f} 分：{_quality_change_detail(k, old, payload)}。"
                   for k, change in changes if abs(change) >= 0.05][:2]
        result.update(mode="COMPARISON", reasons=reasons or ["各項評分大致持平。"], note="列出影響最大的兩項；分數不代表勝率。")
    else:
        # Show actual current weighted points, never invent historical inputs.
        rows = [(k, number(latest.get(k))) for k in COMPONENTS]
        maxima = {"entry_location": 28.5, "spread": 10, "risk_reward": 25, "stop_distance": 13.5, "execution_cost": 5}
        rows = sorted((row for row in rows if row[1] is not None),
                      key=lambda row: maxima[row[0]] - row[1], reverse=True)
        result["reasons"] = [f"本次{COMPONENTS[k]}評分 {value:.1f} 分：{_quality_reason(k, payload)}。" for k, value in rows[:2]]
    if not live.get("quality_cost_estimated"):
        result["note"] += " 本次深度資料不足，成交成本分項使用中性估值。"
    return result


def _quality_change_detail(key, old, payload):
    fields = {"spread": ("spread_pct", "%"), "risk_reward": ("risk_reward", "R"),
              "stop_distance": ("risk_pct", "%"), "execution_cost": ("execution_cost_to_risk_pct", "%")}
    detail = _quality_reason(key, payload)
    if key not in fields:
        return detail
    field, unit = fields[key]
    live = mapping(payload.get("live"))
    previous = number(old.get(field))
    current = number(mapping(live.get("quality_inputs")).get(field))
    if key == "execution_cost" and (not old.get("execution_cost_estimated") or not live.get("quality_cost_estimated")):
        return detail
    if previous is not None and current is not None and previous != current:
        return f"{previous:g}{unit} → {current:g}{unit}；{detail}"
    return detail


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
    confirmation_pending = (
        not lifecycle.get("terminal")
        and live.get("reentry_confirmation_required") is True
        and live.get("closed_retest_confirmed") is not True
    )
    within = mapping(payload.get("position_advisory")).get("state") == "WITHIN"
    result["confirmation_assessment"] = {
        "pending": confirmation_pending, "affects_position_score": False,
        "note": (("價格位於進場區，但" if within else "")
                 + "最近掃描尚未記錄新的收線回踩確認；此提醒不另扣位置分。") if confirmation_pending else "",
    }
    result["early_warning"] = preflight_early_warning(signal, payload)
    return result
