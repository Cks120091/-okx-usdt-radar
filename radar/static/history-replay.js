/* Read-only historical model source. Never changes a signal or permission. */
(() => {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let data = null, groups = new Map(), fetching = false;
  const finite = value => value !== null && value !== undefined && typeof value !== 'boolean' && Number.isFinite(Number(value)) ? Number(value) : null;
  const terminal = new Set(['COMPLETE','PARTIAL_COMPLETE']);
  const active = new Set(['QUEUED','RUNNING','WAITING_LIVE_SCAN']);
  const titles = {IDLE:'尚未執行歷史掃描',QUEUED:'準備歷史掃描',RUNNING:'歷史掃描中',WAITING_LIVE_SCAN:'優先處理即時掃描，歷史工作暫候',PAUSED:'已暫停，可續跑',INTERRUPTED:'工作中斷，可續跑',COMPLETE:'歷史掃描完成',PARTIAL_COMPLETE:'本輪已結束，部分歷史不足',ERROR:'歷史工作失敗，請查看原因',VERSION_CHANGED:'策略或設定已變更，舊回測不混用'};
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
  function textFor(key,horizon,inst) {
    const link='<a class="history-replay-link" href="/history-scan">開啟全範圍歷史掃描 →</a>';
    if(horizon!=='SHORT') return '<h3>歷史 K 棒回測</h3><p>本版先支援 15m；不把短線結果套用到 4H。</p>'+link;
    const known=data?.schema_version==='HISTORY_PRICE_REPLAY_V1',group=key?groups.get(key):null;
    const n=finite(group?.resolved),wins=finite(group?.wins),losses=finite(group?.losses),count=finite(group?.total),days=finite(group?.days);
    const covered=Array.isArray(data?.covered_inst_ids)&&data.covered_inst_ids.includes(inst);
    const enough=known&&data.compatible===true&&terminal.has(data.status)&&covered&&group?.status==='AVAILABLE'
      &&[n,wins,losses,count,days].every(v=>v!==null&&Number.isInteger(v)&&v>=0)&&n===wins+losses&&count>=n
      &&n>=50&&days>=5&&n/count>=.8&&finite(data.scope_coverage_pct)>=80;
    const headline=enough?`${(100*wins/n).toFixed(1)}%`:!known?'尚未載入回測':!terminal.has(data.status)?(titles[data.status]||'尚未完成'):
      !covered?'此幣歷史尚不足':!key?'當前計畫無法配對':!group?'同類情境樣本不足':'同類樣本／覆蓋不足';
    let detail=enough?`已判定 ${n} 筆：TP1 先達 ${wins}｜SL 先達 ${losses}`:group?`同類已判定 ${n??0} 筆；至少50筆、5個取樣日及80%結果覆蓋才顯示`:'不套用五幣測試、全市場總勝率或交易品質分數。';
    if(known&&data.total) detail+=`；標的處理 ${data.done}/${data.total}，完整覆蓋 ${data.covered_symbols??0} 個。`;
    const ci=enough&&Array.isArray(group.interval_pct)&&group.interval_pct.length===2&&group.interval_pct.every(v=>finite(v)!==null)?`Wilson 95%描述區間 ${group.interval_pct[0]}%～${group.interval_pct[1]}%；未校正幣種相關性。`:'';
    const extra=group?`期限未達 ${group.timeout??0}｜結果不明 ${group.unknown??0}；跨幣同類樣本。`:'';
    const period=known&&data.start_ms&&data.end_ms?new Date(data.start_ms).toLocaleString('zh-TW',{timeZone:'Asia/Taipei'})+' ～ '+new Date(data.end_ms).toLocaleString('zh-TW',{timeZone:'Asia/Taipei'}):'—';
    return `<div class="history-replay-heading"><h3>歷史 K 棒回測｜15m</h3><strong data-replay-rate>${esc(headline)}</strong></div><p>${esc(detail)}</p><small>模擬 TP1 先達率，未扣費；非本單預測，不改進場資格。</small><details><summary>回測來源與限制</summary><p>${esc(group?.label||'尚無符合本卡條件的回測')}</p><p>${esc(extra)}</p><p>${esc(ci)}</p><p>訊號期間（台灣時間）：${esc(period)}</p><p>逐根已收線資料重跑原價格核心，保留等待／重新確認；收線後延遲5分鐘，以5m開盤作模擬參考，固定原SL／TP1，觀察24小時。</p><p>目前可交易合約集合，不含已下架幣；不重播歷史OI／CVD、實際價差、滑價或全市場前20名排序。百分比不是實盤獲利率。</p><p>本輪取樣比例與95%區間不代表已驗證可預測未來，也未校正幣種相關性。</p></details>${link}`;
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
      data=await response.json();groups=new Map();
      if(data.schema_version==='HISTORY_PRICE_REPLAY_V1'&&data.compatible===true)for(const [key,value]of Object.entries(data.groups||{})){try{groups.set(JSON.stringify(JSON.parse(key)),value)}catch(_){}}
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
