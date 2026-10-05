"""Exercise real urllib transfers against a local HTTP server, without media tools."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading

import pytest

from bili_mp4.network import DownloadError, HttpDownloader, Interrupted, Resource, RetryBudget


@contextmanager
def range_server(payload: bytes):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path != "/media":
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            range_header = self.headers.get("Range")
            if_range = self.headers.get("If-Range")
            match = re.fullmatch(r"bytes=(\d+)-(\d+)", range_header or "")
            ignore_once = bool(if_range and self.server.ignore_next_chunk)
            if ignore_once:
                self.server.ignore_next_chunk = False
            if match and not ignore_once and (if_range is None or if_range == self.server.etag):
                start, end = map(int, match.groups())
                if start >= len(self.server.payload) or end < start:
                    status, body = 416, b""
                    self.send_response(status)
                    self.send_header("Content-Range", f"bytes */{len(self.server.payload)}")
                else:
                    end = min(end, len(self.server.payload) - 1)
                    status = 206
                    body = self.server.payload[start:end + 1]
                    self.send_response(status)
                    self.send_header(
                        "Content-Range", f"bytes {start}-{end}/{len(self.server.payload)}",
                    )
            else:
                status, body = 200, self.server.payload
                self.send_response(status)
            self.server.requests.append({
                "range": range_header, "if_range": if_range, "status": status,
            })
            self.send_header("ETag", self.server.etag)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # The downloader deliberately closes an ignored range response.
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.payload = payload
    server.etag = '"fixture-resource-v1"'
    server.ignore_next_chunk = False
    server.requests = []
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.05),
        name="local-range-fixture", daemon=True,
    )
    thread.start()
    try:
        host, port = server.server_address
        yield server, Resource("fixture-resource", f"http://{host}:{port}/media")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive(), "HTTP fixture did not shut down"


def pause_after_first_chunk(downloader, resource, target):
    stop = threading.Event()
    def progress(size, _total):
        if size >= downloader.chunk_size:
            stop.set()
    with pytest.raises(Interrupted):
        downloader.download(resource, target, stop, progress=progress)
    return target.with_name(target.name + ".part")


def test_real_urllib_range_download_matches_every_byte(tmp_path):
    payload = bytes(range(256)) * 128 + b"last-byte-marker"
    target = tmp_path / "actual.media"
    with range_server(payload) as (server, resource):
        downloader = HttpDownloader(timeout=3, chunk_size=2048)
        result = downloader.download(resource, target, threading.Event())
        assert result == target
        assert target.read_bytes() == payload
        assert not target.with_name(target.name + ".part").exists()
        manifest = json.loads(target.with_name(target.name + ".json").read_text("utf-8"))
        assert manifest["complete"] is True
        assert manifest["size"] == len(payload)
        assert manifest["etag"] == server.etag
        chunks = [r for r in server.requests if r["if_range"]]
        assert chunks and all(r["status"] == 206 for r in chunks)
        assert all(r["if_range"] == server.etag for r in chunks)


def test_real_urllib_pause_then_resumes_from_existing_bytes(tmp_path):
    payload = bytes(range(256)) * 64 + b"tail"
    target = tmp_path / "resumable.media"
    with range_server(payload) as (server, resource):
        downloader = HttpDownloader(timeout=3, chunk_size=2048)
        partial = pause_after_first_chunk(downloader, resource, target)
        offset = partial.stat().st_size
        assert 0 < offset < len(payload)
        assert partial.read_bytes() == payload[:offset]
        request_count = len(server.requests)
        downloader.download(resource, target, threading.Event())
        assert target.read_bytes() == payload
        resumed = [r for r in server.requests[request_count:] if r["if_range"]]
        assert resumed
        assert resumed[0]["range"].startswith(f"bytes={offset}-")
        assert resumed[0]["if_range"] == server.etag
        assert not partial.exists()


def test_real_urllib_ignored_range_discards_partial_before_restart(tmp_path):
    payload = bytes(range(256)) * 64 + b"complete-tail"
    target = tmp_path / "ignored-range.media"
    with range_server(payload) as (server, resource):
        downloader = HttpDownloader(timeout=3, chunk_size=2048)
        partial = pause_after_first_chunk(downloader, resource, target)
        offset = partial.stat().st_size
        assert offset > 0
        request_count = len(server.requests)
        server.ignore_next_chunk = True
        with pytest.raises(DownloadError, match="重试次数"):
            downloader.download(
                resource, target, threading.Event(), budget=RetryBudget(limit=0),
            )
        assert not partial.exists(), "A 200 response must never be appended"
        assert not target.exists()
        rejected = [r for r in server.requests[request_count:] if r["if_range"]]
        assert rejected[0]["status"] == 200
        assert rejected[0]["range"].startswith(f"bytes={offset}-")
        request_count = len(server.requests)
        downloader.download(resource, target, threading.Event())
        restarted = [r for r in server.requests[request_count:] if r["if_range"]]
        assert restarted[0]["range"].startswith("bytes=0-")
        assert target.read_bytes() == payload
        assert target.stat().st_size == len(payload)
