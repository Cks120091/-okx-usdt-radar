"""Finite, price-only 15m historical replay, isolated from the live ledger.

Historical OHLCV is not historical executable depth or proof of a fill. This
module reuses the price engine/episode and retest logic without modifying them.
It never calls a live scan and never writes observed card_statistics_v1 rows.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
import time
from dataclasses import fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .card_statistics import setup, wilson
from .decision import build_decision_context
from .models import Candle, Instrument, Ticker
from .scanner import MarketScanner, ScannerConfig

VERSION = 'HISTORY_PRICE_REPLAY_V3'
MINUTE = 60_000
STEP = 5 * MINUTE
CORE = 15 * MINUTE
DAY = 86_400_000
INTERVALS = {'5m': STEP, '15m': CORE, '1H': 3_600_000, '4H': 14_400_000}
ALLOWED_DAYS = (3, 7)
HISTORY_SYMBOLS = (
    'BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP', 'XRP-USDT-SWAP',
    'DOGE-USDT-SWAP', 'ADA-USDT-SWAP', 'LINK-USDT-SWAP', 'AVAX-USDT-SWAP',
)
MIN_RESOLVED = 50
MIN_DATES_BY_RANGE = {3: 3, 7: 5}
MIN_RESOLVED_COVERAGE = .8
MAX_PAGES = 180
NOTE = ('歷史價格核心回測；固定8支大型主要代幣，模擬 TP1 先達率，非本單機率或實盤成交勝率。'
        '不含完整歷史 OI／CVD、Bid／Ask、深度及全市場前20名排序。')


class Interrupted(RuntimeError):
    """Cooperative cancellation; not an empty successful result."""


def minimum_sample_days(days: int) -> int:
    """Display guard follows the selected short-history window."""
    return MIN_DATES_BY_RANGE.get(int(days), MIN_DATES_BY_RANGE[7])


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def config_for_replay(settings: dict[str, Any]) -> ScannerConfig:
    allowed = {field.name for field in fields(ScannerConfig)}
    values = {key: value for key, value in settings.items() if key in allowed}
    values.update(state_db_path=':memory:', workers=1)
    return ScannerConfig(**values)


def fingerprint(settings: dict[str, Any]) -> str:
    """Changing price definitions/config invalidates cached replay, not live data."""
    root = Path(__file__).parent
    digest = hashlib.sha256(VERSION.encode())
    for name in ('history_replay.py', 'strategy.py', 'market_story.py', 'indicators.py',
                 'entry_window.py', 'decision.py', 'repository.py', 'scanner.py'):
        digest.update((root / name).read_bytes())
    cfg = config_for_replay(settings)
    relevant = {field.name: getattr(cfg, field.name) for field in fields(cfg)
                if field.name not in {'workers', 'state_db_path', 'previous_open_interest_usd'}}
    digest.update(json.dumps(relevant, sort_keys=True, allow_nan=False).encode())
    return digest.hexdigest()


def valid_bar(bar: Candle, interval: int) -> bool:
    return (bar.confirmed and bar.ts > 0 and bar.ts % interval == 0
            and all(math.isfinite(v) for v in (bar.open, bar.high, bar.low, bar.close,
                                               bar.volume, bar.quote_volume))
            and 0 < bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
            and bar.volume >= 0 and bar.quote_volume >= 0)


def fetch_history(client: Any, inst_id: str, bar: str, start: int, end: int,
                  checkpoint: Callable[[], None] = lambda: None) -> list[Candle]:
    """Paginate actual OKX data. Never interpolate holes or replace with today."""
    interval = INTERVALS[bar]
    if not 0 < start < end:
        raise ValueError('invalid history range')
    cursor, output, conflicts = end, {}, set()
    for _ in range(MAX_PAGES):
        checkpoint()
        rows = client._get('/api/v5/market/history-candles',
                           {'instId': inst_id, 'bar': bar, 'after': str(cursor), 'limit': 300},
                           request_retries=1, request_timeout_seconds=8.0)
        if not rows:
            break
        stamps = []
        for raw in rows:
            if not isinstance(raw, list) or len(raw) < 9:
                continue
            try:
                stamp = int(raw[0])
                stamps.append(stamp)
                candle = Candle(stamp, *(float(value) for value in raw[1:6]),
                                float(raw[7]), str(raw[8]) == '1')
            except (ValueError, TypeError, OverflowError):
                continue
            if not valid_bar(candle, interval) or stamp < start or stamp + interval > end:
                continue
            if stamp in output and output[stamp] != candle:
                conflicts.add(stamp)
            output[stamp] = candle
        if not stamps:
            raise ValueError('歷史接口未回傳可辨認的时间點')
        oldest = min(stamps)
        if oldest >= cursor:
            raise ValueError('歷史分頁沒有向前推進')
        if oldest <= start:
            break
        cursor = oldest
    else:
        raise ValueError('歷史分頁超過安全上限；未宣稱資料完整')
    return [output[ts] for ts in sorted(output) if ts not in conflicts]


def past_window(rows: list[Candle], closes: list[int], asof: int,
                interval: int, limit: int) -> list[Candle]:
    stop = bisect.bisect_right(closes, asof)
    selected = rows[max(0, stop - limit):stop]
    expected_end = asof // interval * interval
    if (len(selected) < 60 or not selected or selected[-1].ts + interval != expected_end
            or any(not valid_bar(bar, interval) for bar in selected)
            or any(b.ts - a.ts != interval for a, b in zip(selected, selected[1:]))):
        return []
    return selected


def classify_path(direction: str, entry: float, stop: float, target: float,
                  entry_ms: int, bars: dict[int, Candle]) -> dict[str, Any]:
    """One immutable plan, full 5m paths. Both barriers in one bar is unknown."""
    long = direction == 'LONG'
    risk = entry - stop if long else stop - entry
    reward = target - entry if long else entry - target
    if direction not in {'LONG', 'SHORT'} or risk <= 0 or reward <= 0 or entry_ms % STEP:
        raise ValueError('invalid simulated plan')
    for ts in range(entry_ms, entry_ms + DAY, STEP):
        bar = bars.get(ts)
        if bar is None or not valid_bar(bar, STEP):
            return {'outcome': 'UNKNOWN', 'reason': '5m路徑缺失', 'r': None}
        hit_sl = bar.low <= stop if long else bar.high >= stop
        hit_tp = bar.high >= target if long else bar.low <= target
        if hit_sl and hit_tp:
            return {'outcome': 'UNKNOWN', 'reason': '同根5m同時觸及TP／SL', 'r': None}
        if hit_sl:
            return {'outcome': 'SL_FIRST', 'r': -1.0}
        if hit_tp:
            return {'outcome': 'TP1_FIRST', 'r': reward / risk}
    close = bars[entry_ms + DAY - STEP].close
    return {'outcome': 'TIMEOUT', 'r': (close - entry) / risk * (1 if long else -1)}


def _price_projection(scanner: MarketScanner, signal: Any, asof: int, price: float) -> Any:
    """Simulation-only permission in an isolated in-memory repository.

    This is NOT a reconstruction of unavailable historical execution gates.
    Price location/retest and closed price conflict rules remain unchanged.
    """
    item = replace(signal, market_metrics={**signal.market_metrics,
                  'last_price': price, 'entry_execution_price': price,
                  'entry_execution_price_source': 'SIMULATED_5M_OPEN',
                  'ticker_sampled_at': asof})
    item = scanner._refresh_entry_eligibility(item)
    decision = build_decision_context(item, scanner.config)
    stop, target = float(item.stop_loss), float(item.take_profit_1)
    risk = price - stop if item.direction == 'LONG' else stop - price
    reward = target - price if item.direction == 'LONG' else price - target
    rr = reward / risk if risk > 0 else None
    allowed = (item.actionable is True and isinstance(rr, (int, float))
               and math.isfinite(rr) and rr >= scanner.config.minimum_rr
               and not decision.get('conflict', {}).get('blocks_entry')
               and not item.lifecycle.get('terminal'))
    return replace(item, actionable=allowed,
                   entry_eligibility={**item.entry_eligibility, 'actionable': allowed,
                                      'new_entry_allowed': allowed},
                   decision_context={'simulation_only': True,
                                     'final': {'status': 'ENTER' if allowed else 'WAIT',
                                               'new_entry_allowed': allowed}})


def delayed_attempt(scanner: MarketScanner, item: Any, asof: int,
                    prices: dict[int, Candle]) -> tuple[Any | None, str]:
    """Retry on later core scans instead of discarding the Episode's first wait."""
    next_bar, delay = prices.get(asof + STEP), prices.get(asof)
    if next_bar is None or delay is None:
        return None, 'MISSING_ENTRY_PATH'
    stop, target = float(item.stop_loss), float(item.take_profit_1)
    long = item.direction == 'LONG'
    if ((delay.low <= stop or delay.high >= target) if long
            else (delay.high >= stop or delay.low <= target)):
        return None, 'BARRIER_BEFORE_ENTRY'
    projected = _price_projection(scanner, item, asof + STEP, next_bar.open)
    return (projected, 'FILLED_REFERENCE') if projected.actionable else (None, 'WAIT')


