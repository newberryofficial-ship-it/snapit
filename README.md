# SnapIT backend

Node 18+ server, no dependencies. Endpoints: `POST /api/grab`, `GET /api/download/<id>`.

## Deploy on Render
- Build command: `npm install`  |  Start command: `npm start`
- Environment: `RAPIDAPI_KEY`, `ALLOWED_ORIGIN` (your frontend origin, no trailing slash)
- Check it: open `/api/grab` in a browser; "Use POST." means it is running.

## Run locally
Copy `.env.example` to `.env`, fill it in, then `npm start`. Set `ALLOWED_ORIGIN` to
wherever you serve the frontend from while testing.


## YouTube

YouTube links are handled by **yt-dlp** and **ffmpeg** instead of the RapidAPI service. When you press
Download, the server reads the video the way YouTube's own apps do, downloads the video and audio, joins
them into one finished MP4 (or an M4A for audio only) and the page saves it. This takes up to a minute, and
the button shows progress while it works.

- `npm install` downloads yt-dlp into `./bin` automatically (that is also what Render runs on every deploy), and
  installs ffmpeg through the `ffmpeg-static` package.
- yt-dlp checks for updates every time the server starts. If YouTube stops working, redeploy on Render
  (Manual Deploy) so the newest yt-dlp is fetched, then look at the Logs for lines that start with `yt-dlp`.
- If yt-dlp isn't installed, YouTube falls back to the RapidAPI method.
