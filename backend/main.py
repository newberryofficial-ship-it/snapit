"""SnapIT backend (FastAPI + yt-dlp), built on the Toolora downloader.

  POST /api/grab                  {"url": "..."} -> the video's details and a list of download options
  GET  /api/download/<id>?probe=1 prepares the chosen option, then answers {"ok": true} (or {"error": "..."})
  GET  /api/download/<id>         returns the file as a real download (attachment)
  GET  /health                    {"ok": true}

How a download works (same logic as Toolora's /download):
  * the link must belong to a supported site (this also blocks SSRF to internal hosts)
  * YouTube is handled ONLY through RapidAPI (set RAPIDAPI_KEY); every other site goes through yt-dlp
  * yt-dlp fetches the video with a format ladder, then checks the file really has sound (and repairs it if not)
  * "video" options give an MP4 that fits the size cap, "audio" options give the sound only as an MP3
  * Snapchat links are read straight from Snapchat's public page (no watermark when the clean copy exists; if that
    copy can't be fetched the standard copy, which may show a watermark, is saved instead of failing)
  * TikTok / Instagram / X posts that hold PICTURES (or that yt-dlp can't read) are read by social.py instead, and the
    pictures are offered as JPG downloads (plus one ZIP of the whole carousel). If one way of reading a post fails
    the next one is tried; a failed yt-dlp download is retried through social.py.
SnapIT adds one step on top of Toolora: /api/grab looks the link up first so the page can show a preview
(title, thumbnail, available qualities) before anything is downloaded.

Run locally:  uvicorn main:app --reload      (also serves ../public at http://localhost:8000)
Env vars (all optional):
  ALLOWED_ORIGINS       comma-separated site origins, e.g. https://snapit.example.com  (default: *)
  MAX_FILESIZE_MB       per-download cap (default 200)
  MAX_CONCURRENT        simultaneous downloads (default 3)
  MAX_CONCURRENT_GRABS  simultaneous link look-ups (default 6)
  RATE_PER_MINUTE       grab + prepare requests per IP per minute (default 20; one video is about 2)
  PROXY_SECRET          shared secret sent by the SnapIT worker; when set, /api/* only accepts requests that carry it
  FILE_TTL_MINUTES      how long a prepared file stays available before it is deleted (default 30)
  CACHE_MAX_MB          total size of prepared files kept on disk (default 1024)
  DOWNLOAD_DIR          where prepared files are kept (default: <system temp>/snapit_downloads)
  RAPIDAPI_KEY          your RapidAPI key - required for YouTube links only
  RAPIDAPI_YT_HOST      the RapidAPI host to call (default: youtube-media-downloader.p.rapidapi.com)
  RAPIDAPI_YT_PATH      the endpoint path on that host (default: /v2/video/details)
  GRAB_CACHE_SECONDS    how long a look-up is remembered so repeat grabs are instant (default 600)
  DOWNLOAD_CONNECTIONS  parallel connections used for YouTube files (default 4)
  FRAGMENT_THREADS      parallel fragment downloads inside yt-dlp (default 4)
  KEEP_ALIVE_SECONDS    the service pings its own public address this often so Render never sleeps (default 120; 0 = off)
  KEEP_ALIVE_URL        address to ping (default: RENDER_EXTERNAL_URL, which Render sets automatically)
  X_FALLBACK_API        public API used as the last layer for X posts (default: https://api.fxtwitter.com; empty = off)
  YTDLP_IMPERSONATE     browser yt-dlp pretends to be for TikTok (default: chrome; empty = off). Needs curl_cffi.
"""
import asyncio, copy, hmac, ipaddress, json, os, re, secrets, shutil, subprocess, tempfile, threading, time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from itertools import islice
from urllib.parse import parse_qs, urljoin, urlparse

import requests
import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from social import MediaError, any_host, follow, read_post, safe_host, save_media, save_zip  # noqa: F401

try:  # lets yt-dlp look like a real browser to TikTok (needs the curl_cffi package)
    import curl_cffi  # noqa: F401
    from yt_dlp.networking.impersonate import ImpersonateTarget
    IMPERSONATE = ImpersonateTarget("chrome", None, None, None) if os.getenv("YTDLP_IMPERSONATE", "chrome").strip() else None
except Exception:
    IMPERSONATE = None

ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
MAX_MB = int(os.getenv("MAX_FILESIZE_MB", "200"))
RATE = int(os.getenv("RATE_PER_MINUTE", "20"))
PROXY_SECRET = os.getenv("PROXY_SECRET", "")
FILE_TTL = int(os.getenv("FILE_TTL_MINUTES", "30")) * 60
CACHE_MAX = int(os.getenv("CACHE_MAX_MB", "1024")) * 1024 * 1024
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR") or os.path.join(tempfile.gettempdir(), "snapit_downloads")
slots = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT", "3")))
grab_slots = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT_GRABS", "6")))
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "").strip()
RAPIDAPI_HOST = os.getenv("RAPIDAPI_YT_HOST", "youtube-media-downloader.p.rapidapi.com").strip()
RAPIDAPI_PATH = os.getenv("RAPIDAPI_YT_PATH", "/v2/video/details").strip()
INFO_TTL = int(os.getenv("GRAB_CACHE_SECONDS", "600"))
DL_WORKERS = max(1, int(os.getenv("DOWNLOAD_CONNECTIONS", "4")))
FRAGMENT_THREADS = max(1, int(os.getenv("FRAGMENT_THREADS", "4")))
KEEP_ALIVE_SECONDS = int(os.getenv("KEEP_ALIVE_SECONDS", "120"))
KEEP_ALIVE_URL = (os.getenv("KEEP_ALIVE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")

# Only these sites (and their subdomains) are accepted; this also blocks SSRF to internal hosts.
ALLOWED_HOSTS = ("youtube.com", "youtu.be", "pinterest.com", "pin.it", "snapchat.com",
                 "tiktok.com", "twitter.com", "x.com", "t.co", "instagram.com")
SHORT_HOSTS = ("t.co", "pin.it")  # short links: resolved here, and the final address is checked again
PLATFORMS = (("youtube.com", "YouTube"), ("youtu.be", "YouTube"), ("pinterest.com", "Pinterest"), ("pin.it", "Pinterest"),
             ("snapchat.com", "Snapchat"), ("tiktok.com", "TikTok"), ("twitter.com", "X"), ("x.com", "X"),
             ("t.co", "X"), ("instagram.com", "Instagram"))

BAD_LINK = "Paste a valid HTTPS video link from a supported platform."
MEDIA_MIME = {".mp3": "audio/mpeg", ".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
              ".mov": "video/quicktime", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
              ".webp": "image/webp", ".gif": "image/gif", ".zip": "application/zip",
              ".heic": "image/heic", ".avif": "image/avif"}

ID_RE = re.compile(r"^[a-f0-9]{24}$")


@asynccontextmanager
async def lifespan(_: FastAPI):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    for name in os.listdir(DOWNLOAD_DIR):  # files from an earlier run can no longer be reached, so clear them
        if ID_RE.match(name):
            shutil.rmtree(os.path.join(DOWNLOAD_DIR, name), ignore_errors=True)
    tasks = [asyncio.create_task(sweeper()), asyncio.create_task(keep_alive())]
    asyncio.create_task(asyncio.to_thread(warm_up))  # load yt-dlp's extractors now, not on the first visitor's request
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()


app = FastAPI(title="SnapIT backend", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],  # the frontend reads the filename from this header
)


