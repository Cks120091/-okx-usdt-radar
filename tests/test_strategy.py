from tests import legacy_strategy_cases as _legacy
from radar.strategy import _entry_eligibility

for _name in dir(_legacy):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_legacy, _name)


class StrategyTests(_legacy.StrategyTests):
    def test_old_episode_cannot_reopen_from_live_price_without_closed_retest(self):
        base = {
            "direction": "LONG",
            "current_price": 100.5,
            "entry_low": 100.0,
            "entry_high": 101.0,
            "stop": 98.0,
            "target": 110.0,
            "atr": 2.0,
            "stage": "CONFIRMED",
            "minimum_rr": 1.8,
            "ready_max_chase_atr": 0.15,
            "missed_chase_atr": 0.50,
        }
        never_ready = _entry_eligibility(existing_episode=True, **base)
        self.assertEqual(never_ready["status"], "ENTRY_READY")
        self.assertTrue(never_ready["actionable"])
        self.assertTrue(never_ready["reentry_confirmation_required"])
        self.assertTrue(never_ready["reentry_confirmation_advisory"])

        previously_ready = _entry_eligibility(entry_ready_once=True, **base)
        self.assertEqual(previously_ready["status"], "WAIT_RETEST")
        self.assertFalse(previously_ready["actionable"])
        self.assertTrue(previously_ready["reentry_confirmation_required"])
        self.assertFalse(previously_ready["closed_retest_confirmed"])
