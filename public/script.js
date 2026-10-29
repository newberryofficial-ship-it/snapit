/* ============ SnapIT — plain JavaScript ============ */

/* Processing service: the Python (FastAPI) backend, reached through the SnapIT worker on this same site
   (POST /api/grab, GET /api/download/<id>). Leave API_BASE empty when the worker serves this page.
   Only if you host this page somewhere else, put the backend's address here (no trailing slash). */
const API_BASE = '';
const ENDPOINT = API_BASE + '/api/grab';

const PLATFORMS = [
  { id: 'youtube',   name: 'YouTube',   hosts: ['youtube.com', 'youtu.be'] },
  { id: 'pinterest', name: 'Pinterest', hosts: ['pinterest.com', 'pin.it'] },
  { id: 'snapchat',  name: 'Snapchat',  hosts: ['snapchat.com'] },
  { id: 'tiktok',    name: 'TikTok',    hosts: ['tiktok.com'] },
  { id: 'x',         name: 'X',         article: 'an', aria: 'X (formerly Twitter)', hosts: ['twitter.com', 'x.com', 't.co'] },
  { id: 'instagram', name: 'Instagram', article: 'an', hosts: ['instagram.com'] },
];

const LOGOS = {
  youtube: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M22 7s-.2-1.5-.8-2.1c-.8-.8-1.6-.8-2-.9C16.4 3.8 12 3.8 12 3.8s-4.4 0-7.2.2c-.4.1-1.2.1-2 .9C2.2 5.5 2 7 2 7s-.2 1.8-.2 3.5v1.6c0 1.8.2 3.5.2 3.5s.2 1.5.8 2.1c.8.8 1.8.8 2.3.9 1.6.2 6.9.2 6.9.2s4.4 0 7.2-.2c.4-.1 1.2-.1 2-.9.6-.6.8-2.1.8-2.1s.2-1.7.2-3.5v-1.6C22.2 8.8 22 7 22 7Z"/><path d="m10 8 6 3.5-6 3.5Z" class="logo-cutout"/></svg>',
  pinterest: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2a10 10 0 0 0-3.6 19.3c0-.8 0-1.8.2-2.7l1.3-5.4s-.3-.7-.3-1.7c0-1.6.9-2.7 2-2.7.9 0 1.3.7 1.3 1.5 0 .9-.6 2.4-.9 3.7-.3 1.1.5 2 1.6 2 1.9 0 3.3-2 3.3-5 0-2.6-1.9-4.4-4.5-4.4-3.1 0-4.9 2.3-4.9 4.7 0 .9.3 1.9.8 2.4l.2.6-.3 1c0 .2-.2.3-.5.2-1.3-.6-2.1-2.3-2.1-4.2 0-3.4 2.5-6.5 7.1-6.5 3.7 0 6.6 2.6 6.6 6.2 0 3.7-2.4 6.7-5.6 6.7-1.1 0-2.2-.6-2.6-1.3l-.7 2.7c-.2 1-.9 1.9-1.4 2.6A10 10 0 1 0 12 2Z"/></svg>',
  snapchat: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2c-3.5 0-5 2.7-5 5.8l.1 2c-.8.4-1.5-.7-2.2-.1-.9.9 1.4 1.7 1.6 2.1-.6 2.6-2.4 3.5-4 4.2-.7.4-.5.9.6 1.1l1.7.5.4 1.5c.2.5 1.5.1 2.4.4 1.4.4 2.5 1.5 4.4 1.5s3-1.1 4.4-1.5c.9-.3 2.2.1 2.4-.4l.4-1.5 1.7-.5c1.1-.2 1.3-.7.6-1.1-1.6-.7-3.4-1.6-4-4.2.2-.4 2.5-1.2 1.6-2.1-.7-.6-1.4.5-2.2.1l.1-2C17 4.7 15.5 2 12 2Z"/></svg>',
  tiktok: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M16.6 2c.4 2.6 1.8 4.2 4.4 4.4v3.4a9.3 9.3 0 0 1-4.4-1.2v7.3a6.3 6.3 0 1 1-5.5-6.2v3.5a2.9 2.9 0 1 0 2.1 2.7V2Z"/></svg>',
  x: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231 5.451-6.231Zm-1.161 17.52h1.833L7.084 4.126H5.117l11.966 15.644Z"/></svg>',
  instagram: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="5"/><circle cx="12" cy="12" r="4"/><circle cx="17.5" cy="6.5" r="1" fill="currentColor" stroke="none"/></svg>',
};

