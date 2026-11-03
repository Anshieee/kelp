/*
 * Kelp phone app service worker.
 *
 * Strategy:
 *  - /api/*            -> never intercepted (network only; chat data must be live)
 *  - navigation        -> network-first, fall back to the cached app shell
 *  - other /phone/*    -> cache-first (hashed assets, icons, manifest)
 *
 * Bump VERSION whenever the app shell changes shape.
 */
const VERSION = "kelp-phone-v1";
const SHELL_CACHE = `${VERSION}-shell`;
const ASSET_CACHE = `${VERSION}-assets`;
const BASE = "/phone/";

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(SHELL_CACHE);
      await cache.addAll([
        BASE,
        `${BASE}manifest.webmanifest`,
        `${BASE}icons/icon-192.png`,
        `${BASE}icons/icon-512.png`,
      ]);
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys.filter((key) => !key.startsWith(VERSION)).map((key) => caches.delete(key)),
      );
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // Chat data is live-only: never cached, never answered from cache.
  if (url.pathname.startsWith("/api/")) return;

  if (request.mode === "navigate") {
    event.respondWith(networkFirstShell(request));
    return;
  }
  if (url.pathname.startsWith(BASE)) {
    event.respondWith(cacheFirst(request));
  }
});

async function networkFirstShell(request) {
  const cache = await caches.open(SHELL_CACHE);
  try {
    const response = await fetch(request);
    if (response && response.ok) cache.put(BASE, response.clone());
    return response;
  } catch {
    const cached = (await cache.match(BASE)) || (await cache.match(request));
    return (
      cached ||
      new Response("Offline", { status: 503, headers: { "Content-Type": "text/plain" } })
    );
  }
}

async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response && response.ok && response.type === "basic") {
    const cache = await caches.open(ASSET_CACHE);
    cache.put(request, response.clone());
  }
  return response;
}
