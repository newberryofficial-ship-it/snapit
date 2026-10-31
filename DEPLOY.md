# SnapIT release package

SnapIT keeps its original page and design, now built the way Toolora is: a static site served by a Cloudflare Worker, and a Python (FastAPI + yt-dlp) downloader on Render. The old Node server is no longer used.

## Layout (same as Toolora)
```
backend/Dockerfile        Render image: Python 3.12, ffmpeg, yt-dlp, FastAPI
backend/main.py           the FastAPI service
backend/social.py         reads TikTok / Instagram / X posts (pictures + backup reader) and saves their media
backend/test_social.py    offline tests for social.py (pytest)
backend/requirements.txt
public/                   the SnapIT page (index.html, style.css, script.js, favicon.svg, flow-background.jpg, robots.txt)
src/index.js              Cloudflare Worker: serves public/ and forwards /api/* to the backend
wrangler.toml
requirements.txt          same packages, for running the backend outside Docker
```
There is no `schema.sql` / D1 database because SnapIT has no accounts.

## How it works
1. Paste a link and press **Grab video** -> `POST /api/grab`. The backend checks the link (supported sites only), reads the video (no download yet) and returns the title, thumbnail, duration and the available options (up to four MP4 qualities, plus MP3 audio). **YouTube links are read through RapidAPI**; every other site uses yt-dlp.
2. Press **Download** on an option -> `GET /api/download/<id>?probe=1`. The backend downloads that option (YouTube: RapidAPI links, fetched in parallel slices; other sites: yt-dlp format ladder; size cap in both) and answers `{"ok": true}` when the file is ready, or `{"error": "..."}` which the page shows under the button.
3. The page then points the browser at `GET /api/download/<id>`, which sends the prepared file as an attachment.

**Sound.** High-quality YouTube files are video-only, so the backend downloads the picture and the best audio track at the same time and joins them with ffmpeg (video is copied, not re-encoded). For yt-dlp sites every finished file is checked with ffprobe: if it came out silent (this happened with some Instagram posts) it is downloaded again with an explicit video+audio pair, and Opus/Vorbis audio is converted to AAC so phones play it.

**Speed.** Look-ups are cached for 10 minutes, a download reuses the look-up instead of reading the page again, yt-dlp fetches several fragments at once, YouTube files use 4 parallel connections, and yt-dlp's extractors are pre-loaded at start-up.

**Never asleep.** Render's free plan sleeps a service after ~15 minutes without traffic, which is why the first visit after a long time was slow. Three things now prevent that: the backend pings its own public address every 2 minutes, the Cloudflare Worker has a cron trigger that calls `/health` every 2 minutes (this also wakes it if it ever did sleep), and the page pings `/api/health` as soon as it opens. The page and the Worker also retry once or twice if the server is still booting. (Staying awake all month uses roughly 720-744 of Render's 750 free instance hours, which is fine for one service.)

Prepared files are deleted after `FILE_TTL_MINUTES` (30 by default).
Snapchat links are read directly from Snapchat's public page (clean, watermark-free copy when available), as before.

