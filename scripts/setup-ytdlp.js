'use strict';
/* Downloads the yt-dlp program next to the server (runs automatically after `npm install`,
   which is also what Render does when it builds). A failure here never stops the build:
   YouTube then simply falls back to the older method. */
const https = require('https');
const fs = require('fs');
const path = require('path');

const ASSET = { win32: 'yt-dlp.exe', darwin: 'yt-dlp_macos' }[process.platform]
  || (process.arch === 'arm64' ? 'yt-dlp_linux_aarch64' : 'yt-dlp_linux');
const BIN_DIR = path.join(__dirname, '..', 'bin');
const TARGET = path.join(BIN_DIR, process.platform === 'win32' ? 'yt-dlp.exe' : 'yt-dlp');
const URL0 = `https://github.com/yt-dlp/yt-dlp/releases/latest/download/${ASSET}`;

function get(url, hops = 0) {
  return new Promise((resolve, reject) => {
    if (hops > 6) return reject(new Error('too many redirects'));
    https.get(url, { headers: { 'User-Agent': 'snapit-setup' } }, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        res.resume();
        return resolve(get(new URL(res.headers.location, url).href, hops + 1));
      }
      if (res.statusCode !== 200) { res.resume(); return reject(new Error('HTTP ' + res.statusCode)); }
      resolve(res);
    }).on('error', reject);
  });
}

(async () => {
  try {
    if (fs.existsSync(TARGET) && Date.now() - fs.statSync(TARGET).mtimeMs < 3 * 24 * 3600 * 1000) {
      console.log('yt-dlp is recent enough, keeping it.');
      return;
    }
    fs.mkdirSync(BIN_DIR, { recursive: true });
    console.log('Downloading yt-dlp (' + ASSET + ')...');
    const res = await get(URL0);
    const tmp = TARGET + '.download';
    await new Promise((resolve, reject) => {
      const out = fs.createWriteStream(tmp);
      res.pipe(out);
      out.on('finish', resolve);
      out.on('error', reject);
      res.on('error', reject);
    });
    if (fs.statSync(tmp).size < 1024 * 1024) throw new Error('download looks incomplete');
    fs.renameSync(tmp, TARGET);
    fs.chmodSync(TARGET, 0o755);
    console.log('yt-dlp installed.');
  } catch (e) {
    console.warn('Could not install yt-dlp (' + e.message + '). YouTube will use the older method.');
  }
})();
