"""Local event notifier for a read-only Server-Sent Events dashboard.

Events mean the background *state* changed, not that a new blockchain tick
or verified market quote arrived. A closed/sleeping Free host cannot notify.
"""
from __future__ import annotations

import threading
import time


class StateSignal:
    def __init__(self):
        self._condition = threading.Condition()
        self._revision = 0
        self._updated_at = 0.0

    def notify(self):
        with self._condition:
            self._revision += 1
            self._updated_at = time.time()
            self._condition.notify_all()
            return self._revision

    def snapshot(self):
        with self._condition:
            return {"revision": self._revision, "last_event_at": self._updated_at}

    def wait(self, after_revision, timeout=12):
        # An independent Condition wait does no RPC, polling, trading or
        # money mutation; avoids flooding the API with status JSON.
        with self._condition:
            if self._revision <= after_revision:
                self._condition.wait_for(
                    lambda: self._revision > after_revision,
                    timeout=max(0, min(float(timeout), 15)),
                )
            return self._revision, self._revision > after_revision
