from pathlib import Path


def exact(path, old, new):
    p = Path(path)
    text = p.read_text()
    count = text.count(old)
    assert count == 1, f"{path}: expected 1 match, got {count}: {old[:100]!r}"
    p.write_text(text.replace(old, new))


# Bump replay schema because the aggregate payload now contains per-symbol groups.
for root in ("radar", "scripts", "tests", "docs"):
    for p in Path(root).rglob("*"):
        if p.is_file() and p.suffix in {".py", ".js", ".html", ".md"}:
            text = p.read_text()
            if "HISTORY_PRICE_REPLAY_V2" in text:
                p.write_text(text.replace("HISTORY_PRICE_REPLAY_V2", "HISTORY_PRICE_REPLAY_V3"))

# Aggregate both the 8-major pooled cohort and each major token's own cohort.
p = Path("radar/history_replay.py")
text = p.read_text()
start = text.index("def aggregate(results: list[dict[str, Any]], *, complete: bool, days: int = 7) -> dict[str, Any]:")
replacement = '''def aggregate(results: list[dict[str, Any]], *, complete: bool, days: int = 7) -> dict[str, Any]:
    required_days = minimum_sample_days(days)

    def add_sample(groups: dict[str, Any], sample: dict[str, Any]) -> None:
        group = groups.setdefault(sample['cohort'], {'label': sample['label'], 'wins': 0,
               'losses': 0, 'timeout': 0, 'unknown': 0, 'days': set(), 'total': 0})
        group['total'] += 1
        group['days'].add(sample['entry_ms'] // DAY)
        key = {'TP1_FIRST': 'wins', 'SL_FIRST': 'losses', 'TIMEOUT': 'timeout'}.get(sample['outcome'], 'unknown')
        group[key] += 1

    def finish(groups: dict[str, Any]) -> None:
        for group in groups.values():
            group['days'] = len(group['days'])
            n = group['wins'] + group['losses']
            group['resolved'] = n
            group['coverage_pct'] = round(100 * n / group['total'], 1) if group['total'] else 0
            group['minimum_days'] = required_days
            available = (complete and n >= MIN_RESOLVED and group['days'] >= required_days
                         and n / group['total'] >= MIN_RESOLVED_COVERAGE)
            group['status'] = 'AVAILABLE' if available else 'INSUFFICIENT' if complete else 'PARTIAL'
            group['rate_pct'] = round(100 * group['wins'] / n, 1) if available else None
            group['interval_pct'] = wilson(group['wins'], n) if available else None

    groups: dict[str, Any] = {}
    symbol_groups: dict[str, dict[str, Any]] = {}
    for result in results:
        inst_id = str(result.get('inst_id') or '')
        own = symbol_groups.setdefault(inst_id, {}) if inst_id else None
        for sample in result.get('samples', []):
            add_sample(groups, sample)
            if own is not None:
                add_sample(own, sample)
    finish(groups)
    for own in symbol_groups.values():
        finish(own)
    return {'groups': groups, 'symbol_groups': symbol_groups,
            'samples': sum(group['total'] for group in groups.values()),
            'minimum_days': required_days,
            'episodes': sum(result.get('episodes', 0) for result in results),
            'entry_attempts': sum(result.get('entry_attempts', 0) for result in results),
            'missing_windows': sum(result.get('missing_windows', 0) for result in results)}
'''
p.write_text(text[:start] + replacement)

exact(
    "radar/history_jobs.py",
    "                      'groups': {}, 'total': 0, 'done': 0, 'failed': 0,",
    "                      'groups': {}, 'symbol_groups': {}, 'total': 0, 'done': 0, 'failed': 0,",
)
exact(
    "radar/history_jobs.py",
    "            output['scope'] = ('固定8支大型主要代幣：BTC、ETH、SOL、XRP、DOGE、ADA、LINK、AVAX；'\n                               '逐時點仍套用歷史24H成交額門檻，不延伸到其他小幣。')",
    "            output['scope'] = ('歷史工作固定掃8支大型主要代幣：BTC、ETH、SOL、XRP、DOGE、ADA、LINK、AVAX；'\n                               '其他幣卡片可引用相同情境的大型幣合併樣本，但會明確標示不是本幣專屬勝率。')",
)

