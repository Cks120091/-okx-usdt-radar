"""Synthetic browser checks for single-coin sample tier labels; no market calls."""
import copy
import json
import shutil
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / 'radar/static/history-replay.js').read_text()
ITEM = {
    'inst_id':'MINA-USDT-SWAP','radar_horizon':'SHORT','direction':'LONG',
    'trigger_type':'CONTINUATION','signal_stage':'EARLY_SIGNAL','stop_loss':95,
    'take_profit_1':112,'market_metrics':{'entry_execution_price':100},
    'timeframe_states':{'4H':{'direction':'LONG'}},
}
KEY = json.dumps(['SHORT','LONG','CONTINUATION','EARLY_SIGNAL','同向背景','2–<3R'], ensure_ascii=False)
VERSION = 'HISTORY_SINGLE_15M_V1'


def group(resolved, tier, wins=None):
    wins = resolved if wins is None else wins
    losses = resolved - wins
    return {'label':'全部15m可進訊號','status':'AVAILABLE','resolved':resolved,'wins':wins,'losses':losses,
            'total':resolved,'days':3,'unknown':0,'timeout':0,'rate_pct':round(100*wins/resolved,1) if resolved else None,
            'tier':tier,'coverage_pct':100,'interval_pct':[0,100] if resolved else None}


def status(resolved, tier, wins=None):
    overall = group(resolved, tier, wins)
    coin = {'inst_id':'MINA-USDT-SWAP','status':'COMPLETE','compatible':True,'days':7,'total':1,'done':1,
            'start_ms':1_799_000_000_000,'end_ms':1_799_604_800_000,'scope_coverage_pct':100,
            'overall':overall,'groups':{KEY:copy.deepcopy(overall)},'excluded':[]}
    return {'schema_version':VERSION,'status':'COMPLETE','csrf':'test','inst_id':'MINA-USDT-SWAP','coins':{'MINA-USDT-SWAP':coin}}


def render(page, data, item=None):
    page.evaluate('data=>window.__historyData=data', data)
    page.evaluate('HistoryReplay.refresh()')
    page.locator('#box').evaluate('(el,html)=>el.innerHTML=html', page.evaluate('item=>HistoryReplay.card(item)', item or ITEM))
    page.evaluate('HistoryReplay.refresh()')
    page.wait_for_timeout(60)
    return page.locator('#box .history-replay-card').inner_text()


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=shutil.which('chromium') or shutil.which('google-chrome'), args=['--no-sandbox'])
        page = browser.new_page(viewport={'width':390,'height':844});page.set_default_timeout(6000)
        bootstrap = """<div id='box'></div><script>
        window.__historyData={};
        window.fetch=async()=>new Response(JSON.stringify(window.__historyData),{status:200,headers:{'Content-Type':'application/json'}});
        </script><script>""" + JS + '</script>'
        page.set_content(bootstrap, wait_until='domcontentloaded')

        for resolved,tier in [(1,'極低樣本'),(9,'極低樣本'),(10,'低樣本參考'),(19,'低樣本參考'),
                              (20,'中等樣本'),(49,'中等樣本'),(50,'樣本充足')]:
            text = render(page, status(resolved, tier))
            assert tier in text and f'已判定 {resolved} 筆' in text, (resolved, text)
            assert '本幣' in text and '8支大型幣' not in text, text

        data = status(10, '低樣本參考', wins=6)
        text = render(page, data)
        assert '60.0%' in text and '目前同類情境' in text, text

        btc = dict(ITEM, inst_id='BTC-USDT-SWAP')
        text = render(page, data, btc)
        assert '尚未更新' in text and '60.0%' not in text, text

        long_item = dict(ITEM, radar_horizon='LONG')
        assert page.evaluate('item=>HistoryReplay.card(item)', long_item) == ''
        assert page.evaluate('item=>HistoryReplay.card(item,true)', ITEM) == ''
        browser.close()
    print('PASS: single-coin sample tiers, no cross-coin fallback, and 4H exclusion')


if __name__ == '__main__':
    main()
