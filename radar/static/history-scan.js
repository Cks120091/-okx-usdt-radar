(() => {
  'use strict';
  let latest = null;
  let busy = false;
  let pushConfig = null;
  let pushSubscription = null;
  let pushLoading = false;
  const HISTORY_PUSH_KEY = 'okx-radar-history-push-enabled';
  const VAPID_KEY_ID = 'okx-radar-vapid-key-id';
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

  function historyPushEnabled() {
    try { return localStorage.getItem(HISTORY_PUSH_KEY) === '1'; }
    catch (_) { return false; }
  }

  function setHistoryPushEnabled(enabled) {
    try {
      if (enabled) localStorage.setItem(HISTORY_PUSH_KEY, '1');
      else localStorage.removeItem(HISTORY_PUSH_KEY);
    } catch (_) {}
  }

  function supportsPush() {
    return window.isSecureContext && 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  }

  function isIOSDevice() {
    return /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  }

  function isStandaloneApp() {
    return window.matchMedia?.('(display-mode: standalone)').matches || navigator.standalone === true;
  }

  function base64UrlBytes(value) {
    const padding = '='.repeat((4 - value.length % 4) % 4);
    const raw = atob((value + padding).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(raw, ch => ch.charCodeAt(0));
  }

  function storedVapidKeyId() {
    try { return localStorage.getItem(VAPID_KEY_ID) || ''; }
    catch (_) { return ''; }
  }

  function setStoredVapidKeyId(value) {
    try {
      if (value) localStorage.setItem(VAPID_KEY_ID, value);
      else localStorage.removeItem(VAPID_KEY_ID);
    } catch (_) {}
  }

  function installNotifyControl() {
    if ($('historyNotifyButton')) return;
    const actions = document.querySelector('.history-secondary-actions');
    if (!actions) return;
    const row = document.createElement('div');
    row.className = 'history-notify-row';
    row.innerHTML = '<div class="history-notify-copy"><b>勝率更新完成通知</b><span id="historyNotifyText">可選擇在這次工作完成後收到瀏覽器通知。</span></div><button id="historyNotifyButton" class="history-notify-button" type="button" aria-pressed="false">完成通知：關</button>';
    actions.after(row);
    $('historyNotifyButton').addEventListener('click', toggleHistoryNotifications);
  }

  function renderNotifyState(message = '') {
    const button = $('historyNotifyButton');
    const copy = $('historyNotifyText');
    if (!button || !copy) return;
    const enabled = historyPushEnabled();
    const running = active.has(latest?.status);
    const available = Boolean(pushConfig?.available && supportsPush());
    button.classList.toggle('on', enabled && available);
    button.setAttribute('aria-pressed', String(enabled && available));
    button.disabled = pushLoading || running || !available;
    button.textContent = enabled && available ? '完成通知：開' : '完成通知：關';
    if (message) copy.textContent = message;
    else if (!supportsPush()) copy.textContent = '這台裝置或瀏覽器目前不支援 Web Push。';
    else if (!pushConfig?.available) copy.textContent = pushConfig?.note || '通知服務目前不可用；勝率更新本身不受影響。';
    else if (running && enabled) copy.textContent = '這一筆工作完成後會通知；執行中先鎖定通知設定。';
    else if (enabled) copy.textContent = '手動啟動／續跑的勝率工作完成後通知；不會訂閱交易訊號。';
    else copy.textContent = '只通知你手動啟動的勝率更新；和市場掃描通知分開設定。';
  }

  async function ensureHistoryPushSubscription() {
    if (!pushConfig?.available || !supportsPush()) throw new Error('這台裝置目前無法使用完成通知');
    if (isIOSDevice() && !isStandaloneApp()) throw new Error('iPhone／iPad 請先把雷達加入主畫面，再開啟通知');
    if (Notification.permission !== 'granted') throw new Error('尚未允許通知權限');
    await navigator.serviceWorker.register('/service-worker.js');
    const registration = await navigator.serviceWorker.ready;
    let subscription = await registration.pushManager.getSubscription();
    const currentKey = String(pushConfig.key_id || '');
    if (subscription && storedVapidKeyId() !== currentKey) {
      await subscription.unsubscribe();
      subscription = null;
    }
    if (!subscription) {
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly:true,
        applicationServerKey:base64UrlBytes(pushConfig.public_key)
      });
    }
    setStoredVapidKeyId(currentKey);
    pushSubscription = subscription;
    return subscription;
  }

  function serializedSubscription(subscription) {
    if (!subscription) return null;
    if (typeof subscription.toJSON === 'function') return subscription.toJSON();
    try { return JSON.parse(JSON.stringify(subscription)); }
    catch (_) { return null; }
  }

  async function loadPushConfig() {
    installNotifyControl();
    if (!supportsPush()) {
      renderNotifyState();
      return;
    }
    try {
      const response = await fetch('/api/push/config', {cache:'no-store'});
      if (!response.ok) throw new Error('通知設定無法取得');
      pushConfig = await response.json();
      if (historyPushEnabled() && Notification.permission === 'granted' && (!isIOSDevice() || isStandaloneApp())) {
        try { await ensureHistoryPushSubscription(); }
        catch (error) { renderNotifyState(`完成通知需重新開啟：${error.message || error}`); return; }
      }
      renderNotifyState();
    } catch (error) {
      pushConfig = {available:false, note:`通知設定無法取得：${error.message || error}`};
      renderNotifyState();
    }
  }

  async function toggleHistoryNotifications() {
    if (pushLoading || active.has(latest?.status)) return;
    if (historyPushEnabled()) {
      setHistoryPushEnabled(false);
      renderNotifyState();
      return;
    }
    pushLoading = true;
    renderNotifyState('正在開啟完成通知…');
    try {
      if (!pushConfig?.available || !supportsPush()) throw new Error('這台裝置目前無法使用完成通知');
      if (isIOSDevice() && !isStandaloneApp()) throw new Error('iPhone／iPad 請先把雷達加入主畫面，再開啟通知');
      let permission = Notification.permission;
      if (permission === 'default') permission = await Notification.requestPermission();
      if (permission !== 'granted') throw new Error('通知權限沒有允許');
      await ensureHistoryPushSubscription();
      setHistoryPushEnabled(true);
      renderNotifyState('已開啟；下一筆手動勝率更新完成時會通知。');
    } catch (error) {
      setHistoryPushEnabled(false);
      renderNotifyState(String(error.message || error));
    } finally {
      pushLoading = false;
      renderNotifyState();
    }
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
    renderNotifyState();
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
    let notificationIssue = '';
    let notificationSubscription = null;
    if ((action === 'start' || action === 'resume') && historyPushEnabled()) {
      try { notificationSubscription = serializedSubscription(await ensureHistoryPushSubscription()); }
      catch (error) { notificationIssue = `勝率更新會照常執行，但完成通知未啟用：${error.message || error}`; }
    }
    try {
      const days = {days:Number($('days').value), inst_id:inst};
      if (notificationSubscription) days.push_subscription = notificationSubscription;
      const response = await fetch('/api/history-scan/' + action, {
        method:'POST',
        headers:{'Content-Type':'application/json','X-History-Intent':'user'},
        body:JSON.stringify(clearAll ? {csrf:latest.csrf} : {csrf:latest.csrf, days})
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
      else if (notificationIssue) renderNotifyState(notificationIssue);
    }
    if (!failure) HistoryReplay.refresh();
  }

  const queryInst = new URLSearchParams(location.search).get('inst_id');
  if (queryInst) $('inst').value = normalize(queryInst) || String(queryInst).toUpperCase();
  installNotifyControl();
  $('inst').addEventListener('input', () => render(latest));
  for (const action of ['start','pause','resume','delete']) $(action).addEventListener('click', () => command(action));
  $('deleteAll').addEventListener('click', () => command('delete_all'));
  window.addEventListener('history-replay-status', event => render(event.detail));
  window.addEventListener('history-replay-error', event => {$('error').textContent = event.detail;});
  render({schema_version:'HISTORY_SINGLE_15M_V1',status:'IDLE',coins:{},csrf:''});
  loadPushConfig();
})();
