"""Offline Chromium regression. Run after installing playwright/chromium; no live market calls."""
import sys,json,mimetypes,re,os,shutil,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
OUTPUT=Path(os.environ.get('RADAR_UI_CHECK_OUTPUT',tempfile.mkdtemp(prefix='radar-ui-')))
OUTPUT.mkdir(parents=True,exist_ok=True)
from playwright.sync_api import sync_playwright
from tests.test_signal_position_policy import signal_dict,preflight_signal,preflight
from radar.decision import build_decision_context
from radar.public_payload import public_candidate_payload
BASE=ROOT/'radar/static'
errors=[]
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=shutil.which('chromium'),args=['--no-sandbox'])
    context=browser.new_context(viewport={'width':390,'height':844},device_scale_factor=1,service_workers='block')
    page=context.new_page()
    page.on('pageerror',lambda e:errors.append(str(e)))
    def route(r):
        from urllib.parse import urlparse
        path=urlparse(r.request.url).path
        if path.startswith('/api/'):
            response={'available':False} if 'push' in path else {'system_status':'FRESH','has_report':False} if path=='/api/status' else {'items':[]}
            r.fulfill(status=200,content_type='application/json',body=json.dumps(response))
        else:
            file=BASE/('pages.html' if path=='/' else path.lstrip('/'))
            r.fulfill(status=200 if file.exists() else 404,content_type=mimetypes.guess_type(str(file))[0] or 'text/plain',body=file.read_bytes() if file.exists() else b'')
    context.route('**/*',route)
    # Offline document render: do not navigate or call external services.
    html=BASE.joinpath('pages.html').read_text()
    html=re.sub(r'<script[^>]+src=["\']([^"\']+)["\'][^>]*></script>',lambda m:'<script>'+BASE.joinpath(m.group(1).lstrip('/')).read_text()+'</script>',html)
    html=re.sub(r'<link[^>]+href=["\']([^"\']+\.css)["\'][^>]*>',lambda m:'<style>'+BASE.joinpath(m.group(1).lstrip('/')).read_text()+'</style>',html)
    page.evaluate("""window.fetch=async path=>new Response(JSON.stringify(String(path).includes('/api/push')?{available:false}:String(path).includes('/api/status')?{system_status:'FRESH',has_report:false}:{items:[]}),{status:200,headers:{'Content-Type':'application/json'}})""")
    page.set_content(html,wait_until='load')
    page.wait_for_timeout(150)
    def show(item):
        item['decision_context']=build_decision_context(item)
        item['actionable']=item['decision_context']['final']['new_entry_allowed']
        public=public_candidate_payload(item,signal=True)
        public['freshness']='NEW'
        page.evaluate("""item=>{state.status={system_status:'FRESH',has_report:true};state.report={runtime_status:'FRESH',signals:[item],long_signals:[],horizon_freshness:{SHORT:{available:true,expired:false},LONG:{available:true,expired:false}}};renderSignals([item]);activateTab('fifteenAll',false)}""",public)
        page.wait_for_timeout(80)
        return public
    public=show(signal_dict(quote=101))
    card=page.locator('.signal-card').first
    assert '做多訊號已觸發' in card.inner_text(),card.inner_text()
    assert '高於可進位置' in card.inner_text()
    assert card.locator('details,.data-open-button,.history-replay-card,.history-stats-panel').count()==0
    assert card.locator('[data-preflight-id]').is_enabled()
    assert card.locator('.decision-state').count()==1
    assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
    page.set_viewport_size({'width':390,'height':1100})
    card.screenshot(path=str(OUTPUT/'card-check.png'))
    print('ACTIVE CARD:',card.inner_text())
    # Actual preflight renderer, not a hand-written UI mock.
    payload=preflight(preflight_signal(),105)
    page.evaluate("""data=>{document.querySelector('#preflightPage').hidden=false;state.preflight={instId:data.inst_id,horizon:data.horizon,triggerId:data.trigger_id};renderPreflight(data)}""",payload)
    assert '做多訊號仍有效' in page.locator('.preflight-verdict').inner_text()
    assert '高於可進位置' in page.locator('.preflight-verdict').inner_text()
    assert '105.01' in page.locator('.preflight-live-price').inner_text()
    page.locator('.preflight-verdict').screenshot(path=str(OUTPUT/'preflight-check.png'))
    page.evaluate("document.querySelector('#preflightPage').hidden=true")
    # Entry distance must never conceal a real mismatch.
    item=signal_dict(quote=101)
    item['market_metrics']['raw_indicators']['1H']['fusion_long_score']=35
    show(item)
    assert '週期方向不同步' in card.locator('.decision-state').inner_text()
    assert '做多訊號已觸發' not in card.locator('.decision-state').inner_text()
    # Expired public report cannot re-use a green signal flag.
    show(signal_dict(quote=101))
    page.evaluate("state.report.horizon_freshness.SHORT.expired=true;renderSignals(state.report.signals)")
    assert '資料已過期' in card.locator('.decision-state').inner_text()
    assert card.locator('.preflight-link').is_disabled()
    show(signal_dict(quote=101))
    page.evaluate("state.report.signals[0].lifecycle={terminal:true,status:'INVALIDATED',outcome:'SL_HIT'};renderSignals(state.report.signals)")
    assert card.locator('.preflight-link').is_disabled()
    assert '訊號已觸發' not in card.locator('.decision-state').inner_text()
    assert not errors,errors
    print('PASS: browser runtime, minimal DOM, preflight live quote, mismatch, stale, terminal; no JS errors')
    browser.close()
