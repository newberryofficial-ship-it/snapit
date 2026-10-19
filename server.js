/* SnapIT — backend
   - serves the page from ./public
   - POST /api/grab           link in  -> list of download options out
   - GET  /api/download/<id>  streams the chosen file to the browser as a real
                              download, keeping a temporary copy in ./downloads
   Run:  npm start   (or  node server.js)   then open http://localhost:8000
   Only Node.js built-ins are used (Node 18+), so there is nothing to install. */
'use strict';

const http = require('node:http');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const crypto = require('node:crypto');
const { Readable } = require('node:stream');

/* ---------- settings (.env is read here; real environment variables win) ---------- */
(function loadEnv() {
  let text;
  try { text = fs.readFileSync(path.join(__dirname, '.env'), 'utf8').replace(/^\uFEFF/, ''); }
  catch { return; }
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq < 1) continue;
    const name = line.slice(0, eq).trim().replace(/^export\s+/, '');
    let value = line.slice(eq + 1).trim();
    if (/^(".*"|'.*')$/.test(value)) value = value.slice(1, -1);
    if (!(name in process.env)) process.env[name] = value;
  }
})();

const PORT = Number(process.env.PORT) || 8000;
const KEY = (process.env.RAPIDAPI_KEY || '').trim();
const KEY_READY = KEY !== '' && KEY !== 'your_key_here';
const API_HOST = 'social-download-all-in-one.p.rapidapi.com';
const API_URL = `https://${API_HOST}/v1/social/autolink`;
const FILE_TTL_MS = (Number(process.env.FILE_TTL_MINUTES) || 30) * 60 * 1000;
const MAX_CACHE_BYTES = (Number(process.env.MAX_FILE_MB) || 500) * 1024 * 1024;

const PUBLIC_DIR = path.join(__dirname, 'public');
const DOWNLOADS_DIR = path.join(__dirname, 'downloads');

const ALLOWED = ['youtube.com', 'youtu.be', 'pinterest.com', 'pin.it', 'snapchat.com', 'tiktok.com', 'twitter.com', 'x.com', 't.co', 'instagram.com'];

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.txt': 'text/plain; charset=utf-8',
  '.ico': 'image/x-icon',
};

const MEDIA_MIME = {
  mp4: 'video/mp4', webm: 'video/webm', mov: 'video/quicktime', mkv: 'video/x-matroska',
  mp3: 'audio/mpeg', m4a: 'audio/mp4', aac: 'audio/aac', ogg: 'audio/ogg', opus: 'audio/ogg', wav: 'audio/wav',
  jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png', webp: 'image/webp', gif: 'image/gif',
};

