from __future__ import annotations

import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable, Protocol

from .models import Candle, Instrument, MarketContext, MarketState, RadarReport, Signal, Ticker
from .strategy import AdaptiveStrategyEngine, AnalysisResult, StrategyConfig


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
    workers: int = 8
    candle_limit: int = 100
    candle_limit_4h: int = 200
    candle_limit_1h: int = 240
    candle_limit_15m: int = 200
    candle_limit_5m: int = 120
    min_quote_volume_24h: float = 5_000_000.0
    max_spread_pct: float = 0.10
    min_open_interest_usd: float = 3_000_000.0
    require_micro_volume_anomaly: bool = False
    minimum_rr: float = 1.8
    context_candidates: int = 100
    estimated_taker_fee_pct: float = 0.05
    max_execution_cost_to_risk_pct: float = 12.0
    max_entry_extension_atr: float = 0.80
    severe_entry_extension_atr: float = 1.80
    max_slippage_pct: float = 0.15
    previous_open_interest_usd: dict[str, float] = field(default_factory=dict)


ProgressCallback = Callable[[str, int | None, int | None, str], None]


class MarketScanner:
    bars = ("4H", "1H", "15m")

    def __init__(self, client: PublicDataClient, config: ScannerConfig | None = None):
        self.client = client
        self.config = config or ScannerConfig()
        self._previous_open_interest_usd = dict(self.config.previous_open_interest_usd)
        self._signal_history: dict[tuple[str, str], dict[str, str]] = {}
        self.engine = AdaptiveStrategyEngine(
            StrategyConfig(
                min_quote_volume_24h=self.config.min_quote_volume_24h,
                max_spread_pct=self.config.max_spread_pct,
                min_open_interest_usd=self.config.min_open_interest_usd,
                require_micro_volume_anomaly=self.config.require_micro_volume_anomaly,
                minimum_rr=self.config.minimum_rr,
                estimated_taker_fee_pct=self.config.estimated_taker_fee_pct,
                max_execution_cost_to_risk_pct=self.config.max_execution_cost_to_risk_pct,
                max_entry_extension_atr=self.config.max_entry_extension_atr,
                severe_entry_extension_atr=self.config.severe_entry_extension_atr,
                max_slippage_pct=self.config.max_slippage_pct,
            )
        )

    def scan_once(
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
        if item.quote_volume_24h < self.config.min_quote_volume_24h:
            return False
        if item.spread_pct > self.config.max_spread_pct:
            return False
        if self.config.min_open_interest_usd <= 0:
            open_interest_ok = True
        else:
            open_interest = item.market_metrics.get("open_interest_usd")
            open_interest_ok = (
                isinstance(open_interest, (int, float))
                and open_interest >= self.config.min_open_interest_usd
            )
        if not open_interest_ok:
            return False
        if require_context and item.market_metrics.get("context_complete") is not True:
            return False
        if isinstance(item, Signal):
            if not item.actionable:
                return False
            if any(
                check.get("hard", True) and not check.get("passed", False)
                for check in item.safety_checks
            ):
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

    def _fetch_bundle(self, inst_id: str) -> dict[str, list[Candle]]:
        output: dict[str, list[Candle]] = {}
        for bar in self.bars:
            try:
                output[bar] = self.client.get_candles(inst_id, bar, self._bar_limit(bar))
            except Exception as exc:
                raise RuntimeError(f"{bar} K 線取得失敗：{exc}") from exc
        return output

    def _bar_limit(self, bar: str) -> int:
        return {
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


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
