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

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["signals"] = [item.to_dict() for item in self.signals]
        payload["safety"] = {
            "mode": "analysis_only",
            "auto_ordering": False,
            "max_risk_per_trade_pct": 1.0,
            "note": "訊號是條件式分析，不是保證獲利；實際下單前需自行確認。",
        }
        return payload

