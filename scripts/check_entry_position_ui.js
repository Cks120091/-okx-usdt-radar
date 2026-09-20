// Execute production renderers, not text-presence assertions.
'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('radar/static/pages.html','utf8');
const code=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m=>m[1]).join('\n');
new vm.Script(code); // Validate the whole inline application, including untouched paths.
function declaration(name){
 const re=new RegExp('(?:^|\\n)    function '+name+'\\('),m=re.exec(code);
 assert(m,`Missing ${name}`);
 const start=m.index+(code[m.index]==='\n'?1:0),tail=code.slice(start+5);
 const next=/\n    (?:async )?function /.exec(tail);
 return code.slice(start,next?start+5+next.index:code.length);
}
const context=vm.createContext({console,JSON,Number,Math,String,Boolean,Array,Object,Set,Date,Intl,
 state:{report:{runtime_status:'FRESH'},scanStarting:false,favorites:new Set()},
 itemSnapshotEntryState:i=>({label:i.lifecycle?.terminal?'交易已結束':'等待確認'}),
 terminalSignalOutcome:i=>i.lifecycle?.terminal?'CLOSED_UNKNOWN':null,
 isTerminalSignal:i=>i.lifecycle?.terminal===true,
 itemReadOnlyReason:i=>i.read_only_reason||null,
 isExpiredSnapshot:i=>i.expired===true,
 isPreviewItem:i=>i.preview===true,
 normalizedHorizon:h=>h==='LONG'?'LONG':'SHORT',
 activePreflightSignal:()=>null,
 signalDataUnavailable:i=>i.data_quality?.core==='UNAVAILABLE',
 preflightDisabledLabel:()=> '資料待更新',
});
for(const name of ['isRecord','itemDecisionContext','itemHardGate','metricNumber','num','esc','tickPrecision','pricePrecision','price','planTargetR','signalTradeGrid','preflightSignalUsable','preflightButton','singleScanButton','preflightActions','separatedSignalView','positionAdviceText','separatedSignalCard','preflightTerminalKind','preflightPresentation']){
 vm.runInContext(declaration(name),context);
}
const fixtures=JSON.parse(fs.readFileSync(0,'utf8'));
for(const row of fixtures.cards){
 const before=JSON.stringify(row.item),result=context.separatedSignalCard(row.item);
 assert.equal(JSON.stringify(row.item),before,'renderer mutated input');
 assert(result.includes('可進位置（參考）'));
 assert(!result.includes('<details'));
 assert(!result.includes('data-detail-key'));
 if(row.expectedActive){
  assert(result.includes(`${row.item.direction==='LONG'?'做多':'做空'}訊號已觸發`));
  assert(result.includes('data-preflight-id='));
  assert(!result.includes('disabled title'));
  assert(!result.includes('必要條件未成立'));
  assert(result.includes(row.item.decision_context.final.entry_position.label));
 }else{
  assert(!result.includes('⚡ 做多訊號已觸發')&&!result.includes('⚡ 做空訊號已觸發'));
 }
}
for(const row of fixtures.preflight){
 const result=context.preflightPresentation(row.payload);
 if(row.expectedActive){
  assert(result.label.includes('訊號已觸發'));
  assert(result.detail.includes(row.payload.entry_position.label));
 }else{
  assert(!result.label.includes('訊號已觸發'));
 }
}
console.log(`UI behavior PASS: ${fixtures.cards.length} cards, ${fixtures.preflight.length} preflight states; complete JS syntax valid`);