def replay_symbol(instrument: Instrument, histories: dict[str, list[Candle]],
                  start: int, end: int, settings: dict[str, Any],
                  checkpoint: Callable[[], None] = lambda: None) -> dict[str, Any]:
    """Scan every 15m chronologically; keep waiting/retest/episode state."""
    scanner = MarketScanner(None, config_for_replay(settings))
    cfg = scanner.config
    limits = {'4H': cfg.candle_limit_4h, '1H': cfg.candle_limit_1h,
              '15m': cfg.candle_limit_15m, '5m': max(288, cfg.candle_limit_5m)}
    closes = {tf: [bar.ts + INTERVALS[tf] for bar in rows] for tf, rows in histories.items()}
    prices = {bar.ts: bar for bar in histories['5m']}
    entered, episodes, samples = set(), set(), []
    attempted, checked, missing, eligible_count = 0, 0, 0, 0
    membership = False
    replay_start = start - DAY  # deterministic warmup for episode/universe state
    try:
        for iteration, asof in enumerate(range(replay_start, end, CORE)):
            if iteration % 8 == 0:
                checkpoint()
                time.sleep(.002)  # yield to the live web process; no scan thread shares this CPU
            if instrument.list_time and asof < instrument.list_time:
                continue
            bundle = {tf: past_window(histories[tf], closes[tf], asof, INTERVALS[tf], limit)
                      for tf, limit in limits.items()}
            if not all(bundle.values()):
                if asof >= start:
                    missing += 1
                continue
            if asof >= start:
                checked += 1
            volume_bars = bundle['5m'][-288:]
            if len(volume_bars) < 288:
                continue
            volume = sum(bar.quote_volume for bar in volume_bars)
            threshold = (max(0, cfg.min_quote_volume_24h - cfg.quote_volume_buffer_24h)
                         if membership else cfg.min_quote_volume_24h)
            membership = volume >= threshold
            if membership and asof >= start:
                eligible_count += 1
            active = scanner.repository.load_active_signal(instrument.inst_id, 'SHORT')
            if not membership and active is None:
                continue
            previous = dict(scanner.repository.load_story(instrument.inst_id, 'SHORT') or {})
            previous['allow_opposite_episode'] = True
            close = bundle['15m'][-1].close
            # Required Ticker is an explicitly modeled zero-spread price input,
            # never presented as actual historical Bid/Ask. No depth is fabricated.
            ticker = Ticker(instrument.inst_id, close, close, close, asof, volume)
            analysis = scanner.engine.analyze(instrument, ticker, bundle['4H'], bundle['1H'],
                                             bundle['15m'], bundle['5m'][-cfg.candle_limit_5m:],
                                             previous_story=previous,
                                             excursion_profile_loader=lambda direction, kind:
                                             scanner.repository.excursion_profile(instrument.inst_id, 'SHORT', direction, kind))
            states = [analysis.market_state] if analysis.market_state is not None else []
            signals = scanner.repository.reconcile(
                [analysis.signal] if analysis.signal is not None and membership else [],
                states, iso(asof), 'SHORT')
            for signal in signals:
                if signal.lifecycle.get('terminal') or not signal.trigger_id:
                    continue
                if asof >= start:
                    episodes.add(signal.trigger_id)
                signal = _price_projection(scanner, signal, asof, close)
                if not membership:
                    signal = replace(signal, actionable=False,
                        entry_eligibility={**signal.entry_eligibility, 'actionable': False,
                                           'new_entry_allowed': False})
                signal = scanner._record_entry_window(signal)
                if not signal.actionable or signal.trigger_id in entered:
                    continue
                if asof >= start:
                    attempted += 1
                filled, reason = delayed_attempt(scanner, signal, asof, prices)
                if filled is None:
                    # An actual modeled departure suspends continuity. A later
                    # in-zone quote alone cannot invent a closed retest.
                    suspended = replace(signal, actionable=False,
                        market_metrics={**signal.market_metrics, 'ticker_sampled_at': asof + STEP},
                        entry_eligibility={**signal.entry_eligibility, 'actionable': False,
                                           'new_entry_allowed': False})
                    scanner._record_entry_window(suspended)
                    continue
                filled = scanner._record_entry_window(filled)
                if not filled.actionable:
                    continue
                entered.add(signal.trigger_id)
                if asof < start:
                    continue
                entry = prices[asof + STEP].open
                spec = setup(filled, entry)
                if spec is None:
                    continue
                samples.append({'cohort': spec['cohort'], 'label': spec['label'],
                                'entry_ms': asof + STEP, 'direction': filled.direction,
                                'entry': entry, 'stop': spec['stop'], 'target': spec['target'],
                                'episode': filled.trigger_id,
                                'outcome': None})
        # Future bars are read for outcomes only AFTER chronological generation.
        for sample in samples:
            checkpoint()
            sample.update(classify_path(sample['direction'], sample['entry'], sample['stop'],
                                        sample['target'], sample['entry_ms'], prices))
        return {'inst_id': instrument.inst_id, 'status': 'OK', 'evaluated': checked,
                'missing_windows': missing, 'eligible_windows': eligible_count,
                'episodes': len(episodes), 'entry_attempts': attempted, 'samples': samples,
                'timeframes': {tf: len(rows) for tf, rows in histories.items()}}
    finally:
        scanner.repository.close()


