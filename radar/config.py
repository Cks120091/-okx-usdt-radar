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
    min_quote_volume_24h: float = 1_000_000.0
    max_spread_pct: float = 0.25
    minimum_rr: float = 1.8
    context_candidates: int = 30
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
            "RADAR_MIN_RR": ("minimum_rr", float),
            "RADAR_CONTEXT_CANDIDATES": ("context_candidates", int),
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