exact(
    "radar/static/history-replay.js",
    "  let data = null, groups = new Map(), fetching = false;",
    "  let data = null, groups = new Map(), symbolGroups = new Map(), fetching = false;",
)
old = """    const known=data?.schema_version==='HISTORY_PRICE_REPLAY_V3',group=key?groups.get(key):null;
    const n=finite(group?.resolved),wins=finite(group?.wins),losses=finite(group?.losses),count=finite(group?.total),days=finite(group?.days);
    const covered=Array.isArray(data?.covered_inst_ids)&&data.covered_inst_ids.includes(inst);
    const enough=known&&data.compatible===true&&terminal.has(data.status)&&covered&&group?.status==='AVAILABLE'
      &&[n,wins,losses,count,days].every(v=>v!==null&&Number.isInteger(v)&&v>=0)&&n===wins+losses&&count>=n
      &&n>=50&&days>=Number(data.minimum_days||5)&&n/count>=.8&&finite(data.scope_coverage_pct)>=80;
    const headline=enough?`${(100*wins/n).toFixed(1)}%`:!known?'尚未載入回測':!terminal.has(data.status)?(titles[data.status]||'尚未完成'):
      !covered?'此幣不在8支大型幣樣本／歷史不足':!key?'當前計畫無法配對':!group?'同類情境樣本不足':'同類樣本／覆蓋不足';
    let detail=enough?`已判定 ${n} 筆：TP1 先達 ${wins}｜SL 先達 ${losses}`:group?`同類已判定 ${n??0} 筆；至少50筆、${Number(data?.minimum_days||5)}個取樣日及80%結果覆蓋才顯示`:'不套用五幣測試、全市場總勝率或交易品質分數。';
    if(known&&data.total) detail+=`；標的處理 ${data.done}/${data.total}，完整覆蓋 ${data.covered_symbols??0} 個。`;
    const ci=enough&&Array.isArray(group.interval_pct)&&group.interval_pct.length===2&&group.interval_pct.every(v=>finite(v)!==null)?`Wilson 95%描述區間 ${group.interval_pct[0]}%～${group.interval_pct[1]}%；未校正幣種相關性。`:'';
    const extra=group?`期限未達 ${group.timeout??0}｜結果不明 ${group.unknown??0}；跨幣同類樣本。`:'';"""