@app.exception_handler(StarletteHTTPException)
async def http_error(_: Request, exc: StarletteHTTPException):
    # The SnapIT page reads {"error": "..."}; "detail" is kept so Toolora-style clients work too.
    msg = exc.detail if isinstance(exc.detail, str) and exc.detail else "Request failed."
    return JSONResponse({"error": msg, "detail": msg}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def bad_request(_: Request, __: RequestValidationError):
    return JSONResponse({"error": "Invalid request.", "detail": "Invalid request."}, status_code=400)


@app.exception_handler(Exception)
async def server_error(_: Request, exc: Exception):
    print("Unhandled error:", repr(exc))
    msg = "Something went wrong on the server. Try again."
    return JSONResponse({"error": msg, "detail": msg}, status_code=500)


hits: dict[str, deque] = defaultdict(deque)


def from_proxy(request: Request) -> bool:
    """True when the request carries the shared secret that only the SnapIT worker knows."""
    if not PROXY_SECRET:
        return False
    sent = request.headers.get("x-proxy-secret", "")
    return hmac.compare_digest(sent.encode(), PROXY_SECRET.encode())


def client_ip(request: Request) -> str:
    if from_proxy(request):
        return request.headers.get("x-client-ip") or "?"  # the real visitor, as seen by the worker
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


def check_rate(ip: str) -> None:
    now, q = time.time(), hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE:
        raise HTTPException(429, "Too many requests right now. Wait a minute and try again.")
    q.append(now)


def check_proxy(request: Request) -> None:
    if PROXY_SECRET and not from_proxy(request):
        raise HTTPException(403, "Use the SnapIT website to download.")


class Req(BaseModel):
    url: str


def host_allowed(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def valid_url(u: str) -> bool:
    if not isinstance(u, str) or len(u) > 2000:
        return False
    try:
        p = urlparse(u.strip())
        port = p.port
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return p.scheme == "https" and not p.username and not p.password and port in (None, 443) and host_allowed(host)


def platform_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for h, name in PLATFORMS:
        if host == h or host.endswith("." + h):
            return name
    return "Video"


def resolve_short(url: str) -> str:
    """Follow t.co / pin.it by hand so the real destination is checked against the allowed sites."""
    for _ in range(5):
        host = (urlparse(url).hostname or "").lower()
        if host not in SHORT_HOSTS and "/url_shortener/" not in urlparse(url).path:
            return clean_pin(url)
        r = requests.get(url, allow_redirects=False, stream=True, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; SnapIT)"})
        try:
            loc = r.headers.get("location")
            if not loc and r.status_code == 200:  # some short links answer with a "refresh" page instead of a redirect
                page = r.raw.read(30000, decode_content=True).decode("utf-8", "ignore")
                m = re.search(r"""url=['"]?(https?://[^'">\s]+)""", page, re.I)
                loc = m.group(1) if m else None
        finally:
            r.close()
        if not loc:
            break
        url = urljoin(url, loc)
        if not valid_url(url):
            raise ValueError("That short link doesn't lead to a supported site.")
    if (urlparse(url).hostname or "").lower() in SHORT_HOSTS or "/url_shortener/" in urlparse(url).path:
        raise ValueError("That short link could not be opened.")
    return clean_pin(url)


def clean_pin(url: str) -> str:
    """Pinterest share links end in /sent/?invite_code=...; yt-dlp only understands /pin/<id>/."""
    m = re.search(r"pinterest\.[a-z.]+/pin/(?:[^/?#]*--)?(\d+)", url)
    return f"https://www.pinterest.com/pin/{m.group(1)}/" if m else url


# ----------------------------------------------------------------------------------------------
# Small in-memory cache: a link that was looked up a moment ago is answered instantly
# ----------------------------------------------------------------------------------------------
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def cache_get(key: str):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < INFO_TTL:
            return hit[1]
        _cache.pop(key, None)
    return None


def cache_put(key: str, value) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), value)
        if len(_cache) > 300:  # oldest first
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:50]:
                _cache.pop(k, None)


def warm_up() -> None:
    """Import every yt-dlp extractor once at start-up (takes ~1s) so the first real request isn't slowed down."""
    try:
        yt_dlp.extractor.gen_extractor_classes()
    except Exception as e:
        print("Warm-up skipped:", repr(e))


def ping_self() -> None:
    try:
        requests.get(KEEP_ALIVE_URL + "/health", timeout=20, headers={"User-Agent": "SnapIT-keepalive"}).close()
    except requests.RequestException as e:
        print("Keep-alive ping failed:", repr(e))


async def keep_alive() -> None:
    """Render's free plan puts a service to sleep after ~15 minutes without traffic. Calling our own public
    address every couple of minutes counts as traffic. (The Cloudflare worker's cron also pings /health, which
    wakes the service even if it ever did fall asleep.)"""
    if not KEEP_ALIVE_URL or KEEP_ALIVE_SECONDS <= 0:
        return
    while True:
        await asyncio.sleep(KEEP_ALIVE_SECONDS)
        await asyncio.to_thread(ping_self)


# ----------------------------------------------------------------------------------------------
# yt-dlp download logic (every site except YouTube)
# ----------------------------------------------------------------------------------------------
SORT = ["res", "ext:mp4:m4a"]  # best resolution, preferring MP4/M4A so the result plays on phones
FRAGMENT = re.compile(r"\.f[\w-]+\.[A-Za-z0-9]+$")  # intermediate streams such as "name.f137.mp4"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
SAFE_AUDIO = {"aac", "mp3"}  # audio codecs every phone/player can play inside an MP4


def clear_folder(folder: str) -> None:
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except OSError:
                pass


def is_youtube(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be" or host.endswith(".youtu.be")


def attempts_for(mode: str, height: int | None = None) -> list[dict]:
    if mode == "audio":
        return [{
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        }]
    if height is None:
        return [{"format": "bv*+ba/b", "format_sort": SORT},
                {"format": "bv*[height<=1080]+ba/b[height<=1080]", "format_sort": SORT},
                {"format": "bv*[height<=720]+ba/b[height<=720]", "format_sort": SORT},
                {"format": "bv*[height<=480]+ba/b[height<=480]", "format_sort": SORT},
                {"format": "best"}]
    # the quality the visitor picked, then the same ladder below it
    caps = [height] + [h for h in (1080, 720, 480) if h < height]
    formats = [{"format": f"bv*[height<={h}]+ba/b[height<={h}]", "format_sort": SORT} for h in caps]
    formats.append({"format": "best"})
    return formats


def sound_attempts(height: int | None) -> list[dict]:
    """Used only when the first download came out silent: ask explicitly for a separate video + audio pair
    (Instagram and a few others offer video-only streams that look like complete files), then for a file
    that is known to carry both."""
    h = f"[height<={height}]" if height else ""
    return [{"format": f"bv*{h}[acodec=none]+ba/bv*{h}+ba", "format_sort": SORT},
            {"format": f"b{h}[acodec!=none][vcodec!=none]/b{h}[acodec!=none]", "format_sort": SORT}]


def pick_file(folder: str, mode: str) -> str | None:
    want = (".mp3",) if mode == "audio" else (".mp4", ".mkv", ".webm", ".mov")
    files = [f for f in os.listdir(folder) if f.lower().endswith(want) and not FRAGMENT.search(f)]
    if not files:
        return None
    return os.path.join(folder, max(files, key=lambda f: os.path.getsize(os.path.join(folder, f))))


def audio_streams(path: str) -> list[str] | None:
    """Codec names of the audio streams in a file ([] = silent). None when it can't be checked."""
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", path],
                           capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
        streams = json.loads(p.stdout or b"{}").get("streams", [])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return [str(s.get("codec_name")) for s in streams if s.get("codec_type") == "audio"]


def run_ffmpeg(args: list[str], timeout: int = 900) -> None:
    p = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                       capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + p.stderr.decode("utf-8", "ignore")[-300:])


def fix_audio_codec(path: str) -> None:
    """Some players stay silent on Opus/Vorbis audio inside an MP4. Re-encode only the audio to AAC (video is copied)."""
    if not path.lower().endswith((".mp4", ".mov")):
        return
    tmp = path + ".aac.mp4"
    try:
        run_ffmpeg(["-i", path, "-map", "0:v?", "-map", "0:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", "-f", "mp4", tmp])
        os.replace(tmp, path)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        try:
            os.remove(tmp)
        except OSError:
            pass


def known_silent(info: dict | None) -> bool:
    fmts = (info or {}).get("formats") or []
    return bool(fmts) and all(f.get("acodec") == "none" for f in fmts)


def impersonation_for(url: str) -> dict:
    host = (urlparse(url).hostname or "").lower()
    return {"impersonate": IMPERSONATE} if IMPERSONATE and (host == "tiktok.com" or host.endswith(".tiktok.com")) else {}


def run_ydl(url: str, opts: dict, state: dict) -> None:
    try:
        _run_ydl(url, opts, state)
    except yt_dlp.utils.DownloadError as e:
        if "impersonate" in opts and "imperson" in str(e).lower():  # the browser copy isn't available here: go without it
            _run_ydl(url, {k: v for k, v in opts.items() if k != "impersonate"}, state)
        else:
            raise


def _run_ydl(url: str, opts: dict, state: dict) -> None:
    """Download with yt-dlp. When the look-up result is still fresh it is reused, which skips a second round of
    page requests and signature work (the slowest part of preparing a short video)."""
    info = state.get("info")
    with yt_dlp.YoutubeDL(opts) as ydl:
        if info is not None:
            try:
                ydl.process_ie_result(copy.deepcopy(info), download=True)
                return
            except yt_dlp.utils.DownloadError as e:
                if "requested format" in str(e).lower():
                    raise  # a real "no such format" answer: let the ladder try the next rung with the same data
                state["info"] = None  # probably stale links: start fresh
            except Exception:
                state["info"] = None
        ydl.download([url])


