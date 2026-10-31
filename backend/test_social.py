"""Offline tests for social.py (no network needed).   Run:  cd backend && pip install pytest && pytest -q
The pages below are small copies of the structures TikTok, Instagram and X really serve. If one of those sites changes
its layout, add the new layout here first, then teach social.py about it."""
import json, os, zipfile

import pytest

import social


# ----------------------------------------------------------------------------------------------- fixtures
def tt_page(item: dict) -> str:
    data = {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"statusCode": 0, "itemInfo": {"itemStruct": item}}}}
    return f'<html><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">{json.dumps(data)}</script></html>'


TT_PHOTO = {
    "id": "7300000000000000001", "desc": "My trip", "author": {"uniqueId": "alice"},
    "imagePost": {"images": [
        {"imageURL": {"urlList": ["https://p16.tiktokcdn.com/a.webp?x=1", "https://p16.tiktokcdn.com/a.jpeg?x=1"]}},
        {"imageURL": {"urlList": ["https://p16.tiktokcdn.com/b.jpeg?x=1"]}}],
        "cover": {"imageURL": {"urlList": ["https://p16.tiktokcdn.com/c.jpeg"]}}},
    "music": {"playUrl": "https://sf16.tiktokcdn.com/m.mp3"}, "video": {"duration": 0, "playAddr": "https://sf16.tiktokcdn.com/music-only.mp3"},
}
TT_VIDEO = {
    "id": "7300000000000000002", "desc": "Dance", "author": {"uniqueId": "bob"},
    "video": {"duration": 12, "cover": "https://p16.tiktokcdn.com/cover.jpeg", "playAddr": "https://v16.tiktokcdn.com/play.mp4",
              "downloadAddr": "https://v16.tiktokcdn.com/dl.mp4",
              "bitrateInfo": [
                  {"CodecType": "bytevc1", "Bitrate": 900000, "PlayAddr": {"UrlList": ["https://v16.tiktokcdn.com/hevc.mp4"]}},
                  {"CodecType": "h264", "Bitrate": 500000, "PlayAddr": {"UrlList": ["https://v16.tiktokcdn.com/h264-hi.mp4"]}},
                  {"CodecType": "h264", "Bitrate": 300000, "PlayAddr": {"UrlList": ["https://v16.tiktokcdn.com/h264-lo.mp4"]}}]},
}


def ig_embed(sm: dict) -> str:
    ctx = {"gql_data": {"shortcode_media": sm}}
    return f'<html><script>window.__x = {{"contextJSON":{json.dumps(json.dumps(ctx))}}};</script></html>'


IG_CAROUSEL = {
    "shortcode": "Cabc123", "owner": {"username": "carol"},
    "edge_media_to_caption": {"edges": [{"node": {"text": "Weekend"}}]},
    "edge_sidecar_to_children": {"edges": [
        {"node": {"is_video": False, "display_url": "https://scontent.cdninstagram.com/1.jpg",
                  "display_resources": [{"src": "https://scontent.cdninstagram.com/1-small.jpg", "config_width": 320},
                                        {"src": "https://scontent.cdninstagram.com/1-big.jpg", "config_width": 1080}]}},
        {"node": {"is_video": True, "video_url": "https://scontent.cdninstagram.com/2.mp4", "display_url": "https://scontent.cdninstagram.com/2.jpg"}},
        {"node": {"is_video": False, "display_url": "https://scontent.cdninstagram.com/3.jpg"}}]},
}

