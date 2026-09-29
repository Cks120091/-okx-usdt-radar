'use strict';
// Exercise the production request path without exchange calls or order placement.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../radar/static/pages.html'),'utf8');
function source(name){
  const start=html.search(new RegExp('    (?:async )?function '+name+'\\('));
  assert.ok(start>=0,'Missing function '+name);
  const tail=html.slice(start),next=tail.slice(1).search(/\n    (?:async )?function /);
  return next<0?tail:tail.slice(0,next+1);
}
function env(payload){
  const elements=new Map(),node=()=>({textContent:'',innerHTML:'',disabled:false,setAttribute(){}});
  const report={signals:[{inst_id:'CFX-USDT-SWAP',trigger_id:'cfx-1',execution_quality:{score:90},market_metrics:{entry_execution_price:.0566},entry_low:.05653,entry_high:.05658,stop_loss:.0561,take_profit_1:.0578},{inst_id:'BTC-USDT-SWAP',execution_quality:{score:80}}]};
  function freeze(x){Object.freeze(x);for(const v of Object.values(x))if(v&&typeof v==='object')freeze(v);return x;}
  const ctx={console,AbortController,URLSearchParams,tech:x=>String(x),esc:x=>String(x).replaceAll('<','&lt;'),taiwanTime:x=>String(x),window:{},state:{report:freeze(report),reportRenderKey:'original',preflight:{instId:'CFX-USDT-SWAP',horizon:'SHORT',triggerId:'cfx-1'},preflightRequestSeq:0},
    num:(n,d)=>Number(n).toFixed(d),normalizedHorizon:x=>x,preflightRequestIsCurrent:()=>true,
    api:async()=>payload,renderPreflight:data=>ctx.rendered=data,refreshPreflightHistoryRate(){},loadReport:async()=>{ctx.terminalRefreshes=(ctx.terminalRefreshes||0)+1;},
    $:selector=>{if(!elements.has(selector))elements.set(selector,node());return elements.get(selector);}};
  Object.assign(ctx,{itemFinalDecision:item=>item.decision_context?.final||{},isTerminalSignal:item=>item.lifecycle?.terminal===true,isPreviewItem:item=>item.preview===true,isExpiredSnapshot:item=>item.expired===true,itemReadOnlyReason:item=>item.read_only===true});
  vm.createContext(ctx);
  for(const name of ['isFormalSignal','signalGroups','shortSignalGroups','longSignalGroups','signalPreparationView','shortPreparationView','currentEntryBadge','preflightQualityScore','preflightQualityComparison','preflightCoreReason','preflightPresentation','preflightQualityExplanation','preflightDataTimes','preflightEarlyWarning','preflightTerminalKind','preflightResponseTerminal','loadPreflight'])vm.runInContext(source(name),ctx);
  return ctx;
}
const payload={inst_id:'CFX-USDT-SWAP',trigger_id:'cfx-1',horizon:'SHORT',direction:'LONG',entry_policy_version:'SIGNAL_POSITION_SEPARATED_V1',original:{quality_score:90},live:{quality_score:65,price:.05669},verdict:{status:'HARD_GATE_BLOCKED',new_entry_allowed:false,label:'核心訊號條件未成立',reason:'核心條件：NO_FORMAL_TRIGGER',hard_blockers:['NO_FORMAL_TRIGGER']},signal_lifecycle:{status:'ACTIVE',terminal:false}};
function longSignal(direction='SHORT'){
  return {inst_id:'LONG-TEST-USDT-SWAP',trigger_id:'original-long-plan',radar_horizon:'LONG',direction,signal_stage:'EARLY_SIGNAL',execution_quality:{score:95},entry_low:.6061,entry_high:.6086,stop_loss:.6553,take_profit_1:.4954,take_profit_2:.3895,decision_context:{final:{status:'ENTER',new_entry_allowed:true,timeframe_alignment:{required:true,passed:true,state:'ALIGNED',timeframe:'1D',trigger_timeframe:'1H',timeframe_direction:direction,trigger_direction:direction}}}};
}
let passed=0;
async function test(name,fn){await fn();passed++;console.log('PASS '+name);}
(async()=>{
  await test('only the exact OKX XAU perpetual alias is normalized and chart links preserve its venue',()=>{
    const c=env(payload);c.displaySymbol=x=>x.replace('-USDT-SWAP','');
    for(const name of ['normalizeInstrumentQuery','chartInstrument','tvUrl'])vm.runInContext(source(name),c);
    for(const value of ['XAU','XAUUSDT','XAU/USDT','XAU-USDT-SWAP','XAUUSDT.P','OKX:XAUUSDT.P',' okx:xauusdt.p '])assert.equal(c.normalizeInstrumentQuery(value),'XAU-USDT-SWAP');
    for(const value of ['BINANCE:XAUUSDT.P','BYBIT:XAUUSDT.P','XAGUSDT.P','XAUTUSDT.P','XAUUSDT.P.EXTRA'])assert.equal(c.normalizeInstrumentQuery(value),'');
    assert.equal(c.normalizeInstrumentQuery('BTC'),'BTC-USDT-SWAP');
    assert.ok(c.tvUrl('XAU-USDT-SWAP').includes('/symbols/XAUUSDT.P/?exchange=OKX'));
    assert.equal(c.tvUrl('BTC-USDT-SWAP'),'https://tw.tradingview.com/symbols/BTCUSDT.P/?exchange=OKX&utm_source=iosapp&utm_medium=share');
  });
  await test('both chart buttons preserve the exact OKX perpetual and reject invalid identifiers',()=>{
    const c=env(payload);for(const name of ['chartInstrument','tvUrl','okxChartUrl','chartLinks'])vm.runInContext(source(name),c);
    for(const symbol of ['BTC','TRX','HUMA','XAU','XAUT','1000SATS']){
      const id=symbol+'-USDT-SWAP',out=c.chartLinks(id);
      assert.equal(c.okxChartUrl(id),symbol==='BTC'?'https://okx.com/ul/x4F1Vb2':'https://www.okx.com/trade-swap/'+id.toLowerCase());
      assert.equal(c.tvUrl(id),'https://tw.tradingview.com/symbols/'+symbol+'USDT.P/?exchange=OKX&utm_source=iosapp&utm_medium=share');
      assert.equal((out.match(/<a /g)||[]).length,2);assert.ok(out.includes('noopener noreferrer'));
    }
    for(const id of [null,'BTC-USDT','BTC-USD-SWAP','<script>','BTC-USDT-SWAP" onclick="alert(1)','BINANCE:BTCUSDT.P'])assert.equal(c.chartLinks(id),'');
    assert.ok(source('decisionPanelBody').includes('chartLinks(item.inst_id)'));
    assert.ok(source('quickLinks').includes('chartLinks(item.inst_id)'));
    assert.ok(source('renderPreflight').includes('chartLinks(data.inst_id)'));
  });
  await test('XAU stays visible as a symbol but does not change crypto market breadth',()=>{
    const c=env(payload);vm.runInContext(source('renderMarketHeat'),c);
    const crypto=[{inst_id:'BTC-USDT-SWAP',direction:'LONG'},{inst_id:'ETH-USDT-SWAP',direction:'SHORT'}],gold={inst_id:'XAU-USDT-SWAP',direction:'LONG'},mixed=[...crypto,gold],before=JSON.stringify(mixed);
    c.renderMarketHeat(crypto);const expected=c.$('#marketHeat').innerHTML;
    c.renderMarketHeat(mixed);assert.equal(c.$('#marketHeat').innerHTML,expected);assert.equal(JSON.stringify(mixed),before);
    c.renderMarketHeat([gold]);assert.ok(c.$('#marketHeat').innerHTML.includes('做多 0'));assert.ok(!c.$('#marketHeat').innerHTML.includes('NaN'));
    const formal=longSignal();formal.inst_id=gold.inst_id;
    assert.equal(c.longSignalGroups({long_signals:[formal]}).formal[0],formal);
    formal.decision_context.final.timeframe_alignment.passed=false;
    assert.equal(c.longSignalGroups({long_signals:[formal]}).preparing[0],formal);
  });
  await test('successful and repeated refresh leave original list, prices, quality and order intact',async()=>{
    const c=env(payload),report=c.state.report,before=JSON.stringify(report);
    await c.loadPreflight();assert.equal(c.rendered,payload);
    c.api=async()=>({...payload,live:{quality_score:97,price:.05672}});
    await c.loadPreflight(true);assert.equal(c.rendered.live.quality_score,97);
    assert.equal(c.state.report,report);assert.equal(JSON.stringify(report),before);assert.equal(c.state.reportRenderKey,'original');
  });
  await test('failed refresh does not rewrite list or manufacture a new score',async()=>{
    const c=env(payload),before=JSON.stringify(c.state.report);c.api=async()=>{throw Error('quote unavailable');};
    await c.loadPreflight(true);assert.equal(c.rendered,undefined);assert.equal(JSON.stringify(c.state.report),before);
    assert.ok(c.$('#preflightContent').innerHTML.includes('目前無法完成進場前更新'));
  });
  await test('terminal plans still refresh authoritative lifecycle and disable repeat updates',async()=>{
    const c=env({...payload,signal_lifecycle:{status:'INVALIDATED',terminal:true}});
    await c.loadPreflight();assert.equal(c.terminalRefreshes,1);assert.equal(c.state.preflight.terminal,true);assert.equal(c.$('#preflightRefresh').disabled,true);
  });
  await test('quality comparison uses distinct API original and current values',()=>{
    const c=env(payload),out=c.preflightQualityComparison(payload);
    assert.ok(out.includes('90.0'));assert.ok(out.includes('65.0'));assert.ok(out.includes('data-quality-change="lower"'));
    assert.ok(c.preflightQualityComparison({...payload,live:{quality_score:96}}).includes('data-quality-change="improved"'));
  });
  await test('missing or invalid quality remains unknown while zero is valid',()=>{
    const c=env(payload);
    for(const value of [null,undefined,'',false,NaN,Infinity,-1,101])assert.equal(c.preflightQualityScore(value),null);
    assert.equal(c.preflightQualityScore(0),0);
    const out=c.preflightQualityComparison({original:{},live:{quality_score:0}});
    assert.ok(out.includes('—'));assert.ok(out.includes('0.0'));assert.ok(out.includes('data-quality-change="unknown"'));
  });
  await test('changed scoring basis is neutral and confirmation stays separate from quality deductions',()=>{
    const c=env(payload),data={...payload,quality_explanation:{basis_changed:true,mode:'CURRENT_ONLY',reasons:['本次止損距離評分 3.0 分。'],note:'評分方式不同'},confirmation_assessment:{pending:true,note:'<提醒>此提醒不另扣位置分。'}};
    const comparison=c.preflightQualityComparison(data),explanation=c.preflightQualityExplanation(data);
    assert.ok(comparison.includes('data-quality-change="unknown"'));
    assert.ok(comparison.includes('不直接比較'));
    assert.ok(explanation.includes('收線確認提醒'));
    assert.ok(explanation.includes('&lt;提醒>'));
    assert.ok(!c.preflightQualityExplanation({...data,signal_lifecycle:{terminal:true,status:'INVALIDATED'}}).includes('收線確認提醒'));
  });
  await test('formal trigger explanation is readable and does not clear its blocker',()=>{
    const c=env(payload),before=JSON.stringify(payload),p=c.preflightPresentation(payload);
    assert.ok(p.detail.includes('目前訊號記錄未符合正式觸發狀態'));assert.ok(!p.detail.includes('NO_FORMAL_TRIGGER'));
    assert.equal(JSON.stringify(payload),before);assert.equal(payload.verdict.new_entry_allowed,false);
    const multiple=c.preflightCoreReason({verdict:{reason:'核心條件：NO_FORMAL_TRIGGER、CORE_CANDLE_UNCONFIRMED'}});
    assert.ok(multiple.includes('尚未確認收盤'));
  });
  await test('terminal verdict takes precedence over formal trigger wording',()=>{
    const c=env(payload),p=c.preflightPresentation({...payload,signal_lifecycle:{status:'INVALIDATED',terminal:true}});
    assert.equal(p.label,'訊號已失效');
  });
  await test('stored scan suspension and unavailable data stay distinct',()=>{
    const c=env(payload),data={...payload,core_assessment:{state:'ENTRY_SUSPENDED',reasons:[{detail:'最近掃描未獲延續'}]}};
    assert.equal(c.preflightPresentation(data).label,'原計畫保留，暫停新進場');
    assert.equal(c.preflightPresentation(data).detail,'最近掃描未獲延續');
    assert.equal(c.preflightPresentation({...data,core_assessment:{state:'DATA_UNAVAILABLE'}}).label,'資料不足，暫時無法確認');
    assert.equal(c.preflightPresentation({...data,signal_lifecycle:{status:'INVALIDATED',terminal:true}}).label,'訊號已失效');
  });
  await test('fresh execution time never substitutes for absent quote or core time',()=>{
    const c=env(payload),times=c.preflightDataTimes({original:{report_generated_at:'scan'},live:{sampled_at:'newer-execution'},data_times:{quote_at:null,execution_at:'execution'}});
    assert.equal(times.quote,'時間未提供');assert.equal(times.core,'scan');assert.equal(times.execution,'execution');
    assert.equal(c.preflightDataTimes({}).core,'掃描時間未提供');
  });
  await test('quality reason content is escaped and original missing details are explicit',()=>{
    const c=env(payload),out=c.preflightQualityExplanation({quality_explanation:{mode:'CURRENT_ONLY',reasons:['<script>'],note:'原品質明細未保存'}});
    assert.ok(!out.includes('<script>'));assert.ok(out.includes('原品質明細未保存'));
  });
  await test('scan warning is escaped, tied to its source time and hidden on terminal plans',()=>{
    const c=env(payload),data={...payload,early_warning:{text:'1H 仍偏多 <script>',advisory_only:true,source:'STORED_SCAN',scan_at:'original-scan'}};
    const out=c.preflightEarlyWarning(data);
    assert.ok(out.includes('最近掃描提醒'));assert.ok(out.includes('僅供觀察'));assert.ok(out.includes('original-scan'));assert.ok(!out.includes('<script>'));
    assert.ok(c.preflightEarlyWarning({...data,early_warning:{...data.early_warning,source:'SINGLE_SCAN',scan_at:null}}).includes('掃描時間未提供'));
    assert.equal(c.preflightEarlyWarning({...data,signal_lifecycle:{status:'INVALIDATED',terminal:true}}),'');
    assert.equal(c.preflightEarlyWarning({...payload,early_warning:{}}),'');
    assert.equal(data.verdict.new_entry_allowed,false);
  });
  await test('only aligned formal short signals reach the signal group; pending plans remain immutable',()=>{
    const c=env(payload),signal={inst_id:'A',trigger_id:'old-plan',radar_horizon:'SHORT',direction:'LONG',signal_stage:'CONFIRMED',entry_low:100,stop_loss:98,take_profit_1:106,decision_context:{final:{status:'ENTER',new_entry_allowed:true,timeframe_alignment:{required:true,passed:true,timeframe:'1H',trigger_timeframe:'15m',timeframe_direction:'LONG',trigger_direction:'LONG'}}}};
    for(const direction of ['LONG','SHORT']){
      const aligned=structuredClone(signal);aligned.direction=direction;Object.assign(aligned.decision_context.final.timeframe_alignment,{timeframe_direction:direction,trigger_direction:direction});
      assert.equal(c.shortSignalGroups({signals:[aligned]}).formal.length,1);
      const opposed=structuredClone(aligned);opposed.inst_id='B';Object.assign(opposed.decision_context.final.timeframe_alignment,{passed:false,timeframe_direction:direction==='LONG'?'SHORT':'LONG'});
      const waiting={inst_id:'C',radar_horizon:'SHORT',direction:'NEUTRAL',status:'WATCH'},continuation={inst_id:'D',radar_horizon:'SHORT',direction, status:'PRE_CONTINUATION'};
      const report={signals:[aligned,opposed],watchlist:[waiting,continuation,{...waiting,inst_id:'A'}]},before=JSON.stringify(report),groups=c.shortSignalGroups(report);
      assert.equal(groups.formal.length,1);assert.equal(groups.preparing.length,3);assert.ok(groups.preparing.includes(opposed));assert.equal(JSON.stringify(report),before);
      assert.equal(opposed.stop_loss,98);assert.equal(opposed.trigger_id,'old-plan');
    }
  });
  await test('aligned but untriggered, unknown direction, failed data and previews are not formal signals',()=>{
    const c=env(payload),item={inst_id:'A',direction:'LONG',signal_stage:'CONFIRMED',decision_context:{final:{status:'ENTER',new_entry_allowed:true,timeframe_alignment:{required:true,passed:true,timeframe:'1H',trigger_timeframe:'15m',timeframe_direction:'LONG',trigger_direction:'LONG'}}}};
    for(const change of [{signal_stage:'PRE_TRIGGER'},{preview:true},{expired:true},{read_only:true}]){
      const result=c.shortSignalGroups({signals:[{...item,...change}]});assert.equal(result.formal.length,0);assert.equal(result.preparing.length,1);
    }
    for(const final of [{status:'DATA_UNAVAILABLE'},{status:'WAIT',new_entry_allowed:false},{status:'ENTER',new_entry_allowed:true,timeframe_alignment:{}}]){
      const result=c.shortSignalGroups({signals:[{...item,decision_context:{final}}]});assert.equal(result.formal.length,0);assert.equal(result.preparing.length,1);
    }
    const closed={...item,lifecycle:{terminal:true}};assert.equal(c.shortSignalGroups({signals:[closed],watchlist:[closed]}).preparing.length,0);
    const long={...item,radar_horizon:'LONG'},report={signals:[],long_signals:[long]},before=JSON.stringify(report);c.shortSignalGroups(report);assert.equal(JSON.stringify(report),before);
  });
  await test('preparation labels distinguish missing data, awaiting alignment and awaiting trigger',()=>{
    const c=env(payload);
    assert.ok(c.shortPreparationView({decision_context:{final:{status:'DATA_UNAVAILABLE'}}}).label.includes('資料不足'));
    assert.ok(c.shortPreparationView({decision_context:{final:{timeframe_alignment:{required:true,passed:false,reason:'1H 多、15m 空'}}}}).reason.includes('1H 多、15m 空'));
    assert.ok(c.shortPreparationView({market_story:{preparation:{advisory_only:true,label:'預備｜等待觸發',reason:'等待同向價格觸發'}}}).reason.includes('同向價格觸發'));
  });
  await test('opposed long plans leave formal and quality lists, remain accessible once, and can return only after confirmation',()=>{
    const c=env(payload);
    for(const direction of ['LONG','SHORT']){
      const aligned=longSignal(direction),opposed=structuredClone(aligned);opposed.inst_id='OPPOSED';
      Object.assign(opposed.decision_context.final,{status:'WAIT',new_entry_allowed:false});
      Object.assign(opposed.decision_context.final.timeframe_alignment,{passed:false,state:'NOT_ALIGNED',timeframe_direction:direction==='LONG'?'SHORT':'LONG',reason:'週期方向不同步'});
      const watch={inst_id:'WATCH',radar_horizon:'LONG',direction,status:'PRE_TRIGGER'},closed={...aligned,inst_id:'CLOSED',lifecycle:{terminal:true}},report={long_signals:[aligned,opposed,closed],long_watchlist:[watch,{...watch,inst_id:'OPPOSED'},{...watch,inst_id:aligned.inst_id},closed]},before=JSON.stringify(report);
      let groups=c.longSignalGroups(report);
      assert.equal(groups.formal.length,1);assert.equal(groups.formal[0],aligned);assert.equal(groups.preparing.length,2);assert.equal(groups.preparing[0],opposed);assert.equal(JSON.stringify(report),before);
      assert.equal(c.signalPreparationView(opposed).label,'預備｜等待同向');assert.ok(!c.currentEntryBadge(opposed).includes('訊號已觸發'));
      const promoted=structuredClone(opposed);promoted.decision_context=structuredClone(aligned.decision_context);
      groups=c.longSignalGroups({long_signals:[aligned,promoted],long_watchlist:[{...watch,inst_id:'OPPOSED'}]});
      assert.equal(groups.formal.length,2);assert.equal(groups.preparing.length,0);
      for(const key of ['trigger_id','entry_low','entry_high','stop_loss','take_profit_1','take_profit_2'])assert.equal(promoted[key],opposed[key]);
    }
  });
  await test('long missing alignment, neutral direction, stale data and pending core never display triggered badges',()=>{
    const c=env(payload),aligned=longSignal();Object.assign(c,{triggerKindBadge:()=>'',stageBadge:()=>''});
    assert.ok(c.currentEntryBadge(aligned).includes('訊號已觸發'));
    for(const change of [{signal_stage:'PRE_TRIGGER'},{preview:true},{expired:true},{read_only:true}]){
      const item={...aligned,...change};assert.equal(c.longSignalGroups({long_signals:[item]}).formal.length,0);assert.ok(!c.currentEntryBadge(item).includes('訊號已觸發'));
    }
    for(const change of [{passed:false,timeframe_direction:'NEUTRAL'},{passed:null,state:'UNKNOWN'},{timeframe:'4H'},{timeframe_direction:'LONG'}]){
      const item=structuredClone(aligned);Object.assign(item.decision_context.final.timeframe_alignment,change);
      assert.equal(c.longSignalGroups({long_signals:[item]}).formal.length,0);assert.ok(!c.currentEntryBadge(item).includes('訊號已觸發'));
    }
    const missing=structuredClone(aligned);delete missing.decision_context.final.timeframe_alignment;
    assert.equal(c.longSignalGroups({long_signals:[missing]}).formal.length,0);assert.ok(c.currentEntryBadge(missing).includes('資料不足'));
    const terminal={...aligned,lifecycle:{terminal:true}};assert.ok(c.currentEntryBadge(terminal).includes('交易已結束'));
  });
  await test('long report rendering uses the same formal group for count and 80-point filter',()=>{
    const c=env(payload),draws=new Map(),aligned=longSignal(),opposed=structuredClone(aligned);opposed.inst_id='OPPOSED';
    Object.assign(opposed.decision_context.final,{status:'WAIT',new_entry_allowed:false});Object.assign(opposed.decision_context.final.timeframe_alignment,{passed:false,timeframe_direction:'LONG'});
    Object.assign(c,{setHomeReportEmpty(){},pruneExpiredTerminalCards(){},reportRenderFingerprint:()=> 'new-report',captureReportUiState:()=>null,restoreReportUiState(){},activeUiScanMode:()=> 'FULL',horizonTransientState:()=>null,horizonSnapshot:()=>({available:true}),horizonReadOnlyReason:()=>null,metricNumber:x=>typeof x==='number'?x:null,signalSortComparator:()=>0,retainedClosedSignals:()=>[],itemWaitingForEntry:()=>false,itemEntryStatus:()=> 'ENTRY_READY',terminalSortComparator:()=>0,renderContextCoverage(){},renderMap(){},renderFavorites(){},renderOverview(){},renderSignals:(items,selector)=>draws.set(selector,items),renderWatchlist:(items,selector)=>draws.set(selector,items)});
    vm.runInContext(source('renderReport'),c);
    c.renderReport({long_signals:[aligned,opposed],long_watchlist:[{...opposed,trigger_id:null}],signals:[]});
    assert.equal(c.$('#longSignalCount').textContent,'1');
    for(const selector of ['#longSignalsBox','#longQuality80Box']){assert.equal(draws.get(selector).length,1);assert.equal(draws.get(selector)[0],aligned);}
    assert.equal(draws.get('#longWatchBox').length,1);assert.equal(draws.get('#longWatchBox')[0],opposed);
  });
  await test('long preparation cards name the pending state, escape reasons and retain plan links',()=>{
    const c=env(payload),opposed=longSignal();Object.assign(opposed.decision_context.final,{status:'WAIT',new_entry_allowed:false});Object.assign(opposed.decision_context.final.timeframe_alignment,{passed:false,timeframe_direction:'LONG',reason:'1D 偏多，1H 做空 <script>'});
    const before=JSON.stringify(opposed);
    Object.assign(c,{readOnlyReferenceBanner:()=>'',metricNumber:x=>typeof x==='number'?x:null,displaySymbol:x=>x,favoriteButton:()=>'',itemDataPage:()=>'<button>查看數據與完整理由</button>',quickLinks:item=>`<button data-preflight-trigger-id="${item.trigger_id}">4H 進場前更新</button>`});
    vm.runInContext(source('renderPreparations'),c);vm.runInContext(source('renderWatchlist'),c);
    c.renderWatchlist([opposed],'#longWatchBox');const out=c.$('#longWatchBox').innerHTML;
    for(const text of ['預備｜等待同向','尚不可新進場','原計畫品質','original-long-plan','signal-horizon">4H'])assert.ok(out.includes(text));
    assert.ok(!out.includes('訊號已觸發'));assert.ok(!out.includes('<script>'));assert.equal(JSON.stringify(opposed),before);
  });
  await test('remaining R summary uses refreshed values for both horizons and explains unavailable cases',()=>{
    const c=env(payload);c.metricNumber=x=>typeof x==='number'&&Number.isFinite(x)?x:null;
    for(const name of ['preflightRemainingMetric','preflightRemainingSummary'])vm.runInContext(source(name),c);
    for(const horizon of ['SHORT','LONG']){
      const data={horizon,live:{remaining_rr:2.75,remaining_rr_applicable:true},signal_lifecycle:{status:'ACTIVE'},verdict:{status:'ENTRY_READY'}};
      assert.ok(c.preflightRemainingSummary(data).includes('2.75R'));
      data.live.remaining_rr=1.25;assert.ok(c.preflightRemainingSummary(data).includes('1.25R'));
      Object.assign(data.live,{remaining_rr:0,remaining_rr_applicable:false,adverse_atr:.1});
      const out=c.preflightRemainingSummary(data);assert.ok(out.includes('暫不適用'));assert.ok(out.includes('現價位於進場區不利側'));assert.ok(!out.includes('0.00R'));
      data.live={};assert.ok(c.preflightRemainingSummary(data).includes('本次資料不足'));
      data.signal_lifecycle={status:'STOP_LOSS',terminal:true};assert.ok(c.preflightRemainingSummary(data).includes('交易計畫已結束'));
    }
    assert.ok(source('renderPreflight').includes('${preflightRemainingSummary(data)}</div>${preflightQualityComparison(data)}'));
  });
  console.log(`${passed} preflight detail regressions passed`);
})().catch(error=>{console.error(error);process.exitCode=1;});