const STROKE = 'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"';
const ICONS = {
  download: `<svg viewBox="0 0 24 24" ${STROKE}><path d="M12 17V3"/><path d="m6 11 6 6 6-6"/><path d="M19 21H5"/></svg>`,
  close: `<svg viewBox="0 0 24 24" ${STROKE}><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>`,
  shield: `<svg viewBox="0 0 24 24" ${STROKE}><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/></svg>`,
  zap: `<svg viewBox="0 0 24 24" ${STROKE}><path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/></svg>`,
  headphones: `<svg viewBox="0 0 24 24" ${STROKE}><path d="M3 14h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-5Zm18 0h-3a2 2 0 0 0-2 2v3a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-5Z"/><path d="M21 14v-2a9 9 0 0 0-9-9 9 9 0 0 0-9 9v2"/></svg>`,
  arrowDown: `<svg viewBox="0 0 24 24" ${STROKE}><path d="M12 5v14"/><path d="m19 12-7 7-7-7"/></svg>`,
  check: `<svg viewBox="0 0 24 24" ${STROKE}><path d="M20 6 9 17l-5-5"/></svg>`,
  sparkles: `<svg viewBox="0 0 24 24" ${STROKE}><path d="m12 3 1.9 5.8a2 2 0 0 0 1.3 1.3L21 12l-5.8 1.9a2 2 0 0 0-1.3 1.3L12 21l-1.9-5.8a2 2 0 0 0-1.3-1.3L3 12l5.8-1.9a2 2 0 0 0 1.3-1.3Z"/></svg>`,
  arrowUpRight: `<svg viewBox="0 0 24 24" ${STROKE}><path d="M7 7h10v10"/><path d="M7 17 17 7"/></svg>`,
  loader: `<svg viewBox="0 0 24 24" ${STROKE}><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>`,
};

const form = document.getElementById('link-form');
const input = document.getElementById('link-input');
const boxLogo = document.getElementById('input-platform');
const picker = document.getElementById('platform-picker');
const status = document.getElementById('status');
const grabBtn = document.getElementById('grab-btn');
const grabIcon = document.getElementById('grab-icon');
const grabText = document.querySelector('.grab-text');
const clearBtn = document.getElementById('clear-btn');

let platform = 'youtube';
let loading = false;

function detectPlatform(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.username || url.password || url.port) return null;
    const hit = PLATFORMS.find(p => p.hosts.some(h => url.hostname === h || url.hostname.endsWith('.' + h)));
    return hit ? hit.id : null;
  } catch { return null; }
}

function renderBoxLogo() {
  boxLogo.innerHTML = `<span class="platform-logo logo-${platform}">${LOGOS[platform]}</span>`;
  const active = PLATFORMS.find(p => p.id === platform);
  input.placeholder = `Paste ${active.article || 'a'} ${active.name} video link here…`;
}

function renderPicker() {
  let glass = picker.querySelector('.platform-glass');
  if (!glass) { glass = document.createElement('span'); glass.className = 'platform-glass'; glass.setAttribute('aria-hidden', 'true'); picker.appendChild(glass); }
  picker.dataset.platform = platform;
  picker.querySelectorAll('button').forEach(b => b.remove());
  PLATFORMS.forEach(p => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'platform-button';
    b.title = p.aria || p.name;
    b.setAttribute('aria-label', p.aria || p.name);
    b.setAttribute('aria-pressed', String(p.id === platform));
    b.innerHTML = `<span class="platform-logo logo-${p.id}">${LOGOS[p.id]}</span>`;
    b.addEventListener('click', () => {
      if (loading) return;
      platform = p.id;
      renderBoxLogo();
      renderPicker();
      clearStatus();
      input.focus();
    });
    picker.appendChild(b);
  });
}

function clearStatus() { status.innerHTML = ''; }

function showError(message) {
  status.innerHTML = `<div class="result-message" role="alert">${ICONS.shield}<p>${message}</p></div>`;
  const dismiss = document.createElement('button');
  dismiss.type = 'button';
  dismiss.className = 'icon-button';
  dismiss.setAttribute('aria-label', 'Dismiss message');
  dismiss.innerHTML = ICONS.close;
  dismiss.addEventListener('click', clearStatus);
  status.querySelector('.result-message').appendChild(dismiss);
}

function showLoading() {
  status.innerHTML = `<div class="loading-status"><span class="loading-dots"><i></i><i></i><i></i></span>Finding your moment…</div>`;
}

/* Video titles come from other people's uploads, so never put them into HTML raw. */
const esc = v => String(v).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const DOWNLOAD_LABEL = `Download ${ICONS.arrowDown}`;

