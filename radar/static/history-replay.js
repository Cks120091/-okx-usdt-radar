/* Single-coin 15m Trigger-time historical statistics. Read-only; never changes entry permission. */
(() => {
  'use strict';
  const VERSION = 'HISTORY_SINGLE_15M_V1';
  const active = new Set(['QUEUED', 'RUNNING', 'WAITING_LIVE_SCAN']);
  const terminal = new Set(['COMPLETE', 'PARTIAL_COMPLETE']);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const finite = value => value !== null && value !== undefined && typeof value !== 'boolean' && Number.isFinite(Number(value)) ? Number(value) : null;
  const periodLabel = days => ({3:'3天',7:'7天',14:'14天',30:'30天'})[Number(days || 7)] || `${Number(days || 7)}天`;
  let data = null;
  let fetching = false;
  let organizing = false;
  const scenarioByInst = new Map();

  function scenarioFor(item) {
    if (item?.radar_horizon !== 'SHORT' || !['LONG','SHORT'].includes(item?.direction)) return null;
    const metrics = item?.market_metrics || {};
    const entryLow = finite(item?.entry_low);
    const entryHigh = finite(item?.entry_high);
    const fallbackEntry = entryLow !== null && entryHigh !== null && entryLow > 0 && entryHigh > 0 ? (entryLow + entryHigh) / 2 : null;
    const entry = [
      finite(metrics?.entry_execution_price),
      finite(metrics?.trigger_price),
      finite(metrics?.plan_entry_price),
      fallbackEntry,
    ].find(v => v !== null && v > 0) ?? null;
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
    const kind = String(item.trigger_type || 'UNKNOWN');
    return {
      horizon:'SHORT',
      direction:item.direction,
      kind,
      relation,
      band,
      label:`${item.direction === 'LONG' ? '做多' : '做空'}｜${({BREAKOUT:'突破',CONTINUATION:'回踩續走',REVERSAL:'反轉',REENTRY:'再次觸發'})[kind] || '其他觸發'}｜${relation}｜${band}`,
    };
  }

  // Exact legacy cohort key retained for compatibility. UI similarity intentionally ignores signal stage.
  function keyFor(item) {
    const scenario = scenarioFor(item);
    if (!scenario) return null;
    return JSON.stringify([
      scenario.horizon,
      scenario.direction,
      scenario.kind,
      String(item.signal_stage || 'UNKNOWN'),
      scenario.relation,
      scenario.band,
    ]);
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

  function sampleTier(resolved) {
    if (resolved >= 50) return '樣本充足';
    if (resolved >= 20) return '中等樣本';
    if (resolved >= 10) return '低樣本參考';
    if (resolved > 0) return '極低樣本';
    return '尚無已判定樣本';
  }

  function similarScenario(coin, scenario) {
    if (!coin || !scenario || !coin.groups || typeof coin.groups !== 'object') return null;
    const bucket = {wins:0, losses:0, timeout:0, unknown:0, total:0};
    let matched = false;
    for (const [rawKey, group] of Object.entries(coin.groups)) {
      let key;
      try { key = JSON.parse(rawKey); } catch (_) { continue; }
      if (!Array.isArray(key) || key.length < 6) continue;
      const same = key[0] === scenario.horizon
        && key[1] === scenario.direction
        && key[2] === scenario.kind
        && key[4] === scenario.relation
        && key[5] === scenario.band;
      if (!same) continue;
      matched = true;
      const n = counts(group);
      bucket.wins += n.wins;
      bucket.losses += n.losses;
      bucket.timeout += n.timeout;
      bucket.unknown += n.unknown;
      bucket.total += n.total;
    }
    if (!matched) return null;
    const resolved = bucket.wins + bucket.losses;
    return {
      ...bucket,
      resolved,
      rate_pct: resolved ? 100 * bucket.wins / resolved : null,
      tier: sampleTier(resolved),
      label: scenario.label,
    };
  }

  function coinLink(instId) {
    return `<a class="history-replay-link" href="/history-scan?inst_id=${encodeURIComponent(instId)}">更新勝率 →</a>`;
  }

  function completedMarkup(instId, coin, heading = '本幣 15m Trigger 勝率', scenario = null) {
    const overall = coin.overall || {};
    const n = counts(overall);
    const rate = finite(overall.rate_pct);
    const baseHeadline = rate === null ? (n.total ? '尚無已判定結果' : '沒有 Trigger 樣本') : `${rate.toFixed(1)}%`;
    const tier = String(overall.tier || (n.resolved ? '短期樣本' : ''));
    const similar = similarScenario(coin, scenario);
    const similarRate = finite(similar?.rate_pct);
    const similarHeadline = similarRate === null ? '—' : `${similarRate.toFixed(1)}%`;
    const similarResolved = similar?.resolved || 0;
    const interval = Array.isArray(overall.interval_pct) && overall.interval_pct.length === 2
      ? `本幣基準 Wilson 95% 描述區間 ${overall.interval_pct[0]}%～${overall.interval_pct[1]}%。`
      : '';
    const period = coin.start_ms && coin.end_ms
      ? `${new Date(coin.start_ms).toLocaleString('zh-TW',{timeZone:'Asia/Taipei'})} ～ ${new Date(coin.end_ms).toLocaleString('zh-TW',{timeZone:'Asia/Taipei'})}`
      : '—';
    const unresolved = n.timeout + n.unknown;
    const primary = similarRate === null ? baseHeadline : similarHeadline;
    const scenarioLine = scenario
      ? `相近情境：${esc(scenario.label)}${similar ? `｜已判定 ${similarResolved} 筆｜${esc(similar.tier)}` : '｜目前沒有相符歷史樣本'}`
      : '相近情境：目前這張卡缺少可比對的完整 Trigger／R:R 資料。';
    const similarDetail = similar
      ? `<p>相近情境合併不同 signal stage，只比方向、Trigger 類型、高週期關係與 R:R 區間；TP1 / SL 為 ${similar.wins} / ${similar.losses}，Timeout / 不明為 ${similar.timeout} / ${similar.unknown}。</p>`
      : '<p>相近情境樣本不足時只保留本幣基準，不會用少量樣本硬做結論。</p>';
    return `<div class="history-replay-heading"><h3>${esc(heading)}</h3><strong data-replay-rate>${esc(primary)}</strong></div>
      <div class="history-replay-metrics">
        <div><span>本幣基準</span><b>${esc(baseHeadline)}</b></div>
        <div><span>相近情境</span><b>${esc(similarHeadline)}</b></div>
        <div><span>Trigger 樣本</span><b>${n.total}</b></div>
        <div><span>期間</span><b>${esc(periodLabel(coin.days || 7))}</b></div>
      </div>
      <p class="history-replay-status-line">${scenarioLine}</p>
      <p class="history-replay-status-line">本幣基準已判定 ${n.resolved} 筆｜TP1 / SL ${n.wins} / ${n.losses}${tier ? `｜${esc(tier)}` : ''}${unresolved ? `｜未判定 ${unresolved}` : ''}</p>
      <p class="history-replay-status-line"><b>歷史勝率只供參考，不影響 Trigger、進場資格、Entry、SL 或 TP。</b></p>
      <details><summary>統計說明</summary><div class="history-replay-details"><p>${esc(interval)}</p><p>訊號期間（台灣）：${esc(period)}</p>${similarDetail}<p>每個 Signal Episode 只取第一次正式 Trigger 成立的 15m 收線當樣本；後續回踩、可進場與再進只算確認／執行狀態，不再增加樣本。</p><p>Trigger 當下固定當時價格、SL、TP1；之後用已收線 5m 判斷 24 小時內 TP1 或 SL 誰先到。</p><p>未扣手續費、滑價與資金費；沒有完整歷史 OI／CVD、Bid／Ask、深度與訂單簿，因此不是實盤成交獲利率，也不是本單預測機率。</p></div></details>
      ${coinLink(instId)}`;
  }

  function markupFor(instId, coin, heading = '本幣 15m Trigger 勝率', scenario = null) {
    if (!data || data.schema_version !== VERSION) {
      return `<div class="history-replay-heading"><h3>${esc(heading)}</h3><strong>載入中</strong></div><p>讀取歷史資料中。</p>${coinLink(instId)}`;
    }
    if (!coin) {
      return `<div class="history-replay-heading"><h3>${esc(heading)}</h3><strong>尚未更新</strong></div><p>${esc(instId)} 尚無歷史勝率。</p><p class="history-replay-status-line"><b>勝率只供歷史參考，不參與進場判定。</b></p>${coinLink(instId)}`;
    }
    if (coin.compatible === false || coin.status === 'VERSION_CHANGED') {
      return `<div class="history-replay-heading"><h3>${esc(heading)}</h3><strong>需重新更新</strong></div><p>歷史樣本口徑或設定已變更。</p>${coinLink(instId)}`;
    }
    if (!terminal.has(coin.status)) {
      const label = coin.status === 'WAITING_LIVE_SCAN' ? '即時掃描優先，歷史暫候' : coin.status === 'PAUSED' ? '已暫停，可續跑' : coin.status === 'INTERRUPTED' ? '已中斷，可續跑' : coin.status === 'ERROR' ? '更新失敗' : '更新中';
      return `<div class="history-replay-heading"><h3>${esc(heading)}</h3><strong>${esc(label)}</strong></div><div class="history-replay-metrics compact"><div><span>期間</span><b>${esc(periodLabel(coin.days || 7))}</b></div><div><span>進度</span><b>${esc(coin.done || 0)} / ${esc(coin.total || 1)}</b></div></div><p class="history-replay-status-line"><b>勝率只供歷史參考，不參與進場判定。</b></p>${coinLink(instId)}`;
    }
    return completedMarkup(instId, coin, heading, scenario);
  }

  function organizeSignalCards() {
    document.querySelectorAll('.history-stats-panel').forEach(panel => {
      panel.hidden = true;
      panel.setAttribute('aria-hidden', 'true');
    });
    document.querySelectorAll('.signal-card').forEach(cardEl => {
      let grid = cardEl.querySelector(':scope > .decision-front-grid');
      const decision = cardEl.querySelector(':scope > .decision-panel');
      const directQuick = cardEl.querySelector(':scope > .quicklook-panel');
      const directHistory = cardEl.querySelector(':scope > .history-replay-card');
      const quick = directQuick || grid?.querySelector(':scope > .quicklook-panel');
      const history = directHistory || grid?.querySelector(':scope > .history-replay-card');
      if (!quick && !history) return;
      if (!grid) {
        grid = document.createElement('div');
        grid.className = 'decision-front-grid';
        if (decision) cardEl.insertBefore(grid, decision);
        else cardEl.insertBefore(grid, cardEl.firstChild?.nextSibling || null);
      }
      if (quick && quick.parentElement !== grid) grid.appendChild(quick);
      if (history && history.parentElement !== grid) grid.appendChild(history);
      grid.classList.toggle('single', !(quick && history));
    });
  }

  function blockWithHeading(root, text) {
    return [...root.querySelectorAll(':scope > .preflight-block')].find(block =>
      String(block.querySelector(':scope > h3')?.textContent || '').includes(text)
    ) || null;
  }

  function moveAfter(anchor, node) {
    if (!anchor || !node || anchor === node || anchor.nextElementSibling === node) return;
    anchor.after(node);
  }

  function organizePreflightPage() {
    const root = document.querySelector('#preflightContent');
    if (!root || organizing || !root.querySelector(':scope > .preflight-verdict')) return;
    organizing = true;
    try {
      root.classList.add('compact-preflight-layout');
      const verdict = root.querySelector(':scope > .preflight-verdict');
      const position = blockWithHeading(root, '現在位置與進場資格');
      const plan = root.querySelector(':scope > .preflight-plan-block');
      const history = root.querySelector(':scope > #preflightHistoryRate');
      if (position) position.classList.add('preflight-priority-position');
      if (plan) plan.classList.add('preflight-priority-plan');

      let anchor = verdict;
      for (const node of [position, plan, history]) {
        if (!node) continue;
        moveAfter(anchor, node);
        anchor = node;
      }

      let details = root.querySelector(':scope > .preflight-secondary-stack');
      if (!details) {
        details = document.createElement('details');
        details.className = 'preflight-secondary-stack';
        details.innerHTML = '<summary><span>更多確認與資料細節</span><small>成交品質・OI/CVD・續走・資料來源</small></summary><div class="preflight-secondary-stack-body"></div>';
      }
      const body = details.querySelector('.preflight-secondary-stack-body');
      const keep = new Set([verdict, position, plan, history, details].filter(Boolean));
      const extras = [...root.children].filter(child => !keep.has(child));
      extras.forEach(child => body.appendChild(child));
      if (body.children.length) {
        moveAfter(anchor, details);
      } else if (details.parentElement === root) {
        details.remove();
      }
    } finally {
      organizing = false;
    }
  }

  function organizeSingleScanDialog() {
    const root = document.querySelector('#singleScanContent');
    if (!root || organizing || !root.children.length) return;
    organizing = true;
    try {
      root.classList.add('compact-single-scan');
      const decision = root.querySelector(':scope > .decision-panel');
      if (!decision) return;
      decision.classList.add('single-scan-core');
      root.querySelectorAll('.single-scan-button').forEach(button => {
        button.hidden = true;
        button.setAttribute('aria-hidden', 'true');
      });

      const note = root.querySelector(':scope > .intraday-note');
      const badges = root.querySelector(':scope > .badges');
      const warning = root.querySelector(':scope > .entry-callout.wait');
      let anchor = note || badges || decision;
      if (note && badges) moveAfter(note, badges);
      if (badges) anchor = badges;
      moveAfter(anchor, decision);
      anchor = decision;
      if (warning) {
        moveAfter(anchor, warning);
        anchor = warning;
      }

      let details = root.querySelector(':scope > .single-scan-details');
      if (!details) {
        details = document.createElement('details');
        details.className = 'single-scan-details';
        details.innerHTML = '<summary><span>更多市場與持倉資料</span><small>資金流・OI/CVD・完整數據</small></summary><div class="single-scan-details-body"></div>';
      }
      const body = details.querySelector('.single-scan-details-body');
      const keep = new Set([note, badges, decision, warning, details].filter(Boolean));
      const extras = [...root.children].filter(child => !keep.has(child));
      extras.forEach(child => body.appendChild(child));
      if (body.children.length) moveAfter(anchor, details);
      else if (details.parentElement === root) details.remove();
    } finally {
      organizing = false;
    }
  }

  function installLayoutObservers() {
    const observe = (selector, organizer) => {
      const root = document.querySelector(selector);
      if (!root || typeof MutationObserver !== 'function') return;
      const observer = new MutationObserver(() => queueMicrotask(organizer));
      observer.observe(root, {childList:true, subtree:false});
      queueMicrotask(organizer);
    };
    observe('#preflightContent', organizePreflightPage);
    observe('#singleScanContent', organizeSingleScanDialog);
  }

  function card(item, preview = false) {
    queueMicrotask(organizeSignalCards);
    if (preview || item?.radar_horizon !== 'SHORT') return '';
    const instId = String(item?.inst_id || '').toUpperCase();
    if (!instId) return '';
    const scenario = scenarioFor(item);
    if (scenario) scenarioByInst.set(instId, scenario);
    else scenarioByInst.delete(instId);
    return `<section class="history-replay-card" aria-label="本幣15m Trigger歷史參考" data-replay-inst="${esc(instId)}">${markupFor(instId, snapshot(instId), '本幣 15m Trigger 勝率', scenario)}</section>`;
  }

  function preflight(instId) {
    instId = String(instId || '').toUpperCase();
    if (!instId) return '';
    const scenario = scenarioByInst.get(instId) || null;
    return `<section class="history-replay-card preflight-history-rate" aria-label="本幣15m Trigger歷史參考" data-replay-inst="${esc(instId)}">${markupFor(instId, snapshot(instId), '本幣 15m Trigger 勝率', scenario)}</section>`;
  }

  function refreshCards() {
    for (const panel of document.querySelectorAll('.history-replay-card')) {
      const instId = String(panel.dataset.replayInst || '').toUpperCase();
      if (!instId) continue;
      const scenario = scenarioByInst.get(instId) || null;
      const html = markupFor(instId, snapshot(instId), '本幣 15m Trigger 勝率', scenario);
      if (panel._historyMarkup !== html) {
        panel._historyMarkup = html;
        panel.innerHTML = html;
      }
    }
    organizeSignalCards();
    organizePreflightPage();
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

  window.HistoryReplay = {card, preflight, refresh, refreshCards, keyFor, scenarioFor, similarScenario, snapshot, organizeSignalCards, organizePreflightPage, organizeSingleScanDialog};
  const boot = () => {
    installLayoutObservers();
    refresh();
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, {once:true});
  else boot();
  setInterval(() => {
    const onHistoryPage = document.body?.dataset?.historyPage === 'true';
    const running = data && active.has(data.status);
    if (!onHistoryPage && document.visibilityState !== 'visible') return;
    if (onHistoryPage || running) refresh();
  }, 5000);
})();
