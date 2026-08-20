from __future__ import annotations

import inspect
import math
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from .models import Candle, Instrument, MarketContext, MarketState, RadarReport, Signal, Ticker
from .repository import SignalRepository, classify_microstructure
from .strategy import (
    AdaptiveStrategyEngine,
    AnalysisResult,
    StrategyConfig,
    _entry_eligibility,
)


class PublicDataClient(Protocol):
    def get_usdt_swap_instruments(self) -> list[Instrument]: ...

    def get_swap_tickers(self) -> dict[str, Ticker]: ...

    def get_candles(self, inst_id: str, bar: str, limit: int = 100) -> list[Candle]: ...

    def get_open_interest_usd(self) -> dict[str, float]: ...

    def get_market_context(
        self,
        inst_id: str,
        open_interest_usd: float | None = None,
    ) -> MarketContext: ...


@dataclass(frozen=True)
class ScannerConfig:
    max_signals: int = 20
    max_watchlist: int = 20
    workers: int = 12
    candle_limit: int = 100
    candle_limit_1d: int = 200
    candle_limit_4h: int = 200
    candle_limit_1h: int = 240
    candle_limit_15m: int = 200
    candle_limit_5m: int = 120
    min_quote_volume_24h: float = 5_000_000.0
    max_spread_pct: float = 0.10
    universe_max_spread_pct: float = 1.00
    min_open_interest_usd: float = 3_000_000.0
    require_micro_volume_anomaly: bool = False
    minimum_rr: float = 1.8
    context_candidates: int = 100
    estimated_taker_fee_pct: float = 0.05
    max_execution_cost_to_risk_pct: float = 12.0
    max_entry_extension_atr: float = 0.80
    severe_entry_extension_atr: float = 1.80
    max_slippage_pct: float = 0.15
    early_signal_max_age_bars: int = 2
    entry_ready_max_chase_atr: float = 0.15
    entry_missed_chase_atr: float = 0.50
    previous_open_interest_usd: dict[str, float] = field(default_factory=dict)
    state_db_path: str = ":memory:"


ProgressCallback = Callable[[str, int | None, int | None, str], None]
PreviewCallback = Callable[[RadarReport], None]