def aggregate(results: list[dict[str, Any]], *, complete: bool, days: int = 7) -> dict[str, Any]:
    required_days = minimum_sample_days(days)

    def add_sample(groups: dict[str, Any], sample: dict[str, Any]) -> None:
        group = groups.setdefault(sample['cohort'], {'label': sample['label'], 'wins': 0,
               'losses': 0, 'timeout': 0, 'unknown': 0, 'days': set(), 'total': 0})
        group['total'] += 1
        group['days'].add(sample['entry_ms'] // DAY)
        key = {'TP1_FIRST': 'wins', 'SL_FIRST': 'losses', 'TIMEOUT': 'timeout'}.get(sample['outcome'], 'unknown')
        group[key] += 1

    def finish(groups: dict[str, Any]) -> None:
        for group in groups.values():
            group['days'] = len(group['days'])
            n = group['wins'] + group['losses']
            group['resolved'] = n
            group['coverage_pct'] = round(100 * n / group['total'], 1) if group['total'] else 0
            group['minimum_days'] = required_days
            available = (complete and n >= MIN_RESOLVED and group['days'] >= required_days
                         and n / group['total'] >= MIN_RESOLVED_COVERAGE)
            group['status'] = 'AVAILABLE' if available else 'INSUFFICIENT' if complete else 'PARTIAL'
            group['rate_pct'] = round(100 * group['wins'] / n, 1) if available else None
            group['interval_pct'] = wilson(group['wins'], n) if available else None

    groups: dict[str, Any] = {}
    symbol_groups: dict[str, dict[str, Any]] = {}
    for result in results:
        inst_id = str(result.get('inst_id') or '')
        own = symbol_groups.setdefault(inst_id, {}) if inst_id else None
        for sample in result.get('samples', []):
            add_sample(groups, sample)
            if own is not None:
                add_sample(own, sample)
    finish(groups)
    for own in symbol_groups.values():
        finish(own)
    return {'groups': groups, 'symbol_groups': symbol_groups,
            'samples': sum(group['total'] for group in groups.values()),
            'minimum_days': required_days,
            'episodes': sum(result.get('episodes', 0) for result in results),
            'entry_attempts': sum(result.get('entry_attempts', 0) for result in results),
            'missing_windows': sum(result.get('missing_windows', 0) for result in results)}
