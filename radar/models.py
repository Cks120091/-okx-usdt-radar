from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Instrument:
    inst_id: str
    state: str
    settle_ccy: str
    ct_type: str
    tick_size: float
    list_time: int = 0
    contract_value: float = 1.0
    contract_multiplier: float = 1.0
    contract_value_ccy: str = ""


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    confirmed: bool


@dataclass(frozen=True)
class Ticker:
    inst_id: str
    last: float
    bid: float
    ask: float
    ts: int

    @property
    def spread_pct(self) -> float:
        mid = (self.bid + self.ask) / 2.0
        if mid <= 0 or self.ask < self.bid:
            return float("inf")
        return (self.ask - self.bid) / mid * 100.0


@dataclass(frozen=True)
class MarketContext:
    inst_id: str
    open_interest_usd: float | None
    funding_rate: float | None
    order_book_imbalance: float | None
    taker_buy_ratio: float | None
    sampled_at: int
    failures: list[str] = field(default_factory=list)
    bid_depth_usd: float | None = None
    ask_depth_usd: float | None = None
    buy_slippage_pct: float | None = None
    sell_slippage_pct: float | None = None
    execution_notional_usdt: float = 0.0
    open_interest_change_pct: float | None = None

    @property
    def complete(self) -> bool:
        return all(
            value is not None
            for value in (
                self.funding_rate,
                self.order_book_imbalance,
                self.taker_buy_ratio,
            )
        )

    @property
    def execution_quality_complete(self) -> bool:
        if self.execution_notional_usdt <= 0:
            return True
        return all(
            value is not None
            for value in (
                self.bid_depth_usd,
                self.ask_depth_usd,
                self.buy_slippage_pct,
                self.sell_slippage_pct,
            )
        )


@dataclass
class Signal:
    inst_id: str
    direction: str
    strategy: str
    score: float
    evidence: list[str]
    entry_low: str
    entry_high: str
    stop_loss: str
    take_profit_1: str
    take_profit_2: str
    risk_reward: float
    invalidation: str
    spread_pct: float
    quote_volume_24h: float
    closed_candle_ts: int
    regime: str
    notes: list[str] = field(default_factory=list)
    factor_scores: dict[str, float] = field(default_factory=dict)
    market_metrics: dict[str, Any] = field(default_factory=dict)
    signal_stage: str = "CONFIRMED"
    trend_strength_label: str = "中等"
    trend_strength_score: float = 50.0
    management_plan: dict[str, Any] = field(default_factory=dict)
    readiness_score: float = 0.0
    evidence_groups: dict[str, Any] = field(default_factory=dict)
    timeframe_states: dict[str, Any] = field(default_factory=dict)
    supporting_evidence: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    neutral_evidence: list[str] = field(default_factory=list)
    safety_checks: list[dict[str, Any]] = field(default_factory=list)
    entry_quality: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    lifecycle: dict[str, Any] = field(default_factory=dict)
    actionable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MarketState:
    inst_id: str
    regime: str
    direction: str
    preferred_strategy: str
    readiness_score: float
    status: str
    missing_conditions: list[str]
    spread_pct: float
    quote_volume_24h: float
    closed_candle_ts: int
    passed_conditions: list[str] = field(default_factory=list)
    factor_scores: dict[str, float] = field(default_factory=dict)
    market_metrics: dict[str, Any] = field(default_factory=dict)
    evidence_groups: dict[str, Any] = field(default_factory=dict)
    timeframe_states: dict[str, Any] = field(default_factory=dict)
    supporting_evidence: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    neutral_evidence: list[str] = field(default_factory=list)
    safety_checks: list[dict[str, Any]] = field(default_factory=list)
    entry_quality: dict[str, Any] = field(default_factory=dict)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RadarReport:
    status: str
    generated_at: str
    scope: str
    target_count: int
    fetched_count: int
    analyzable_count: int
    coverage_pct: float
    target_instruments: list[str]
    failed_instruments: dict[str, str]
    signals: list[Signal]
    exclusion_counts: dict[str, int]
    duration_seconds: float
    message: str
    market_regime_counts: dict[str, int] = field(default_factory=dict)
    watchlist: list[MarketState] = field(default_factory=list)
    market_map: list[MarketState] = field(default_factory=list)
    context_target_count: int = 0
    context_enriched_count: int = 0
    context_failures: dict[str, list[str]] = field(default_factory=dict)
    market_bias: dict[str, Any] = field(default_factory=dict)
    scan_id: str = ""
    scan_started_at: str = ""
    completed_at: str = ""
    runtime_status: str = "FRESH"
    actionable: bool = True
    signals_suppressed_reason: str | None = None
    max_signals: int = 20
    api_metrics: dict[str, Any] = field(default_factory=dict)
    version: str = "2.0"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["signals"] = [item.to_dict() for item in self.signals]
        payload["watchlist"] = [item.to_dict() for item in self.watchlist]
        payload["market_map"] = [item.to_dict() for item in self.market_map]
        payload["safety"] = {
            "mode": "analysis_only",
            "auto_ordering": False,
            "actionable": self.actionable,
            "max_risk_per_trade_pct": 1.0,
            "note": "訊號是條件式分析，不是保證獲利；實際下單前需自行確認。",
        }
        return payload
