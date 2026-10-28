# SnapIT release package

SnapIT keeps its original page and design, now built the way Toolora is: a static site served by a Cloudflare Worker, and a Python (FastAPI + yt-dlp) downloader on Render. The old Node server and the RapidAPI key are no longer used.

## Layout (same as Toolora)
```
backend/Dockerfile        Render image: Python 3.12, ffmpeg, Deno (for YouTube), yt-dlp, FastAPI
backend/main.py           the FastAPI service
backend/requirements.txt
public/                   the SnapIT page (index.html, style.css, script.js, favicon.svg, flow-background.jpg, robots.txt)
src/index.js              Cloudflare Worker: serves public/ and forwards /api/* to the backend
wrangler.toml
requirements.txt          same packages, for running the backend outside Docker
```
There is no `schema.sql` / D1 database because SnapIT has no accounts.

## How it works
1. Paste a link and press **Grab video** -> `POST /api/grab`. The backend checks the link (supported sites only), reads the video with yt-dlp (no download yet) and returns the title, thumbnail, duration and the available options (up to four MP4 qualities, plus MP3 audio).
2. Press **Download** on an option -> `GET /api/download/<id>?probe=1`. The backend downloads that option with the same Toolora logic (format ladder, YouTube client fallbacks, size cap) and answers `{"ok": true}` when the file is ready, or `{"error": "..."}` which the page shows under the button.
3. The page then points the browser at `GET /api/download/<id>`, which sends the prepared file as an attachment.

Prepared files are deleted after `FILE_TTL_MINUTES` (30 by default).
Snapchat links are read directly from Snapchat's public page (clean, watermark-free copy when available), as before.

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
| `FILE_TTL_MINUTES` | 30 | How long prepared files are kept |
| `CACHE_MAX_MB` | 1024 | Total disk used by prepared files |

3. Run a **single** instance (prepared files are kept in that instance's memory/disk). Use **Clear build cache & deploy** if Render offers it.
4. Check it: open `https://<your-service>.onrender.com/health` -> `{"ok":true}`.

## Cloudflare Worker
1. In `wrangler.toml` set `BACKEND_URL` to your Render service URL (no trailing slash).
2. Add the same secret you gave Render:
```powershell
npx.cmd wrangler secret put PROXY_SECRET
npx.cmd wrangler deploy
```
`public/script.js` already uses `API_BASE = ''`, so the page talks to the Worker on its own domain and no CORS setup is needed.

## Run everything locally
```bash
cd backend
pip install -r requirements.txt     # also install ffmpeg (and Deno for YouTube)
uvicorn main:app --reload
```
Open http://localhost:8000 . The backend serves `../public` when it sits next to it. Interactive API docs are at `/docs`.

## Notes
- Only download videos you own or have permission to download.
- The RapidAPI key from the old `.env` is not used anywhere now. If that file was ever shared or committed, revoke the key in your RapidAPI dashboard.
- Image-only posts (for example a Pinterest photo pin) are not offered, because yt-dlp handles video. Snapchat images still work.
