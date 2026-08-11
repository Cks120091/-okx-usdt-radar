from __future__ import annotations

import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .models import Candle, Instrument, MarketState, RadarReport, Ticker
from .strategy import AdaptiveStrategyEngine, StrategyConfig


class PublicDataClient(Protocol):
    def get_usdt_swap_instruments(self) -> list[Instrument]: ...

    def get_swap_tickers(self) -> dict[str, Ticker]: ...

    def get_candles(self, inst_id: str, bar: str, limit: int = 100) -> list[Candle]: ...


@dataclass(frozen=True)
class ScannerConfig:
    max_signals: int = 10
    workers: int = 8
    candle_limit: int = 100
    min_quote_volume_24h: float = 1_000_000.0
    max_spread_pct: float = 0.25
    minimum_rr: float = 1.8


class MarketScanner:
    bars = ("4H", "1H", "15m")

    def __init__(self, client: PublicDataClient, config: ScannerConfig | None = None):
        self.client = client
        self.config = config or ScannerConfig()
        self.engine = AdaptiveStrategyEngine(
            StrategyConfig(
                min_quote_volume_24h=self.config.min_quote_volume_24h,
                max_spread_pct=self.config.max_spread_pct,
                minimum_rr=self.config.minimum_rr,
            )
        )

    def scan_once(self) -> RadarReport:
        started = time.monotonic()
        now = datetime.now(timezone.utc).isoformat()
        scope = "OKX state=live、USDT 結算、線性永續合約"
        try:
            instruments = self.client.get_usdt_swap_instruments()
        except Exception as exc:
            return self._fatal_report(now, scope, started, f"無法取得合約母清單：{exc}")
        if not instruments:
            return self._fatal_report(now, scope, started, "OKX 回傳的 live USDT 永續母清單為空。")

        target_ids = [item.inst_id for item in instruments]
        try:
            tickers = self.client.get_swap_tickers()
        except Exception as exc:
            return RadarReport(
                status="DATA_INCOMPLETE",
                generated_at=now,
                scope=scope,
                target_count=len(instruments),
                fetched_count=0,
                analyzable_count=0,
                coverage_pct=0.0,
                target_instruments=target_ids,
                failed_instruments={"_TICKERS_": str(exc)},
                signals=[],
                exclusion_counts={},
                duration_seconds=round(time.monotonic() - started, 3),
                message="雷達資料不完整：無法取得全市場 ticker，因此禁止輸出交易訊號。",
            )

        failures: dict[str, str] = {}
        bundles: dict[str, dict[str, list[Candle]]] = {}
        eligible = []
        for instrument in instruments:
            if instrument.inst_id not in tickers:
                failures[instrument.inst_id] = "bulk ticker 缺少此 live 合約"
            else:
                eligible.append(instrument)

        with ThreadPoolExecutor(max_workers=max(1, self.config.workers)) as executor:
            future_map = {
                executor.submit(self._fetch_bundle, instrument.inst_id): instrument.inst_id
                for instrument in eligible
            }
            for future in as_completed(future_map):
                inst_id = future_map[future]
                try:
                    bundles[inst_id] = future.result()
                except Exception as exc:
                    failures[inst_id] = str(exc)

        fetched_count = len(instruments) - len(failures)
        coverage = round((fetched_count / len(instruments)) * 100.0, 4)
        analyzable_count = sum(
            1
            for bundle in bundles.values()
            if all(len(bundle[bar]) >= 60 for bar in self.bars)
        )
        if failures:
            return RadarReport(
                status="DATA_INCOMPLETE",
                generated_at=now,
                scope=scope,
                target_count=len(instruments),
                fetched_count=fetched_count,
                analyzable_count=analyzable_count,
                coverage_pct=coverage,
                target_instruments=target_ids,
                failed_instruments=dict(sorted(failures.items())),
                signals=[],
                exclusion_counts={},
                duration_seconds=round(time.monotonic() - started, 3),
                message="雷達資料不完整：覆蓋率未達 100%，依安全規則禁止輸出多空與進場訊號。",
            )

        exclusion_counts: Counter[str] = Counter()
        signals = []
        market_states: list[MarketState] = []
        analysis_failures: dict[str, str] = {}
        instrument_map = {item.inst_id: item for item in instruments}
        for inst_id in target_ids:
            bundle = bundles[inst_id]
            try:
                result = self.engine.analyze(
                    instrument_map[inst_id],
                    tickers[inst_id],
                    bundle["4H"],
                    bundle["1H"],
                    bundle["15m"],
                )
            except Exception as exc:
                analysis_failures[inst_id] = f"分析引擎錯誤：{exc}"
                continue
            if result.market_state is not None:
                market_states.append(result.market_state)
            if result.signal is None:
                exclusion_counts[result.reason] += 1
            else:
                signals.append(result.signal)

        if analysis_failures:
            return RadarReport(
                status="DATA_INCOMPLETE",
                generated_at=now,
                scope=scope,
                target_count=len(instruments),
                fetched_count=fetched_count,
                analyzable_count=analyzable_count,
                coverage_pct=100.0,
                target_instruments=target_ids,
                failed_instruments=dict(sorted(analysis_failures.items())),
                signals=[],
                exclusion_counts=dict(exclusion_counts),
                duration_seconds=round(time.monotonic() - started, 3),
                message="雷達資料已取得，但部分合約分析失敗；為避免部分市場結果，禁止輸出訊號。",
            )

        signals.sort(key=lambda item: (item.score, item.quote_volume_24h), reverse=True)
        signals = signals[: min(max(self.config.max_signals, 0), 10)]
        watchlist = [
            item
            for item in market_states
            if item.status in ("NEAR_TRIGGER", "WATCH")
            and item.regime != "DISORDER"
            and item.direction != "NEUTRAL"
            and item.readiness_score >= 50.0
        ]
        watchlist.sort(
            key=lambda item: (item.readiness_score, item.quote_volume_24h),
            reverse=True,
        )
        watchlist = watchlist[:10]
        market_states.sort(key=lambda item: item.inst_id)
        regime_counts = Counter(item.regime for item in market_states)
        status = "SIGNALS_FOUND" if signals else "NO_QUALIFIED_SIGNAL"
        message = (
            f"完整掃描完成：{len(signals)} 個正式訊號，另列出 {len(watchlist)} 個接近觸發的觀察候選。"
            if signals
            else f"完整掃描完成：本輪 0 個正式訊號；另列出 {len(watchlist)} 個接近觸發的觀察候選，不代表可以直接進場。"
        )
        return RadarReport(
            status=status,
            generated_at=now,
            scope=scope,
            target_count=len(instruments),
            fetched_count=fetched_count,
            analyzable_count=len(market_states),
            coverage_pct=100.0,
            target_instruments=target_ids,
            failed_instruments={},
            signals=signals,
            exclusion_counts=dict(exclusion_counts.most_common()),
            duration_seconds=round(time.monotonic() - started, 3),
            message=message,
            market_regime_counts=dict(regime_counts.most_common()),
            watchlist=watchlist,
            market_map=market_states,
        )

    def _fetch_bundle(self, inst_id: str) -> dict[str, list[Candle]]:
        output: dict[str, list[Candle]] = {}
        for bar in self.bars:
            try:
                output[bar] = self.client.get_candles(inst_id, bar, self.config.candle_limit)
            except Exception as exc:
                raise RuntimeError(f"{bar} K 線取得失敗：{exc}") from exc
        return output

    @staticmethod
    def _fatal_report(now: str, scope: str, started: float, message: str) -> RadarReport:
        return RadarReport(
            status="DATA_INCOMPLETE",
            generated_at=now,
            scope=scope,
            target_count=0,
            fetched_count=0,
            analyzable_count=0,
            coverage_pct=0.0,
            target_instruments=[],
            failed_instruments={"_INSTRUMENTS_": message},
            signals=[],
            exclusion_counts={},
            duration_seconds=round(time.monotonic() - started, 3),
            message=f"雷達資料不完整：{message}",
        )
