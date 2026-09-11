"""Synthetic read-only card + explicit start/pause UI checks; no market calls."""
import copy
import json
import os
import shutil
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from playwright.sync_api import sync_playwright
from scripts.check_layout_ui import fixtures, CHECK_TEXT
from radar.card_statistics import setup


def main():
    report,items,single=fixtures()
    item=copy.deepcopy(items[1]);item['radar_horizon']='SHORT';item['direction']='LONG'
    item.update(entry_low='99',entry_high='101',stop_loss='95',take_profit_1='110',take_profit_2='115',trigger_type='CONTINUATION',signal_stage='EARLY_SIGNAL')
    item['market_metrics'].update(entry_execution_price=100)
    item['timeframe_states']={'4H':{'direction':'LONG'},'1H':{'direction':'LONG'},'15m':{'direction':'LONG'}}
    key=setup(item,100)['cohort']
    good={'schema_version':'HISTORY_PRICE_REPLAY_V3','status':'COMPLETE','compatible':True,'csrf':'test-only',
          'total':8,'done':8,'failed':0,'covered_symbols':8,'covered_inst_ids':[item['inst_id']],
          'scope_coverage_pct':100,'minimum_days':5,'groups':{key:{'label':'模擬情境','status':'AVAILABLE','resolved':50,
          'wins':31,'losses':19,'total':50,'days':5,'rate_pct':62,'unknown':0,'timeout':0,'interval_pct':[48.1,74.1]}},
          'symbol_groups':{item['inst_id']:{key:{'label':'模擬情境','status':'AVAILABLE','resolved':50,'wins':40,'losses':10,'total':50,'days':5,'rate_pct':80,'unknown':0,'timeout':0,'interval_pct':[66.9,89.1]}}}}
    cases=[]
    def case(name,changes,text):
        data=copy.deepcopy(good);changes(data);cases.append((name,data,text))
    case('available-own',lambda d:None,'80.0%')
    case('running',lambda d:d.update(status='RUNNING',done=10),'歷史掃描中')
    case('missing-cohort',lambda d:d.update(groups={},symbol_groups={}),'同類情境樣本不足')
    case('wrong-version',lambda d:d.update(compatible=False,status='VERSION_CHANGED'),'已變更')
    case('too-small',lambda d:(d['groups'][key].update(resolved=5,wins=3,losses=2,total=5),d['symbol_groups'][item['inst_id']][key].update(resolved=5,wins=3,losses=2,total=5)),'不足')
    case('coverage',lambda d:d.update(scope_coverage_pct=10),'不足')
    case('unknown-symbol',lambda d:d.update(covered_inst_ids=[]),'62.0%')
    case('no-job',lambda d:d.update(status='IDLE',groups={}),'尚未執行')
    outputs=Path(os.environ.get('RADAR_UI_OUTPUT','/tmp/radar-history-ui'));outputs.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,executable_path=shutil.which('chromium') or shutil.which('google-chrome'),args=['--no-sandbox'])
        for width,height in [(320,740),(390,844),(430,932),(768,1024),(1280,900),(844,390)]:
            context=browser.new_context(viewport={'width':width,'height':height},service_workers='block')
            page=context.new_page();page.set_default_timeout(6000);errors=[];posts=[];current=[copy.deepcopy(good)]
            page.route('**/*',lambda route:route.abort())
            page.on('pageerror',lambda e:errors.append(str(e)))
            def install(target, data):
                target.evaluate("""({data,report})=>{
                  window.__historyData=data;window.__historyPosts=[];
                  window.fetch=async(path,options={})=>{
                    const url=String(path);let result={enabled:false,available:false};
                    if(url==='/api/history-scan/status')result=window.__historyData;
                    else if(url.startsWith('/api/history-scan/')){
                      window.__historyPosts.push({path:url,body:JSON.parse(options.body)});
                      window.__historyData.status=url.endsWith('/start')?'RUNNING':'PAUSED';result=window.__historyData;
                    }else if(url==='/api/status')result={system_status:'FRESH',has_report:true};
                    else if(url.startsWith('/api/report'))result=report;
                    else if(url.startsWith('/api/history'))result={short_items:[],long_items:[]};
                    return new Response(JSON.stringify(result),{status:200,headers:{'Content-Type':'application/json'}});
                  };
                }""",{'data':data,'report':report})
            def document(name):
                html=(ROOT/'radar/static'/name).read_text()
                for js in ('history-replay.js','history-scan.js'):
                    html=html.replace('<script src="/'+js+'"></script>','<script>'+(ROOT/'radar/static'/js).read_text()+'</script>')
                return html.replace('<link rel="stylesheet" href="/history-replay.css">','<style>'+(ROOT/'radar/static/history-replay.css').read_text()+'</style>')
            install(page,good)
            page.set_content(document('pages.html'),wait_until='domcontentloaded')
            for name,data,expected in cases:
                page.evaluate("data=>window.__historyData=data",data)
                result=page.evaluate('''item=>{const before=JSON.stringify(item),allowed=itemCurrentEntryReady(item);renderSignals([item],'#fifteenAllBox');activateTab('fifteenAll',false);return {same:JSON.stringify(item)===before,unchanged:allowed===itemCurrentEntryReady(item)}}''',item)
                page.evaluate('HistoryReplay.refresh()')
                panel=page.locator('#fifteenAllBox .history-replay-card').first
                page.wait_for_function('(expected)=>document.querySelector("#fifteenAllBox .history-replay-card")?.textContent.includes(expected)',arg=expected)
                assert result['same'] and result['unchanged']
                assert panel.evaluate('(el)=>el.scrollWidth<=el.clientWidth+1'),(width,name,'overflow')
                assert page.locator('#fifteenAllBox .history-stats-panel').count()==1,'observed source preserved'
                if name=='available' and width==390:
                    panel.scroll_into_view_if_needed();page.screenshot(path=str(outputs/'history-card-390.png'))
            outsider=copy.deepcopy(item);outsider['inst_id']='MINA-USDT-SWAP'
            page.evaluate("data=>window.__historyData=data",good);page.evaluate('HistoryReplay.refresh()')
            outsider_html=page.evaluate('item=>HistoryReplay.card(item)',outsider)
            page.locator('#fifteenAllBox').evaluate('(el,html)=>el.innerHTML=html',outsider_html)
            page.evaluate('HistoryReplay.refresh()')
            page.wait_for_function('document.querySelector(\"#fifteenAllBox .history-replay-card\")?.textContent.includes(\"62.0%\")')
            assert '8支大型幣同類情境樣本' in page.locator('#fifteenAllBox .history-replay-card').inner_text()
            # No heavy POST happens when opening a normal card / home page.
            assert not page.evaluate("window.__historyPosts")
            long=copy.deepcopy(item);long['radar_horizon']='LONG'
            assert '不把短線結果套用' in page.evaluate('item=>HistoryReplay.card(item)',long)
            assert page.evaluate('item=>HistoryReplay.card(item,true)',item)==''
            page.close();page=context.new_page();page.set_default_timeout(6000);page.on('pageerror',lambda e:errors.append(str(e)));page.route('**/*',lambda route:route.abort())
            current[0]=copy.deepcopy(good);current[0].update(status='IDLE',groups={},done=0,total=0)
            install(page,current[0]);page.set_content(document('history-scan.html'),wait_until='domcontentloaded')
            page.locator('#start').wait_for();page.wait_for_function('!document.getElementById("start").disabled')
            assert not page.evaluate("window.__historyPosts")
            page.locator('#start').click();page.wait_for_function('document.getElementById("status").textContent==="歷史掃描中"')
            posts=page.evaluate('window.__historyPosts')
            assert page.locator('#days option').evaluate_all('(nodes)=>nodes.map(n=>n.value)')==['3','7']
            assert posts[-1]['path'].endswith('/start') and posts[-1]['body']['days']==7
            assert posts[-1]['body']['csrf']=='test-only'
            page.locator('#pause').click();page.wait_for_function('document.getElementById("status").textContent==="已暫停"')
            assert not page.locator('#resume').is_disabled()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
            assert not errors,errors
            print(f'History UI {width}x{height}: 8 card states + start/pause/dual-source/no auto-start passed',flush=True)
            context.close()
        browser.close()
    print('PASS: 48 history card scenarios; six responsive control pages; synthetic only.')

if __name__=='__main__':main()
