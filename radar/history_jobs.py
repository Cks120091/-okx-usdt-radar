"""On-demand single-coin 15m historical replay in one bounded worker.

The worker is user-triggered only.  It never starts from the home page, never
places orders and never writes the live signal/statistics databases.  Finished
coin snapshots are cached in a separate research SQLite database so a normal
single-coin refresh can read them without replaying seven days every time.

Long ranges are processed in bounded seven-day chunks.  Every completed chunk
is persisted before the next one starts, so a 60-minute pause, service restart
or explicit user pause can resume from the remaining chunks instead of
restarting an entire 30/90/180/270/365-day replay.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .history_single_replay import (
    CORE,
    DAY,
    INTERVALS,
    MIN_RESOLVED,
    MIN_RESOLVED_COVERAGE,
    NOTE,
    STEP,
    VERSION,
    Interrupted,
    aggregate,
    config_for_replay,
    fetch_history,
    fingerprint,
    minimum_sample_days,
    replay_symbol,
)

MAX_WALL_SECONDS = 3600
HISTORY_CHUNK_DAYS = 7
MAX_DATABASE_BYTES = 32 * 1024 * 1024
MAX_CACHED_COINS = 50
ACTIVE = {"QUEUED", "RUNNING", "WAITING_LIVE_SCAN"}
LEGACY_DEFAULT_INST = "BTC-USDT-SWAP"
_INST_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,48}-USDT-SWAP$")
ALLOWED_DAYS = (3, 7, 30, 90, 180, 270, 365)


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


def _chunk_ranges(start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    """Split one requested signal window into contiguous resumable chunks."""
    if not 0 < int(start_ms) < int(end_ms):
        raise ValueError("invalid history chunk range")
    chunk_ms = HISTORY_CHUNK_DAYS * DAY
    output: list[tuple[int, int]] = []
    cursor = int(start_ms)
    end_ms = int(end_ms)
    while cursor < end_ms:
        stop = min(end_ms, cursor + chunk_ms)
        output.append((cursor, stop))
        cursor = stop
    return output


def _init(path: Path) -> None:
    with _connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS history_jobs_v1 (
              id TEXT PRIMARY KEY,
              created_ms INTEGER NOT NULL,
              fingerprint TEXT NOT NULL,
              settings TEXT NOT NULL,
              inst_id TEXT NOT NULL,
              days INTEGER NOT NULL,
              start_ms INTEGER NOT NULL,
              end_ms INTEGER NOT NULL,
              status TEXT NOT NULL,
              control TEXT NOT NULL DEFAULT '',
              live_busy INTEGER NOT NULL DEFAULT 0,
              current_symbol TEXT NOT NULL DEFAULT '',
              total INTEGER NOT NULL DEFAULT 1,
              done INTEGER NOT NULL DEFAULT 0,
              failed INTEGER NOT NULL DEFAULT 0,
              error TEXT NOT NULL DEFAULT '',
              heartbeat_ms INTEGER NOT NULL DEFAULT 0,
              instruments TEXT NOT NULL DEFAULT '[]',
              summary TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_history_jobs_coin
              ON history_jobs_v1(inst_id, created_ms DESC);
            CREATE TABLE IF NOT EXISTS history_symbols_v1 (
              job_id TEXT NOT NULL,
              inst_id TEXT NOT NULL,
              status TEXT NOT NULL,
              result TEXT NOT NULL,
              PRIMARY KEY(job_id, inst_id)
            );
            CREATE TABLE IF NOT EXISTS history_chunks_v1 (
              job_id TEXT NOT NULL,
              inst_id TEXT NOT NULL,
              start_ms INTEGER NOT NULL,
              end_ms INTEGER NOT NULL,
              status TEXT NOT NULL,
              result TEXT NOT NULL,
              PRIMARY KEY(job_id, inst_id, start_ms)
            );
            CREATE INDEX IF NOT EXISTS idx_history_chunks_job
              ON history_chunks_v1(job_id, inst_id, start_ms);
            """
        )


