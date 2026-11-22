/* Service worker for offline app shell. localhost is a secure context,
   so this installs without HTTPS when the app runs on the phone itself. */
const CACHE = 'gwmon-v2';
const SHELL = [
  '/',
  '/static/style.css',
  '/static/app.js',
  '/static/manifest.webmanifest',
  '/static/icon-192.png',
  '/static/icon-512.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  // Live router data must never be served from the cache.
  if (url.pathname.startsWith('/api/')) return;

  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(() => caches.match('/')));
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => cached || fetch(request).then((response) => {
      const copy = response.clone();
      caches.open(CACHE).then((cache) => cache.put(request, copy)).catch(() => {});
      return response;
    }))
  );
});
