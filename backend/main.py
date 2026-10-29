"""SnapIT backend (FastAPI + yt-dlp), built on the Toolora downloader.

  POST /api/grab                  {"url": "..."} -> the video's details and a list of download options
  GET  /api/download/<id>?probe=1 prepares the chosen option, then answers {"ok": true} (or {"error": "..."})
  GET  /api/download/<id>         returns the file as a real download (attachment)
  GET  /health                    {"ok": true}

How a download works (same logic as Toolora's /download):
  * the link must belong to a supported site (this also blocks SSRF to internal hosts)
  * yt-dlp fetches the video with the same format ladder and the same YouTube client fallbacks
  * "video" options give an MP4 that fits the size cap, "audio" options give the sound only as an MP3
  * Snapchat links are read straight from Snapchat's public page (no watermark when the clean copy exists)
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
"""
import asyncio, hmac, ipaddress, json, os, re, secrets, shutil, tempfile, time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from itertools import islice
from urllib.parse import urljoin, urlparse

import requests
import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
MAX_MB = int(os.getenv("MAX_FILESIZE_MB", "200"))
RATE = int(os.getenv("RATE_PER_MINUTE", "20"))
PROXY_SECRET = os.getenv("PROXY_SECRET", "")
FILE_TTL = int(os.getenv("FILE_TTL_MINUTES", "30")) * 60
CACHE_MAX = int(os.getenv("CACHE_MAX_MB", "1024")) * 1024 * 1024
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR") or os.path.join(tempfile.gettempdir(), "snapit_downloads")
slots = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT", "3")))
grab_slots = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT_GRABS", "6")))

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
              ".webp": "image/webp", ".gif": "image/gif"}

ID_RE = re.compile(r"^[a-f0-9]{24}$")


@asynccontextmanager
async def lifespan(_: FastAPI):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    for name in os.listdir(DOWNLOAD_DIR):  # files from an earlier run can no longer be reached, so clear them
        if ID_RE.match(name):
            shutil.rmtree(os.path.join(DOWNLOAD_DIR, name), ignore_errors=True)
    sweeper_task = asyncio.create_task(sweeper())
    try:
        yield
    finally:
        sweeper_task.cancel()


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
# yt-dlp download logic (Toolora)
# ----------------------------------------------------------------------------------------------
SORT = ["res", "ext:mp4:m4a"]  # best resolution, preferring MP4/M4A so the result plays on phones
FRAGMENT = re.compile(r"\.f[\w-]+\.[A-Za-z0-9]+$")  # intermediate streams such as "name.f137.mp4"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
YT_CLIENTS = ["tv", "android_vr", "web_embedded", "web_safari", "web"]
YT_RETRY = ("player response", "sign in to confirm", "requested format", "unable to extract", "http error 403", "forbidden")


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


def attempts_for(mode: str, url: str, height: int | None = None) -> list[dict]:
    if mode == "audio":
        formats = [{
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        }]
    elif height is None:
        formats = [{"format": "bv*+ba/b", "format_sort": SORT},
                   {"format": "bv*[height<=1080]+ba/b[height<=1080]", "format_sort": SORT},
                   {"format": "bv*[height<=720]+ba/b[height<=720]", "format_sort": SORT},
                   {"format": "bv*[height<=480]+ba/b[height<=480]", "format_sort": SORT},
                   {"format": "best"}]
    else:  # the quality the visitor picked, then the same ladder below it
        caps = [height] + [h for h in (1080, 720, 480) if h < height]
        formats = [{"format": f"bv*[height<={h}]+ba/b[height<={h}]", "format_sort": SORT} for h in caps]
        formats.append({"format": "best"})
    if not is_youtube(url):
        return formats

    # YouTube's extractor changes frequently. Try current clients in a deliberate order.
    out = []
    for client in YT_CLIENTS:
        for item in formats[:4 if mode == "video" else 1]:
            x = dict(item)
            x["extractor_args"] = {"youtube": {"player_client": [client]}}
            x["remote_components"] = ["ejs:github"]
            out.append(x)
    # Final generic attempts let yt-dlp choose a newly supported client.
    out.extend({**x, "remote_components": ["ejs:github"]} for x in formats)
    return out


