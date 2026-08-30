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

from .context import (
    active_sessions,
    build_interpretation,
    build_market_context,
    classify_market_driver,
    detect_anomaly,
    summarize_flow_history,
)
from .decision import build_decision_context
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

    def get_usdt_swap_instrument(self, inst_id: str) -> Instrument | None: ...

    def get_swap_tickers(self) -> dict[str, Ticker]: ...

    def get_ticker(self, inst_id: str) -> Ticker: ...

    def get_candles(self, inst_id: str, bar: str, limit: int = 100) -> list[Candle]: ...

    def get_open_interest_usd(self) -> dict[str, float]: ...

    def get_open_interest_for(self, inst_id: str) -> float | None: ...

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
    max_execution_cost_to_risk_pct: float = 15.0
    max_entry_extension_atr: float = 0.80
    severe_entry_extension_atr: float = 1.80
    max_slippage_pct: float = 0.15
    early_signal_max_age_bars: int = 2
    entry_ready_max_chase_atr: float = 0.15
    entry_missed_chase_atr: float = 0.50
    previous_open_interest_usd: dict[str, float] = field(default_factory=dict)
    state_db_path: str = ":memory:"
    short_stop_floor_atr: float = 1.60
    long_stop_floor_atr: float = 1.80
    short_stop_floor_pct: float = 0.45
    long_stop_floor_pct: float = 0.90


@dataclass(frozen=True)
class SingleInstrumentReanalysis:
    previous_signal: Signal
    ticker: Ticker
    context: MarketContext
    market_state: MarketState | None
    raw_signal: Signal | None
    analyzed_at: str
    reason: str


