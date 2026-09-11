/* Single-coin 15m historical statistics. Read-only; never changes entry permission. */
(() => {
  'use strict';
  const VERSION = 'HISTORY_SINGLE_15M_V1';
  const active = new Set(['QUEUED', 'RUNNING', 'WAITING_LIVE_SCAN']);
  const terminal = new Set(['COMPLETE', 'PARTIAL_COMPLETE']);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const finite = value => value !== null && value !== undefined && typeof value !== 'boolean' && Number.isFinite(Number(value)) ? Number(value) : null;
  let data = null;
  let fetching = false;

  function keyFor(item) {
    if (item?.radar_horizon !== 'SHORT' || !['LONG','SHORT'].includes(item?.direction)) return null;
    const entry = finite(item?.market_metrics?.entry_execution_price);
    const stop = finite(item?.stop_loss);
    const target = finite(item?.take_profit_1);
    if ([entry, stop, target].some(v => v === null || v <= 0)) return null;
    const risk = item.direction === 'LONG' ? entry - stop : stop - entry;
    const reward = item.direction === 'LONG' ? target - entry : entry - target;
    if (risk <= 0 || reward <= 0) return null;
    const rr = reward / risk;
    const band = rr < 2 ? '<2R' : rr < 3 ? '2–<3R' : rr < 4 ? '3–<4R' : rr < 6 ? '4–<6R' : '≥6R';
    const bg = item?.timeframe_states?.['4H']?.direction;
    const relation = bg === item.direction ? '同向背景' : ['LONG','SHORT'].includes(bg) ? '逆高週期背景' : bg === 'NEUTRAL' ? '中性背景' : '背景未知';
    return JSON.stringify(['SHORT', item.direction, String(item.trigger_type || 'UNKNOWN'), String(item.signal_stage || 'UNKNOWN'), relation, band]);
  }

  function snapshot(instId) {
    if (!data || data.schema_version !== VERSION) return null;
    return data.coins?.[String(instId || '')] || null;
  }

  function counts(group) {
    const wins = Math.max(0, finite(group?.wins) ?? 0);
    const losses = Math.max(0, finite(group?.losses) ?? 0);
    const total = Math.max(0, finite(group?.total) ?? 0);
    const timeout = Math.max(0, finite(group?.timeout) ?? 0);
    const unknown = Math.max(0, finite(group?.unknown) ?? 0);
    const resolved = Math.max(0, finite(group?.resolved) ?? wins + losses);
    return {wins, losses, total, timeout, unknown, resolved};
  }

  function coinLink(instId) {
    return `<a class="history-replay-link" href="/history-scan?inst_id=${encodeURIComponent(instId)}">更新本幣 15m 歷史勝率 →</a>`;
  }

  function textFor(item) {
    const instId = String(item?.inst_id || '').toUpperCase();
    const link = coinLink(instId);
    const coin = snapshot(instId);
    const key = keyFor(item);
    if (!data || data.schema_version !== VERSION) {
      return `<div class="history-replay-heading"><h3>本幣 15m 歷史勝率</h3><strong>尚未載入</strong></div><p>新版只使用這顆幣自己的 3／7 日 15m 可進場訊號。</p>${link}`;
    }
    if (!coin) {
      return `<div class="history-replay-heading"><h3>本幣 15m 歷史勝率</h3><strong>尚未更新</strong></div><p>${esc(instId)} 還沒有單幣歷史資料；不再套用其他幣的勝率。</p>${link}`;
    }
    if (coin.compatible === false || coin.status === 'VERSION_CHANGED') {
      return `<div class="history-replay-heading"><h3>本幣 15m 歷史勝率</h3><strong>版本已變更</strong></div><p>舊的單幣回放不混用，請重新更新 ${esc(instId)}。</p>${link}`;
    }
    if (!terminal.has(coin.status)) {
      const label = coin.status === 'WAITING_LIVE_SCAN' ? '即時掃描優先，歷史暫候' : coin.status === 'PAUSED' ? '歷史更新已暫停' : coin.status === 'INTERRUPTED' ? '歷史更新中斷，可續跑' : coin.status === 'ERROR' ? '歷史更新失敗' : '歷史更新中';
      return `<div class="history-replay-heading"><h3>本幣 15m 歷史勝率</h3><strong>${esc(label)}</strong></div><p>${esc(instId)}｜最近 ${esc(coin.days || 7)} 天｜處理 ${esc(coin.done || 0)}/${esc(coin.total || 1)}</p>${link}`;
    }

    const overall = coin.overall || {};
    const overallCounts = counts(overall);
    const rate = finite(overall.rate_pct);
    const headline = rate === null ? (overallCounts.total ? '尚無已判定結果' : '沒有可進訊號') : `${rate.toFixed(1)}%`;
    const tier = String(overall.tier || (overallCounts.resolved ? '短期樣本' : ''));
    const coverage = finite(overall.coverage_pct);
    let detail = `${instId}｜近 ${coin.days || 7} 日 15m｜可進訊號 ${overallCounts.total} 筆｜已判定 ${overallCounts.resolved} 筆`;
    if (overallCounts.resolved) detail += `：TP1 ${overallCounts.wins}｜SL ${overallCounts.losses}`;
    if (overallCounts.timeout || overallCounts.unknown) detail += `｜Timeout ${overallCounts.timeout}｜不明 ${overallCounts.unknown}`;
    if (tier) detail += `｜${tier}`;
    if (coverage !== null) detail += `｜可判定率 ${coverage.toFixed(1)}%`;

    let cohortHtml = '';
    const cohort = key ? coin.groups?.[key] : null;
    if (cohort) {
      const c = counts(cohort);
      const cohortRate = finite(cohort.rate_pct);
      cohortHtml = `<p><b>目前同類情境：</b>${cohortRate === null ? '尚無已判定結果' : cohortRate.toFixed(1) + '%'}｜可進 ${c.total} 筆｜已判定 ${c.resolved} 筆${c.resolved ? `｜TP1 ${c.wins}／SL ${c.losses}` : ''}</p>`;
    } else if (key && overallCounts.total) {
      cohortHtml = '<p><b>目前同類情境：</b>這次本幣回放沒有相同情境樣本。</p>';
    }

    const interval = Array.isArray(overall.interval_pct) && overall.interval_pct.length === 2 ? `Wilson 95% 描述區間 ${overall.interval_pct[0]}%～${overall.interval_pct[1]}%。` : '';
    const period = coin.start_ms && coin.end_ms ? `${new Date(coin.start_ms).toLocaleString('zh-TW',{timeZone:'Asia/Taipei'})} ～ ${new Date(coin.end_ms).toLocaleString('zh-TW',{timeZone:'Asia/Taipei'})}` : '—';
    return `<div class="history-replay-heading"><h3>本幣 15m 歷史勝率</h3><strong data-replay-rate>${esc(headline)}</strong></div><p>${esc(detail)}</p>${cohortHtml}<small>只統計本幣每個 Episode 第一次達到可進場的 15m 收線；非本單預測，不改進場資格。</small><details><summary>歷史來源與限制</summary><p>${esc(interval)}</p><p>訊號期間（台灣時間）：${esc(period)}</p><p>逐個15m收線點重跑價格核心；同一Episode只算第一次可進。固定當時SL／TP1，再用後續已收線5m判定24小時內TP1或SL誰先到。</p><p>沒有完整歷史OI／CVD、實際Bid／Ask、深度與訂單簿；未扣手續費、滑價與資金費，因此不是實盤成交獲利率。</p></details>${link}`;
  }

  function card(item, preview = false) {
    if (preview || item?.radar_horizon !== 'SHORT') return '';
    const instId = String(item?.inst_id || '').toUpperCase();
    if (!instId) return '';
    return `<section class="history-replay-card" aria-label="本幣15m歷史勝率" data-replay-inst="${esc(instId)}" data-replay-key="${esc(encodeURIComponent(keyFor(item) || ''))}">${textFor(item)}</section>`;
  }

  function preflight(instId) {
    instId = String(instId || '').toUpperCase();
    if (!instId) return '';
    const link = `<a class="history-replay-link" href="/history-scan?inst_id=${encodeURIComponent(instId)}">歷史 K 棒勝率 →</a>`;
    const coin = snapshot(instId);
    if (!data || data.schema_version !== VERSION) {
      return `<section class="history-replay-card preflight-history-rate" aria-label="15m進場前更新歷史K棒勝率"><div class="history-replay-heading"><h3>歷史 K 棒勝率｜本幣 15m</h3><strong>載入中</strong></div><p>只讀既有單幣 3／7 日歷史結果；本次進場前更新不會重跑歷史 K 棒。</p>${link}</section>`;
    }
    if (!coin) {
      return `<section class="history-replay-card preflight-history-rate" aria-label="15m進場前更新歷史K棒勝率"><div class="history-replay-heading"><h3>歷史 K 棒勝率｜本幣 15m</h3><strong>尚未更新</strong></div><p>${esc(instId)} 尚無已完成的單幣歷史結果；進場前更新不會自動啟動歷史掃描。</p>${link}</section>`;
    }
    if (coin.compatible === false || coin.status === 'VERSION_CHANGED') {
      return `<section class="history-replay-card preflight-history-rate" aria-label="15m進場前更新歷史K棒勝率"><div class="history-replay-heading"><h3>歷史 K 棒勝率｜本幣 15m</h3><strong>版本已變更</strong></div><p>舊回放不混用；如需新勝率請手動更新歷史 K 棒。</p>${link}</section>`;
    }
    if (!terminal.has(coin.status)) {
      const label = coin.status === 'PAUSED' ? '歷史更新已暫停' : coin.status === 'INTERRUPTED' ? '歷史更新中斷' : coin.status === 'ERROR' ? '歷史更新失敗' : '歷史更新中';
      return `<section class="history-replay-card preflight-history-rate" aria-label="15m進場前更新歷史K棒勝率"><div class="history-replay-heading"><h3>歷史 K 棒勝率｜本幣 15m</h3><strong>${esc(label)}</strong></div><p>${esc(instId)}｜沿用目前已存歷史資料；進場前更新不會另開歷史回放。</p>${link}</section>`;
    }
    const overall = coin.overall || {};
    const n = counts(overall);
    const rate = finite(overall.rate_pct);
    const headline = rate === null ? (n.total ? '尚無已判定結果' : '沒有可進訊號') : `${rate.toFixed(1)}%`;
    let detail = `${instId}｜近 ${coin.days || 7} 日 15m｜可進訊號 ${n.total} 筆｜已判定 ${n.resolved} 筆`;
    if (n.resolved) detail += `：TP1 ${n.wins}｜SL ${n.losses}`;
    if (overall.tier) detail += `｜${overall.tier}`;
    return `<section class="history-replay-card preflight-history-rate" aria-label="15m進場前更新歷史K棒勝率"><div class="history-replay-heading"><h3>歷史 K 棒勝率｜本幣 15m</h3><strong data-preflight-history-rate>${esc(headline)}</strong></div><p>${esc(detail)}</p><small>沿用最近一次已完成的單幣歷史回放；按進場前更新只更新現在行情，不重跑歷史 K 棒。</small>${link}</section>`;
  }

  function refreshCards() {
    for (const panel of document.querySelectorAll('.history-replay-card')) {
      let key = '';
      try { key = decodeURIComponent(panel.dataset.replayKey || ''); } catch (_) {}
      const synthetic = {
        inst_id: panel.dataset.replayInst || '',
        radar_horizon: 'SHORT',
      };
      // Existing cards already carry a cohort key. Re-render directly from it
      // instead of reconstructing trade fields that are not stored in dataset.
      const instId = String(synthetic.inst_id || '').toUpperCase();
      const coin = snapshot(instId);
      const link = coinLink(instId);
      let html;
      if (!data || data.schema_version !== VERSION) {
        html = `<div class="history-replay-heading"><h3>本幣 15m 歷史勝率</h3><strong>尚未載入</strong></div><p>新版只使用這顆幣自己的 3／7 日 15m 可進場訊號。</p>${link}`;
      } else if (!coin) {
        html = `<div class="history-replay-heading"><h3>本幣 15m 歷史勝率</h3><strong>尚未更新</strong></div><p>${esc(instId)} 還沒有單幣歷史資料；不再套用其他幣的勝率。</p>${link}`;
      } else {
        const overall = coin.overall || {};
        const n = counts(overall);
        const rate = finite(overall.rate_pct);
        if (coin.compatible === false || coin.status === 'VERSION_CHANGED') {
          html = `<div class="history-replay-heading"><h3>本幣 15m 歷史勝率</h3><strong>版本已變更</strong></div><p>請重新更新 ${esc(instId)}。</p>${link}`;
        } else if (!terminal.has(coin.status)) {
          const label = coin.status === 'PAUSED' ? '歷史更新已暫停' : coin.status === 'INTERRUPTED' ? '歷史更新中斷，可續跑' : coin.status === 'ERROR' ? '歷史更新失敗' : coin.status === 'WAITING_LIVE_SCAN' ? '即時掃描優先，歷史暫候' : '歷史更新中';
          html = `<div class="history-replay-heading"><h3>本幣 15m 歷史勝率</h3><strong>${esc(label)}</strong></div><p>${esc(instId)}｜最近 ${esc(coin.days || 7)} 天</p>${link}`;
        } else {
          const headline = rate === null ? (n.total ? '尚無已判定結果' : '沒有可進訊號') : `${rate.toFixed(1)}%`;
          let detail = `${instId}｜近 ${coin.days || 7} 日 15m｜可進訊號 ${n.total} 筆｜已判定 ${n.resolved} 筆`;
          if (n.resolved) detail += `：TP1 ${n.wins}｜SL ${n.losses}`;
          if (overall.tier) detail += `｜${overall.tier}`;
          const cohort = key ? coin.groups?.[key] : null;
          let same = '';
          if (cohort) {
            const c = counts(cohort), cr = finite(cohort.rate_pct);
            same = `<p><b>目前同類情境：</b>${cr === null ? '尚無已判定結果' : cr.toFixed(1) + '%'}｜可進 ${c.total} 筆｜已判定 ${c.resolved} 筆</p>`;
          }
          html = `<div class="history-replay-heading"><h3>本幣 15m 歷史勝率</h3><strong data-replay-rate>${esc(headline)}</strong></div><p>${esc(detail)}</p>${same}<small>只用本幣15m可進場Episode；非本單預測，不改進場資格。</small>${link}`;
        }
      }
      if (panel._historyMarkup !== html) {
        panel._historyMarkup = html;
        panel.innerHTML = html;
      }
    }
  }

  async function refresh() {
    if (fetching) return data;
    fetching = true;
    try {
      const response = await fetch('/api/history-scan/status', {cache:'no-store'});
      if (!response.ok) throw new Error('單幣歷史狀態無法取得');
      data = await response.json();
      refreshCards();
      window.dispatchEvent(new CustomEvent('history-replay-status', {detail:data}));
      return data;
    } catch (error) {
      window.dispatchEvent(new CustomEvent('history-replay-error', {detail:String(error.message || error)}));
      return data;
    } finally {
      fetching = false;
    }
  }

  window.HistoryReplay = {card, preflight, refresh, refreshCards, keyFor, snapshot};
  const boot = () => refresh();
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, {once:true});
  else boot();
  setInterval(() => {
    if (document.visibilityState !== 'visible') return;
    const onHistoryPage = document.body?.dataset?.historyPage === 'true';
    const running = data && active.has(data.status);
    if (onHistoryPage || running) refresh();
  }, 5000);
})();
