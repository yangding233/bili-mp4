import gzip
import io
import json
import sys
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from bili_mp4 import resolver
from bili_mp4.domain import (
    DependencyError, FormatUnavailableError, InputError, Part, ResolveError,
)

BV = "BV12jup6AEVi"
BASE = f"https://www.bilibili.com/video/{BV}"


@pytest.mark.parametrize(("value", "expected"), [
    (BV, BASE),
    (f"  {BASE}/?p=2&spm_id_from=foo#reply  ", f"{BASE}?p=2"),
    (f"http://www.bilibili.com/video/{BV}?p=01", f"{BASE}?p=1"),
    (f"www.bilibili.com/video/{BV}", BASE),
])
def test_normalize_valid_urls(value, expected):
    assert resolver.normalize_url(value) == expected


@pytest.mark.parametrize("value", [
    "https://evil.invalid/video/BV12jup6AEVi",
    "https://www.bilibili.com.evil.invalid/video/BV12jup6AEVi",
    f"https://evil@www.bilibili.com/video/{BV}",
    f"{BASE}?p=0", f"{BASE}?p=-1", f"{BASE}?p=1.5",
    f"{BASE}?p=", f"{BASE}?p=1&p=2",
    f"https://www.bilibili.com:8443/video/{BV}",
    f"https://www.bilibili.com/cheese/{BV}", "av12345",
    f"https://www.bilibili.com/video/{BV}/extra",
    f"ftp://www.bilibili.com/video/{BV}",
])
def test_normalize_rejects_wrong_hosts_and_invalid_parts(value):
    with pytest.raises(InputError):
        resolver.normalize_url(value)


def metadata():
    return {
        "bvid": BV, "title": "视频标题",
        "pages": [
            {"page": 1, "cid": 101, "part": "第一集", "duration": 90},
            {"page": 2, "cid": 102, "part": "第二集", "duration": 120},
        ],
    }


@pytest.fixture(autouse=True)
def clear_metadata_cache(monkeypatch):
    monkeypatch.setattr(resolver, "_METADATA_CACHE", {})


def test_parts_lists_one_bv_and_keeps_full_identity(monkeypatch):
    calls = []
    def fetch(bvid):
        calls.append(bvid)
        return metadata()
    monkeypatch.setattr(resolver, "_fetch_metadata", fetch)
    monkeypatch.setattr(resolver, "_extract", lambda *a, **k: pytest.fail("Listing P must not request playback formats"))
    parts = resolver.resolve_parts(f"{BASE}?p=2")
    assert calls == [BV]
    assert [(p.index, p.cid, p.title) for p in parts] == [
        (1, "101", "第一集"), (2, "102", "第二集"),
    ]
    assert parts[1].url == f"{BASE}?p=2"


def test_page_rejects_video_identity_change():
    data = metadata()
    data["bvid"] = "BV1bK411W797"
    with pytest.raises(ResolveError, match="身份"):
        resolver._metadata_from_page(page_for(data), BV)


def test_parts_rejects_missing_entries(monkeypatch):
    data = metadata()
    data["pages"] = []
    monkeypatch.setattr(resolver, "_fetch_metadata", lambda _: data)
    with pytest.raises(ResolveError, match="分 P"):
        resolver.resolve_parts(BASE)


def test_page_rejects_unsupported_content_type():
    page = "window.__INITIAL_STATE__=" + json.dumps({"videoInfo": metadata()})
    with pytest.raises(ResolveError, match="普通 BV"):
        resolver._metadata_from_page(page, BV)


def test_parts_rejects_nonexistent_requested_part(monkeypatch):
    monkeypatch.setattr(resolver, "_fetch_metadata", lambda _: metadata())
    with pytest.raises(InputError, match="不存在"):
        resolver.resolve_parts(f"{BASE}?p=3")


def format_info():
    def fmt(identifier, **kwargs):
        return {
            "format_id": identifier, "url": f"https://cdn.invalid/{identifier}",
            "protocol": "https", "ext": "mp4", **kwargs,
        }
    return {
        "id": f"{BV}_p2", "extractor_key": "BiliBili",
        "webpage_url": f"{BASE}?p=2", "duration": 120,
        "formats": [
            fmt("h264-720", vcodec="avc1.64001F", acodec="none", height=720, fps=30, filesize=1000),
            fmt("h264-1080", vcodec="avc1.640028", acodec="none", height=1080, fps=60, filesize=2000),
            fmt("av1-2160", vcodec="av01.0.12M.08", acodec="none", height=2160, fps=60, filesize=3000),
            fmt("audio-low", ext="m4a", vcodec="none", acodec="mp4a.40.2", abr=128, filesize=100),
            fmt("audio-high", ext="m4a", vcodec="none", acodec="mp4a.40.2", abr=192, filesize=200),
            fmt("audio-flac", ext="flac", vcodec="none", acodec="flac", abr=1000, filesize=1000),
        ],
    }