/* ---------- small response helpers ---------- */
function send(res, status, body, headers = {}) {
  res.writeHead(status, { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', ...headers });
  res.end(body);
}
const json = (res, status, obj) => send(res, status, JSON.stringify(obj), { 'Content-Type': 'application/json; charset=utf-8' });
const plain = (res, status, msg) => send(res, status, msg, { 'Content-Type': 'text/plain; charset=utf-8' });

function readBody(req, limit = 20000) {
  return new Promise((resolve, reject) => {
    let size = 0;
    let tooBig = false;
    const chunks = [];
    req.on('data', (c) => {
      if (tooBig) return; // keep draining so the 413 reply can be delivered
      size += c.length;
      if (size > limit) {
        tooBig = true;
        chunks.length = 0;
        reject(Object.assign(new Error('too large'), { status: 413 }));
        return;
      }
      chunks.push(c);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

/* ---------- link checks ---------- */
function isAllowedLink(v) {
  if (typeof v !== 'string' || v.length > 2000) return false;
  try {
    const u = new URL(v);
    return u.protocol === 'https:' && !u.username && !u.password &&
      ALLOWED.some((h) => u.hostname === h || u.hostname.endsWith('.' + h));
  } catch {
    return false;
  }
}

/* The media address comes from a third-party service, so refuse anything that points back at this machine or network. */
function isSafeMediaUrl(v) {
  try {
    const u = new URL(v);
    if (u.protocol !== 'https:' && u.protocol !== 'http:') return false;
    const h = u.hostname.toLowerCase().replace(/^\[|\]$/g, '');
    if (h === 'localhost' || h.endsWith('.localhost') || h.endsWith('.local') || h.endsWith('.internal')) return false;
    if (net.isIPv6(h)) return false;
    if (net.isIPv4(h)) {
      const [a, b] = h.split('.').map(Number);
      if (a === 0 || a === 10 || a === 127 || a >= 224) return false;
      if (a === 169 && b === 254) return false;
      if (a === 172 && b >= 16 && b <= 31) return false;
      if (a === 192 && b === 168) return false;
    }
    return true;
  } catch {
    return false;
  }
}

/* ---------- temporary download store ---------- */
const files = new Map();      // id -> { url, filename, ext, created }
const inflight = new Set();   // ids currently being copied into ./downloads

function register(entry) {
  const id = crypto.randomBytes(12).toString('hex');
  files.set(id, { ...entry, created: Date.now() });
  if (files.size > 2000) files.delete(files.keys().next().value); // oldest first
  return id;
}

function sweep(everything = false) {
  const now = Date.now();
  for (const [id, e] of files) if (everything || now - e.created > FILE_TTL_MS) files.delete(id);
  fs.readdir(DOWNLOADS_DIR, (err, names) => {
    if (err) return;
    for (const n of names) {
      if (n === '.gitkeep') continue;
      const p = path.join(DOWNLOADS_DIR, n);
      fs.stat(p, (e, st) => {
        if (!e && st.isFile() && (everything || now - st.mtimeMs > FILE_TTL_MS)) fs.unlink(p, () => {});
      });
    }
  });
}

/* ---------- /api/grab ---------- */
const cleanExt = (v) => String(v || '').replace(/[^a-z0-9]/gi, '').toLowerCase().slice(0, 5);

function extFromUrl(v) {
  try { return cleanExt(path.extname(new URL(v).pathname)); } catch { return ''; }
}

function kindOf(m) {
  const t = String(m.type || '').toLowerCase();
  if (t === 'audio') return 'audio';
  if (t === 'image') return 'image';
  return 'video';
}


/* ---------- Snapchat: read the media straight from Snapchat's own servers ----------
   Snapchat's web page embeds its data as JSON (__NEXT_DATA__). The "contentUrl" /
   "mediaUrl" it lists for Spotlight is the *sharing* render (variant .27) which has the
   "Snapchat @user" watermark burned in. The same media id also exists as the clean
   original render (variant .1034) with no overlay, so we rewrite to that and check it. */
const BROWSER_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36';
const isSnapHost = (h) => h === 'snapchat.com' || h.endsWith('.snapchat.com');
const isSnapCdn = (h) => h.endsWith('.sc-cdn.net') || h.endsWith('.snapchat.com');

function cleanCandidates(mediaUrl) {
  let u;
  try { u = new URL(mediaUrl); } catch { return [mediaUrl]; }
  const m = u.pathname.match(/^\/([a-z0-9]+)\/([A-Za-z0-9_-]+)\.(\d+)\.([A-Za-z0-9]+)$/);
  if (!m || m[3] === '1034') return [mediaUrl];
  const [, dir, id, , tail] = m;
  const out = [];
  const add = (host, d) => { const x = `https://${host}/${d}/${id}.1034.${tail}`; if (!out.includes(x)) out.push(x); };
  add(u.hostname, dir);
  add('cf-st.sc-cdn.net', 'd');
  add('bolt-gcdn.sc-cdn.net', dir);
  add('bolt-gcdn.sc-cdn.net', 'v');
  add('bolt-gcdn.sc-cdn.net', 'x');
  return out;
}

async function firstWorking(urls) {
  for (const u of urls) {
    try {
      const r = await fetch(u, { headers: { 'User-Agent': BROWSER_UA, Range: 'bytes=0-0' }, signal: AbortSignal.timeout(10000) });
      if (r.body) r.body.cancel().catch(() => {});
      if ((r.status === 200 || r.status === 206) && /video|octet/.test(r.headers.get('content-type') || '')) return u;
    } catch {}
  }
  return null;
}

async function grabSnapchat(link) {
  let r;
  try {
    r = await fetch(link, { headers: { 'User-Agent': BROWSER_UA, 'Accept-Language': 'en-US,en;q=0.9' }, signal: AbortSignal.timeout(20000) });
  } catch { return null; }
  if (!r.ok) return null;
  const html = await r.text();
  const mt = html.match(/<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)<\/script>/);
  if (!mt) return null;
  let pp;
  try { pp = JSON.parse(mt[1]).props.pageProps || {}; } catch { return null; }

  const found = [];
  const push = (url, kind = 'video') => { if (typeof url === 'string' && url && !found.some((f) => f.url === url)) found.push({ url, kind }); };
  // Spotlight page: the snap the link points at.
  if (pp.videoMetadata) push(pp.videoMetadata.contentUrl);
  // Public story / profile pages: every snap in the story.
  const lists = [pp.story && pp.story.snapList, ...(pp.curatedHighlights || []).map((h) => h.snapList), ...(pp.spotlightHighlights || []).map((h) => h.snapList)];
  if (!found.length) {
    for (const list of lists) for (const s of list || []) {
      const u = s && s.snapUrls && s.snapUrls.mediaUrl;
      push(u, s.snapMediaType === 0 ? 'image' : 'video');
    }
  }
  if (!found.length && pp.spotlightFeed) {
    const st = (pp.spotlightFeed.spotlightStories || [])[0];
    if (st) push(st.metadata && st.metadata.videoMetadata && st.metadata.videoMetadata.contentUrl);
  }
  if (!found.length) return null;

  const title = String((pp.videoMetadata && (pp.videoMetadata.name || pp.videoMetadata.description)) || (pp.userProfile && pp.userProfile.publicProfileInfo && pp.userProfile.publicProfileInfo.username) || 'snapchat');
  const medias = [];
  for (const f of found.slice(0, 10)) {
    if (f.kind === 'image') { medias.push({ url: f.url, type: 'image', extension: 'jpg' }); continue; }
    const clean = await firstWorking(cleanCandidates(f.url));
    if (clean) medias.push({ url: clean, type: 'video', extension: 'mp4', quality: 'Original · no watermark' });
  }
  return medias.length ? { title, medias } : null;
}

async function handleGrab(req, res) {
  let url;
  try {
    url = JSON.parse(await readBody(req)).url;
  } catch (e) {
    if (e.status === 413) res.setHeader('Connection', 'close');
    return json(res, e.status || 400, { error: e.status === 413 ? 'Request too large.' : 'Invalid request.' });
  }
  if (!isAllowedLink(url)) {
    return json(res, 400, { error: 'Paste a valid HTTPS video link from a supported platform.' });
  }
  let snapResult = null;
  if (isSnapHost(new URL(url).hostname)) {
    snapResult = await grabSnapchat(url).catch((e) => { console.error('Snapchat grab failed:', e.message); return null; });
    if (snapResult) return sendOptions(res, snapResult);
    if (!KEY_READY) return json(res, 502, { error: 'This Snapchat video could not be read. It may be private, removed or not public.' });
  }
  if (!KEY_READY) {
    return json(res, 500, { error: 'The download service isn\'t connected yet. Put your RapidAPI key in the .env file (RAPIDAPI_KEY=...) and restart the server.' });
  }

  let upstream;
  try {
    upstream = await fetch(API_URL, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-rapidapi-key': KEY,
        'x-rapidapi-host': API_HOST,
      },
      body: JSON.stringify({ url }),
      signal: AbortSignal.timeout(45000),
    });
  } catch (e) {
    console.error('Upstream unreachable:', e.name === 'TimeoutError' ? 'timed out' : e);
    return json(res, 502, { error: 'Could not reach the download service. Try again in a moment.' });
  }

  let text;
  try { text = await upstream.text(); }
  catch (e) {
    console.error('Upstream body failed:', e);
    return json(res, 502, { error: 'Could not reach the download service. Try again in a moment.' });
  }

  if (!upstream.ok) {
    console.error(`Download service failed [${upstream.status}]: ${text.slice(0, 500)}`);
    if (upstream.status === 429) return json(res, 429, { error: 'Too many requests right now. Wait a minute and try again.' });
    if (upstream.status === 401 || upstream.status === 403) {
      return json(res, 502, { error: 'The download service key isn\'t valid or isn\'t subscribed.' });
    }
    return json(res, 502, { error: 'The video could not be fetched. Check the link or try again.' });
  }

  let data;
  try { data = JSON.parse(text); }
  catch {
    console.error('Download service sent non-JSON:', text.slice(0, 300));
    return json(res, 502, { error: 'The download service sent an unexpected reply.' });
  }

  // Some providers wrap the result in { data: {...} }; accept both shapes.
  const payload = Array.isArray(data.medias) ? data : (data.data && typeof data.data === 'object' ? data.data : data);
  const medias = Array.isArray(payload.medias) ? payload.medias : null;
  if (data.error || !medias) {
    console.error('No medias in reply:', text.slice(0, 500));
    return json(res, 502, { error: (typeof data.message === 'string' && data.message) || 'No downloadable version of this video was found.' });
  }

  sendOptions(res, { title: payload.title || data.title, medias });
}

function sendOptions(res, src) {
  const title = String(src.title || 'video')
    .replace(/[^\p{L}\p{N}\s_-]/gu, '').replace(/\s+/g, ' ').trim().slice(0, 80) || 'video';

  const usable = src.medias.filter((m) => m && typeof m.url === 'string' && isSafeMediaUrl(m.url));
  const videos = usable.filter((m) => kindOf(m) === 'video');
  const audios = usable.filter((m) => kindOf(m) === 'audio');
  const images = usable.filter((m) => kindOf(m) === 'image');
  let picked = [...videos.slice(0, 4), ...audios.slice(0, 2)];
  if (!picked.length) picked = images.slice(0, 4);

  const options = picked.map((m) => {
    const kind = kindOf(m);
    const ext = cleanExt(m.extension) || extFromUrl(m.url) || (kind === 'audio' ? 'mp3' : kind === 'image' ? 'jpg' : 'mp4');
    const filename = `${title}.${ext}`;
    const id = register({ url: m.url, filename, ext });
    return {
      label: kind === 'audio' ? 'Audio' : kind === 'image' ? 'Image' : `Video${m.quality ? ' · ' + m.quality : ''}`,
      format: ext.toUpperCase(),
      kind,
      filename,
      url: `/api/download/${id}`,
    };
  });

  if (!options.length) return json(res, 502, { error: 'No downloadable version of this video was found.' });
  json(res, 200, { options });
}

/* ---------- /api/download/<id> ---------- */
function contentDisposition(name) {
  const ascii = name.replace(/[^\x20-\x7E]/g, '_').replace(/["\\]/g, '_');
  return `attachment; filename="${ascii}"; filename*=UTF-8''${encodeURIComponent(name)}`;
}

/* Follow redirects by hand so every hop is checked, not just the first address. */
async function fetchMedia(url, signal) {
  for (let hop = 0; hop < 5; hop++) {
    if (!isSafeMediaUrl(url)) throw new Error('blocked media address');
    const r = await fetch(url, { signal, redirect: 'manual', headers: isSnapCdn(new URL(url).hostname) ? { 'User-Agent': BROWSER_UA, Referer: 'https://www.snapchat.com/' } : { 'User-Agent': 'Mozilla/5.0 (compatible; SnapIT)' } });
    if (r.status >= 300 && r.status < 400 && r.headers.get('location')) {
      url = new URL(r.headers.get('location'), url).href;
      if (r.body) r.body.cancel().catch(() => {});
      continue;
    }
    return r;
  }
  throw new Error('too many redirects');
}

async function handleDownload(id, res) {
  const entry = files.get(id);
  if (!entry) return plain(res, 410, 'This download link has expired. Go back to SnapIT and grab the video again.');

  const cachePath = path.join(DOWNLOADS_DIR, id);
  const baseHeaders = {
    'Content-Disposition': contentDisposition(entry.filename),
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
  };

  // Already processed earlier? Serve the temporary copy.
  const st = await fs.promises.stat(cachePath).catch(() => null);
  if (st && st.isFile()) {
    res.writeHead(200, { ...baseHeaders, 'Content-Type': MEDIA_MIME[entry.ext] || 'application/octet-stream', 'Content-Length': st.size });
    const rs = fs.createReadStream(cachePath);
    rs.on('error', () => res.destroy());
    res.on('close', () => rs.destroy());
    rs.pipe(res);
    return;
  }

  const ac = new AbortController();
  res.on('close', () => { if (!res.writableFinished) ac.abort(); });

  let up;
  try {
    up = await fetchMedia(entry.url, ac.signal);
  } catch (e) {
    if (ac.signal.aborted) return;
    console.error('Media fetch failed:', e.message);
    return plain(res, 502, 'The file could not be fetched. Go back to SnapIT and grab the video again.');
  }
  if (!up.ok || !up.body) {
    console.error(`Media host answered ${up.status}`);
    return plain(res, 502, 'The file could not be fetched. Go back to SnapIT and grab the video again.');
  }

  const headers = {
    ...baseHeaders,
    'Content-Type': MEDIA_MIME[entry.ext] || up.headers.get('content-type') || 'application/octet-stream',
  };
  // A length is only trustworthy when the body isn't being re-encoded on the way.
  const len = Number(up.headers.get('content-length')) || 0;
  if (len && !up.headers.get('content-encoding')) headers['Content-Length'] = len;
  res.writeHead(200, headers);

  const src = Readable.fromWeb(up.body);
  const cacheIt = !inflight.has(id) && (!len || len <= MAX_CACHE_BYTES);
  const tmp = cachePath + '.part';
  let out = null;
  let cacheDead = !cacheIt;

  const dropCache = () => {
    if (cacheDead) return;
    cacheDead = true;
    if (out) { src.unpipe(out); out.destroy(); }
    fs.unlink(tmp, () => {});
  };

  if (cacheIt) {
    inflight.add(id);
    out = fs.createWriteStream(tmp);
    out.on('error', dropCache);
    out.on('finish', () => { if (!cacheDead) fs.rename(tmp, cachePath, () => {}); });
    out.on('close', () => inflight.delete(id));
    let size = 0;
    src.on('data', (c) => { size += c.length; if (size > MAX_CACHE_BYTES) dropCache(); });
    src.pipe(out);
  }

  src.on('error', (e) => {
    if (!ac.signal.aborted) console.error('Media stream broke:', e.message);
    dropCache();
    res.destroy();
  });
  src.on('close', () => {
    if (!src.readableEnded) { dropCache(); res.destroy(); }
  });
  res.on('close', () => {
    if (!res.writableFinished) { src.destroy(); dropCache(); }
  });
  src.pipe(res);
}

/* ---------- static files from ./public ---------- */
function serveStatic(req, res, pathName) {
  let rel;
  try { rel = decodeURIComponent(pathName); } catch { return plain(res, 400, 'Bad request'); }
  if (rel.includes('\0')) return plain(res, 400, 'Bad request');
  if (rel.endsWith('/')) rel += 'index.html';

  const file = path.normalize(path.join(PUBLIC_DIR, rel));
  if (!file.startsWith(PUBLIC_DIR + path.sep)) return plain(res, 403, 'Forbidden');
  if (/(^|[\\/])\./.test(path.relative(PUBLIC_DIR, file))) return plain(res, 404, 'Not found');

  fs.readFile(file, (err, buf) => {
    if (err) return plain(res, 404, 'Not found');
    send(res, 200, buf, { 'Content-Type': TYPES[path.extname(file).toLowerCase()] || 'application/octet-stream' });
  });
}

/* ---------- CORS (frontend is hosted on a different domain) ---------- */
// Set ALLOWED_ORIGIN to your frontend's exact origin, e.g. https://snapit.pages.dev
// (no trailing slash). Requests from any other website's browser are not allowed.
const ALLOWED_ORIGIN = (process.env.ALLOWED_ORIGIN || '').replace(/\/+$/, '');

function applyCors(req, res) {
  const origin = req.headers.origin;
  if (origin && ALLOWED_ORIGIN && origin === ALLOWED_ORIGIN) {
    res.setHeader('Access-Control-Allow-Origin', origin);
    res.setHeader('Vary', 'Origin');
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
    res.setHeader('Access-Control-Max-Age', '86400');
  }
}

/* ---------- routing ---------- */
async function route(req, res) {
  applyCors(req, res);
  if (req.method === 'OPTIONS') { res.writeHead(204); return res.end(); }

  let pathName;
  try { pathName = new URL(req.url, 'http://localhost').pathname; }
  catch { return plain(res, 400, 'Bad request'); }

  if (pathName === '/api/grab') {
    if (req.method !== 'POST') return json(res, 405, { error: 'Use POST.' });
    return handleGrab(req, res);
  }

  const dl = pathName.match(/^\/api\/download\/([a-f0-9]{24})$/);
  if (dl) {
    if (req.method !== 'GET') return plain(res, 405, 'Use GET.');
    return handleDownload(dl[1], res);
  }

  if (req.method !== 'GET' && req.method !== 'HEAD') return plain(res, 405, 'Method not allowed');
  serveStatic(req, res, pathName);
}

const server = http.createServer((req, res) => {
  route(req, res).catch((err) => {
    console.error('Unhandled error:', err);
    if (!res.headersSent) json(res, 500, { error: 'Something went wrong on the server. Try again.' });
    else res.destroy();
  });
});

server.on('error', (e) => {
  if (e.code === 'EADDRINUSE') console.error(`Port ${PORT} is already in use. Change PORT in .env or stop the other program.`);
  else console.error(e);
  process.exit(1);
});

fs.mkdirSync(DOWNLOADS_DIR, { recursive: true });
sweep(true); // copies from an earlier run can no longer be reached, so clear them
setInterval(() => sweep(false), 5 * 60 * 1000).unref();

server.listen(PORT, () => {
  console.log(`SnapIT running at http://localhost:${PORT}`);
  console.log(KEY_READY
    ? 'Download service key loaded.'
    : 'WARNING: no RAPIDAPI_KEY set in .env — the Grab button will report the service as not connected.');
});
