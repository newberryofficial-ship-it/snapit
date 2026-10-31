"""SnapIT - shared network helpers + readers for TikTok, Instagram and X posts (photos AND videos).

main.py uses this module in two ways:
  * as the "extra layer" when yt-dlp cannot read a post (photo posts, blocked requests, changed pages), and
  * as a second way to save the media when a yt-dlp download fails.

Every reader returns the same shape, or None when the post could not be read:
  {"platform", "title", "thumbnail", "duration",
   "videos": [{"urls": [...]}],      # best address first, the rest are backups
   "images": [{"urls": [...]}],
   "audio":  {"urls": [...]} | None,  # the post's own sound (TikTok photo posts)
   "headers": {...}, "cookies": {...}}  # what the media servers want to see when the file is requested

Nothing here talks to a site that is not in the lists below, and every redirect hop is checked.
"""
import ipaddress, json, math, os, re, shutil, time, zipfile
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import requests

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
BOT_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"

# Optional fallback for X posts when X's own embed endpoint refuses us. Set to "" to switch it off.
X_FALLBACK_API = os.getenv("X_FALLBACK_API", "https://api.fxtwitter.com").strip().rstrip("/")

IMAGE_EXT = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif",
             "image/heic": "heic", "image/avif": "avif"}


class MediaError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


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


def follow(url: str, headers: dict, host_ok, timeout: int = 60, session=None, cookies: dict | None = None):
    """Follow redirects by hand so every hop is checked, not just the first address."""
    getter = session.get if session is not None else requests.get
    for _ in range(6):
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if p.scheme not in ("http", "https") or not safe_host(host) or not host_ok(host):
            raise MediaError(0, "blocked media address")
        r = getter(url, headers=headers, cookies=cookies, stream=True, allow_redirects=False, timeout=(15, timeout))
        loc = r.headers.get("location")
        if 300 <= r.status_code < 400 and loc:
            r.close()
            url = urljoin(url, loc)
            continue
        return r
    raise MediaError(0, "too many redirects")


def any_host(_h: str) -> bool:
    return True  # the address itself is still checked by safe_host() inside follow()


def host_in(*roots: str):
    return lambda h: any(h == r or h.endswith("." + r) for r in roots)


def decode_entities(v: str) -> str:
    return (v.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'").replace("&#039;", "'")
            .replace("&#x27;", "'").replace("&lt;", "<").replace("&gt;", ">"))


def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def _l(v) -> list:
    return v if isinstance(v, list) else []


def _https(u) -> bool:
    return isinstance(u, str) and u.startswith("https://")


def _uniq(urls) -> list[str]:
    out: list[str] = []
    for u in urls:
        if _https(u) and u not in out:
            out.append(u)
    return out


def get_page(url: str, headers: dict, host_ok, session=None, timeout: int = 20, limit: int = 6_000_000):
    """-> (text, final address) or None when the page could not be read (blocked, missing, network trouble)."""
    try:
        r = follow(url, headers, host_ok, timeout, session=session)
    except (MediaError, requests.RequestException):
        return None
    try:
        if not r.ok:
            return None
        raw = r.raw.read(limit, decode_content=True)
        return raw.decode(r.encoding or "utf-8", "ignore"), r.url
    except (requests.RequestException, OSError):
        return None
    finally:
        r.close()


def get_json(url: str, headers: dict, host_ok, timeout: int = 20):
    got = get_page(url, headers, host_ok, timeout=timeout, limit=3_000_000)
    if not got:
        return None
    try:
        data = json.loads(got[0])
    except ValueError:
        return None
    return data if isinstance(data, (dict, list)) else None


