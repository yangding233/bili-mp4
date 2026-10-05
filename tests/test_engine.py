import json
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from bili_mp4.domain import AppError, Part, FormatChoice, Task
from bili_mp4.engine import DownloadManager, _commit_no_overwrite, _workdir, _task_key, redact, safe_name
from bili_mp4.mux import MediaError, find_tools, inspect_media, mux_mp4, validate_media
from bili_mp4.network import atomic_json, digest_file
from bili_mp4.store import TaskStore


def sample_part():
    return Part("BV12jup6AEVi", "12345", 1, "测试视频", 2.0, "")


def sample_choice():
    return FormatChoice("video", "audio", 180, 24.0, "avc1.64000d", "mp4a.40.2", None)


@pytest.fixture
def tools():
    try:
        return find_tools()
    except AppError:
        if os.environ.get("CI"):
            pytest.fail("CI must provision ffmpeg/ffprobe; media acceptance cannot be skipped")
        pytest.skip("FFmpeg tools are not installed")


@pytest.fixture
def input_streams(tmp_path, tools):
    ffmpeg, ffprobe = tools
    video, audio = tmp_path / "video.mp4", tmp_path / "audio.m4a"
    hidden = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                    "color=c=red:s=320x180:r=24", "-t", "2", "-an", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(video)],
                   check=True, capture_output=True, creationflags=hidden)
    subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                    "sine=frequency=1000:sample_rate=48000", "-t", "2", "-vn", "-c:a", "aac", str(audio)],
                   check=True, capture_output=True, creationflags=hidden)
    return video, audio


def test_real_mux_contains_both_streams_and_decodes(tmp_path, tools, input_streams):
    video, audio = input_streams
    target = tmp_path / "with_sound.mp4"
    mux_mp4(tools[0], video, audio, target, 2.0, threading.Event())
    info = inspect_media(target, tools[1])
    validate_media(info, video=True, audio=True, expected_duration=2.0, height=180)
    hidden = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run([tools[0], "-v", "error", "-xerror", "-i", str(target),
                             "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"],
                            capture_output=True, creationflags=hidden)
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_mux_validator_rejects_silent_or_short_output():
    silent = {"format": {"duration": "100"}, "streams": [{"codec_type": "video", "height": 720}]}
    with pytest.raises(MediaError, match="音频"):
        validate_media(silent, video=True, audio=True, expected_duration=100)
    short = {"format": {"duration": "10"}, "streams": [{"codec_type": "video", "height": 720}, {"codec_type": "audio"}]}
    with pytest.raises(MediaError, match="时长"):
        validate_media(short, video=True, audio=True, expected_duration=100)


def test_final_publication_does_not_overwrite_existing_file(tmp_path):
    source, target = tmp_path / "temporary.mp4", tmp_path / "final.mp4"
    source.write_bytes(b"new")
    target.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        _commit_no_overwrite(source, target)
    assert target.read_bytes() == b"existing"
    assert source.read_bytes() == b"new"