class MarketScanner:
    bars = ("1D", "4H", "1H", "15m")
    short_bars = ("4H", "1H", "15m")
    _bar_interval_ms = {
        "1D": 86_400_000,
        "4H": 14_400_000,
        "1H": 3_600_000,
    }

    def __init__(self, client: PublicDataClient, config: ScannerConfig | None = None):
        self.client = client
        self.config = config or ScannerConfig()
        self._previous_open_interest_usd = dict(self.config.previous_open_interest_usd)
        self._signal_history: dict[tuple[str, str], dict[str, str]] = {}
        self._candle_cache: dict[tuple[str, str], list[Candle]] = {}
        self._candle_cache_lock = threading.Lock()
        self.repository = SignalRepository(
            self.config.state_db_path,
            self.config.early_signal_max_age_bars,
        )
        self.engine = AdaptiveStrategyEngine(
            StrategyConfig(
                min_quote_volume_24h=self.config.min_quote_volume_24h,
                max_spread_pct=self.config.max_spread_pct,
                universe_max_spread_pct=self.config.universe_max_spread_pct,
                min_open_interest_usd=self.config.min_open_interest_usd,
                require_micro_volume_anomaly=self.config.require_micro_volume_anomaly,
                minimum_rr=self.config.minimum_rr,
                estimated_taker_fee_pct=self.config.estimated_taker_fee_pct,
                max_execution_cost_to_risk_pct=self.config.max_execution_cost_to_risk_pct,
                max_entry_extension_atr=self.config.max_entry_extension_atr,
                severe_entry_extension_atr=self.config.severe_entry_extension_atr,
                max_slippage_pct=self.config.max_slippage_pct,
                early_signal_max_age_bars=self.config.early_signal_max_age_bars,
                entry_ready_max_chase_atr=self.config.entry_ready_max_chase_atr,
                entry_missed_chase_atr=self.config.entry_missed_chase_atr,
            )
        )

    def scan_once(
        self,
        progress: ProgressCallback | None = None,
        scan_id: str | None = None,
        preview: PreviewCallback | None = None,
    ) -> RadarReport:
        """Run the V3.3 two-radar pipeline without fake fallback values."""

        started = time.monotonic()
        scan_started_at = datetime.now(timezone.utc).isoformat()
        scan_id = scan_id or str(uuid.uuid4())
        reset_metrics = getattr(self.client, "reset_metrics", None)
        if callable(reset_metrics):
            reset_metrics()
        scope = "OKX state=live、USDT 結算、線性永續合約"
        self._progress(progress, "INSTRUMENTS", 0, None, "正在同步 OKX live USDT 永續 Universe")
        try:
            instruments = self.client.get_usdt_swap_instruments()
        except Exception as exc:
            return self._fatal_report(
                scan_started_at,
                scope,
                started,
                f"無法取得合約母清單：{exc}",
                scan_id,
            )
        if not instruments:
            return self._fatal_report(
                scan_started_at,
                scope,
                started,
                "OKX 回傳的 live USDT 永續母清單為空。",
                scan_id,
            )
        try:
            tickers = self.client.get_swap_tickers()
        except Exception as exc:
            return self._fatal_report(
                scan_started_at,
                scope,
                started,
                f"無法取得全市場 Ticker：{exc}",
                scan_id,
            )

        instrument_map = {item.inst_id: item for item in instruments}
        target_ids = sorted(instrument_map)
        failures: dict[str, str] = {}
        eligible = []
        for instrument in instruments:
            if instrument.inst_id not in tickers:
                failures[instrument.inst_id] = "bulk ticker 缺少此 live 合約"
            else:
                eligible.append(instrument)

        self._progress(
            progress,
            "CANDLES",
            0,
            len(eligible),
            "正在取得 4H／1H／15m 已收盤 K 線",
        )
        bundles: dict[str, dict[str, list[Candle]]] = {}
        with ThreadPoolExecutor(max_workers=max(1, self.config.workers)) as executor:
            future_map = {
                executor.submit(
                    self._fetch_bundle,
                    item.inst_id,
                    self.short_bars,
                ): item.inst_id
                for item in eligible
            }
            for completed, future in enumerate(as_completed(future_map), 1):
                inst_id = future_map[future]
                try:
                    bundle = future.result()
                    if not all(
                        len(bundle.get(bar, [])) >= 60
                        for bar in self.short_bars
                    ):
                        raise RuntimeError("核心 K 線少於 60 根")
                    bundles[inst_id] = bundle
                except Exception as exc:
                    failures[inst_id] = str(exc)
                self._progress(
                    progress,
                    "CANDLES",
                    completed,
                    len(eligible),
                    "正在取得全市場多時間框架資料",
                )

        if not bundles:
            completed_at = datetime.now(timezone.utc).isoformat()
            return RadarReport(
                status="DATA_INCOMPLETE",
                generated_at=completed_at,
                scope=scope,
                target_count=len(instruments),
                fetched_count=0,
                analyzable_count=0,
                coverage_pct=0.0,
                target_instruments=target_ids,
                failed_instruments=dict(sorted(failures.items())),
                signals=[],
                exclusion_counts={},
                duration_seconds=round(time.monotonic() - started, 3),
                message="所有標的核心 K 線皆不可用，本輪無法判定。",
                scan_id=scan_id,
                scan_started_at=scan_started_at,
                completed_at=completed_at,
                runtime_status="ERROR",
                actionable=False,
                max_signals=min(max(self.config.max_signals, 0), 20),
                data_quality={"core": "UNAVAILABLE", "no_fake_fallback": True},
            )

        short_results: dict[str, AnalysisResult] = {}
        analysis_failures: dict[str, str] = {}
        self._progress(
            progress,
            "ANALYSIS",
            0,
            len(bundles),
            "正在建立 15m Market Story 與價格 Trigger",
        )
        for index, (inst_id, bundle) in enumerate(sorted(bundles.items()), 1):
            try:
                short_results[inst_id] = self._analyze_short_v33(
                    instrument_map[inst_id],
                    tickers[inst_id],
                    bundle,
                )
            except Exception as exc:
                analysis_failures[f"{inst_id}:SHORT"] = f"短線分析錯誤：{exc}"
            self._progress(
                progress,
                "ANALYSIS",
                index,
                len(bundles),
                "正在判定短線 15m Trigger",
            )

        market_bias = self._calculate_market_bias(short_results)
        if preview is not None:
            preview(
                self._core_preview_report(
                    scan_id=scan_id,
                    scan_started_at=scan_started_at,
                    scope=scope,
                    started=started,
                    instruments=instruments,
                    target_ids=target_ids,
                    bundles=bundles,
                    failures=failures,
                    analysis_failures=analysis_failures,
                    short_results=short_results,
                    market_bias=market_bias,
                )
            )

        long_results: dict[str, AnalysisResult] = {}
        if callable(getattr(self.engine, "analyze_long", None)):
            self._progress(
                progress,
                "LONG_CANDLES",
                0,
                len(bundles),
                "15m 已發布；正在補 1D 與長線雷達",
            )
            with ThreadPoolExecutor(
                max_workers=max(1, self.config.workers)
            ) as executor:
                future_map = {
                    executor.submit(
                        self._fetch_bundle,
                        inst_id,
                        ("1D",),
                    ): inst_id
                    for inst_id in bundles
                }
                for completed, future in enumerate(as_completed(future_map), 1):
                    inst_id = future_map[future]
                    try:
                        daily = future.result().get("1D", [])
                        if len(daily) < 60:
                            raise RuntimeError("1D K 線少於 60 根")
                        bundles[inst_id]["1D"] = daily
                    except Exception as exc:
                        analysis_failures[f"{inst_id}:LONG"] = (
                            f"長線 1D 取得錯誤：{exc}"
                        )
                    self._progress(
                        progress,
                        "LONG_CANDLES",
                        completed,
                        len(bundles),
                        "15m 已發布；正在補 1D 資料",
                    )

            long_ready = [
                inst_id for inst_id, bundle in bundles.items() if "1D" in bundle
            ]
            self._progress(
                progress,
                "LONG_ANALYSIS",
                0,
                len(long_ready),
                "正在判定長線 4H Trigger",
            )
            for index, inst_id in enumerate(sorted(long_ready), 1):
                try:
                    long_result = self._analyze_long_v33(
                        instrument_map[inst_id],
                        tickers[inst_id],
                        bundles[inst_id],
                    )
                    if long_result is not None:
                        long_results[inst_id] = long_result
                except Exception as exc:
                    analysis_failures[f"{inst_id}:LONG"] = (
                        f"長線分析錯誤：{exc}"
                    )
                self._progress(
                    progress,
                    "LONG_ANALYSIS",
                    index,
                    len(long_ready),
                    "15m 已發布；長線 4H Trigger 分析中",
                )

        if not short_results and not long_results:
            completed_at = datetime.now(timezone.utc).isoformat()
            return RadarReport(
                status="DATA_INCOMPLETE",
                generated_at=completed_at,
                scope=scope,
                target_count=len(instruments),
                fetched_count=len(bundles),
                analyzable_count=0,
                coverage_pct=round(len(bundles) / len(instruments) * 100.0, 3),
                target_instruments=target_ids,
                failed_instruments={
                    **dict(sorted(failures.items())),
                    **analysis_failures,
                },
                signals=[],
                exclusion_counts={},
                duration_seconds=round(time.monotonic() - started, 3),
                message="核心資料已取得，但 Market Story Engine 無法完成任何標的。",
                scan_id=scan_id,
                scan_started_at=scan_started_at,
                completed_at=completed_at,
                runtime_status="ERROR",
                actionable=False,
                max_signals=min(max(self.config.max_signals, 0), 20),
                data_quality={
                    "core": "ANALYSIS_FAILED",
                    "no_fake_fallback": True,
                },
            )
        context_failures: dict[str, list[str]] = {}
        open_interest: dict[str, float] = {}
        oi_loader = getattr(self.client, "get_open_interest_usd", None)
        if callable(oi_loader):
            try:
                open_interest = oi_loader()
                if not open_interest:
                    context_failures["_OPEN_INTEREST_"] = ["OKX Open Interest 清單為空"]
            except Exception as exc:
                context_failures["_OPEN_INTEREST_"] = [str(exc)]

        for results in (short_results, long_results):
            for inst_id, result in list(results.items()):
                current_oi = open_interest.get(inst_id)
                results[inst_id] = self._attach_oi_snapshot(
                    result,
                    current_oi,
                    self._open_interest_change(inst_id, current_oi),
                )

        context_loader = getattr(self.client, "get_market_context", None)
        context_applier = getattr(self.engine, "apply_market_context", None)
        ranked_ids = self._rank_context_candidates(short_results, long_results)
        context_limit = min(max(0, int(self.config.context_candidates)), 100)
        context_target_ids = ranked_ids[:context_limit]
        contexts: dict[str, MarketContext] = {}
        micro_candles: dict[str, list[Candle]] = {}
        context_enriched_count = 0
        context_complete_count = 0
        source_success = Counter()
        source_missing = Counter()

        if callable(context_loader) and callable(context_applier) and context_target_ids:
            self._progress(
                progress,
                "CONTEXT",
                0,
                len(context_target_ids),
                "正在取得 5m、Funding、Taker、CVD 與 Order Book",
            )

            def load_context(inst_id: str) -> tuple[MarketContext, list[Candle], list[str]]:
                local_errors: list[str] = []
                timing: list[Candle] = []
                try:
                    timing = self.client.get_candles(
                        inst_id,
                        "5m",
                        self.config.candle_limit_5m,
                    )
                    if len(timing) < 60:
                        local_errors.append("5m 已收盤 K 線不足 60 根")
                        timing = []
                except Exception as exc:
                    local_errors.append(f"5m: {exc}")
                context = context_loader(inst_id, open_interest.get(inst_id))
                context = replace(
                    context,
                    open_interest_change_pct=self._open_interest_change(
                        inst_id,
                        context.open_interest_usd,
                    ),
                )
                return context, timing, local_errors

            with ThreadPoolExecutor(max_workers=max(1, min(self.config.workers, 8))) as executor:
                future_map = {
                    executor.submit(load_context, inst_id): inst_id
                    for inst_id in context_target_ids
                }
                for completed, future in enumerate(as_completed(future_map), 1):
                    inst_id = future_map[future]
                    try:
                        context, timing, local_errors = future.result()
                        contexts[inst_id] = context
                        if timing:
                            micro_candles[inst_id] = timing
                        failures_for_symbol = [*context.failures, *local_errors]
                        if failures_for_symbol:
                            context_failures[inst_id] = failures_for_symbol
                        available = set(context.source_timestamps)
                        if timing:
                            available.add("timing")
                        if context.funding_rate is not None:
                            available.add("funding")
                        if context.order_book_imbalance is not None:
                            available.add("order_book")
                        if context.taker_buy_ratio is not None:
                            available.add("trades")
                        if context.open_interest_usd is not None:
                            available.add("open_interest")
                        for source in available:
                            source_success[source] += 1
                        for source in (
                            "funding",
                            "order_book",
                            "trades",
                            "timing",
                            "open_interest",
                        ):
                            if source not in available:
                                source_missing[source] += 1
                        if available:
                            context_enriched_count += 1
                        if (
                            context.complete
                            and bool(timing)
                            and context.open_interest_usd is not None
                        ):
                            context_complete_count += 1
                    except Exception as exc:
                        context_failures[inst_id] = [str(exc)]
                    self._progress(
                        progress,
                        "CONTEXT",
                        completed,
                        len(context_target_ids),
                        "正在取得候選深度資料；缺失只標記、不刪 Trigger",
                    )

            btc_result = short_results.get("BTC-USDT-SWAP")
            btc_bias = (
                btc_result.market_state.direction
                if btc_result and btc_result.market_state
                else "NEUTRAL"
            )
            for inst_id in context_target_ids:
                context = contexts.get(inst_id)
                if context is None:
                    if inst_id in short_results:
                        short_results[inst_id] = self._mark_deep_data_missing(
                            short_results[inst_id],
                            context_failures.get(inst_id, ["Deep Data 暫缺"]),
                        )
                    if inst_id in long_results:
                        long_results[inst_id] = self._mark_deep_data_missing(
                            long_results[inst_id],
                            context_failures.get(inst_id, ["Deep Data 暫缺"]),
                        )
                    continue
                previous_micro = self.repository.load_microstructure(inst_id)
                for results, timing in (
                    (short_results, micro_candles.get(inst_id)),
                    (long_results, bundles[inst_id]["1H"]),
                ):
                    result = results.get(inst_id)
                    if result is None or result.market_state is None:
                        continue
                    direction = result.market_state.direction
                    sequence = (
                        classify_microstructure(
                            previous_micro,
                            context,
                            direction,
                            result.market_state.market_metrics.get("price_change_core_pct"),
                        )
                        if direction in ("LONG", "SHORT")
                        else {
                            "state": "NEUTRAL",
                            "reason": "方向中性，不替單張委託簿賦予多空意義",
                        }
                    )
                    directional_context = replace(
                        context,
                        order_book_sequence=sequence,
                    )
                    try:
                        results[inst_id] = context_applier(
                            result,
                            directional_context,
                            btc_bias,
                            timing,
                            market_bias,
                        )
                    except Exception as exc:
                        context_failures.setdefault(inst_id, []).append(
                            f"Context 整合失敗：{exc}"
                        )
                        results[inst_id] = self._mark_deep_data_missing(
                            result,
                            [str(exc)],
                        )
                self.repository.save_microstructure(
                    context,
                    datetime.now(timezone.utc).isoformat(),
                )

        if open_interest:
            self._previous_open_interest_usd = dict(open_interest)

        exclusion_counts: Counter[str] = Counter()
        short_states, raw_short_signals = self._collect_results(short_results, exclusion_counts)
        long_states, raw_long_signals = self._collect_results(long_results, exclusion_counts)
        completed_at = datetime.now(timezone.utc).isoformat()
        short_signals = self.repository.reconcile(
            raw_short_signals,
            short_states,
            completed_at,
            "SHORT",
        )
        long_signals = self.repository.reconcile(
            raw_long_signals,
            long_states,
            completed_at,
            "LONG",
        )
        short_signals = [self._refresh_entry_eligibility(item) for item in short_signals]
        long_signals = [self._refresh_entry_eligibility(item) for item in long_signals]
        short_signals = [_without_internal_metrics(item) for item in short_signals]
        long_signals = [_without_internal_metrics(item) for item in long_signals]
        short_states = [_without_internal_metrics(item) for item in short_states]
        long_states = [_without_internal_metrics(item) for item in long_states]
        short_signals = sorted(short_signals, key=self._signal_sort_key, reverse=True)[
            : min(max(self.config.max_signals, 0), 20)
        ]
        long_signals = sorted(long_signals, key=self._signal_sort_key, reverse=True)[
            : min(max(self.config.max_signals, 0), 20)
        ]
        short_watchlist = self._watchlist(short_states)
        long_watchlist = self._watchlist(long_states)
        short_states.sort(key=lambda item: item.inst_id)
        long_states.sort(key=lambda item: item.inst_id)

        api_metrics_loader = getattr(self.client, "metrics_snapshot", None)
        api_metrics = api_metrics_loader() if callable(api_metrics_loader) else {}
        duration = round(time.monotonic() - started, 3)
        all_failures = {**dict(sorted(failures.items())), **dict(sorted(analysis_failures.items()))}
        coverage = round(len(bundles) / len(instruments) * 100.0, 4)
        status = (
            "PARTIAL_DATA"
            if all_failures
            else "SIGNALS_FOUND"
            if short_signals or long_signals
            else "NO_QUALIFIED_SIGNAL"
        )
        data_quality = {
            "core_status": "PARTIAL" if all_failures else "AVAILABLE",
            "core_coverage_pct": coverage,
            "core_failed_count": len(all_failures),
            "deep_candidate_limit": context_limit,
            "deep_target_count": len(context_target_ids),
            "deep_enriched_count": context_enriched_count,
            "deep_complete_count": context_complete_count,
            "deep_completeness_pct": round(
                context_complete_count / len(context_target_ids) * 100.0,
                2,
            )
            if context_target_ids
            else 0.0,
            "deep_source_completeness_pct": round(
                sum(
                    source_success.get(source, 0)
                    for source in (
                        "funding",
                        "order_book",
                        "trades",
                        "timing",
                        "open_interest",
                    )
                )
                / (len(context_target_ids) * 5)
                * 100.0,
                2,
            )
            if context_target_ids
            else 0.0,
            "source_success": dict(source_success),
            "source_missing": dict(source_missing),
            "context_failure_count": len(context_failures),
            "scan_duration_seconds": duration,
            "api_metrics": api_metrics,
            "no_fake_fallback": True,
        }
        historical = self.repository.performance()
        early_short = sum(
            item.signal_stage == "EARLY_SIGNAL"
            and item.entry_eligibility.get("status") == "ENTRY_READY"
            for item in short_signals
        )
        ready_short = sum(
            item.entry_eligibility.get("status") == "ENTRY_READY"
            for item in short_signals
        )
        wait_short = sum(
            item.entry_eligibility.get("status") == "WAIT_RETEST"
            for item in short_signals
        )
        missed_short = sum(
            item.entry_eligibility.get("status") == "MISSED_ENTRY"
            for item in short_signals
        )
        message = (
            f"掃描完成：15m 早期可進 {early_short}、目前可進 {ready_short}、"
            f"等待回踩 {wait_short}、已錯過 {missed_short}；長線 {len(long_signals)}。"
            if short_signals or long_signals
            else "掃描完成：目前無新鮮進場訊號；系統未為湊數降低 Trigger 標準。"
        )
        if all_failures:
            message += f" 另有 {len(all_failures)} 個核心資料缺失標的已獨立排除。"

        report = RadarReport(
            status=status,
            generated_at=completed_at,
            scope=scope,
            target_count=len(instruments),
            fetched_count=len(bundles),
            analyzable_count=len({*short_results, *long_results}),
            coverage_pct=coverage,
            target_instruments=target_ids,
            failed_instruments=all_failures,
            signals=short_signals,
            exclusion_counts=dict(exclusion_counts.most_common()),
            duration_seconds=duration,
            message=message,
            market_regime_counts=dict(Counter(item.regime for item in short_states)),
            watchlist=short_watchlist,
            market_map=short_states,
            context_target_count=len(context_target_ids),
            context_enriched_count=context_enriched_count,
            context_failures=dict(sorted(context_failures.items())),
            market_bias=market_bias,
            scan_id=scan_id,
            scan_started_at=scan_started_at,
            completed_at=completed_at,
            runtime_status="FRESH",
            actionable=True,
            max_signals=min(max(self.config.max_signals, 0), 20),
            api_metrics=api_metrics,
            long_signals=long_signals,
            long_watchlist=long_watchlist,
            long_market_map=long_states,
            data_quality=data_quality,
            historical_performance=historical,
        )
        self.repository.record_scan(
            scan_id,
            scan_started_at,
            completed_at,
            status,
            len(instruments),
            report.analyzable_count,
            len(short_signals) + len(long_signals),
            duration,
            data_quality,
        )
        self._progress(progress, "FINALIZING", 1, 1, "最新 V3.3 雙雷達已完成")
        return report

    def _core_preview_report(
        self,
        *,
        scan_id: str,
        scan_started_at: str,
        scope: str,
        started: float,
        instruments: list[Instrument],
        target_ids: list[str],
        bundles: dict[str, dict[str, list[Candle]]],
        failures: dict[str, str],
        analysis_failures: dict[str, str],
        short_results: dict[str, AnalysisResult],
        market_bias: dict[str, Any],
    ) -> RadarReport:
        """Publish closed-candle 15m decisions before optional deep enrichment."""

        exclusion_counts: Counter[str] = Counter()
        generated_at = datetime.now(timezone.utc).isoformat()
        short_states, raw_short_signals = self._collect_results(
            short_results,
            exclusion_counts,
        )
        short_signals = self.repository.reconcile(
            raw_short_signals,
            short_states,
            generated_at,
            "SHORT",
        )
        short_signals = [
            _without_internal_metrics(self._refresh_entry_eligibility(item))
            for item in short_signals
        ]
        short_states = [_without_internal_metrics(item) for item in short_states]
        short_signals = sorted(
            short_signals,
            key=self._signal_sort_key,
            reverse=True,
        )[: min(max(self.config.max_signals, 0), 20)]
        short_watchlist = self._watchlist(short_states)
        short_states.sort(key=lambda item: item.inst_id)
        all_failures = {
            **dict(sorted(failures.items())),
            **dict(sorted(analysis_failures.items())),
        }
        coverage = round(len(bundles) / len(instruments) * 100.0, 4)
        early_count = sum(
            item.signal_stage == "EARLY_SIGNAL"
            and item.entry_eligibility.get("status") == "ENTRY_READY"
            for item in short_signals
        )
        ready_count = sum(
            item.entry_eligibility.get("status") == "ENTRY_READY"
            for item in short_signals
        )
        wait_count = sum(
            item.entry_eligibility.get("status") == "WAIT_RETEST"
            for item in short_signals
        )
        missed_count = sum(
            item.entry_eligibility.get("status") == "MISSED_ENTRY"
            for item in short_signals
        )
        return RadarReport(
            status="CORE_PREVIEW",
            generated_at=generated_at,
            scope=scope,
            target_count=len(instruments),
            fetched_count=len(bundles),
            analyzable_count=len(short_results),
            coverage_pct=coverage,
            target_instruments=target_ids,
            failed_instruments=all_failures,
            signals=short_signals,
            exclusion_counts=dict(exclusion_counts.most_common()),
            duration_seconds=round(time.monotonic() - started, 3),
            message=(
                f"15m 核心結果已先發布：早期可進 {early_count}、目前可進 {ready_count}、"
                f"等待回踩 {wait_count}、已錯過 {missed_count}；"
                "正在補 Funding、OI、Order Book 與長線結果。"
            ),
            market_regime_counts=dict(
                Counter(item.regime for item in short_states)
            ),
            watchlist=short_watchlist,
            market_map=short_states,
            market_bias=market_bias,
            scan_id=scan_id,
            scan_started_at=scan_started_at,
            completed_at="",
            runtime_status="CORE_PREVIEW",
            actionable=True,
            max_signals=min(max(self.config.max_signals, 0), 20),
            data_quality={
                "core_status": "PARTIAL" if all_failures else "AVAILABLE",
                "core_coverage_pct": coverage,
                "deep_status": "PENDING",
                "long_radar_status": "PENDING",
                "preliminary": True,
                "no_fake_fallback": True,
            },
            historical_performance=self.repository.performance(),
        )

    def _analyze_short_v33(
        self,
        instrument: Instrument,
        ticker: Ticker,
        bundle: dict[str, list[Candle]],
    ) -> AnalysisResult:
        previous = self.repository.load_story(instrument.inst_id, "SHORT")
        parameters = inspect.signature(self.engine.analyze).parameters
        kwargs: dict[str, Any] = {}
        if "previous_story" in parameters:
            kwargs["previous_story"] = previous
        return self.engine.analyze(
            instrument,
            ticker,
            bundle["4H"],
            bundle["1H"],
            bundle["15m"],
            **kwargs,
        )

    def _analyze_long_v33(
        self,
        instrument: Instrument,
        ticker: Ticker,
        bundle: dict[str, list[Candle]],
    ) -> AnalysisResult | None:
        analyzer = getattr(self.engine, "analyze_long", None)
        if not callable(analyzer):
            return None
        previous = self.repository.load_story(instrument.inst_id, "LONG")
        parameters = inspect.signature(analyzer).parameters
        kwargs: dict[str, Any] = {}
        if "previous_story" in parameters:
            kwargs["previous_story"] = previous
        return analyzer(
            instrument,
            ticker,
            bundle["1D"],
            bundle["4H"],
            bundle["1H"],
            **kwargs,
        )

    def _attach_oi_snapshot(
        self,
        result: AnalysisResult,
        open_interest_usd: float | None,
        change_pct: float | None,
    ) -> AnalysisResult:
        state = result.market_state
        if state is None:
            return result
        metrics = dict(state.market_metrics)
        metrics.update(
            {
                "open_interest_usd": open_interest_usd,
                "open_interest_change_pct": change_pct,
                "oi_flow_state": self._classify_oi_flow(
                    change_pct,
                    metrics.get("price_change_1h_pct"),
                ),
            }
        )
        data_quality = dict(state.data_quality)
        if open_interest_usd is None:
            missing = list(data_quality.get("missing_sources", []))
            data_quality["missing_sources"] = _unique_strings([*missing, "open_interest"])
        updated_state = replace(
            state,
            market_metrics=metrics,
            data_quality=data_quality,
        )
        updated_signal = (
            replace(
                result.signal,
                market_metrics=metrics,
                data_quality=data_quality,
            )
            if result.signal is not None
            else None
        )
        updated_candidate = (
            replace(
                result.candidate_signal,
                market_metrics=metrics,
                data_quality=data_quality,
            )
            if result.candidate_signal is not None
            else None
        )
        return replace(
            result,
            signal=updated_signal,
            market_state=updated_state,
            candidate_signal=updated_candidate,
        )

    @staticmethod
    def _rank_context_candidates(
        short_results: dict[str, AnalysisResult],
        long_results: dict[str, AnalysisResult],
    ) -> list[str]:
        stage_priority = {
            "EARLY_SIGNAL": 8,
            "REENTRY": 7,
            "NEAR_TRIGGER": 6,
            "CONFIRMED": 5,
            "TRENDING": 4,
            "EXTENDED": 3,
            "NO_FOLLOW_THROUGH": 2,
            "WATCH": 1,
            "FILTERED": 0,
        }
        candidates: dict[str, tuple[Any, ...]] = {}
        for horizon_priority, results in ((1, short_results), (0, long_results)):
            for inst_id, result in results.items():
                state = result.market_state
                if state is None or state.status == "FILTERED":
                    continue
                freshness_priority = {
                    "NEW": 5,
                    "REACTIVATED": 4,
                    "NONE": 3,
                    "ACTIVE": 2,
                    "EXTENDED": 1,
                }.get(state.freshness, 0)
                rank = (
                    freshness_priority,
                    stage_priority.get(state.status, 0),
                    result.signal is not None,
                    state.direction in ("LONG", "SHORT"),
                    horizon_priority,
                    state.readiness_score,
                    state.quote_volume_24h,
                )
                if inst_id not in candidates or rank > candidates[inst_id]:
                    candidates[inst_id] = rank
        return [
            inst_id
            for inst_id, _ in sorted(
                candidates.items(),
                key=lambda item: item[1],
                reverse=True,
            )
        ]

    @staticmethod
    def _mark_deep_data_missing(
        result: AnalysisResult,
        reasons: list[str],
    ) -> AnalysisResult:
        state = result.market_state
        if state is None:
            return result
        data_quality = dict(state.data_quality)
        data_quality.update(
            {
                "deep": "MISSING",
                "missing_sources": _unique_strings(
                    [*data_quality.get("missing_sources", []), *reasons]
                ),
            }
        )
        participation = {
            "state": "DATA_MISSING",
            "label": "資料暫缺",
            "supporting": [],
            "conflicts": [],
            "neutral": ["Deep Data 暫缺；核心 Trigger 保留"],
            "missing_sources": reasons,
            "permission": "CONTEXT_ONLY_NEVER_CANCELS_TRIGGER",
        }
        updated_state = replace(
            state,
            data_quality=data_quality,
            market_participation=participation,
            neutral_evidence=_unique_strings(
                [*state.neutral_evidence, "Deep Data 暫缺；不取消核心 Trigger"]
            ),
            missing_conditions=_unique_strings(
                [*state.missing_conditions, *reasons]
            )[:10],
        )
        updated_signal = (
            replace(
                result.signal,
                data_quality=data_quality,
                market_participation=participation,
                neutral_evidence=_unique_strings(
                    [
                        *result.signal.neutral_evidence,
                        "Deep Data 暫缺；不取消核心 Trigger",
                    ]
                ),
            )
            if result.signal is not None
            else None
        )
        return replace(
            result,
            signal=updated_signal,
            candidate_signal=updated_signal or result.candidate_signal,
            market_state=updated_state,
        )

    def _collect_results(
        self,
        results: dict[str, AnalysisResult],
        exclusion_counts: Counter[str],
    ) -> tuple[list[MarketState], list[Signal]]:
        states: list[MarketState] = []
        signals: list[Signal] = []
        for result in results.values():
            if result.market_state is not None:
                states.append(result.market_state)
            if result.signal is None:
                exclusion_counts[result.reason] += 1
            elif self._passes_output_liquidity(result.signal, False):
                signals.append(result.signal)
            else:
                exclusion_counts["universe_output_gate"] += 1
        return states, signals

    @staticmethod
    def _signal_sort_key(signal: Signal) -> tuple[Any, ...]:
        entry_priority = {
            "ENTRY_READY": 3,
            "WAIT_RETEST": 2,
            "MISSED_ENTRY": 1,
        }
        freshness_priority = {
            "NEW": 8,
            "REACTIVATED": 7,
            "NONE": 6,
            "ACTIVE": 4,
            "NO_FOLLOW_THROUGH": 2,
            "EXTENDED": 1,
            "INVALIDATED": 0,
            "COMPLETED": 0,
        }
        stage_priority = {
            "EARLY_SIGNAL": 8,
            "REENTRY": 7,
            "NEAR_TRIGGER": 6,
            "CONFIRMED": 5,
            "TRENDING": 4,
            "EXTENDED": 2,
            "NO_FOLLOW_THROUGH": 1,
            "INVALIDATED": 0,
        }
        return (
            entry_priority.get(signal.entry_eligibility.get("status"), 0),
            freshness_priority.get(signal.freshness, 0),
            stage_priority.get(signal.signal_stage, 0),
            -int(signal.lifecycle.get("age_bars", 0) or 0),
            signal.execution_quality.get("score", 0.0),
            signal.score,
            signal.quote_volume_24h,
        )

    def _refresh_entry_eligibility(self, signal: Signal) -> Signal:
        metrics = dict(signal.market_metrics)
        story_raw = signal.market_story.get("raw", {})
        story_trigger = signal.market_story.get("trigger", {})
        values = {
            "current_price": _finite_number(metrics.get("last_price")),
            "entry_low": _finite_number(signal.entry_low),
            "entry_high": _finite_number(signal.entry_high),
            "stop": _finite_number(signal.stop_loss),
            "target": _finite_number(signal.take_profit_1),
            "atr": _finite_number(
                story_trigger.get("event_atr") or story_raw.get("core_atr")
            ),
        }
        if any(value is None for value in values.values()):
            fallback = dict(signal.entry_eligibility)
            if not fallback:
                fallback = {
                    "status": "ENTRY_READY" if signal.actionable else "MISSED_ENTRY",
                    "label": "目前可進" if signal.actionable else "目前不可進",
                    "reason": "缺少即時 Entry 距離資料，沿用策略狀態。",
                    "actionable": signal.actionable,
                    "chase_atr": None,
                    "remaining_rr": signal.risk_reward,
                }
            return replace(signal, entry_eligibility=fallback)
        eligibility = _entry_eligibility(
            direction=signal.direction,
            current_price=float(values["current_price"]),
            entry_low=float(values["entry_low"]),
            entry_high=float(values["entry_high"]),
            stop=float(values["stop"]),
            target=float(values["target"]),
            atr=float(values["atr"]),
            stage=signal.signal_stage,
            minimum_rr=self.config.minimum_rr,
            ready_max_chase_atr=self.config.entry_ready_max_chase_atr,
            missed_chase_atr=self.config.entry_missed_chase_atr,
        )
        metrics.update(
            {
                "entry_status": eligibility["status"],
                "entry_chase_atr": eligibility["chase_atr"],
                "remaining_rr": eligibility["remaining_rr"],
            }
        )
        checks = [
            item
            for item in signal.safety_checks
            if item.get("key") != "entry_eligibility"
        ]
        checks.append(
            {
                "key": "entry_eligibility",
                "label": eligibility["label"],
                "passed": bool(eligibility["actionable"]),
                "value": eligibility["chase_atr"],
                "hard": False,
            }
        )
        return replace(
            signal,
            market_metrics=metrics,
            safety_checks=checks,
            entry_eligibility=eligibility,
            actionable=bool(eligibility["actionable"]),
        )

    def _watchlist(self, states: list[MarketState]) -> list[MarketState]:
        selected = [
            item
            for item in states
            if item.status in ("NEAR_TRIGGER", "WATCH", "NO_FOLLOW_THROUGH")
            and item.direction in ("LONG", "SHORT")
            and item.status != "FILTERED"
            and self._passes_output_liquidity(item, False)
        ]
        selected.sort(
            key=lambda item: (
                item.status == "NEAR_TRIGGER",
                item.freshness == "NEW",
                item.readiness_score,
                item.quote_volume_24h,
            ),
            reverse=True,
        )
        return selected[: max(0, self.config.max_watchlist)]

    def _scan_once_v2(
        self,
        progress: ProgressCallback | None = None,
        scan_id: str | None = None,
    ) -> RadarReport:
        started = time.monotonic()
        now = datetime.now(timezone.utc).isoformat()
        scan_id = scan_id or str(uuid.uuid4())
        metrics_reset = getattr(self.client, "reset_metrics", None)
        if callable(metrics_reset):
            metrics_reset()
        self._progress(progress, "INSTRUMENTS", 0, None, "正在取得 OKX live USDT 永續合約")
        scope = "OKX state=live、USDT 結算、線性永續合約"
        try:
            instruments = self.client.get_usdt_swap_instruments()
        except Exception as exc:
            return self._fatal_report(now, scope, started, f"無法取得合約母清單：{exc}", scan_id)
        if not instruments:
            return self._fatal_report(now, scope, started, "OKX 回傳的 live USDT 永續母清單為空。", scan_id)

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
                scan_id=scan_id,
                scan_started_at=now,
                completed_at=datetime.now(timezone.utc).isoformat(),
                runtime_status="ERROR",
                actionable=False,
                max_signals=self.config.max_signals,
            )

        failures: dict[str, str] = {}
        bundles: dict[str, dict[str, list[Candle]]] = {}
        eligible = []
        for instrument in instruments:
            if instrument.inst_id not in tickers:
                failures[instrument.inst_id] = "bulk ticker 缺少此 live 合約"
            else:
                eligible.append(instrument)

        self._progress(
            progress,
            "CANDLES",
            0,
            len(eligible),
            "正在取得 4H／1H／15m 已收盤 K 線",
        )
        completed_bundles = 0
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
                completed_bundles += 1
                self._progress(
                    progress,
                    "CANDLES",
                    completed_bundles,
                    len(eligible),
                    "正在取得全市場多時間框架資料",
                )

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
                scan_id=scan_id,
                scan_started_at=now,
                completed_at=datetime.now(timezone.utc).isoformat(),
                runtime_status="ERROR",
                actionable=False,
                max_signals=self.config.max_signals,
            )

        analysis_results = {}
        analysis_failures: dict[str, str] = {}
        instrument_map = {item.inst_id: item for item in instruments}
        self._progress(progress, "ANALYSIS", 0, len(target_ids), "正在融合市場證據")
        for index, inst_id in enumerate(target_ids, 1):
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
            analysis_results[inst_id] = result
            self._progress(
                progress,
                "ANALYSIS",
                index,
                len(target_ids),
                "正在建立 4H Bias、1H Setup 與 15m Trigger",
            )

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
                exclusion_counts={},
                duration_seconds=round(time.monotonic() - started, 3),
                message="雷達資料已取得，但部分合約分析失敗；為避免部分市場結果，禁止輸出訊號。",
                scan_id=scan_id,
                scan_started_at=now,
                completed_at=datetime.now(timezone.utc).isoformat(),
                runtime_status="ERROR",
                actionable=False,
                max_signals=self.config.max_signals,
            )

        market_bias = self._calculate_market_bias(analysis_results)
        context_failures: dict[str, list[str]] = {}
        context_target_ids: list[str] = []
        context_enriched_count = 0
        context_loader = getattr(self.client, "get_market_context", None)
        context_applier = getattr(self.engine, "apply_market_context", None)
        if callable(context_loader) and callable(context_applier):
            open_interest: dict[str, float] = {}
            open_interest_error: str | None = None
            oi_loader = getattr(self.client, "get_open_interest_usd", None)
            if callable(oi_loader):
                try:
                    open_interest = oi_loader()
                except Exception as exc:
                    open_interest_error = str(exc)
            else:
                open_interest_error = "client does not provide the bulk open-interest endpoint"
            if not open_interest:
                open_interest_error = open_interest_error or "OKX 回傳的 Open Interest 清單為空"
                context_failures["_OPEN_INTEREST_"] = [open_interest_error]
                completed_at = datetime.now(timezone.utc).isoformat()
                states = [
                    result.market_state
                    for result in analysis_results.values()
                    if result.market_state is not None
                ]
                states.sort(key=lambda item: item.inst_id)
                metrics_loader = getattr(self.client, "metrics_snapshot", None)
                api_metrics = metrics_loader() if callable(metrics_loader) else {}
                return RadarReport(
                    status="DATA_INCOMPLETE",
                    generated_at=completed_at,
                    scope=scope,
                    target_count=len(instruments),
                    fetched_count=fetched_count,
                    analyzable_count=len(states),
                    coverage_pct=100.0,
                    target_instruments=target_ids,
                    failed_instruments={"_OPEN_INTEREST_": open_interest_error},
                    signals=[],
                    exclusion_counts={},
                    duration_seconds=round(time.monotonic() - started, 3),
                    message=(
                        "雷達深度資料不完整：無法可靠取得 Open Interest，"
                        "依安全規則禁止輸出進場訊號。"
                    ),
                    market_regime_counts=dict(Counter(item.regime for item in states)),
                    market_map=states,
                    context_failures=context_failures,
                    market_bias=market_bias,
                    scan_id=scan_id,
                    scan_started_at=now,
                    completed_at=completed_at,
                    runtime_status="ERROR",
                    actionable=False,
                    max_signals=min(max(self.config.max_signals, 0), 20),
                    api_metrics=api_metrics,
                )
            if open_interest:
                for inst_id, result in list(analysis_results.items()):
                    state = result.market_state
                    if state is None:
                        continue
                    current_oi = open_interest.get(inst_id)
                    oi_change_pct = self._open_interest_change(inst_id, current_oi)
                    metrics = dict(state.market_metrics)
                    metrics.update(
                        {
                            "open_interest_usd": current_oi,
                            "open_interest_change_pct": oi_change_pct,
                            "oi_flow_state": self._classify_oi_flow(
                                oi_change_pct,
                                metrics.get("price_change_1h_pct"),
                            ),
                        }
                    )
                    filtered = (
                        self.config.min_open_interest_usd > 0
                        and (
                            current_oi is None
                            or current_oi < self.config.min_open_interest_usd
                        )
                    )
                    updated_state = replace(
                        state,
                        status="FILTERED" if filtered else state.status,
                        readiness_score=min(state.readiness_score, 49.0) if filtered else state.readiness_score,
                        missing_conditions=(
                            [
                                "無法取得持倉量，依流動性安全規則淘汰"
                                if current_oi is None
                                else f"持倉量需達 {self.config.min_open_interest_usd:,.0f} USD",
                                *state.missing_conditions,
                            ][:6]
                            if filtered
                            else state.missing_conditions
                        ),
                        market_metrics=metrics,
                    )
                    updated_signal = (
                        replace(result.signal, market_metrics=metrics)
                        if result.signal is not None
                        else None
                    )
                    analysis_results[inst_id] = replace(
                        result,
                        signal=updated_signal,
                        reason="open_interest_too_low" if filtered else result.reason,
                        market_state=updated_state,
                        candidate_signal=(
                            replace(result.candidate_signal, market_metrics=metrics)
                            if result.candidate_signal is not None
                            else None
                        ),
                    )
            ranked_results = [
                (inst_id, result)
                for inst_id, result in analysis_results.items()
                if result.market_state is not None
                and result.market_state.status != "FILTERED"
            ]
            stage_priority = {
                "CONFIRMED": 4,
                "EARLY_SIGNAL": 3,
                "NEAR_TRIGGER": 2,
                "WATCH": 1,
            }
            ranked_results.sort(
                key=lambda item: (
                    item[1].signal is not None,
                    item[1].candidate_plan is not None
                    or item[1].candidate_signal is not None,
                    stage_priority.get(item[1].market_state.status, 0),
                    item[1].market_state.regime != "DISORDER",
                    item[1].market_state.direction != "NEUTRAL",
                    item[1].market_state.readiness_score,
                    item[1].market_state.quote_volume_24h,
                ),
                reverse=True,
            )
            context_target_ids = [
                inst_id
                for inst_id, _ in ranked_results[: max(0, self.config.context_candidates)]
            ]
            contexts: dict[str, MarketContext] = {}
            micro_candles: dict[str, list[Candle]] = {}

            def load_candidate(inst_id: str) -> tuple[MarketContext, list[Candle]]:
                candles_5m = self.client.get_candles(
                    inst_id,
                    "5m",
                    self.config.candle_limit_5m,
                )
                if len(candles_5m) < 60:
                    raise RuntimeError("5m 已收盤 K 線不足 60 根")
                context = context_loader(inst_id, open_interest.get(inst_id))
                return (
                    replace(
                        context,
                        open_interest_change_pct=self._open_interest_change(
                            inst_id,
                            context.open_interest_usd,
                        ),
                    ),
                    candles_5m,
                )

            self._progress(
                progress,
                "CONTEXT",
                0,
                len(context_target_ids),
                "正在取得 5m、Funding、Taker Flow 與 Order Book",
            )
            completed_context = 0
            with ThreadPoolExecutor(max_workers=max(1, min(self.config.workers, 8))) as executor:
                future_map = {
                    executor.submit(load_candidate, inst_id): inst_id
                    for inst_id in context_target_ids
                }
                for future in as_completed(future_map):
                    inst_id = future_map[future]
                    try:
                        context, candles_5m = future.result()
                        contexts[inst_id] = context
                        micro_candles[inst_id] = candles_5m
                        if context.complete and context.execution_quality_complete:
                            context_enriched_count += 1
                        candidate_failures = list(context.failures)
                        if candidate_failures:
                            context_failures[inst_id] = candidate_failures
                    except Exception as exc:
                        context_failures[inst_id] = [str(exc)]
                    completed_context += 1
                    self._progress(
                        progress,
                        "CONTEXT",
                        completed_context,
                        len(context_target_ids),
                        "正在取得深度即時市場資料",
                    )

            btc_result = analysis_results.get("BTC-USDT-SWAP")
            btc_bias = (
                btc_result.market_state.direction
                if btc_result is not None
                and btc_result.market_state is not None
                and btc_result.market_state.regime in ("TREND", "BREAKOUT", "BREAKOUT_READY")
                else "NEUTRAL"
            )
            for inst_id, context in contexts.items():
                try:
                    analysis_results[inst_id] = context_applier(
                        analysis_results[inst_id],
                        context,
                        btc_bias,
                        micro_candles.get(inst_id),
                        market_bias,
                    )
                except Exception as exc:
                    context_failures.setdefault(inst_id, []).append(
                        f"綜合候選判斷失敗：{exc}"
                    )

            for inst_id in context_target_ids:
                if inst_id in contexts and inst_id in micro_candles:
                    continue
                failed_result = analysis_results[inst_id]
                state = failed_result.market_state
                analysis_results[inst_id] = replace(
                    failed_result,
                    signal=None,
                    reason="market_context_unavailable",
                    market_state=(
                        replace(
                            state,
                            status="FILTERED",
                            missing_conditions=_unique_strings(
                                [
                                    "5m／Funding／Order Book／Taker Flow 未完整取得",
                                    *state.missing_conditions,
                                ]
                            )[:8],
                        )
                        if state is not None
                        else None
                    ),
                )

            if open_interest:
                self._previous_open_interest_usd = dict(open_interest)

        exclusion_counts: Counter[str] = Counter()
        signals = []
        market_states: list[MarketState] = []
        context_required = callable(context_loader) and callable(context_applier)
        for result in analysis_results.values():
            if result.market_state is not None:
                market_states.append(result.market_state)
            if result.signal is None:
                exclusion_counts[result.reason] += 1
            elif self._passes_output_liquidity(result.signal, context_required):
                signals.append(result.signal)
            else:
                exclusion_counts["output_liquidity_gate"] += 1

        signals.sort(key=lambda item: (item.score, item.quote_volume_24h), reverse=True)
        signals = signals[: min(max(self.config.max_signals, 0), 20)]
        completed_at = datetime.now(timezone.utc).isoformat()
        signals = self._apply_lifecycle(signals, completed_at)
        early_count = sum(item.signal_stage == "EARLY_SIGNAL" for item in signals)
        confirmed_count = len(signals) - early_count
        watchlist = [
            item
            for item in market_states
            if item.status in ("NEAR_TRIGGER", "WATCH")
            and item.regime != "DISORDER"
            and item.direction != "NEUTRAL"
            and item.readiness_score >= 50.0
            and self._passes_output_liquidity(item, False)
        ]
        watchlist.sort(
            key=lambda item: (item.readiness_score, item.quote_volume_24h),
            reverse=True,
        )
        watchlist = watchlist[: max(0, self.config.max_watchlist)]
        market_states.sort(key=lambda item: item.inst_id)
        regime_counts = Counter(item.regime for item in market_states)
        status = "SIGNALS_FOUND" if signals else "NO_QUALIFIED_SIGNAL"
        message = (
            f"完整掃描完成：{early_count} 個早期訊號、{confirmed_count} 個完整確認，另列出 {len(watchlist)} 個接近觸發候選。"
            if signals
            else f"完整掃描完成：本輪 0 個進場訊號；另列出 {len(watchlist)} 個接近觸發候選，不代表可以直接進場。"
        )
        self._progress(progress, "FINALIZING", 1, 1, "正在建立最新雷達結果")
        metrics_loader = getattr(self.client, "metrics_snapshot", None)
        api_metrics = metrics_loader() if callable(metrics_loader) else {}
        return RadarReport(
            status=status,
            generated_at=completed_at,
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
            context_target_count=len(context_target_ids),
            context_enriched_count=context_enriched_count,
            context_failures=dict(sorted(context_failures.items())),
            market_bias=market_bias,
            scan_id=scan_id,
            scan_started_at=now,
            completed_at=completed_at,
            runtime_status="FRESH",
            actionable=True,
            max_signals=min(max(self.config.max_signals, 0), 20),
            api_metrics=api_metrics,
        )

    def _passes_output_liquidity(
        self,
        item: Signal | MarketState,
        require_context: bool = False,
    ) -> bool:
        del require_context
        if item.quote_volume_24h < self.config.min_quote_volume_24h:
            return False
        if item.spread_pct > self.config.universe_max_spread_pct:
            return False
        return True

    def _calculate_market_bias(self, results: dict[str, object]) -> dict[str, object]:
        states = [
            result.market_state
            for result in results.values()
            if getattr(result, "market_state", None) is not None
        ]
        directional = [
            state
            for state in states
            if state.status != "FILTERED"
            and state.regime in ("TREND", "BREAKOUT_READY", "BREAKOUT")
            and state.direction in ("LONG", "SHORT")
        ]

        def breadth(items: list[MarketState]) -> tuple[float, int, int]:
            long_count = sum(item.direction == "LONG" for item in items)
            short_count = sum(item.direction == "SHORT" for item in items)
            total = long_count + short_count
            return (
                round(long_count / total * 100.0, 1) if total else 50.0,
                long_count,
                short_count,
            )

        breadth_score, long_count, short_count = breadth(directional)
        liquid = sorted(
            (
                state
                for state in directional
                if state.quote_volume_24h >= self.config.min_quote_volume_24h
                and state.spread_pct <= self.config.max_spread_pct
            ),
            key=lambda item: item.quote_volume_24h,
            reverse=True,
        )[:50]
        liquid_score, _, _ = breadth(liquid)

        def anchor_score(inst_id: str) -> float:
            result = results.get(inst_id)
            state = getattr(result, "market_state", None)
            if (
                state is None
                or state.status == "FILTERED"
                or state.regime == "DISORDER"
                or state.direction not in ("LONG", "SHORT")
            ):
                return 50.0
            confidence = max(0.0, min(100.0, state.readiness_score)) / 100.0
            offset = 25.0 + (confidence * 25.0)
            return round(50.0 + offset if state.direction == "LONG" else 50.0 - offset, 1)

        btc_score = anchor_score("BTC-USDT-SWAP")
        eth_score = anchor_score("ETH-USDT-SWAP")
        score = round(
            (breadth_score * 0.35)
            + (liquid_score * 0.25)
            + (btc_score * 0.25)
            + (eth_score * 0.15),
            1,
        )
        label = "偏多" if score >= 65.0 else "偏空" if score <= 35.0 else "中性"
        return {
            "score": score,
            "label": label,
            "market_breadth_long_pct": breadth_score,
            "liquid_breadth_long_pct": liquid_score,
            "btc_score": btc_score,
            "eth_score": eth_score,
            "long_count": long_count,
            "short_count": short_count,
            "sample_count": len(directional),
        }

    def _fetch_bundle(
        self,
        inst_id: str,
        bars: tuple[str, ...] | None = None,
    ) -> dict[str, list[Candle]]:
        output: dict[str, list[Candle]] = {}
        for bar in bars or self.bars:
            try:
                output[bar] = self._cached_or_fresh_candles(inst_id, bar)
            except Exception as exc:
                raise RuntimeError(f"{bar} K 線取得失敗：{exc}") from exc
        return output

    def _cached_or_fresh_candles(
        self,
        inst_id: str,
        bar: str,
    ) -> list[Candle]:
        cache_key = (inst_id, bar)
        if bar in self._bar_interval_ms:
            with self._candle_cache_lock:
                cached = self._candle_cache.get(cache_key)
            if cached is not None and self._cache_covers_current_bar(cached, bar):
                return list(cached)

        candles = self.client.get_candles(
            inst_id,
            bar,
            self._bar_limit(bar),
        )
        if bar in self._bar_interval_ms and len(candles) >= 60:
            with self._candle_cache_lock:
                self._candle_cache[cache_key] = list(candles)
        return candles

    def _cache_covers_current_bar(
        self,
        candles: list[Candle],
        bar: str,
    ) -> bool:
        """Reuse HTF data only until the next candle should have closed."""

        interval_ms = self._bar_interval_ms.get(bar)
        if not candles or interval_ms is None or not candles[-1].confirmed:
            return False
        last_open_ts = int(candles[-1].ts)
        if last_open_ts < 1_000_000_000_000:
            return False
        return int(time.time() * 1000) < last_open_ts + (interval_ms * 2)

    def _bar_limit(self, bar: str) -> int:
        return {
            "1D": self.config.candle_limit_1d,
            "4H": self.config.candle_limit_4h,
            "1H": self.config.candle_limit_1h,
            "15m": self.config.candle_limit_15m,
            "5m": self.config.candle_limit_5m,
        }.get(bar, self.config.candle_limit)

    def _open_interest_change(
        self,
        inst_id: str,
        current_open_interest_usd: float | None,
    ) -> float | None:
        previous = self._previous_open_interest_usd.get(inst_id)
        if (
            current_open_interest_usd is None
            or previous is None
            or previous <= 0
        ):
            return None
        return round(
            (current_open_interest_usd - previous) / previous * 100.0,
            3,
        )

    @staticmethod
    def _classify_oi_flow(
        open_interest_change_pct: float | None,
        price_change_pct: object,
    ) -> str | None:
        if open_interest_change_pct is None or not isinstance(
            price_change_pct,
            (int, float),
        ):
            return None
        if open_interest_change_pct >= 0.5:
            return "LONG_BUILD" if price_change_pct >= 0 else "SHORT_BUILD"
        if open_interest_change_pct <= -0.8:
            return "SHORT_COVER" if price_change_pct >= 0 else "LONG_EXIT"
        return "STABLE"

    def _apply_lifecycle(
        self,
        signals: list[Signal],
        completed_at: str,
    ) -> list[Signal]:
        stage_order = {"EARLY_SIGNAL": 1, "CONFIRMED": 2}
        output: list[Signal] = []
        for signal in signals:
            key = (signal.inst_id, signal.direction)
            previous = self._signal_history.get(key)
            previous_stage = previous.get("stage") if previous else None
            if previous_stage is None:
                transition = "NEW"
                first_seen = completed_at
            else:
                prior_rank = stage_order.get(previous_stage, 0)
                current_rank = stage_order.get(signal.signal_stage, 0)
                transition = (
                    "UPGRADED"
                    if current_rank > prior_rank
                    else "DOWNGRADED"
                    if current_rank < prior_rank
                    else "UNCHANGED"
                )
                first_seen = previous.get("first_seen_at", completed_at)
            lifecycle = {
                "first_seen_at": first_seen,
                "last_seen_at": completed_at,
                "previous_stage": previous_stage,
                "current_stage": signal.signal_stage,
                "transition": transition,
            }
            self._signal_history[key] = {
                "stage": signal.signal_stage,
                "first_seen_at": first_seen,
                "last_seen_at": completed_at,
            }
            output.append(replace(signal, lifecycle=lifecycle))
        return output

    @staticmethod
    def _progress(
        callback: ProgressCallback | None,
        phase: str,
        completed: int | None,
        total: int | None,
        message: str,
    ) -> None:
        if callback is None:
            return
        try:
            callback(phase, completed, total, message)
        except Exception:
            return

    def _fatal_report(
        self,
        now: str,
        scope: str,
        started: float,
        message: str,
        scan_id: str,
    ) -> RadarReport:
        completed_at = datetime.now(timezone.utc).isoformat()
        return RadarReport(
            status="DATA_INCOMPLETE",
            generated_at=completed_at,
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
            scan_id=scan_id,
            scan_started_at=now,
            completed_at=completed_at,
            runtime_status="ERROR",
            actionable=False,
            max_signals=min(max(self.config.max_signals, 0), 20),
        )


def _without_internal_metrics(item: Signal | MarketState) -> Signal | MarketState:
    metrics = {
        key: value
        for key, value in item.market_metrics.items()
        if not key.startswith("_")
    }
    return replace(item, market_metrics=metrics)


def _finite_number(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