new = """    const known=data?.schema_version==='HISTORY_PRICE_REPLAY_V3',pooled=key?groups.get(key):null;
    const ownMap=symbolGroups.get(inst),own=key&&ownMap?ownMap.get(key):null;
    const covered=Array.isArray(data?.covered_inst_ids)&&data.covered_inst_ids.includes(inst);
    const validGroup=group=>{
      const n=finite(group?.resolved),wins=finite(group?.wins),losses=finite(group?.losses),count=finite(group?.total),days=finite(group?.days);
      return group?.status==='AVAILABLE'&&[n,wins,losses,count,days].every(v=>v!==null&&Number.isInteger(v)&&v>=0)
        &&n===wins+losses&&count>=n&&n>=50&&days>=Number(data?.minimum_days||5)&&n/count>=.8;
    };
    const baseReady=known&&data.compatible===true&&terminal.has(data.status)&&finite(data.scope_coverage_pct)>=80;
    const ownEnough=baseReady&&covered&&validGroup(own),pooledEnough=baseReady&&validGroup(pooled);
    const group=ownEnough?own:pooledEnough?pooled:null,source=ownEnough?'本幣歷史樣本':pooledEnough?'8支大型幣同類情境樣本':'';
    const n=finite(group?.resolved),wins=finite(group?.wins),losses=finite(group?.losses),count=finite(group?.total),days=finite(group?.days);
    const enough=Boolean(group);
    const headline=enough?`${(100*wins/n).toFixed(1)}%`:!known?'尚未載入回測':!terminal.has(data.status)?(titles[data.status]||'尚未完成'):
      !key?'當前計畫無法配對':pooled||own?'本幣／同類樣本不足':'同類情境樣本不足';
    let detail=enough?`${source}｜已判定 ${n} 筆：TP1 先達 ${wins}｜SL 先達 ${losses}`:(own||pooled)?`目前樣本未達門檻；至少50筆、${Number(data?.minimum_days||5)}個取樣日及80%結果覆蓋才顯示`:'沒有符合本卡條件的歷史樣本。';
    if(known&&data.total) detail+=`；大型幣處理 ${data.done}/${data.total}，完整覆蓋 ${data.covered_symbols??0} 個。`;
    const ci=enough&&Array.isArray(group.interval_pct)&&group.interval_pct.length===2&&group.interval_pct.every(v=>finite(v)!==null)?`Wilson 95%描述區間 ${group.interval_pct[0]}%～${group.interval_pct[1]}%；未校正幣種相關性。`:'';
    const extra=group?`期限未達 ${group.timeout??0}｜結果不明 ${group.unknown??0}；${ownEnough?'本幣樣本。':'跨8支大型幣同類情境樣本，非本幣專屬勝率。'}`:'';"""
exact("radar/static/history-replay.js", old, new)
exact(
    "radar/static/history-replay.js",
    "<p>固定8支大型主要代幣樣本；不重播歷史OI／CVD、實際價差、滑價或全市場前20名排序。其他小幣不直接套用此回測百分比。</p>",
    "<p>歷史工作固定取樣8支大型主要代幣；本幣樣本達標時優先使用本幣，否則只引用相同情境的大型幣合併樣本並標示來源。不重播歷史OI／CVD、實際價差、滑價或全市場前20名排序。</p>",
)
exact(
    "radar/static/history-replay.js",
    "      data=await response.json();groups=new Map();\n      if(data.schema_version==='HISTORY_PRICE_REPLAY_V3'&&data.compatible===true)for(const [key,value]of Object.entries(data.groups||{})){try{groups.set(JSON.stringify(JSON.parse(key)),value)}catch(_){}}",
    "      data=await response.json();groups=new Map();symbolGroups=new Map();\n      if(data.schema_version==='HISTORY_PRICE_REPLAY_V3'&&data.compatible===true){\n        for(const [key,value]of Object.entries(data.groups||{})){try{groups.set(JSON.stringify(JSON.parse(key)),value)}catch(_){}}\n        for(const [inst,bucket]of Object.entries(data.symbol_groups||{})){const mapped=new Map();for(const [key,value]of Object.entries(bucket||{})){try{mapped.set(JSON.stringify(JSON.parse(key)),value)}catch(_){}}symbolGroups.set(inst,mapped);}\n      }",
)

exact(
    "radar/static/history-scan.html",
    "掃描範圍固定為8支大型主要代幣：BTC、ETH、SOL、XRP、DOGE、ADA、LINK、AVAX。逐時點仍核對當時的24H成交額門檻；不掃其他小幣，讓短線回測更快完成。",
    "歷史工作固定掃8支大型主要代幣：BTC、ETH、SOL、XRP、DOGE、ADA、LINK、AVAX。其他幣不另外下載歷史資料；卡片會先用本幣樣本，若不足則引用8大幣相同情境樣本並清楚標示來源。",
)

exact(
    "docs/HISTORY_SCAN.md",
    "quality-score conversion, five-coin constant, or 15m-rate reuse on a 4H card.",
    "quality-score conversion, five-coin constant, or 15m-rate reuse on a 4H card.\nFor any 15m card, an adequate same-symbol cohort is preferred. If that symbol is\nnot in the eight replay instruments or its same-symbol cohort is inadequate, an\nadequate pooled eight-major cohort with the exact same setup tuple may be shown\nas an explicitly labeled reference. This fallback is not represented as that\ncoin's own historical win rate.",
)
exact(
    "docs/HISTORY_SCAN.md",
    "configuration, strategy threshold or ranking is provisioned by this patch.",
    "configuration, strategy threshold or ranking is provisioned by this patch.\nThe fallback changes only which already-computed aggregate is displayed; it does\nnot cause additional symbols to be downloaded or replayed.",
)

