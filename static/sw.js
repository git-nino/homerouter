/* Service worker for offline app shell. localhost is a secure context,
   so this installs without HTTPS when the app runs on the phone itself. */
const CACHE = 'gwmon-v11';
const SHELL = [
  '/',
  '/static/style.css',
  '/static/app.js',
  '/static/manifest.webmanifest',
  '/static/favicon.png',
  '/static/apple-touch-icon.png',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/icon-maskable-192.png',
  '/static/icon-maskable-512.png'
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

  // Network-first for navigations and core shell assets so updated UI/defaults
  // are not blocked by stale PWA cache; fall back to cache when offline.
  const networkFirst = request.mode === 'navigate' ||
    url.pathname === '/' ||
    url.pathname === '/static/app.js' ||
    url.pathname === '/static/style.css' ||
    url.pathname === '/static/manifest.webmanifest';

  if (networkFirst) {
    event.respondWith(
      fetch(request).then((response) => {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy)).catch(() => {});
        return response;
      }).catch(() => caches.match(request).then((c) => c || caches.match('/')))
    );
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
