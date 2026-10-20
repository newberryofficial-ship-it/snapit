'use strict';
/* SnapIT — YouTube
   Instead of handing out raw YouTube addresses (which YouTube refuses when the server
   asks for them the way a plain download does), this lets yt-dlp do what it is built for:
   read the video the way YouTube's own apps do, download the video and audio streams,
   and let ffmpeg join them into ONE finished MP4 (or an M4A for audio only).

   The page asks for the file, the work runs in the background (a few at a time), the
   page polls for progress, and when the file is ready the normal download route serves it. */

const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const EXE_LOCAL = path.join(__dirname, 'bin', process.platform === 'win32' ? 'yt-dlp.exe' : 'yt-dlp');
const MAX_HEIGHT = Number(process.env.YT_MAX_HEIGHT) || 1080;            // biggest quality offered
const MAX_MINUTES = Number(process.env.YT_MAX_MINUTES) || 30;           // longest video accepted
const MAX_JOBS = Math.max(1, Number(process.env.YT_MAX_JOBS) || 1);     // videos built at the same time
const JOB_TIMEOUT_MS = (Number(process.env.YT_JOB_MINUTES) || 10) * 60 * 1000;
const MAX_FILE_MB = Number(process.env.MAX_FILE_MB) || 500;

let FFMPEG = null;
try { const p = require('ffmpeg-static'); if (p && fs.existsSync(p)) FFMPEG = p; } catch { /* system ffmpeg, if any, is used instead */ }

/* YouTube treats each "player client" differently, and which one is accepted changes over
   time. Try yt-dlp's own default first, then a few others, and remember what worked. */
const LADDER = [
  [],
  ['--extractor-args', 'youtube:player_client=android_vr'],
  ['--extractor-args', 'youtube:player_client=tv'],
  ['--extractor-args', 'youtube:player_client=mweb,web_safari'],
];

function isYouTubeLink(link) {
  try {
    const h = new URL(link).hostname.toLowerCase();
    return h === 'youtu.be' || h === 'youtube.com' || h.endsWith('.youtube.com');
  } catch { return false; }
}

/* ---------- running yt-dlp ---------- */
function command() {
  if (process.env.YTDLP_PATH) return process.env.YTDLP_PATH;
  return fs.existsSync(EXE_LOCAL) ? EXE_LOCAL : 'yt-dlp';
}

function run(args, { timeoutMs = 120000, onLine } = {}) {
  return new Promise((resolve) => {
    let child;
    let out = '';
    let err = '';
    let buf = '';
    let timedOut = false;
    try { child = spawn(command(), args, { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true }); }
    catch (e) { return resolve({ code: -1, out, err: String(e.message), timedOut }); }
    const timer = setTimeout(() => { timedOut = true; child.kill('SIGKILL'); }, timeoutMs);
    const feed = (s) => {
      if (!onLine) return;
      buf += s;
      let i;
      while ((i = buf.indexOf('\n')) >= 0) { onLine(buf.slice(0, i).replace(/\r$/, '')); buf = buf.slice(i + 1); }
    };
    child.stdout.on('data', (d) => { const s = d.toString(); if (out.length < 30e6) out += s; feed(s); });
    child.stderr.on('data', (d) => { err = (err + d.toString()).slice(-6000); });
    child.on('error', (e) => { clearTimeout(timer); resolve({ code: -1, out, err: String(e.message), timedOut }); });
    child.on('close', (code) => { clearTimeout(timer); resolve({ code, out, err, timedOut }); });
  });
}

/* Recent yt-dlp versions need a JavaScript runtime to solve YouTube's puzzles; this server
   already has one (Node). If an older yt-dlp doesn't know the option, carry on without it. */
let jsRuntimeOk = true;
async function ytdlp(args, opts) {
  const withJs = () => (jsRuntimeOk ? ['--js-runtimes', `node:${process.execPath}`] : []);
  let r = await run([...withJs(), ...args], opts);
  if (r.code !== 0 && jsRuntimeOk && /no such option|unrecognized arguments/i.test(r.err)) {
    jsRuntimeOk = false;
    console.error('yt-dlp does not accept --js-runtimes; continuing without it.');
    r = await run(args, opts);
  }
  return r;
}

const baseArgs = () => [
  '--no-playlist', '--no-warnings', '--ignore-config', '--no-cache-dir',
  '--socket-timeout', '30', '--retries', '3',
  ...(FFMPEG ? ['--ffmpeg-location', FFMPEG] : []),
];

let readyState = { ok: null, at: 0 };
async function isReady() {
  if (readyState.ok === true) return true;
  if (readyState.ok === false && Date.now() - readyState.at < 60000) return false;
  const r = await run(['--version'], { timeoutMs: 15000 });
  readyState = { ok: r.code === 0, at: Date.now() };
  if (!readyState.ok) console.error('yt-dlp is not available:', (r.err || '').slice(0, 200));
  return readyState.ok;
}

