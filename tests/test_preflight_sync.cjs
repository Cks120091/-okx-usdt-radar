/* Public response contract regression tests; no exchange calls or orders. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../radar/static/preflight-card-sync.js'),'utf8');
const mem=new Map();const stamp=Date.now(),t=ms=>new Date(ms).toISOString();
function env(){
 const ctx={console,sessionStorage:{getItem:k=>mem.get(k)||null,setItem:(k,v)=>mem.set(k,v)},
  state:{report:null},preflightResponseTerminal:p=>p.signal_lifecycle?.terminal===true,
  renderReport:r=>{ctx.state.report=ctx.reapplyPreflightCardSnapshots(r);}};
 vm.createContext(ctx);vm.runInContext(source,ctx);return ctx;
}
const item=(h='SHORT',id='slx-1')=>({inst_id:'SLX-USDT-SWAP',trigger_id:id,radar_horizon:h,direction:'LONG',
 trigger_type:'BREAKOUT',signal_stage:'EARLY_SIGNAL',entry_low:.07101,entry_high:.07108,
 stop_loss:.07018,take_profit_1:.07315,take_profit_2:.07424,market_metrics:{entry_execution_price:.07151,last_price:.07150,rsi_core:54.8},
 execution_quality:{score:76},entry_eligibility:{status:'ENTRY_READY',remaining_rr:1.23},lifecycle:{status:'ACTIVE'}});
const report=(items=[item()],long=[])=>({signals:items,long_signals:long,short_completed_at:t(stamp-600000),long_completed_at:t(stamp-600000)});
const payload=(h='SHORT',id='slx-1',price=.07160,time=stamp)=>({inst_id:'SLX-USDT-SWAP',trigger_id:id,horizon:h,direction:'LONG',trigger_type:'BREAKOUT',
 original:{price:.07151},live:{price,ticker_last_price:price-.00001,sampled_at:t(time),price_source:'BEST_ASK',remaining_rr:1.08,quality_score:74,quality_label:'普通'},
 verdict:{status:'ENTRY_READY',label:'做多訊號仍有效',reason:'現價高於可進位置；可等回踩。',new_entry_allowed:true},signal_lifecycle:{status:'ACTIVE',terminal:false}});
let passed=0;
function test(name,fn){mem.clear();fn();passed++;console.log('PASS '+name);}
test('SLX 0.07151 -> 0.07160 and actual live.quality_score/live.remaining_rr fields',()=>{
 const c=env(),r=report(),before=JSON.stringify(r);c.state.report=r;assert.equal(c.applyPreflightToSignalCard(payload()),true);
 const x=c.state.report.signals[0];assert.equal(x.preflight_snapshot.live_price,.0716);assert.equal(x.market_metrics.entry_execution_price,.0716);
 assert.equal(x.execution_quality.score,74);assert.equal(x.entry_eligibility.remaining_rr,1.08);
 assert.equal(x.market_metrics.rsi_core,54.8);assert.equal(JSON.stringify(r),before,'input report never mutated');
 for(const k of ['trigger_id','trigger_type','direction','entry_low','entry_high','stop_loss','take_profit_1','take_profit_2'])assert.equal(x[k],r.signals[0][k]);
});
test('background old report does not erase a newer snapshot',()=>{
 const c=env();c.state.report=report();c.applyPreflightToSignalCard(payload());c.renderReport(report());assert.equal(c.state.report.signals[0].market_metrics.entry_execution_price,.0716);
});
test('page reload restores same-tab snapshot (regression of memory-only Map)',()=>{
 const c=env();c.state.report=report();c.applyPreflightToSignalCard(payload());const reload=env();reload.renderReport(report());
 assert.equal(reload.state.report.signals[0].preflight_snapshot.live_price,.0716);
});
test('15m and long-horizon snapshots never cross-contaminate, even same trigger id',()=>{
 const c=env();c.state.report=report([item()],[item('LONG')]);c.applyPreflightToSignalCard(payload());assert.equal(c.state.report.long_signals[0].market_metrics.entry_execution_price,.07151);
 c.applyPreflightToSignalCard(payload('LONG','slx-1',.072));assert.equal(c.state.report.long_signals[0].preflight_snapshot.live_price,.072);
 assert.equal(c.state.report.signals[0].preflight_snapshot.live_price,.0716);
});
test('older out-of-order response cannot overwrite newer price',()=>{
 const c=env();c.state.report=report();c.applyPreflightToSignalCard(payload());assert.equal(c.applyPreflightToSignalCard(payload('SHORT','slx-1',.07155,stamp-1000)),false);
 assert.equal(c.state.report.signals[0].preflight_snapshot.live_price,.0716);
});
test('new scan supersedes the older update',()=>{
 const c=env();c.state.report=report();c.applyPreflightToSignalCard(payload());const r=report();r.short_completed_at=t(stamp+1000);r.signals[0].market_metrics.entry_execution_price=.0717;c.renderReport(r);
 assert.equal(c.state.report.signals[0].market_metrics.entry_execution_price,.0717);assert.equal(c.state.report.signals[0].preflight_snapshot,undefined);
});
test('new trigger and changed plan retain their own prices',()=>{
 const c=env();c.state.report=report();c.applyPreflightToSignalCard(payload());c.renderReport(report([item('SHORT','slx-2')]));assert.equal(c.state.report.signals[0].preflight_snapshot,undefined);
 const changed=item();changed.entry_low=.07;c.renderReport(report([changed]));assert.equal(c.state.report.signals[0].preflight_snapshot,undefined);
});
test('terminal plan cannot be resurrected by active saved quote',()=>{
 const c=env();c.state.report=report();c.applyPreflightToSignalCard(payload());const x=item();x.signal_stage='INVALIDATED';x.lifecycle.status='INVALIDATED';c.renderReport(report([x]));
 assert.equal(c.state.report.signals[0].preflight_snapshot,undefined);assert.equal(c.state.report.signals[0].signal_stage,'INVALIDATED');
});
test('failed/mismatched quote cannot become a successful update',()=>{
 for(const patch of [{live:{price:null}},{live:{price:0}},{live:{price:NaN}},{horizon:'WRONG'},{direction:'SHORT'},{trigger_id:'OTHER'}]){
  const c=env();c.state.report=report();assert.equal(c.applyPreflightToSignalCard({...payload(),...patch}),false);assert.equal(c.state.report.signals[0].market_metrics.entry_execution_price,.07151);
 }
});
test('sessionStorage failure does not break immediate UI sync',()=>{
 const c=env();c.sessionStorage.setItem=()=>{throw Error('unavailable')};c.state.report=report();assert.equal(c.applyPreflightToSignalCard(payload()),true);
});
test('expired saved quote is ignored rather than made fresh',()=>{
 const c=env();c.state.report=report();assert.equal(c.applyPreflightToSignalCard(payload('SHORT','slx-1',.0716,stamp-31*60000)),false);
});
test('same-price newer timestamp is retained and rerendered',()=>{
 const c=env();c.state.report=report();c.applyPreflightToSignalCard(payload());assert.equal(c.applyPreflightToSignalCard(payload('SHORT','slx-1',.0716,stamp+1000)),true);
 assert.equal(c.state.report.signals[0].preflight_snapshot.sampled_at,t(stamp+1000));
});
test('terminal transition on a projected object also wins over its base',()=>{
 const c=env();c.state.report=report();c.applyPreflightToSignalCard(payload());
 c.state.report.signals[0]={...c.state.report.signals[0],signal_stage:'INVALIDATED',lifecycle:{status:'INVALIDATED'}};
 c.renderReport(c.state.report);assert.equal(c.state.report.signals[0].signal_stage,'INVALIDATED');assert.equal(c.state.report.signals[0].preflight_snapshot,undefined);
});
console.log(`${passed} regression tests passed`);
