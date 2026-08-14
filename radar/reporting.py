from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .models import RadarReport


def _quality_name(status: str) -> str:
    return {
        "FULL": "完整",
        "PARTIAL_CONTEXT": "部分深度資料缺漏",
        "DATA_INCOMPLETE": "資料不完整",
    }.get(status, status)


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


def report_markdown(report: RadarReport) -> str:
    status_names = {
        "SIGNALS_FOUND": "資料最新",
        "NO_QUALIFIED_SIGNAL": "資料最新／沒有合格訊號",
        "DATA_INCOMPLETE": "掃描異常／資料不完整",
    }
    direction_names = {"LONG": "做多", "SHORT": "做空", "NEUTRAL": "中性"}
    stage_names = {
        "EARLY": "早期訊號",
        "EARLY_SIGNAL": "早期訊號",
        "CONFIRMED": "完整確認",
    }
    lines = [
        "# OKX USDT 永續雷達",
        "",
        f"- 狀態：{status_names.get(report.status, report.status)}",
        f"- 產生時間：{report.generated_at}",
        f"- 覆蓋：{report.fetched_count}/{report.target_count}（{report.coverage_pct:.2f}%）",
        f"- 可分析：{report.analyzable_count}",
        f"- 即時市場資料：{report.context_enriched_count}/{report.context_target_count}（{report.context_coverage_pct:.2f}%）",
        f"- 資料品質：{_quality_name(report.data_quality_status)}",
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
    if report.data_quality_status == "PARTIAL_CONTEXT":
        lines.extend(
            [
                "## 深度資料提醒",
                "",
                "部分候選的 5m／Funding／Taker Flow／Order Book／成交成本資料未完整取得；這些候選已排除，不會成為正式訊號。",
                "",
            ]
        )
    if not report.signals:
        lines.extend(["本輪沒有符合位置、證據與成交成本的進場訊號。", ""])
    else:
        lines.extend(["## 進場訊號", ""])
        for index, signal in enumerate(report.signals, 1):
            lines.extend(
                [
                    f"### {index}. {signal.inst_id} — {direction_names.get(signal.direction, '中性')}",
                    "",
                    f"- 策略：{signal.strategy}（{signal.regime}）",
                    f"- 階段：{stage_names.get(signal.signal_stage, signal.signal_stage)}",
                    f"- 準備度：{signal.readiness_score}%",
                    f"- 說明：{signal.summary}",
                    f"- 趨勢力度：{signal.trend_strength_label}（{signal.trend_strength_score}）",
                    f"- 分數：{signal.score}",
                    f"- 進場區：{signal.entry_low} ～ {signal.entry_high}",
                    f"- 止損：{signal.stop_loss}",
                    f"- TP1 / TP2：{signal.take_profit_1} / {signal.take_profit_2}",
                    f"- 風報比：{signal.risk_reward}",
                    f"- 失效條件：{signal.invalidation}",
                    "- 證據：" + "；".join(signal.evidence),
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
    lines.append("分析用途，不保證獲利；單筆最大風險建議不超過帳戶 1%。")
    return "\n".join(lines)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
