/* Compiled into pages.html by scripts/build_preflight_sync.py.
 * Snapshot projection only. Never rewrite a Trigger, its type, direction, or plan.
 */
const PREFLIGHT_CARD_SYNC_VERSION = 'PREFLIGHT_CARD_SYNC_V2';
const PREFLIGHT_CARD_STORAGE = 'okx-radar-preflight-cards-v2';
const PREFLIGHT_CARD_MAX_AGE = 30 * 60 * 1000;

function pfNumber(value){
  if(value===null||value===undefined||typeof value==='boolean'||(typeof value==='string'&&!value.trim()))return null;
  const n=Number(value);return Number.isFinite(n)?n:null;
}
function pfTime(value){
  if(value===null||value===undefined||value==='')return null;
  if(typeof value==='number')return Number.isFinite(value)?(value<1e11?value*1000:value):null;
  const n=Date.parse(value);return Number.isFinite(n)?n:null;
}
function pfIdentity(horizon,instId,triggerId){
  return JSON.stringify([horizon,String(instId||''),String(triggerId||'')]);
}
function pfTerminal(item){
  const life=item?.lifecycle||{};
  return life.terminal===true||[item?.signal_stage,life.status,item?.entry_eligibility?.status].some(
    v=>['INVALIDATED','COMPLETED','CLOSED_UNKNOWN','TARGET_REACHED','PLAN_INVALIDATED'].includes(String(v||'').toUpperCase())
  );
}
function pfSnapshotValid(snapshot){
  const n=pfNumber(snapshot?.payload?.live?.price),stamp=pfTime(snapshot?.payload?.live?.sampled_at);
  return snapshot?.version===PREFLIGHT_CARD_SYNC_VERSION&&['SHORT','LONG'].includes(snapshot.horizon)
    &&!!snapshot.inst_id&&!!snapshot.trigger_id&&n!==null&&n>0&&stamp!==null
    &&stamp<=Date.now()+120000&&Date.now()-stamp<=PREFLIGHT_CARD_MAX_AGE;
}
function pfStore(){
  if(!state.preflightCardStoreV2){
    state.preflightCardStoreV2=new Map();
    try{
      const rows=JSON.parse(sessionStorage.getItem(PREFLIGHT_CARD_STORAGE)||'[]');
      if(Array.isArray(rows))for(const s of rows.slice(-200))if(pfSnapshotValid(s))
        state.preflightCardStoreV2.set(pfIdentity(s.horizon,s.inst_id,s.trigger_id),s);
    }catch(_){ /* Storage is optional; an in-memory update must still work. */ }
  }
  for(const [key,s] of state.preflightCardStoreV2)if(!pfSnapshotValid(s))state.preflightCardStoreV2.delete(key);
  return state.preflightCardStoreV2;
}
function pfPersist(){
  const store=pfStore();
  while(store.size>200)store.delete(store.keys().next().value);
  try{sessionStorage.setItem(PREFLIGHT_CARD_STORAGE,JSON.stringify([...store.values()]));return true;}
  catch(_){return false;}
}
function pfReportTime(report,horizon){
  const value=report?.horizon_freshness?.[horizon]?.completed_at
    ||report?.[horizon==='LONG'?'long_completed_at':'short_completed_at'];
  return pfTime(value)||0;
}
function pfDevelopment(item,payload){
  if(preflightResponseTerminal(payload))return '原交易計畫已結束';
  if(payload.verdict?.new_entry_allowed===false)return payload.verdict.label||'原訊號保留｜最新條件待確認';
  const value=pfNumber(payload.live?.price),low=pfNumber(item.entry_low),high=pfNumber(item.entry_high);
  if(value!==null&&low!==null&&high!==null){
    if(value>=Math.min(low,high)&&value<=Math.max(low,high))return '目前位於原進場區｜回踩確認依原規則';
    const favorable=item.direction==='LONG'?value>Math.max(low,high):value<Math.min(low,high);
    return favorable?'價格已往原方向延伸｜留意追價':'目前往原方向的反向移動｜留意原止損';
  }
  return '已更新原訊號目前狀態';
}
function pfProject(item,snapshot){
  item=item._preflight_base||item;
  const p=snapshot.payload,live=p.live,stamp=pfTime(live.sampled_at);
  const source=live.price_source==='BEST_ASK'?'ASK':live.price_source==='BEST_BID'?'BID':String(live.price_source||'PREFLIGHT');
  // Map the actual /api/preflight schema. Do not copy stale RSI/MA/K-line fields.
  const metrics={...item.market_metrics,entry_execution_price:Number(live.price),
    entry_execution_price_source:source,preflight_sampled_at:live.sampled_at};
  if(pfNumber(live.ticker_last_price)!==null)metrics.last_price=Number(live.ticker_last_price);
  const eligibility={...item.entry_eligibility};
  for(const k of ['remaining_rr','remaining_rr_applicable','chase_atr','adverse_atr'])
    if(Object.prototype.hasOwnProperty.call(live,k))eligibility[k]=live[k];
  const quality={...item.execution_quality};
  if(Object.prototype.hasOwnProperty.call(live,'quality_score'))quality.score=pfNumber(live.quality_score);
  if(live.quality_label!==undefined)quality.label=live.quality_label;
  if(live.quality_recommendation!==undefined)quality.recommendation=live.quality_recommendation;
  return {...item,_preflight_base:item,market_metrics:metrics,entry_eligibility:eligibility,execution_quality:quality,
    preflight_snapshot:{version:PREFLIGHT_CARD_SYNC_VERSION,source:'PREFLIGHT',sampled_at:live.sampled_at,
      live_price:Number(live.price),price_source:source,development:pfDevelopment(item,p),
      payload:p,original_scan_price:snapshot.original_scan_price,identity:snapshot.identity},
    preflight_updated_at:stamp};
}
function reapplyPreflightCardSnapshots(report){
  if(!report)return report;
  const store=pfStore(),result={...report};
  for(const [field,horizon] of [['signals','SHORT'],['long_signals','LONG']]){
    if(!Array.isArray(report[field]))continue;
    result[field]=report[field].map(item=>{
      if(pfTerminal(item)){
        store.delete(pfIdentity(horizon,item.inst_id,item.trigger_id));
        const {preflight_snapshot,_preflight_base,...terminal}=item;
        return terminal;
      }
      item=item._preflight_base||item;
      const key=pfIdentity(horizon,item.inst_id,item.trigger_id),s=store.get(key);
      if(!s)return item;
      // A new scan, new episode/direction/plan, or terminal state supersedes an old quote.
      if(pfTerminal(item)||pfReportTime(report,horizon)>pfTime(s.payload.live.sampled_at)
        ||String(item.direction)!==String(s.direction)||pfPlanIdentity(item)!==s.identity){
        store.delete(key);return item;
      }
      return pfProject(item,s);
    });
  }
  pfPersist();
  return result;
}
function pfPlanIdentity(item){
  return JSON.stringify([String(item.trigger_type||item.market_story?.trigger?.type||''),String(item.direction||''),
    item.entry_low??null,item.entry_high??null,item.stop_loss??null,item.take_profit_1??null,item.take_profit_2??null]);
}
function applyPreflightToSignalCard(data){
  const horizon=String(data?.horizon||'').toUpperCase(),field=horizon==='LONG'?'long_signals':'signals';
  if(!['SHORT','LONG'].includes(horizon)||!data?.inst_id||!data?.trigger_id||!state.report)return false;
  const item=(state.report[field]||[]).find(x=>x.inst_id===data.inst_id&&String(x.trigger_id)===String(data.trigger_id));
  if(!item||pfTerminal(item)||String(item.direction)!==String(data.direction))return false;
  const live=data.live||{},stamp=pfTime(live.sampled_at);
  if(stamp===null||pfReportTime(state.report,horizon)>stamp)return false;
  const identity=pfPlanIdentity(item),key=pfIdentity(horizon,item.inst_id,item.trigger_id),store=pfStore(),previous=store.get(key);
  if(previous&&pfTime(previous.payload.live.sampled_at)>stamp)return false;
  // Persist a bounded public snapshot, not original plan objects or arbitrary API fields.
  let presentation={label:data.verdict?.label||'原訊號目前狀態',detail:data.verdict?.reason||''};
  if(typeof preflightPresentation==='function'){try{presentation=preflightPresentation(data)}catch(_){}}
  const payload={presentation:{label:String(presentation.label||''),detail:String(presentation.detail||'')},inst_id:data.inst_id,trigger_id:data.trigger_id,horizon,direction:data.direction,
    trigger_type:item.trigger_type||item.market_story?.trigger?.type,entry_policy_version:data.entry_policy_version,
    live:{...live},verdict:{...data.verdict},signal_lifecycle:{...data.signal_lifecycle},
    plan_state:{...data.plan_state},position_advisory:{...data.position_advisory},
    continuation_confirmation:data.continuation_confirmation||null,
    continuation_status:data.continuation?.refresh_failed?'更新失敗':String(data.continuation?.current?.label||'')};
  const snapshot={version:PREFLIGHT_CARD_SYNC_VERSION,horizon,inst_id:item.inst_id,
    trigger_id:String(item.trigger_id),direction:item.direction,identity,payload,
    original_scan_price:previous?.original_scan_price??pfNumber(data.original?.price)
      ??pfNumber(item.market_metrics?.entry_execution_price)};
  if(!pfSnapshotValid(snapshot))return false;
  store.set(key,snapshot);pfPersist();
  state.reportRenderKey=null;
  renderReport(state.report);
  const synced=(state.report?.[field]||[]).find(x=>x.inst_id===item.inst_id&&String(x.trigger_id)===String(item.trigger_id));
  return synced?.preflight_snapshot?.sampled_at===live.sampled_at
    &&pfNumber(synced?.preflight_snapshot?.live_price)===pfNumber(live.price);
}
function preflightCardSyncNotice(ok){
  document.getElementById('preflightCardSyncNotice')?.remove();
  const root=document.querySelector('#preflightContent .preflight-verdict');
  if(!root)return;
  const p=document.createElement('p');p.id='preflightCardSyncNotice';p.setAttribute('role','status');
  p.textContent=ok?'✓ 已同步回原訊號卡：最新價格、時間與本次狀態。'
    :'卡片尚未同步：本次更新資料仍可查看，請返回確認是否已換成新訊號。';
  p.style.cssText='margin-top:12px;font-size:12px;line-height:1.6;color:'+(ok?'#8bdac1':'#f1c77b');
  root.appendChild(p);
}
function decoratePreflightCard(item,html){
  const snapshot=item?.preflight_snapshot;
  if(snapshot?.version!==PREFLIGHT_CARD_SYNC_VERSION||pfTerminal(item))return html;
  const p=snapshot.payload,template=document.createElement('template');template.innerHTML=html;
  const root=template.content.querySelector('.decision-panel');if(!root)return html;
  root.dataset.preflightSync=PREFLIGHT_CARD_SYNC_VERSION;
  const presentation=p.presentation||{label:p.verdict?.label||'原訊號目前狀態',detail:p.verdict?.reason||''},quote=root.querySelector('.decision-price');
  if(quote){
    const label=quote.querySelector('span'),amount=quote.querySelector('b');
    if(label)label.textContent='進場前更新價格'+(snapshot.price_source==='ASK'?'｜買入參考':snapshot.price_source==='BID'?'｜賣出參考':'');
    if(amount)amount.textContent=price(snapshot.live_price,item);
  }
  const title=root.querySelector('.decision-state');
  if(title)title.textContent=presentation.label+'｜'+entryReasonLabel(item);
  const top=root.querySelector('.decision-top');
  const note=root.querySelector(':scope > p.signal-position-note');
  if(note)note.textContent=presentation.detail||p.verdict?.reason||snapshot.development;
  // Remove only the obsolete scan-time maturity sentence, not plan safety text.
  root.querySelectorAll(':scope > small.signal-position-note').forEach(n=>{if(/^行情已/.test(n.textContent))n.remove();});
  const stamp=document.createElement('p');stamp.className='signal-position-note preflight-card-stamp';
  stamp.textContent='本卡更新：'+taiwanTime(snapshot.sampled_at)+'（台灣時間）；原訊號與進出場計畫不變。';
  if(top)top.after(stamp);
  return template.innerHTML;
}
function preflightSuggestedAction(item){
  const s=item?.preflight_snapshot;
  if(s?.version!==PREFLIGHT_CARD_SYNC_VERSION||pfTerminal(item))return null;
  const p=s.payload,parts=[p.verdict?.reason||s.development];
  if(p.verdict?.new_entry_allowed===false)parts.unshift('本次更新不允許新進場；高分不能解除這個限制。');
  if(p.position_advisory?.note)parts.push(p.position_advisory.note);
  if(p.continuation_status)parts.push('續走輔助資料：'+p.continuation_status+'。');
  if(p.continuation_confirmation?.label)parts.push('原方向續走資料：'+p.continuation_confirmation.label+'（輔助）。');
  parts.push('價格偏移不等於已確認回踩守住；原 Entry、SL、TP 與訊號種類保持不變。');
  return {title:'依 '+taiwanTime(s.sampled_at)+' 的進場前更新',body:parts.join(' ')};
}
