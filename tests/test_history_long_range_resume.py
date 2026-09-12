"""Regression tests for resumable 14/30-day single-coin history jobs."""
import resource
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from radar.config import AppConfig
from radar.history_jobs import (
    HistoryManager,
    _chunk_ranges,
    _connect,
    _rebuild,
    _update,
    run_job,
)
from radar.history_replay import CORE, DAY, Interrupted
from radar.models import Instrument


class LongRangeChunkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        runtime = SimpleNamespace(
            config=AppConfig(data_dir=self.temp.name),
            _running=False,
            _scan_lock=threading.RLock(),
        )
        self.manager = HistoryManager(runtime)
        self.addCleanup(self.manager.close)

    def _start(self, days=30, inst_id="SOL-USDT-SWAP"):
        with patch.object(self.manager, "_spawn"):
            return self.manager.command(
                "start",
                days={"days": days, "inst_id": inst_id},
                token=self.manager.token,
            )

    def test_supported_resumable_ranges_split_into_seven_day_chunks(self):
        start = 1_800_000_000_000 // DAY * DAY
        self.assertEqual(len(_chunk_ranges(start, start + 7 * DAY)), 1)
        self.assertEqual(len(_chunk_ranges(start, start + 14 * DAY)), 2)
        self.assertEqual(len(_chunk_ranges(start, start + 30 * DAY)), 5)

    def test_job_progress_is_persisted_per_chunk(self):
        status = self._start(14)
        job = status["id"]
        chunks = _chunk_ranges(status["start_ms"], status["end_ms"])
        self.assertEqual(status["total"], 2)

        chunk_start, chunk_end = chunks[0]
        result = {
            "inst_id": "SOL-USDT-SWAP",
            "status": "OK",
            "evaluated": (chunk_end - chunk_start) // CORE,
            "missing_windows": 0,
            "eligible_windows": 0,
            "episodes": 0,
            "initial_signals": 0,
            "reentry_signals": 0,
            "actionable_signals": 0,
            "entry_attempts": 0,
            "samples": [],
        }
        import json
        with _connect(self.manager.path) as connection:
            connection.execute(
                "INSERT INTO history_chunks_v1 "
                "(job_id,inst_id,start_ms,end_ms,status,result) VALUES(?,?,?,?,?,?)",
                (job, "SOL-USDT-SWAP", chunk_start, chunk_end, "OK", json.dumps(result)),
            )
        _rebuild(self.manager.path, job, complete=False)
        coin = self.manager.status()["coins"]["SOL-USDT-SWAP"]
        self.assertEqual(coin["done"], 1)
        self.assertEqual(coin["total"], 2)
        self.assertEqual(coin["chunks_done"], 1)
        self.assertEqual(coin["chunks_total"], 2)

    def test_resume_skips_completed_chunks_instead_of_restarting_30_days(self):
        status = self._start(30)
        job = status["id"]
        instrument = Instrument("SOL-USDT-SWAP", "live", "USDT", "linear", 0.01)

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def get_usdt_swap_instruments(self):
                return [instrument]

        def successful_replay(_instrument, _histories, start, end, _settings, _checkpoint):
            return {
                "inst_id": instrument.inst_id,
                "status": "OK",
                "evaluated": (end - start) // CORE,
                "missing_windows": 0,
                "eligible_windows": 0,
                "episodes": 0,
                "initial_signals": 0,
                "reentry_signals": 0,
                "actionable_signals": 0,
                "entry_attempts": 0,
                "samples": [],
            }

        first_calls = {"count": 0}

        def interrupt_on_third(*args, **kwargs):
            first_calls["count"] += 1
            if first_calls["count"] == 3:
                raise Interrupted("test pause")
            return successful_replay(*args, **kwargs)

        common = (
            patch("radar.api.OKXPublicClient", FakeClient),
            patch("radar.history_jobs.fetch_history", return_value=[object()]),
            patch("os.nice"),
            patch.object(resource, "setrlimit"),
        )
        with common[0], common[1], common[2], common[3], patch(
            "radar.history_jobs.replay_symbol", side_effect=interrupt_on_third
        ):
            run_job(self.manager.path, job)

        with _connect(self.manager.path) as connection:
            first_rows = connection.execute(
                "SELECT start_ms,status FROM history_chunks_v1 WHERE job_id=? ORDER BY start_ms",
                (job,),
            ).fetchall()
        self.assertEqual(len(first_rows), 2)
        self.assertTrue(all(row["status"] == "OK" for row in first_rows))

        _update(
            self.manager.path,
            job,
            status="QUEUED",
            control="",
            live_busy=0,
            error="",
        )
        second_calls = {"count": 0}

        def count_remaining(*args, **kwargs):
            second_calls["count"] += 1
            return successful_replay(*args, **kwargs)

        with patch("radar.api.OKXPublicClient", FakeClient), patch(
            "radar.history_jobs.fetch_history", return_value=[object()]
        ), patch("os.nice"), patch.object(resource, "setrlimit"), patch(
            "radar.history_jobs.replay_symbol", side_effect=count_remaining
        ):
            run_job(self.manager.path, job)

        self.assertEqual(second_calls["count"], 3)
        final = self.manager.status()["coins"]["SOL-USDT-SWAP"]
        self.assertEqual(final["done"], 5)
        self.assertEqual(final["total"], 5)
        self.assertEqual(final["status"], "COMPLETE")
        self.assertEqual(final["scope_coverage_pct"], 100.0)


if __name__ == "__main__":
    unittest.main()
