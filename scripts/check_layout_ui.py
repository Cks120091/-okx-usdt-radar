"""Offline layout regression for blocked cards and independent mobile scrollers.

All API results are synthetic. No live market requests or trading operations.
Run with Playwright/Chromium; RADAR_LAYOUT_HTML can point to an earlier build
for reproducing the inset-stripe regression. Screenshots go outside the repo.
"""
from __future__ import annotations

import base64
import copy
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright
from radar.intraday_flow import summarize_intraday_flow
from tests.test_intraday_flow import flow_fixture, SYMBOL, END
from tests.test_preflight import make_signal, make_report


def fixtures():
    candles, oi, taker = flow_fixture()
    flow = summarize_intraday_flow(SYMBOL, candles, oi, taker, observed_at_ms=END + 1000)
    signal = make_signal()
    signal.summary = '合成測試：突破後角色轉換，等待價格重新確認；並非實際行情。'
    signal.supporting_evidence = ['突破後角色轉換回踩守住', '1H 背景反向，屬逆勢 Trigger，僅為測試資料。']
    signal.actionable = True
    signal.entry_eligibility.update(new_entry_allowed=True, actionable=True)
    signal.decision_context = {'final': {'status': 'ENTER', 'new_entry_allowed': True, 'label': '目前可進', 'reasons': ['合成測試：價格確認。']}, 'hard_gate': {'status': 'PASSED', 'passed': True, 'blocked': False}}
    ready = signal.to_dict()
    blocked = copy.deepcopy(ready)
    blocked.update(direction='SHORT', trigger_id='layout-blocked', entry_low='2.576', entry_high='2.582', stop_loss='2.639', take_profit_1='2.273', take_profit_2='2.100', actionable=False)
    blocked['market_metrics'].update(last_price=2.586, entry_execution_price=2.586, entry_execution_price_source='BID', instrument_tick_size=.001, rsi_15m=63.8)
    blocked['entry_eligibility'].update(status='HARD_GATE_BLOCKED', new_entry_allowed=False, actionable=False, reason='目前價格位置不允許新進場。')
    blocked['decision_context'] = {'final': {'status': 'BLOCKED', 'new_entry_allowed': False, 'label': '先不要進場｜風險條件未通過', 'reasons': ['目前價格位置不允許新進場。']}, 'hard_gate': {'status': 'BLOCKED', 'passed': False, 'blocked': True, 'reasons': ['目前價格位置不允許新進場。']}}
    blocked['decision_context']['hard_gate']['checks'] = [{'status': 'BLOCKED', 'hard': True, 'reason': '目前價格位置不允許新進場。'}]
    blocked['decision_context']['conflict'] = {'items': ['1H 背景反向，屬逆勢 Trigger', '高週期方向與本卡相反，這是逆勢訊號。']}
    rows = [blocked, ready]
    for status, label in [('WAIT_RETEST', '等待回踩｜目前不要追價'), ('MISSED_ENTRY', '已錯過｜禁止追價')]:
        row = copy.deepcopy(ready)
        row.update(trigger_id='layout-' + status, actionable=False)
        row['entry_eligibility'].update(status=status, label=label, actionable=False, new_entry_allowed=False)
        row['decision_context']['final'].update(status='WAIT' if status == 'WAIT_RETEST' else 'NO_CHASE', label=label, new_entry_allowed=False)
        rows.append(row)
    ended = copy.deepcopy(ready)
    ended.update(trigger_id='layout-ended', signal_stage='COMPLETED')
    ended['lifecycle'].update(outcome='TAKE_PROFIT', stage='COMPLETED')
    rows.append(ended)
    tiny = copy.deepcopy(ready)
    tiny.update(inst_id='LONGSYMBOL123456789-USDT-SWAP', trigger_id='layout-precision', entry_low='0.00000001234', entry_high='0.00000001256', stop_loss='0.00000001100', take_profit_1='0.00000001678', take_profit_2='0.00000001999')
    tiny['market_metrics'].update(instrument_tick_size=1e-11, last_price=.0000000124)
    rows.append(tiny)
    report = make_report(signal).to_dict()
    report['horizon_freshness'] = {'SHORT': {'available': True, 'expired': False}, 'LONG': {'available': True, 'expired': False}}
    single = {'inst_id': SYMBOL, 'analyzed_at': report['generated_at'], 'cross_timeframe': flow, 'short': {'item': ready, 'message': '合成測試，非行情'}, 'long': None}
    return report, rows, single


