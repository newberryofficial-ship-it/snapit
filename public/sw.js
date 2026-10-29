/* SnapIT service worker.
   - Caches the app shell so SnapIT opens instantly and shows a friendly page when offline.
   - NEVER touches /api/* (look-ups, downloads, health pings) - those always go straight to the network.
   Bump VERSION whenever you change any cached file, so installed copies pick up the update. */
const VERSION = 'snapit-v1';
const SHELL = `${VERSION}-shell`;
const RUNTIME = `${VERSION}-runtime`;

const SHELL_FILES = [
  '/',
  '/style.css?v=7',
  '/script.js?v=7',
  '/favicon.svg',
  '/flow-background.jpg',
  '/manifest.webmanifest',
  '/offline.html',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(SHELL).then(cache => cache.addAll(SHELL_FILES)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => !k.startsWith(VERSION)).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Downloads, look-ups and health pings: always live, never cached.
  if (url.origin === self.location.origin && url.pathname.startsWith('/api/')) return;

  // Pages: network first, then the cached page, then the offline page.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)
        .then(res => {
          if (res.ok && url.origin === self.location.origin && url.pathname === '/') {
            const copy = res.clone();
            caches.open(SHELL).then(c => c.put('/', copy));
          }
          return res;
        })
        .catch(async () => (await caches.match('/')) || (await caches.match('/offline.html')))
    );
    return;
  }

  // Google Fonts: cache after first use.
  if (url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com') {
    event.respondWith(staleWhileRevalidate(req, RUNTIME));
    return;
  }

  // Same-origin files (css, js, images, icons): serve cached, refresh in the background.
  if (url.origin === self.location.origin) {
    event.respondWith(staleWhileRevalidate(req, SHELL));
  }
});

async function staleWhileRevalidate(req, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(req);
  const refresh = fetch(req)
    .then(res => { if (res && (res.ok || res.type === 'opaque')) cache.put(req, res.clone()); return res; })
    .catch(() => null);
  return cached || (await refresh) || Response.error();
}
