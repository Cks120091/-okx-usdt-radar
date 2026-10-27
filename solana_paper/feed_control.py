"""One process-wide, fail-closed budget for public Solana market HTTP feeds.

All scanner and held-position monitors share per-provider cooldowns and short
freshness-limited caches.  Never use an expired cache to trigger a paper fill.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.request import Request, urlopen

LOG = logging.getLogger("solana_paper.market_http")


class MarketTemporarilyUnavailable(RuntimeError):
    def __init__(self, provider: str, reason: str, retry_at: float):
        self.provider = provider
        self.reason = reason
        self.retry_at = retry_at
        super().__init__("%s: %s (retry_after=%ds)" %
                         (provider, reason, max(0, int(retry_at - time.time()))))


def retry_after_seconds(header, now: float) -> float:
    if not header:
        return 0.0
    try:
        return max(0.0, float(str(header).strip()))
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(str(header))
            if not date.tzinfo:
                date = date.replace(tzinfo=dt.timezone.utc)
            return max(0.0, date.timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            return 0.0


class MarketHttpGate:
    """Serialize competing workers, budget requests and respect 429 Retry-After.

    An unexpired cache hit is available to scanning and monitoring. A 429 never
    returns stale cached prices and never triggers a simulated trade.
    """
    SOURCES = {
        "DEX_SCREENER": {"base": "https://api.dexscreener.com", "gap": 1.0},
        "GECKOTERMINAL": {"base": "https://api.geckoterminal.com/api/v2", "gap": 3.0},
    }

    def __init__(self, clock=None, fetcher=None, sleeper=None):
        self.clock = clock or time.time
        self.fetcher = fetcher or urlopen
        self.sleeper = sleeper or time.sleep
        self.lock = threading.RLock()
        self.cache: dict[tuple[str, str], tuple[float, object]] = {}
        self.state = {
            source: {"cooldown_until": 0.0, "last_request": 0.0, "failure_count": 0,
                     "http_calls": 0, "cache_hits": 0, "blocked_calls": 0,
                     "last_error": "", "last_success": 0.0, "last_rate_limit": 0.0}
            for source in self.SOURCES
        }

    def _backoff(self, source, now, reason, retry_header="", rate_limited=False):
        st = self.state[source]
        st["failure_count"] += 1
        base = 300 if rate_limited else 60
        delay = min(3600, base * 2 ** min(st["failure_count"] - 1, 4))
        delay = max(delay, min(7200, retry_after_seconds(retry_header, now)))
        st["cooldown_until"] = now + delay
        st["last_error"] = reason
        if rate_limited:
            st["last_rate_limit"] = now
        LOG.warning("%s unavailable: %s; cooldown %.0fs", source, reason, delay)
        return MarketTemporarilyUnavailable(source, reason, now + delay)

    def get(self, source: str, path: str, headers: dict, ttl: int, max_bytes=2_000_000):
        if source not in self.SOURCES or not path.startswith("/") or path.startswith("//"):
            raise ValueError("Untrusted market API provider/path")
        key = (source, path)
        with self.lock:
            now = self.clock()
            st = self.state[source]
            # Once the provider is rate-limited, never treat even a short
            # cached snapshot as confirmation for a NEW simulated entry.
            if now < st["cooldown_until"]:
                st["blocked_calls"] += 1
                raise MarketTemporarilyUnavailable(source, st["last_error"] or "cooldown", st["cooldown_until"])
            cached = self.cache.get(key)
            if cached and now < cached[0]:
                st["cache_hits"] += 1
                return cached[1]
            gap = self.SOURCES[source]["gap"]
            if st["last_request"] and now < st["last_request"] + gap:
                self.sleeper(st["last_request"] + gap - now)
            now = self.clock()
            # Global per-provider time budget across scanner + position monitor.
            st["last_request"] = now
            st["http_calls"] += 1
            request = Request(self.SOURCES[source]["base"] + path, headers=headers)
            try:
                with self.fetcher(request, timeout=12) as response:
                    raw = response.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise ValueError("Market response too large")
                decoded = json.loads(raw)
            except HTTPError as exc:
                if exc.code in (403, 429, 500, 502, 503, 504):
                    retry = exc.headers.get("Retry-After", "") if exc.headers is not None else ""
                    raise self._backoff(source, now, "HTTP %s" % exc.code, retry,
                                        rate_limited=exc.code in (403, 429)) from exc
                raise self._backoff(source, now, "HTTP %s" % exc.code) from exc
            except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                raise self._backoff(source, now, type(exc).__name__) from exc
            st["last_success"] = now
            st["last_error"] = ""
            st["failure_count"] = 0
            st["cooldown_until"] = 0.0
            self.cache[key] = (now + max(0, ttl), decoded)
            # Keep memory bounded and prefer fresh prices over unbounded history.
            if len(self.cache) > 128:
                for stale_key, item in list(self.cache.items()):
                    if self.clock() >= item[0]:
                        del self.cache[stale_key]
                if len(self.cache) > 128:
                    oldest = min(self.cache, key=lambda k: self.cache[k][0])
                    del self.cache[oldest]
            return decoded

    def snapshot(self):
        # Health/dashboard requests must not wait behind a slow upstream HTTP
        # request while the scan/monitor worker holds the gateway mutex.
        if not self.lock.acquire(blocking=False):
            return {"gateway": {"state": "FETCH_IN_PROGRESS", "last_error": "",
                                "http_calls": 0, "cache_hits": 0}}
        try:
            now = self.clock()
            return {
                source: {
                    **{k: v for k, v in st.items()
                       if k in ("http_calls", "cache_hits", "blocked_calls", "last_error",
                                "last_success", "last_rate_limit")},
                    "retry_in_seconds": max(0, int(st["cooldown_until"] - now)),
                    "state": "BACKOFF" if now < st["cooldown_until"]
                              else ("OK" if st["last_success"] else "NOT_FETCHED"),
                } for source, st in self.state.items()
            }
        finally:
            self.lock.release()


SHARED_GATE = MarketHttpGate()