/* Keep yt-dlp current: YouTube changes often and old versions stop working. */
function selfUpdate() {
  run(['-U'], { timeoutMs: 120000 }).then((r) => {
    const line = (r.out + r.err).split('\n').map((s) => s.trim()).filter(Boolean).pop() || '';
    console.log('yt-dlp update check:', line.slice(0, 160));
  });
}

/* ---------- understanding failures ---------- */
function classify(err) {
  const e = String(err || '');
  if (/private video|this video is private/i.test(e)) return { kind: 'final', message: 'This video is private, so it can\'t be downloaded.' };
  if (/members[- ]only|join this channel/i.test(e)) return { kind: 'final', message: 'This video is for channel members only.' };
  if (/live event will begin|premieres in|this live event/i.test(e)) return { kind: 'final', message: 'This video hasn\'t started yet. Try again once it has aired.' };
  if (/confirm your age|age[- ]restricted|inappropriate for some users/i.test(e)) return { kind: 'soft', message: 'This video is age-restricted and can\'t be downloaded without signing in.' };
  if (/video unavailable|has been removed|no longer available|does not exist|isn't available|is not available|terminated/i.test(e)) return { kind: 'soft', message: 'This video is unavailable (removed or blocked).' };
  if (/not a bot|sign in to confirm|http error 403|http error 429|too many requests/i.test(e)) return { kind: 'blocked', message: '' };
  return { kind: 'unknown', message: '' };
}

function problemError(problems) {
  const pick = problems.find((p) => p.kind === 'final') || problems.find((p) => p.kind === 'soft');
  const e = new Error('yt-dlp could not read the video');
  e.final = !!(pick && pick.kind === 'final');
  e.userMessage = pick ? pick.message : '';
  return e;
}

const tail = (s) => String(s || '').split('\n').map((x) => x.trim()).filter(Boolean).slice(-3).join(' | ').slice(0, 400);
const clientName = (extra) => (extra.length ? extra[1].replace('youtube:player_client=', '') : 'default');

/* ---------- /api/grab for YouTube ---------- */
const STANDARD = [1080, 720, 480, 360];

async function grab(link) {
  const started = Date.now();
  const problems = [];
  let found = null;
  for (const extra of LADDER) {
    if (problems.length && Date.now() - started > 55000) break; // keep the whole request well under a minute and a half
    const r = await ytdlp([...baseArgs(), ...extra, '--dump-single-json', '--skip-download', link], { timeoutMs: 45000 });
    if (r.code === 0) {
      try { const info = JSON.parse(r.out); if (info && typeof info === 'object') { found = { info, clientArgs: extra }; break; } } catch { /* try the next one */ }
    }
    const c = classify(r.err);
    problems.push(c);
    console.error(`yt-dlp info [${clientName(extra)}] failed (${c.kind}): ${tail(r.err) || (r.timedOut ? 'timed out' : 'no output')}`);
    if (c.kind === 'final') break;
  }
  if (!found) throw problemError(problems);

  const { info, clientArgs } = found;
  const fail = (message) => Object.assign(new Error(message), { final: true, userMessage: message });
  if (info.is_live || info.live_status === 'is_live') throw fail('Live streams can\'t be downloaded until they have finished.');
  const minutes = Number(info.duration) / 60;
  if (minutes > MAX_MINUTES) throw fail(`This video is longer than ${MAX_MINUTES} minutes, which is the limit here.`);

  let best = 0;
  for (const f of info.formats || []) {
    if (!f || !f.vcodec || f.vcodec === 'none') continue;
    const r = f.width && f.height ? Math.min(f.width, f.height) : (f.height || f.width || 0);
    if (r > best) best = r;
  }
  let offered = STANDARD.filter((v) => v <= MAX_HEIGHT && best >= v * 0.9);
  if (!offered.length) offered = [0]; // small video: just offer the best there is
  return { link, title: info.title || info.id || 'youtube', clientArgs, offered };
}

/* ---------- building the file ---------- */
const jobs = new Map(); // id -> { state, pct, label, error, created }
let running = 0;
const waiting = [];

function schedule(fn) {
  const go = () => { running++; fn().finally(() => { running--; const next = waiting.shift(); if (next) next(); }); };
  if (running < MAX_JOBS) go(); else waiting.push(go);
}

function pickOutput(dir) {
  let best = null;
  for (const n of fs.readdirSync(dir)) {
    if (n.startsWith('.') || /\.(part|ytdl|temp)$/i.test(n) || /\.f\d+\.[a-z0-9]+$/i.test(n)) continue;
    const p = path.join(dir, n);
    const st = fs.statSync(p);
    if (st.isFile() && st.size > 0 && (!best || st.size > best.size)) best = { p, size: st.size };
  }
  return best && best.p;
}

function clearDir(dir) {
  for (const n of fs.readdirSync(dir)) fs.rmSync(path.join(dir, n), { recursive: true, force: true });
}

async function runJob(id, entry, job, dest, dir) {
  const work = path.join(dir, '.work-' + id);
  const started = Date.now();
  const spec = entry.yt;
  job.state = 'running';
  job.label = 'Starting';
  job.pct = 1;
  try {
    fs.mkdirSync(work, { recursive: true });
    const order = [spec.clientArgs || [], ...LADDER].filter((a, i, all) => all.findIndex((b) => JSON.stringify(b) === JSON.stringify(a)) === i);
    const formatArgs = spec.audio
      ? ['-f', 'ba[ext=m4a]/ba/b']
      : ['-f', 'bv*+ba/b', '-S', `res:${spec.res || MAX_HEIGHT},ext:mp4:m4a`, '--merge-output-format', 'mp4'];
    const problems = [];
    let produced = null;
    let timedOut = false;

    for (const extra of order) {
      const left = JOB_TIMEOUT_MS - (Date.now() - started);
      if (left < 15000) break;
      clearDir(work);
      let stream = 0;
      const onLine = (line) => {
        if (/^\[download\] Destination:/.test(line)) stream++;
        else if (/^\[(Merger|VideoRemuxer|ExtractAudio|Fixup\w*)\]/.test(line)) { job.label = 'Merging'; job.pct = Math.max(job.pct, 95); }
        else if (line.startsWith('SNAPIT ')) {
          const m = line.match(/(\d+(?:\.\d+)?)%/);
          if (!m) return;
          const total = spec.audio ? 1 : 2; // video and audio arrive as two separate streams
          const overall = ((Math.max(stream, 1) - 1) * 100 + Number(m[1])) / total;
          job.pct = Math.max(job.pct, Math.min(94, Math.round(overall)));
          job.label = `Downloading ${job.pct}%`;
        }
      };
      const r = await ytdlp([
        ...baseArgs(), ...extra, ...formatArgs,
        '--max-filesize', `${MAX_FILE_MB}M`, '--http-chunk-size', '10M',
        '--newline', '--progress-template', 'download:SNAPIT %(progress._percent_str)s',
        '-o', path.join(work, 'out.%(ext)s'), spec.link,
      ], { timeoutMs: left, onLine });

      if (r.code === 0) { produced = pickOutput(work); if (produced) break; }
      if (r.timedOut) { timedOut = true; break; }
      const c = classify(r.err);
      problems.push(c);
      console.error(`yt-dlp download [${clientName(extra)}] failed (${c.kind}): ${tail(r.err) || 'no output'}`);
      if (/larger than max-filesize/i.test(r.err)) throw Object.assign(new Error('too big'), { userMessage: `This file is larger than ${MAX_FILE_MB} MB, which is the limit here. Choose a lower quality.` });
      if (c.kind === 'final') break;
      job.pct = 1;
      job.label = 'Retrying';
    }

    if (!produced) {
      if (timedOut) throw Object.assign(new Error('timeout'), { userMessage: 'This took too long and was stopped. Try a lower quality.' });
      throw problemError(problems);
    }

    fs.renameSync(produced, dest);
    const ext = path.extname(produced).slice(1).toLowerCase() || (spec.audio ? 'm4a' : 'mp4');
    entry.ext = ext;
    entry.filename = entry.filename.replace(/\.[^.]+$/, '') + '.' + ext;
    job.state = 'done';
    job.pct = 100;
    job.label = 'Ready';
  } catch (e) {
    console.error('YouTube job failed:', e.message);
    job.state = 'error';
    job.error = e.userMessage || 'YouTube did not allow this download right now. Try again in a few minutes, or choose another quality.';
  } finally {
    fs.rm(work, { recursive: true, force: true }, () => {});
  }
}

function start(id, entry, dest, dir) {
  const job = { state: 'queued', pct: 0, label: 'Queued', error: '', created: Date.now() };
  jobs.set(id, job);
  schedule(() => runJob(id, entry, job, dest, dir));
  return job;
}

const getJob = (id) => jobs.get(id);
const forget = (id) => { jobs.delete(id); };

function sweepJobs() {
  const now = Date.now();
  for (const [id, j] of jobs) if (j.state !== 'queued' && j.state !== 'running' && now - j.created > 60 * 60 * 1000) jobs.delete(id);
}

/* Leftovers of a build that was cut short by a restart. */
function cleanup(dir) {
  try { for (const n of fs.readdirSync(dir)) if (n.startsWith('.work-')) fs.rmSync(path.join(dir, n), { recursive: true, force: true }); } catch { /* nothing to clean */ }
}

module.exports = { isYouTubeLink, isReady, selfUpdate, grab, start, getJob, forget, sweepJobs, cleanup };