exact(
    "tests/test_history_replay.py",
    "    def test_partial_never_advertises_full_market_percentage(self):",
    "    def test_symbol_groups_are_separate_and_pooled_group_is_preserved(self):\n        btc=[sample(i%5,'TP1_FIRST') for i in range(50)]\n        eth=[sample(i%5,'SL_FIRST') for i in range(50)]\n        result=aggregate([{'inst_id':'BTC-USDT-SWAP','samples':btc},{'inst_id':'ETH-USDT-SWAP','samples':eth}],complete=True,days=7)\n        key='[\\\"SHORT\\\",\\\"LONG\\\"]'\n        self.assertEqual(result['symbol_groups']['BTC-USDT-SWAP'][key]['rate_pct'],100)\n        self.assertEqual(result['symbol_groups']['ETH-USDT-SWAP'][key]['rate_pct'],0)\n        self.assertEqual(result['groups'][key]['rate_pct'],50)\n\n    def test_partial_never_advertises_full_market_percentage(self):",
)

exact(
    "scripts/check_history_replay_ui.py",
    "          'scope_coverage_pct':100,'minimum_days':5,'groups':{key:{'label':'模擬情境','status':'AVAILABLE','resolved':50,\n          'wins':31,'losses':19,'total':50,'days':5,'rate_pct':62,'unknown':0,'timeout':0,'interval_pct':[48.1,74.1]}}}",
    "          'scope_coverage_pct':100,'minimum_days':5,'groups':{key:{'label':'模擬情境','status':'AVAILABLE','resolved':50,\n          'wins':31,'losses':19,'total':50,'days':5,'rate_pct':62,'unknown':0,'timeout':0,'interval_pct':[48.1,74.1]}},\n          'symbol_groups':{item['inst_id']:{key:{'label':'模擬情境','status':'AVAILABLE','resolved':50,'wins':40,'losses':10,'total':50,'days':5,'rate_pct':80,'unknown':0,'timeout':0,'interval_pct':[66.9,89.1]}}}}",
)
exact(
    "scripts/check_history_replay_ui.py",
    "    case('available',lambda d:None,'62.0%')",
    "    case('available-own',lambda d:None,'80.0%')",
)
exact(
    "scripts/check_history_replay_ui.py",
    "            # No heavy POST happens when opening a normal card / home page.\n            assert not page.evaluate(\"window.__historyPosts\")",
    "            outsider=copy.deepcopy(item);outsider['inst_id']='MINA-USDT-SWAP'\n            page.evaluate(\"data=>window.__historyData=data\",good);page.evaluate('HistoryReplay.refresh()')\n            outsider_html=page.evaluate('item=>HistoryReplay.card(item)',outsider)\n            page.locator('#fifteenAllBox').evaluate('(el,html)=>el.innerHTML=html',outsider_html)\n            page.evaluate('HistoryReplay.refresh()')\n            page.wait_for_function('document.querySelector(\\\"#fifteenAllBox .history-replay-card\\\")?.textContent.includes(\\\"62.0%\\\")')\n            assert '8支大型幣同類情境樣本' in page.locator('#fifteenAllBox .history-replay-card').inner_text()\n            # No heavy POST happens when opening a normal card / home page.\n            assert not page.evaluate(\"window.__historyPosts\")",
)

assert "symbol_groups" in Path("radar/history_replay.py").read_text()
assert "HISTORY_PRICE_REPLAY_V3" in Path("radar/static/history-replay.js").read_text()
print("Applied per-coin + pooled historical sample fallback patch")
