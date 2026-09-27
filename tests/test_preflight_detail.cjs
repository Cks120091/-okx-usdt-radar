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
  vm.createContext(ctx);
  for(const name of ['preflightQualityScore','preflightQualityComparison','preflightCoreReason','preflightPresentation','preflightQualityExplanation','preflightDataTimes','preflightEarlyWarning','preflightTerminalKind','preflightResponseTerminal','loadPreflight'])vm.runInContext(source(name),ctx);
  return ctx;
}
const payload={inst_id:'CFX-USDT-SWAP',trigger_id:'cfx-1',horizon:'SHORT',direction:'LONG',entry_policy_version:'SIGNAL_POSITION_SEPARATED_V1',original:{quality_score:90},live:{quality_score:65,price:.05669},verdict:{status:'HARD_GATE_BLOCKED',new_entry_allowed:false,label:'核心訊號條件未成立',reason:'核心條件：NO_FORMAL_TRIGGER',hard_blockers:['NO_FORMAL_TRIGGER']},signal_lifecycle:{status:'ACTIVE',terminal:false}};
let passed=0;
async function test(name,fn){await fn();passed++;console.log('PASS '+name);}
(async()=>{
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
  console.log(`${passed} preflight detail regressions passed`);
})().catch(error=>{console.error(error);process.exitCode=1;});