CHECK_FRAME = """() => {
 const r=s=>document.querySelector(s).getBoundingClientRect();
 const top=r('.top'),shell=r('.shell'),nav=r('.primary-nav');
 const f=document.querySelector('#filterShell');
 const errors=[];
 if(top.bottom>shell.top+1)errors.push('header covers content');
 if(!f.hidden && r('#filterShell').bottom>shell.top+1)errors.push('filters cover content');
 if(shell.bottom>nav.top+1)errors.push('bottom navigation covers content');
 if(nav.bottom>innerHeight+1)errors.push('navigation beyond viewport');
 if(shell.height<Math.min(140,innerHeight*.25))errors.push('content row collapsed');
 if(document.documentElement.scrollWidth>innerWidth+1)errors.push('page horizontal overflow');
 return errors;
}"""
CHECK_TEXT = """root => {
 const errors=[],box=root.getBoundingClientRect();
 const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
 while(walker.nextNode()){
  const node=walker.currentNode;if(!node.textContent.trim())continue;
  const range=document.createRange();range.selectNodeContents(node);
  for(const r of range.getClientRects()){
   if(r.width && (r.left<box.left-1 || r.right>box.right+1)){
    errors.push(node.textContent.trim().slice(0,60));break;
   }
  }
 }
 return errors;
}"""


