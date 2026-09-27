"""Assemble the dashboard's snapshot module at image build time.

The historical dashboard is a monolithic inline script. Keep the repaired state
logic separately testable; replace only its exact two legacy functions and wire
presentation hooks. Fail the image build rather than silently deploying half a fix.
No trading or API code is transformed.
"""
from pathlib import Path

VERSION = 'PREFLIGHT_CARD_SYNC_V2'
ROOT = Path(__file__).resolve().parents[1]


def replace_once(text: str, old: str, new: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ValueError(f'Expected one UI hook, found {count}: {old[:90]}')
    return text.replace(old, new, 1)


def build(page: str, module: str) -> str:
    if f'/* {VERSION}: compiled */' in page:
        return page
    start = '    function reapplyPreflightCardSnapshots(report){'
    end = '    function renderPreflight(data){'
    if page.count(start) != 1 or page.count(end) != 1:
        raise ValueError('Preflight module boundaries are missing or duplicated')
    a, b = page.index(start), page.index(end)
    if a >= b:
        raise ValueError('Unexpected preflight function order')
    page = page[:a] + f'/* {VERSION}: compiled */\n' + module + '\n' + page[b:]
    page = replace_once(page,
        'function decisionPanel(item){return decisionPanelBody(item)}',
        'function decisionPanel(item){return decoratePreflightCard(item,decisionPanelBody(item))}')
    page = replace_once(page,
        'function suggestedAction(item){',
        'function suggestedAction(item){\n      const latestAdvice=preflightSuggestedAction(item);if(latestAdvice)return latestAdvice;')
    page = replace_once(page,
        'renderPreflight(data);applyPreflightToSignalCard(data);refreshPreflightHistoryRate();',
        "renderPreflight(data);let cardSynced=false;try{cardSynced=applyPreflightToSignalCard(data)}catch(syncError){console.error('Preflight card sync failed',syncError)}preflightCardSyncNotice(cardSynced);refreshPreflightHistoryRate();")
    page = replace_once(page,
        'item.historical_performance,item.timeframe_states,item.trigger_type',
        'item.preflight_snapshot,item.execution_quality?.score,item.historical_performance,item.timeframe_states,item.trigger_type')
    # A returning page rerenders stored updates without fetching another price.
    page = replace_once(page,
        "function hidePreflight(){const returnFocus=state.preflightReturnFocus;",
        "function hidePreflight(){if(state.report){state.reportRenderKey=null;renderReport(state.report)}const returnFocus=state.preflightReturnFocus;")
    page = replace_once(page, '</head>',
        '<meta name="radar-ui-build" content="preflight-card-sync-v2">\n</head>')
    return page


if __name__ == '__main__':
    path = ROOT / 'radar/static/pages.html'
    module = (ROOT / 'radar/static/preflight-card-sync.js').read_text(encoding='utf-8')
    result = build(path.read_text(encoding='utf-8'), module)
    path.write_text(result, encoding='utf-8')
    print(f'Dashboard built: {VERSION}', flush=True)
