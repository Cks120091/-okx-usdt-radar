from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .models import RadarReport


def save_report(report: RadarReport, data_dir: str | Path) -> tuple[Path, Path]:
    directory = Path(data_dir)
    history = directory / "history"
    history.mkdir(parents=True, exist_ok=True)
    payload = report.to_dict()
    timestamp = report.generated_at.replace(":", "-").replace("+", "_")
    history_path = history / f"{timestamp}.json"
    latest_path = directory / "latest.json"
    _atomic_json(history_path, payload)
    _atomic_json(latest_path, payload)
    return latest_path, history_path


def load_latest_report(data_dir: str | Path) -> RadarReport | None:
    latest_path = Path(data_dir) / "latest.json"
    if not latest_path.exists():
        return None
    try:
        payload = json.loads(latest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        return RadarReport.from_dict(payload)
    except (OSError, ValueError, TypeError):
        return None


def save_runtime_state(data_dir: str | Path, payload: dict) -> Path:
    """Persist the last scan-attempt state separately from market snapshots."""

    path = Path(data_dir) / "runtime_state.json"
    _atomic_json(path, payload)
    return path


def load_runtime_state(data_dir: str | Path) -> dict:
    path = Path(data_dir) / "runtime_state.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def report_markdown(report: RadarReport) -> str:
    status_names = {
        "SIGNALS_FOUND": "資料最新",
        "NO_QUALIFIED_SIGNAL": "資料最新／沒有合格訊號",
        "PARTIAL_DATA": "資料最新／部分標的核心資料缺失",
        "DATA_INCOMPLETE": "掃描異常／資料不完整",
    }
    direction_names = {"LONG": "做多", "SHORT": "做空", "NEUTRAL": "中性"}
    stage_names = {
        "EARLY": "早期訊號",
        "EARLY_SIGNAL": "早期訊號",
        "CONFIRMED": "完整確認",
        "TRENDING": "趨勢進行中",
        "REENTRY": "回踩再發動",
        "EXTENDED": "已延伸",
        "NO_FOLLOW_THROUGH": "未獲延續",
        "COMPLETED": "交易計畫完成",
        "INVALIDATED": "訊號失效",
    }
    lines = [
        "# OKX USDT 永續雷達",
        "",
        f"- 狀態：{status_names.get(report.status, report.status)}",
        f"- 產生時間：{report.generated_at}",
        f"- 覆蓋：{report.fetched_count}/{report.target_count}（{report.coverage_pct:.2f}%）",
        f"- 可分析：{report.analyzable_count}",
        f"- 即時市場資料：{report.context_enriched_count}/{report.context_target_count}",
        f"- 訊息：{report.message}",
        "",
    ]
    if report.status == "DATA_INCOMPLETE":
        lines.extend(["## 資料缺漏", ""])
        for inst_id, reason in report.failed_instruments.items():
            lines.append(f"- {inst_id}: {reason}")
        lines.append("")
        lines.append("本輪依規則不提供多空或進場訊號。")
        return "\n".join(lines)
    if not report.signals:
        lines.extend(["短線目前無新鮮進場訊號；系統不為湊數降低 Trigger 標準。", ""])
    else:
        lines.extend(["## 進場訊號", ""])
        for index, signal in enumerate(report.signals, 1):
            lines.extend(
                [
                    f"### {index}. {signal.inst_id} — {direction_names.get(signal.direction, '中性')}",
                    "",
                    f"- 策略：{signal.strategy}（{signal.regime}）",
                    f"- 階段：{stage_names.get(signal.signal_stage, signal.signal_stage)}",
                    f"- 目前進場狀態：{signal.entry_eligibility.get('label', '待確認')}",
                    f"- 防追價判定：{signal.entry_eligibility.get('reason', '距離資料不足')}",
                    f"- 新鮮度：{signal.freshness}",
                    f"- Trigger 類型：{signal.trigger_type}",
                    f"- 證據一致度（非勝率）：{signal.readiness_score}%",
                    f"- 說明：{signal.summary}",
                    f"- 趨勢力度：{signal.trend_strength_label}（{signal.trend_strength_score}）",
                    f"- 分數：{signal.score}",
                    f"- 進場區：{signal.entry_low} ～ {signal.entry_high}",
                    f"- 止損：{signal.stop_loss}",
                    f"- TP1 / TP2：{signal.take_profit_1} / {signal.take_profit_2}",
                    f"- 風報比：{signal.risk_reward}",
                    f"- 交易品質（非勝率）：{signal.execution_quality.get('score', '—')}%",
                    f"- 市場參與：{signal.market_participation.get('label', '資料暫缺')}",
                    f"- 失效條件：{signal.invalidation}",
                    "- 證據：" + "；".join(signal.evidence),
                    "",
                ]
            )
    if report.long_signals:
        lines.extend(["## 波段／長線雷達", ""])
        for index, signal in enumerate(report.long_signals, 1):
            lines.extend(
                [
                    f"### {index}. {signal.inst_id} — {direction_names.get(signal.direction, '中性')}",
                    "",
                    f"- Trigger：4H {stage_names.get(signal.signal_stage, signal.signal_stage)}",
                    f"- 類型：{signal.trigger_type}",
                    f"- 新鮮度：{signal.freshness}",
                    f"- 說明：{signal.summary}",
                    f"- 交易品質（非勝率）：{signal.execution_quality.get('score', '—')}%",
                    "",
                ]
            )
    if report.watchlist:
        lines.extend(["## 接近觸發觀察名單（不是進場訊號）", ""])
        for index, item in enumerate(report.watchlist, 1):
            lines.extend(
                [
                    f"### {index}. {item.inst_id} — {direction_names.get(item.direction, '中性')}",
                    "",
                    f"- 市場型態：{item.regime}",
                    f"- 適用策略：{item.preferred_strategy}",
                    f"- 準備度：{item.readiness_score}%",
                    f"- 說明：{item.summary}",
                    "",
                ]
            )
    performance = report.historical_performance.get("overall", {})
    if performance.get("sample_size", 0):
        lines.extend(
            [
                "## 真實歷史績效",
                "",
                f"- 樣本：n={performance['sample_size']}",
                f"- 勝率：{performance['win_rate_pct']}%",
                f"- 平均 R / Expectancy：{performance['average_r']} / {performance['expectancy_r']}",
                f"- Profit Factor：{performance['profit_factor']}",
                "",
            ]
        )
    else:
        lines.extend(["歷史樣本尚不足，因此不顯示假勝率。", ""])
    lines.append("分析用途，不保證獲利；Radar Signal 與是否實際下單完全分離。")
    return "\n".join(lines)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            handle.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
