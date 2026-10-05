from dataclasses import fields
import pytest

from bili_mp4.domain import (
    FormatChoice, InputError, Part, Task, redact_message,
)


def make_part(**changes):
    data = dict(
        bvid="BV12jup6AEVi", cid="12345", index=2,
        title="中文分 P", duration=90.3,
        url="https://signed.cdn.invalid/video?token=private",
    )
    data.update(changes)
    return Part(**data)


def make_choice():
    return FormatChoice("avc", "aac", 1080, 30, "avc1.640028", "mp4a.40.2", 1000)


def test_part_canonicalizes_url_and_cid():
    part = make_part(cid=12345)
    assert part.cid == "12345"
    assert part.url == "https://www.bilibili.com/video/BV12jup6AEVi?p=2"
    assert "token" not in str(part.to_dict())


@pytest.mark.parametrize("changes", [
    {"bvid": "av123"}, {"cid": "0"}, {"cid": "1.2"},
    {"index": 0}, {"index": True}, {"duration": -1},
    {"duration": float("nan")}, {"duration": float("inf")},
])
def test_part_rejects_invalid_identity_or_duration(changes):
    with pytest.raises(InputError):
        make_part(**changes)


def test_unknown_duration_is_serializable():
    part = make_part(duration=None)
    assert Part.from_dict(part.to_dict()).duration is None


def test_task_roundtrip_preserves_progress_and_nested_records():
    task = Task(
        id="task-1", part=make_part(), choice=make_choice(),
        output_dir="D:/视频", status="paused", downloaded_bytes=320,
        total_bytes=1000, speed=10, eta=68, message="等待继续",
    )
    encoded = task.to_dict()
    restored = Task.from_dict(encoded)
    assert restored == task
    assert isinstance(restored.part, Part)
    assert isinstance(restored.choice, FormatChoice)
    encoded["part"]["title"] = "changed"
    assert task.part.title == "中文分 P"
    assert task.created_at.endswith("+00:00")


def test_task_from_old_minimal_record_gets_defaults():
    data = {
        "id": "older", "part": make_part().to_dict(),
        "choice": make_choice().to_dict(), "output_dir": "D:/视频",
    }
    task = Task.from_dict(data)
    assert task.status == "queued"
    assert task.downloaded_bytes == 0


def test_unknown_task_state_is_rejected():
    task = Task("task", make_part(), make_choice(), "D:/视频")
    encoded = task.to_dict()
    encoded["status"] = "some_unknown_future_state"
    with pytest.raises(InputError, match="状态"):
        Task.from_dict(encoded)


def test_progressive_choice_has_single_format_selector():
    choice = FormatChoice("combined", None, 720, None, "h264", "aac", None)
    assert choice.format_selector == "combined"
    assert make_choice().format_selector == "avc+aac"
    assert FormatChoice.from_dict(choice.to_dict()) == choice


def test_redaction_removes_signed_urls():
    text = "失败 https://cdn.invalid/file?token=secret 和 http://other.invalid/x"
    redacted = redact_message(text)
    assert "secret" not in redacted
    assert "cdn.invalid" not in redacted
    assert redacted.count("[链接已隐藏]") == 2