def test_task_queue_deduplicates_and_keeps_multiple_parts(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    manager = DownloadManager(store, lambda _e: None)
    first = manager.enqueue([(sample_part(), sample_choice())], tmp_path / "out")
    assert len(first) == 1
    assert manager.enqueue([(sample_part(), sample_choice())], tmp_path / "out") == []
    second_part = Part("BV12jup6AEVi", "67890", 2, "P2", 2.0, "")
    assert len(manager.enqueue([(second_part, sample_choice())], tmp_path / "out")) == 1
    assert not manager._thread  # Enqueue does not implicitly perform network I/O.
    store.close()


def test_startup_pauses_interrupted_task_and_preserves_complete_file(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    task = Task("a" * 32, sample_part(), sample_choice(), str(tmp_path), status="merging")
    store.save(task)
    manager = DownloadManager(store, lambda _e: None)
    assert manager.tasks[task.id].status == "paused"
    assert manager._thread is None
    store.close()


def test_commit_after_crash_is_recovered_without_network(tmp_path, tools, input_streams):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    task = Task("a" * 32, sample_part(), sample_choice(), str(tmp_path), status="committing")
    folder = tmp_path / task.part.bvid
    folder.mkdir()
    final = folder / "final.mp4"
    mux_mp4(tools[0], *input_streams, final, 2.0, threading.Event())
    task.output_file = str(final)
    directory = _workdir(task)
    directory.mkdir(parents=True)
    atomic_json(directory / "commit.json", {
        "task_id": task.id, "key": list(_task_key(task)), "target": str(final),
        "size": final.stat().st_size, "sha256": digest_file(final),
    })
    store.save(task)
    manager = DownloadManager(store, lambda _e: None)
    restored = manager.tasks[task.id]
    assert restored.status == "paused"
    manager._execute(restored)
    assert restored.status == "completed"
    assert final.is_file()
    store.close()


def test_tampered_committed_file_is_not_marked_complete(tmp_path, tools):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    task = Task("b" * 32, sample_part(), sample_choice(), str(tmp_path), status="paused")
    folder = tmp_path / task.part.bvid
    folder.mkdir()
    final = folder / "final.mp4"
    final.write_bytes(b"original")
    task.output_file = str(final)
    directory = _workdir(task)
    directory.mkdir(parents=True)
    atomic_json(directory / "commit.json", {
        "task_id": task.id, "key": list(_task_key(task)), "target": str(final),
        "size": 8, "sha256": digest_file(final),
    })
    final.write_bytes(b"tampered")
    store.save(task)
    manager = DownloadManager(store, lambda _e: None)
    with pytest.raises(AppError, match="不一致"):
        manager._execute(manager.tasks[task.id])
    assert final.read_bytes() == b"tampered"
    assert manager.tasks[task.id].status != "completed"
    store.close()


@pytest.mark.parametrize("name", ["CON", "LPT1.txt", "..", "A/B:C*D", ""])
def test_windows_filenames_are_safe(name):
    cleaned = safe_name(name)
    assert cleaned and not cleaned.endswith((".", " "))
    assert not any(c in cleaned for c in '<>:"/\\|?*')
    assert not cleaned.upper().startswith(("CON", "LPT1"))


def test_logs_hide_signed_addresses():
    assert "secret" not in redact("403 https://cdn.example/file?token=secret")


@pytest.mark.parametrize("failure_site", ["inspect", "validate"])
def test_invalid_stream_manifest_is_invalidated_without_deleting_other_stream(
    tmp_path, monkeypatch, failure_site,
):
    from bili_mp4 import engine as engine_module

    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        task = Task("c" * 32, sample_part(), sample_choice(), str(tmp_path / "out"), status="paused")
        directory = _workdir(task)
        directory.mkdir(parents=True)
        other_stream = directory / "audio.media"
        other_manifest = directory / "audio.media.json"
        other_stream.write_bytes(b"valid-audio-cache")
        audio_metadata = {"complete": True, "size": other_stream.stat().st_size}
        atomic_json(other_manifest, audio_metadata)

        class Downloader:
            def download(self, resource, destination, stop, progress, notice, budget):
                destination.write_bytes(b"invalid-video-cache")
                atomic_json(destination.with_name(destination.name + ".json"), {
                    "key": resource.key, "complete": True,
                    "size": destination.stat().st_size, "sha256": digest_file(destination),
                })
                progress(destination.stat().st_size, destination.stat().st_size)
                return destination

        resolved_info = {
            "formats": [
                {"format_id": "video", "url": "https://cdn.invalid/video", "protocol": "https"},
                {"format_id": "audio", "url": "https://cdn.invalid/audio", "protocol": "https"},
            ],
        }
        fake_resolver = SimpleNamespace(resolve_formats=lambda _: (resolved_info, [task.choice]))
        manager = DownloadManager(
            store, lambda _e: None, downloader=Downloader(), resolver_module=fake_resolver,
        )
        monkeypatch.setattr(engine_module, "find_tools", lambda _: ("stub-ffmpeg", "stub-ffprobe"))

        def inspect(path, *_args):
            if failure_site == "inspect":
                raise MediaError("输入媒体无法读取")
            return {"tested_path": str(path)}

        def validate(_info, **_kwargs):
            raise MediaError("输入视频时长不一致")

        def unexpected_mux(*_args, **_kwargs):
            pytest.fail("Invalid input must not reach FFmpeg muxing")

        monkeypatch.setattr(engine_module, "inspect_media", inspect)
        monkeypatch.setattr(engine_module, "validate_media", validate)
        monkeypatch.setattr(engine_module, "mux_mp4", unexpected_mux)
        with pytest.raises(MediaError, match="缓存已失效"):
            manager._execute(task)
        assert not (directory / "video.media.json").exists()
        assert other_stream.read_bytes() == b"valid-audio-cache"
        assert json.loads(other_manifest.read_text("utf-8")) == audio_metadata
        assert (directory / "video.media").is_file()


def test_database_save_failure_blocks_queue_and_emits_safe_failure(tmp_path, monkeypatch):
    from bili_mp4.domain import StoreError

    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        first = Task("d" * 32, sample_part(), sample_choice(), str(tmp_path / "out"))
        second = Task("e" * 32, sample_part(), sample_choice(), str(tmp_path / "out"))
        store.save(first)
        store.save(second)
        events = []
        failure_emitted = threading.Event()
        executed = []

        def emit(event):
            events.append(event)
            if event.get("type") == "task" and "数据库" in event["task"]["message"]:
                failure_emitted.set()

        manager = DownloadManager(store, emit)

        def broken_save(_task):
            raise StoreError("database full https://cdn.invalid/stream?token=save-secret")

        def execute(task):
            executed.append(task.id)
            raise AppError("task failed https://cdn.invalid/stream?token=execute-secret")

        monkeypatch.setattr(store, "save", broken_save)
        monkeypatch.setattr(manager, "_execute", execute)
        manager.start()
        try:
            assert failure_emitted.wait(timeout=3), "Storage failure was not reported"
            assert manager._storage_blocked is True
            assert manager.tasks[first.id].status == "failed"
            assert manager.tasks[second.id].status == "queued"
            assert executed == [first.id]
        finally:
            assert manager.shutdown(timeout=3)
        emitted = json.dumps(events, ensure_ascii=False)
        assert "save-secret" not in emitted
        assert "execute-secret" not in emitted
        assert "cdn.invalid" not in emitted
        assert "队列已暂停" in emitted
        assert any(event.get("type") == "log" for event in events)


def test_real_manager_closes_loop_and_recovers_commit_without_redownload(
    tmp_path, tools, input_streams,
):
    video, audio = input_streams
    choice = sample_choice()
    resolution_calls = []
    download_calls = []

    def resolve(part):
        resolution_calls.append((part.bvid, part.cid, part.index))
        return {
            "id": f"{part.bvid}_p{part.index}",
            "extractor_key": "BiliBili", "duration": part.duration,
            "http_headers": {},
            "formats": [
                {
                    "format_id": choice.video_id, "url": "https://fixture.invalid/video",
                    "protocol": "https", "ext": "mp4", "height": choice.height,
                    "fps": choice.fps, "vcodec": choice.vcodec, "acodec": "none",
                },
                {
                    "format_id": choice.audio_id, "url": "https://fixture.invalid/audio",
                    "protocol": "https", "ext": "m4a", "vcodec": "none",
                    "acodec": choice.acodec,
                },
            ],
        }, [choice]

    class CopyDownloader:
        def download(self, resource, destination, stop, progress, notice, budget):
            identifier = resource.key.rsplit(":", 1)[-1]
            download_calls.append(identifier)
            source = video if identifier == choice.video_id else audio
            destination.write_bytes(source.read_bytes())
            size = destination.stat().st_size
            atomic_json(destination.with_name(destination.name + ".json"), {
                "key": resource.key, "complete": True,
                "size": size, "sha256": digest_file(destination),
            })
            progress(size, size)
            return destination

    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        manager = DownloadManager(
            store, lambda _e: None,
            tool_dir=str(Path(tools[0]).parent),
            downloader=CopyDownloader(),
            resolver_module=SimpleNamespace(resolve_formats=resolve),
        )
        task = manager.enqueue([(sample_part(), choice)], tmp_path / "output")[0]
        manager._execute(task)
        final = Path(task.output_file)
        directory = _workdir(task)
        receipt = directory / "commit.json"
        assert task.status == "completed"
        assert final.is_file()
        validate_media(
            inspect_media(final, tools[1]), video=True, audio=True,
            expected_duration=sample_part().duration, height=choice.height,
        )
        assert receipt.is_file()
        publication = json.loads(receipt.read_text("utf-8"))
        assert publication["target"] == str(final)
        assert publication["sha256"] == digest_file(final)
        assert not (directory / "video.media").exists()
        assert not (directory / "audio.media").exists()
        assert not (directory / "video.media.json").exists()
        assert not (directory / "audio.media.json").exists()
        assert resolution_calls == [(task.part.bvid, task.part.cid, task.part.index)]
        assert download_calls == [choice.video_id, choice.audio_id]
        manager._execute(task)
        assert resolution_calls == [(task.part.bvid, task.part.cid, task.part.index)]
        assert download_calls == [choice.video_id, choice.audio_id]
        assert task.status == "completed"
        assert store.load_all()[0].status == "completed"
        assert final.is_file()


def test_failed_mux_retries_offline_using_verified_input_cache(
    tmp_path, tools, input_streams, monkeypatch,
):
    from bili_mp4 import engine as engine_module

    video, audio = input_streams
    choice = sample_choice()
    resolutions = []
    downloads = []

    def resolve(part):
        resolutions.append(part.index)
        return {
            "formats": [
                {"format_id": "video", "url": "https://fixture.invalid/video", "protocol": "https"},
                {"format_id": "audio", "url": "https://fixture.invalid/audio", "protocol": "https"},
            ],
        }, [choice]

    class CopyDownloader:
        def download(self, resource, destination, stop, progress, notice, budget):
            identifier = resource.key.rsplit(":", 1)[-1]
            downloads.append(identifier)
            source = video if identifier == "video" else audio
            destination.write_bytes(source.read_bytes())
            size = destination.stat().st_size
            atomic_json(destination.with_name(destination.name + ".json"), {
                "key": resource.key, "complete": True,
                "size": size, "sha256": digest_file(destination),
            })
            progress(size, size)
            return destination

    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        fake_resolver = SimpleNamespace(resolve_formats=resolve)
        downloader = CopyDownloader()
        manager = DownloadManager(
            store, lambda _e: None, tool_dir=str(Path(tools[0]).parent),
            downloader=downloader, resolver_module=fake_resolver,
        )
        task = manager.enqueue([(sample_part(), choice)], tmp_path / "output")[0]
        original_mux = engine_module.mux_mp4

        def fail_mux(*_args, **_kwargs):
            raise MediaError("模拟一次封装失败")

        monkeypatch.setattr(engine_module, "mux_mp4", fail_mux)
        with pytest.raises(MediaError, match="封装失败"):
            manager._execute(task)
        directory = _workdir(task)
        assert (directory / "video.media").is_file()
        assert (directory / "audio.media").is_file()
        assert (directory / "video.media.json").is_file()
        assert (directory / "audio.media.json").is_file()
        assert resolutions == [1]
        assert downloads == ["video", "audio"]

        def unavailable_network(*_args, **_kwargs):
            pytest.fail("Re-muxing complete verified inputs must not access the network")

        monkeypatch.setattr(fake_resolver, "resolve_formats", unavailable_network)
        monkeypatch.setattr(downloader, "download", unavailable_network)
        monkeypatch.setattr(engine_module, "mux_mp4", original_mux)
        manager._execute(task)
        assert task.status == "completed"
        final = Path(task.output_file)
        assert final.is_file()
        validate_media(
            inspect_media(final, tools[1]), video=True, audio=True,
            expected_duration=2.0, height=choice.height,
        )
        assert (directory / "commit.json").is_file()
        assert not (directory / "video.media").exists()
        assert not (directory / "audio.media").exists()
        assert resolutions == [1]
        assert downloads == ["video", "audio"]
