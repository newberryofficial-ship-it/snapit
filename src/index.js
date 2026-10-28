// SnapIT Worker: serves ./public and forwards the downloader API to the Render backend (backend/main.py).
// The backend only accepts requests that carry PROXY_SECRET, and the worker tells it the visitor's real IP.
export default {
  async fetch(req, env) {
    const u = new URL(req.url);
    const p = u.pathname;
    if (p === '/api/grab' && req.method === 'POST') return proxy(req, env, u);
    if (/^\/api\/download\/[a-f0-9]{24}$/.test(p) && (req.method === 'GET' || req.method === 'HEAD')) return proxy(req, env, u);
    if (p === '/api/health' && req.method === 'GET') return proxy(req, env, u);
    if (p.startsWith('/api/')) return j({ error: p === '/api/grab' ? 'Use POST.' : 'Not found.' }, p === '/api/grab' ? 405 : 404);
    return env.ASSETS.fetch(req);
  }
};

async function proxy(req, env, u) {
  if (!env.BACKEND_URL) return j({ error: 'BACKEND_URL is not configured.' }, 500);
  let t;
  try { t = new URL(u.pathname + u.search, env.BACKEND_URL); } catch { return j({ error: 'BACKEND_URL is invalid.' }, 500); }
  const h = new Headers(req.headers);
  h.delete('X-Proxy-Secret');
  h.delete('X-Client-IP');
  if (env.PROXY_SECRET) h.set('X-Proxy-Secret', env.PROXY_SECRET);
  const ip = req.headers.get('CF-Connecting-IP');
  if (ip) h.set('X-Client-IP', ip);
  h.delete('Host');
  const init = { method: req.method, headers: h, redirect: 'manual' };
  if (req.method === 'POST') init.body = req.body;
  return fetch(t, init);
}

function j(x, s = 200) {
  return new Response(JSON.stringify(x), { status: s, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' } });
}
