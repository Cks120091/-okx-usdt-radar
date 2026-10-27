/* One-time SW retirement shim for previous OKX PWA installs.
   This site is now a read-only Solana dashboard with no offline shell.
   An HTTP 404 here leaves the previously installed worker in control. */
"use strict";

self.addEventListener("install", function () {
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil((async function () {
    try {
      const allCaches = await caches.keys();
      await Promise.all(allCaches
        .filter(function (name) {
          return name.startsWith("okx-radar-") || name.startsWith("solana-paper-");
        })
        .map(function (name) { return caches.delete(name); }));
    } catch (_) {
      // Do not prevent unregistration if storage APIs are unavailable.
    }
    await self.registration.unregister();
    const windows = await self.clients.matchAll({
      type: "window", includeUncontrolled: true
    });
    await Promise.all(windows.map(function (client) {
      if ("navigate" in client) {
        return client.navigate(client.url).catch(function () {});
      }
      return Promise.resolve();
    }));
  })());
});
