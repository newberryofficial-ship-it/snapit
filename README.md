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

### If YouTube says "Sign in to confirm you're not a bot"

YouTube often challenges servers in data centres such as Render's. Fix it with cookies from a signed-in session:

1. Use a throwaway Google account (never your main one).
2. Open a private/incognito window, sign in to YouTube, then open `https://www.youtube.com/robots.txt` in that same tab.
3. Export the youtube.com cookies as `cookies.txt` with a cookie-export extension (for example "Get cookies.txt LOCALLY"), then close the private window.
4. In Render: your service > Environment > Secret Files > add a file named `cookies.txt` with that content, then redeploy.

Never commit cookies.txt to git. The cookies expire after a while; repeat the steps when the error comes back.
A paid proxy (`YT_PROXY`) is the other option.
