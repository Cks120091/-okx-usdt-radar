/* Read-only historical model source. Never changes a signal or permission. */
(() => {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let data = null, groups = new Map(), symbolGroups = new Map(), fetching = false;
  const finite = value => value !== null && value !== undefined && typeof value !== 'boolean' && Number.isFinite(Number(value)) ? Number(value) : null;
  const terminal = new Set(['COMPLETE','PARTIAL_COMPLETE']);
  const active = new Set(['QUEUED','RUNNING','WAITING_LIVE_SCAN']);
  const titles = {IDLE:'尚未執行歷史掃描',QUEUED:'準備歷史掃描',RUNNING:'歷史掃描中',WAITING_LIVE_SCAN:'優先處理即時掃描，歷史工作暫候',PAUSED:'已暫停，可續跑',INTERRUPTED:'工作中斷，可續跑',COMPLETE:'歷史掃描完成',PARTIAL_COMPLETE:'本輪已結束，部分歷史不足',ERROR:'歷史工作失敗，請查看原因',VERSION_CHANGED:'策略或設定已變更，舊回測不混用'};
  const triggerNames = {CONTINUATION:'回踩續走',BREAKOUT:'突破',REVERSAL:'反轉',REENTRY:'再次觸發'};

  function keyFor(item) {
    if (item?.radar_horizon !== 'SHORT' || !['LONG','SHORT'].includes(item.direction)) return null;
    const entry=finite(item.market_metrics?.entry_execution_price),stop=finite(item.stop_loss),target=finite(item.take_profit_1);
    if ([entry,stop,target].some(v=>v===null||v<=0)) return null;
    const risk=item.direction==='LONG'?entry-stop:stop-entry,reward=item.direction==='LONG'?target-entry:entry-target;
    if(risk<=0||reward<=0) return null;
    const rr=reward/risk,band=rr<2?'<2R':rr<3?'2–<3R':rr<4?'3–<4R':rr<6?'4–<6R':'≥6R';
    const bg=item.timeframe_states?.['4H']?.direction,relation=bg===item.direction?'同向背景':['LONG','SHORT'].includes(bg)?'逆高週期背景':bg==='NEUTRAL'?'中性背景':'背景未知';
    return JSON.stringify(['SHORT',item.direction,String(item.trigger_type||'UNKNOWN'),String(item.signal_stage||'UNKNOWN'),relation,band]);
  }

  function parseKey(key) {
    try {
      const value=JSON.parse(key);
      return Array.isArray(value)&&value.length===6?value:null;
    } catch (_) { return null; }
  }

  function groupNumbers(group) {
    const wins=finite(group?.wins),losses=finite(group?.losses),total=finite(group?.total),days=finite(group?.days);
    if([wins,losses,total,days].some(v=>v===null||!Number.isInteger(v)||v<0)) return null;
    const resolved=wins+losses;
    if(total<resolved) return null;
    return {wins,losses,total,days,resolved,coverage:total?resolved/total:0};
  }

  function wilson(wins,n) {
    if(!Number.isInteger(wins)||!Number.isInteger(n)||n<=0||wins<0||wins>n) return null;
    const z=1.96,z2=z*z,p=wins/n,den=1+z2/n;
    const center=(p+z2/(2*n))/den;
    const margin=z*Math.sqrt((p*(1-p)+z2/(4*n))/n)/den;
    return [Math.max(0,100*(center-margin)),Math.min(100,100*(center+margin))].map(v=>Math.round(v*10)/10);
  }

  function quality(group) {
    const numbers=groupNumbers(group);
    if(!numbers||numbers.resolved<10||numbers.coverage<.8) return null;
    const fullDays=Math.max(1,Number(data?.minimum_days||5));
    let tier='低樣本參考',rank=1;
    if(numbers.resolved>=50&&numbers.days>=fullDays){tier='樣本充足';rank=3;}
    else if(numbers.resolved>=20&&numbers.days>=2){tier='中等樣本';rank=2;}
    const dayNote=numbers.days<fullDays?`取樣日 ${numbers.days} 天，低於完整門檻 ${fullDays} 天`:'';
    return {...numbers,tier,rank,dayNote};
  }

  function mergedGroup(key, level) {
    const target=parseKey(key);
    if(!target) return null;
    const matched=[];
    for(const [candidateKey,group] of groups.entries()){
      const candidate=parseKey(candidateKey);
      if(!candidate) continue;
      const sameBase=candidate[0]===target[0]&&candidate[1]===target[1]&&candidate[2]===target[2]&&candidate[4]===target[4];
      if(!sameBase) continue;
      if(level==='STAGE'&&candidate[5]!==target[5]) continue;
      matched.push(group);
    }
    if(!matched.length) return null;
    const totals={wins:0,losses:0,total:0,days:0,unknown:0,timeout:0};
    for(const group of matched){
      const n=groupNumbers(group);
      if(!n) continue;
      totals.wins+=n.wins;totals.losses+=n.losses;totals.total+=n.total;totals.days=Math.max(totals.days,n.days);
      totals.unknown+=Math.max(0,finite(group?.unknown)||0);totals.timeout+=Math.max(0,finite(group?.timeout)||0);
    }
    const resolved=totals.wins+totals.losses;
    if(!totals.total) return null;
    const side=target[1]==='LONG'?'多':'空',kind=triggerNames[target[2]]||'其他觸發';
    const label=level==='STAGE'
      ?`15m ${side}｜${kind}｜${target[4]}｜${target[5]}｜合併訊號階段`
      :`15m ${side}｜${kind}｜${target[4]}｜合併訊號階段與R區間`;
    return {...totals,resolved,label,interval_pct:wilson(totals.wins,resolved)};
  }

  function candidate(group,source,match,own=false) {
    const q=quality(group);
    return q?{group,q,source,match,own}:null;
  }

  function chooseCandidate(key,inst) {
    const covered=Array.isArray(data?.covered_inst_ids)&&data.covered_inst_ids.includes(inst);
    const ownMap=symbolGroups.get(inst),own=covered&&key&&ownMap?ownMap.get(key):null,pooled=key?groups.get(key):null;
    const exact=[candidate(own,'本幣歷史樣本','精準情境',true),candidate(pooled,'8支大型幣同類情境樣本','精準情境',false)].filter(Boolean);
    if(exact.length){
      exact.sort((a,b)=>b.q.rank-a.q.rank||(a.own===b.own?b.q.resolved-a.q.resolved:(a.own?-1:1)));
      return {selected:exact[0],own,pooled};
    }
    const stage=mergedGroup(key,'STAGE');
    const stageCandidate=candidate(stage,'8支大型幣近似情境樣本','合併訊號階段',false);
    if(stageCandidate) return {selected:stageCandidate,own,pooled,stage};
    const broad=mergedGroup(key,'BROAD');
    const broadCandidate=candidate(broad,'8支大型幣較寬鬆情境樣本','合併訊號階段＋R區間',false);
    if(broadCandidate) return {selected:broadCandidate,own,pooled,stage,broad};
    return {selected:null,own,pooled,stage,broad};
  }

  function textFor(key,horizon,inst) {
    const link='<a class="history-replay-link" href="/history-scan">開啟大型幣歷史掃描 →</a>';
    if(horizon!=='SHORT') return '<h3>歷史 K 棒回測</h3><p>本版先支援 15m；不把短線結果套用到 4H。</p>'+link;
    const known=data?.schema_version==='HISTORY_PRICE_REPLAY_V3';
    const baseReady=known&&data.compatible===true&&terminal.has(data.status)&&finite(data.scope_coverage_pct)>=80;
    const picked=key?chooseCandidate(key,inst):{selected:null};
    const selected=baseReady?picked.selected:null;
    const group=selected?.group,q=selected?.q;
    const bestObserved=[picked.own,picked.pooled,picked.stage,picked.broad].map(groupNumbers).filter(Boolean).sort((a,b)=>b.resolved-a.resolved)[0]||null;
    const headline=selected?`${(100*q.wins/q.resolved).toFixed(1)}%`:!known?'尚未載入回測':!terminal.has(data.status)?(titles[data.status]||'尚未完成'):
      !key?'當前計畫無法配對':bestObserved?'樣本不足':'同類情境樣本不足';
    let detail='';
    if(selected){
      detail=`${selected.source}｜${q.tier}｜${selected.match}｜已判定 ${q.resolved} 筆：TP1 先達 ${q.wins}｜SL 先達 ${q.losses}`;
      if(q.dayNote) detail+=`；${q.dayNote}`;
    }else if(bestObserved){
      detail=`目前最多 ${bestObserved.resolved} 筆已判定樣本；至少10筆且結果可判定率需達80%，才顯示低樣本參考。`;
    }else{
      detail='沒有符合本卡方向與觸發結構的歷史樣本。';
    }
    if(known&&data.total) detail+=`；大型幣處理 ${data.done}/${data.total}，完整覆蓋 ${data.covered_symbols??0} 個。`;
    const ci=selected?`Wilson 95%描述區間 ${(group.interval_pct||wilson(q.wins,q.resolved)||[])[0]??'—'}%～${(group.interval_pct||wilson(q.wins,q.resolved)||[])[1]??'—'}%；未校正幣種相關性。`:'';
    let extra='';
    if(selected){
      extra=`期限未達 ${group.timeout??0}｜結果不明 ${group.unknown??0}；${selected.own?'本幣樣本。':'跨8支大型幣同類樣本，非本幣專屬勝率。'}`;
      if(selected.match!=='精準情境') extra+=` 為避免樣本被切得過碎，本卡已${selected.match}；只作近期統計參考。`;
      if(q.tier!=='樣本充足') extra+=` ${q.tier}的不確定性較高，不應單獨作為進場依據。`;
    }
    const period=known&&data.start_ms&&data.end_ms?new Date(data.start_ms).toLocaleString('zh-TW',{timeZone:'Asia/Taipei'})+' ～ '+new Date(data.end_ms).toLocaleString('zh-TW',{timeZone:'Asia/Taipei'}):'—';
    return `<div class="history-replay-heading"><h3>歷史 K 棒回測｜15m</h3><strong data-replay-rate>${esc(headline)}</strong></div><p>${esc(detail)}</p><small>模擬 TP1 先達率，未扣費；非本單預測，不改進場資格。</small><details><summary>回測來源與限制</summary><p>${esc(group?.label||'尚無足夠的同類歷史樣本')}</p><p>${esc(extra)}</p><p>${esc(ci)}</p><p>訊號期間（台灣時間）：${esc(period)}</p><p>逐根已收線資料重跑原價格核心，保留等待／重新確認；收線後延遲5分鐘，以5m開盤作模擬參考，固定原SL／TP1，觀察24小時。</p><p>歷史工作固定取樣8支大型主要代幣。精準情境樣本不足10筆時，才依序合併訊號階段、再合併R區間；方向、Trigger類型與4H關係始終保持一致。</p><p>10～19筆標示低樣本參考、20～49筆標示中等樣本；50筆以上且涵蓋完整取樣日門檻才標示樣本充足。所有顯示仍要求至少80%結果可判定。</p><p>不重播歷史OI／CVD、實際價差、滑價或全市場前20名排序。本輪取樣比例與95%區間不代表已驗證可預測未來。</p></details>${link}`;
  }

  function card(item,preview=false) {
    if(preview) return '';
    const key=keyFor(item),horizon=item?.radar_horizon||'SHORT',inst=String(item?.inst_id||'');
    return `<section class="history-replay-card" aria-label="歷史K棒回測" data-replay-key="${esc(encodeURIComponent(key||''))}" data-replay-horizon="${esc(horizon)}" data-replay-inst="${esc(inst)}">${textFor(key,horizon,inst)}</section>`;
  }

  function refreshCards() {
    for(const panel of document.querySelectorAll('.history-replay-card')) {
      let key='';try{key=decodeURIComponent(panel.dataset.replayKey||'')}catch(_){}
      const html=textFor(key,panel.dataset.replayHorizon,panel.dataset.replayInst);
      if(panel._historyMarkup!==html){panel._historyMarkup=html;panel.innerHTML=html;}
    }
  }

  async function refresh() {
    if(fetching) return;
    fetching=true;
    try{
      const response=await fetch('/api/history-scan/status',{cache:'no-store'});
      if(!response.ok) throw new Error('歷史狀態無法取得');
      data=await response.json();groups=new Map();symbolGroups=new Map();
      if(data.schema_version==='HISTORY_PRICE_REPLAY_V3'&&data.compatible===true){
        for(const [key,value]of Object.entries(data.groups||{})){try{groups.set(JSON.stringify(JSON.parse(key)),value)}catch(_){}}
        for(const [inst,bucket]of Object.entries(data.symbol_groups||{})){const mapped=new Map();for(const [key,value]of Object.entries(bucket||{})){try{mapped.set(JSON.stringify(JSON.parse(key)),value)}catch(_){}}symbolGroups.set(inst,mapped);}
      }
      refreshCards();window.dispatchEvent(new CustomEvent('history-replay-status',{detail:data}));
    }catch(error){window.dispatchEvent(new CustomEvent('history-replay-error',{detail:String(error.message||error)}));}
    finally{fetching=false;}
  }

  window.HistoryReplay={card,keyFor,refresh,getStatus:()=>data};
  document.addEventListener('DOMContentLoaded',()=>{
    let scheduled=false;
    new MutationObserver(()=>{if(!scheduled){scheduled=true;setTimeout(()=>{scheduled=false;refreshCards()},75)}}).observe(document.body,{childList:true,subtree:true});
    refresh();
    setInterval(()=>{if(document.visibilityState==='visible'&&(active.has(data?.status)||document.body.dataset.historyPage==='true'))refresh()},5000);
  });
})();
