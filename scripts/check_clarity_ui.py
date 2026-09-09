"""Offline browser regression: no market, account or external network requests."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from dataclasses import replace
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from playwright.sync_api import sync_playwright
from radar.intraday_flow import summarize_intraday_flow
from radar.exit_review import review_exit_plan
from radar.preflight import build_preflight_payload
from radar.config import AppConfig
from radar.models import MarketContext,Ticker
from tests.test_preflight import make_signal,make_report
from tests.test_intraday_flow import flow_fixture,SYMBOL,END


def main():
    c,o,t=flow_fixture();flow=summarize_intraday_flow(SYMBOL,c,o,t,observed_at_ms=END+1000)
    signal=make_signal();signal.summary='合成測試畫面：價格守住支撐，短線買方開始反推。'
    signal.supporting_evidence=['價格守住支撐，回踩後重新上推。','短線動能開始配合價格；不是實際行情。']
    signal.actionable=True
    signal.entry_eligibility.update(new_entry_allowed=True,actionable=True)
    signal.decision_context={"final":{"status":"ENTER","new_entry_allowed":True,"label":"可進場","reasons":["合成測試：價格觸發，執行檢查通過。"]},"hard_gate":{"status":"PASSED","passed":True,"blocked":False}}
    signal.management_plan.update(adaptive_market_plan=True,stop_method='結構失效位＋波動／影線緩衝',target_method='前方壓力＋波動力度',structural_target_price=103)
    report=make_report(signal).to_dict();report['horizon_freshness']={'SHORT':{'available':True,'expired':False},'LONG':{'available':False,'expired':False}}
    single={'inst_id':SYMBOL,'analyzed_at':datetime.now(timezone.utc).isoformat(),'cross_timeframe':flow,'short':{'item':signal.to_dict(),'message':'合成測試，非行情','exit_review':review_exit_plan(signal,flow,current_price=101,now_ms=END+1000)},'long':None}
    quote=Ticker(SYMBOL,100,99.99,100.01,int(datetime.now(timezone.utc).timestamp()*1000),20_000_000)
    context=MarketContext(SYMBOL,1e7,.0001,.05,.55,quote.ts,bid_depth_usd=1e6,ask_depth_usd=1e6,buy_slippage_pct=.01,sell_slippage_pct=.01,execution_notional_usdt=1000,best_bid=99.99,best_ask=100.01)
    preflight=build_preflight_payload(signal,quote,context,AppConfig(),report_generated_at=report['generated_at'])
    preflight['continuation']={'current':{'cross_timeframe':flow},'refresh_failed':False}
    preflight['exit_review']=single['short']['exit_review']
    html=(ROOT/'radar/static/pages.html').read_text()
    output=Path(os.environ.get('RADAR_UI_OUTPUT','/tmp/radar-clarity-ui'));output.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,executable_path=shutil.which('chromium') or shutil.which('google-chrome'),args=['--no-sandbox'])
        for width in (360,390,768,1280):
            context=browser.new_context(viewport={'width':width,'height':844},service_workers='block')
            page=context.new_page();page.set_default_timeout(4000);errors=[];print('viewport',width,flush=True)
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.route('**/*',lambda r:r.abort())
            page.evaluate('''({single,preflight,report})=>{window.__requests=[];window.fetch=async(path,opts={})=>{const url=String(path);let body=null;try{body=JSON.parse(opts.body||'null')}catch(_){}window.__requests.push({url,body});const payload=url.includes('/instrument/scan')?single:url.includes('/preflight')?preflight:url.includes('/report/')?report:url.includes('/status')?{system_status:'LATEST',has_report:true}:url.includes('/history')?{short_items:[],long_items:[]}:{enabled:false,available:false};return new Response(JSON.stringify(payload),{status:200,headers:{'Content-Type':'application/json'}})}}''',{'single':single,'preflight':preflight,'report':report})
            page.set_content(html,wait_until='domcontentloaded')
            page.evaluate('report=>{state.status={system_status:"LATEST",has_report:true};renderReport(report);activateTab("signals",false)}',report)
            card=page.locator('#signalsBox > .signal-card').first
            print('rendered',page.locator('#signalsBox').inner_text()[:200],errors,flush=True)
            card.wait_for()
            assert card.locator('.primary-reasons').count()==1
            assert card.locator('.signal-plan-cell.target2').count()==1
            assert card.locator('.signal-plan-cell.stop').inner_text().find('98')>=0
            assert card.locator('.primary-reasons').bounding_box()['y'] < card.locator('.signal-plan-grid').bounding_box()['y']
            assert not card.locator('.intraday-flow-cell').count()
            assert card.evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
            card.locator('[data-detail-key^="item:"]').click()
            page.locator('#dataDetailDialog').wait_for()
            assert '完整理由' in page.locator('#dataDetailContent').inner_text()
            assert page.locator('#dataDetailDialog').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
            page.keyboard.press('Escape')
            assert not page.locator('#dataDetailDialog').is_visible()
            # Open single-coin scan, preserving the card direction.
            page.evaluate("openSingleScan('AAA-USDT-SWAP','SHORT','LONG')")
            page.locator('#singleScanDialog .flow-mini').first.wait_for()
            assert page.locator('#singleScanDialog .flow-mini').count()==3
            requests=page.evaluate('window.__requests')
            assert [r for r in requests if '/instrument/scan' in r['url']][-1]['body']['direction_lock']=='LONG'
            page.locator('#singleScanDialog [data-detail-key^="flow:"]').click()
            assert page.locator('#dataDetailDialog [data-flow-window]').count()==3
            for tf in ('4H','1H','15m'):
                page.locator(f'#dataDetailDialog [data-flow-window="{tf}"]').click()
                assert page.locator(f'#dataDetailDialog [data-flow-window="{tf}"]').get_attribute('aria-pressed')=='true'
            assert 'USDT' in page.locator('#dataDetailContent').inner_text()
            assert '持倉增加不等於做多' in page.locator('#dataDetailContent').inner_text()
            if width==390:page.screenshot(path=str(output/'data-390.png'))
            page.keyboard.press('Escape')
            assert page.locator('#singleScanDialog').is_visible()
            page.locator('#singleScanClose').click()
            if width==390:
                page.locator('.shell').evaluate('(e)=>e.scrollTop=0')
                page.screenshot(path=str(output/'main-390.png'))
            # Preflight data page must not dismiss underlying page or trap its focus.
            page.evaluate('data=>{$("#preflightPage").hidden=false;setAppInert(true);renderPreflight(data)}',preflight)
            page.locator('#preflightContent [data-detail-key^="flow:"]').click()
            page.keyboard.press('Tab');assert page.evaluate('document.querySelector("#dataDetailDialog").contains(document.activeElement)')
            page.keyboard.press('Escape');assert page.locator('#preflightPage').is_visible()
            page.locator('#preflightContent [data-detail-key^="execution:"]').click()
            assert '成交' in page.locator('#dataDetailContent').inner_text()
            page.locator('#dataDetailClose').click()
            assert not errors,errors
            context.close()
        browser.close()
    print('Clarity UI passed: 360/390/768/1280, entry/reasons/TP2, independent data tabs, focus/Escape, preserved card direction; fully mocked, no live data.')


if __name__=='__main__':main()