**Snapchat reliability (v3.1).** Snapchat sometimes blocks or rate-limits one kind of visitor, and its signed media addresses expire. So the backend now: reads the page up to 3 rounds with 3 kinds of visitor, also finds the video in the raw page text if Snapchat changes its JSON layout, remembers every working address (clean copy first, the page's own copy last) and tries each one at download time, rejects error pages that pretend to be a video, and if every address is stale it re-reads the Snapchat page for fresh ones before giving up.

**Play button.** `/api/grab` now also returns `preview`, the option the thumbnail's play button streams (Snapchat: the first video; other sites: the smallest video of at least 360p, so it is ready fast). Pressing play calls `/api/download/<id>?probe=1` to prepare the file, then plays `/api/download/<id>?inline=1` in a `<video>` on the page. It is the same prepared file the Download button uses, so no extra storage. Needs Starlette 0.39+ for seeking (Range requests); the unpinned `fastapi` install already provides it.

## Render backend
1. Create a **Web Service** from this repo, runtime **Docker**, root directory `backend`.
2. Environment variables (all optional):

| Variable | Default | Purpose |
|---|---|---|
| `PROXY_SECRET` | none | Shared secret. When set, only the Worker can use `/api/*`. Recommended. |
| `ALLOWED_ORIGINS` | `*` | Only matters if the page is hosted somewhere other than the Worker. |
| `MAX_FILESIZE_MB` | 200 | Per-download cap |
| `MAX_CONCURRENT` | 3 | Simultaneous downloads |
| `MAX_CONCURRENT_GRABS` | 6 | Simultaneous link look-ups |
| `RATE_PER_MINUTE` | 20 | Grab + prepare requests per visitor per minute (one video is about 2) |
| `RAPIDAPI_KEY` | none | **Required for YouTube.** Your RapidAPI key. Without it YouTube links show a clear "not switched on" message and everything else keeps working. |
| `RAPIDAPI_YT_HOST` | `youtube-media-downloader.p.rapidapi.com` | The RapidAPI host to call (change only if you subscribe to a different YouTube API) |
| `RAPIDAPI_YT_PATH` | `/v2/video/details` | Endpoint path on that host |
| `GRAB_CACHE_SECONDS` | 600 | How long a look-up is remembered |
| `DOWNLOAD_CONNECTIONS` | 4 | Parallel connections per YouTube file |
| `FRAGMENT_THREADS` | 4 | Parallel fragments in yt-dlp downloads |
| `KEEP_ALIVE_SECONDS` | 120 | Self-ping interval (0 turns it off) |
| `KEEP_ALIVE_URL` | `RENDER_EXTERNAL_URL` | Address to ping; Render fills the default in automatically |
| `FILE_TTL_MINUTES` | 30 | How long prepared files are kept |
| `CACHE_MAX_MB` | 1024 | Total disk used by prepared files |

3. Run a **single** instance (prepared files are kept in that instance's memory/disk). Use **Clear build cache & deploy** if Render offers it.
4. For YouTube: subscribe to the **YouTube Media Downloader** API on RapidAPI, copy your key from the RapidAPI dashboard and add it as `RAPIDAPI_KEY` in Render > Environment, then redeploy.
5. Check it: open `https://<your-service>.onrender.com/health` -> `{"ok":true}`.

## Cloudflare Worker
1. In `wrangler.toml` set `BACKEND_URL` to your Render service URL (no trailing slash). The `[triggers]` block there is the 2-minute wake-up cron; keep it.
2. Add the same secret you gave Render:
```powershell
npx.cmd wrangler secret put PROXY_SECRET
npx.cmd wrangler deploy
```
`public/script.js` already uses `API_BASE = ''`, so the page talks to the Worker on its own domain and no CORS setup is needed.

## Run everything locally
```bash
cd backend
pip install -r requirements.txt     # also install ffmpeg
uvicorn main:app --reload
```
Open http://localhost:8000 . The backend serves `../public` when it sits next to it. Interactive API docs are at `/docs`.

## Notes
- Only download videos you own or have permission to download.
- Keep `RAPIDAPI_KEY` only in Render's environment settings. Never put it in `script.js`, `wrangler.toml` or a committed file. If an old `.env` with a key was ever shared or committed, revoke that key in the RapidAPI dashboard and use a new one.
- Every YouTube grab costs one RapidAPI request (repeat grabs of the same link within 10 minutes are served from the cache and cost nothing). Watch your plan's monthly quota.
- Image-only posts (for example a Pinterest photo pin) are not offered, because yt-dlp handles video. Snapchat images still work.

## PWA (installable app)
SnapIT is now an installable Progressive Web App. New files in `public/`: `manifest.webmanifest`, `sw.js` (service worker), `offline.html`, `_headers`, and `icons/` (192/512 px, maskable, Apple touch icon). `index.html`, `style.css` and `script.js` were extended (manifest link, install button, service-worker registration, share target).
- **Installing:** Chrome/Edge/Android show an "Install app" button in the header; on iPhone use Share > Add to Home Screen.
- **Offline:** the page shell is cached so the app opens instantly; with no connection it shows a friendly offline message. Grabbing and downloading always need the internet, and `/api/*` is never cached by the service worker.
- **Share target:** on Android/desktop Chrome, sharing a link to SnapIT pre-fills the link box.
- **Updating:** when you change `style.css`, `script.js` or other cached files, bump `VERSION` in `sw.js` (and the `?v=9` in `index.html` + `SHELL_FILES` if you change those) so installed copies refresh.
- Needs HTTPS (the Cloudflare Worker domain already is). `_headers` makes `sw.js` revalidate on every load.

## Browser extension on the site
A **Get extension** button appears in the header (desktop Chrome/Edge/Brave/Opera only). It opens a dialog with the download (`public/extension/snapit-extension.zip`), install steps and this site's address with a Copy button. The extension's source and README are inside that zip.
- When you publish the extension to the Chrome Web Store / Edge Add-ons, set `EXTENSION_STORE_URL` near the end of `public/script.js`; the dialog then shows a one-click **Add to browser** button.
- If you change the extension, replace the zip with a new build and bump its `version` in `manifest.json`.

## v3.5 - pictures, Snapchat fallback, extra reliability layers
**Pictures.** TikTok photo posts, Instagram picture/carousel posts and X posts with photos are now offered instead of an error: one **ZIP** with every photo, the first six photos as single **JPG** files, and **Audio** (the post's sound) where there is one. Videos in the same post are listed too. Reading is done by `backend/social.py`.

**Layers (each one only runs if the one before it failed)**
| Site | Reading the post | Saving a video / audio |
|---|---|---|
| TikTok | own page reader (browser, then phone) -> TikTok embed page -> yt-dlp (now impersonating Chrome through `curl_cffi`) | addresses from the first reading -> post read again (fresh addresses + cookies) -> yt-dlp |
| Instagram | yt-dlp -> embed page JSON (carousels) -> embed picture -> link-preview picture | yt-dlp -> post read through social.py |
| X | yt-dlp -> X's embed endpoint -> `X_FALLBACK_API` (default api.fxtwitter.com; set empty to switch off) | yt-dlp -> post read through social.py |
| Snapchat | page as browser / phone / link-preview bot, 3 rounds, also the cleaned-up address (50 s budget) -> yt-dlp | original (no watermark) -> backup clean addresses -> Snapchat's own copy (may show a watermark) -> page read again for fresh addresses -> yt-dlp |

**Snapchat watermark fallback.** If the clean copy can't be fetched the standard copy is saved instead of failing, and the page tells the visitor ("...the standard copy was saved. It may show a watermark."). If checking for the clean copy itself crashes, the video is still offered.

**New env vars (optional):** `X_FALLBACK_API`, `YTDLP_IMPERSONATE` (default `chrome`, empty = off). `requirements.txt` now installs `yt-dlp[default,curl-cffi]`; redeploy the backend with **Clear build cache & deploy**, and make sure the Dockerfile copies `social.py` (it does).

**Limits to know about.** TikTok, Instagram and X change their pages and block data-centre addresses from time to time; the layers make failures much rarer but cannot make them impossible. Private posts are never reachable. Instagram sometimes shows only the first picture of a carousel to visitors who aren't logged in. Run the offline tests with `cd backend && pip install pytest && pytest -q`.
