import io
import json
import threading
import urllib.error
from pathlib import Path

import pytest

from bili_mp4.network import (
    DownloadError, HttpDownloader, Interrupted, RefreshRequired, Resource,
    RetryBudget, content_range, strong_etag,
)


class Response:
    def __init__(self, body, status=200, headers=None, truncate=None):
        self.body = io.BytesIO(body)
        self.status = status
        self.headers = headers or {}
        self.truncate = truncate

    def read(self, size=-1):
        if self.truncate is not None:
            size = min(size, self.truncate) if size >= 0 else self.truncate
            self.truncate = 0
        return self.body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.body.close()


class Server:
    def __init__(self, body=b"abcdefghij", etag='"one"', ranges=True):
        self.body, self.etag, self.ranges = body, etag, ranges
        self.requests = []
        self.invalid_start = False
        self.ignore_after_probe = False
        self.fail_code = None

    def __call__(self, request, timeout=20):
        range_value = request.get_header("Range")
        self.requests.append(range_value)
        if self.fail_code:
            raise urllib.error.HTTPError(request.full_url, self.fail_code, "test", {}, None)
        headers = {"ETag": self.etag} if self.etag else {}
        if range_value and self.ranges and not (self.ignore_after_probe and range_value != "bytes=0-0"):
            start, end = map(int, range_value[6:].split("-"))
            actual_start = start + 1 if self.invalid_start and start else start
            headers.update({
                "Content-Range": f"bytes {actual_start}-{end}/{len(self.body)}",
                "Content-Length": str(end - start + 1),
            })
            return Response(self.body[start:end+1], 206, headers)
        headers["Content-Length"] = str(len(self.body))
        return Response(self.body, 200, headers)


def resource():
    return Resource("BV:CID:format", "https://media.example/video?token=secret", {})


def test_complete_range_download_and_reuse_without_network(tmp_path):
    server = Server()
    destination = tmp_path / "video.media"
    downloader = HttpDownloader(server, chunk_size=4)
    assert downloader.download(resource(), destination, threading.Event()).read_bytes() == server.body
    requests = len(server.requests)
    assert downloader.download(resource(), destination, threading.Event()) == destination
    assert len(server.requests) == requests
    assert not destination.with_name("video.media.part").exists()
    assert "token" not in destination.with_name("video.media.json").read_text()


def pause_at_four(downloader, destination, stop):
    def progress(n, total):
        if n >= 4:
            stop.set()
    with pytest.raises(Interrupted):
        downloader.download(resource(), destination, stop, progress)


def test_resume_only_verified_partial_with_same_strong_etag(tmp_path):
    server = Server()
    downloader = HttpDownloader(server, chunk_size=4)
    destination = tmp_path / "video.media"
    stop = threading.Event()
    pause_at_four(downloader, destination, stop)
    assert destination.with_name("video.media.part").stat().st_size == 4
    server.requests.clear()
    stop.clear()
    downloader.download(resource(), destination, stop)
    assert "bytes=4-7" in server.requests
    assert "bytes=0-3" not in server.requests
    assert destination.read_bytes() == server.body


def test_changed_etag_restarts_instead_of_appending(tmp_path):
    server = Server()
    downloader = HttpDownloader(server, chunk_size=4)
    destination = tmp_path / "video.media"
    stop = threading.Event()
    pause_at_four(downloader, destination, stop)
    server.body, server.etag = b"0123456789", '"two"'
    server.requests.clear()
    stop.clear()
    downloader.download(resource(), destination, stop)
    assert "bytes=0-3" in server.requests
    assert destination.read_bytes() == server.body


def test_missing_etag_uses_one_full_response_and_restarts_partial(tmp_path):
    server = Server(etag=None)
    destination = tmp_path / "video.media"
    destination.with_name("video.media.part").write_bytes(b"WRONG")
    downloader = HttpDownloader(server)
    downloader.download(resource(), destination, threading.Event())
    assert server.requests == ["bytes=0-0", None]
    assert destination.read_bytes() == server.body


def test_no_range_support_downloads_full_body(tmp_path):
    server = Server(ranges=False)
    destination = tmp_path / "video.media"
    HttpDownloader(server).download(resource(), destination, threading.Event())
    assert destination.read_bytes() == server.body


def test_invalid_range_is_rejected_before_appending(tmp_path):
    server = Server()
    downloader = HttpDownloader(server, chunk_size=4)
    destination = tmp_path / "video.media"
    stop = threading.Event()
    pause_at_four(downloader, destination, stop)
    server.invalid_start = True
    stop.clear()
    with pytest.raises(DownloadError, match="范围"):
        downloader.download(resource(), destination, stop)
    assert destination.with_name("video.media.part").read_bytes() == b"abcd"
    assert not destination.exists()


def test_ignored_range_never_appends_complete_response(tmp_path):
    server = Server()
    downloader = HttpDownloader(server, chunk_size=4)
    destination = tmp_path / "video.media"
    stop = threading.Event()
    pause_at_four(downloader, destination, stop)
    server.ignore_after_probe = True
    stop.clear()
    with pytest.raises(DownloadError, match="耗尽"):
        downloader.download(resource(), destination, stop, budget=RetryBudget(limit=0))
    assert not destination.exists()
    assert not destination.with_name("video.media.part").exists()


@pytest.mark.parametrize("code", [403, 404, 410])
def test_expired_url_requests_reparse_without_exposing_url(tmp_path, code):
    server = Server()
    server.fail_code = code
    with pytest.raises(RefreshRequired) as error:
        HttpDownloader(server).download(resource(), tmp_path / "video.media", threading.Event())
    assert "secret" not in str(error.value)


def test_retries_are_bounded_without_sleep_after_budget_exhausted(tmp_path):
    server = Server()
    server.fail_code = 503
    with pytest.raises(DownloadError, match="耗尽"):
        HttpDownloader(server).download(resource(), tmp_path / "video.media", threading.Event(), budget=RetryBudget(limit=0))
    assert len(server.requests) == 1


def test_416_reprobes_and_never_assumes_completion(tmp_path):
    server = Server()
    server.fail_code = 416
    with pytest.raises(DownloadError, match="耗尽"):
        HttpDownloader(server).download(resource(), tmp_path / "video.media", threading.Event(), budget=RetryBudget(limit=0))
    assert not (tmp_path / "video.media").exists()


def test_corrupted_completed_file_is_redownloaded(tmp_path):
    server = Server()
    destination = tmp_path / "video.media"
    downloader = HttpDownloader(server, chunk_size=4)
    downloader.download(resource(), destination, threading.Event())
    destination.write_bytes(b"0123456789")
    server.requests.clear()
    downloader.download(resource(), destination, threading.Event())
    assert destination.read_bytes() == server.body
    assert server.requests


def test_cancellation_before_start_makes_no_request(tmp_path):
    server = Server()
    stop = threading.Event()
    stop.set()
    with pytest.raises(Interrupted):
        HttpDownloader(server).download(resource(), tmp_path / "video.media", stop)
    assert server.requests == []


@pytest.mark.parametrize("value", [None, "bytes 1-0/2", "bytes 0-2/2", "bytes */10", "bytes 0-1/*"])
def test_invalid_content_range(value):
    with pytest.raises(DownloadError):
        content_range(value)


def test_strong_validator_rules():
    assert strong_etag('"tag"') == '"tag"'
    assert strong_etag('W/"tag"') is None
    assert strong_etag("tag") is None