/* Starts a download in this same tab. The page first asks the server whether the file can
   be fetched (so a problem shows up here as a message instead of an error page), then the
   browser is pointed at the file. The server marks it as an attachment, so the browser
   saves it and the page stays exactly where it is — no new window or tab. */
function triggerDownload(href) {
  const a = document.createElement('a');
  a.href = href;
  a.rel = 'noopener';
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  setTimeout(() => a.remove(), 1000);
}

async function startDownload(btn, note, o) {
  if (btn.disabled) return;
  btn.disabled = true;
  btn.classList.remove('is-done');
  btn.classList.add('is-busy');
  btn.innerHTML = `<span class="spinner">${ICONS.loader}</span> Preparing`;
  note.hidden = true;
  const href = API_BASE + o.url;
  try {
    const res = await fetch(href + '?probe=1');
    let data = {};
    try { data = await res.json(); } catch {}
    if (!res.ok || data.error) throw new Error(data.error || 'The file could not be fetched. Try another option, or grab the link again.');
    triggerDownload(href + '?t=' + Date.now());
    btn.classList.remove('is-busy');
    btn.classList.add('is-done');
    btn.innerHTML = `Started ${ICONS.check}`;
    setTimeout(() => { if (btn.isConnected) { btn.classList.remove('is-done'); btn.innerHTML = DOWNLOAD_LABEL; } }, 2500);
  } catch (e) {
    btn.classList.remove('is-busy');
    btn.innerHTML = DOWNLOAD_LABEL;
    note.textContent = e instanceof TypeError ? 'Could not reach the SnapIT server. Check your connection and try again.' : e.message;
    note.hidden = false;
  } finally {
    btn.disabled = false;
  }
}

function fmtDuration(sec) {
  if (typeof sec !== 'number' || !isFinite(sec) || sec <= 0) return '';
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(s).padStart(2, '0');
}

/* Thumbnail + title card shown above the download buttons. */
function previewCard(info) {
  const card = document.createElement('div');
  card.className = 'preview-card';
  const media = document.createElement('div');
  media.className = 'preview-thumb';
  const thumb = typeof info.thumbnail === 'string' && info.thumbnail.startsWith('https://') ? info.thumbnail : '';
  if (thumb) {
    const img = document.createElement('img');
    img.alt = '';
    img.referrerPolicy = 'no-referrer'; // several video sites block images shown on other websites
    img.src = thumb;
    img.addEventListener('error', () => { img.remove(); media.classList.add('is-empty'); });
    media.appendChild(img);
  } else media.classList.add('is-empty');
  const fallback = document.createElement('span');
  fallback.className = 'preview-fallback';
  fallback.innerHTML = ICONS.download;
  media.appendChild(fallback);
  const d = fmtDuration(info.duration);
  if (d) { const t = document.createElement('span'); t.className = 'preview-time'; t.textContent = d; media.appendChild(t); }
  const text = document.createElement('div');
  text.className = 'preview-text';
  const title = document.createElement('strong');
  title.textContent = info.title || 'Your video';
  const plat = document.createElement('small');
  plat.textContent = info.platform || '';
  text.appendChild(title);
  text.appendChild(plat);
  card.appendChild(media);
  card.appendChild(text);
  return card;
}

function showResults(options, info) {
  info = info || {};
  clearStatus(); // remove the "Finding your moment…" loader before showing the list
  const wrap = document.createElement('div');
  wrap.className = 'results';
  wrap.innerHTML = `<div class="results-heading"><span>${ICONS.check} Your moment is ready</span><small>Quality depends on the original video</small></div>`;
  wrap.appendChild(previewCard(info));
  options.forEach((o, i) => {
    const row = document.createElement('div');
    row.className = 'download-row';
    row.style.animationDelay = (i * 0.07) + 's';
    const audio = String(o.format).toUpperCase() === 'MP3';
    row.innerHTML =
      `<div class="format-icon">${audio ? ICONS.headphones : ICONS.download}</div>` +
      `<div class="download-detail"><strong>${esc(o.label)} <span>${esc(o.format)}</span></strong><small title="${esc(o.filename)}">${esc(o.filename)}</small></div>`;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'glass-button';
    btn.innerHTML = DOWNLOAD_LABEL;
    const note = document.createElement('p');
    note.className = 'row-error';
    note.hidden = true;
    note.setAttribute('role', 'alert');
    btn.addEventListener('click', () => startDownload(btn, note, o));
    row.appendChild(btn);
    row.appendChild(note);
    wrap.appendChild(row);
  });
  status.appendChild(wrap);
}

