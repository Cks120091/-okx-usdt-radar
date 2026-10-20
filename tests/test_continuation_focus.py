"""No exchange or network required: continuation-only signal selection."""
import unittest
from types import SimpleNamespace

from radar.continuation_focus import (
    is_continuation_focus, is_continuation_promotion,
)


def event(original, stage="CONFIRMED", new_type=None, triggered=True,
          confirmation_ts=1800000000000):
    # Simulate the repository's accepted REENTRY metadata only for
    # genuinely confirmed distinct continuation events.
    accepted = (
        original != "CONTINUATION"
        and stage == "REENTRY"
        and new_type == "CONTINUATION"
        and triggered is True
        and isinstance(confirmation_ts, int)
        and confirmation_ts > 0
    )
    return SimpleNamespace(
        trigger_type=original,
        signal_stage=stage,
        lifecycle={"continuation_promotion_confirmation_ms": confirmation_ts} if accepted else {},
        market_story={"trigger": {
            "type": new_type or original,
            "triggered": triggered,
            "confirmation_ts": confirmation_ts,
        }},
    )


class ContinuationFocusTests(unittest.TestCase):
    def test_original_continuation_main_signal(self):
        self.assertTrue(is_continuation_focus(event("CONTINUATION")))
        self.assertFalse(is_continuation_promotion(event("CONTINUATION")))

    def test_breakouts_and_reversals_remain_backup(self):
        for original in ("BREAKOUT", "BREAKOUT_RETEST", "REVERSAL", "NONE"):
            with self.subTest(original=original):
                self.assertFalse(is_continuation_focus(event(original)))

    def test_later_independent_reentry_promotes_backup(self):
        for original in ("BREAKOUT", "BREAKOUT_RETEST", "REVERSAL"):
            with self.subTest(original=original):
                signal = event(original, "REENTRY", "CONTINUATION")
                self.assertTrue(is_continuation_focus(signal))
                self.assertTrue(is_continuation_promotion(signal))

    def test_no_fake_promotion_without_accepted_new_event(self):
        cases = [
            event("BREAKOUT", "CONFIRMED", "CONTINUATION"),
            event("BREAKOUT", "EARLY_SIGNAL", "CONTINUATION"),
            event("BREAKOUT", "REENTRY", "CONTINUATION", triggered=False),
            event("BREAKOUT", "REENTRY", "CONTINUATION", confirmation_ts=0),
            event("BREAKOUT", "REENTRY", "CONTINUATION", confirmation_ts="nan"),
        ]
        for index, signal in enumerate(cases):
            with self.subTest(case=index):
                self.assertFalse(is_continuation_focus(signal))


if __name__ == "__main__":
    unittest.main()
