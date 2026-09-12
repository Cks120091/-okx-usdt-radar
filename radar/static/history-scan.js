(() => {
  'use strict';
  let latest = null;
  let busy = false;
  const $ = id => document.getElementById(id);
  const active = new Set(['QUEUED','RUNNING','WAITING_LIVE_SCAN']);
  const labels = {
    IDLE:'尚未建立本幣歷史資料', QUEUED:'準備更新', RUNNING:'15m Trigger 歷史更新中',
    WAITING_LIVE_SCAN:'即時掃描優先，歷史暫候', PAUSED:'歷史已暫停', INTERRUPTED:'歷史中斷，可續跑',
    ERROR:'歷史更新失敗', COMPLETE:'15m Trigger 歷史更新完成', PARTIAL_COMPLETE:'更新完成，但部分窗口不足',
    VERSION_CHANGED:'樣本口徑／設定已變更，請重新更新'
  };

  function normalize(raw) {
    let value = String(raw || '').trim().toUpperCase();
    if (!value) return '';
    value = value.replace(/\s+/g, '');
    if (!value.includes('-')) value += '-USDT-SWAP';
    else if (/^[A-Z0-9]+-USDT$/.test(value)) value += '-SWAP';
    return /^[A-Z0-9][A-Z0-9-]{0,48}-USDT-SWAP$/.test(value) ? value : '';
  }

  function selectedInst() {
    return normalize($('inst').value);
  }

  function selectedCoin() {
    const inst = selectedInst();
    return inst && latest?.coins ? latest.coins[inst] || null : null;
  }

  function fmtTime(value) {
    return value ? new Date(value).toLocaleString('zh-TW', {timeZone:'Asia/Taipei'}) : '—';
  }

  function periodLabel(days) {
    const n = Number(days || 7);
    return ({3:'3日',7:'7日',14:'14日',30:'30日'})[n] || `${n}日`;
  }

  function render(data) {
    latest = data || latest || {};
    const inst = selectedInst();
    const coin = selectedCoin();
    const globalBusy = active.has(latest?.status);
    const activeInst = globalBusy ? String(latest?.inst_id || '') : '';
    const sameActive = !!inst && activeInst === inst;
    const valid = !!inst;
    const state = coin?.status || (sameActive ? latest.status : 'IDLE');
    $('selected').textContent = valid ? inst : '請輸入幣種，例如 BTC';
    $('status').textContent = labels[state] || '讀取中';

    const done = coin?.done ?? (sameActive ? latest?.done : 0) ?? 0;
    const total = Math.max(coin?.total ?? (sameActive ? latest?.total : 1) ?? 1, 1);
    $('progress').max = total;
    $('progress').value = Math.min(done, total);

    const overall = coin?.overall || {};
    const triggerSamples = Number(overall.total || coin?.trigger_signals || 0);
    const resolved = Number(overall.resolved || 0);
    const wins = Number(overall.wins || 0);
    const losses = Number(overall.losses || 0);
    const timeout = Number(overall.timeout || 0);
    const unknown = Number(overall.unknown || 0);
    const rate = overall.rate_pct;
    if (coin && ['COMPLETE','PARTIAL_COMPLETE'].includes(coin.status)) {
      $('counts').textContent = `${periodLabel(coin.days)}｜TP1先達率 ${rate === null || rate === undefined ? '—' : Number(rate).toFixed(1) + '%'}｜Trigger樣本 ${triggerSamples}｜已判定 ${resolved}｜TP1 ${wins}／SL ${losses}｜未判定 ${timeout + unknown}｜${overall.tier || '樣本統計中'}`;
    } else if (sameActive) {
      $('counts').textContent = `${periodLabel(latest.days || Number($('days').value))}｜處理區段 ${done}/${total}`;
    } else if (coin) {
      $('counts').textContent = labels[coin.status] || coin.status;
    } else {
      $('counts').textContent = valid ? '尚未做過這顆幣的 15m Trigger 歷史更新。' : '輸入幣種後即可更新。';
    }

    $('current').textContent = globalBusy && activeInst ? `目前處理：${activeInst}` : '';
    $('period').textContent = coin?.start_ms ? `期間：${fmtTime(coin.start_ms)} ～ ${fmtTime(coin.end_ms)}` : '';
    $('storage').textContent = `歷史資料庫約 ${((latest?.storage_bytes || 0) / 1048576).toFixed(2)} MB／32 MB`;
    $('error').textContent = sameActive ? (latest?.error || '') : (coin?.error || (globalBusy && activeInst && activeInst !== inst ? `${activeInst} 正在更新；一次只跑一顆幣。` : ''));

    $('excluded').replaceChildren();
    const excluded = Array.isArray(coin?.excluded) ? coin.excluded : [];
    if (!excluded.length) $('excluded').textContent = coin ? '沒有已列出的歷史缺漏。' : '尚無本幣歷史資料。';
    for (const row of excluded) {
      const p = document.createElement('p');
      p.textContent = `${row.reason || row.status}（已核對 15m 窗口 ${row.evaluated || 0}）`;
      $('excluded').append(p);
    }

    $('start').disabled = busy || !valid || globalBusy || ['PAUSED','INTERRUPTED','ERROR'].includes(coin?.status);
    $('pause').disabled = busy || !globalBusy;
    $('resume').disabled = busy || !valid || globalBusy || !['PAUSED','INTERRUPTED','ERROR'].includes(coin?.status);
    $('delete').disabled = busy || !valid || globalBusy || !coin;
    $('deleteAll').disabled = busy || globalBusy || !Object.keys(latest?.coins || {}).length;
    $('days').disabled = busy || globalBusy;
    $('inst').disabled = busy || globalBusy;
  }

  async function command(action) {
    if (busy || !latest) return;
    const clearAll = action === 'delete_all';
    const inst = selectedInst();
    if (!clearAll && !inst) {
      $('error').textContent = '請輸入正確幣種，例如 BTC 或 BTC-USDT-SWAP。';
      return;
    }
    if (action === 'delete' && !confirm(`確定清除 ${inst} 的15m歷史資料？正式訊號與即時掃描觀測不會刪除。`)) return;
    if (clearAll) {
      const count = Object.keys(latest?.coins || {}).length;
      if (!count) return;
      if (!confirm(`確定清除目前儲存的 ${count} 顆幣全部歷史資料？`)) return;
      if (!confirm('最後確認：清除後各幣都要重新手動更新。仍要全部清除嗎？')) return;
    }
    busy = true;
    render(latest);
    let failure = '';
    try {
      const response = await fetch('/api/history-scan/' + action, {
        method:'POST',
        headers:{'Content-Type':'application/json','X-History-Intent':'user'},
        body:JSON.stringify(clearAll ? {csrf:latest.csrf} : {
          csrf:latest.csrf,
          days:{days:Number($('days').value), inst_id:inst}
        })
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || '操作失敗');
      latest = result;
    } catch (error) {
      failure = String(error.message || error);
    } finally {
      busy = false;
      render(latest);
      if (failure) $('error').textContent = failure;
    }
    if (!failure) HistoryReplay.refresh();
  }

  const queryInst = new URLSearchParams(location.search).get('inst_id');
  if (queryInst) $('inst').value = normalize(queryInst) || String(queryInst).toUpperCase();
  $('inst').addEventListener('input', () => render(latest));
  for (const action of ['start','pause','resume','delete']) $(action).addEventListener('click', () => command(action));
  $('deleteAll').addEventListener('click', () => command('delete_all'));
  window.addEventListener('history-replay-status', event => render(event.detail));
  window.addEventListener('history-replay-error', event => {$('error').textContent = event.detail;});
  render({schema_version:'HISTORY_SINGLE_15M_V1',status:'IDLE',coins:{},csrf:''});
})();