@dataclass(frozen=True)
class SingleInstrumentScan:
    inst_id: str
    ticker: Ticker
    context: MarketContext
    short_result: AnalysisResult | None
    long_result: AnalysisResult | None
    analyzed_at: str
    errors: list[str]


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
                short_stop_floor_atr=self.config.short_stop_floor_atr,
                long_stop_floor_atr=self.config.long_stop_floor_atr,
                short_stop_floor_pct=self.config.short_stop_floor_pct,
                long_stop_floor_pct=self.config.long_stop_floor_pct,
            )
        )

    def scan_once(
        self,
        progress: ProgressCallback | None = None,
        scan_id: str | None = None,
        preview: PreviewCallback | None = None,
        scan_mode: str = "FULL",
    ) -> RadarReport:
        """Run the V3.4 two-radar pipeline without fake fallback values."""

        normalized_mode = str(scan_mode or "FULL").strip().upper()
        if normalized_mode not in {"SHORT", "LONG", "FULL"}:
            raise ValueError("scan_mode must be SHORT, LONG, or FULL")
        include_short = normalized_mode in {"SHORT", "FULL"}
        include_long = normalized_mode in {"LONG", "FULL"}
        started = time.monotonic()
        scan_started_at = datetime.now(timezone.utc).isoformat()
        scan_id = scan_id or str(uuid.uuid4())
        reset_metrics = getattr(self.client, "reset_metrics", None)
        if callable(reset_metrics):
            reset_metrics()
        mode_label = {
            "SHORT": "15m 短線",
            "LONG": "4H 長線",
            "FULL": "15m＋4H 全市場",
        }[normalized_mode]
        scope = f"OKX state=live、USDT 結算、線性永續合約；{mode_label}掃描"
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
                normalized_mode,
            )
        if not instruments:
            return self._fatal_report(
                scan_started_at,
                scope,
                started,
                "OKX 回傳的 live USDT 永續母清單為空。",
                scan_id,
                normalized_mode,
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
                normalized_mode,
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

        required_bars = self.short_bars if include_short else ("1D", "4H", "1H")
        self._progress(
            progress,
            "CANDLES",
            0,
            len(eligible),
            (
                "正在取得 4H／1H／15m 已收盤 K 線"
                if include_short
                else "正在取得 1D／4H／1H 已收盤 K 線"
            ),
        )
        bundles: dict[str, dict[str, list[Candle]]] = {}
        with ThreadPoolExecutor(max_workers=max(1, self.config.workers)) as executor:
            future_map = {
                executor.submit(
                    self._fetch_bundle,
                    item.inst_id,
                    required_bars,
                ): item.inst_id
                for item in eligible
            }
            for completed, future in enumerate(as_completed(future_map), 1):
                inst_id = future_map[future]
                try:
                    bundle = future.result()
                    if not all(
                        len(bundle.get(bar, [])) >= 60
                        for bar in required_bars
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
                scan_mode=normalized_mode,
            )

        short_results: dict[str, AnalysisResult] = {}
        analysis_failures: dict[str, str] = {}
        if include_short:
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

        market_bias = (
            self._calculate_market_bias(short_results)
            if include_short
            else {}
        )
        if include_short and preview is not None:
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
        long_radar_supported = callable(getattr(self.engine, "analyze_long", None))
        long_radar_enabled = include_long and long_radar_supported
        if long_radar_enabled:
            if include_short:
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
                    (
                        "15m 已發布；長線 4H Trigger 分析中"
                        if include_short
                        else "長線 4H Trigger 分析中"
                    ),
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
                scan_mode=normalized_mode,
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
        horizon_market_bias = {
            "SHORT": market_bias,
            "LONG": (
                self._calculate_market_bias(long_results)
                if include_long
                else {}
            ),
        }

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
                (
                    "正在取得 5m、Funding、Taker、CVD 與 Order Book"
                    if include_short
                    else "正在取得 Funding、Taker、CVD 與 Order Book"
                ),
            )

            def load_context(inst_id: str) -> tuple[MarketContext, list[Candle], list[str]]:
                local_errors: list[str] = []
                timing: list[Candle] = list(bundles[inst_id].get("1H", []))
                if include_short:
                    timing = []
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

            btc_result = short_results.get("BTC-USDT-SWAP") or long_results.get(
                "BTC-USDT-SWAP"
            )
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
                for horizon, results, timing in (
                    ("SHORT", short_results, micro_candles.get(inst_id)),
                    ("LONG", long_results, bundles[inst_id]["1H"]),
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
                        updated_result = context_applier(
                            result,
                            directional_context,
                            btc_bias,
                            timing,
                            horizon_market_bias[horizon],
                        )
                        results[inst_id] = self._apply_professional_context(
                            updated_result,
                            directional_context,
                            previous_micro,
                            horizon_market_bias[horizon],
                            tickers[inst_id].last,
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
        batch_reconciler = getattr(self.repository, "reconcile_batch", None)
        if normalized_mode == "FULL":
            if not callable(batch_reconciler):
                raise RuntimeError(
                    "FULL scan repository must support atomic reconciliation"
                )
            # Both horizons belong to this completed market scan. Persist them
            # together so a LONG failure cannot leave SHORT half-committed (or
            # vice versa).
            reconciled = batch_reconciler(
                {
                    "SHORT": (raw_short_signals, short_states),
                    "LONG": (raw_long_signals, long_states),
                },
                completed_at,
            )
            short_signals = reconciled.get("SHORT", [])
            long_signals = reconciled.get("LONG", [])
        else:
            short_signals = (
                self.repository.reconcile(
                    raw_short_signals,
                    short_states,
                    completed_at,
                    "SHORT",
                )
                if include_short
                else []
            )
            long_signals = (
                self.repository.reconcile(
                    raw_long_signals,
                    long_states,
                    completed_at,
                    "LONG",
                )
                if include_long
                else []
            )
        short_signals = [
            self._attach_decision_context(self._refresh_entry_eligibility(item))
            for item in short_signals
        ]
        long_signals = [
            self._attach_decision_context(self._refresh_entry_eligibility(item))
            for item in long_signals
        ]
        short_states = [self._attach_decision_context(item) for item in short_states]
        long_states = [self._attach_decision_context(item) for item in long_states]
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
        short_market_map = [_compact_market_map_state(item) for item in short_states]
        long_market_map = [_compact_market_map_state(item) for item in long_states]

        api_metrics_loader = getattr(self.client, "metrics_snapshot", None)
        api_metrics = api_metrics_loader() if callable(api_metrics_loader) else {}
        duration = round(time.monotonic() - started, 3)
        short_analysis_failures = {
            key: value
            for key, value in analysis_failures.items()
            if key.endswith(":SHORT")
        }
        long_failures = {
            key: value
            for key, value in analysis_failures.items()
            if key.endswith(":LONG")
        }
        uncategorized_analysis_failures = {
            key: value
            for key, value in analysis_failures.items()
            if key not in short_analysis_failures and key not in long_failures
        }
        core_failures = (
            {
                **dict(sorted(failures.items())),
                **dict(sorted(short_analysis_failures.items())),
                **dict(sorted(uncategorized_analysis_failures.items())),
            }
            if include_short
            else {}
        )
        if include_long and not include_short:
            long_failures = {
                **dict(sorted(failures.items())),
                **dict(sorted(long_failures.items())),
                **dict(sorted(uncategorized_analysis_failures.items())),
            }
        all_failures = {
            **core_failures,
            **dict(sorted(long_failures.items())),
        }
        coverage = round(len(bundles) / len(instruments) * 100.0, 4)
        long_coverage = (
            round(len(long_results) / len(instruments) * 100.0, 4)
            if instruments and long_radar_enabled
            else 0.0
        )
        status = (
            "PARTIAL_DATA"
            if all_failures
            else "SIGNALS_FOUND"
            if short_signals or long_signals
            else "NO_QUALIFIED_SIGNAL"
        )
        data_quality = {
            "core_status": (
                "PARTIAL"
                if core_failures
                else "AVAILABLE"
                if include_short
                else "NOT_SCANNED"
            ),
            "core_coverage_pct": coverage if include_short else 0.0,
            "core_failed_count": len(core_failures),
            "long_status": (
                "PARTIAL"
                if long_failures
                else "AVAILABLE"
                if long_radar_enabled
                else "NOT_SCANNED"
                if not include_long
                else "NOT_SUPPORTED"
            ),
            "long_coverage_pct": long_coverage,
            "long_target_count": len(instruments) if long_radar_enabled else 0,
            "long_analyzable_count": len(long_results),
            "long_failed_count": len(long_failures),
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
        if normalized_mode == "SHORT":
            message = (
                f"15m 掃描完成：早期可進 {early_short}、目前可進 {ready_short}、"
                f"等待回踩 {wait_short}、已錯過 {missed_short}。"
                if short_signals
                else "15m 掃描完成：目前無新鮮進場訊號；系統未降低 Trigger 標準。"
            )
        elif normalized_mode == "LONG":
            message = (
                f"4H 掃描完成：正式長線訊號 {len(long_signals)}。"
                if long_signals
                else "4H 掃描完成：目前無新鮮長線進場訊號；系統未降低 Trigger 標準。"
            )
        else:
            message = (
                f"全市場掃描完成：15m 早期可進 {early_short}、目前可進 {ready_short}、"
                f"等待回踩 {wait_short}、已錯過 {missed_short}；4H 訊號 {len(long_signals)}。"
                if short_signals or long_signals
                else "全市場掃描完成：目前無新鮮進場訊號；系統未降低 Trigger 標準。"
            )
        if core_failures:
            message += (
                f" 另有 {len(core_failures)} 個短線核心資料不足標的已獨立排除。"
            )
        if long_failures:
            message += f" 另有 {len(long_failures)} 個長線資料不足標的。"
            if include_short:
                message += "不影響其短線判定。"

        closed_signals: list[Signal] = []
        long_closed_signals: list[Signal] = []
        terminal_loader = getattr(
            self.repository,
            "recent_terminal_signals",
            None,
        )
        if callable(terminal_loader):
            reference_time = datetime.fromisoformat(completed_at)
            if include_short:
                closed_signals = terminal_loader(
                    "SHORT",
                    as_of=reference_time,
                )
            if include_long:
                long_closed_signals = terminal_loader(
                    "LONG",
                    as_of=reference_time,
                )

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
            market_map=short_market_map,
            context_target_count=len(context_target_ids),
            context_enriched_count=context_enriched_count,
            context_failures=dict(sorted(context_failures.items())),
            market_bias=market_bias,
            long_market_bias=horizon_market_bias["LONG"],
            scan_id=scan_id,
            scan_started_at=scan_started_at,
            completed_at=completed_at,
            runtime_status="FRESH",
            actionable=True,
            max_signals=min(max(self.config.max_signals, 0), 20),
            api_metrics=api_metrics,
            closed_signals=closed_signals,
            long_signals=long_signals,
            long_closed_signals=long_closed_signals,
            long_watchlist=long_watchlist,
            long_market_map=long_market_map,
            data_quality=data_quality,
            historical_performance=historical,
            scan_mode=normalized_mode,
            short_completed_at=completed_at if include_short else "",
            long_completed_at=completed_at if include_long else "",
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
        self._progress(
            progress,
            "FINALIZING",
            None,
            None,
            {
                "SHORT": "15m 分析完成，正在保存最新短線報告",
                "LONG": "4H 分析完成，正在保存最新長線報告",
                "FULL": "分析完成，正在保存並發布最新雙雷達報告",
            }[normalized_mode],
        )
        return report

    def _single_scan_call(
        self,
        function: Callable[..., Any],
        *args: Any,
        _retry_limit: int = 1,
        _timeout_limit: float = 6.0,
    ) -> Any:
        """Call one OKX method with a bounded on-demand request budget.

        The web endpoint already retries the complete transaction once. Four
        retries of up to twelve seconds inside every individual OKX request
        made a single card refresh exceed common mobile-network wait windows.
        Real client methods opt into this shorter budget; test/compatibility
        clients keep their existing signatures and behavior.
        """

        try:
            parameters = inspect.signature(function).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        options: dict[str, Any] = {}
        if "request_retries" in parameters or accepts_kwargs:
            options["request_retries"] = min(
                max(int(getattr(self.client, "retries", 1)), 0),
                max(int(_retry_limit), 0),
            )
        if "request_timeout_seconds" in parameters or accepts_kwargs:
            options["request_timeout_seconds"] = min(
                max(float(getattr(self.client, "timeout_seconds", 6.0)), 1.0),
                max(float(_timeout_limit), 1.0),
            )
        return function(*args, **options)

    def _single_scan_candles(self, inst_id: str, bar: str) -> list[Candle]:
        cache_key = (inst_id, bar)
        if bar in self._bar_interval_ms:
            with self._candle_cache_lock:
                cached = self._candle_cache.get(cache_key)
            if cached is not None and self._cache_covers_current_bar(cached, bar):
                return list(cached)
        candles = self._single_scan_call(
            self.client.get_candles,
            inst_id,
            bar,
            self._bar_limit(bar),
        )
        if bar in self._bar_interval_ms and len(candles) >= 60:
            with self._candle_cache_lock:
                self._candle_cache[cache_key] = list(candles)
        return candles

    def scan_instrument(
        self,
        inst_id: str,
        market_bias: dict[str, Any] | None = None,
        long_market_bias: dict[str, Any] | None = None,
        btc_bias: str = "NEUTRAL",
        long_btc_bias: str = "NEUTRAL",
        requested_horizon: str = "BOTH",
        direction_lock: str | None = None,
    ) -> SingleInstrumentScan:
        """Analyze one explicitly requested symbol without scanning the universe.

        Results are returned to the requesting page only and are not inserted
        into the published market report.  The requested symbol is reconciled
        through the same durable Signal Episode repository as a market scan so
        refreshes cannot manufacture a second Trigger.
        """

        inst_id = str(inst_id or "").strip().upper()
        if not inst_id.endswith("-USDT-SWAP"):
            raise ValueError("幣種格式不正確")
        requested_horizon = str(requested_horizon or "BOTH").strip().upper()
        requested_horizon = {
            "15M": "SHORT",
            "4H": "LONG",
            "FULL": "BOTH",
            "ALL": "BOTH",
        }.get(requested_horizon, requested_horizon)
        if requested_horizon not in {"SHORT", "LONG", "BOTH"}:
            raise ValueError("單幣掃描週期必須是 SHORT、LONG 或 BOTH")
        direction_lock = str(direction_lock or "").strip().upper() or None
        if direction_lock not in {None, "LONG", "SHORT"}:
            raise ValueError("卡片方向鎖定必須是 LONG 或 SHORT")
        include_short = requested_horizon in {"SHORT", "BOTH"}
        include_long = requested_horizon in {"LONG", "BOTH"}

        instrument_loader = getattr(self.client, "get_usdt_swap_instrument", None)
        def load_instrument() -> Instrument | None:
            if callable(instrument_loader):
                return self._single_scan_call(instrument_loader, inst_id)
            return next(
                (
                    item
                    for item in self.client.get_usdt_swap_instruments()
                    if item.inst_id == inst_id
                ),
                None,
            )

        ticker_loader = getattr(self.client, "get_ticker", None)
        requested_bars = (
            ("1D", "4H", "1H")
            if include_long and not include_short
            else self.short_bars
        )
        bars_to_fetch = list(requested_bars)
        if include_long and include_short and "1D" not in bars_to_fetch:
            bars_to_fetch.append("1D")
        if include_short:
            bars_to_fetch.append("5m")

        oi_loader = getattr(self.client, "get_open_interest_for", None)
        bulk_oi_loader = getattr(self.client, "get_open_interest_usd", None)
        context_loader = getattr(self.client, "get_market_context", None)
        context_applier = getattr(self.engine, "apply_market_context", None)

        def load_ticker() -> Ticker:
            if callable(ticker_loader):
                return self._single_scan_call(ticker_loader, inst_id)
            ticker_value = self.client.get_swap_tickers().get(inst_id)
            if ticker_value is None:
                raise ValueError("OKX 最新 Ticker 中找不到這個幣種")
            return ticker_value

        def load_open_interest() -> float | None:
            if callable(oi_loader):
                return self._single_scan_call(oi_loader, inst_id)
            if callable(bulk_oi_loader):
                return bulk_oi_loader().get(inst_id)
            return None

        errors: list[str] = []
        bundle: dict[str, list[Candle]] = {}
        bar_errors: dict[str, Exception] = {}
        open_interest_usd: float | None = None
        timing: list[Candle] = []
        loaded_context: MarketContext | None = None
        task_count = 3 + len(bars_to_fetch) + int(callable(context_loader))
        with ThreadPoolExecutor(max_workers=max(1, min(8, task_count))) as executor:
            instrument_future = executor.submit(load_instrument)
            ticker_future = executor.submit(load_ticker)
            oi_future = executor.submit(load_open_interest)
            bar_futures = {
                bar: executor.submit(self._single_scan_candles, inst_id, bar)
                for bar in bars_to_fetch
            }

            def load_context() -> MarketContext:
                try:
                    oi_value = oi_future.result()
                except Exception:
                    oi_value = None
                # Targeted metadata populates contract sizing used by depth
                # and slippage calculations. Wait for it here while all K-line
                # and ticker calls continue in parallel.
                instrument_future.result()
                # Funding, book and trades are useful enrichment, but they are
                # optional and fetched serially inside get_market_context.
                # Give them one short attempt each so an unhealthy Deep Data
                # route cannot hold a mobile card refresh open for a minute.
                return self._single_scan_call(
                    context_loader,
                    inst_id,
                    oi_value,
                    _retry_limit=0,
                    _timeout_limit=4.0,
                )

            context_future = (
                executor.submit(load_context)
                if callable(context_loader) and callable(context_applier)
                else None
            )
            instrument = instrument_future.result()
            if instrument is None:
                raise ValueError("OKX 最新 live USDT 永續清單中找不到這個幣種")
            ticker = ticker_future.result()
            for bar, future in bar_futures.items():
                try:
                    candles = future.result()
                except Exception as exc:
                    bar_errors[bar] = exc
                    continue
                if bar == "5m":
                    timing = candles
                else:
                    bundle[bar] = candles
            try:
                open_interest_usd = oi_future.result()
            except Exception as exc:
                errors.append(f"Open Interest：{exc}")
            if context_future is not None:
                try:
                    loaded_context = context_future.result()
                except Exception as exc:
                    errors.append(f"Context 整合失敗：{exc}")

        failed_core_bars = [bar for bar in requested_bars if bar in bar_errors]
        if failed_core_bars:
            bar = failed_core_bars[0]
            raise RuntimeError(f"{bar} K 線取得失敗：{bar_errors[bar]}") from bar_errors[bar]
        missing_core_bars = [
            bar for bar in requested_bars if len(bundle.get(bar, [])) < 60
        ]
        if missing_core_bars:
            raise ValueError(
                f"OKX 回傳的 {'、'.join(missing_core_bars)} 已收盤 K 線少於 60 根。"
                "這通常是新上幣歷史不足或 OKX 暫時缺資料，不是訊號失效；"
                "資料補齊前暫不判斷是否進場。"
            )
        short_result = (
            self._analyze_short_v33(instrument, ticker, bundle)
            if include_short
            else None
        )

        long_result: AnalysisResult | None = None
        if include_long and callable(getattr(self.engine, "analyze_long", None)):
            try:
                if "1D" in bar_errors:
                    raise RuntimeError(
                        f"1D K 線取得失敗：{bar_errors['1D']}"
                    ) from bar_errors["1D"]
                if len(bundle.get("1D", [])) < 60:
                    raise ValueError(
                        "OKX 回傳的 1D 已收盤 K 線少於 60 根；"
                        "只會暫停 4H 長線判定，不代表幣種或短線訊號失效。"
                    )
                long_result = self._analyze_long_v33(instrument, ticker, bundle)
            except Exception as exc:
                errors.append(f"長線資料：{exc}")

        oi_change = self._open_interest_change(inst_id, open_interest_usd)
        if short_result is not None:
            short_result = self._attach_oi_snapshot(
                short_result,
                open_interest_usd,
                oi_change,
            )
        if long_result is not None:
            long_result = self._attach_oi_snapshot(
                long_result,
                open_interest_usd,
                oi_change,
            )

        if include_short:
            if "5m" in bar_errors:
                errors.append(f"5m：{bar_errors['5m']}")
                timing = []
            elif len(timing) < 60:
                errors.append("5m 已收盤 K 線不足 60 根")
                timing = []

        context = MarketContext(
            inst_id=inst_id,
            open_interest_usd=open_interest_usd,
            funding_rate=None,
            order_book_imbalance=None,
            taker_buy_ratio=None,
            sampled_at=int(ticker.ts or 0),
            failures=list(errors),
            best_bid=ticker.bid,
            best_ask=ticker.ask,
        )
        if callable(context_loader) and callable(context_applier):
            try:
                if loaded_context is None:
                    raise RuntimeError("Deep Data 端點未完成")
                errors = list(dict.fromkeys([*errors, *loaded_context.failures]))
                context = replace(
                    loaded_context,
                    open_interest_usd=open_interest_usd,
                    open_interest_change_pct=oi_change,
                    failures=list(errors),
                )
                previous_micro = self.repository.load_microstructure(inst_id)
                for result_name, result, result_timing in (
                    ("short_result", short_result, timing or None),
                    ("long_result", long_result, bundle.get("1H")),
                ):
                    if result is None or result.market_state is None:
                        continue
                    direction = result.market_state.direction
                    sequence = (
                        classify_microstructure(
                            previous_micro,
                            context,
                            direction,
                            result.market_state.market_metrics.get(
                                "price_change_core_pct"
                            ),
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
                    result_market_bias = (
                        dict(long_market_bias or {})
                        if result_name == "long_result"
                        else dict(market_bias or {})
                    )
                    result_btc_bias = (
                        long_btc_bias
                        if result_name == "long_result"
                        else btc_bias
                    )
                    updated = context_applier(
                        result,
                        directional_context,
                        result_btc_bias,
                        result_timing,
                        result_market_bias,
                    )
                    updated = self._apply_professional_context(
                        updated,
                        directional_context,
                        previous_micro,
                        result_market_bias,
                        ticker.last,
                    )
                    if result_name == "short_result":
                        short_result = updated
                    else:
                        long_result = updated
                self.repository.save_microstructure(
                    context,
                    datetime.now(timezone.utc).isoformat(),
                )
            except Exception as exc:
                errors.append(f"Context 整合失敗：{exc}")
                if short_result is not None:
                    short_result = self._mark_deep_data_missing(short_result, errors)
                if long_result is not None:
                    long_result = self._mark_deep_data_missing(long_result, errors)
                context = replace(context, failures=list(errors))
        else:
            errors.append("Deep Data 單幣服務暫不可用")
            if short_result is not None:
                short_result = self._mark_deep_data_missing(short_result, errors)
            if long_result is not None:
                long_result = self._mark_deep_data_missing(long_result, errors)
            context = replace(context, failures=list(errors))

        if open_interest_usd is not None:
            self._previous_open_interest_usd[inst_id] = open_interest_usd

        analyzed_at = datetime.now(timezone.utc).isoformat()

        def finalize(
            result: AnalysisResult | None,
            horizon: str,
        ) -> AnalysisResult | None:
            if result is None:
                return None
            signal = result.signal
            reason = result.reason
            if signal is not None and not self._passes_output_liquidity(signal, False):
                signal = None
                reason = "universe_output_gate"
            if (
                signal is not None
                and direction_lock is not None
                and signal.direction != direction_lock
            ):
                # A scan launched from an existing card is an update of that
                # card's original thesis, never an opposite-signal publisher.
                # Suppress the raw opposite Trigger before repository
                # reconciliation so only a normal 15m/4H/full radar scan can
                # persist a genuinely reversed Episode.
                signal = None
                reason = "card_direction_locked_opposite"
            raw_signal = signal
            reconciler = getattr(self.repository, "reconcile_instrument", None)
            if callable(reconciler):
                persisted = reconciler(
                    raw_signal,
                    result.market_state,
                    analyzed_at,
                    horizon,
                )
                if (
                    persisted is not None
                    and raw_signal is not None
                    and persisted.direction == raw_signal.direction
                ):
                    signal = self._episode_with_latest_signal_context(
                        persisted,
                        raw_signal,
                    )
                elif persisted is not None and result.market_state is not None:
                    signal = self._episode_with_latest_state_context(
                        persisted,
                        result.market_state,
                    )
                else:
                    signal = persisted
            if signal is not None:
                live_metrics = dict(signal.market_metrics)
                live_metrics["last_price"] = ticker.last
                signal = _without_internal_metrics(
                    self._attach_decision_context(
                        self._refresh_entry_eligibility(
                            replace(signal, market_metrics=live_metrics)
                        )
                    )
                )
            state = (
                _without_internal_metrics(
                    self._attach_decision_context(result.market_state)
                )
                if result.market_state is not None
                else None
            )
            return replace(
                result,
                signal=signal,
                market_state=state,
                reason=reason,
            )

        finalized_short = finalize(short_result, "SHORT")
        finalized_long = finalize(long_result, "LONG")
        return SingleInstrumentScan(
            inst_id=inst_id,
            ticker=ticker,
            context=context,
            short_result=finalized_short,
            long_result=finalized_long,
            analyzed_at=analyzed_at,
            errors=list(dict.fromkeys(errors)),
        )

    @staticmethod
    def _episode_with_latest_signal_context(
        episode: Signal,
        latest: Signal,
    ) -> Signal:
        """Project fresh context onto an immutable episode/trade plan."""

        story = dict(latest.market_story)
        story["trigger"] = dict(episode.market_story.get("trigger", {}))
        return replace(
            latest,
            direction=episode.direction,
            strategy=episode.strategy,
            entry_low=episode.entry_low,
            entry_high=episode.entry_high,
            stop_loss=episode.stop_loss,
            take_profit_1=episode.take_profit_1,
            take_profit_2=episode.take_profit_2,
            risk_reward=episode.risk_reward,
            invalidation=episode.invalidation,
            trigger_id=episode.trigger_id,
            trigger_type=episode.trigger_type,
            signal_stage=episode.signal_stage,
            freshness=episode.freshness,
            lifecycle=dict(episode.lifecycle),
            generated_at=episode.generated_at,
            market_story=story,
            decision_context={},
        )

    @staticmethod
    def _episode_with_latest_state_context(
        episode: Signal,
        state: MarketState,
    ) -> Signal:
        """Keep an episode visible while applying the newest market state."""

        story = dict(state.market_story)
        story["trigger"] = dict(episode.market_story.get("trigger", {}))
        metrics = dict(episode.market_metrics)
        metrics.update(state.market_metrics)
        return replace(
            episode,
            spread_pct=state.spread_pct,
            quote_volume_24h=state.quote_volume_24h,
            closed_candle_ts=state.closed_candle_ts,
            regime=state.regime,
            readiness_score=state.readiness_score,
            factor_scores=dict(state.factor_scores),
            market_metrics=metrics,
            evidence_groups=dict(state.evidence_groups),
            timeframe_states=dict(state.timeframe_states),
            supporting_evidence=list(state.supporting_evidence),
            conflicts=list(state.conflicts),
            neutral_evidence=list(state.neutral_evidence),
            safety_checks=list(state.safety_checks),
            entry_quality=dict(state.entry_quality),
            summary=state.summary,
            direction_state=state.direction_state,
            market_participation=dict(state.market_participation),
            execution_quality=dict(state.execution_quality),
            data_quality=dict(state.data_quality),
            market_story=story,
            data_timestamp=state.data_timestamp,
            actionable=False,
            decision_context={},
        )

    def reanalyze_instrument(
        self,
        previous_signal: Signal,
        market_bias: dict[str, Any] | None = None,
    ) -> SingleInstrumentReanalysis:
        """Re-run the unchanged V3.4 pipeline for one invalidated plan.

        The returned signal is only populated when the latest closed-candle
        analysis contains a genuinely new Trigger event. The original event is
        never repackaged with newly calculated prices.
        """

        inst_id = previous_signal.inst_id
        horizon = previous_signal.radar_horizon
        if horizon not in ("SHORT", "LONG"):
            raise ValueError("單幣重新分析的週期不正確")

        instrument_loader = getattr(self.client, "get_usdt_swap_instrument", None)
        if callable(instrument_loader):
            instrument = instrument_loader(inst_id)
        else:
            instruments = self.client.get_usdt_swap_instruments()
            instrument = next((item for item in instruments if item.inst_id == inst_id), None)
        if instrument is None:
            raise ValueError("OKX 最新 live USDT 永續清單中已找不到這個幣種")

        ticker_loader = getattr(self.client, "get_ticker", None)
        if callable(ticker_loader):
            ticker = ticker_loader(inst_id)
        else:
            ticker = self.client.get_swap_tickers().get(inst_id)
            if ticker is None:
                raise ValueError("OKX 最新 Ticker 中找不到這個幣種")

        core_bars = self.short_bars if horizon == "SHORT" else ("1D", "4H", "1H")
        bundle = self._fetch_bundle(inst_id, core_bars)
        missing_core_bars = [
            bar for bar in core_bars if len(bundle.get(bar, [])) < 60
        ]
        if missing_core_bars:
            raise ValueError(
                f"OKX 回傳的 {'、'.join(missing_core_bars)} 已收盤 K 線少於 60 根。"
                "這是行情歷史不足，不是舊訊號再次失效；資料補齊前暫不重新分析。"
            )

        result = (
            self._analyze_short_v33(instrument, ticker, bundle)
            if horizon == "SHORT"
            else self._analyze_long_v33(instrument, ticker, bundle)
        )
        if result is None:
            raise ValueError("目前分析引擎不支援這個週期的單幣重新分析")

        context_errors: list[str] = []
        open_interest_usd: float | None = None
        oi_loader = getattr(self.client, "get_open_interest_usd", None)
        if callable(oi_loader):
            try:
                open_interest_usd = oi_loader().get(inst_id)
            except Exception as exc:
                context_errors.append(f"open_interest: {exc}")
        result = self._attach_oi_snapshot(
            result,
            open_interest_usd,
            self._open_interest_change(inst_id, open_interest_usd),
        )

        timing: list[Candle] = []
        if horizon == "SHORT":
            try:
                timing = self.client.get_candles(
                    inst_id,
                    "5m",
                    self.config.candle_limit_5m,
                )
                if len(timing) < 60:
                    context_errors.append("5m 已收盤 K 線不足 60 根")
                    timing = []
            except Exception as exc:
                context_errors.append(f"5m: {exc}")
        else:
            timing = bundle["1H"]

        context = MarketContext(
            inst_id=inst_id,
            open_interest_usd=open_interest_usd,
            funding_rate=None,
            order_book_imbalance=None,
            taker_buy_ratio=None,
            sampled_at=int(ticker.ts or 0),
            failures=list(context_errors),
            best_bid=ticker.bid,
            best_ask=ticker.ask,
        )
        context_loader = getattr(self.client, "get_market_context", None)
        context_applier = getattr(self.engine, "apply_market_context", None)
        if callable(context_loader) and callable(context_applier):
            try:
                loaded_context = context_loader(inst_id, open_interest_usd)
                context = replace(
                    loaded_context,
                    open_interest_change_pct=self._open_interest_change(
                        inst_id,
                        loaded_context.open_interest_usd,
                    ),
                    failures=[*loaded_context.failures, *context_errors],
                )
                state = result.market_state
                direction = state.direction if state is not None else "NEUTRAL"
                previous_micro = self.repository.load_microstructure(inst_id)
                sequence = (
                    classify_microstructure(
                        previous_micro,
                        context,
                        direction,
                        state.market_metrics.get("price_change_core_pct") if state else None,
                    )
                    if direction in ("LONG", "SHORT")
                    else {
                        "state": "NEUTRAL",
                        "reason": "方向中性，不替單張委託簿賦予多空意義",
                    }
                )
                directional_context = replace(context, order_book_sequence=sequence)
                result = context_applier(
                    result,
                    directional_context,
                    "NEUTRAL",
                    timing or None,
                    market_bias or {},
                )
                result = self._apply_professional_context(
                    result,
                    directional_context,
                    previous_micro,
                    dict(market_bias or {}),
                    ticker.last,
                )
                context = directional_context
                self.repository.save_microstructure(
                    directional_context,
                    datetime.now(timezone.utc).isoformat(),
                )
            except Exception as exc:
                context_errors.append(f"Context 整合失敗：{exc}")
                result = self._mark_deep_data_missing(result, context_errors)
                context = replace(context, failures=list(context_errors))
        elif result.market_state is not None:
            context_errors.append("Deep Data 單幣服務暫不可用")
            result = self._mark_deep_data_missing(result, context_errors)
            context = replace(context, failures=list(context_errors))

        if open_interest_usd is not None:
            self._previous_open_interest_usd[inst_id] = open_interest_usd

        raw_signal = result.signal
        reason = result.reason
        if raw_signal is not None and not self._passes_output_liquidity(raw_signal, False):
            raw_signal = None
            reason = "universe_output_gate"
        if raw_signal is not None and self._plan_crossed_live_stop(raw_signal, ticker.last):
            raw_signal = None
            reason = "new_plan_already_invalidated"
        if raw_signal is not None:
            live_metrics = dict(raw_signal.market_metrics)
            live_metrics["last_price"] = ticker.last
            raw_signal = self._refresh_entry_eligibility(
                replace(raw_signal, market_metrics=live_metrics)
            )
            if raw_signal.entry_eligibility.get("status") not in (
                "ENTRY_READY",
                "WAIT_RETEST",
            ):
                raw_signal = None
                reason = "new_trigger_not_an_entry_opportunity"
        if raw_signal is not None and not self._is_new_trigger_event(
            previous_signal,
            raw_signal,
        ):
            raw_signal = None
            reason = "same_original_trigger"

        return SingleInstrumentReanalysis(
            previous_signal=previous_signal,
            ticker=ticker,
            context=context,
            market_state=result.market_state,
            raw_signal=raw_signal,
            analyzed_at=datetime.now(timezone.utc).isoformat(),
            reason=reason,
        )

    def commit_single_reanalysis(
        self,
        analysis: SingleInstrumentReanalysis,
    ) -> Signal | None:
        """Close the old plan and optionally publish one genuinely new plan."""

        self.repository.invalidate_preflight_plan(
            analysis.previous_signal,
            analysis.analyzed_at,
        )
        states = [analysis.market_state] if analysis.market_state is not None else []
        raw_signals = [analysis.raw_signal] if analysis.raw_signal is not None else []
        committed = self.repository.reconcile(
            raw_signals,
            states,
            analysis.analyzed_at,
            analysis.previous_signal.radar_horizon,
        )
        candidates = [
            item
            for item in committed
            if item.inst_id == analysis.previous_signal.inst_id
            and item.trigger_id != analysis.previous_signal.trigger_id
            and self._is_new_trigger_event(analysis.previous_signal, item)
        ]
        if not candidates:
            return None
        selected = max(candidates, key=self._signal_sort_key)
        return _without_internal_metrics(self._refresh_entry_eligibility(selected))

    @staticmethod
    def _is_new_trigger_event(previous: Signal, candidate: Signal) -> bool:
        if candidate.inst_id != previous.inst_id:
            return False
        previous_trigger = previous.market_story.get("trigger", {})
        candidate_trigger = candidate.market_story.get("trigger", {})
        previous_key = str(
            previous.lifecycle.get("event_key")
            or previous_trigger.get("trigger_event_key")
            or ""
        )
        candidate_key = str(
            candidate.lifecycle.get("event_key")
            or candidate_trigger.get("trigger_event_key")
            or ""
        )
        previous_ts = int(
            previous_trigger.get("event_ts")
            or previous.data_timestamp
            or previous.closed_candle_ts
            or 0
        )
        candidate_ts = int(
            candidate_trigger.get("event_ts")
            or candidate.data_timestamp
            or candidate.closed_candle_ts
            or 0
        )
        if previous_key and candidate_key and previous_key == candidate_key:
            return False
        if candidate.direction != previous.direction and candidate_ts >= previous_ts:
            return True
        return bool(candidate_ts and candidate_ts > previous_ts)

    @staticmethod
    def _plan_crossed_live_stop(signal: Signal, current_price: float) -> bool:
        stop = _finite_number(signal.stop_loss)
        if stop is None:
            return True
        return (
            current_price <= stop
            if signal.direction == "LONG"
            else current_price >= stop
        )

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
        # A core preview is deliberately read-only.  Funding, OI, Order Book,
        # slippage and execution-risk details have not finished yet, so it must
        # neither create/advance a durable Signal Episode nor claim final entry
        # permission.  The final report is the only commit point.
        def preview_projection(item: Signal) -> Signal:
            refreshed = self._refresh_entry_eligibility(item)
            projected = self._attach_decision_context(refreshed)
            eligibility = {
                **dict(projected.entry_eligibility),
                "status": "DATA_PENDING",
                "label": "初步候選｜完整掃描中",
                "reason": "成交深度與風險提醒仍在補齊，完成後顯示正式結果。",
                "actionable": False,
                "new_entry_allowed": False,
                "wait_reason_code": "DEEP_DATA_PENDING",
                "direction_still_valid": True,
                "hard_blockers": [],
            }
            decision = dict(projected.decision_context)
            decision["final"] = {
                **dict(decision.get("final", {}) or {}),
                "status": "WAIT",
                "label": "初步候選｜完整掃描中",
                "new_entry_allowed": False,
                "wait_reason": {
                    "code": "DEEP_DATA_PENDING",
                    "label": "等待正式掃描完成",
                },
            }
            return replace(
                projected,
                actionable=False,
                entry_eligibility=eligibility,
                decision_context=decision,
            )

        short_signals = [preview_projection(item) for item in raw_short_signals]
        short_signals = [_without_internal_metrics(item) for item in short_signals]
        short_states = [
            _without_internal_metrics(self._attach_decision_context(item))
            for item in short_states
        ]
        short_signals = sorted(
            short_signals,
            key=self._signal_sort_key,
            reverse=True,
        )[: min(max(self.config.max_signals, 0), 20)]
        short_watchlist = self._watchlist(short_states)
        short_states.sort(key=lambda item: item.inst_id)
        short_market_map = [_compact_market_map_state(item) for item in short_states]
        all_failures = {
            **dict(sorted(failures.items())),
            **dict(sorted(analysis_failures.items())),
        }
        coverage = round(len(bundles) / len(instruments) * 100.0, 4)
        candidate_count = len(short_signals)
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
                f"15m 核心初步候選 {candidate_count} 筆；目前一律不可進場。"
                "正在補 Funding、OI、Order Book、滑價與風險提醒。"
            ),
            market_regime_counts=dict(
                Counter(item.regime for item in short_states)
            ),
            watchlist=short_watchlist,
            market_map=short_market_map,
            market_bias=market_bias,
            scan_id=scan_id,
            scan_started_at=scan_started_at,
            completed_at="",
            runtime_status="CORE_PREVIEW",
            actionable=False,
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
            scan_mode="SHORT",
            short_completed_at=generated_at,
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
        if "excursion_profile_loader" in parameters:
            kwargs["excursion_profile_loader"] = (
                lambda direction, trigger_type: self.repository.excursion_profile(
                    instrument.inst_id,
                    "SHORT",
                    direction,
                    trigger_type,
                )
            )
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
        if "excursion_profile_loader" in parameters:
            kwargs["excursion_profile_loader"] = (
                lambda direction, trigger_type: self.repository.excursion_profile(
                    instrument.inst_id,
                    "LONG",
                    direction,
                    trigger_type,
                )
            )
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
            "trigger_permission": "NEVER_CREATES_OR_CANCELS_TRIGGER",
            "entry_permission": "ADVISORY_ONLY",
        }
        updated_state = replace(
            state,
            data_quality=data_quality,
            market_participation=participation,
            actionable=state.actionable,
            safety_checks=[
                *[
                    item
                    for item in state.safety_checks
                    if item.get("key") != "deep_data_available"
                ],
                {
                    "key": "deep_data_available",
                    "label": "Deep Data 不完整；保留 Trigger 並顯示提醒",
                    "passed": False,
                    "value": "MISSING",
                    "hard": False,
                },
            ],
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
                actionable=result.signal.actionable,
                safety_checks=[
                    *[
                        item
                        for item in result.signal.safety_checks
                        if item.get("key") != "deep_data_available"
                    ],
                    {
                        "key": "deep_data_available",
                        "label": "Deep Data 不完整；保留 Trigger 並顯示提醒",
                        "passed": False,
                        "value": "MISSING",
                        "hard": False,
                    },
                ],
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
            else:
                signals.append(result.signal)
        return states, signals

    @staticmethod
    def _signal_sort_key(signal: Signal) -> tuple[Any, ...]:
        entry_priority = {
            "ENTRY_READY": 3,
            "WAIT_RETEST": 2,
            "MISSED_ENTRY": 1,
        }
        final_priority = {
            "ENTER": 8,
            "WAIT": 7,
            "NO_CHASE": 6,
            "NO_EDGE": 5,
            "DATA_UNAVAILABLE": 4,
            "ANOMALY": 3,
            "COMPLETED": 2,
            "INVALIDATED": 0,
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
        execution_score = _finite_number(signal.execution_quality.get("score"))
        execution_score = execution_score if execution_score is not None else 0.0
        remaining_rr = _finite_number(
            signal.entry_eligibility.get("remaining_rr")
        )
        if remaining_rr is None:
            remaining_rr = _finite_number(signal.risk_reward)
        remaining_rr = remaining_rr if remaining_rr is not None else 0.0
        freshness_timestamps = [
            value
            for value in (
                _finite_number(signal.data_timestamp),
                _finite_number(signal.closed_candle_ts),
            )
            if value is not None and value > 0
        ]
        freshness_timestamp = max(freshness_timestamps, default=0.0)
        slippage_key = (
            "buy_slippage_pct" if signal.direction == "LONG" else "sell_slippage_pct"
        )
        slippage = _finite_number(signal.market_metrics.get(slippage_key))
        if slippage is None:
            slippage = _finite_number(signal.spread_pct)
        slippage = slippage if slippage is not None else float("inf")
        decision = (
            signal.decision_context
            if isinstance(signal.decision_context, dict)
            else {}
        )
        final = decision.get("final", {})
        final = final if isinstance(final, dict) else {}
        final_status = str(final.get("status") or "").upper()
        if final:
            # Canonical five-layer decision must outrank the raw positional
            # eligibility.  A Hard-Gate-blocked signal may still carry the
            # original ``ENTRY_READY`` position, but it must never displace an
            # actually permitted signal when the formal list is truncated.
            permission_priority = int(
                final.get("new_entry_allowed") is True
                and final_status == "ENTER"
            )
            status_priority = final_priority.get(final_status, 1)
        else:
            # Some internal pre-commit callers rank a Signal before the
            # decision projection is attached.  Preserve their deterministic
            # positional ordering without treating it as a formal permission.
            permission_priority = int(
                signal.entry_eligibility.get("new_entry_allowed") is True
                or (
                    "new_entry_allowed" not in signal.entry_eligibility
                    and signal.actionable
                    and signal.entry_eligibility.get("status") == "ENTRY_READY"
                )
            )
            status_priority = entry_priority.get(
                signal.entry_eligibility.get("status"),
                0,
            )
        return (
            permission_priority,
            status_priority,
            execution_score,
            freshness_timestamp,
            freshness_priority.get(signal.freshness, 0),
            -int(signal.lifecycle.get("age_bars", 0) or 0),
            remaining_rr,
            -slippage,
            signal.quote_volume_24h,
            stage_priority.get(signal.signal_stage, 0),
            signal.score,
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
            missing_inputs = [key for key, value in values.items() if value is None]
            eligibility = {
                "status": "DATA_UNAVAILABLE",
                "label": "資料不足｜禁止新進場",
                "reason": (
                    "缺少即時進場判定資料：" + "、".join(missing_inputs)
                    + "；不知道就不使用舊值或 0 補算。"
                ),
                "actionable": False,
                "new_entry_allowed": False,
                "direction_still_valid": True,
                "wait_reason_code": "DATA_MISSING",
                "hard_blockers": ["ENTRY_INPUT_MISSING"],
                "chase_atr": None,
                "remaining_rr": None,
                "remaining_rr_applicable": False,
            }
            checks = [
                item
                for item in signal.safety_checks
                if item.get("key") != "entry_inputs_available"
            ]
            checks.append(
                {
                    "key": "entry_inputs_available",
                    "label": "即時 Entry／SL／TP／ATR 資料完整",
                    "passed": False,
                    "value": missing_inputs,
                    "hard": True,
                }
            )
            return replace(
                signal,
                safety_checks=checks,
                entry_eligibility=eligibility,
                actionable=False,
            )
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
        eligibility = dict(eligibility)
        position_status = str(eligibility.get("status", "DATA_UNAVAILABLE"))
        direction_still_valid = not (
            position_status == "MISSED_ENTRY"
            and "失效" in str(eligibility.get("label", ""))
        )
        risk_warnings = _unique_strings(
            [
                *list(eligibility.get("risk_warnings", []) or []),
                *self._signal_risk_warning_codes(signal),
            ]
        )
        position_actionable = bool(eligibility.get("actionable"))
        eligibility.update(
            {
                "actionable": position_actionable,
                "new_entry_allowed": position_actionable,
                "direction_still_valid": direction_still_valid,
                "hard_blockers": [],
                "risk_warnings": risk_warnings,
                "wait_reason_code": self._entry_wait_reason(eligibility),
            }
        )
        metrics.update(
            {
                "entry_status": eligibility["status"],
                "entry_chase_atr": eligibility["chase_atr"],
                "remaining_rr": eligibility["remaining_rr"],
            }
        )
        checks = [
            {**item, "hard": False}
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

    def _attach_decision_context(
        self,
        item: Signal | MarketState,
    ) -> Signal | MarketState:
        decision = build_decision_context(item, self.config)
        final = decision.get("final", {})
        allowed = bool(final.get("new_entry_allowed"))
        if isinstance(item, Signal):
            eligibility = dict(item.entry_eligibility)
            eligibility["new_entry_allowed"] = allowed
            eligibility.setdefault(
                "direction_still_valid",
                str(final.get("status")) != "INVALIDATED",
            )
            risk_review = decision.get("hard_gate", {})
            risk_warnings = [
                *list(eligibility.get("risk_warnings", []) or []),
                *list(risk_review.get("blockers", []) or []),
                *list(risk_review.get("unknowns", []) or []),
            ]
            eligibility["hard_blockers"] = []
            eligibility["risk_warnings"] = _unique_strings(risk_warnings)
            wait_reason = final.get("wait_reason")
            if isinstance(wait_reason, dict):
                eligibility["wait_reason_code"] = wait_reason.get("code")
            metrics = dict(item.market_metrics)
            return replace(
                item,
                actionable=allowed,
                market_metrics=metrics,
                entry_eligibility=eligibility,
                decision_context=decision,
            )
        return replace(
            item,
            actionable=allowed,
            decision_context=decision,
        )

    def _apply_professional_context(
        self,
        result: AnalysisResult,
        context: MarketContext,
        previous_micro: dict[str, Any] | None,
        market_bias: dict[str, Any],
        reference_price: float,
    ) -> AnalysisResult:
        """Attach bounded context interpretation without changing the Trigger."""

        state = result.market_state
        if state is None:
            return result
        direction = state.direction if state.direction in {"LONG", "SHORT"} else "NEUTRAL"
        history = list((previous_micro or {}).get("history", []) or [])
        if not history and previous_micro and previous_micro.get("sampled_at"):
            history.append(dict(previous_micro))
        current_sample = {
            "timestamp_ms": int(context.sampled_at or 0),
            "price": _finite_number(reference_price),
            "open_interest_usd": context.open_interest_usd,
            "taker_buy_ratio": context.taker_buy_ratio,
            "funding_rate": context.funding_rate,
            "bid_depth_usd": context.bid_depth_usd,
            "ask_depth_usd": context.ask_depth_usd,
            "order_book_imbalance": context.order_book_imbalance,
        }
        if current_sample["timestamp_ms"] > 0:
            history.append(current_sample)
        flow = summarize_flow_history(history[-8:], direction)

        story = dict(state.market_story)
        raw = dict(story.get("raw", {}) or {})
        metrics = dict(state.market_metrics)
        anomaly_metrics = {
            "range_atr": raw.get("core_range_atr"),
            "volume_ratio": raw.get("core_volume_ratio"),
            "funding_rate": context.funding_rate,
            "spread_pct": state.spread_pct,
            "buy_slippage_pct": context.buy_slippage_pct,
            "sell_slippage_pct": context.sell_slippage_pct,
            "order_book_sequence": dict(context.order_book_sequence),
            "required_missing_sources": list(
                state.data_quality.get("missing_sources", []) or []
            ),
            "api_failures": list(context.failures),
        }
        anomaly = detect_anomaly(anomaly_metrics, flow)
        btc = dict(market_bias.get("btc", {}) or {})
        resonance = dict(market_bias.get("resonance", {}) or {})
        symbol_change = _finite_number(metrics.get("price_change_core_pct"))
        driver = classify_market_driver(
            symbol_change,
            btc.get("core_change_pct"),
            state.market_participation,
            _finite_number(market_bias.get("market_breadth_long_pct")) / 100.0
            if _finite_number(market_bias.get("market_breadth_long_pct")) is not None
            else None,
            resonance,
        )
        trigger_type = str(
            getattr(result.signal, "trigger_type", "")
            or story.get("trigger", {}).get("type", "NONE")
        ).upper()
        phase = (
            "BREAKOUT"
            if trigger_type == "BREAKOUT"
            else "RETEST"
            if trigger_type == "CONTINUATION"
            else "REVERSAL"
            if trigger_type == "REVERSAL"
            else "WEAKENING"
            if state.status in {"NO_FOLLOW_THROUGH", "EXTENDED"}
            else "MATURE"
            if state.status in {"CONFIRMED", "TRENDING"}
            else "FORMING"
        )
        anomaly_status = str(anomaly.get("status", "NORMAL"))
        volatility = (
            "ANOMALOUS"
            if anomaly_status == "BLOCK"
            else "HIGH"
            if anomaly_status == "WATCH"
            or (_finite_number(raw.get("core_range_atr")) or 0.0) >= 2.0
            else "NORMAL"
        )
        context_payload = build_market_context(
            regime=state.regime,
            phase=phase,
            volatility=volatility,
            anomaly=anomaly,
            driver=driver,
            sessions=active_sessions(datetime.now(timezone.utc)),
        )
        invalidation_condition = (
            result.signal.invalidation
            if result.signal is not None
            else "等待正式 Trigger 後建立失效條件"
        )
        interpretation = build_interpretation(
            evidence_groups=state.evidence_groups,
            flow_summary=flow,
            anomaly=anomaly,
            main_conflicts=state.conflicts,
            change_conditions={
                "weaken": [
                    "OI／Taker／Volume 持續衰退",
                    "核心結構失去延續性",
                ],
                "invalidate": [invalidation_condition],
            },
            data_quality=state.data_quality,
        )
        story.update(
            {
                "context": context_payload,
                "interpretation": interpretation,
            }
        )
        metrics.update(
            {
                "flow_trend": flow.get("state"),
                "flow_velocity_abnormal": flow.get("abnormal_speed"),
                "market_driver": driver,
                "relative_strength": driver.get("relative_strength"),
                "market_resonance": resonance,
                "market_sessions": context_payload.get("sessions", {}).get("items", []),
                "anomaly_state": anomaly_status,
                "anomalies": [
                    str(item.get("label"))
                    for item in anomaly.get("reasons", [])
                    if isinstance(item, dict) and item.get("label")
                ],
            }
        )
        conflicts = list(state.conflicts)
        supporting = list(state.supporting_evidence)
        if flow.get("state") == "WEAKENING":
            conflicts = _unique_strings([*conflicts, "市場參與趨勢正在轉弱"])
        elif flow.get("state") == "STRENGTHENING":
            supporting = _unique_strings([*supporting, "市場參與趨勢正在增強"])
        participation = dict(state.market_participation)
        participation["trend"] = {
            "state": flow.get("state", "UNKNOWN"),
            "label": flow.get("label", "資料不足"),
        }
        checks = [
            item
            for item in state.safety_checks
            if item.get("key") != "anomalous_market"
        ]
        checks.append(
            {
                "key": "anomalous_market",
                "label": anomaly.get("label", "行情狀態未知"),
                "passed": anomaly_status != "BLOCK",
                "value": anomaly_status,
                "hard": False,
            }
        )
        updated_state = replace(
            state,
            market_metrics=metrics,
            market_story=story,
            market_participation=participation,
            conflicts=conflicts,
            supporting_evidence=supporting,
            safety_checks=checks,
            actionable=state.actionable,
        )

        def update_signal(signal: Signal | None) -> Signal | None:
            if signal is None:
                return None
            return replace(
                signal,
                market_metrics=metrics,
                market_story=story,
                market_participation=participation,
                conflicts=conflicts,
                supporting_evidence=supporting,
                safety_checks=checks,
                actionable=signal.actionable,
            )

        updated_signal = update_signal(result.signal)
        updated_candidate = update_signal(result.candidate_signal)
        return replace(
            result,
            market_state=updated_state,
            signal=updated_signal,
            candidate_signal=updated_candidate,
        )

    def _signal_risk_warning_codes(self, signal: Signal) -> list[str]:
        """Return advisory risk codes without cancelling or hiding a Trigger."""

        blockers: list[str] = []
        for check in signal.safety_checks:
            if check.get("hard") and not bool(check.get("passed")):
                blockers.append(str(check.get("key") or "HARD_CHECK_FAILED").upper())

        quality = signal.data_quality or {}
        core_status = str(
            quality.get("core_status") or quality.get("core") or "UNKNOWN"
        ).upper()
        if core_status not in {"AVAILABLE", "FRESH"}:
            blockers.append("CORE_DATA_UNAVAILABLE")
        if quality.get("closed_candle") is False:
            blockers.append("CORE_DATA_UNAVAILABLE")
        deep_status = str(
            quality.get("deep_status") or quality.get("deep") or "UNKNOWN"
        ).upper()
        if deep_status not in {"AVAILABLE", "FRESH"}:
            blockers.append("DEEP_DATA_UNAVAILABLE")

        spread = _finite_number(signal.spread_pct)
        if spread is None or spread > self.config.max_spread_pct:
            blockers.append("SPREAD_TOO_HIGH")

        metrics = signal.market_metrics or {}
        slippage_key = (
            "buy_slippage_pct" if signal.direction == "LONG" else "sell_slippage_pct"
        )
        slippage = _finite_number(metrics.get(slippage_key))
        execution_notional = _finite_number(metrics.get("execution_notional_usdt"))
        execution_required = execution_notional is None or execution_notional > 0
        if execution_required and (
            metrics.get("execution_quality_complete") is not True
            or slippage is None
        ):
            blockers.append("EXECUTION_DATA_UNAVAILABLE")
        elif slippage is not None and slippage > self.config.max_slippage_pct:
            blockers.append("SLIPPAGE_TOO_HIGH")

        cost_to_risk = _finite_number(
            metrics.get("execution_cost_to_risk_pct")
            if metrics.get("execution_cost_to_risk_pct") is not None
            else signal.execution_quality.get("execution_cost_to_risk_pct")
        )
        if execution_required and cost_to_risk is None:
            blockers.append("EXECUTION_DATA_UNAVAILABLE")
        elif (
            cost_to_risk is not None
            and cost_to_risk > self.config.max_execution_cost_to_risk_pct
        ):
            blockers.append("EXECUTION_COST_TOO_HIGH")

        rr = _finite_number(signal.risk_reward)
        if rr is None or rr < self.config.minimum_rr:
            blockers.append("RR_INSUFFICIENT")

        context = (signal.market_story or {}).get("context", {})
        anomaly = context.get("anomaly", {}) if isinstance(context, dict) else {}
        if str(anomaly.get("status", "NORMAL")).upper() == "BLOCK":
            blockers.append("ANOMALOUS_MARKET")
        return _unique_strings(blockers)

    @staticmethod
    def _entry_wait_reason(eligibility: dict[str, Any]) -> str | None:
        status = str(eligibility.get("status", ""))
        label = str(eligibility.get("label", ""))
        if status == "ENTRY_READY":
            return None
        if "追價" in label or "錯過" in label:
            return "NO_CHASE"
        if status == "WAIT_RETEST":
            return "WAIT_RETEST"
        return "SIGNAL_FORMING"

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
        # Formal price Triggers are no longer removed by liquidity/Spread
        # thresholds.  Those values remain visible as risk warnings.
        del item, require_context
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
        btc_result = results.get("BTC-USDT-SWAP")
        btc_state = getattr(btc_result, "market_state", None)
        btc_core_change = (
            _finite_number(btc_state.market_metrics.get("price_change_core_pct"))
            if btc_state is not None
            else None
        )
        btc_direction = (
            btc_state.direction
            if btc_state is not None and btc_state.direction in {"LONG", "SHORT"}
            else "NEUTRAL"
        )
        formal_directions = [
            str(getattr(result.signal, "direction", ""))
            for result in results.values()
            if getattr(result, "signal", None) is not None
            and str(getattr(result.signal, "direction", "")) in {"LONG", "SHORT"}
        ]
        formal_long = sum(item == "LONG" for item in formal_directions)
        formal_short = sum(item == "SHORT" for item in formal_directions)
        formal_count = len(formal_directions)
        dominant_formal = max(formal_long, formal_short)
        resonance_ratio = (
            dominant_formal / formal_count if formal_count else 0.0
        )
        resonance_direction = (
            "LONG"
            if formal_long > formal_short
            else "SHORT"
            if formal_short > formal_long
            else "NEUTRAL"
        )
        resonance_active = formal_count >= 5 and resonance_ratio >= 0.65
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
            "btc": {
                "direction": btc_direction,
                "core_change_pct": btc_core_change,
            },
            "resonance": {
                "active": resonance_active,
                "direction": resonance_direction,
                "formal_count": formal_count,
                "ratio": round(resonance_ratio, 3),
            },
            "exposure_warning": {
                "active": resonance_active and dominant_formal >= 3,
                "label": (
                    "多個機會可能屬同一市場方向曝險"
                    if resonance_active
                    else "未見明顯同向群體曝險"
                ),
            },
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

    def release_transient_data(self) -> int:
        """Release cross-timeframe candle reuse after a completed web scan.

        The cache prevents duplicate 4H/1H requests while one scan builds both
        radar horizons. Keeping hundreds of candle arrays after the report is
        published provides little value on a memory-constrained web service.
        """

        with self._candle_cache_lock:
            cached_series = len(self._candle_cache)
            self._candle_cache.clear()
        return cached_series

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
        scan_mode: str = "FULL",
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
            scan_mode=scan_mode,
        )


def _without_internal_metrics(item: Signal | MarketState) -> Signal | MarketState:
    metrics = {
        key: value
        for key, value in item.market_metrics.items()
        if not key.startswith("_")
    }
    return replace(item, market_metrics=metrics)


_MARKET_MAP_METRIC_KEYS = frozenset(
    {
        "last_price",
        "price_change_core_pct",
        "price_change_15m_pct",
        "price_change_1h_pct",
        "price_change_24h_pct",
        "rsi_core",
        "rsi_15m",
        "open_interest_usd",
        "open_interest_change_pct",
        "oi_flow_state",
        "funding_rate_pct",
    }
)


def _compact_market_map_state(item: MarketState) -> MarketState:
    """Keep overview/search fields without duplicating full analysis stories.

    Full Market Story, evidence and raw indicators remain available on the
    bounded signal and watchlist collections. The all-market maps are used by
    the mobile overview, heat map, OI anomalies, favorites and symbol search,
    all of which only need this compact projection.
    """

    metrics = {
        key: value
        for key, value in item.market_metrics.items()
        if key in _MARKET_MAP_METRIC_KEYS
    }
    return replace(
        item,
        market_metrics=metrics,
        evidence_groups={},
        timeframe_states={},
        supporting_evidence=[],
        conflicts=[],
        neutral_evidence=[],
        safety_checks=[],
        entry_quality={},
        trigger={},
        lifecycle={},
        market_participation={},
        execution_quality={},
        market_story={},
        decision_context={},
    )


def _finite_number(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
