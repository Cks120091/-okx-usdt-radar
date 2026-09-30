"""Short-radar observations: explain scan facts, never change entry permission."""
from __future__ import annotations

from .position_advisory import mapping, number
from .short_direction import hourly_direction

VERSION = "SHORT_SCAN_OBSERVATION_V1"
PREPARING = {"PRE_TRIGGER", "NEAR_TRIGGER", "PRE_CONTINUATION"}


def short_scan_preparation(hourly, candidates, *, closed):
    """Keep a real setup visible while direction/trigger permission is pending."""
    side = hourly["direction"]
    if not closed or side == "UNKNOWN":
        return {}
    own = mapping(candidates.get(side))
    if own.get("triggered") is True:
        return {}
    eligible = [mapping(candidate) for candidate in candidates.values()
                if (candidate.get("stage") in PREPARING
                    or (candidate.get("triggered") is True
                        and candidate.get("stage") in {"EARLY_SIGNAL", "CONFIRMED", "REENTRY"}))
                and not candidate.get("compression_block")
                and not mapping(candidate.get("noise")).get("high")
                and not mapping(candidate.get("control_transfer")).get("opponent_reclaimed")]
    if not eligible:
        return {}
    candidate = max(eligible, key=lambda value: (
        value.get("triggered") is True, value.get("direction") == side,
        number(value.get("explainability_score")) or 0,
    ))
    candidate_side = candidate.get("direction")
    if candidate_side not in {"LONG", "SHORT"}:
        return {}
    aligned = side == candidate_side
    name = "多" if candidate_side == "LONG" else "空"
    reason = (f"1H {hourly['label']}，15m {name}方形態形成中；等待同向價格觸發。"
              if aligned else f"1H {hourly['label']}，15m 出現{name}方形態；等待 1H 與 15m 同向，尚不可進場。")
    return {"code": "TRIGGER_PENDING" if aligned else "DIRECTION_PENDING",
            "candidate_direction": candidate_side, "hourly_direction": side,
            "label": "預備｜等待觸發" if aligned else "預備｜等待同向",
            "reason": reason, "advisory_only": True}


def short_scan_observation(hourly, candidates, *, closed, max_age_bars):
    """Keep otherwise-discarded opposite price evidence outside the Trigger."""
    side = hourly["direction"]
    result = {"version": VERSION, "direction": side, "hourly_score": hourly["score"],
              "code": "NONE", "closed": closed, "advisory_only": True}
    if not closed or side not in {"LONG", "SHORT"}:
        return result
    opposite = mapping(candidates.get("SHORT" if side == "LONG" else "LONG"))
    age = number(opposite.get("event_age_bars"))
    control = mapping(opposite.get("control_transfer"))
    fresh_opposite = (
        age is not None and age >= 0
        and not opposite.get("compression_block")
        and not mapping(opposite.get("noise")).get("high")
        and not control.get("opponent_reclaimed")
        and ((opposite.get("triggered") is True
              and opposite.get("stage") in {"EARLY_SIGNAL", "CONFIRMED", "REENTRY"})
             or (age <= max_age_bars and opposite.get("stage") in PREPARING
                 and control.get("push_away") is True
                 and control.get("micro_defense_broken") is True))
    )
    own = mapping(candidates.get(side))
    if fresh_opposite:
        result.update(code="OPPOSITE_PRICE", event_age_bars=age,
                      observed_direction=opposite.get("direction"))
    elif (own.get("triggered") is False and own.get("stage") in PREPARING
          and not own.get("compression_block")
          and not mapping(own.get("control_transfer")).get("opponent_reclaimed")
          and not mapping(own.get("noise")).get("high")):
        result.update(code="SETUP_FORMING", setup_type=own.get("pre_trigger_type"),
                      stage=own.get("stage"))
    return result


def preflight_early_warning(signal, payload, *, scan_at=None, source="STORED_SCAN"):
    """One readable paragraph; missing evidence never becomes a positive signal."""
    if (getattr(signal, "radar_horizon", None) != "SHORT"
            or mapping(payload.get("signal_lifecycle")).get("terminal") is True):
        return {}
    raw = mapping(mapping(getattr(signal, "market_metrics", {})).get("raw_indicators"))
    hourly = hourly_direction(mapping(raw.get("1H")))
    side = hourly["direction"]
    story = mapping(getattr(signal, "market_story", {}))
    observation = mapping(mapping(story.get("raw")).get("early_observation"))
    # Never mix an older/opposite scan observation with newer hourly evidence.
    valid_observation = (observation.get("version") == VERSION
                         and observation.get("advisory_only") is True
                         and observation.get("direction") == side
                         and observation.get("hourly_score") == hourly["score"])
    code = observation.get("code") if valid_observation else None
    stage = str(getattr(signal, "signal_stage", "") or getattr(signal, "status", "") or "").upper()
    quality = mapping(getattr(signal, "data_quality", {}))
    if side == "UNKNOWN":
        code, text = "DATA_UNAVAILABLE", "1H 方向資料不足，暫時無法判讀轉弱或反向變化；請重新掃描。"
    elif (quality.get("core") == "UNAVAILABLE" or quality.get("closed_candle") is False
          or (valid_observation and observation.get("closed") is False)):
        code, text = "DATA_UNAVAILABLE", "核心 K 線資料不足或尚未收盤，暫時無法確認預警；請重新掃描。"
    elif side == "NEUTRAL":
        code, text = "DIRECTION_UNSET", "1H 多空方向未定，15m 的變化先觀察；等待 1H 方向明確。"
    else:
        name, counter, move = ("多", "空", "回落") if side == "LONG" else ("空", "多", "反彈")
        weak = hourly["state"].endswith("_WEAKENING")
        prefix = f"1H 仍偏{name}" + (f"，但{name}頭力道偏弱" if weak else "")
        if code == "OPPOSITE_PRICE":
            text = prefix + f"；15m 出現反向價格變化，留意{move}，尚不能據此轉做{counter}。"
        elif stage == "NO_FOLLOW_THROUGH":
            code = "NO_FOLLOW_THROUGH"
            text = prefix + "；原訊號觸發後未獲延續，等待同向價格條件重新確認。"
        elif code == "SETUP_FORMING":
            setup = {"BREAKOUT": "突破", "REVERSAL": "反轉", "CONTINUATION": "回踩續行"}.get(observation.get("setup_type"), "同向")
            text = prefix + f"；15m {setup}形態正在形成，仍待同向價格條件確認，尚未正式觸發。"
        elif weak:
            code = "DIRECTION_WEAKENING"
            text = prefix + f"；留意{move}，目前尚未確認轉{counter}。"
        else:
            return {}
    return {"code": code, "text": text, "advisory_only": True,
            "source": source, "scan_at": scan_at or mapping(payload.get("original")).get("report_generated_at"),
            "timeframes": ["1H", "15m"]}