def pick_file(folder: str, mode: str) -> str | None:
    want = (".mp3",) if mode == "audio" else (".mp4", ".mkv", ".webm", ".mov")
    files = [f for f in os.listdir(folder) if f.lower().endswith(want) and not FRAGMENT.search(f)]
    if not files:
        return None
    return os.path.join(folder, max(files, key=lambda f: os.path.getsize(os.path.join(folder, f))))


def fetch(url: str, folder: str, mode: str = "video", height: int | None = None) -> str:
    base = {
        "outtmpl": os.path.join(folder, "%(title).80B [%(id)s].%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "restrictfilenames": True,
        "max_filesize": MAX_MB * 1024 * 1024,
        "socket_timeout": 20,
        "quiet": True,
        "no_warnings": False,
        "retries": 3,
        "fragment_retries": 3,
        "remote_components": ["ejs:github"],
    }
    last = None
    for extra in attempts_for(mode, url, height):
        clear_folder(folder)
        try:
            with yt_dlp.YoutubeDL({**base, **extra}) as ydl:
                ydl.download([url])
        except yt_dlp.utils.DownloadError as e:
            msg = str(e).lower()
            if is_youtube(url) and any(x in msg for x in YT_RETRY):
                last = e
                continue
            if mode == "audio" or "requested format" not in msg:
                raise
            last = e
            continue
        path = pick_file(folder, mode)
        if path:
            return path
    if last:
        raise last
    raise RuntimeError("no file produced (it may exceed the size limit)")


def info_attempts(url: str) -> list[dict]:
    if not is_youtube(url):
        return [{}]
    out = [{"extractor_args": {"youtube": {"player_client": [c]}}, "remote_components": ["ejs:github"]} for c in YT_CLIENTS]
    out.append({"remote_components": ["ejs:github"]})
    return out


def first_entry(info: dict) -> dict | None:
    """yt-dlp answers a post with several videos (X, Instagram) as a playlist; the first one is previewed."""
    if info.get("entries") is not None:
        return next((e for e in islice(info["entries"], 20) if e), None)
    return info


def has_media(entry: dict | None) -> bool:
    return bool(entry and (entry.get("formats") or entry.get("url")))


def lookup(url: str) -> dict:
    """Read the video's details without downloading it (same clients, same order as the download).

    The preview must never be stricter than the download itself: YouTube often hides the format
    list from a look-up (bot checks, "page needs to be reloaded", PO tokens) while the real download
    still works with another client. So a look-up that finds the video but no formats, or that fails
    outright, falls back to a plain "best quality" option - exactly what Toolora's /download does.
    """
    base = {
        "noplaylist": True,
        "socket_timeout": 20,
        "quiet": True,
        "no_warnings": False,
        "retries": 3,
        "skip_download": True,
        "ignore_no_formats_error": True,
        "remote_components": ["ejs:github"],
    }
    partial = None
    last = None
    for extra in info_attempts(url):
        try:
            with yt_dlp.YoutubeDL({**base, **extra}) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as e:
            last = e
            if is_youtube(url):
                continue  # any YouTube error: try the next client, like the download does
            break
        entry = first_entry(info) if info else None
        if has_media(entry):
            return entry
        if entry and partial is None:
            partial = entry  # title/thumbnail without formats: still usable for the preview
    if partial is not None:
        return partial
    if is_youtube(url) or last is None or "unsupported url" not in str(last).lower():
        # Let the download step (Toolora's proven logic) decide; it reports a real error if it fails.
        return {"title": None, "formats": [], "fallback": True}
    raise last


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


class MediaError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def is_snap_host(h: str) -> bool:
    return h == "snapchat.com" or h.endswith(".snapchat.com")


def is_snap_cdn(h: str) -> bool:
    return h.endswith(".sc-cdn.net") or h.endswith(".snapchat.com")


def safe_host(h: str | None) -> bool:
    """Refuse addresses that point back at this machine or its network."""
    h = (h or "").lower().strip("[]")
    if not h or h == "localhost" or h.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return True
    if ip.version == 6:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def follow(url: str, headers: dict, host_ok, timeout: int = 60):
    """Follow redirects by hand so every hop is checked, not just the first address."""
    for _ in range(6):
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if p.scheme not in ("http", "https") or not safe_host(host) or not host_ok(host):
            raise MediaError(0, "blocked media address")
        r = requests.get(url, headers=headers, stream=True, allow_redirects=False, timeout=(15, timeout))
        loc = r.headers.get("location")
        if 300 <= r.status_code < 400 and loc:
            r.close()
            url = urljoin(url, loc)
            continue
        return r
    raise MediaError(0, "too many redirects")


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
        return r.status_code in (200, 206) and bool(re.search(r"video|octet", r.headers.get("content-type", "")))
    finally:
        r.close()


def first_working(urls: list[str]) -> str | None:
    """Checks every address at the same time but keeps the order of preference."""
    if not urls:
        return None
    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        ok = list(pool.map(snap_reachable, urls))
    return urls[ok.index(True)] if True in ok else None


def resolve_snap_video(media_url: str) -> tuple[str, str]:
    m = SNAP_MEDIA_RE.match(urlparse(media_url).path)
    if m and m.group(3) == "1034":
        return media_url, SNAP_ORIGINAL
    clean = first_working(clean_candidates(media_url))
    return (clean, SNAP_ORIGINAL) if clean else (media_url, SNAP_STANDARD)


def decode_entities(v: str) -> str:
    return (v.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'").replace("&#039;", "'")
            .replace("&#x27;", "'").replace("&lt;", "<").replace("&gt;", ">"))


def looks_like_video_url(v: str) -> bool:
    return bool(re.match(r"^https?://", v, re.I) and (re.search(r"\.sc-cdn\.net/", v, re.I) or re.search(r"\.(mp4|mov|m4v|webm)(\?|$)", v, re.I)))


SNAP_DEEP_RE = re.compile(r"^https?://[^\s?]*sc-cdn\.net/[^\s?]*\.\d+\.mp4(\?|$)", re.I)


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
    return found, title


def grab_snapchat(link: str) -> dict | None:
    best = None
    snap_thumb = None
    # Snapchat sometimes answers one kind of visitor with a block or a bare page, so try a few.
    for ua in (BROWSER_UA, MOBILE_UA, BOT_UA):
        try:
            r = follow(link, {"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}, is_snap_host, 20)
        except (MediaError, requests.RequestException):
            continue
        try:
            if r.status_code in (404, 410):
                break
            if not r.ok:
                continue
            html = r.raw.read(6_000_000, decode_content=True).decode(r.encoding or "utf-8", "ignore")
        except (requests.RequestException, OSError):
            continue
        finally:
            r.close()
        found, title = extract_snap_media(html, link)
        if found:
            m = re.search(r"""<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']+)""", html, re.I)
            snap_thumb = m.group(1).replace("&amp;", "&") if m else None
            best = (found, title)
            break
    if not best:
        return None

    found, title = best
    thumb = snap_thumb if snap_thumb and snap_thumb.startswith("https://") else None
    medias = []
    for url, kind in found[:10]:
        if kind == "image":
            medias.append({"url": url, "kind": "image", "ext": "jpg", "quality": ""})
            continue
        url, quality = resolve_snap_video(url)
        medias.append({"url": url, "kind": "video", "ext": "mp4", "quality": quality})
    medias = [m for m in medias if is_snap_cdn((urlparse(m["url"]).hostname or "").lower())]
    return {"title": title or "snapchat", "medias": medias, "thumbnail": thumb} if medias else None


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


def fetch_direct(item: "Item", folder: str) -> str:
    """Save one Snapchat media file straight from Snapchat's servers."""
    try:
        r = follow(item.direct["url"], {"User-Agent": BROWSER_UA, "Referer": "https://www.snapchat.com/", "Accept": "*/*"}, is_snap_cdn)
    except requests.RequestException:
        raise MediaError(502, media_fail_message(0))
    try:
        if not r.ok:
            raise MediaError(r.status_code, media_fail_message(r.status_code))
        cap = MAX_MB * 1024 * 1024
        if int(r.headers.get("content-length") or 0) > cap:
            raise MediaError(413, f"That file is larger than the {MAX_MB} MB limit.")
        path = os.path.join(folder, f"{item.id}.{item.ext}")
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
        os.replace(path + ".part", path)
        return path
    finally:
        r.close()


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


def build(item: Item, folder: str) -> str:
    if item.direct:
        return fetch_direct(item, folder)
    return fetch(item.source, folder, item.mode, item.height)


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
        raise HTTPException(502, str(e))
    except Exception as e:
        shutil.rmtree(folder, ignore_errors=True)
        what = "extract the audio from" if item.mode == "audio" else "download"
        raise HTTPException(422, f"Couldn't {what} that video: {why(e)}")
    finally:
        item.task = None


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
    out = []
    for h in video_heights(entry) or [None]:
        item = register(source=url, mode="video", title=title, ext="mp4", height=h)
        out.append(option(item, f"Video · {h}p" if h else "Video"))
    audio = register(source=url, mode="audio", title=title, ext="mp3")
    out.append(option(audio, "Audio"))
    return out


def options_from_snap(link: str, snap: dict) -> list[dict]:
    title = clean_title(snap["title"])
    videos = [m for m in snap["medias"] if m["kind"] == "video"][:4]
    picked = videos or [m for m in snap["medias"] if m["kind"] == "image"][:4]
    out = []
    for i, m in enumerate(picked, 1):
        item = register(source=link, mode="video", title=title, ext=m["ext"], direct={"url": m["url"], "kind": m["kind"]})
        if m["kind"] == "image":
            label = "Image"
        else:
            label = "Video" + (f" {i}" if len(videos) > 1 else "") + (f" · {m['quality']}" if m["quality"] else "")
        out.append({**option(item, label), "kind": m["kind"]})
    return out


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
    async with grab_slots:
        if is_snap_host((urlparse(url).hostname or "").lower()):
            snap = await asyncio.to_thread(grab_snapchat, url)
            if snap:
                return {"title": clean_title(snap["title"]), "platform": platform, "thumbnail": snap.get("thumbnail"), "duration": None,
                        "options": options_from_snap(url, snap)}
        try:
            entry = await asyncio.to_thread(lookup, url)
        except Exception as e:
            if platform == "Snapchat":
                raise HTTPException(422, SNAP_FAIL)
            raise HTTPException(422, f"Couldn't read that video: {why(e)}")

    thumb = entry.get("thumbnail")
    duration = entry.get("duration")
    return {
        "title": clean_title(entry.get("title")),
        "platform": platform,
        "thumbnail": thumb if isinstance(thumb, str) and thumb.startswith("https://") else None,
        "duration": duration if isinstance(duration, (int, float)) else None,
        "options": options_from_info(url, entry),
    }


@app.get("/api/download/{item_id}")
async def download(item_id: str, request: Request, probe: int = 0):
    check_proxy(request)
    item = items.get(item_id) if ID_RE.match(item_id) else None
    if not item:
        raise HTTPException(410, "This download link has expired. Grab the video again.")
    if not (item.path and os.path.isfile(item.path)):
        check_rate(client_ip(request))  # only work that has to be done counts against the limit
    path = await prepare(item)
    if probe:
        return {"ok": True}
    ext = os.path.splitext(path)[1].lower()
    return FileResponse(path, media_type=MEDIA_MIME.get(ext, "application/octet-stream"),
                        filename=f"{item.title}{ext}",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


# Optional: if a "static" folder sits next to this file (or the project's "public" folder is one level up),
# serve it as the website (handy for local testing). In production the site is served by the Cloudflare
# worker, so the backend runs without this folder.
_HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = next((d for d in (os.path.join(_HERE, "static"), os.path.join(_HERE, "..", "public")) if os.path.isdir(d)), None)
if STATIC_DIR:
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="site")
