const CACHE = 'kelp-mobile-public-shell-v1';
const SHELL = [
  '/mobile/',
  '/mobile/index.html',
  '/mobile/app.css?v=1',
  '/mobile/app.js?v=1',
  '/mobile/manifest.webmanifest',
  '/mobile/icon.svg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key.startsWith('kelp-mobile-public-shell-') && key !== CACHE).map((key) => caches.delete(key)))).then(() => self.clients.claim()));
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/') || !SHELL.some((path) => new URL(path, self.location.origin).pathname === url.pathname)) return;
  event.respondWith(caches.match(request).then((cached) => cached || fetch(request)));
});