def main():
    report, rows, single = fixtures()
    source = Path(os.environ.get('RADAR_LAYOUT_HTML', ROOT / 'radar/static/pages.html'))
    html = source.read_text()
    icon = base64.b64encode((ROOT / 'radar/static/radar-icon.svg').read_bytes()).decode()
    html = html.replace('src="/radar-icon.svg"', 'src="data:image/svg+xml;base64,' + icon + '"')
    output = Path(os.environ.get('RADAR_UI_OUTPUT', '/tmp/radar-layout-ui'))
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=shutil.which('chromium') or shutil.which('google-chrome'), args=['--no-sandbox'])
        for width, height in [(320, 740), (360, 800), (390, 844), (430, 932), (768, 1024), (1280, 900), (844, 390)]:
            context = browser.new_context(viewport={'width': width, 'height': height}, has_touch=width <= 430, service_workers='block')
            page = context.new_page(); page.set_default_timeout(4000)
            errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
            page.route('**/*', lambda route: route.abort())
            page.evaluate('''({report,single})=>{window.__requests=[];window.fetch=async(path,opts={})=>{const url=String(path);let body=null;try{body=JSON.parse(opts.body||'null')}catch(_){}window.__requests.push({url,body});const value=url.includes('/instrument/scan')?single:url.includes('/report/')?report:url.includes('/status')?{system_status:'FRESH',has_report:true}:url.includes('/history')?{short_items:[],long_items:[]}:{enabled:false,available:false};return new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}})}}''', {'report': report, 'single': single})
            page.set_content(html, wait_until='domcontentloaded')
            page.evaluate('''({report,rows})=>{state.status={system_status:'FRESH',has_report:true};renderReport(report);renderSignals(rows,'#fifteenAllBox');activateTab('fifteenAll',false)}''', {'report': report, 'rows': rows})
            card = page.locator('#fifteenAllBox > .signal-card').first
            panel = card.locator('.decision-panel')
            panel.scroll_into_view_if_needed()
            if width == 390: page.screenshot(path=str(output / 'blocked-390.png'))
            assert panel.evaluate('(e)=>getComputedStyle(e,"::before").content') in ('none', 'normal'), 'Old inset stripe still overlays zero-padding decision text'
            assert not page.evaluate(CHECK_FRAME), (width, page.evaluate(CHECK_FRAME))
            for index in range(len(rows)):
                item = page.locator('#fifteenAllBox > .signal-card').nth(index)
                assert item.evaluate('(e)=>e.scrollWidth<=e.clientWidth+1'), (width, index, 'card overflow')
                assert not item.locator('.decision-panel').evaluate(CHECK_TEXT), (width, index, item.locator('.decision-panel').evaluate(CHECK_TEXT))
                if width <= 600:
                    title = item.locator('.decision-top>div').first.bounding_box()
                    quote = item.locator('.decision-price').bounding_box()
                    assert quote['y'] >= title['y'] + title['height'], 'Mobile quote must be below status text'
            # Last card controls must remain reachable, above the navigation.
            last_button = page.locator('#fifteenAllBox > .signal-card').last.locator('[data-detail-key^="item:"]')
            last_button.scroll_into_view_if_needed()
            assert last_button.bounding_box()['y'] + last_button.bounding_box()['height'] <= page.locator('.shell').bounding_box()['y'] + page.locator('.shell').bounding_box()['height'] + 1
            # Data title remains outside scrolling content even with long names.
            last_button.click()
            dialog = page.locator('#dataDetailDialog'); content = page.locator('#dataDetailContent')
            content.evaluate('(e)=>e.scrollTop=e.scrollHeight')
            assert dialog.evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
            head = page.locator('.data-page-head').bounding_box()
            assert head['y'] >= dialog.bounding_box()['y'] - 1
            assert head['y'] + head['height'] <= content.bounding_box()['y'] + 1
            page.keyboard.press('Escape')
            page.evaluate("openSingleScan('AAA-USDT-SWAP','SHORT','LONG')")
            page.locator('#singleScanDialog .flow-mini').first.wait_for()
            sc = page.locator('#singleScanContent'); sc.evaluate('(e)=>e.scrollTop=e.scrollHeight')
            sh = page.locator('.intraday-dialog-head').bounding_box()
            assert sh['y'] + sh['height'] <= sc.bounding_box()['y'] + 1
            page.locator('#singleScanDialog [data-detail-key^="flow:"]').click()
            page.locator('[data-flow-window="4H"]').click()
            content.evaluate('(e)=>e.scrollTop=150')
            tabs = page.locator('.data-time-tabs').bounding_box()
            head = page.locator('.data-page-head').bounding_box()
            assert tabs['y'] >= head['y'] + head['height'] - 1, 'Time tabs must not overlap header'
            if width == 390: page.screenshot(path=str(output / 'data-390.png'))
            page.keyboard.press('Escape'); assert page.locator('#singleScanDialog').is_visible()
            page.locator('#singleScanClose').click()
            # Main, short/long, market, history, manual and empty states share
            # the same bounded content row. No cross-timeframe strategy changes.
            page.evaluate('rows=>{renderSignals(rows.map(x=>({...x,radar_horizon:"LONG"})),"#longSignalsBox");renderMap(rows);renderHistory({short_items:[],long_items:[]})}', rows)
            for tab in ('overview', 'longSignals', 'map', 'history', 'manual', 'stats', 'fifteenAll'):
                page.evaluate('tab=>activateTab(tab,false)', tab)
                assert not page.evaluate(CHECK_FRAME), (width, tab, page.evaluate(CHECK_FRAME))
            if width == 390:
                panel.scroll_into_view_if_needed()
                # Anchor above the decision panel, not the midway point of a tall card.
                page.evaluate("{const s=document.querySelector('.shell'),e=document.querySelector('#fifteenAllBox .decision-top');s.scrollTop+=e.getBoundingClientRect().top-s.getBoundingClientRect().top-16}")
                page.screenshot(path=str(output / 'blocked-390.png'))
            assert not errors, errors
            context.close(); print(f'PASS {width}x{height}', flush=True)
        browser.close()
    print('Layout regression passed: blocked/ready/wait/missed/closed/precision cards; 7 viewports; independent modal scroll; no navigation overlap. Synthetic data only.')


if __name__ == '__main__':
    main()
