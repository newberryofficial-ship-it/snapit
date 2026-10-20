/* SnapIT — backend (API only; the page itself is hosted separately)
   - POST /api/grab           link in  -> list of download options out
   - GET  /api/download/<id>  streams the chosen file to the browser as a real
                              download, keeping a temporary copy in ./downloads
   Run:  npm start   (or  node server.js)
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

const yt = require('./youtube'); // after .env is read, because it reads its own settings when it loads
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
   original render (variant .1034) with no overlay, so we try that first and fall back
   to the address the page gave us, so a video is never lost just because the clean copy
   isn't available. */
const BROWSER_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36';
const MOBILE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1';
const BOT_UA = 'facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)';
const isSnapHost = (h) => h === 'snapchat.com' || h.endsWith('.snapchat.com');
const isSnapCdn = (h) => h.endsWith('.sc-cdn.net') || h.endsWith('.snapchat.com');
const SNAP_MEDIA_RE = /^\/([A-Za-z0-9]+)\/([A-Za-z0-9_-]+)\.(\d+)\.([A-Za-z0-9]+)$/;

function cleanCandidates(mediaUrl) {
  let u;
  try { u = new URL(mediaUrl); } catch { return []; }
  const m = u.pathname.match(SNAP_MEDIA_RE);
  if (!m || m[3] === '1034') return [];
  const [, dir, id, , tail] = m;
  const out = [];
  const add = (host, d, query = '') => { const x = `https://${host}/${d}/${id}.1034.${tail}${query}`; if (!out.includes(x)) out.push(x); };
  add(u.hostname, dir);
  add(u.hostname, dir, u.search); // some copies need the signed query string the page gave us
  add('cf-st.sc-cdn.net', 'd');
  add('bolt-gcdn.sc-cdn.net', dir);
  add('bolt-gcdn.sc-cdn.net', 'v');
  add('bolt-gcdn.sc-cdn.net', 'x');
  return out;
}

async function snapReachable(u) {
  try {
    const r = await fetch(u, { headers: { 'User-Agent': BROWSER_UA, Referer: 'https://www.snapchat.com/', Range: 'bytes=0-0' }, signal: AbortSignal.timeout(10000) });
    if (r.body) r.body.cancel().catch(() => {});
    return (r.status === 200 || r.status === 206) && /video|octet/.test(r.headers.get('content-type') || '');
  } catch { return false; }
}

/* Checks every address at the same time but keeps the order of preference. */
async function firstWorking(urls) {
  const ok = await Promise.all(urls.map(snapReachable));
  const i = ok.indexOf(true);
  return i === -1 ? null : urls[i];
}

async function resolveSnapVideo(mediaUrl) {
  let m = null;
  try { m = new URL(mediaUrl).pathname.match(SNAP_MEDIA_RE); } catch {}
  if (m && m[3] === '1034') return { url: mediaUrl, quality: 'Original · no watermark' };
  const clean = await firstWorking(cleanCandidates(mediaUrl));
  if (clean) return { url: clean, quality: 'Original · no watermark' };
  return { url: mediaUrl, quality: 'Standard · may show watermark' };
}

const decodeEntities = (v) => v.replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/&#0*39;|&#x27;/gi, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>');
const looksLikeVideoUrl = (v) => /^https?:\/\//i.test(v) && (/\.sc-cdn\.net\//i.test(v) || /\.(mp4|mov|m4v|webm)(\?|$)/i.test(v));

function deepScanSnap(node, push, depth = 0) {
  if (node == null || depth > 14) return;
  if (typeof node === 'string') {
    if (/^https?:\/\/[^\s?]*sc-cdn\.net\/[^\s?]*\.\d+\.mp4(\?|$)/i.test(node)) push(node);
    return;
  }
  if (Array.isArray(node)) { for (const x of node) deepScanSnap(x, push, depth + 1); return; }
  if (typeof node === 'object') for (const k of Object.keys(node)) deepScanSnap(node[k], push, depth + 1);
}