def _script_json(html: str, script_id: str):
    m = re.search(r"""<script[^>]*\bid=["']""" + re.escape(script_id) + r"""["'][^>]*>([\s\S]*?)</script>""", html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def _find_key(node, key: str, depth: int = 0):
    """First dict value stored under `key` anywhere in the data (page layouts move things around)."""
    if depth > 12:
        return None
    if isinstance(node, dict):
        if isinstance(node.get(key), dict):
            return node[key]
        for v in node.values():
            hit = _find_key(v, key, depth + 1)
            if hit is not None:
                return hit
    elif isinstance(node, list):
        for v in node:
            hit = _find_key(v, key, depth + 1)
            if hit is not None:
                return hit
    return None


def _meta(html: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for tag in re.findall(r"<meta\b[^>]*>", html, re.I):
        pm = re.search(r"""\b(?:property|name)=["']([^"']+)["']""", tag, re.I)
        cm = re.search(r"""\bcontent=["']([^"']*)["']""", tag, re.I)
        if pm and cm:
            out.setdefault(pm.group(1).lower(), decode_entities(cm.group(1)))
    return out


def _post(platform: str, title: str, thumb, duration, videos, images, audio, headers: dict, cookies: dict | None = None):
    if not videos and not images:
        return None
    return {"platform": platform, "title": title or platform.lower(), "thumbnail": thumb if _https(thumb) else None,
            "duration": duration if isinstance(duration, (int, float)) and duration > 0 else None,
            "videos": videos, "images": images, "audio": audio, "headers": headers, "cookies": cookies or {}}


# ----------------------------------------------------------------------------------------------
# TikTok
# ----------------------------------------------------------------------------------------------
is_tiktok_host = host_in("tiktok.com")
TT_ID = re.compile(r"/(?:video|photo)/(\d{8,25})")


def _pick_tt_images(urls: list) -> list[str]:
    urls = _uniq(urls)  # JPEG first: it opens everywhere (TikTok also lists WebP / HEIC copies)
    return sorted(urls, key=lambda u: 0 if re.search(r"\.jpe?g(\?|$)", u, re.I) else 1)


def tt_post_from_item(item: dict, headers: dict, cookies: dict):
    """`item` is TikTok's own description of one post (the "itemStruct")."""
    video, music = _d(item.get("video")), _d(item.get("music"))
    images = []
    for im in _l(_d(item.get("imagePost")).get("images")):
        urls = _pick_tt_images(_l(_d(_d(im).get("imageURL")).get("urlList")))
        if urls:
            images.append({"urls": urls})
    videos = []
    if not images:  # a photo post also has a "video" block, but it only holds the slideshow sound
        ranked = []
        for b in _l(video.get("bitrateInfo")):
            b = _d(b)
            h264 = "h264" in str(b.get("CodecType") or "").lower() or "h264" in str(b.get("GearName") or "").lower()
            urls = _l(_d(b.get("PlayAddr")).get("UrlList"))
            ranked.append((0 if h264 else 1, -int(b.get("Bitrate") or 0), urls))  # H.264 plays everywhere; HEVC does not
        ranked.sort(key=lambda x: (x[0], x[1]))
        urls = [u for r in ranked if r[0] == 0 for u in r[2]]
        for key in ("playAddr", "downloadAddr"):
            v = video.get(key)
            urls += [v] if isinstance(v, str) else _l(_d(v).get("UrlList"))
        urls += [u for r in ranked if r[0] == 1 for u in r[2]]
        urls = _uniq(urls)
        if urls:
            videos.append({"urls": urls})
    music_url = music.get("playUrl")
    audio = {"urls": _uniq([music_url] if isinstance(music_url, str) else _l(_d(music_url).get("UrlList")))} if music_url else None
    if audio is not None and not audio["urls"]:
        audio = None
    author = str(_d(item.get("author")).get("uniqueId") or "")
    title = str(item.get("desc") or "").strip() or f"tiktok {author} {item.get('id') or ''}".strip()
    cover = (video.get("originCover") or video.get("cover")
             or next(iter(_l(_d(_d(_d(item.get("imagePost")).get("cover")).get("imageURL")).get("urlList"))), None)
             or (images[0]["urls"][0] if images else None))
    return _post("TikTok", title, cover, video.get("duration"), videos, images, audio, headers, cookies)


def tt_from_html(html: str, headers: dict, cookies: dict):
    data = _script_json(html, "__UNIVERSAL_DATA_FOR_REHYDRATION__")
    item = None
    if data is not None:
        detail = _d(_d(_d(data).get("__DEFAULT_SCOPE__")).get("webapp.video-detail"))
        item = _d(_d(detail.get("itemInfo")).get("itemStruct")) or None
        if not item:
            item = _find_key(data, "itemStruct")
    if not item:
        sigi = _script_json(html, "SIGI_STATE")
        mods = _d(_d(sigi).get("ItemModule"))
        item = next((v for v in mods.values() if isinstance(v, dict)), None)
    return tt_post_from_item(item, headers, cookies) if item else None


def tt_from_embed(html: str, video_id: str, headers: dict, cookies: dict):
    """TikTok's embed player page: lighter and blocked less often, but videos only."""
    data = _script_json(html, "__FRONTITY_CONNECT_STATE__")
    infos = _find_key(data, "itemInfos") if data is not None else None
    if not infos:
        return None
    v = _d(infos.get("video"))
    urls = _uniq(_l(v.get("urls")))
    if not urls:
        return None
    title = str(infos.get("text") or "").strip() or f"tiktok {video_id}"
    return _post("TikTok", title, next(iter(_uniq(_l(v.get("covers")))), None), _d(v.get("videoMeta")).get("duration"),
                 [{"urls": urls}], [], None, headers, cookies)


def read_tiktok(link: str):
    sess = requests.Session()
    vid = (TT_ID.search(link) or [None, None])[1]
    for ua in (BROWSER_UA, MOBILE_UA):  # two kinds of visitor: TikTok answers them differently
        headers = {"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}
        got = get_page(link, headers, is_tiktok_host, session=sess)
        if not got:
            continue
        html, final = got
        vid = (TT_ID.search(final) or [None, vid])[1]
        media_headers = {"User-Agent": ua, "Referer": "https://www.tiktok.com/", "Accept": "*/*"}
        post = tt_from_html(html, media_headers, requests.utils.dict_from_cookiejar(sess.cookies))
        if post:
            return post
    if vid:  # last resort for videos: the embed player page
        ua = BROWSER_UA
        got = get_page(f"https://www.tiktok.com/embed/v2/{vid}", {"User-Agent": ua, "Accept-Language": "en-US,en;q=0.9"},
                       is_tiktok_host, session=sess)
        if got:
            return tt_from_embed(got[0], vid, {"User-Agent": ua, "Referer": "https://www.tiktok.com/", "Accept": "*/*"},
                                 requests.utils.dict_from_cookiejar(sess.cookies))
    return None


# ----------------------------------------------------------------------------------------------
# Instagram
# ----------------------------------------------------------------------------------------------
is_instagram_host = host_in("instagram.com")
IG_CODE = re.compile(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")


def _ig_best_image(node: dict):
    res = [r for r in _l(node.get("display_resources")) if isinstance(r, dict) and _https(r.get("src"))]
    if res:
        return max(res, key=lambda r: r.get("config_width") or 0)["src"]
    return node.get("display_url")


def ig_from_media(sm: dict, headers: dict):
    nodes = [_d(e.get("node")) for e in _l(_d(sm.get("edge_sidecar_to_children")).get("edges")) if isinstance(e, dict)] or [sm]
    videos, images = [], []
    for n in nodes:
        if n.get("is_video"):
            if _https(n.get("video_url")):
                videos.append({"urls": [n["video_url"]]})
        else:
            u = _ig_best_image(n)
            if _https(u):
                images.append({"urls": _uniq([u, n.get("display_url")])})
    cap = next(iter(_l(_d(sm.get("edge_media_to_caption")).get("edges"))), None)
    text = str(_d(_d(cap).get("node")).get("text") or "").strip()
    owner = str(_d(sm.get("owner")).get("username") or "")
    title = text or f"instagram {owner} {sm.get('shortcode') or ''}".strip()
    first = _ig_best_image(nodes[0]) if nodes else None
    return _post("Instagram", title, first, sm.get("video_duration"), videos, images, None, headers)


def ig_from_embed(html: str, headers: dict):
    """Instagram's embed page carries the post's data (including every picture of a carousel) as JSON."""
    candidates = []
    m = re.search(r'"contextJSON"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    if m:
        try:
            candidates.append(json.loads(json.loads('"' + m.group(1) + '"')))
        except ValueError:
            pass
    for m in re.finditer(r"__additionalDataLoaded\(\s*['\"][^'\"]*['\"]\s*,\s*(\{[\s\S]*?\})\s*\)\s*;", html):
        try:
            candidates.append(json.loads(m.group(1)))
        except ValueError:
            pass
    for c in candidates:
        sm = _find_key(c, "shortcode_media")
        if sm:
            post = ig_from_media(sm, headers)
            if post:
                return post
    return None


def ig_from_markup(html: str, headers: dict):
    """The embed page's plain <img>: a single picture (used when the JSON is not there)."""
    m = re.search(r"""<img[^>]*class=["'][^"']*EmbeddedMediaImage[^"']*["'][^>]*\bsrc=["']([^"']+)["']""", html, re.I) \
        or re.search(r"""<img[^>]*\bsrc=["']([^"']+)["'][^>]*class=["'][^"']*EmbeddedMediaImage""", html, re.I)
    if not m or re.search(r"""<video\b""", html, re.I):
        return None  # no picture, or the post is a video (a cover picture would be the wrong file)
    u = decode_entities(m.group(1))
    return _post("Instagram", "instagram post", u, None, [], [{"urls": [u]}], None, headers) if _https(u) else None


def ig_from_preview(html: str, headers: dict):
    """The link-preview tags of the post page. Pictures only: for a video post the cover would be the wrong file."""
    meta = _meta(html)
    if meta.get("og:video") or meta.get("og:video:secure_url") or "video" in meta.get("og:type", ""):
        return None
    u = meta.get("og:image")
    if not _https(u):
        return None
    return _post("Instagram", meta.get("og:title") or "instagram post", u, None, [], [{"urls": [u]}], None, headers)


def read_instagram(link: str):
    m = IG_CODE.search(urlparse(link).path)
    if not m:
        return None
    code = m.group(1)
    headers = {"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}
    media_headers = {"User-Agent": BROWSER_UA, "Accept": "*/*"}
    for path in (f"/p/{code}/embed/captioned/", f"/p/{code}/embed/"):
        got = get_page("https://www.instagram.com" + path, headers, is_instagram_host)
        if got:
            post = ig_from_embed(got[0], media_headers) or ig_from_markup(got[0], media_headers)
            if post:
                return post
    got = get_page(f"https://www.instagram.com/p/{code}/", {**headers, "User-Agent": BOT_UA}, is_instagram_host)
    return ig_from_preview(got[0], media_headers) if got else None


# ----------------------------------------------------------------------------------------------
# X (Twitter)
# ----------------------------------------------------------------------------------------------
X_ID = re.compile(r"/status(?:es)?/(\d+)")
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _js_base36(value: float) -> str:
    """Number.prototype.toString(36) exactly as JavaScript (V8) prints it: digits only as far as the number is precise."""
    integer = math.floor(value)
    fraction = value - integer
    delta = max(0.5 * (math.nextafter(value, math.inf) - value), 5e-324)
    frac_digits: list[int] = []
    if fraction >= delta:
        while True:
            fraction *= 36
            delta *= 36
            digit = int(fraction)
            frac_digits.append(digit)
            fraction -= digit
            if fraction > 0.5 or (fraction == 0.5 and digit & 1):
                if fraction + delta > 1:  # round up, carrying into earlier digits
                    while True:
                        if not frac_digits:
                            integer += 1
                            break
                        last = frac_digits.pop()
                        if last + 1 < 36:
                            frac_digits.append(last + 1)
                            break
                    break
            if fraction < delta:
                break
    whole = ""
    n = int(integer)
    while n:
        n, r = divmod(n, 36)
        whole = _B36[r] + whole
    return (whole or "0") + ("." + "".join(_B36[d] for d in frac_digits) if frac_digits else "")


def x_token(tweet_id: str) -> str:
    """The small token X's embed endpoint expects: ((id / 1e15) * pi) in base 36, without zeros and the dot."""
    return re.sub(r"(0+|\.)", "", _js_base36(int(tweet_id) / 1e15 * math.pi))


def _x_title(text, user, tid) -> str:
    t = re.sub(r"https?://\S+", "", str(text or "")).strip()
    return t[:80].strip() or f"x {user} {tid}".strip()


def _x_variants(variants) -> list[str]:
    mp4 = [v for v in _l(variants) if isinstance(v, dict) and "mp4" in str(v.get("content_type") or v.get("type") or "").lower()]
    mp4.sort(key=lambda v: -(v.get("bitrate") or 0))
    return _uniq([v.get("url") or v.get("src") for v in mp4])


def x_from_syndication(data: dict, headers: dict):
    videos, images = [], []
    for md in _l(data.get("mediaDetails")):
        md = _d(md)
        base = md.get("media_url_https")
        if md.get("type") == "photo" and _https(base):
            images.append({"urls": _uniq([base + ("&" if "?" in base else "?") + "name=orig", base])})
        elif md.get("type") in ("video", "animated_gif"):
            urls = _x_variants(_d(md.get("video_info")).get("variants"))
            if urls:
                videos.append({"urls": urls})
    if not images and not videos:  # older answer layout
        images = [{"urls": [p["url"]]} for p in _l(data.get("photos")) if isinstance(p, dict) and _https(p.get("url"))]
        urls = _x_variants(_d(data.get("video")).get("variants"))
        if urls:
            videos.append({"urls": urls})
    user = str(_d(data.get("user")).get("screen_name") or "")
    thumb = (images[0]["urls"][0] if images else None) or _d(data.get("video")).get("poster") \
        or next((_d(m).get("media_url_https") for m in _l(data.get("mediaDetails"))), None)
    return _post("X", _x_title(data.get("text"), user, data.get("id_str")), thumb, None, videos, images, None, headers)


def x_from_fx(data: dict, headers: dict):
    tw = _d(_d(data).get("tweet"))
    media = _d(tw.get("media"))
    images = [{"urls": [p["url"]]} for p in _l(media.get("photos")) if isinstance(p, dict) and _https(p.get("url"))]
    videos = [{"urls": [v["url"]]} for v in _l(media.get("videos")) if isinstance(v, dict) and _https(v.get("url"))]
    thumb = images[0]["urls"][0] if images else next((v.get("thumbnail_url") for v in _l(media.get("videos")) if isinstance(v, dict)), None)
    return _post("X", _x_title(tw.get("text"), _d(tw.get("author")).get("screen_name"), tw.get("id")), thumb, None, videos, images, None, headers)


def read_x(link: str):
    m = X_ID.search(urlparse(link).path)
    if not m:
        return None
    tid = m.group(1)
    headers = {"User-Agent": BROWSER_UA, "Accept": "application/json,*/*;q=0.8", "Origin": "https://platform.twitter.com",
               "Referer": "https://platform.twitter.com/"}
    media_headers = {"User-Agent": BROWSER_UA, "Accept": "*/*"}
    ok = host_in("twimg.com")
    for token in dict.fromkeys((x_token(tid), "a")):
        data = get_json(f"https://cdn.syndication.twimg.com/tweet-result?id={tid}&lang=en&token={token}", headers, ok)
        post = x_from_syndication(data, media_headers) if isinstance(data, dict) and data else None
        if post:
            return post
    if X_FALLBACK_API:
        host = urlparse(X_FALLBACK_API).hostname or ""
        data = get_json(f"{X_FALLBACK_API}/status/{tid}", {"User-Agent": "SnapIT", "Accept": "application/json"}, host_in(host))
        post = x_from_fx(data, media_headers) if isinstance(data, dict) else None
        if post:
            return post
    return None


# ----------------------------------------------------------------------------------------------
# One entry point
# ----------------------------------------------------------------------------------------------
READERS = {"TikTok": read_tiktok, "Instagram": read_instagram, "X": read_x}


def read_post(platform: str, link: str):
    """Never raises: any trouble just means 'could not read it', and the caller moves on to its next layer."""
    reader = READERS.get(platform)
    if not reader:
        return None
    try:
        return reader(link)
    except Exception as e:  # a changed page layout must not turn into a server error
        print(f"{platform} reader failed:", repr(e))
        return None


# ----------------------------------------------------------------------------------------------
# Saving the media
# ----------------------------------------------------------------------------------------------
def _save_one(url: str, dest_base: str, headers: dict, cookies: dict, kind: str, cap: int) -> str:
    try:
        r = follow(url, headers, any_host, 60, cookies=cookies or None)
    except MediaError:
        raise
    except requests.RequestException:
        raise MediaError(502, "network")
    path = None
    try:
        if not r.ok:
            raise MediaError(r.status_code, f"host answered {r.status_code}")
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype.startswith(("text/", "application/json")) or (kind == "image" and ctype.startswith(("video/", "audio/"))) \
                or (kind != "image" and ctype.startswith("image/")):
            raise MediaError(502, "not a media file")  # an error page or a picture instead of the video
        if int(r.headers.get("content-length") or 0) > cap:
            raise MediaError(413, "too large")
        ext = IMAGE_EXT.get(ctype) or (os.path.splitext(urlparse(url).path)[1].lstrip(".").lower() or "jpg") if kind == "image" else "mp4"
        if kind == "image" and ext not in ("jpg", "jpeg", "png", "webp", "gif", "heic", "avif"):
            ext = "jpg"
        path = f"{dest_base}.{ext}"
        size = 0
        try:
            with open(path + ".part", "wb") as out:
                for chunk in r.iter_content(262144):
                    size += len(chunk)
                    if size > cap:
                        raise MediaError(413, "too large")
                    out.write(chunk)
        except requests.RequestException:
            raise MediaError(502, "network")
        if size < (400 if kind == "image" else 1024):
            raise MediaError(502, "empty answer")
        os.replace(path + ".part", path)
        return path
    finally:
        r.close()
        if path and os.path.exists(path + ".part"):
            try:
                os.remove(path + ".part")
            except OSError:
                pass


def save_media(urls: list[str], dest_base: str, post: dict, kind: str, cap: int) -> str:
    """Try every address in turn (two tries each for network trouble). Raises MediaError when all fail."""
    last = MediaError(502, "no address")
    for u in urls:
        for attempt in range(2):
            try:
                return _save_one(u, dest_base, post.get("headers") or {}, post.get("cookies") or {}, kind, cap)
            except MediaError as e:
                if e.status == 413:
                    raise
                last = e
                if e.status in (400, 401, 403, 404, 410, 0):
                    break  # this address is refused or gone: no point repeating it
                time.sleep(0.8 * (attempt + 1))
            except requests.RequestException as e:
                last = MediaError(502, repr(e))
                time.sleep(0.8 * (attempt + 1))
    raise last


def build_zip(paths: list[str], dest: str) -> None:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as z:  # pictures are already compressed
        for i, p in enumerate(paths, 1):
            z.write(p, f"photo-{i:02d}{os.path.splitext(p)[1]}")


def save_zip(post: dict, folder: str, dest: str, cap: int) -> int:
    """Save every picture of a post into one ZIP. -> how many pictures made it in. Raises when none did."""
    work = os.path.join(folder, "photos")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    images = post["images"]

    def one(ix: int):
        try:
            return ix, save_media(images[ix]["urls"], os.path.join(work, f"{ix + 1:02d}"), post, "image", cap)
        except MediaError as e:
            if e.status == 413:
                raise
            return ix, None

    with ThreadPoolExecutor(max_workers=4) as pool:
        got = sorted(pool.map(one, range(len(images))), key=lambda t: t[0])
    paths = [p for _, p in got if p]
    if not paths:
        raise MediaError(502, "none of the pictures could be fetched")
    build_zip(paths, dest)
    shutil.rmtree(work, ignore_errors=True)
    return len(paths)
