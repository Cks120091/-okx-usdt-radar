const SHELL_CACHE = "okx-radar-shell-v3.3";
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
