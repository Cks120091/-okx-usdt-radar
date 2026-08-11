from __future__ import annotations

import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .models import Candle, Instrument, MarketContext, MarketState, RadarReport, Signal, Ticker
from .strategy import AdaptiveStrategyEngine, StrategyConfig


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
    max_signals: int = 10
    workers: int = 8
    candle_limit: int = 100
    min_quote_volume_24h: float = 5_000_000.0
    max_spread_pct: float = 0.10
    min_open_interest_usd: float = 3_000_000.0
    require_micro_volume_anomaly: bool = True
    minimum_rr: float = 1.8
    context_candidates: int = 30
    estimated_taker_fee_pct: float = 0.05
    max_execution_cost_to_risk_pct: float = 12.0
    max_entry_extension_atr: float = 0.80


class MarketScanner:
    bars = ("4H", "1H", "15m")

    def __init__(self, client: PublicDataClient, config: ScannerConfig | None = None):
        self.client = client
        self.config = config or ScannerConfig()
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

        analysis_results = {}
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
            analysis_results[inst_id] = result

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
            )

        market_bias = self._calculate_market_bias(analysis_results)
        context_failures: dict[str, list[str]] = {}
        context_target_ids: list[str] = []
        context_enriched_count = 0
        context_loader = getattr(self.client, "get_market_context", None)
        context_applier = getattr(self.engine, "apply_market_context", None)
        if callable(context_loader) and callable(context_applier):
            open_interest: dict[str, float] = {}
            oi_loader = getattr(self.client, "get_open_interest_usd", None)
            if callable(oi_loader):
                try:
                    open_interest = oi_loader()
                except Exception as exc:
                    context_failures["_OPEN_INTEREST_"] = [str(exc)]
            ranked_results = [
                (inst_id, result)
                for inst_id, result in analysis_results.items()
                if result.market_state is not None
                and result.market_state.status != "FILTERED"
                and result.market_state.regime != "DISORDER"
                and result.market_state.direction != "NEUTRAL"
                and result.market_state.readiness_score >= 50.0
            ]
            ranked_results.sort(
                key=lambda item: (
                    item[1].signal is not None,
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

            def load_candidate(inst_id: str) -> tuple[MarketContext, list[Candle], str | None]:
                context = context_loader(inst_id, open_interest.get(inst_id))
                try:
                    candles_5m = self.client.get_candles(inst_id, "5m", self.config.candle_limit)
                    if len(candles_5m) < 60:
                        return context, candles_5m, "5m K 線不足 60 根"
                    return context, candles_5m, None
                except Exception as exc:
                    return context, [], f"5m K 線取得失敗：{exc}"

            with ThreadPoolExecutor(max_workers=max(1, min(self.config.workers, 8))) as executor:
                future_map = {
                    executor.submit(load_candidate, inst_id): inst_id
                    for inst_id in context_target_ids
                }
                for future in as_completed(future_map):
                    inst_id = future_map[future]
                    try:
                        context, candles_5m, micro_error = future.result()
                        contexts[inst_id] = context
                        micro_candles[inst_id] = candles_5m
                        if context.complete and context.execution_quality_complete and not micro_error:
                            context_enriched_count += 1
                        candidate_failures = list(context.failures)
                        if micro_error:
                            candidate_failures.append(micro_error)
                        if candidate_failures:
                            context_failures[inst_id] = candidate_failures
                    except Exception as exc:
                        context_failures[inst_id] = [str(exc)]

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

        exclusion_counts: Counter[str] = Counter()
        signals = []
        market_states: list[MarketState] = []
        for result in analysis_results.values():
            if result.market_state is not None:
                market_states.append(result.market_state)
            if result.signal is None:
                exclusion_counts[result.reason] += 1
            elif self._passes_output_liquidity(result.signal):
                signals.append(result.signal)
            else:
                exclusion_counts["output_liquidity_gate"] += 1

        signals.sort(key=lambda item: (item.score, item.quote_volume_24h), reverse=True)
        signals = signals[: min(max(self.config.max_signals, 0), 10)]
        early_count = sum(item.signal_stage == "EARLY" for item in signals)
        confirmed_count = len(signals) - early_count
        watchlist = [
            item
            for item in market_states
            if item.status in ("NEAR_TRIGGER", "WATCH")
            and item.regime != "DISORDER"
            and item.direction != "NEUTRAL"
            and item.readiness_score >= 50.0
            and self._passes_output_liquidity(item)
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
            f"完整掃描完成：{early_count} 個提早訊號、{confirmed_count} 個完整確認，另列出 {len(watchlist)} 個接近觸發候選。"
            if signals
            else f"完整掃描完成：本輪 0 個進場訊號；另列出 {len(watchlist)} 個接近觸發候選，不代表可以直接進場。"
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
            context_target_count=len(context_target_ids),
            context_enriched_count=context_enriched_count,
            context_failures=dict(sorted(context_failures.items())),
            market_bias=market_bias,
        )

    def _passes_output_liquidity(self, item: Signal | MarketState) -> bool:
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
        return (
            not self.config.require_micro_volume_anomaly
            or item.market_metrics.get("micro_volume_anomaly") is True
        )

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
