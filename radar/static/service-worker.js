const SHELL_CACHE = "okx-radar-shell-v3.3-trigger-lifecycle-1";
const SHELL_ASSETS = ["/", "/manifest.webmanifest", "/radar-icon.svg"];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(SHELL_CACHE).then(cache => cache.addAll(SHELL_ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(key => key !== SHELL_CACHE).map(key => caches.delete(key))
    ))
  );
  self.clients.claim();
});

self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/api/") || url.pathname === "/health") {
    event.respondWith(fetch(event.request, {cache: "no-store"}));
    return;
  }
  if (event.request.method !== "GET") return;
  event.respondWith(
    fetch(event.request)
      .then(response => {
        const copy = response.clone();
        caches.open(SHELL_CACHE).then(cache => cache.put(event.request, copy));
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});

self.addEventListener("push", event => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_) {
    payload = {};
  }
  const title = String(payload.title || "OKX 雷達掃描完成");
  const body = String(payload.body || "最新市場報告已完成，點擊查看結果。");
  let target = "/";
  try {
    const candidate = new URL(String(payload.url || "/"), self.location.origin);
    if (candidate.origin === self.location.origin) target = candidate.pathname + candidate.search + candidate.hash;
  } catch (_) {}
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({type: "window", includeUncontrolled: true});
    if (windows.some(client => client.visibilityState === "visible")) return;
    await self.registration.showNotification(title, {
      body,
      icon: "/radar-icon.svg",
      badge: "/radar-icon.svg",
      tag: String(payload.tag || "okx-radar-scan"),
      renotify: false,
      data: {url: target},
    });
  })());
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || "/", self.location.origin);
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({type: "window", includeUncontrolled: true});
    for (const client of windows) {
      if (new URL(client.url).origin !== self.location.origin) continue;
      if ("navigate" in client) await client.navigate(target.href);
      return client.focus();
    }
    return self.clients.openWindow(target.href);
  })());
});
