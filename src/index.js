// SnapIT Worker: serves ./public and forwards the downloader API to the Render backend (backend/main.py).
// The backend only accepts requests that carry PROXY_SECRET, and the worker tells it the visitor's real IP.
//
// Keep-awake: wrangler.toml has a cron trigger (every 2 minutes). It calls scheduled() below, which hits the
// backend's /health. Render's free plan sleeps a service after ~15 minutes without traffic; this keeps it
// awake, and would also wake it up if it ever did fall asleep.
export default {
  async fetch(req, env) {
    const u = new URL(req.url);
    const p = u.pathname;
    if (p === '/api/grab' && req.method === 'POST') return proxy(req, env, u);
    if (/^\/api\/download\/[a-f0-9]{24}$/.test(p) && (req.method === 'GET' || req.method === 'HEAD')) return proxy(req, env, u);
    if (p === '/api/health' && req.method === 'GET') return proxy(req, env, u);
    if (p.startsWith('/api/')) return j({ error: p === '/api/grab' ? 'Use POST.' : 'Not found.' }, p === '/api/grab' ? 405 : 404);
    return env.ASSETS.fetch(req);
  },

  // Cron trigger: runs every 2 minutes (see [triggers] in wrangler.toml)
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(wake(env));
  }
};

async function wake(env) {
  if (!env.BACKEND_URL) return;
  try {
    const r = await fetch(new URL('/health', env.BACKEND_URL), { headers: { 'User-Agent': 'SnapIT-cron' } });
    await r.arrayBuffer();
  } catch { /* the next run tries again */ }
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

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
  const body = req.method === 'POST' ? await req.arrayBuffer() : undefined; // buffered so it can be sent again

  // A sleeping Render service can answer its first request with a 502/503/504 HTML page while it boots.
  // Our own backend always answers errors as JSON, so only non-JSON gateway errors are retried.
  for (let i = 0; i < 3; i++) {
    try {
      const res = await fetch(t, { method: req.method, headers: h, redirect: 'manual', body });
      const type = res.headers.get('content-type') || '';
      if (![502, 503, 504].includes(res.status) || type.includes('application/json') || i === 2) return res;
      res.body && res.body.cancel();
    } catch {
      if (i === 2) return j({ error: 'The SnapIT server is waking up. Try again in a few seconds.' }, 503);
    }
    await sleep(2500);
  }
}

function j(x, s = 200) {
  return new Response(JSON.stringify(x), { status: s, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' } });
}