def _update(path: Path, job: str, **values: Any) -> None:
    allowed = {
        "status",
        "control",
        "live_busy",
        "current_symbol",
        "total",
        "done",
        "failed",
        "error",
        "heartbeat_ms",
        "instruments",
        "summary",
    }
    if not values or not set(values) <= allowed:
        raise ValueError("invalid job update")
    with _connect(path) as connection:
        connection.execute(
            "UPDATE history_jobs_v1 SET "
            + ",".join(key + "=?" for key in values)
            + " WHERE id=?",
            (*values.values(), job),
        )


def _latest(path: Path, inst_id: str | None = None) -> dict | None:
    sql = "SELECT * FROM history_jobs_v1"
    params: tuple[Any, ...] = ()
    if inst_id:
        sql += " WHERE inst_id=?"
        params = (inst_id,)
    sql += " ORDER BY created_ms DESC, id DESC LIMIT 1"
    with _connect(path) as connection:
        row = connection.execute(sql, params).fetchone()
    return dict(row) if row else None


def _all_latest(path: Path) -> list[dict[str, Any]]:
    with _connect(path) as connection:
        rows = connection.execute(
            "SELECT * FROM history_jobs_v1 ORDER BY created_ms DESC, id DESC"
        ).fetchall()
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        item = dict(row)
        inst_id = str(item.get("inst_id") or "")
        if not inst_id or inst_id in seen:
            continue
        seen.add(inst_id)
        output.append(item)
    return output