function setLoading(state) {
  loading = state;
  form.classList.toggle('is-loading', state);
  input.disabled = state;
  grabBtn.disabled = state;
  clearBtn.style.visibility = state ? 'hidden' : 'visible';
  grabIcon.innerHTML = state ? `<span class="spinner">${ICONS.loader}</span>` : ICONS.download;
  grabText.textContent = state ? 'Grabbing' : 'Grab video';
  grabBtn.setAttribute('aria-label', state ? 'Preparing downloads' : 'Grab video');
}

function updateUrl(value) {
  input.value = value;
  clearStatus();
  const found = detectPlatform(value.trim());
  if (found && found !== platform) { platform = found; renderBoxLogo(); renderPicker(); }
}

/* X button: empties the link box and clears the list of grabbed downloads. */
function clearAll() {
  if (loading) return;
  input.value = '';
  clearStatus();
  input.focus();
}

async function submit(event) {
  event.preventDefault();
  if (loading) return;
  const value = input.value.trim();
  const found = detectPlatform(value);
  if (!found) {
    showError('Paste a valid HTTPS video link from a supported platform.');
    input.focus();
    return;
  }
  platform = found;
  renderBoxLogo();
  renderPicker();
  setLoading(true);
  showLoading();

  if (location.protocol === 'file:') {
    setLoading(false);
    showError('Open SnapIT from its hosted address (or a local web server), not by double-clicking index.html. The page cannot reach the backend from a file.');
    return;
  }

  if (!ENDPOINT) {
    setLoading(false);
    showError('No processing service is connected yet. Add your service address at the top of script.js to turn checked links into download files.');
    return;
  }

  try {
    const res = await fetch(ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: value }),
    });
    let data;
    try { data = await res.json(); }
    catch { data = { error: 'The SnapIT server did not respond properly. Try again in a moment.' }; }
    setLoading(false);
    if (data.error) showError(data.error);
    if (Array.isArray(data.options) && data.options.length) showResults(data.options, data);
    else if (!data.error) showError('Nothing came back for that link. Try another one.');
  } catch {
    setLoading(false);
    showError('Could not reach the SnapIT server. Check your connection, then try again.');
  }
}

/* Static icons in the page */
document.getElementById('brand-symbol').innerHTML = ICONS.download;

document.getElementById('grab-icon').innerHTML = ICONS.download;
clearBtn.innerHTML = ICONS.close;
document.getElementById('trust-1').innerHTML = ICONS.shield;
document.getElementById('trust-2').innerHTML = ICONS.zap;
document.getElementById('trust-3').innerHTML = ICONS.headphones;
document.getElementById('modal-spark').innerHTML = ICONS.sparkles;
document.getElementById('modal-check').innerHTML = ICONS.check;
document.getElementById('modal-close').innerHTML = ICONS.close;

/* About dialog */
const modal = document.getElementById('modal');
const openModal = () => { modal.hidden = false; };
const closeModal = () => { modal.hidden = true; };

document.getElementById('modal-close').addEventListener('click', closeModal);
document.getElementById('modal-done').addEventListener('click', closeModal);
modal.addEventListener('click', e => { if (e.target === modal) closeModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !modal.hidden) closeModal(); });

input.addEventListener('input', e => updateUrl(e.target.value));
form.addEventListener('submit', submit);
clearBtn.addEventListener('click', clearAll);

renderBoxLogo();
renderPicker();
setLoading(false);

/* Animated theme switch, without changing downloader behavior. */
const themeSwitch = document.getElementById('theme-switch');
const snapPage = document.querySelector('.snap-page');
let theme = 'light';
try { theme = localStorage.getItem('snapit-theme') === 'dark' ? 'dark' : 'light'; } catch {}
function applyTheme(next) {
 theme = next; snapPage.dataset.theme = next;
 themeSwitch.setAttribute('aria-checked', String(next === 'dark'));
 themeSwitch.title = next === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
 try { localStorage.setItem('snapit-theme', next); } catch {}
}
applyTheme(theme);
themeSwitch.addEventListener('click', async () => {
 if (themeSwitch.disabled) return;
 const next = theme === 'light' ? 'dark' : 'light';
 if (matchMedia('(prefers-reduced-motion: reduce)').matches) { applyTheme(next); return; }
 themeSwitch.disabled = true;
 if (document.startViewTransition) {
  try { await document.startViewTransition(() => applyTheme(next)).finished; } catch {}
 } else {
  snapPage.classList.add('theme-changing'); applyTheme(next);
  await new Promise(resolve => setTimeout(resolve, 1500)); snapPage.classList.remove('theme-changing');
 }
 themeSwitch.disabled = false;
});
