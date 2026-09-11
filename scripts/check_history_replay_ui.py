"""Synthetic browser checks for single-coin 15m history; no market calls."""
import copy
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright
from scripts.check_layout_ui import fixtures
from radar.card_statistics import setup

VERSION = 'HISTORY_SINGLE_15M_V1'


def main():
    report, items, _ = fixtures()
    item = copy.deepcopy(items[1])
    item['inst_id'] = 'MINA-USDT-SWAP'
    item['radar_horizon'] = 'SHORT'
    item['direction'] = 'LONG'
    item.update(entry_low='99', entry_high='101', stop_loss='95', take_profit_1='110',
                take_profit_2='115', trigger_type='CONTINUATION', signal_stage='EARLY_SIGNAL')
    item['market_metrics'].update(entry_execution_price=100)
    item['timeframe_states'] = {'4H': {'direction': 'LONG'}, '1H': {'direction': 'LONG'}, '15m': {'direction': 'LONG'}}
    key = setup(item, 100)['cohort']
    same = {'label':'15m 多｜回踩續走｜同向背景｜2–<3R','status':'AVAILABLE','resolved':6,
            'wins':4,'losses':2,'total':7,'days':3,'rate_pct':66.7,'unknown':1,'timeout':0,
            'tier':'極低樣本','coverage_pct':85.7,'interval_pct':[30.0,90.3]}
    coin = {'id':'job-mina','inst_id':'MINA-USDT-SWAP','status':'COMPLETE','compatible':True,'days':7,
            'start_ms':1_799_000_000_000,'end_ms':1_799_604_800_000,'total':1,'done':1,'failed':0,
            'scope_coverage_pct':100,'covered_symbols':1,'excluded':[],
            'overall':{'label':'全部15m可進訊號','status':'AVAILABLE','resolved':26,'wins':17,'losses':9,
                       'total':28,'days':7,'rate_pct':65.4,'unknown':1,'timeout':1,'tier':'中等樣本',
                       'coverage_pct':92.9,'interval_pct':[46.2,80.6]},
            'groups':{key:same}}
    good = {'schema_version':VERSION,'status':'COMPLETE','csrf':'test-only','inst_id':'MINA-USDT-SWAP',
            'days':7,'total':1,'done':1,'failed':0,'storage_bytes':1048576,
            'coins':{'MINA-USDT-SWAP':coin}}

    def document(name):
        html = (ROOT/'radar/static'/name).read_text()
        for js in ('history-replay.js','history-scan.js'):
            html = html.replace(f'<script src="/{js}"></script>', '<script>'+(ROOT/'radar/static'/js).read_text()+'</script>')
        return html.replace('<link rel="stylesheet" href="/history-replay.css">', '<style>'+(ROOT/'radar/static/history-replay.css').read_text()+'</style>')

    def install(page, data):
        page.evaluate("""({data,report})=>{
          window.__historyData=data;window.__historyPosts=[];
          window.fetch=async(path,options={})=>{
            const url=String(path);let result={enabled:false,available:false};
            if(url==='/api/history-scan/status') result=window.__historyData;
            else if(url.startsWith('/api/history-scan/')){
              const body=JSON.parse(options.body||'{}');window.__historyPosts.push({path:url,body});
              const inst=body?.days?.inst_id||window.__historyData.inst_id||'';
              const days=body?.days?.days||7;
              if(url.endsWith('/start')){
                window.__historyData={...window.__historyData,status:'RUNNING',inst_id:inst,days,total:1,done:0};
                window.__historyData.coins={...(window.__historyData.coins||{}),[inst]:{inst_id:inst,status:'RUNNING',compatible:true,days,total:1,done:0,overall:{},groups:{}}};
              }else if(url.endsWith('/pause')){
                window.__historyData={...window.__historyData,status:'PAUSED',inst_id:inst};
                if(window.__historyData.coins?.[inst]) window.__historyData.coins[inst].status='PAUSED';
              }else if(url.endsWith('/resume')){
                window.__historyData={...window.__historyData,status:'RUNNING',inst_id:inst};
                if(window.__historyData.coins?.[inst]) window.__historyData.coins[inst].status='RUNNING';
              }else if(url.endsWith('/delete')){
                const coins={...(window.__historyData.coins||{})};delete coins[inst];window.__historyData={...window.__historyData,status:'IDLE',coins};
              }
              result=window.__historyData;
            }else if(url==='/api/status')result={system_status:'FRESH',has_report:true};
            else if(url.startsWith('/api/report'))result=report;
            else if(url.startsWith('/api/history'))result={short_items:[],long_items:[]};
            return new Response(JSON.stringify(result),{status:200,headers:{'Content-Type':'application/json'}});
          };
        }""", {'data':data,'report':report})

    outputs = Path(os.environ.get('RADAR_UI_OUTPUT','/tmp/radar-history-ui'));outputs.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=shutil.which('chromium') or shutil.which('google-chrome'), args=['--no-sandbox'])
        for width,height in [(320,740),(390,844),(430,932),(768,1024),(1280,900),(844,390)]:
            context = browser.new_context(viewport={'width':width,'height':height},service_workers='block')
            page = context.new_page();page.set_default_timeout(6000);errors=[]
            page.route('**/*', lambda route: route.abort())
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.set_content(document('pages.html'), wait_until='domcontentloaded')
            install(page, copy.deepcopy(good))
            page.evaluate("item=>{renderSignals([item],'#fifteenAllBox');activateTab('fifteenAll',false)}", item)
            page.evaluate('HistoryReplay.refresh()')
            page.wait_for_function("document.querySelector('#fifteenAllBox .history-replay-card')?.textContent.includes('65.4%')")
            text = page.locator('#fifteenAllBox .history-replay-card').inner_text()
            assert 'MINA-USDT-SWAP' in text and '可進訊號 28 筆' in text and '目前同類情境' in text, text
            assert '66.7%' in text, text
            assert page.locator('#fifteenAllBox .history-stats-panel').count() == 1, 'observed-entry block must remain separate'
            assert not page.evaluate('window.__historyPosts'), 'card load must not start replay'

            btc = copy.deepcopy(item);btc['inst_id']='BTC-USDT-SWAP'
            btc_html = page.evaluate('item=>HistoryReplay.card(item)', btc)
            assert '尚未更新' in btc_html and '65.4%' not in btc_html, btc_html
            long_item = copy.deepcopy(item);long_item['radar_horizon']='LONG'
            assert page.evaluate('item=>HistoryReplay.card(item)', long_item) == '', '4H/long card must not use 15m history'
            assert page.evaluate('item=>HistoryReplay.card(item,true)', item) == ''
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'), width
            assert not errors, errors

            page.close();page=context.new_page();page.set_default_timeout(6000);errors=[]
            page.route('**/*', lambda route: route.abort());page.on('pageerror',lambda e:errors.append(str(e)))
            idle={'schema_version':VERSION,'status':'IDLE','csrf':'test-only','coins':{},'storage_bytes':0}
            page.set_content(document('history-scan.html'),wait_until='domcontentloaded')
            install(page, idle)
            page.evaluate('HistoryReplay.refresh()')
            page.locator('#inst').fill('MINA')
            page.wait_for_function("!document.getElementById('start').disabled")
            assert page.locator('#days option').evaluate_all('(nodes)=>nodes.map(n=>n.value)') == ['3','7']
            page.locator('#start').click()
            page.wait_for_function("document.getElementById('status').textContent.includes('更新中')")
            posts = page.evaluate('window.__historyPosts')
            assert posts[-1]['path'].endswith('/start')
            assert posts[-1]['body']['csrf'] == 'test-only'
            assert posts[-1]['body']['days'] == {'days':7,'inst_id':'MINA-USDT-SWAP'}, posts[-1]
            page.locator('#pause').click()
            page.wait_for_function("document.getElementById('status').textContent.includes('暫停')")
            assert not page.locator('#resume').is_disabled()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'), width
            assert not errors, errors
            print(f'Single-coin history UI {width}x{height}: own coin, no fallback, 15m-only, controls passed', flush=True)
            context.close()
        browser.close()
    print('PASS: single-coin 15m history cards and controls; six responsive viewports; synthetic only.')


if __name__ == '__main__':
    main()
