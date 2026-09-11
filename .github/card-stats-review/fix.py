"""Review-only deterministic corrections and end-to-end coverage."""
import json
from pathlib import Path
path=Path('.github/card_statistics_bundle.json')
bundle=json.loads(path.read_text())
key='scripts/check_card_statistics_ui.py'
assert bundle[key].count("'62.0%'")==2
bundle[key]=bundle[key].replace("'62.0%'","'62%'")
key='history_panel.js'
old="return v===null?'—':taiwanTime(new Date(v).toISOString())"
new="return v===null||!Number.isFinite(new Date(v).getTime())?'—':taiwanTime(new Date(v).toISOString())"
assert bundle[key].count(old)==1
bundle[key]=bundle[key].replace(old,new)
key='tests/test_card_statistics.py'
extra='''
class PublishedScannerStatisticsTests(unittest.TestCase):
    def test_final_publication_enrolls_once_preview_and_blocked_do_not(self):
        import time
        from dataclasses import replace
        from tests.test_scanner import ContextFakeClient, qualified_signal, qualified_state
        from radar.models import Ticker
        from radar.scanner import MarketScanner, ScannerConfig
        from radar.strategy import AnalysisResult
        class Client(ContextFakeClient):
            def __init__(self):
                super().__init__()
                self.instruments=self.instruments[:1]
                self.last=100.0
            def get_swap_tickers(self):
                return {x.inst_id:Ticker(x.inst_id,self.last,self.last-.01,self.last+.01,
                        int(time.time()*1000),20_000_000) for x in self.instruments}
        class Engine:
            def analyze(self,instrument,ticker,*args,**kwargs):
                s=qualified_signal(instrument.inst_id)
                s=replace(s,trigger_id='',lifecycle={'current_stage':'CONFIRMED','transition':'TECHNICAL_EVENT'},
                          market_metrics={**s.market_metrics,'last_price':ticker.last})
                return AnalysisResult(s,'qualified',qualified_state(s))
        client=Client()
        scanner=MarketScanner(client,ScannerConfig(workers=1,minimum_rr=1.5))
        scanner.engine=Engine()
        try:
            def preview(report):
                self.assertEqual(scanner.repository._connection.execute('SELECT COUNT(*) FROM card_statistics_v1').fetchone()[0],0)
            report=scanner.scan_once(scan_mode='SHORT',preview=preview)
            self.assertTrue(report.signals[0].actionable)
            rows=scanner.repository._connection.execute('SELECT * FROM card_statistics_v1').fetchall()
            self.assertEqual(len(rows),1)
            self.assertAlmostEqual(rows[0]['entry'],100.01)
            self.assertEqual(report.signals[0].historical_performance['schema_version'],'CARD_STATISTICS_V1')
            self.assertIsNone(report.signals[0].historical_performance['rate_pct'])
            client.last=104.0
            report=scanner.scan_once(scan_mode='SHORT')
            self.assertFalse(report.signals[0].actionable)
            self.assertEqual(scanner.repository._connection.execute('SELECT COUNT(*) FROM card_statistics_v1').fetchone()[0],1)
            self.assertAlmostEqual(scanner.repository._connection.execute('SELECT entry FROM card_statistics_v1').fetchone()[0],100.01)
        finally:
            scanner.repository.close()
'''
needle="if __name__ == '__main__':"
assert bundle[key].count(needle)==1
bundle[key]=bundle[key].replace(needle,extra+'\n'+needle)
path.write_text(json.dumps(bundle,ensure_ascii=False))
