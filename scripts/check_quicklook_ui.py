"""Offline quick-look regression; synthetic fixtures, no market requests.

Run: python scripts/check_quicklook_ui.py (Playwright + Chromium required).
The summary must be read-only, reuse final entry permission, preserve card
identity, show missing flow explicitly and fit mobile/desktop screens.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright
from scripts.check_layout_ui import fixtures, CHECK_TEXT


def main():
    report, existing, single = fixtures()
    ready = copy.deepcopy(existing[1])
    ready['timeframe_states'] = {
        '15m': {'direction': 'LONG', 'label': '做多早期訊號'},
        '1H': {'direction': 'LONG', 'label': '偏多'},
        '4H': {'direction': 'LONG', 'label': '偏多'},
    }
    ready['trigger_type'] = 'CONTINUATION'
    ready['decision_context']['continuation_confirmation'] = {'core_votes': {
        'OI': {'state': 'SUPPORT'}, 'TAKER_CVD': {'state': 'SUPPORT'}}}
    intraday = copy.deepcopy(ready)
    intraday.update(direction='SHORT', trigger_id='quicklook-short')
    intraday['timeframe_states']['15m'] = {'direction': 'SHORT', 'label': '做空早期訊號'}
    intraday['timeframe_states']['1H'] = {'direction': 'SHORT', 'label': '偏空'}
    intraday['timeframe_states']['4H'].update(phase='多頭背景中的短線回落')
    mirror = copy.deepcopy(ready)
    mirror['timeframe_states']['4H'] = {'direction': 'SHORT', 'label': '偏空', 'phase': '空頭背景中的短線反彈'}
    missing = copy.deepcopy(ready)
    missing['decision_context']['continuation_confirmation']['core_votes']['OI']['state'] = 'UNKNOWN'
    conflict = copy.deepcopy(ready)
    conflict['decision_context']['continuation_confirmation']['core_votes']['TAKER_CVD']['state'] = 'CONFLICT'
    neutral = copy.deepcopy(ready)
    neutral['decision_context']['continuation_confirmation']['core_votes'] = {'OI': {'state': 'NEUTRAL'}, 'TAKER_CVD': {'state': 'NEUTRAL'}}
    stale = copy.deepcopy(ready); stale['read_only_reason'] = 'STALE'
    scanning = copy.deepcopy(ready); scanning['read_only_reason'] = 'SCANNING'
    unknown = copy.deepcopy(ready); unknown['decision_context'].pop('final')
    long = copy.deepcopy(ready)
    long.update(radar_horizon='LONG', direction='SHORT', trigger_id='quicklook-long')
    long['timeframe_states'] = {'4H': {'direction': 'SHORT', 'label': '做空早期訊號'}, '1H': {'direction': 'SHORT', 'label': '偏空'}, '1D': {'direction': 'LONG', 'label': '偏多'}}
    cases = [
        ('ready', ready, '可進', '順勢續走做多', '支持｜'),
        ('intraday-short', intraday, '可進', '多頭回踩內短空', '支持｜'),
        ('intraday-long', mirror, '可進', '空頭反彈內短多', '支持｜'),
        ('blocked', existing[0], '先不要', None, '資料不足'),
        ('wait', existing[2], '等回踩', None, '資料不足'),
        ('missed', existing[3], '禁止追價', None, '資料不足'),
        ('closed', existing[4], '已結束', None, '歷史資料'),
        ('missing', missing, '可進', None, '資料不足'),
        ('conflict', conflict, '可進', None, '有反證'),
        ('neutral', neutral, '可進', None, '中性'),
        ('stale', stale, '先不要｜資料過期', None, '待更新'),
        ('scanning', scanning, '先不要｜掃描中', None, '待更新'),
        ('unknown', unknown, '先不要', None, '支持｜'),
        ('long', long, '可進', '逆 1D 波段空', '支持｜'),
    ]
    single['short']['item'] = intraday
    html = (ROOT/'radar/static/pages.html').read_text()
    output = Path(os.environ.get('RADAR_UI_OUTPUT', '/tmp/radar-quicklook-ui')); output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=shutil.which('chromium') or shutil.which('google-chrome'), args=['--no-sandbox'])
        for width, height in [(320,740),(360,800),(390,844),(430,932),(768,1024),(1280,900),(844,390)]:
            context = browser.new_context(viewport={'width':width,'height':height},service_workers='block')
            page = context.new_page();page.set_default_timeout(5000)
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.route('**/*',lambda route:route.abort())
            page.evaluate('''({report,single})=>{window.__requests=[];window.fetch=async(path,opts={})=>{const url=String(path);let body=null;try{body=JSON.parse(opts.body||'null')}catch(_){}window.__requests.push({url,body});const value=url.includes('/instrument/scan')?single:url.includes('/report/')?report:url.includes('/status')?{system_status:'FRESH',has_report:true}:url.includes('/history')?{short_items:[],long_items:[]}:{enabled:false,available:false};return new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}})}}''',{'report':report,'single':single})
            page.set_content(html,wait_until='domcontentloaded')
            page.evaluate("report=>{state.status={system_status:'FRESH',has_report:true};renderReport(report);activateTab('fifteenAll',false)}",report)
            for name,item,action,setup,funds in cases:
                result=page.evaluate('''item=>{const before=JSON.stringify(item),permission=itemCurrentEntryReady(item),view=quickLookData(item);renderSignals([item],'#fifteenAllBox');return {view,unchanged:JSON.stringify(item)===before,permission,after:itemCurrentEntryReady(item)}}''',item)
                assert result['unchanged'] and result['permission']==result['after'],name
                assert result['view']['action'].startswith(action),(name,result)
                if setup:assert result['view']['setup']==setup,(name,result)
                assert funds in result['view']['funds'],(name,result)
                card=page.locator('#fifteenAllBox > .signal-card').first
                panel=card.locator('.quicklook-panel')
                assert panel.count()==1 and panel.locator('dt').all_text_contents()==['行動','型態','依據','資金']
                assert not panel.evaluate(CHECK_TEXT),(width,name,panel.evaluate(CHECK_TEXT))
                assert card.evaluate('(e)=>e.scrollWidth<=e.clientWidth+1'),(width,name,'overflow')
                assert card.locator('.signal-plan-cell').count()>=4
                if name=='long':assert '15m' not in result['view']['basis'] and '1D' in result['view']['basis']
                if width==390 and name=='intraday-short':
                    panel.scroll_into_view_if_needed();page.screenshot(path=str(output/'intraday-short-390.png'))
            # Preview/stale evidence must never inherit an old 'ready' decision.
            preview=page.evaluate('''item=>{state.report.runtime_status='CORE_PREVIEW';state.report.scan_request_mode='SHORT';state.currentPreviewGeneratedAt=state.report.generated_at;const v=quickLookData(item);state.report.runtime_status='SIGNALS_FOUND';state.currentPreviewGeneratedAt=null;return v}''',ready)
            assert preview['action'].startswith('先不要') and '待更新' in preview['funds']
            # Changed timeframe labels invalidate the render without a new timestamp.
            assert page.evaluate('''item=>{const a={signals:[item]},before=reportRenderFingerprint(a);item.timeframe_states['4H'].label='新背景';return before!==reportRenderFingerprint(a)}''',copy.deepcopy(ready))
            hostile=copy.deepcopy(ready);hostile['timeframe_states']['1H']['label']='<img src=x onerror="window.__bad=true">'
            assert page.evaluate("item=>{document.querySelector('#fifteenAllBox').innerHTML=quickLookPanel(item);return !document.querySelector('#fifteenAllBox img')&&!window.__bad}",hostile)
            page.evaluate("openSingleScan('AAA-USDT-SWAP','SHORT','SHORT')")
            panel=page.locator('#singleScanDialog .quicklook-panel');panel.wait_for()
            assert '多頭回踩內短空' in panel.inner_text()
            assert page.locator('#singleScanDialog .flow-mini').count()==3
            requests=page.evaluate('window.__requests')
            assert [r for r in requests if '/instrument/scan' in r['url']][-1]['body']['direction_lock']=='SHORT'
            page.locator('#singleScanClose').click()
            assert not errors,errors
            print(f'Quick-look {width}x{height}: {len(cases)} states, read-only permission, direction lock, escaping and layout OK',flush=True)
            context.close()
        browser.close()
    print('Quick-look PASS: 98 card cases + preview/fingerprint/escaping/single-coin checks; all synthetic/offline.')


if __name__=='__main__':main()
