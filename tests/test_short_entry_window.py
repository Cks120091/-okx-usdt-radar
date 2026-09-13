from tests import legacy_short_entry_window_cases as _legacy
from radar.decision import build_decision_context


DurableWindowTests = _legacy.DurableWindowTests
EntryWindowTests = _legacy.EntryWindowTests
WindowIntegrationTests = _legacy.WindowIntegrationTests


class ShortContextPolicyTests(_legacy.ShortContextPolicyTests):
    def test_risk_gates_still_block_against_background(self):
        item = _legacy.complete_signal(); item['conflicts']=['4H 背景明顯反向']; item['spread_pct']=.3
        self.assertFalse(build_decision_context(item)['final']['new_entry_allowed'])

        item = _legacy.complete_signal(); item['conflicts']=['4H 背景明顯反向']; item['risk_reward']=.5; item['entry_eligibility']['remaining_rr']=.5
        rr = build_decision_context(item)
        self.assertTrue(rr['final']['new_entry_allowed'])
        self.assertNotIn('risk_reward', rr['hard_gate']['blockers'])

        item = _legacy.complete_signal(); item['data_quality']['core']='MISSING'
        self.assertFalse(build_decision_context(item)['final']['new_entry_allowed'])
        item = _legacy.complete_signal(); item['market_story']['trigger']['new_entry_suspended']=True
        self.assertFalse(build_decision_context(item)['final']['new_entry_allowed'])