def _merge_chunk_results(inst_id: str, results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Merge successful non-overlapping chunk outputs into one aggregate input."""
    successful = [result for result in results if result.get("status") == "OK"]
    if not successful:
        return None
    samples: list[dict[str, Any]] = []
    for result in successful:
        samples.extend(result.get("samples", []))
    samples.sort(key=lambda item: int(item.get("entry_ms") or 0))
    return {
        "inst_id": inst_id,
        "status": "OK",
        "evaluated": sum(int(result.get("evaluated", 0)) for result in successful),
        "missing_windows": sum(int(result.get("missing_windows", 0)) for result in successful),
        "eligible_windows": sum(int(result.get("eligible_windows", 0)) for result in successful),
        "episodes": sum(int(result.get("episodes", 0)) for result in successful),
        "initial_signals": sum(int(result.get("initial_signals", 0)) for result in successful),
        "reentry_signals": sum(int(result.get("reentry_signals", 0)) for result in successful),
        "actionable_signals": len(samples),
        "entry_attempts": len(samples),
        "samples": samples,
    }


def _rebuild(path: Path, job: str, complete: bool) -> None:
    with _connect(path) as connection:
        chunk_rows = connection.execute(
            "SELECT start_ms,end_ms,status,result FROM history_chunks_v1 "
            "WHERE job_id=? ORDER BY start_ms", (job,)
        ).fetchall()
        legacy_rows = connection.execute(
            "SELECT result FROM history_symbols_v1 WHERE job_id=?", (job,)
        ).fetchall()
        meta = connection.execute(
            "SELECT total,start_ms,end_ms,days,inst_id FROM history_jobs_v1 WHERE id=?",
            (job,),
        ).fetchone()
    if meta is None:
        return

    failure_results: list[dict[str, Any]] = []
    if chunk_rows:
        chunk_results = [json.loads(row["result"]) for row in chunk_rows]
        failure_results = [result for result in chunk_results if result.get("status") != "OK"]
        merged = _merge_chunk_results(str(meta["inst_id"]), chunk_results)
        results = [merged] if merged is not None else []
        done = len(chunk_rows)
        failed = len(failure_results)
    else:
        results = [json.loads(row["result"]) for row in legacy_rows]
        failure_results = [result for result in results if result.get("status") != "OK"]
        done = len(results)
        failed = len(failure_results)

    expected = max(1, (meta["end_ms"] - meta["start_ms"]) // CORE)
    successful = next(
        (
            result
            for result in results
            if result and result.get("status") == "OK" and result.get("inst_id") == meta["inst_id"]
        ),
        None,
    )
    evaluated = int(successful.get("evaluated", 0)) if successful else 0
    coverage = min(1.0, evaluated / expected)
    finished_all = done >= int(meta["total"])
    covered = bool(complete and finished_all and failed == 0 and successful is not None and coverage >= 0.95)
    summary = aggregate(results, complete=covered, days=int(meta["days"]))
    excluded = []
    if complete and successful is None:
        reason = "尚未完成本幣歷史回放"
        if failure_results:
            reason = str(failure_results[0].get("error") or failure_results[0].get("status") or reason)
        excluded.append(
            {
                "inst_id": meta["inst_id"],
                "status": failure_results[0].get("status", "ERROR") if failure_results else "PENDING",
                "reason": reason,
                "evaluated": evaluated,
            }
        )
    elif complete and failed:
        reason = str(
            failure_results[0].get("error")
            or f"{failed} 個歷史區段失敗；按續跑只會重試失敗區段"
        )
        excluded.append(
            {
                "inst_id": meta["inst_id"],
                "status": "CHUNK_ERROR",
                "reason": reason,
                "evaluated": evaluated,
            }
        )
    elif complete and not covered:
        excluded.append(
            {
                "inst_id": meta["inst_id"],
                "status": "INCOMPLETE_HISTORY",
                "reason": "15m歷史窗口完整度不足95%",
                "evaluated": evaluated,
            }
        )
    summary.update(
        scope_coverage_pct=round(coverage * 100, 1),
        covered_symbols=1 if covered else 0,
        covered_inst_ids=[meta["inst_id"]] if covered else [],
        excluded=excluded,
        chunk_days=HISTORY_CHUNK_DAYS,
        chunks_done=done,
        chunks_total=int(meta["total"]),
    )
    _update(
        path,
        job,
        done=done,
        failed=failed,
        summary=json.dumps(summary, ensure_ascii=False),
    )


def _request_scope(value: Any) -> tuple[int, str]:
    """Accept new nested UI request while keeping integer calls test/backward-safe."""
    if isinstance(value, dict):
        raw_days = value.get("days", 7)
        raw_inst = value.get("inst_id", "")
    else:
        raw_days = value
        raw_inst = LEGACY_DEFAULT_INST
    if isinstance(raw_days, bool) or not isinstance(raw_days, int) or raw_days not in ALLOWED_DAYS:
        raise ValueError("15m短線歷史更新只接受3天、7天、30天、3個月、6個月、9個月或12個月")
    inst_id = str(raw_inst or "").strip().upper()
    if not _INST_RE.fullmatch(inst_id):
        raise ValueError("請輸入正確的 USDT 永續幣種，例如 BTC-USDT-SWAP")
    return raw_days, inst_id


def _delete_jobs_for_coin(path: Path, inst_id: str) -> None:
    with _connect(path) as connection:
        ids = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM history_jobs_v1 WHERE inst_id=?", (inst_id,)
            )
        ]
        for job_id in ids:
            connection.execute("DELETE FROM history_chunks_v1 WHERE job_id=?", (job_id,))
            connection.execute("DELETE FROM history_symbols_v1 WHERE job_id=?", (job_id,))
        connection.execute("DELETE FROM history_jobs_v1 WHERE inst_id=?", (inst_id,))


def _delete_all_jobs(path: Path) -> None:
    with _connect(path) as connection:
        connection.execute("DELETE FROM history_chunks_v1")
        connection.execute("DELETE FROM history_symbols_v1")
        connection.execute("DELETE FROM history_jobs_v1")


def _prune(path: Path) -> None:
    with _connect(path) as connection:
        rows = connection.execute(
            "SELECT id FROM history_jobs_v1 WHERE status NOT IN ('QUEUED','RUNNING','WAITING_LIVE_SCAN') "
            "ORDER BY created_ms DESC, id DESC"
        ).fetchall()
        stale = [row[0] for row in rows[MAX_CACHED_COINS:]]
        for job_id in stale:
            connection.execute("DELETE FROM history_chunks_v1 WHERE job_id=?", (job_id,))
            connection.execute("DELETE FROM history_symbols_v1 WHERE job_id=?", (job_id,))
            connection.execute("DELETE FROM history_jobs_v1 WHERE id=?", (job_id,))


class HistoryManager:
    def __init__(self, runtime: Any):
        self.runtime = runtime
        # New DB name prevents the previous eight-major pooled cache from being
        # interpreted as single-coin history.
        self.path = Path(runtime.config.data_dir) / "history_single_15m_v1.sqlite3"
        self.settings = asdict(runtime.config)
        self.fingerprint = fingerprint(self.settings)
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.RLock()
        self.process: subprocess.Popen | None = None
        self._job: str | None = None
        self._closed = False
        _init(self.path)
        with _connect(self.path) as connection:
            active = connection.execute(
                "SELECT id FROM history_jobs_v1 WHERE status IN ('QUEUED','RUNNING','WAITING_LIVE_SCAN')"
            ).fetchall()
        for row in active:
            _update(
                self.path,
                row["id"],
                status="INTERRUPTED",
                control="PAUSE",
                error="服務曾重啟；已完成的歷史區段保留，請按續跑接著處理。",
            )

    def _snapshot(self, row: dict[str, Any]) -> dict[str, Any]:
        output: dict[str, Any] = {
            "id": row["id"],
            "inst_id": row["inst_id"],
            "status": row["status"],
            "days": row["days"],
            "start_ms": row["start_ms"],
            "end_ms": row["end_ms"],
            "current_symbol": row["current_symbol"],
            "total": row["total"],
            "done": row["done"],
            "failed": row["failed"],
            "error": row["error"],
            "heartbeat_ms": row["heartbeat_ms"],
            "compatible": row["fingerprint"] == self.fingerprint,
            "groups": {},
            "overall": {},
            "scope_coverage_pct": 0.0,
            "covered_symbols": 0,
            "excluded": [],
        }
        try:
            summary = json.loads(row["summary"] or "{}")
        except (TypeError, json.JSONDecodeError):
            summary = {}
        if isinstance(summary, dict):
            output.update(summary)
        if not output["compatible"]:
            output.update(
                status="VERSION_CHANGED",
                groups={},
                symbol_groups={},
                overall={},
                scope_coverage_pct=0.0,
            )
        return output

    def status(self) -> dict:
        with self.lock:
            row = _latest(self.path)
            if (
                row
                and row["status"] in ACTIVE
                and self.process is not None
                and self.process.poll() is not None
            ):
                _update(
                    self.path,
                    row["id"],
                    status="INTERRUPTED",
                    error="本幣歷史工作程序停止；已完成區段保留，可續跑剩餘區段。",
                )
                row = _latest(self.path)

            base: dict[str, Any] = {
                "schema_version": VERSION,
                "status": "IDLE",
                "csrf": self.token,
                "source": "SINGLE_COIN_15M_PRICE_SIMULATION",
                "note": NOTE,
                "minimum_resolved": MIN_RESOLVED,
                "minimum_days": minimum_sample_days(7),
                "minimum_coverage_pct": MIN_RESOLVED_COVERAGE * 100,
                "storage_bytes": self.path.stat().st_size if self.path.exists() else 0,
                "groups": {},
                "overall": {},
                "coins": {},
                "total": 0,
                "done": 0,
                "failed": 0,
                "compatible": True,
                "holding_hours": 24,
                "scope": "單幣15m短線歷史勝率；長區間每7天保存一段，可續跑；4H／長線卡完全不使用此功能。",
                "assumptions": (
                    "每個15m收線點重跑價格核心；Episode先統計首次可進，之後須連續至少4根15m不可進再重新可進才算有效再進。"
                    "長區間以連續7天區段回放，每段自帶1天Episode暖機並保存完成結果。"
                    "固定當時SL／TP1，使用後續已收線5m判定最多24小時；未扣費，且不含完整歷史OI／CVD、"
                    "實際Bid／Ask、深度或訂單簿。"
                ),
            }
            coins: dict[str, Any] = {}
            for item in _all_latest(self.path):
                snapshot = self._snapshot(item)
                coins[item["inst_id"]] = snapshot
            base["coins"] = coins
            if not row:
                return base
            latest = self._snapshot(row)
            base.update(latest)
            base["csrf"] = self.token
            base["schema_version"] = VERSION
            base["source"] = "SINGLE_COIN_15M_PRICE_SIMULATION"
            base["note"] = NOTE
            base["coins"] = coins
            base["storage_bytes"] = self.path.stat().st_size if self.path.exists() else 0
            base["scope"] = "單幣15m短線歷史勝率；長區間每7天保存一段，可續跑；4H／長線卡完全不使用此功能。"
            base["assumptions"] = (
                "每個15m收線點重跑價格核心；Episode先統計首次可進，之後須連續至少4根15m不可進再重新可進才算有效再進。"
                "長區間以連續7天區段回放，每段自帶1天Episode暖機並保存完成結果。"
                "固定當時SL／TP1，使用後續已收線5m判定最多24小時；未扣費，且不含完整歷史OI／CVD、"
                "實際Bid／Ask、深度或訂單簿。"
            )
            return base

    def _spawn(self, job: str) -> None:
        self._job = job
        try:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "radar.history_jobs",
                    "--database",
                    str(self.path.resolve()),
                    "--job",
                    job,
                ],
                cwd=str(Path(__file__).resolve().parent.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            _update(
                self.path,
                job,
                status="ERROR",
                error="無法啟動單幣歷史程序；即時雷達不受影響。",
            )
            raise
        process = self.process
        threading.Thread(
            target=self._supervise,
            args=(job, process),
            daemon=True,
            name="single-coin-history-supervisor",
        ).start()

    def _supervise(self, job: str, process: subprocess.Popen) -> None:
        previous = None
        while not self._closed and process.poll() is None:
            busy = bool(getattr(self.runtime, "_running", False))
            scan_lock = getattr(self.runtime, "_scan_lock", None)
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
            time.sleep(0.5)

    def command(self, action: str, *, days: Any = 7, token: str = "") -> dict:
        if not secrets.compare_digest(str(token), self.token):
            raise PermissionError("操作驗證已過期，請重新整理歷史掃描頁。")
        if action not in {"start", "resume", "pause", "delete", "delete_all"}:
            raise ValueError("不支援的歷史掃描操作")
        if action == "delete_all":
            requested_days, inst_id = 7, ""
        else:
            requested_days, inst_id = _request_scope(days)

        with self.lock:
            running = self.process is not None and self.process.poll() is None
            active = _latest(self.path)
            if active and active["status"] not in ACTIVE:
                active = None
            if action == "pause":
                if running and active:
                    _update(self.path, active["id"], control="PAUSE")
                return self.status()
            if action == "delete_all":
                if running:
                    raise ValueError("請先暫停歷史更新，等工作程序結束後再清除所有歷史資料。")
                _delete_all_jobs(self.path)
                self.process = None
                self._job = None
                with _connect(self.path) as connection:
                    connection.execute("VACUUM")
                return self.status()
            if action == "delete":
                if running:
                    raise ValueError("請先暫停歷史更新，等工作程序結束後再清除此幣資料。")
                _delete_jobs_for_coin(self.path, inst_id)
                with _connect(self.path) as connection:
                    connection.execute("VACUUM")
                return self.status()
            if running:
                if (
                    active
                    and active["inst_id"] == inst_id
                    and int(active["days"]) == requested_days
                ):
                    return self.status()
                other = active["inst_id"] if active else "另一顆幣"
                raise ValueError(f"{other} 的15m歷史更新正在執行，請完成或暫停後再換幣。")
            if self._closed:
                raise ValueError("服務正在關閉")
            if self.path.stat().st_size > MAX_DATABASE_BYTES:
                raise ValueError("單幣歷史暫存已達32MB上限；請先清除不需要的幣種回測。")

            target = _latest(self.path, inst_id)
            if action == "resume":
                if not target or target["fingerprint"] != self.fingerprint:
                    raise ValueError("這顆幣沒有可續跑的同版本工作")
                if target["status"] in {"COMPLETE", "PARTIAL_COMPLETE"}:
                    return self.status()
                _update(
                    self.path,
                    target["id"],
                    status="QUEUED",
                    control="",
                    live_busy=0,
                    error="",
                )
                self._spawn(target["id"])
                return self.status()

            if target and target["status"] in {"PAUSED", "INTERRUPTED", "ERROR"}:
                raise ValueError("這顆幣已有未完成歷史工作；請按續跑，或先清除後重新更新。")

            # A fresh user update replaces only this coin's old completed cache.
            if target:
                _delete_jobs_for_coin(self.path, inst_id)
            _prune(self.path)

            now = int(time.time() * 1000)
            end = (now // CORE * CORE) - DAY - STEP
            end = end // CORE * CORE
            start = end - requested_days * DAY
            chunks = _chunk_ranges(start, end)
            job = uuid.uuid4().hex
            with _connect(self.path) as connection:
                connection.execute(
                    """INSERT INTO history_jobs_v1
                    (id,created_ms,fingerprint,settings,inst_id,days,start_ms,end_ms,status,total)
                    VALUES(?,?,?,?,?,?,?,?, 'QUEUED',?)""",
                    (
                        job,
                        now,
                        self.fingerprint,
                        json.dumps(self.settings),
                        inst_id,
                        requested_days,
                        start,
                        end,
                        len(chunks),
                    ),
                )
            self._spawn(job)
            return self.status()

    def close(self) -> None:
        self._closed = True
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                if self._job:
                    _update(self.path, self._job, control="PAUSE")
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.terminate()


def run_job(path: Path, job: str) -> None:
    # Resource limits apply only to the research child, never the web service.
    if hasattr(os, "nice"):
        os.nice(15)
    try:
        import resource

        resource.setrlimit(
            resource.RLIMIT_AS,
            (256 * 1024 * 1024, 256 * 1024 * 1024),
        )
    except (ImportError, ValueError, OSError):
        pass

    from .api import OKXPublicClient
    from .models import Instrument

    started = time.monotonic()
    last_check = [0.0]
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT * FROM history_jobs_v1 WHERE id=?", (job,)
        ).fetchone()
    if row is None:
        return
    meta = dict(row)
    settings = json.loads(meta["settings"])
    if meta["fingerprint"] != fingerprint(settings):
        _update(path, job, status="ERROR", error="程式指紋已改變，未混用舊回測。")
        return

    def checkpoint() -> None:
        if time.monotonic() - last_check[0] < 0.2:
            return
        while True:
            with _connect(path) as connection:
                control = connection.execute(
                    "SELECT control,live_busy FROM history_jobs_v1 WHERE id=?", (job,)
                ).fetchone()
            if control is None or control["control"] == "PAUSE":
                raise Interrupted("使用者暫停；已完成區段保留，續跑會接剩餘區段。")
            if time.monotonic() - started >= MAX_WALL_SECONDS:
                raise Interrupted("單次工作已達60分鐘保護上限；已完成區段保留，按續跑接剩餘區段。")
            if not control["live_busy"]:
                break
            _update(
                path,
                job,
                status="WAITING_LIVE_SCAN",
                heartbeat_ms=int(time.time() * 1000),
            )
            time.sleep(0.5)
        _update(path, job, status="RUNNING", heartbeat_ms=int(time.time() * 1000))
        last_check[0] = time.monotonic()

    try:
        client = OKXPublicClient(
            base_url=settings.get("okx_base_url", "https://openapi.okx.com"),
            timeout_seconds=8,
            retries=1,
            rate_limit_requests=4,
        )
        checkpoint()
        saved = json.loads(meta["instruments"])
        if not saved:
            instruments = client.get_usdt_swap_instruments()
            by_id = {item.inst_id: item for item in instruments}
            instrument = by_id.get(meta["inst_id"])
            if instrument is None:
                raise ValueError(f"OKX目前沒有可用的 {meta['inst_id']} USDT永續合約")
            saved = [asdict(instrument)]
            _update(path, job, instruments=json.dumps(saved))

        chunks = _chunk_ranges(int(meta["start_ms"]), int(meta["end_ms"]))
        _update(path, job, total=len(chunks))
        cfg = config_for_replay(settings)
        limits = {
            "4H": cfg.candle_limit_4h,
            "1H": cfg.candle_limit_1h,
            "15m": cfg.candle_limit_15m,
            "5m": max(288, cfg.candle_limit_5m),
        }

        for raw in saved:
            instrument = Instrument(**raw)
            with _connect(path) as connection:
                completed_chunks = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT start_ms FROM history_chunks_v1 "
                        "WHERE job_id=? AND inst_id=? AND status='OK'",
                        (job, instrument.inst_id),
                    )
                }
            _update(path, job, current_symbol=instrument.inst_id)
            for chunk_start, chunk_end in chunks:
                if chunk_start in completed_chunks:
                    continue
                checkpoint()
                if path.stat().st_size > MAX_DATABASE_BYTES:
                    raise Interrupted("單幣歷史暫存達32MB保護上限；請清除不需要的研究資料。")
                try:
                    histories: dict[str, Any] = {}
                    for tf, interval in INTERVALS.items():
                        begin = chunk_start - DAY - (limits[tf] + 2) * interval
                        finish = (
                            chunk_end + DAY + STEP
                            if tf == "5m"
                            else chunk_end
                        )
                        histories[tf] = fetch_history(
                            client,
                            instrument.inst_id,
                            tf,
                            begin,
                            finish,
                            checkpoint,
                        )
                    if any(not rows for rows in histories.values()):
                        raise ValueError("必要週期歷史為空")
                    result = replay_symbol(
                        instrument,
                        histories,
                        chunk_start,
                        chunk_end,
                        settings,
                        checkpoint,
                    )
                except Interrupted:
                    raise
                except (Exception, MemoryError) as exc:
                    result = {
                        "inst_id": instrument.inst_id,
                        "status": "ERROR",
                        "samples": [],
                        "error": (type(exc).__name__ + ": " + str(exc))[:240],
                    }
                result["chunk_start_ms"] = chunk_start
                result["chunk_end_ms"] = chunk_end
                with _connect(path) as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO history_chunks_v1 "
                        "(job_id,inst_id,start_ms,end_ms,status,result) VALUES(?,?,?,?,?,?)",
                        (
                            job,
                            instrument.inst_id,
                            chunk_start,
                            chunk_end,
                            result["status"],
                            json.dumps(result, ensure_ascii=False),
                        ),
                    )
                _rebuild(path, job, complete=False)

        _rebuild(path, job, complete=True)
        with _connect(path) as connection:
            row = connection.execute(
                "SELECT summary,failed,done,total FROM history_jobs_v1 WHERE id=?", (job,)
            ).fetchone()
        summary = json.loads(row["summary"]) if row else {}
        excluded = summary.get("excluded", [])
        failed = int(row["failed"]) if row else 0
        if failed:
            _update(
                path,
                job,
                status="ERROR",
                error=f"{failed} 個歷史區段失敗；已完成區段保留，按續跑只重試失敗區段。",
                current_symbol="",
                heartbeat_ms=int(time.time() * 1000),
            )
        else:
            _update(
                path,
                job,
                status="PARTIAL_COMPLETE" if excluded else "COMPLETE",
                error="",
                current_symbol="",
                heartbeat_ms=int(time.time() * 1000),
            )
    except Interrupted as exc:
        _rebuild(path, job, complete=False)
        _update(path, job, status="PAUSED", error=str(exc), current_symbol="")
    except (Exception, MemoryError) as exc:
        _rebuild(path, job, complete=False)
        _update(
            path,
            job,
            status="ERROR",
            error=(type(exc).__name__ + ": " + str(exc))[:240],
            current_symbol="",
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Explicit single-coin 15m historical replay worker (no orders)"
    )
    parser.add_argument("--database", required=True)
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    run_job(Path(args.database), args.job)