def ensure_sound(url: str, folder: str, path: str, height: int | None, base: dict, state: dict) -> str:
    """Make sure the finished video really has sound, in a codec phones can play."""
    audio = audio_streams(path)
    if audio is None:  # ffprobe unavailable: nothing to check with
        return path
    if audio:
        if not (set(audio) & SAFE_AUDIO):
            fix_audio_codec(path)
        return path
    if known_silent(state.get("info")):
        return path  # the post genuinely has no audio track
    alt = os.path.join(folder, "alt")
    for extra in sound_attempts(height):
        shutil.rmtree(alt, ignore_errors=True)
        os.makedirs(alt, exist_ok=True)
        try:
            run_ydl(url, {**base, "outtmpl": os.path.join(alt, "%(title).80B [%(id)s].%(ext)s"), **extra}, state)
        except Exception:
            continue
        cand = pick_file(alt, "video")
        if cand and audio_streams(cand):
            final = os.path.join(folder, os.path.basename(cand))
            try:
                os.remove(path)
            except OSError:
                pass
            os.replace(cand, final)
            shutil.rmtree(alt, ignore_errors=True)
            if not (set(audio_streams(final) or []) & SAFE_AUDIO):
                fix_audio_codec(final)
            return final
    shutil.rmtree(alt, ignore_errors=True)
    return path  # still silent: the original has none, so deliver what we have


