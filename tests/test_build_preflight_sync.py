"""Assembly contract tests. No remote data or trade logic."""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('build_sync', ROOT / 'scripts/build_preflight_sync.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
MODULE = (ROOT / 'radar/static/preflight-card-sync.js').read_text()
FIXTURE = '''<html><head></head><body><script>
function decisionPanel(item){return decisionPanelBody(item)}
function suggestedAction(item){return null;}
function success(data){renderPreflight(data);applyPreflightToSignalCard(data);refreshPreflightHistoryRate();}
function fingerprint(item){return [item.historical_performance,item.timeframe_states,item.trigger_type];}
function hidePreflight(){const returnFocus=state.preflightReturnFocus;}
    function reapplyPreflightCardSnapshots(report){return report;}
    function applyPreflightToSignalCard(data){return false;}
    function renderPreflight(data){}
</script></body></html>'''

class BuildTests(unittest.TestCase):
    def test_wiring_and_idempotence(self):
        output=m.build(FIXTURE,MODULE)
        self.assertIn('decoratePreflightCard(item,decisionPanelBody(item))',output)
        self.assertIn('preflightCardSyncNotice(cardSynced)',output)
        self.assertIn('item.preflight_snapshot,item.execution_quality?.score',output)
        self.assertEqual(output.count('function applyPreflightToSignalCard('),1)
        self.assertEqual(m.build(output,MODULE),output)
    def test_missing_hook_blocks_build(self):
        with self.assertRaises(ValueError):m.build(FIXTURE.replace('function suggestedAction(item){','function other(item){'),MODULE)
    def test_duplicate_hook_blocks_build(self):
        with self.assertRaises(ValueError):m.build(FIXTURE+'function decisionPanel(item){return decisionPanelBody(item)}',MODULE)

if __name__=='__main__':unittest.main()
