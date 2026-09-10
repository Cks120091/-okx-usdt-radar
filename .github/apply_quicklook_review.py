"""Temporary isolated-branch patch builder; never deployed to main."""
from pathlib import Path
import hashlib
root=Path.cwd()
p=root/'radar/static/pages.html';s=p.read_text()
data=p.read_bytes()
assert hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()=='90ccc2226e79eabb96e2358e5259a9b2c687161b','Unexpected base: stop rather than overwrite'
css='''
    /* Read-only summary of existing decisions; never creates entry permission. */
    .quicklook-panel{margin:12px 0;padding:12px;border:1px solid var(--line);border-radius:12px;background:var(--panel);min-width:0}
    .quicklook-head{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px}
    .quicklook-head h3{font-size:13px;margin:0}.quicklook-head small{font-size:10px;color:var(--muted)}
    .quicklook-grid{margin:0;display:grid;gap:7px}.quicklook-row{display:grid;grid-template-columns:34px minmax(0,1fr);gap:8px;align-items:start}
    .quicklook-row dt{font-size:11px;color:var(--muted);line-height:1.6}.quicklook-row dd{margin:0;font-size:12px;line-height:1.6;overflow-wrap:anywhere;min-width:0}
    .quicklook-row:first-child dd{font-weight:700}.quicklook-ready{color:var(--green)}.quicklook-wait{color:var(--amber,#f0b86e)}.quicklook-muted{color:var(--muted)}
'''
assert s.count('  </style>')==1
s=s.replace('  </style>',css+'  </style>')
js='''    // Presentation only: reuse the existing final/eligibility checks unchanged.
    function quickLookData(item){
      const frames=isRecord(item?.timeframe_states)?item.timeframe_states:{},long=item?.radar_horizon==='LONG',core=long?'4H':'15m',background=long?'1D':'4H',direction=item?.direction;
      const terminal=terminalSignalOutcome(item),preview=isPreviewItem(item),readOnly=itemReadOnlyReason(item),expired=isExpiredSnapshot(item),entryStatus=itemEntryStatus(item);
      let action='先不要｜等待確認',tone='quicklook-wait';
      if(terminal){action='已結束｜原計畫不可沿用';tone='quicklook-muted'}
      else if(preview){action='先不要｜初步候選'}
      else if(expired||readOnly){action=expired?'先不要｜資料過期':readOnly==='SCANNING'?'先不要｜掃描中':readOnly==='ERROR'?'先不要｜更新失敗':'先不要｜上一輪資料';tone='quicklook-muted'}
      else if(itemCurrentEntryReady(item)){action='可進｜現有條件通過';tone='quicklook-ready'}
      else if(itemHardGateBlocked(item)||entryStatus==='HARD_GATE_BLOCKED'){action='先不要｜風險條件未通過'}
      else if(['MISSED_ENTRY','NO_CHASE'].includes(entryStatus)){action='禁止追價｜等待新確認'}
      else if(entryStatus==='WAIT_RETEST'){action='等回踩／收線確認'}
      const bg=frames[background]||{},bgDirection=bg.direction,known=d=>['LONG','SHORT'].includes(d),side=direction==='LONG'?'多':direction==='SHORT'?'空':'',type=String(item?.trigger_type||item?.market_story?.trigger?.type||'').toUpperCase();
      let setup='型態待確認';
      if(known(direction)){
        if(known(bgDirection)&&bgDirection!==direction){
          setup=!long&&bg.phase==='多頭背景中的短線回落'&&direction==='SHORT'?'多頭回踩內短空':!long&&bg.phase==='空頭背景中的短線反彈'&&direction==='LONG'?'空頭反彈內短多':`逆 ${background} ${long?'波段':'短'}${side}`;
        }else if(type==='CONTINUATION'||type==='REENTRY')setup=known(bgDirection)&&bgDirection===direction?`順勢續走做${side}`:`回踩再發動做${side}`;
        else if(type==='BREAKOUT')setup=`突破做${side}`;
        else if(type==='REVERSAL')setup=`反轉候選做${side}`;
        else setup=`${core} 做${side}｜型態待確認`;
      }
      const order=long?['4H','1H','1D']:['15m','1H','4H'];
      const basis=order.map(tf=>{
        const row=frames[tf];if(!isRecord(row))return `${tf} 資料不足`;
        if(tf===core)return `${tf} ${row.label||stageName[item?.signal_stage]||'觸發待確認'}`;
        if(tf===background&&row.phase==='多頭背景中的短線回落')return `${tf} 多頭背景回落`;
        if(tf===background&&row.phase==='空頭背景中的短線反彈')return `${tf} 空頭背景反彈`;
        return `${tf} ${row.label||(row.direction==='LONG'?'偏多':row.direction==='SHORT'?'偏空':row.direction==='NEUTRAL'?'中性':'資料不足')}`;
      }).join('｜');
      const votes=itemDecisionContext(item).continuation_confirmation?.core_votes||{},oi=String(votes.OI?.state||'UNKNOWN').toUpperCase(),flow=String(votes.TAKER_CVD?.state||'UNKNOWN').toUpperCase(),available=v=>['SUPPORT','NEUTRAL','CONFLICT'].includes(v);
      let funds='資料不足｜OI／主動成交待補';
      if(available(oi)&&available(flow))funds=[oi,flow].includes('CONFLICT')?'有反證｜與本卡方向不一致':oi==='SUPPORT'&&flow==='SUPPORT'?'支持｜OI／主動成交同向':'中性／部分支持｜尚未一致';
      else if(oi==='CONFLICT'||flow==='CONFLICT')funds='有反證｜另有資料不足';
      if(terminal)funds='歷史資料｜詳見完整數據';
      else if(preview||expired||readOnly)funds='待更新｜不沿用舊支持結論';
      return {action,tone,setup,basis,funds};
    }
    function quickLookPanel(item){
      const view=quickLookData(item);
      return `<section class="quicklook-panel" aria-label="快看重點"><div class="quicklook-head"><h3>快看重點</h3><small>摘要，不新增判定</small></div><dl class="quicklook-grid"><div class="quicklook-row"><dt>行動</dt><dd class="${view.tone}" data-quicklook="action">${esc(view.action)}</dd></div><div class="quicklook-row"><dt>型態</dt><dd data-quicklook="setup">${esc(view.setup)}</dd></div><div class="quicklook-row"><dt>依據</dt><dd data-quicklook="basis">${esc(view.basis)}</dd></div><div class="quicklook-row"><dt>資金</dt><dd data-quicklook="funds">${esc(view.funds)}（僅供輔助）</dd></div></dl></section>`;
    }
    function decisionPanel(item){return quickLookPanel(item)+decisionPanelBody(item)}
'''
assert s.count('    function decisionPanel(item){')==1
s=s.replace('    function decisionPanel(item){',js+'    function decisionPanelBody(item){')
needle='return [item.trigger_id,item.inst_id,item.signal_stage,'
assert s.count(needle)==1
s=s.replace(needle,'return [item.timeframe_states,item.trigger_type,item.trigger_id,item.inst_id,item.signal_stage,')
p.write_text(s)
p=root/'radar/static/service-worker.js';s=p.read_text();assert s.count('okx-radar-shell-v4.4-layout-2')==1;p.write_text(s.replace('okx-radar-shell-v4.4-layout-2','okx-radar-shell-v4.4-quicklook-1'))
p=root/'tests/test_v2_contract.py';s=p.read_text();assert s.count('okx-radar-shell-v4.4-layout-2')==3;p.write_text(s.replace('okx-radar-shell-v4.4-layout-2','okx-radar-shell-v4.4-quicklook-1'))
print('Applied quick-look presentation only; original backend and decisions unchanged.')