def test_choice_prefers_h264_and_best_aac():
    choices = resolver.choices_from_info(format_info())
    selected = resolver.select_choice(choices)
    assert selected.video_id == "h264-1080"
    assert selected.audio_id == "audio-high"
    assert selected.estimated_bytes == 2200
    assert resolver.select_choice(choices, 2160).video_id == "av1-2160"


def test_explicit_height_does_not_silently_downgrade():
    choices = resolver.choices_from_info(format_info())
    with pytest.raises(FormatUnavailableError, match="1440p"):
        resolver.select_choice(choices, 1440)


def test_filter_excludes_drm_fragmented_unknown_codec_and_missing_audio():
    info = format_info()
    video = info["formats"][0]
    info["formats"] = [
        {**video, "format_id": "drm", "has_drm": True},
        {**video, "format_id": "fragments", "fragments": [{"url": "https://cdn.invalid/part"}]},
        {**video, "format_id": "unknown", "vcodec": "unknown"},
        {**video, "format_id": "hls", "protocol": "m3u8_native"},
        video,
    ]
    assert resolver.choices_from_info(info) == []


def test_progressive_mp4_with_explicit_codecs_is_supported():
    info = format_info()
    info["formats"] = [{
        **info["formats"][0], "format_id": "combined",
        "acodec": "mp4a.40.2",
    }]
    choice = resolver.choices_from_info(info)[0]
    assert choice.audio_id is None
    assert choice.estimated_bytes == 1000


def test_format_resolution_fetches_only_selected_p(monkeypatch):
    calls = []
    def extract(url, *, flat):
        calls.append((url, flat))
        return format_info()
    monkeypatch.setattr(resolver, "_extract", extract)
    monkeypatch.setattr(resolver, "_fetch_metadata", lambda _: metadata())
    part = Part(BV, "102", 2, "第二集", 120, f"{BASE}?p=2")
    info, choices = resolver.resolve_formats(part)
    assert calls == [(f"{BASE}?p=2", False)]
    assert info["id"] == f"{BV}_p2"
    assert choices


def test_format_resolution_rejects_cid_change(monkeypatch):
    monkeypatch.setattr(resolver, "_fetch_metadata", lambda _: metadata())
    part = Part(BV, "999", 2, "第二集", 120, f"{BASE}?p=2")
    with pytest.raises(ResolveError, match="身份"):
        resolver.resolve_formats(part)


def test_format_resolution_rejects_preview_duration(monkeypatch):
    info = format_info()
    info["duration"] = 30
    monkeypatch.setattr(resolver, "_extract", lambda *a, **k: info)
    monkeypatch.setattr(resolver, "_fetch_metadata", lambda _: metadata())
    part = Part(BV, "102", 2, "第二集", 120, f"{BASE}?p=2")
    with pytest.raises(ResolveError, match="试看"):
        resolver.resolve_formats(part)


def test_format_resolution_rejects_different_p_identity(monkeypatch):
    info = format_info()
    info["id"] = f"{BV}_p1"
    monkeypatch.setattr(resolver, "_extract", lambda *a, **k: info)
    monkeypatch.setattr(resolver, "_fetch_metadata", lambda _: metadata())
    part = Part(BV, "102", 2, "第二集", 120, f"{BASE}?p=2")
    with pytest.raises(ResolveError, match="身份"):
        resolver.resolve_formats(part)


def test_extract_passes_noplaylist_and_rejects_preview_warning(monkeypatch):
    captured = {}
    class FakeDownloader:
        def __init__(self, options):
            captured.update(options)
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def extract_info(self, url, download):
            assert download is False
            captured["logger"].warning("only the preview will be extracted")
            return format_info()
    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=FakeDownloader))
    with pytest.raises(ResolveError, match="试看"):
        resolver._extract(f"{BASE}?p=2", flat=False)
    assert captured["noplaylist"] is True
    assert captured["allowed_extractors"] == ["BiliBili"]
    assert captured["cookiefile"] is None


def test_missing_dependency_is_actionable(monkeypatch):
    monkeypatch.setitem(sys.modules, "yt_dlp", None)
    with pytest.raises(DependencyError, match="yt-dlp"):
        resolver._extract(BASE, flat=True)


def page_for(data):
    return '<script>window.__INITIAL_STATE__ = ' + json.dumps({"videoData": data}, ensure_ascii=False) + '; otherScript();</script>'


class PageResponse(io.BytesIO):
    def __init__(self, body, *, encoding="identity", url=BASE + "/"):
        super().__init__(body)
        self.headers = {"Content-Encoding": encoding}
        self.url = url

    def geturl(self):
        return self.url


