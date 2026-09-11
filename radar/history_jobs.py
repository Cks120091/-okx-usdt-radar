"""On-demand, resumable historical scan in ONE bounded low-priority process.

No startup scan, scheduler, order submission or writes to the live SQLite DB.
HTTP requests only read small aggregate status or launch/pause an explicit job.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .history_replay import (ALLOWED_DAYS, CORE, DAY, HISTORY_SYMBOLS, INTERVALS, MIN_RESOLVED,
                             MIN_RESOLVED_COVERAGE, STEP, VERSION, NOTE, Interrupted,
                             aggregate, config_for_replay, fetch_history, fingerprint,
                             minimum_sample_days, replay_symbol)

MAX_WALL_SECONDS = 3600
MAX_DATABASE_BYTES = 32 * 1024 * 1024
ACTIVE = {'QUEUED', 'RUNNING', 'WAITING_LIVE_SCAN'}


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=3)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _init(path: Path) -> None:
    with _connect(path) as connection:
        connection.executescript('''
          CREATE TABLE IF NOT EXISTS history_jobs_v1 (
            id TEXT PRIMARY KEY, created_ms INTEGER NOT NULL, fingerprint TEXT NOT NULL,
            settings TEXT NOT NULL, days INTEGER NOT NULL, start_ms INTEGER NOT NULL,
            end_ms INTEGER NOT NULL, status TEXT NOT NULL, control TEXT NOT NULL DEFAULT '',
            live_busy INTEGER NOT NULL DEFAULT 0, current_symbol TEXT NOT NULL DEFAULT '',
            total INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
            heartbeat_ms INTEGER NOT NULL DEFAULT 0, instruments TEXT NOT NULL DEFAULT '[]',
            summary TEXT NOT NULL DEFAULT '{}');
          CREATE TABLE IF NOT EXISTS history_symbols_v1 (
            job_id TEXT NOT NULL, inst_id TEXT NOT NULL, status TEXT NOT NULL,
            result TEXT NOT NULL, PRIMARY KEY(job_id, inst_id));
        ''')


def _update(path: Path, job: str, **values: Any) -> None:
    allowed = {'status', 'control', 'live_busy', 'current_symbol', 'total', 'done', 'failed',
               'error', 'heartbeat_ms', 'instruments', 'summary'}
    if not values or not set(values) <= allowed:
        raise ValueError('invalid job update')
    with _connect(path) as connection:
        connection.execute('UPDATE history_jobs_v1 SET ' + ','.join(key + '=?' for key in values)
                           + ' WHERE id=?', (*values.values(), job))


def _latest(path: Path) -> dict | None:
    with _connect(path) as connection:
        row = connection.execute('SELECT * FROM history_jobs_v1 ORDER BY created_ms DESC, id DESC LIMIT 1').fetchone()
    return dict(row) if row else None


def _rebuild(path: Path, job: str, complete: bool) -> None:
    with _connect(path) as connection:
        rows = connection.execute('SELECT result FROM history_symbols_v1 WHERE job_id=?', (job,)).fetchall()
        meta = connection.execute('SELECT total,start_ms,end_ms,days FROM history_jobs_v1 WHERE id=?', (job,)).fetchone()
    results = [json.loads(row['result']) for row in rows]
    failed = sum(result.get('status') != 'OK' for result in results)
    expected = (meta['end_ms'] - meta['start_ms']) // CORE
    fully_covered = sum(result.get('status') == 'OK' and result.get('evaluated', 0) >= expected * .95
                        for result in results)
    scope_coverage = fully_covered / meta['total'] if meta['total'] else 0
    summary = aggregate(results, complete=complete and scope_coverage >= .8, days=int(meta['days']))
    summary.update(scope_coverage_pct=round(scope_coverage * 100, 1),
                   covered_symbols=fully_covered,
                   covered_inst_ids=[result['inst_id'] for result in results if result.get('status') == 'OK' and result.get('evaluated', 0) >= expected * .95],
                   excluded=[{'inst_id': result['inst_id'], 'status': result['status'],
                              'reason': result.get('error', '歷史窗口不足'),
                              'evaluated': result.get('evaluated', 0)}
                             for result in results if result.get('status') != 'OK'
                             or result.get('evaluated', 0) < expected * .95])
    _update(path, job, done=len(results), failed=failed, summary=json.dumps(summary, ensure_ascii=False))


class HistoryManager:
    def __init__(self, runtime: Any):
        self.runtime = runtime
        # Explicitly separate from both live episodes and observed-entry statistics.
        self.path = Path(runtime.config.data_dir) / 'history_replay_v1.sqlite3'
        self.settings = asdict(runtime.config)
        self.fingerprint = fingerprint(self.settings)
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.RLock()
        self.process: subprocess.Popen | None = None
        self._job: str | None = None
        self._closed = False
        _init(self.path)
        old = _latest(self.path)
        if old and old['status'] in ACTIVE:
            _update(self.path, old['id'], status='INTERRUPTED', control='PAUSE',
                    error='服務曾重啟；已完成標的保留，請按續跑。')

    def status(self) -> dict:
        with self.lock:
            row = _latest(self.path)
            if row and row['status'] in ACTIVE and self.process and self.process.poll() is not None:
                _update(self.path, row['id'], status='INTERRUPTED',
                        error='歷史工作程序停止；保留已完成進度，可續跑。')
                row = _latest(self.path)
            output = {'schema_version': VERSION, 'status': 'IDLE', 'csrf': self.token,
                      'source': 'HISTORICAL_PRICE_SIMULATION', 'note': NOTE,
                      'minimum_resolved': MIN_RESOLVED, 'minimum_days': minimum_sample_days(7),
                      'minimum_coverage_pct': MIN_RESOLVED_COVERAGE * 100,
                      'storage_bytes': self.path.stat().st_size if self.path.exists() else 0,
                      'groups': {}, 'total': 0, 'done': 0, 'failed': 0,
                      'compatible': True, 'holding_hours': 24}
            if not row:
                return output
            for key in ('id', 'status', 'days', 'start_ms', 'end_ms', 'current_symbol',
                        'total', 'done', 'failed', 'error', 'heartbeat_ms'):
                output[key] = row[key]
            output.update(json.loads(row['summary']))
            output['minimum_days'] = minimum_sample_days(int(row['days']))
            output['compatible'] = row['fingerprint'] == self.fingerprint
            if not output['compatible']:
                output.update(status='VERSION_CHANGED', groups={})
            # Core 15m outcome timeframe is deliberately not a 4H backtest.
            output['scope'] = ('固定8支大型主要代幣：BTC、ETH、SOL、XRP、DOGE、ADA、LINK、AVAX；'
                               '逐時點仍套用歷史24H成交額門檻，不延伸到其他小幣。')
            output['assumptions'] = ('15m收線後延遲5分鐘，以5m開盤作模擬參考；等待可於後續收線重新評估。'
                                     '固定原SL／TP1、最多24小時；5m同棒TP／SL先後不明另列。'
                                     '百分比未扣費，無歷史深度，不代表當時線上一定會放行。')
            return output

    def _spawn(self, job: str) -> None:
        self._job = job
        try:
            self.process = subprocess.Popen(
                [sys.executable, '-m', 'radar.history_jobs', '--database', str(self.path.resolve()), '--job', job],
                cwd=str(Path(__file__).resolve().parent.parent), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            _update(self.path, job, status='ERROR', error='無法啟動獨立歷史程序；即時掃描不受影響。')
            raise
        process = self.process
        threading.Thread(target=self._supervise, args=(job, process), daemon=True,
                         name='history-low-priority-supervisor').start()

    def _supervise(self, job: str, process: subprocess.Popen) -> None:
        previous = None
        while not self._closed and process.poll() is None:
            busy = bool(getattr(self.runtime, '_running', False))
            scan_lock = getattr(self.runtime, '_scan_lock', None)
            if scan_lock is not None:
                acquired = scan_lock.acquire(blocking=False)
                if acquired:
                    scan_lock.release()
                else:
                    busy = True
            if busy != previous:
                try:
                    _update(self.path, job, live_busy=int(busy))
                    previous = busy
                except sqlite3.Error:
                    pass
            time.sleep(.5)

    def command(self, action: str, *, days: Any = 7, token: str = '') -> dict:
        if not secrets.compare_digest(str(token), self.token):
            raise PermissionError('操作驗證已過期，請重新整理歷史掃描頁。')
        if action not in {'start', 'resume', 'pause', 'delete'}:
            raise ValueError('不支援的歷史掃描操作')
        with self.lock:
            row = _latest(self.path)
            running = self.process is not None and self.process.poll() is None
            if action == 'pause':
                if row and running:
                    _update(self.path, row['id'], control='PAUSE')
                return self.status()
            if action == 'delete':
                if running:
                    raise ValueError('請先暫停，等工作程序結束後才清除。')
                # User-triggered deletion removes only history-scan research data.
                with _connect(self.path) as connection:
                    connection.execute('DELETE FROM history_symbols_v1')
                    connection.execute('DELETE FROM history_jobs_v1')
                with _connect(self.path) as connection:
                    connection.execute('VACUUM')
                return self.status()
            if running:
                return self.status()  # join existing work; never start a second scan
            if self._closed:
                raise ValueError('服務正在關閉')
            if self.path.stat().st_size > MAX_DATABASE_BYTES:
                raise ValueError('歷史暫存已達32MB上限；請先清除歷史回測，不影響正式紀錄。')
            if action == 'resume':
                if not row or row['fingerprint'] != self.fingerprint:
                    raise ValueError('沒有可續跑的同版本工作')
                if row['status'] in {'COMPLETE', 'PARTIAL_COMPLETE'}:
                    return self.status()
                _update(self.path, row['id'], status='QUEUED', control='', live_busy=0, error='')
                self._spawn(row['id'])
                return self.status()
            if isinstance(days, bool) or not isinstance(days, int) or days not in ALLOWED_DAYS:
                raise ValueError('短線歷史掃描只接受3天或7天')
            if row and row['status'] in {'PAUSED', 'INTERRUPTED', 'ERROR'}:
                raise ValueError('已有未完成工作；請按續跑，或先清除再開始新工作。')
            with _connect(self.path) as connection:
                count = connection.execute('SELECT COUNT(*) FROM history_jobs_v1').fetchone()[0]
            if count >= 3:
                raise ValueError('已保留3次歷史掃描；請先清除歷史回測再建立，正式紀錄不受影響。')
            now = int(time.time() * 1000)
            end = (now // CORE * CORE) - DAY - STEP
            end = end // CORE * CORE  # leave complete outcome horizon for the last delayed entry
            job = uuid.uuid4().hex
            with _connect(self.path) as connection:
                connection.execute('''INSERT INTO history_jobs_v1
                    (id,created_ms,fingerprint,settings,days,start_ms,end_ms,status)
                    VALUES(?,?,?,?,?,?,?,'QUEUED')''',
                    (job, now, self.fingerprint, json.dumps(self.settings), days, end - days * DAY, end))
            self._spawn(job)
            return self.status()

    def close(self) -> None:
        self._closed = True
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                if self._job:
                    _update(self.path, self._job, control='PAUSE')
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.terminate()


def run_job(path: Path, job: str) -> None:
    # Resource limits apply ONLY to this child, not the web service.
    if hasattr(os, 'nice'):
        os.nice(15)
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    except (ImportError, ValueError, OSError):
        pass
    from .api import OKXPublicClient
    from .models import Instrument
    started = time.monotonic()
    last_check = [0.0]
    with _connect(path) as connection:
        row = connection.execute('SELECT * FROM history_jobs_v1 WHERE id=?', (job,)).fetchone()
    if row is None:
        return
    meta = dict(row)
    settings = json.loads(meta['settings'])
    if meta['fingerprint'] != fingerprint(settings):
        _update(path, job, status='ERROR', error='程式指紋已改變，未混用舊回測。')
        return

    def checkpoint() -> None:
        if time.monotonic() - last_check[0] < .2:
            return
        while True:
            with _connect(path) as connection:
                control = connection.execute('SELECT control,live_busy FROM history_jobs_v1 WHERE id=?', (job,)).fetchone()
            if control is None or control['control'] == 'PAUSE':
                raise Interrupted('使用者暫停；已完成進度保留。')
            if time.monotonic() - started >= MAX_WALL_SECONDS:
                raise Interrupted('單次工作已達60分鐘保護上限，按續跑可接續；不是已完成。')
            if not control['live_busy']:
                break
            _update(path, job, status='WAITING_LIVE_SCAN', heartbeat_ms=int(time.time() * 1000))
            time.sleep(.5)
        _update(path, job, status='RUNNING', heartbeat_ms=int(time.time() * 1000))
        last_check[0] = time.monotonic()

    try:
        client = OKXPublicClient(base_url=settings.get('okx_base_url', 'https://openapi.okx.com'),
                                 timeout_seconds=8, retries=1, rate_limit_requests=4)
        checkpoint()
        saved = json.loads(meta['instruments'])
        if not saved:
            instruments = client.get_usdt_swap_instruments()
            if not instruments:
                raise ValueError('未取得固定大型幣掃描範圍')
            by_id = {item.inst_id: item for item in instruments}
            missing = [inst_id for inst_id in HISTORY_SYMBOLS if inst_id not in by_id]
            if missing:
                raise ValueError('固定大型幣未完整取得：' + ', '.join(missing))
            saved = [asdict(by_id[inst_id]) for inst_id in HISTORY_SYMBOLS]
            _update(path, job, total=len(saved), instruments=json.dumps(saved))
        with _connect(path) as connection:
            done = {row[0] for row in connection.execute('SELECT inst_id FROM history_symbols_v1 WHERE job_id=?', (job,))}
        cfg = config_for_replay(settings)
        limits = {'4H': cfg.candle_limit_4h, '1H': cfg.candle_limit_1h,
                  '15m': cfg.candle_limit_15m, '5m': max(288, cfg.candle_limit_5m)}
        for raw in saved:
            if raw['inst_id'] in done:
                continue
            checkpoint()
            if path.stat().st_size > MAX_DATABASE_BYTES:
                raise Interrupted('歷史暫存達32MB保護上限；請清除研究資料後重跑。')
            instrument = Instrument(**raw)
            _update(path, job, current_symbol=instrument.inst_id)
            try:
                histories = {}
                for tf, interval in INTERVALS.items():
                    begin = meta['start_ms'] - DAY - (limits[tf] + 2) * interval
                    finish = meta['end_ms'] + DAY + STEP if tf == '5m' else meta['end_ms']
                    histories[tf] = fetch_history(client, instrument.inst_id, tf, begin, finish, checkpoint)
                if any(not rows for rows in histories.values()):
                    raise ValueError('必要週期歷史為空')
                result = replay_symbol(instrument, histories, meta['start_ms'], meta['end_ms'], settings, checkpoint)
            except Interrupted:
                raise
            except (Exception, MemoryError) as exc:
                # Isolate a failed instrument and disclose it, never a zero-loss success.
                result = {'inst_id': instrument.inst_id, 'status': 'ERROR', 'samples': [],
                          'error': (type(exc).__name__ + ': ' + str(exc))[:240]}
            with _connect(path) as connection:
                connection.execute('INSERT OR IGNORE INTO history_symbols_v1 VALUES(?,?,?,?)',
                                   (job, instrument.inst_id, result['status'], json.dumps(result, ensure_ascii=False)))
            _rebuild(path, job, complete=False)
        _rebuild(path, job, complete=True)
        with _connect(path) as connection:
            row = connection.execute('SELECT failed,summary FROM history_jobs_v1 WHERE id=?', (job,)).fetchone()
        excluded = json.loads(row['summary']).get('excluded', [])
        _update(path, job, status='PARTIAL_COMPLETE' if excluded else 'COMPLETE',
                current_symbol='', heartbeat_ms=int(time.time() * 1000))
    except Interrupted as exc:
        _update(path, job, status='PAUSED', error=str(exc), current_symbol='')
    except (Exception, MemoryError) as exc:
        _update(path, job, status='ERROR', error=(type(exc).__name__ + ': ' + str(exc))[:240])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Explicit finite historical replay worker (no orders)')
    parser.add_argument('--database', required=True)
    parser.add_argument('--job', required=True)
    args = parser.parse_args()
    run_job(Path(args.database), args.job)
