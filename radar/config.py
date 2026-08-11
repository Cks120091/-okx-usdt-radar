from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    okx_base_url: str = "https://www.okx.com"
    host: str = "0.0.0.0"
    port: int = 8000
    data_dir: str = "data"
    max_signals: int = 10
    workers: int = 8
    candle_limit: int = 100
    request_timeout_seconds: float = 12.0
    request_retries: int = 3
    rate_limit_requests_per_2s: int = 18
    min_quote_volume_24h: float = 5_000_000.0
    max_spread_pct: float = 0.10
    min_open_interest_usd: float = 3_000_000.0
    require_micro_volume_anomaly: bool = True
    minimum_rr: float = 1.8
    context_candidates: int = 30
    execution_notional_usdt: float = 1_000.0
    estimated_taker_fee_pct: float = 0.05
    max_execution_cost_to_risk_pct: float = 12.0
    max_entry_extension_atr: float = 0.80
    scan_at_start: bool = True
    align_to_hour: bool = True
    interval_seconds: int = 3600

    @classmethod
    def load(cls, path: str | None = None) -> "AppConfig":
        values: dict = {}
        if path:
            file_path = Path(path)
            if file_path.exists():
                values.update(json.loads(file_path.read_text(encoding="utf-8")))
        env_map = {
            "OKX_BASE_URL": ("okx_base_url", str),
            "HOST": ("host", str),
            "PORT": ("port", int),
            "RADAR_DATA_DIR": ("data_dir", str),
            "RADAR_MAX_SIGNALS": ("max_signals", int),
            "RADAR_WORKERS": ("workers", int),
            "RADAR_MIN_QUOTE_VOLUME": ("min_quote_volume_24h", float),
            "RADAR_MAX_SPREAD_PCT": ("max_spread_pct", float),
            "RADAR_MIN_OPEN_INTEREST_USD": ("min_open_interest_usd", float),
            "RADAR_REQUIRE_MICRO_VOLUME_ANOMALY": ("require_micro_volume_anomaly", _bool),
            "RADAR_MIN_RR": ("minimum_rr", float),
            "RADAR_CONTEXT_CANDIDATES": ("context_candidates", int),
            "RADAR_EXECUTION_NOTIONAL_USDT": ("execution_notional_usdt", float),
            "RADAR_ESTIMATED_TAKER_FEE_PCT": ("estimated_taker_fee_pct", float),
            "RADAR_MAX_EXECUTION_COST_TO_RISK_PCT": ("max_execution_cost_to_risk_pct", float),
            "RADAR_MAX_ENTRY_EXTENSION_ATR": ("max_entry_extension_atr", float),
            "RADAR_SCAN_AT_START": ("scan_at_start", _bool),
            "RADAR_ALIGN_TO_HOUR": ("align_to_hour", _bool),
            "RADAR_INTERVAL_SECONDS": ("interval_seconds", int),
        }
        for env_name, (field_name, converter) in env_map.items():
            if env_name in os.environ:
                values[field_name] = converter(os.environ[env_name])
        known = cls.__dataclass_fields__.keys()
        filtered = {key: value for key, value in values.items() if key in known}
        config = cls(**filtered)
        if not 0 <= config.max_signals <= 10:
            raise ValueError("max_signals must be between 0 and 10")
        if config.interval_seconds < 60:
            raise ValueError("interval_seconds must be at least 60")
        if not 0 <= config.context_candidates <= 100:
            raise ValueError("context_candidates must be between 0 and 100")
        if config.min_quote_volume_24h < 0 or config.min_open_interest_usd < 0:
            raise ValueError("liquidity thresholds must not be negative")
        if config.max_spread_pct < 0:
            raise ValueError("max_spread_pct must not be negative")
        if config.execution_notional_usdt < 0 or config.estimated_taker_fee_pct < 0:
            raise ValueError("execution-cost assumptions must not be negative")
        if config.max_execution_cost_to_risk_pct <= 0 or config.max_entry_extension_atr <= 0:
            raise ValueError("execution and entry-risk limits must be positive")
        return config


def _bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean: {value}")