def fetch(url: str, folder: str, mode: str = "video", height: int | None = None, info: dict | None = None) -> str:
    if is_youtube(url):
        raise RuntimeError("YouTube links are fetched through RapidAPI, not yt-dlp.")
    base = {
        "outtmpl": os.path.join(folder, "%(title).80B [%(id)s].%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "restrictfilenames": True,
        "max_filesize": MAX_MB * 1024 * 1024,
        "socket_timeout": 20,
        "quiet": True,
        "noprogress": True,
        "no_warnings": False,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": FRAGMENT_THREADS,  # HLS/DASH videos download several pieces at once
        **impersonation_for(url),
    }
    state = {"info": info if info and info.get("formats") else None}
    last = None
    for extra in attempts_for(mode, height):
        clear_folder(folder)
        try:
            run_ydl(url, {**base, **extra}, state)
        except yt_dlp.utils.DownloadError as e:
            if mode == "audio" or "requested format" not in str(e).lower():
                raise
            last = e
            continue
        path = pick_file(folder, mode)
        if path:
            return ensure_sound(url, folder, path, height, base, state) if mode == "video" else path
    if last:
        raise last
    raise RuntimeError("no file produced (it may exceed the size limit)")


def first_entry(info: dict) -> dict | None:
    """yt-dlp answers a post with several videos (X, Instagram) as a playlist; the first one is previewed."""
    if info.get("entries") is not None:
        return next((e for e in islice(info["entries"], 20) if e), None)
    return info


def has_media(entry: dict | None) -> bool:
    return bool(entry and (entry.get("formats") or entry.get("url")))


def lookup(url: str) -> dict:
    """Read the video's details without downloading it. Answers from the cache when the same link was
    looked up in the last few minutes."""
    hit = cache_get("info:" + url)
    if hit is not None:
        return hit
    base = {
        "noplaylist": True,
        "lazy_playlist": True,  # a post with many videos: only read the ones we need
        "socket_timeout": 20,
        "quiet": True,
        "no_warnings": False,
        "retries": 2,
        "skip_download": True,
        "ignore_no_formats_error": True,
        **impersonation_for(url),
    }
    try:
        try:
            with yt_dlp.YoutubeDL(base) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as e:
            if "impersonate" not in base or "imperson" not in str(e).lower():
                raise
            with yt_dlp.YoutubeDL({k: v for k, v in base.items() if k != "impersonate"}) as ydl:
                info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        if "unsupported url" in str(e).lower():
            raise
        # Let the download step decide; it reports a real error if the video truly can't be fetched.
        return {"title": None, "formats": [], "fallback": True}
    entry = first_entry(info) if info else None
    if not entry:
        return {"title": None, "formats": [], "fallback": True}
    if has_media(entry):
        entry.pop("requested_formats", None)  # stale selection data; the download picks its own formats
        entry.pop("requested_downloads", None)
        cache_put("info:" + url, entry)
    return entry


# ----------------------------------------------------------------------------------------------
# Snapchat: read the media straight from Snapchat's own servers
# ----------------------------------------------------------------------------------------------
# Snapchat's web page embeds its data as JSON (__NEXT_DATA__). The "contentUrl" / "mediaUrl" it lists is the
# *sharing* render (variant .27) with the "Snapchat @user" watermark burned in. The same media id also exists as
# the clean original (variant .1034), so that is tried first, falling back to the address the page gave us.
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
BOT_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
SNAP_MEDIA_RE = re.compile(r"^/([A-Za-z0-9]+)/([A-Za-z0-9_-]+)\.(\d+)\.([A-Za-z0-9]+)$")
SNAP_ORIGINAL = "Original · no watermark"
SNAP_STANDARD = "Standard · may show watermark"
SNAP_FAIL = "This Snapchat video could not be read. It may be private, removed or not public."


def is_snap_host(h: str) -> bool:
    return h == "snapchat.com" or h.endswith(".snapchat.com")


def is_snap_cdn(h: str) -> bool:
    return h.endswith(".sc-cdn.net") or h.endswith(".snapchat.com")


def clean_candidates(media_url: str) -> list[str]:
    u = urlparse(media_url)
    m = SNAP_MEDIA_RE.match(u.path)
    if not m or m.group(3) == "1034":
        return []
    d, mid, _, tail = m.groups()
    out: list[str] = []

    def add(host: str, folder: str, query: str = "") -> None:
        x = f"https://{host}/{folder}/{mid}.1034.{tail}{query}"
        if x not in out:
            out.append(x)

    add(u.hostname, d)
    add(u.hostname, d, "?" + u.query if u.query else "")  # some copies need the signed query string the page gave us
    add("cf-st.sc-cdn.net", "d")
    add("bolt-gcdn.sc-cdn.net", d)
    add("bolt-gcdn.sc-cdn.net", "v")
    add("bolt-gcdn.sc-cdn.net", "x")
    return out


def snap_reachable(u: str) -> bool:
    try:
        r = follow(u, {"User-Agent": BROWSER_UA, "Referer": "https://www.snapchat.com/", "Range": "bytes=0-0"}, is_snap_cdn, 10)
    except (MediaError, requests.RequestException):
        return False
    try:
        return r.status_code in (200, 206) and bool(re.search(r"video|octet|binary|mp4", r.headers.get("content-type", ""), re.I))
    finally:
        r.close()


def working_urls(urls: list[str]) -> list[str]:
    """Checks every address at the same time and returns the ones that answer, in order of preference."""
    if not urls:
        return []
    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        ok = list(pool.map(snap_reachable, urls))
    return [u for u, good in zip(urls, ok) if good]


def resolve_snap_video(media_url: str) -> tuple[str, str, list[str]]:
    """-> (best address, quality label, fallback addresses to try if the best one fails at download time)."""
    m = SNAP_MEDIA_RE.match(urlparse(media_url).path)
    if m and m.group(3) == "1034":
        return media_url, SNAP_ORIGINAL, []
    good = working_urls(clean_candidates(media_url))
    if good:
        return good[0], SNAP_ORIGINAL, good[1:] + [media_url]  # the page's own (watermarked) copy is the last resort
    return media_url, SNAP_STANDARD, []


def decode_entities(v: str) -> str:
    return (v.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'").replace("&#039;", "'")
            .replace("&#x27;", "'").replace("&lt;", "<").replace("&gt;", ">"))


def looks_like_video_url(v: str) -> bool:
    return bool(re.match(r"^https?://", v, re.I) and (re.search(r"\.sc-cdn\.net/", v, re.I) or re.search(r"\.(mp4|mov|m4v|webm)(\?|$)", v, re.I)))


SNAP_DEEP_RE = re.compile(r"^https?://[^\s?]*sc-cdn\.net/[^\s?]*\.\d+\.(?:mp4|mov|m4v)(\?|$)", re.I)
SNAP_RAW_RE = re.compile(r"https?://[A-Za-z0-9.-]*sc-cdn\.net/[A-Za-z0-9/_.\-]*\.\d+\.(?:mp4|mov|m4v)(?:\?[^\s\"'<>\\]*)?", re.I)


def deep_scan_snap(node, push, depth: int = 0) -> None:
    if node is None or depth > 14:
        return
    if isinstance(node, str):
        if SNAP_DEEP_RE.match(node):
            push(node)
    elif isinstance(node, list):
        for x in node:
            deep_scan_snap(x, push, depth + 1)
    elif isinstance(node, dict):
        for x in node.values():
            deep_scan_snap(x, push, depth + 1)


def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def extract_snap_media(html: str, link: str) -> tuple[list[tuple[str, str]], str]:
    """Pulls every snap out of one copy of the Snapchat page -> ([(url, kind)], title)."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def push(url, kind: str = "video") -> None:
        if not isinstance(url, str) or not re.match(r"^https?://", url, re.I):
            return
        m = SNAP_MEDIA_RE.match(urlparse(url).path)
        key = m.group(2) if m else url  # same media id = same snap, whichever render it is
        if key in seen:
            return
        seen.add(key)
        found.append((url, kind))

    title = ""
    root, pp = None, None
    mt = re.search(r"""<script[^>]*\bid=["']__NEXT_DATA__["'][^>]*>([\s\S]*?)</script>""", html)
    if mt:
        try:
            root = json.loads(mt.group(1))
            pp = _d(_d(_d(root).get("props")).get("pageProps"))
        except ValueError:
            root, pp = None, None

    if pp is not None:
        vm = _d(pp.get("videoMetadata"))
        if vm:  # Spotlight page: the snap the link points at
            push(vm.get("contentUrl"))
        # Public story / profile pages: every snap in the story
        lists = [_d(pp.get("story")).get("snapList")]
        lists += [_d(h).get("snapList") for h in (pp.get("curatedHighlights") or [])]
        lists += [_d(h).get("snapList") for h in (pp.get("spotlightHighlights") or [])]
        if not found:
            for lst in lists:
                for s in lst or []:
                    s = _d(s)
                    push(_d(s.get("snapUrls")).get("mediaUrl"), "image" if s.get("snapMediaType") == 0 else "video")
        if not found and pp.get("spotlightFeed"):
            stories = _d(pp["spotlightFeed"]).get("spotlightStories") or []
            if stories:
                push(_d(_d(_d(stories[0]).get("metadata")).get("videoMetadata")).get("contentUrl"))
        title = str(vm.get("name") or vm.get("description")
                    or _d(_d(pp.get("userProfile")).get("publicProfileInfo")).get("username") or "")

    # The page layout changes now and then: if the known places are empty, scan all of the page data.
    if not found and root is not None:
        deep: list[str] = []
        seen_deep: set[str] = set()

        def collect(u: str) -> None:
            if u not in seen_deep:
                seen_deep.add(u)
                deep.append(u)

        deep_scan_snap(root, collect)
        limit = 1 if re.search(r"/spotlight/", link, re.I) else 10
        for u in deep[:limit]:
            push(u)

    # Last resort: the link-preview tags and structured data every public page carries.
    if not found:
        for tag in re.findall(r"<meta\b[^>]*>", html, re.I):
            pm = re.search(r"""\b(?:property|name)=["']([^"']+)["']""", tag, re.I)
            cm = re.search(r"""\bcontent=["']([^"']*)["']""", tag, re.I)
            if not pm or not cm:
                continue
            prop, content = pm.group(1), cm.group(1)
            if re.match(r"^(og:video(:secure_url|:url)?|twitter:player:stream)$", prop, re.I):
                v = decode_entities(content)
                if looks_like_video_url(v):
                    push(v)
            elif not title and re.match(r"^og:title$", prop, re.I):
                title = decode_entities(content)
        for m in re.finditer(r'"contentUrl"\s*:\s*"((?:[^"\\]|\\.)*)"', html):
            try:
                v = json.loads('"' + m.group(1) + '"')
            except ValueError:
                continue
            if looks_like_video_url(v):
                push(v)

    # Still nothing: the page text itself may hold the address (escaped inside a script the JSON route missed).
    if not found:
        raw = html.replace("\\u002F", "/").replace("\\/", "/").replace("&amp;", "&")
        limit = 1 if re.search(r"/spotlight/", link, re.I) else 10
        for m in SNAP_RAW_RE.finditer(raw):
            push(m.group(0))
            if len(found) >= limit:
                break
    return found, title


def snap_thumbnail(html: str) -> str | None:
    """The cover picture: og:image / twitter:image (either attribute order), else the page's structured data."""
    for tag in re.findall(r"<meta\b[^>]*>", html, re.I):
        pm = re.search(r"""\b(?:property|name)=["']([^"']+)["']""", tag, re.I)
        cm = re.search(r"""\bcontent=["']([^"']*)["']""", tag, re.I)
        if pm and cm and re.match(r"^(og:image(:secure_url)?|twitter:image(:src)?)$", pm.group(1), re.I):
            v = decode_entities(cm.group(1))
            if v.startswith("https://"):
                return v
    m = re.search(r'"thumbnailUrl"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    if m:
        try:
            v = json.loads('"' + m.group(1) + '"')
        except ValueError:
            return None
        if isinstance(v, str) and v.startswith("https://"):
            return v
    return None


def snap_link_variants(link: str) -> list[str]:
    """The link as given, then a cleaned copy (no tracking query) / the canonical Spotlight address."""
    p = urlparse(link)
    out = [link]
    if p.query or p.fragment:
        out.append(f"https://{p.netloc}{p.path}")
    m = re.search(r"/spotlight/([A-Za-z0-9_-]+)", p.path)
    if m:
        out.append(f"https://www.snapchat.com/spotlight/{m.group(1)}")
    return list(dict.fromkeys(out))[:2]


def snap_read_once(link: str, ua: str):
    """One visit to the Snapchat page -> ("ok", (found, title, thumbnail)) | ("gone", None) | ("none", None)."""
    try:
        r = follow(link, {"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}, is_snap_host, 20)
    except (MediaError, requests.RequestException):
        return "none", None
    try:
        if r.status_code in (404, 410):
            return "gone", None
        if not r.ok:
            return "none", None
        html = r.raw.read(6_000_000, decode_content=True).decode(r.encoding or "utf-8", "ignore")
    except (requests.RequestException, OSError):
        return "none", None
    finally:
        r.close()
    found, title = extract_snap_media(html, link)
    return ("ok", (found, title, snap_thumbnail(html))) if found else ("none", None)


def snap_page(link: str):
    # Snapchat sometimes answers one kind of visitor with a block, a rate limit or a bare page, and the answer can
    # differ from one minute to the next. So: three kinds of visitor, and if none worked wait a moment, also try the
    # cleaned-up address, and go round again - within a time budget so a visitor never waits forever.
    variants = snap_link_variants(link)
    deadline = time.time() + 50
    for attempt in range(3):
        if attempt:
            time.sleep(1.5 * attempt)
        for v in (variants[:1] if attempt == 0 else variants):
            for ua in (BROWSER_UA, MOBILE_UA, BOT_UA):
                if time.time() > deadline:
                    return None
                state, got = snap_read_once(v, ua)
                if state == "ok":
                    return got
                if state == "gone":
                    return None
    return None


def grab_snapchat(link: str) -> dict | None:
    page = snap_page(link)
    if not page:
        return None
    found, title, snap_thumb = page
    thumb = snap_thumb if snap_thumb and snap_thumb.startswith("https://") else None
    medias = []
    for url, kind in found[:10]:
        if kind == "image":
            medias.append({"url": url, "kind": "image", "ext": "jpg", "quality": "", "alts": []})
            continue
        page_url = url  # Snapchat's own copy (this is the one that may carry the watermark)
        try:
            url, quality, alts = resolve_snap_video(page_url)
        except Exception as e:  # looking for the clean copy is a bonus: it must never lose the video we already have
            print("Snapchat clean-copy check failed:", repr(e))
            url, quality, alts = page_url, SNAP_STANDARD, []
        medias.append({"url": url, "kind": "video", "ext": "mp4", "quality": quality, "alts": alts, "std": page_url})
    medias = [m for m in medias if is_snap_cdn((urlparse(m["url"]).hostname or "").lower())]
    return {"title": title or "snapchat", "medias": medias, "thumbnail": thumb} if medias else None


def grab_snapchat_cached(link: str) -> dict | None:
    hit = cache_get("snap:" + link)
    if hit is not None:
        return hit
    snap = grab_snapchat(link)
    if snap:
        cache_put("snap:" + link, snap)
    return snap


def media_fail_message(status: int) -> str:
    if status == 403:
        return "The video host refused the download (403). Try another option, or grab the link again."
    if status in (404, 410):
        return "This file is no longer available from the video host. Grab the link again."
    if status == 429:
        return "The video host is limiting requests right now. Wait a minute and try again."
    if status >= 500:
        return f"The video host had a problem ({status}). Try again in a moment."
    return "The file could not be fetched. Try another option, or grab the link again."


SNAP_HEADERS = ({"User-Agent": BROWSER_UA, "Referer": "https://www.snapchat.com/", "Accept": "*/*"},
                {"User-Agent": MOBILE_UA, "Accept": "*/*"})  # second try: look like a phone, no referrer
SNAP_WATERMARK_NOTE = "The original copy (no watermark) wasn't available right now, so the standard copy was saved. It may show a watermark."


def _save_snap(url: str, item: "Item", folder: str, headers: dict | None = None) -> str:
    """Save one Snapchat media address. Raises MediaError when it is refused, expired or not really a video."""
    try:
        r = follow(url, headers or SNAP_HEADERS[0], is_snap_cdn)
    except requests.RequestException:
        raise MediaError(502, media_fail_message(0))
    path = os.path.join(folder, f"{item.id}.{item.ext}")
    try:
        if not r.ok:
            raise MediaError(r.status_code, media_fail_message(r.status_code))
        kind = (item.direct or {}).get("kind")
        ctype = r.headers.get("content-type", "").lower()
        if ctype.startswith(("text/", "application/json")) or (kind == "video" and ctype.startswith("image/")):
            raise MediaError(502, media_fail_message(0))  # an error page, not the video
        cap = MAX_MB * 1024 * 1024
        if int(r.headers.get("content-length") or 0) > cap:
            raise MediaError(413, f"That file is larger than the {MAX_MB} MB limit.")
        size = 0
        try:
            with open(path + ".part", "wb") as out:
                for chunk in r.iter_content(262144):
                    size += len(chunk)
                    if size > cap:
                        raise MediaError(413, f"That file is larger than the {MAX_MB} MB limit.")
                    out.write(chunk)
        except requests.RequestException:
            raise MediaError(502, media_fail_message(0))
        if size < 1024:  # an empty or truncated answer
            raise MediaError(502, media_fail_message(0))
        os.replace(path + ".part", path)
        return path
    finally:
        r.close()
        if os.path.exists(path + ".part"):
            try:
                os.remove(path + ".part")
            except OSError:
                pass


def fetch_direct(item: "Item", folder: str) -> str:
    """Save one Snapchat media file straight from Snapchat's servers.
    Every known address is tried in turn. If they all fail (the signed addresses expire), the Snapchat page is read
    again for fresh ones and those are tried too, so a stale look-up no longer ends in a failed download."""
    d = item.direct or {}
    urls = [d["url"]] + [u for u in d.get("alts", []) if u != d["url"]]
    if d.get("std") and d["std"] not in urls:
        urls.append(d["std"])  # the standard copy is always the last resort
    item.note = None
    last: MediaError | None = None

    def attempt(candidates: list[str]) -> str | None:
        nonlocal last
        for u in candidates:
            for headers in SNAP_HEADERS:
                try:
                    path = _save_snap(u, item, folder, headers)
                except MediaError as e:
                    if e.status == 413:
                        raise
                    last = e
                    if e.status in (404, 410):
                        break  # this address is gone: other headers won't bring it back
                    continue
                # the clean copy was promised but the page's own copy is what we got: say so
                if d.get("quality") == SNAP_ORIGINAL and u == d.get("std") and u != d["url"]:
                    item.note = SNAP_WATERMARK_NOTE
                return path
        return None

    got = attempt(urls)
    if got:
        return got
    if is_snap_host((urlparse(item.source).hostname or "").lower()):
        cache_put("snap:" + item.source, None)  # forget the stale look-up
        fresh = grab_snapchat(item.source)
        if fresh:
            cache_put("snap:" + item.source, fresh)
            mid = (SNAP_MEDIA_RE.match(urlparse(d["url"]).path) or [None, None, None])[2]
            pool = [m for m in fresh["medias"] if m["kind"] == d.get("kind")]
            same = [m for m in pool if mid and any(mid in u for u in [m["url"], *m.get("alts", [])])] or pool[:1]
            for m in same:
                d = {**d, "url": m["url"], "std": m.get("std") or d.get("std"), "quality": m.get("quality")}
                got = attempt([m["url"], *m.get("alts", []), *([m["std"]] if m.get("std") else [])])
                if got:
                    return got
    raise last or MediaError(502, media_fail_message(0))


# ----------------------------------------------------------------------------------------------
# YouTube through RapidAPI (the only way YouTube links are handled)
# ----------------------------------------------------------------------------------------------
# One call to the API's "video details" endpoint returns the title, thumbnail, duration and direct links:
#   * "videos": ready-made files that already contain sound (usually up to 720p), and
#   * higher qualities as VIDEO-ONLY files, plus separate "audios".
# Video-only files have no sound, so those are downloaded together with the best audio track and joined with
# ffmpeg (no re-encoding of the video). That is what keeps long / high-quality videos from coming out silent.
PART_BYTES = 8 * 1024 * 1024  # size of each piece downloaded in parallel
MEDIA_HEADERS = {"User-Agent": BROWSER_UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
rapid = requests.Session()  # keeps the connection to RapidAPI open between calls (faster repeat look-ups)
YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def youtube_id(url: str) -> str | None:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if host == "youtu.be" or host.endswith(".youtu.be"):
        m = re.match(r"^/([A-Za-z0-9_-]{11})", p.path)
        return m.group(1) if m else None
    v = (parse_qs(p.query).get("v") or [""])[0]
    if YT_ID.match(v):
        return v
    m = re.match(r"^/(?:shorts|embed|live|v|e)/([A-Za-z0-9_-]{11})", p.path)
    return m.group(1) if m else None


def yt_details(video_id: str, fresh: bool = False) -> dict:
    """Ask RapidAPI about one video (cached for a few minutes; `fresh` skips the cache after a link expired)."""
    if not fresh:
        hit = cache_get("yt:" + video_id)
        if hit is not None:
            return hit
    if not RAPIDAPI_KEY:
        raise MediaError(503, "YouTube downloads are not switched on yet. The site owner needs to add a RAPIDAPI_KEY on the server.")
    try:
        r = rapid.get(f"https://{RAPIDAPI_HOST}{RAPIDAPI_PATH}",
                      params={"videoId": video_id, "urlAccess": "normal", "videos": "true", "audios": "true"},
                      headers={"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": RAPIDAPI_HOST},
                      timeout=(8, 30))
    except requests.RequestException:
        raise MediaError(502, "Couldn't reach the YouTube service. Try again in a moment.")
    if r.status_code in (401, 403):
        print("RapidAPI refused the key:", r.status_code, r.text[:200])
        raise MediaError(503, "The YouTube service refused this server's RapidAPI key. The site owner should check RAPIDAPI_KEY and the API subscription.")
    if r.status_code == 429:
        raise MediaError(429, "The YouTube service has reached its request limit for now. Try again later.")
    if not r.ok:
        raise MediaError(502, f"The YouTube service had a problem ({r.status_code}). Try again in a moment.")
    try:
        data = r.json()
    except ValueError:
        raise MediaError(502, "The YouTube service sent an unreadable answer. Try again in a moment.")
    if not isinstance(data, dict) or data.get("status") is False:
        reason = str(data.get("reason") or data.get("errorId") or "") if isinstance(data, dict) else ""
        raise MediaError(422, f"YouTube says this video can't be fetched{': ' + reason[:120] if reason else ''}.")
    cache_put("yt:" + video_id, data)
    return data


def yt_items(block) -> list[dict]:
    if isinstance(block, dict):
        block = block.get("items")
    return [x for x in (block or []) if isinstance(x, dict) and isinstance(x.get("url"), str) and x["url"].startswith("https://")]


def yt_streams(data: dict) -> tuple[list[dict], list[dict]]:
    return yt_items(data.get("videos")), yt_items(data.get("audios"))


def item_height(x: dict) -> int | None:
    m = re.search(r"(\d{3,4})p", str(x.get("quality") or x.get("qualityLabel") or ""))  # "1080p60" -> 1080 (also right for Shorts)
    if m:
        return int(m.group(1))
    h = x.get("height")
    return int(h) if isinstance(h, (int, float)) and h > 0 else None


def item_size(x: dict) -> int:
    try:
        return int(x.get("size") or x.get("contentLength") or 0)
    except (TypeError, ValueError):
        return 0


def item_mime(x: dict) -> str:
    return f"{x.get('mimeType') or x.get('mime_type') or ''} {x.get('extension') or ''}".lower()


def is_mp4(x: dict) -> bool:
    m = item_mime(x)
    return "mp4" in m and "av01" not in m and "vp9" not in m  # H.264 in MP4 plays everywhere


def is_aac(x: dict) -> bool:
    m = item_mime(x)
    return "mp4a" in m or "audio/mp4" in m or "m4a" in m


def best_audio(audios: list[dict], want_aac: bool = False) -> dict | None:
    return max(audios, key=lambda x: (is_aac(x) if want_aac else False, item_size(x)), default=None)


def usable_videos(videos: list[dict], audios: list[dict]) -> list[dict]:
    """Videos that can become a playable MP4 *with sound* and fit the size cap."""
    cap = MAX_MB * 1024 * 1024
    audio_size = max((item_size(a) for a in audios), default=0)
    out = []
    for x in videos:
        has = bool(x.get("hasAudio"))
        if has and not is_mp4(x):
            continue  # a finished file in an odd container: skip it rather than save a broken .mp4
        if not has and not audios:
            continue  # video-only and nothing to join it with: would be silent
        if item_size(x) and item_size(x) + (0 if has else audio_size) > cap:
            continue
        out.append(x)
    return out


def pick_video(usable: list[dict], height: int | None) -> dict | None:
    pool = usable
    if height is not None:
        known = [x for x in usable if item_height(x)]
        exact = [x for x in known if item_height(x) == height]
        if exact:
            pool = exact
        else:
            below = [x for x in known if item_height(x) <= height]  # asked quality vanished: nearest one below
            top = max((item_height(x) for x in below), default=None)
            pool = [x for x in below if item_height(x) == top]
    # at the same height prefer a finished file with sound (no joining needed), then H.264, then the biggest
    return max(pool, key=lambda x: (bool(x.get("hasAudio")), is_mp4(x), item_size(x)), default=None)


def yt_thumbnail(data: dict, video_id: str) -> str:
    thumbs = [t for t in (data.get("thumbnails") or []) if isinstance(t, dict) and str(t.get("url", "")).startswith("https://")]
    if thumbs:
        return max(thumbs, key=lambda t: t.get("width") or 0)["url"]
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def too_big() -> MediaError:
    return MediaError(413, f"That file is larger than the {MAX_MB} MB limit.")


_any_host = any_host  # the address itself is still checked by safe_host() inside follow()


def _fetch_whole(url: str, part: str, cap: int) -> None:
    r = follow(url, MEDIA_HEADERS, _any_host, 60)
    try:
        if not r.ok:
            raise MediaError(r.status_code, media_fail_message(r.status_code))
        size = 0
        with open(part, "wb") as out:
            for chunk in r.iter_content(262144):
                size += len(chunk)
                if size > cap:
                    raise too_big()
                out.write(chunk)
    except requests.RequestException:
        raise MediaError(502, media_fail_message(0))
    finally:
        r.close()


def _fetch_in_parts(url: str, part: str, total: int) -> bool:
    """Download several slices at once. Returns False if the server doesn't cooperate (the caller then streams normally)."""
    ranges = [(s, min(s + PART_BYTES, total) - 1) for s in range(0, total, PART_BYTES)]
    sess = requests.Session()

    def one(rng: tuple[int, int]) -> None:
        start, end = rng
        for attempt in range(3):
            try:
                r = sess.get(url, headers={**MEDIA_HEADERS, "Range": f"bytes={start}-{end}"}, stream=True,
                             allow_redirects=False, timeout=(15, 60))
                try:
                    if r.status_code != 206:
                        raise OSError(f"unexpected status {r.status_code}")
                    pos = start
                    with open(part, "r+b") as f:
                        f.seek(start)
                        for chunk in r.iter_content(262144):
                            f.write(chunk)
                            pos += len(chunk)
                    if pos != end + 1:
                        raise OSError("short slice")
                    return
                finally:
                    r.close()
            except (requests.RequestException, OSError):
                if attempt == 2:
                    raise

    try:
        with open(part, "wb") as f:
            f.truncate(total)
        with ThreadPoolExecutor(max_workers=DL_WORKERS) as pool:
            list(pool.map(one, ranges))
        return True
    except (requests.RequestException, OSError):
        return False
    finally:
        sess.close()


def download_to(url: str, dest: str, cap: int) -> None:
    """Save one media file. Large files are fetched as parallel slices (much faster than one connection)."""
    try:
        r = follow(url, {**MEDIA_HEADERS, "Range": "bytes=0-0"}, _any_host, 30)
    except requests.RequestException:
        raise MediaError(502, media_fail_message(0))
    try:
        final, ranged, total = r.url, False, 0
        if r.status_code == 206:
            tail = r.headers.get("content-range", "").rsplit("/", 1)[-1]
            total = int(tail) if tail.isdigit() else 0
            ranged = total > 0
        elif r.ok:
            total = int(r.headers.get("content-length") or 0)
        else:
            raise MediaError(r.status_code, media_fail_message(r.status_code))
    finally:
        r.close()
    if total > cap:
        raise too_big()
    part = dest + ".part"
    try:
        if not (ranged and total >= 2 * PART_BYTES and DL_WORKERS > 1 and _fetch_in_parts(final, part, total)):
            _fetch_whole(final, part, cap)
        os.replace(part, dest)
    finally:
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass


def build_youtube(data: dict, item: "Item", folder: str) -> str:
    cap = MAX_MB * 1024 * 1024
    videos, audios = yt_streams(data)
    out = os.path.join(folder, f"{item.id}.{item.ext}")

    if item.mode == "audio":
        src = best_audio(audios)
        if src is None:  # no separate audio offered: take it from the smallest finished video
            src = min((x for x in videos if x.get("hasAudio")), key=lambda x: item_size(x) or 1 << 60, default=None)
        if src is None:
            raise MediaError(422, "YouTube offered no audio for this video.")
        tmp = os.path.join(folder, "audio.src")
        download_to(src["url"], tmp, cap)
        run_ffmpeg(["-i", tmp, "-vn", "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3", out])
        os.remove(tmp)
        return out

    video = pick_video(usable_videos(videos, audios), item.height)
    if video is None:
        raise MediaError(422, "That quality isn't available for this video. Grab the link again and pick another.")
    if video.get("hasAudio"):  # already a complete file
        download_to(video["url"], out, cap)
        return out

    audio = best_audio(audios, want_aac=is_mp4(video))
    vtmp, atmp = os.path.join(folder, "video.src"), os.path.join(folder, "audio.src")
    with ThreadPoolExecutor(max_workers=2) as pool:  # picture and sound download at the same time
        jobs = [pool.submit(download_to, video["url"], vtmp, cap), pool.submit(download_to, audio["url"], atmp, cap)]
        for j in jobs:
            j.result()
    run_ffmpeg(["-i", vtmp, "-i", atmp, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                *(["-c:a", "copy"] if is_aac(audio) else ["-c:a", "aac", "-b:a", "192k"]),
                "-shortest", "-movflags", "+faststart", "-f", "mp4", out])
    for t in (vtmp, atmp):
        os.remove(t)
    if os.path.getsize(out) > cap:
        raise too_big()
    return out


def fetch_youtube(item: "Item", folder: str) -> str:
    vid = item.yt["id"]
    for attempt in (0, 1):
        data = yt_details(vid, fresh=attempt == 1)
        try:
            return build_youtube(data, item, folder)
        except MediaError as e:
            if attempt == 0 and e.status in (403, 404, 410):  # the links in the answer expire: ask once more for new ones
                clear_folder(folder)
                continue
            raise
    raise MediaError(502, media_fail_message(0))


def youtube_options(video_id: str, data: dict) -> list[dict]:
    title = clean_title(data.get("title"))
    videos, audios = yt_streams(data)
    usable = usable_videos(videos, audios)
    link = f"https://www.youtube.com/watch?v={video_id}"
    heights = sorted({item_height(x) for x in usable if item_height(x)}, reverse=True)[:4]
    out = []
    for h in heights or ([None] if usable else []):
        it = register(source=link, mode="video", title=title, ext="mp4", height=h, yt={"id": video_id})
        out.append(option(it, f"Video · {h}p" if h else "Video"))
    if audios or any(x.get("hasAudio") for x in videos):
        it = register(source=link, mode="audio", title=title, ext="mp3", yt={"id": video_id})
        out.append(option(it, "Audio"))
    if not out:
        raise too_big() if videos else MediaError(422, "No downloadable video or audio was found for this link.")
    return out


# ----------------------------------------------------------------------------------------------
# Prepared downloads: grab -> options -> prepare (probe) -> file
# ----------------------------------------------------------------------------------------------
@dataclass
class Item:
    id: str
    source: str                      # the video link (for Snapchat: the page link)
    mode: str                        # "video" | "audio"
    title: str
    ext: str
    height: int | None = None        # the quality the visitor picked (None = best)
    direct: dict | None = None       # Snapchat: {"url": media address, "kind": "video" | "image"}
    yt: dict | None = None           # YouTube (RapidAPI): {"id": video id}
    media: dict | None = None        # TikTok / Instagram / X read by social.py: {"platform", "type", "index", "post"}
    note: str | None = None          # a plain-language remark shown after the file is ready (e.g. watermark fallback)
    info: dict | None = None         # yt-dlp's look-up result, reused by the download while it is fresh
    created: float = field(default_factory=time.time)
    touched: float = field(default_factory=time.time)
    path: str | None = None          # the finished file, once prepared
    task: asyncio.Task | None = None


items: dict[str, Item] = {}


def folder_of(item_id: str) -> str:
    return os.path.join(DOWNLOAD_DIR, item_id)


def drop_item(item_id: str) -> None:
    items.pop(item_id, None)
    shutil.rmtree(folder_of(item_id), ignore_errors=True)


def register(**kw) -> Item:
    item = Item(id=secrets.token_hex(12), **kw)
    items[item.id] = item
    if len(items) > 2000:  # oldest first
        drop_item(next(iter(items)))
    return item


def sweep() -> None:
    now = time.time()
    for iid, it in list(items.items()):
        if it.task is None and now - it.touched > FILE_TTL:
            drop_item(iid)
    ready = [(it.touched, it.id, os.path.getsize(it.path)) for it in items.values() if it.path and os.path.isfile(it.path)]
    total = sum(r[2] for r in ready)
    for _, iid, size in sorted(ready):  # over the disk budget: the least recently used go first
        if total <= CACHE_MAX:
            break
        if items[iid].task is None:
            drop_item(iid)
            total -= size
    for ip in list(hits):
        q = hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if not q:
            del hits[ip]


async def sweeper() -> None:
    while True:
        await asyncio.sleep(300)
        try:
            sweep()
        except Exception as e:  # never let housekeeping stop the server
            print("Sweep failed:", repr(e))


def clean_title(t) -> str:
    s = "".join(c for c in str(t or "") if c.isalnum() or c.isspace() or c in "_-")
    return re.sub(r"\s+", " ", s).strip()[:80].strip() or "video"


SOCIAL = ("TikTok", "Instagram", "X")


def post_cached(platform: str, link: str, force: bool = False):
    """social.py's reading of a post, remembered for a few minutes (a failed reading is never remembered)."""
    key = "post:" + link
    if not force:
        hit = cache_get(key)
        if hit is not None:
            return hit
    post = read_post(platform, link)
    if post:
        cache_put(key, post)
    return post


def save_post_item(post: dict, m: dict, item: Item, folder: str) -> str:
    cap = MAX_MB * 1024 * 1024
    base = os.path.join(folder, item.id)
    kind, ix = m["type"], m["index"]
    gone = MediaError(422, "That part of the post is no longer available. Grab the link again.")
    if kind == "image":
        if ix >= len(post["images"]):
            raise gone
        return save_media(post["images"][ix]["urls"], base, post, "image", cap)
    if kind == "zip":
        if not post["images"]:
            raise gone
        out = base + ".zip"
        saved = save_zip(post, folder, out, cap)
        if saved < len(post["images"]):
            item.note = f"{saved} of {len(post['images'])} photos could be saved; the others were not available."
        return out
    if kind == "video":
        if ix >= len(post["videos"]):
            raise gone
        path = save_media(post["videos"][ix]["urls"], base, post, "video", cap)
        audio = audio_streams(path)
        if audio and not (set(audio) & SAFE_AUDIO):
            fix_audio_codec(path)
        return path
    # audio: from the video when there is one, otherwise the post's own sound (photo posts)
    src_urls = post["videos"][0]["urls"] if post["videos"] else (post.get("audio") or {}).get("urls") or []
    if not src_urls:
        raise MediaError(422, "This post has no sound to save.")
    src = save_media(src_urls, os.path.join(folder, "audio-src"), post, "video", cap)
    out = base + ".mp3"
    run_ffmpeg(["-i", src, "-vn", "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3", out])
    os.remove(src)
    return out


def fetch_media(item: Item, folder: str, allow_ytdlp: bool = True) -> str:
    """Save one TikTok / Instagram / X item. Layers: the addresses read at grab time -> the post read again (fresh
    addresses and cookies) -> yt-dlp on the original link (video and audio only)."""
    m = item.media
    item.note = None
    last: Exception | None = None
    try:
        return save_post_item(m["post"], m, item, folder)
    except MediaError as e:
        if e.status == 413:
            raise
        last = e
    clear_folder(folder)
    fresh = post_cached(m["platform"], item.source, force=True)
    if fresh:
        m["post"] = fresh
        try:
            return save_post_item(fresh, m, item, folder)
        except MediaError as e:
            if e.status == 413:
                raise
            last = e
    if allow_ytdlp and m["type"] in ("video", "audio"):
        clear_folder(folder)
        try:
            return fetch(item.source, folder, m["type"], None, None)
        except Exception as e:
            print("yt-dlp last layer failed:", repr(e))
    raise last


def rescue(item: Item, folder: str) -> str | None:
    """A yt-dlp download failed: try the other ways of reading the same post before giving up."""
    platform = platform_of(item.source)
    try:
        clear_folder(folder)
        if platform == "Snapchat":
            snap = grab_snapchat(item.source)
            video = next((x for x in (snap or {}).get("medias", []) if x["kind"] == "video"), None)
            if not video:
                return None
            item.direct = {"url": video["url"], "kind": "video", "alts": video.get("alts", []), "std": video.get("std"), "quality": video.get("quality")}
            if item.mode != "audio":
                return fetch_direct(item, folder)
            want, item.ext = item.ext, "mp4"  # save the video first, then take the sound out of it
            try:
                src = fetch_direct(item, folder)
            finally:
                item.ext = want
            out = os.path.join(folder, f"{item.id}.mp3")
            run_ffmpeg(["-i", src, "-vn", "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3", out])
            os.remove(src)
            return out
        if platform in SOCIAL and item.mode in ("video", "audio"):
            post = post_cached(platform, item.source, force=True)
            if not post:
                return None
            item.media = {"platform": platform, "type": item.mode, "index": 0, "post": post}
            return fetch_media(item, folder, allow_ytdlp=False)
    except Exception as e:
        print("Rescue layer failed:", repr(e))
        if isinstance(e, MediaError) and e.status == 413:
            raise
    return None


def build(item: Item, folder: str) -> str:
    if item.yt:
        return fetch_youtube(item, folder)
    if item.direct:
        return fetch_direct(item, folder)
    if item.media:
        return fetch_media(item, folder)
    fresh = item.info if time.time() - item.created < INFO_TTL else None
    try:
        return fetch(item.source, folder, item.mode, item.height, fresh)
    except Exception as first:
        if re.search(r"larger than|max-filesize|too large", str(first), re.I):
            raise  # a size problem: another route would hit the same limit
        got = rescue(item, folder)
        if got:
            return got
        raise


def why(e: Exception) -> str:
    return ANSI.sub("", str(e)).removeprefix("ERROR: ")[:260]


async def _build(item: Item) -> str:
    folder = folder_of(item.id)
    try:
        os.makedirs(folder, exist_ok=True)
        async with slots:
            path = await asyncio.to_thread(build, item, folder)
        item.path = path
        return path
    except MediaError as e:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(e.status if e.status in (413, 422, 429, 503) else 502, str(e))
    except Exception as e:
        shutil.rmtree(folder, ignore_errors=True)
        what = "extract the audio from" if item.mode == "audio" else "download"
        raise HTTPException(422, f"Couldn't {what} that video: {why(e)}")
    finally:
        item.task = None
        item.info = None


async def prepare(item: Item) -> str:
    """Get the file ready (one job per item, even if the page asks twice) and return its path."""
    if item.path and os.path.isfile(item.path):
        item.touched = time.time()
        return item.path
    task = item.task
    if task is None:
        task = item.task = asyncio.create_task(_build(item))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())  # an unread failure is not an error
    # shield: if the visitor leaves mid-download the job still finishes and the file is kept for their retry
    path = await asyncio.shield(task)
    item.touched = time.time()
    return path


def size_of(f: dict):
    return f.get("filesize") or f.get("filesize_approx")


def video_heights(entry: dict) -> list[int]:
    """Qualities worth offering: distinct heights, best first, skipping any that clearly exceed the size cap."""
    cap = MAX_MB * 1024 * 1024
    fmts = entry.get("formats") or []
    audio = [size_of(f) or 0 for f in fmts if f.get("vcodec") == "none" and f.get("acodec") != "none"]
    best_audio = max(audio, default=0)
    sizes: dict[int, list] = {}
    for f in fmts:
        h = f.get("height")
        if not isinstance(h, int) or h <= 0 or f.get("vcodec") == "none":
            continue
        sizes.setdefault(h, []).append((size_of(f) or 0) + (best_audio if f.get("acodec") in (None, "none") else 0))
    out = []
    for h in sorted(sizes, reverse=True):
        known = [s for s in sizes[h] if s]
        if known and min(known) > cap:
            continue
        out.append(h)
    return out[:4]


def option(item: Item, label: str) -> dict:
    return {
        "label": label,
        "format": item.ext.upper(),
        "kind": "audio" if item.mode == "audio" else "video",
        "filename": f"{item.title}.{item.ext}",
        "url": f"/api/download/{item.id}",
    }


def options_from_info(url: str, entry: dict) -> list[dict]:
    title = clean_title(entry.get("title"))
    known = entry if entry.get("formats") else None
    out = []
    for h in video_heights(entry) or [None]:
        item = register(source=url, mode="video", title=title, ext="mp4", height=h, info=known)
        out.append(option(item, f"Video · {h}p" if h else "Video"))
    audio = register(source=url, mode="audio", title=title, ext="mp3", info=known)
    out.append(option(audio, "Audio"))
    return out


def pick_preview(options: list[dict], first: bool = False) -> str | None:
    """Which option the thumbnail's play button streams. Snapchat: the first video (it is the snap shown).
    Other sites: the smallest video of at least 360p (quick to prepare), else the smallest there is."""
    vids = [o for o in options if o.get("kind") == "video"]
    if not vids:
        return None
    if first or len(vids) == 1:
        return vids[0]["url"]

    def h(o: dict) -> int:
        m = re.search(r"(\d+)p", o.get("label", ""))
        return int(m.group(1)) if m else 0

    ok = [o for o in vids if h(o) >= 360]
    return (min(ok, key=h) if ok else min(vids, key=h))["url"]


def options_from_snap(link: str, snap: dict) -> list[dict]:
    title = clean_title(snap["title"])
    videos = [m for m in snap["medias"] if m["kind"] == "video"][:4]
    picked = videos or [m for m in snap["medias"] if m["kind"] == "image"][:4]
    out = []
    for i, m in enumerate(picked, 1):
        item = register(source=link, mode="video", title=title, ext=m["ext"],
                        direct={"url": m["url"], "kind": m["kind"], "alts": m.get("alts", []), "std": m.get("std"), "quality": m.get("quality")})
        if m["kind"] == "image":
            label = "Image"
        else:
            label = "Video" + (f" {i}" if len(videos) > 1 else "") + (f" · {m['quality']}" if m["quality"] else "")
        out.append({**option(item, label), "kind": m["kind"]})
    return out


def options_from_post(link: str, platform: str, post: dict) -> list[dict]:
    """Videos, pictures (one ZIP for the whole carousel + the first few as single files) and the sound of a post."""
    title = clean_title(post["title"])
    out = []

    def add(mode: str, ext: str, label: str, kind: str, mtype: str, ix: int) -> None:
        it = register(source=link, mode=mode, title=title, ext=ext, media={"platform": platform, "type": mtype, "index": ix, "post": post})
        out.append({**option(it, label), "kind": kind})

    videos, images = post["videos"][:4], post["images"]
    for i in range(len(videos)):
        add("video", "mp4", "Video" + (f" {i + 1}" if len(videos) > 1 else ""), "video", "video", i)
    if len(images) > 1:
        add("image", "zip", f"All {len(images)} photos", "image", "zip", 0)
    for i in range(min(len(images), 6)):
        add("image", "jpg", "Photo" if len(images) == 1 else f"Photo {i + 1}", "image", "image", i)
    if post["videos"] or post.get("audio"):
        add("audio", "mp3", "Audio", "audio", "audio", 0)
    return out


def post_response(link: str, platform: str, post: dict) -> dict:
    opts = options_from_post(link, platform, post)
    return {"title": clean_title(post["title"]), "platform": platform, "thumbnail": post.get("thumbnail"),
            "duration": post.get("duration"), "options": opts, "preview": pick_preview(opts, first=True)}


@app.get("/health")
@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/grab")
def grab_info():
    return JSONResponse({"error": "Use POST."}, status_code=405, headers={"Allow": "POST"})


@app.post("/api/grab")
async def grab(req: Req, request: Request):
    check_proxy(request)
    check_rate(client_ip(request))
    url = req.url.strip()
    if not valid_url(url):
        raise HTTPException(400, BAD_LINK)
    try:
        url = await asyncio.to_thread(resolve_short, url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except requests.RequestException:
        raise HTTPException(502, "Could not open that link. Check it or try again.")

    platform = platform_of(url)
    if is_youtube(url):  # YouTube: RapidAPI only
        vid = youtube_id(url)
        if not vid:
            raise HTTPException(400, "That YouTube link doesn't point to a single video. Paste a video, Shorts or youtu.be link.")
        async with grab_slots:
            try:
                data = await asyncio.to_thread(yt_details, vid)
                options = youtube_options(vid, data)
            except MediaError as e:
                raise HTTPException(e.status if 400 <= e.status < 600 else 502, str(e))
        length = data.get("lengthSeconds") or data.get("duration")
        try:
            length = float(length)
        except (TypeError, ValueError):
            length = None
        return {"title": clean_title(data.get("title")), "platform": platform, "thumbnail": yt_thumbnail(data, vid),
                "duration": length, "options": options, "preview": pick_preview(options)}

    async with grab_slots:
        if is_snap_host((urlparse(url).hostname or "").lower()):
            snap = await asyncio.to_thread(grab_snapchat_cached, url)
            if snap:
                opts = options_from_snap(url, snap)
                return {"title": clean_title(snap["title"]), "platform": platform, "thumbnail": snap.get("thumbnail"), "duration": None,
                        "options": opts, "preview": pick_preview(opts, first=True)}
        # TikTok: our own page reader first (yt-dlp is the one that fails most often there, and it can't read photo
        # posts). Instagram / X: yt-dlp first, and when it can't read the post (photo posts, blocked, changed page)
        # social.py reads it instead, so a picture post is offered as pictures rather than shown as an error.
        if platform == "TikTok":
            post = await asyncio.to_thread(post_cached, platform, url)
            if post:
                return post_response(url, platform, post)
        entry, lookup_err = None, None
        try:
            entry = await asyncio.to_thread(lookup, url)
        except Exception as e:
            lookup_err = e
        if platform in ("Instagram", "X") and not (entry and has_media(entry) and not entry.get("fallback")):
            post = await asyncio.to_thread(post_cached, platform, url)
            if post:
                return post_response(url, platform, post)
        if entry is None:
            if platform == "Snapchat":
                raise HTTPException(422, SNAP_FAIL)
            if platform in SOCIAL:
                raise HTTPException(422, f"Couldn't read that {platform} post right now. It may be private or removed, or {platform} is blocking requests. "
                                         f"Try again in a moment. ({why(lookup_err)[:120]})")
            raise HTTPException(422, f"Couldn't read that video: {why(lookup_err)}")

    thumb = entry.get("thumbnail")
    duration = entry.get("duration")
    opts = options_from_info(url, entry)
    return {
        "title": clean_title(entry.get("title")),
        "platform": platform,
        "thumbnail": thumb if isinstance(thumb, str) and thumb.startswith("https://") else None,
        "duration": duration if isinstance(duration, (int, float)) else None,
        "options": opts,
        "preview": pick_preview(opts),
    }


@app.get("/api/download/{item_id}")
async def download(item_id: str, request: Request, probe: int = 0, inline: int = 0):
    check_proxy(request)
    item = items.get(item_id) if ID_RE.match(item_id) else None
    if not item:
        raise HTTPException(410, "This download link has expired. Grab the video again.")
    if not (item.path and os.path.isfile(item.path)):
        check_rate(client_ip(request))  # only work that has to be done counts against the limit
    path = await prepare(item)
    if probe:
        return {"ok": True, **({"note": item.note} if item.note else {})}
    ext = os.path.splitext(path)[1].lower()
    return FileResponse(path, media_type=MEDIA_MIME.get(ext, "application/octet-stream"),
                        filename=f"{item.title}{ext}",
                        content_disposition_type="inline" if inline else "attachment",  # inline = the page's play button
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


# Optional: if a "static" folder sits next to this file (or the project's "public" folder is one level up),
# serve it as the website (handy for local testing). In production the site is served by the Cloudflare
# worker, so the backend runs without this folder.
_HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = next((d for d in (os.path.join(_HERE, "static"), os.path.join(_HERE, "..", "public")) if os.path.isdir(d)), None)
if STATIC_DIR:
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="site")
