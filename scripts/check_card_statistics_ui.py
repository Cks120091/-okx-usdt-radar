"""Offline browser checks of the actual card-statistics component."""
from pathlib import Path
import json
import shutil
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]

def main():
    html=(ROOT/'radar/static/pages.html').read_text()
    sufficient={'schema_version':'CARD_STATISTICS_V1','status':'AVAILABLE','resolved':50,
                'wins':31,'losses':19,'mature':50,'days':5,'coverage_pct':100,'rate_pct':62,
                'interval_pct':[48.1,74.1],'as_of_ms':1789088400000,'period_start_ms':1788000000000,
                'cohort_label':'15m 多｜回踩續走｜逆高週期背景｜2–<3R',
                'pending':0,'unknown':0,'timeout':0,'immature':3,'holding_hours':24}
    cases=[('missing',{},'樣本不足'),('quality',{'score':99},'樣本不足'),
           ('ready',sufficient,'62%'),
           ('small',{**sufficient,'resolved':49,'wins':30},'樣本不足'),
           ('one-day',{**sufficient,'days':1},'樣本不足'),
           ('gap',{**sufficient,'status':'LOW_COVERAGE'},'可判定比例不足'),
           ('bad-rate',{**sufficient,'rate_pct':None},'樣本不足'),
           ('wrong-count',{**sufficient,'wins':45},'樣本不足'),
           ('legacy',{**sufficient,'schema_version':'OLD'},'樣本不足'),
           ('injected',{**sufficient,'cohort_label':'<img src=x onerror="window.__xss=1">'},'62%')]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,executable_path=shutil.which('chromium') or shutil.which('google-chrome'),args=['--no-sandbox'])
        for w,h in ((320,740),(360,800),(390,844),(430,932),(768,1024),(1280,900),(844,390)):
            context=browser.new_context(viewport={'width':w,'height':h},service_workers='block')
            page=context.new_page();errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.route('**/*',lambda route:route.abort())
            page.evaluate("window.fetch=async()=>new Response(JSON.stringify({enabled:false,available:false,has_report:false}),{status:200,headers:{'Content-Type':'application/json'}})")
            page.set_content(html,wait_until='domcontentloaded')
            for name,data,expected in cases:
                item={'direction':'LONG','radar_horizon':'SHORT','historical_performance':data,
                      'entry_eligibility':{'status':'WAIT_RETEST','new_entry_allowed':False},'actionable':False}
                result=page.evaluate('''item=>{const before=JSON.stringify(item);let host=document.getElementById('test-stat');if(!host){host=document.createElement('div');host.id='test-stat';document.body.append(host)}host.innerHTML=historicalStatsPanel(item);return JSON.stringify(item)===before}''',item)
                assert result, name
                panel=page.locator('#test-stat .history-stats-panel')
                assert panel.locator('[data-history-rate]').inner_text()==expected,(name,panel.inner_text())
                panel.locator('summary').click()
                assert panel.evaluate('(e)=>e.scrollWidth<=e.clientWidth+1'),(name,w)
                assert '不改進場資格' in panel.inner_text()
                assert not page.evaluate('Boolean(window.__xss)')
                assert not panel.locator('img').count()
            assert not errors,errors
            context.close()
            print(f'Card statistics {w}x{h}: {len(cases)} cases passed')
        browser.close()
    print('PASS: 70 offline statistics cases; insufficient/coverage/legacy/escaping and no input mutation.')

if __name__=='__main__': main()