X_TWEET = {
    "id_str": "1846987139428634858", "text": "look https://t.co/abc", "user": {"screen_name": "dave"},
    "mediaDetails": [
        {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/AAA.jpg"},
        {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/BBB.jpg"},
        {"type": "video", "media_url_https": "https://pbs.twimg.com/media/POSTER.jpg", "video_info": {"variants": [
            {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/x.m3u8"},
            {"content_type": "video/mp4", "bitrate": 256000, "url": "https://video.twimg.com/lo.mp4"},
            {"content_type": "video/mp4", "bitrate": 2176000, "url": "https://video.twimg.com/hi.mp4"}]}}],
}


# ----------------------------------------------------------------------------------------------- readers
def test_tiktok_photo_post_is_read_as_pictures():
    post = social.tt_from_html(tt_page(TT_PHOTO), {"User-Agent": "x"}, {})
    assert post["videos"] == [] and len(post["images"]) == 2
    assert post["images"][0]["urls"][0].endswith(".jpeg?x=1")  # JPEG preferred over WebP
    assert post["audio"]["urls"] == ["https://sf16.tiktokcdn.com/m.mp3"]
    assert post["title"] == "My trip" and post["thumbnail"].endswith("c.jpeg")


def test_tiktok_video_prefers_h264():
    post = social.tt_from_html(tt_page(TT_VIDEO), {}, {})
    urls = post["videos"][0]["urls"]
    assert urls[0].endswith("h264-hi.mp4") and urls[-1].endswith("hevc.mp4")
    assert post["images"] == [] and post["duration"] == 12


def test_tiktok_old_layout_and_embed_layout():
    sigi = f'<script id="SIGI_STATE" type="application/json">{json.dumps({"ItemModule": {"1": TT_VIDEO}})}</script>'
    assert social.tt_from_html(sigi, {}, {})["videos"]
    embed = {"source": {"data": {"/embed/v2/9": {"videoData": {"itemInfos": {"text": "hey", "video": {"urls": ["https://v.tiktokcdn.com/e.mp4"], "covers": ["https://c.tiktokcdn.com/c.jpg"]}}}}}}}
    html = f'<script id="__FRONTITY_CONNECT_STATE__" type="application/json">{json.dumps(embed)}</script>'
    assert social.tt_from_embed(html, "9", {}, {})["videos"][0]["urls"] == ["https://v.tiktokcdn.com/e.mp4"]


def test_tiktok_page_without_data_gives_none():
    assert social.tt_from_html("<html>captcha</html>", {}, {}) is None


def test_instagram_carousel_mixed():
    post = social.ig_from_embed(ig_embed(IG_CAROUSEL), {})
    assert [i["urls"][0] for i in post["images"]] == ["https://scontent.cdninstagram.com/1-big.jpg", "https://scontent.cdninstagram.com/3.jpg"]
    assert post["videos"][0]["urls"] == ["https://scontent.cdninstagram.com/2.mp4"]
    assert post["title"] == "Weekend"


def test_instagram_single_image_markup_and_preview_rules():
    markup = '<img class="EmbeddedMediaImage" alt="" src="https://scontent.cdninstagram.com/s.jpg?a=1&amp;b=2">'
    assert social.ig_from_markup(markup, {})["images"][0]["urls"] == ["https://scontent.cdninstagram.com/s.jpg?a=1&b=2"]
    assert social.ig_from_markup(markup + "<video src='x'></video>", {}) is None  # a video post: a cover picture would be wrong
    og = '<meta property="og:image" content="https://scontent.cdninstagram.com/o.jpg">'
    assert social.ig_from_preview(og, {})["images"]
    assert social.ig_from_preview(og + '<meta property="og:video" content="https://v/x.mp4">', {}) is None


def test_x_photos_and_video():
    post = social.x_from_syndication(X_TWEET, {})
    assert [i["urls"][0] for i in post["images"]] == ["https://pbs.twimg.com/media/AAA.jpg?name=orig", "https://pbs.twimg.com/media/BBB.jpg?name=orig"]
    assert post["videos"][0]["urls"] == ["https://video.twimg.com/hi.mp4", "https://video.twimg.com/lo.mp4"]  # best first, no .m3u8
    assert post["title"] == "look"


def test_x_fxtwitter_layout():
    data = {"tweet": {"id": "1", "text": "hi", "author": {"screen_name": "e"}, "media": {"photos": [{"url": "https://pbs.twimg.com/media/Z.jpg"}]}}}
    assert social.x_from_fx(data, {})["images"][0]["urls"] == ["https://pbs.twimg.com/media/Z.jpg"]


def test_x_without_media_is_none():
    assert social.x_from_syndication({"text": "just words", "mediaDetails": []}, {}) is None


def test_x_read_falls_back_to_second_layer(monkeypatch):
    calls = []

    def fake_json(url, headers, host_ok, timeout=20):
        calls.append(url)
        if "syndication" in url:
            return None  # X's own endpoint refuses us
        return {"tweet": {"id": "1", "text": "t", "media": {"photos": [{"url": "https://pbs.twimg.com/media/Q.jpg"}]}}}

    monkeypatch.setattr(social, "get_json", fake_json)
    post = social.read_x("https://x.com/someone/status/1846987139428634858")
    assert post["images"] and any("fxtwitter" in c for c in calls) and sum("syndication" in c for c in calls) == 2


def test_x_token_matches_javascript():
    assert social.x_token("1585341984679469056") == "3uchycv2wqc"


def test_read_post_never_raises(monkeypatch):
    monkeypatch.setitem(social.READERS, "TikTok", lambda link: 1 / 0)
    assert social.read_post("TikTok", "https://www.tiktok.com/@a/video/1") is None
    assert social.read_post("Nope", "x") is None


def test_tiktok_reader_tries_two_visitors_then_embed(monkeypatch):
    seen = []

    def fake_page(url, headers, host_ok, session=None, timeout=20, limit=0):
        seen.append((url, headers.get("User-Agent")))
        if "/embed/v2/" in url:
            embed = {"a": {"itemInfos": {"text": "e", "video": {"urls": ["https://v.tiktokcdn.com/e.mp4"]}}}}
            return f'<script id="__FRONTITY_CONNECT_STATE__">{json.dumps(embed)}</script>', url
        return "<html>blocked</html>", "https://www.tiktok.com/@a/video/7300000000000000002"

    monkeypatch.setattr(social, "get_page", fake_page)
    post = social.read_tiktok("https://vm.tiktok.com/ZMabc/")
    assert post["videos"][0]["urls"] == ["https://v.tiktokcdn.com/e.mp4"]
    assert [u for u, _ in seen][:2] == ["https://vm.tiktok.com/ZMabc/"] * 2 and seen[0][1] != seen[1][1]


# ----------------------------------------------------------------------------------------------- saving
class FakeResp:
    def __init__(self, status=200, ctype="image/jpeg", body=b"x" * 2048):
        self.status_code, self.ok, self.body = status, status < 400, body
        self.headers = {"content-type": ctype, "content-length": str(len(body))}
        self.url = "https://example.com/f"

    def iter_content(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i:i + n]

    def close(self):
        pass


def patch_follow(monkeypatch, plan: dict):
    """plan: address -> FakeResp or an exception to raise. Records the order the addresses were asked for."""
    asked = []

    def fake(url, headers, host_ok, timeout=60, session=None, cookies=None):
        asked.append(url)
        r = plan[url]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(social, "follow", fake)
    monkeypatch.setattr(social.time, "sleep", lambda s: None)
    return asked


def test_save_image_uses_backup_address_and_content_type(monkeypatch, tmp_path):
    asked = patch_follow(monkeypatch, {"https://a/1": FakeResp(403), "https://a/2": FakeResp(200, "image/webp")})
    path = social.save_media(["https://a/1", "https://a/2"], str(tmp_path / "img"), {}, "image", 10_000_000)
    assert path.endswith(".webp") and os.path.getsize(path) == 2048 and asked == ["https://a/1", "https://a/2"]


def test_save_rejects_error_pages_and_wrong_kinds(monkeypatch, tmp_path):
    patch_follow(monkeypatch, {"https://a/html": FakeResp(200, "text/html"), "https://a/img": FakeResp(200, "image/jpeg")})
    with pytest.raises(social.MediaError):
        social.save_media(["https://a/html"], str(tmp_path / "v"), {}, "video", 10_000_000)
    with pytest.raises(social.MediaError):  # a picture where a video was promised
        social.save_media(["https://a/img"], str(tmp_path / "v"), {}, "video", 10_000_000)
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".part")]


def test_save_retries_network_trouble_then_gives_up_on_size(monkeypatch, tmp_path):
    import requests
    asked = patch_follow(monkeypatch, {"https://a/flaky": requests.ConnectionError("boom"), "https://a/big": FakeResp(200, "video/mp4", b"x" * 5000)})
    with pytest.raises(social.MediaError):
        social.save_media(["https://a/flaky"], str(tmp_path / "v"), {}, "video", 10_000_000)
    assert asked == ["https://a/flaky"] * 2  # two tries, then it stops
    with pytest.raises(social.MediaError) as e:
        social.save_media(["https://a/big"], str(tmp_path / "v2"), {}, "video", 1000)
    assert e.value.status == 413


def test_zip_of_a_carousel_survives_one_missing_picture(monkeypatch, tmp_path):
    post = {"images": [{"urls": ["https://a/1"]}, {"urls": ["https://a/gone"]}, {"urls": ["https://a/3"]}], "headers": {}, "cookies": {}}
    patch_follow(monkeypatch, {"https://a/1": FakeResp(), "https://a/gone": FakeResp(404), "https://a/3": FakeResp(200, "image/png")})
    out = str(tmp_path / "all.zip")
    assert social.save_zip(post, str(tmp_path), out, 10_000_000) == 2
    assert sorted(zipfile.ZipFile(out).namelist()) == ["photo-01.jpg", "photo-02.png"]


def test_private_addresses_are_refused():
    for bad in ("http://127.0.0.1/x", "http://10.0.0.5/x", "http://localhost/x", "http://[::1]/x", "http://169.254.169.254/x"):
        with pytest.raises(social.MediaError):
            social.follow(bad, {}, social.any_host)
