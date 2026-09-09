"""Optional Playwright smoke test; mock all market APIs, never scan live accounts."""
from __future__ import annotations
import json
import shutil
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from radar.intraday_flow import summarize_intraday_flow
from tests.test_intraday_flow import flow_fixture, SYMBOL, END
from playwright.sync_api import sync_playwright


def main():
    c, o, f = flow_fixture()
    flow = summarize_intraday_flow(SYMBOL, c, o, f, observed_at_ms=END + 1000)
    item = {"inst_id": SYMBOL, "direction": "SHORT", "radar_horizon": "SHORT",
            "status": "NEAR_TRIGGER", "timeframe_states": {
                "4H": {"label": "多頭背景中的短線回落"},
                "1H": {"label": "回落段"}, "15m": {"label": "空方價格事件形成"}}}
    data = {"inst_id": SYMBOL, "analyzed_at": "2026-09-09T12:00:00+00:00",
            "cross_timeframe": flow, "short": {"item": item, "message": "合成測試資料，非行情"}, "long": None}
    requests = []
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=str(ROOT / 'radar/static')))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            chrome = shutil.which('google-chrome') or shutil.which('chromium')
            browser = p.chromium.launch(headless=True, executable_path=chrome, args=['--no-sandbox'])
            for width in (390, 980):
                context = browser.new_context(viewport={"width": width, "height": 844}, service_workers='block')
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                def respond(route):
                    url = route.request.url
                    if '/api/instrument/scan' in url:
                        requests.append(route.request.post_data_json)
                        payload = data
                    elif '/api/status' in url:
                        payload = {"system_status": "BOOTING", "has_report": False}
                    elif '/api/history' in url:
                        payload = {"short_items": [], "long_items": []}
                    else:
                        payload = {"enabled": False, "available": False}
                    route.fulfill(status=200, content_type='application/json', body=json.dumps(payload))
                page.route('**/api/**', respond)
                page.goto(f'http://127.0.0.1:{server.server_port}/pages.html')
                page.evaluate("activateTab('fifteenAll',false)")
                page.locator('#globalSearch').fill('AAA')
                page.locator('#singleSearch').click()
                page.locator('#singleScanDialog .flow-mini').first.wait_for()
                assert page.locator('#singleScanDialog .flow-mini').count() == 3
                assert page.locator('#singleScanDialog').evaluate('(x)=>x.scrollWidth<=x.clientWidth+1')
                assert requests[-1]['horizon'] == 'SHORT'
                assert requests[-1]['direction_lock'] is None
                assert '持倉' in page.locator('#singleScanDialog').inner_text()
                page.locator('#singleScanDialog [data-detail-key^="flow:"]').click()
                page.locator('#dataDetailDialog').wait_for()
                assert page.locator('#dataDetailDialog [data-flow-window]').count() == 3
                page.locator('[data-flow-window="4H"]').click()
                assert page.locator('[data-flow-window="4H"]').get_attribute('aria-pressed') == 'true'
                assert 'USDT' in page.locator('#dataDetailContent').inner_text()
                page.keyboard.press('Escape')
                assert page.locator('#singleScanDialog').is_visible()
                assert not page.locator('#dataDetailDialog').is_visible()
                page.locator('#singleScanClose').click()
                # The real card button must pass its original direction.
                page.evaluate("document.querySelector('#searchFeedback').outerHTML += singleScanButton('AAA-USDT-SWAP','SHORT',{direction:'SHORT'})")
                page.locator('[data-single-id]').first.click()
                page.locator('#singleScanDialog .flow-mini').first.wait_for()
                assert requests[-1]['direction_lock'] == 'SHORT'
                page.locator('#singleScanClose').click()
                assert not errors, errors
                context.close()
            browser.close()
    finally:
        server.shutdown(); server.server_close()
    print('Intraday UI smoke passed: mobile/desktop, three windows, no overflow, card direction lock')


if __name__ == '__main__':
    main()
