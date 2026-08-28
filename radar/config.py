from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    okx_base_url: str = "https://openapi.okx.com"
    host: str = "0.0.0.0"
    port: int = 8000
    data_dir: str = "data"
    max_signals: int = 20
    max_watchlist: int = 20
    workers: int = 12
    candle_limit: int = 100
    candle_limit_1d: int = 200
    candle_limit_4h: int = 200
    candle_limit_1h: int = 240
    candle_limit_15m: int = 200
    candle_limit_5m: int = 120
    request_timeout_seconds: float = 12.0
    request_retries: int = 3
    rate_limit_requests_per_2s: int = 30
    min_quote_volume_24h: float = 5_000_000.0
    max_spread_pct: float = 0.10
    universe_max_spread_pct: float = 1.00
    min_open_interest_usd: float = 3_000_000.0
    require_micro_volume_anomaly: bool = False
    minimum_rr: float = 1.8
    context_candidates: int = 100
    execution_notional_usdt: float = 1_000.0
    estimated_taker_fee_pct: float = 0.05
    max_execution_cost_to_risk_pct: float = 12.0
    max_slippage_pct: float = 0.15
    max_entry_extension_atr: float = 0.80
    severe_entry_extension_atr: float = 1.80
    early_signal_max_age_bars: int = 2
    entry_ready_max_chase_atr: float = 0.15
    entry_missed_chase_atr: float = 0.50
    previous_report_url: str = ""
    stale_after_seconds: int = 1800
    state_db_path: str = ""

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
            "RADAR_MAX_WATCHLIST": ("max_watchlist", int),
            "RADAR_WORKERS": ("workers", int),
            "RADAR_CANDLE_LIMIT_1D": ("candle_limit_1d", int),
            "RADAR_CANDLE_LIMIT_4H": ("candle_limit_4h", int),
            "RADAR_CANDLE_LIMIT_1H": ("candle_limit_1h", int),
            "RADAR_CANDLE_LIMIT_15M": ("candle_limit_15m", int),
            "RADAR_CANDLE_LIMIT_5M": ("candle_limit_5m", int),
            "RADAR_MIN_QUOTE_VOLUME": ("min_quote_volume_24h", float),
            "RADAR_MAX_SPREAD_PCT": ("max_spread_pct", float),
            "RADAR_UNIVERSE_MAX_SPREAD_PCT": ("universe_max_spread_pct", float),
            "RADAR_MIN_OPEN_INTEREST_USD": ("min_open_interest_usd", float),
            "RADAR_REQUIRE_MICRO_VOLUME_ANOMALY": ("require_micro_volume_anomaly", _bool),
            "RADAR_MIN_RR": ("minimum_rr", float),
            "RADAR_CONTEXT_CANDIDATES": ("context_candidates", int),
            "RADAR_EXECUTION_NOTIONAL_USDT": ("execution_notional_usdt", float),
            "RADAR_ESTIMATED_TAKER_FEE_PCT": ("estimated_taker_fee_pct", float),
            "RADAR_MAX_EXECUTION_COST_TO_RISK_PCT": ("max_execution_cost_to_risk_pct", float),
            "RADAR_MAX_SLIPPAGE_PCT": ("max_slippage_pct", float),
            "RADAR_MAX_ENTRY_EXTENSION_ATR": ("max_entry_extension_atr", float),
            "RADAR_SEVERE_ENTRY_EXTENSION_ATR": ("severe_entry_extension_atr", float),
            "RADAR_EARLY_SIGNAL_MAX_AGE_BARS": ("early_signal_max_age_bars", int),
            "RADAR_ENTRY_READY_MAX_CHASE_ATR": ("entry_ready_max_chase_atr", float),
            "RADAR_ENTRY_MISSED_CHASE_ATR": ("entry_missed_chase_atr", float),
            "RADAR_PREVIOUS_REPORT_URL": ("previous_report_url", str),
            "RADAR_STALE_AFTER_SECONDS": ("stale_after_seconds", int),
            "RADAR_STATE_DB_PATH": ("state_db_path", str),
        }
        for env_name, (field_name, converter) in env_map.items():
            if env_name in os.environ:
                values[field_name] = converter(os.environ[env_name])
        known = cls.__dataclass_fields__.keys()
        filtered = {key: value for key, value in values.items() if key in known}
        config = cls(**filtered)
        if not 0 <= config.max_signals <= 20:
            raise ValueError("max_signals must be between 0 and 20")
        if not 0 <= config.max_watchlist <= 100:
            raise ValueError("max_watchlist must be between 0 and 100")
        if not 0 <= config.context_candidates <= 100:
            raise ValueError("context_candidates must be between 0 and 100")
        if config.min_quote_volume_24h < 0 or config.min_open_interest_usd < 0:
            raise ValueError("liquidity thresholds must not be negative")
        if (
            config.max_spread_pct < 0
            or config.universe_max_spread_pct <= 0
            or config.universe_max_spread_pct < config.max_spread_pct
        ):
            raise ValueError("spread thresholds are invalid")
        if config.execution_notional_usdt < 0 or config.estimated_taker_fee_pct < 0:
            raise ValueError("execution-cost assumptions must not be negative")
        if (
            config.max_execution_cost_to_risk_pct <= 0
            or config.max_slippage_pct <= 0
            or config.max_entry_extension_atr <= 0
            or config.severe_entry_extension_atr <= config.max_entry_extension_atr
        ):
            raise ValueError("execution and entry-risk limits must be positive")
        if not 1 <= config.early_signal_max_age_bars <= 5:
            raise ValueError("early_signal_max_age_bars must be between 1 and 5")
        if (
            config.entry_ready_max_chase_atr < 0
            or config.entry_missed_chase_atr <= config.entry_ready_max_chase_atr
            or config.entry_missed_chase_atr > config.severe_entry_extension_atr
        ):
            raise ValueError("entry chase thresholds are invalid")
        if config.stale_after_seconds < 60:
            raise ValueError("stale_after_seconds must be at least 60")
        if not 1 <= config.rate_limit_requests_per_2s <= 40:
            raise ValueError("rate_limit_requests_per_2s must be between 1 and 40")
        candle_limits = (
            config.candle_limit_1d,
            config.candle_limit_4h,
            config.candle_limit_1h,
            config.candle_limit_15m,
            config.candle_limit_5m,
        )
        if any(limit < 60 or limit > 300 for limit in candle_limits):
            raise ValueError("timeframe candle limits must be between 60 and 300")
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