/* Pulls every snap out of one copy of the Snapchat page. */
function extractSnapMedia(html, link) {
  const found = [];
  const seen = new Set();
  const push = (url, kind = 'video') => {
    if (typeof url !== 'string' || !/^https?:\/\//i.test(url)) return;
    let key = url;
    try { const m = new URL(url).pathname.match(SNAP_MEDIA_RE); if (m) key = m[2]; } catch { return; }
    if (seen.has(key)) return; // same media id = same snap, whichever render it is
    seen.add(key);
    found.push({ url, kind });
  };

  let title = '';
  let root = null;
  let pp = null;
  const mt = html.match(/<script[^>]*\bid=["']__NEXT_DATA__["'][^>]*>([\s\S]*?)<\/script>/);
  if (mt) { try { root = JSON.parse(mt[1]); pp = (root && root.props && root.props.pageProps) || {}; } catch {} }

  if (pp) {
    // Spotlight page: the snap the link points at.
    if (pp.videoMetadata) push(pp.videoMetadata.contentUrl);
    // Public story / profile pages: every snap in the story.
    const lists = [pp.story && pp.story.snapList, ...(pp.curatedHighlights || []).map((h) => h && h.snapList), ...(pp.spotlightHighlights || []).map((h) => h && h.snapList)];
    if (!found.length) {
      for (const list of lists) for (const s of list || []) {
        const u = s && s.snapUrls && s.snapUrls.mediaUrl;
        push(u, s && s.snapMediaType === 0 ? 'image' : 'video');
      }
    }
    if (!found.length && pp.spotlightFeed) {
      const st = (pp.spotlightFeed.spotlightStories || [])[0];
      if (st) push(st.metadata && st.metadata.videoMetadata && st.metadata.videoMetadata.contentUrl);
    }
    title = String((pp.videoMetadata && (pp.videoMetadata.name || pp.videoMetadata.description)) || (pp.userProfile && pp.userProfile.publicProfileInfo && pp.userProfile.publicProfileInfo.username) || '');
  }

  // The page layout changes now and then: if the known places are empty, scan all of the page data.
  if (!found.length && root) {
    const deep = [];
    const seenDeep = new Set();
    deepScanSnap(root, (u) => { if (!seenDeep.has(u)) { seenDeep.add(u); deep.push(u); } });
    const limit = /\/spotlight\//i.test(link) ? 1 : 10;
    deep.slice(0, limit).forEach((u) => push(u));
  }

  // Last resort: the link-preview tags and structured data every public page carries.
  if (!found.length) {
    for (const tag of html.match(/<meta\b[^>]*>/gi) || []) {
      const prop = (tag.match(/\b(?:property|name)=["']([^"']+)["']/i) || [])[1];
      const content = (tag.match(/\bcontent=["']([^"']*)["']/i) || [])[1];
      if (!prop || !content) continue;
      if (/^(og:video(:secure_url|:url)?|twitter:player:stream)$/i.test(prop)) {
        const v = decodeEntities(content);
        if (looksLikeVideoUrl(v)) push(v);
      } else if (!title && /^og:title$/i.test(prop)) title = decodeEntities(content);
    }
    for (const m of html.matchAll(/"contentUrl"\s*:\s*"((?:[^"\\]|\\.)*)"/g)) {
      try { const v = JSON.parse('"' + m[1] + '"'); if (looksLikeVideoUrl(v)) push(v); } catch {}
    }
  }
  return { found, title };
}

async function grabSnapchat(link) {
  let best = null;
  // Snapchat sometimes answers one kind of visitor with a block or a bare page, so try a few.
  for (const ua of [BROWSER_UA, MOBILE_UA, BOT_UA]) {
    let r;
    try {
      r = await fetch(link, { headers: { 'User-Agent': ua, Accept: 'text/html,application/xhtml+xml,*/*;q=0.8', 'Accept-Language': 'en-US,en;q=0.9' }, signal: AbortSignal.timeout(20000) });
    } catch { continue; }
    if (r.status === 404 || r.status === 410) break;
    if (!r.ok) { if (r.body) r.body.cancel().catch(() => {}); continue; }
    let html;
    try { html = await r.text(); } catch { continue; }
    const got = extractSnapMedia(html, link);
    if (got.found.length) { best = got; break; }
  }
  if (!best) return null;

  const title = best.title || 'snapchat';
  const medias = [];
  for (const f of best.found.slice(0, 10)) {
    if (f.kind === 'image') { medias.push({ url: f.url, type: 'image', extension: 'jpg' }); continue; }
    const v = await resolveSnapVideo(f.url);
    medias.push({ url: v.url, type: 'video', extension: 'mp4', quality: v.quality });
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
  let ytProblem = '';
  let ytTried = false;
  if (yt.isYouTubeLink(url) && await yt.isReady()) {
    ytTried = true;
    try {
      return sendYoutubeOptions(res, await yt.grab(url));
    } catch (e) {
      console.error('YouTube grab failed:', e.userMessage || e.message);
      if (e.final) return json(res, 422, { error: e.userMessage });
      ytProblem = e.userMessage || '';
      // otherwise carry on with the older method below
    }
  }
  let snapResult = null;
  if (isSnapHost(new URL(url).hostname)) {
    snapResult = await grabSnapchat(url).catch((e) => { console.error('Snapchat grab failed:', e.message); return null; });
    if (snapResult) return sendOptions(res, snapResult);
    if (!KEY_READY) return json(res, 502, { error: 'This Snapchat video could not be read. It may be private, removed or not public.' });
  }
  if (!KEY_READY && ytTried) {
    return json(res, 502, { error: ytProblem || 'YouTube did not allow this video to be read right now. Try again in a few minutes.' });
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

const cleanTitle = (t) => String(t || 'video')
  .replace(/[^\p{L}\p{N}\s_-]/gu, '').replace(/\s+/g, ' ').trim().slice(0, 80) || 'video';

/* YouTube: one entry per quality. The file itself is built later, when Download is pressed. */
function sendYoutubeOptions(res, g) {
  const title = cleanTitle(g.title);
  const options = g.offered.map((h) => {
    const filename = `${title} (${h ? h + 'p' : 'best'}).mp4`;
    const id = register({ yt: { link: g.link, clientArgs: g.clientArgs, res: h, audio: false }, filename, ext: 'mp4' });
    return { label: h ? `Video · ${h}p` : 'Video · best quality', format: 'MP4', kind: 'video', filename, url: `/api/download/${id}` };
  });
  const aFile = `${title}.m4a`;
  const aId = register({ yt: { link: g.link, clientArgs: g.clientArgs, res: 0, audio: true }, filename: aFile, ext: 'm4a' });
  options.push({ label: 'Audio', format: 'M4A', kind: 'audio', filename: aFile, url: `/api/download/${aId}` });
  json(res, 200, { options });
}

function sendOptions(res, src) {
  const title = cleanTitle(src.title);

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

/* ---------- fetching the media file ----------
   Different hosts want different "faces". Each host gets a short list of header sets
   (profiles); they are tried in order until one is answered with a real file, and the
   one that worked is remembered for the real download. */
const isYouTubeHost = (h) => h === 'googlevideo.com' || h.endsWith('.googlevideo.com') || h === 'youtube.com' || h.endsWith('.youtube.com');

const APP_UAS = {
  ANDROID: 'com.google.android.youtube/19.44.38 (Linux; U; Android 14; en_US) gzip',
  ANDROID_VR: 'com.google.android.apps.youtube.vr.oculus/1.60.19 (Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip',
  IOS: 'com.google.ios.youtube/19.45.4 (iPhone16,2; U; CPU iOS 18_1_0 like Mac OS X;)',
};

function mediaProfiles(url) {
  let host = '';
  let client = '';
  try { const u = new URL(url); host = u.hostname.toLowerCase(); client = (u.searchParams.get('c') || '').toUpperCase(); } catch {}

  if (isSnapCdn(host)) {
    return [{ name: 'snapchat', headers: { 'User-Agent': BROWSER_UA, Referer: 'https://www.snapchat.com/' } }];
  }

  if (isYouTubeHost(host)) {
    const list = [];
    const web = { Accept: '*/*', 'Accept-Language': 'en-US,en;q=0.9', Origin: 'https://www.youtube.com', Referer: 'https://www.youtube.com/' };
    const app = (name) => ({ name: 'youtube-' + name.toLowerCase(), headers: { 'User-Agent': APP_UAS[name], Accept: '*/*', Range: 'bytes=0-' } });
    // 1) the address was made for a particular YouTube app: look like that app
    if (APP_UAS[client]) list.push(app(client));
    // 2) look like a browser on youtube.com, asking for the whole file
    list.push({ name: 'youtube-browser', headers: { ...web, 'User-Agent': BROWSER_UA, Range: 'bytes=0-' } });
    // 3) the same without a Range header
    list.push({ name: 'youtube-browser-plain', headers: { ...web, 'User-Agent': BROWSER_UA } });
    // 4) the Android app, if not tried yet
    if (client !== 'ANDROID') list.push(app('ANDROID'));
    return list;
  }

  return [
    { name: 'default', headers: { 'User-Agent': 'Mozilla/5.0 (compatible; SnapIT)' } },
    { name: 'browser', headers: { 'User-Agent': BROWSER_UA, Accept: '*/*' } },
  ];
}

/* Follow redirects by hand so every hop is checked, not just the first address.
   The timer only covers waiting for the answer, not the download itself. */
async function fetchFollow(startUrl, headers, outer) {
  let url = startUrl;
  for (let hop = 0; hop < 6; hop++) {
    if (!isSafeMediaUrl(url)) throw new Error('blocked media address');
    const ac = new AbortController();
    const onAbort = () => ac.abort();
    if (outer.aborted) ac.abort(); else outer.addEventListener('abort', onAbort, { once: true });
    const timer = setTimeout(() => ac.abort(), 60000);
    let r;
    try { r = await fetch(url, { signal: ac.signal, redirect: 'manual', headers }); }
    finally { clearTimeout(timer); }
    if (r.status >= 300 && r.status < 400 && r.headers.get('location')) {
      url = new URL(r.headers.get('location'), url).href;
      if (r.body) r.body.cancel().catch(() => {});
      continue;
    }
    return { res: r, url };
  }
  throw new Error('too many redirects');
}

function mediaFailMessage(status) {
  if (status === 403) return 'The video host refused the download (403). Try another quality or format, or grab the link again.';
  if (status === 404 || status === 410) return 'This file is no longer available from the video host. Grab the link again.';
  if (status === 429) return 'The video host is limiting requests right now. Wait a minute and try again.';
  if (status >= 500) return `The video host had a problem (${status}). Try again in a moment.`;
  return 'The file could not be fetched. Try another option, or grab the link again.';
}

/* Returns an open, successful response for the file, or throws with a .status. */
async function openMedia(entry, signal) {
  const tries = [];
  const seen = new Set();
  const add = (url, profile) => { const k = url + '|' + profile.name; if (!seen.has(k)) { seen.add(k); tries.push({ url, profile }); } };
  if (entry.winner) add(entry.winner.url, entry.winner.profile);
  for (const p of mediaProfiles(entry.url)) add(entry.url, p);

  let lastStatus = 0;
  let host = '';
  try { host = new URL(entry.url).hostname; } catch {}
  for (const t of tries) {
    if (signal.aborted) throw Object.assign(new Error('aborted'), { aborted: true });
    try {
      const { res, url } = await fetchFollow(t.url, t.profile.headers, signal);
      if (res.ok && res.body) { entry.winner = { url, profile: t.profile }; return res; }
      lastStatus = res.status;
      console.error(`Media host ${host} answered ${res.status} (${res.headers.get('content-type') || 'no type'}) using "${t.profile.name}"`);
      if (res.body) res.body.cancel().catch(() => {});
    } catch (e) {
      if (signal.aborted) throw Object.assign(new Error('aborted'), { aborted: true });
      console.error(`Media host ${host} failed using "${t.profile.name}": ${e.message}`);
    }
  }
  throw Object.assign(new Error('media unavailable'), { status: lastStatus });
}

/* GET /api/download/<id>?probe=1 — the page asks first, so a failure is shown inside the
   page instead of the browser being sent to an error screen. */
async function handleYoutubeProbe(id, entry, res) {
  const dest = path.join(DOWNLOADS_DIR, id);
  const st = await fs.promises.stat(dest).catch(() => null);
  if (st && st.isFile()) return json(res, 200, { ok: true });
  let job = yt.getJob(id);
  if (job && job.state === 'error') { yt.forget(id); return json(res, 502, { error: job.error }); }
  if (job && job.state === 'done') { yt.forget(id); job = null; } // the finished file has since been cleared
  if (!job) job = yt.start(id, entry, dest, DOWNLOADS_DIR);
  return json(res, 202, { pending: true, pct: job.pct, label: job.label });
}

async function handleProbe(id, res) {
  const entry = files.get(id);
  if (!entry) return json(res, 410, { error: 'This download link has expired. Grab the video again.' });
  if (entry.yt) return handleYoutubeProbe(id, entry, res);
  const cached = await fs.promises.stat(path.join(DOWNLOADS_DIR, id)).catch(() => null);
  if (cached && cached.isFile()) return json(res, 200, { ok: true });

  const ac = new AbortController();
  res.on('close', () => { if (!res.writableFinished) ac.abort(); });
  try {
    const up = await openMedia(entry, ac.signal);
    if (up.body) up.body.cancel().catch(() => {});
    return json(res, 200, { ok: true });
  } catch (e) {
    if (e.aborted) return;
    return json(res, 502, { error: mediaFailMessage(e.status || 0) });
  }
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

  if (entry.yt) return plain(res, 409, 'This file is not ready yet. Go back to SnapIT and press Download again.');

  const ac = new AbortController();
  res.on('close', () => { if (!res.writableFinished) ac.abort(); });

  let up;
  try {
    up = await openMedia(entry, ac.signal);
  } catch (e) {
    if (e.aborted || ac.signal.aborted) return;
    return plain(res, 502, mediaFailMessage(e.status || 0));
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
  let query;
  try { const u = new URL(req.url, 'http://localhost'); pathName = u.pathname; query = u.searchParams; }
  catch { return plain(res, 400, 'Bad request'); }

  if (pathName === '/api/grab') {
    if (req.method !== 'POST') return json(res, 405, { error: 'Use POST.' });
    return handleGrab(req, res);
  }

  const dl = pathName.match(/^\/api\/download\/([a-f0-9]{24})$/);
  if (dl) {
    if (req.method !== 'GET') return plain(res, 405, 'Use GET.');
    if (query.get('probe') === '1') return handleProbe(dl[1], res);
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
yt.cleanup(DOWNLOADS_DIR);
sweep(true); // copies from an earlier run can no longer be reached, so clear them
setInterval(() => { sweep(false); yt.sweepJobs(); }, 5 * 60 * 1000).unref();

server.listen(PORT, () => {
  console.log(`SnapIT running at http://localhost:${PORT}`);
  yt.isReady().then((ok) => {
    console.log(ok ? 'YouTube: yt-dlp found.' : 'YouTube: yt-dlp NOT found, so YouTube uses the older method.');
    if (ok) yt.selfUpdate();
  });
  console.log(KEY_READY
    ? 'Download service key loaded.'
    : 'WARNING: no RAPIDAPI_KEY set in .env — the Grab button will report the service as not connected.');
});