@pytest.mark.parametrize("encoding", ["identity", "gzip"])
def test_public_page_metadata_reads_gzip_and_reuses_bounded_cache(monkeypatch, encoding):
    data = metadata()
    data["title"] = '中文标题 with } and "quotes"'
    data["playurl"] = "https://cdn.invalid/media?token=secret"
    body = page_for(data).encode("utf-8")
    if encoding == "gzip":
        body = gzip.compress(body)
    calls = []
    def open_page(request, *, timeout):
        calls.append(request.full_url)
        assert request.full_url == BASE + "/"
        return PageResponse(body, encoding=encoding)
    monkeypatch.setattr(resolver, "urlopen", open_page)
    first = resolver._fetch_metadata(BV)
    first["pages"][0]["cid"] = 999
    again = resolver._fetch_metadata(BV)
    assert again["pages"][0]["cid"] == 101
    assert again["title"] == data["title"]
    assert "playurl" not in again
    assert calls == [BASE + "/"]
    stamp, cached = resolver._METADATA_CACHE[BV]
    resolver._METADATA_CACHE[BV] = (stamp - resolver._METADATA_TTL - 1, cached)
    resolver._fetch_metadata(BV)
    assert calls == [BASE + "/", BASE + "/"]


def test_public_page_rejects_redirects_before_reading_metadata(monkeypatch):
    monkeypatch.setattr(resolver, "urlopen", lambda *a, **k: PageResponse(
        page_for(metadata()).encode(), url="https://www.bilibili.com/bangumi/play/ep123"))
    with pytest.raises(ResolveError, match="重定向"):
        resolver._fetch_metadata(BV)


def test_public_page_enforces_decompressed_size_limit(monkeypatch):
    monkeypatch.setattr(resolver, "_MAX_METADATA_BYTES", 1024)
    monkeypatch.setattr(resolver, "urlopen", lambda *a, **k: PageResponse(
        gzip.compress(b"x" * 1025), encoding="gzip"))
    with pytest.raises(ResolveError, match="过大"):
        resolver._fetch_metadata(BV)


@pytest.mark.parametrize("page, expected", [
    ("window._riskdata_={}", "请求验证"),
    ("<html>unexpected page</html>", "结构不兼容"),
    ("window.__INITIAL_STATE__ = invalid json", "元信息无效"),
    ('window.__INITIAL_STATE__={"error":{"trueCode":-403}}', "权限"),
    ('window.__INITIAL_STATE__={"error":{"trueCode":-404}}', "删除"),
    ('window.__INITIAL_STATE__={"error":{"trueCode":-352}}', "-352"),
])
def test_public_page_errors_have_distinct_causes(page, expected):
    with pytest.raises(ResolveError, match=expected):
        resolver._metadata_from_page(page, BV)


@pytest.mark.parametrize("field, value, expected", [
    ("is_upower_exclusive", True, "充电"),
    ("is_preview", True, "完整"),
    ("has_drm", True, "DRM"),
    ("rights", {"is_stein_gate": True}, "互动"),
])
def test_public_page_still_rejects_unsupported_access(field, value, expected):
    data = metadata()
    data[field] = value
    with pytest.raises(ResolveError, match=expected):
        resolver._metadata_from_page(page_for(data), BV)


@pytest.mark.parametrize("status, expected", [(412, "请求校验"), (429, "过于频繁"), (403, "权限"), (404, "删除")])
def test_http_failures_are_distinct_and_include_stage(monkeypatch, status, expected):
    def fail(*args, **kwargs):
        raise HTTPError("https://cdn.invalid/?token=secret", status, "denied", {}, None)
    monkeypatch.setattr(resolver, "urlopen", fail)
    with pytest.raises(ResolveError) as raised:
        resolver._fetch_metadata(BV)
    message = str(raised.value)
    assert expected in message
    assert f"HTTP {status}" in message
    assert "获取视频页面" in message
    assert "token" not in message and "secret" not in message
    assert BV not in resolver._METADATA_CACHE


def test_error_numbers_in_video_ids_or_urls_are_not_http_statuses():
    error = Exception("[BiliBili] BV1xx412abcd: parsing failed https://cdn.invalid/403?token=secret")
    assert resolver._http_status(error) is None
    message = str(resolver._request_failure("播放格式解析", error))
    assert "权限" not in message and "请求校验" not in message
    assert "token" not in message and "secret" not in message


def test_nested_extractor_http_error_is_preserved():
    cause = HTTPError("https://cdn.invalid/private", 412, "denied", {}, None)
    extractor = RuntimeError("wrapped extractor error")
    extractor.cause = cause
    outer = RuntimeError("download error")
    outer.exc_info = (type(extractor), extractor, None)
    assert resolver._http_status(outer) == 412
