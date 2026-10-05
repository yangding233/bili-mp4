from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from bili_mp4.domain import FormatChoice, Part, StoreError, Task
from bili_mp4.store import TaskStore


def make_task(identifier="task-1"):
    return Task(
        identifier,
        Part("BV12jup6AEVi", "123", 1, "中文标题", 90, "https://cdn.invalid/private"),
        FormatChoice("v1", "a1", 1080, 30, "avc1", "mp4a.40.2", 2000),
        "D:/视频", created_at="2026-10-05T00:00:00+00:00",
    )


def test_persistent_roundtrip_and_upsert(tmp_path):
    path = tmp_path / "中文目录" / "tasks.sqlite3"
    with TaskStore(path) as store:
        task = make_task()
        store.save(task)
        task.status = "paused"
        task.downloaded_bytes = 1000
        store.save(task)
        assert store.load_all() == [task]
    with TaskStore(path) as reopened:
        result = reopened.load_all()
        assert len(result) == 1
        assert result[0].status == "paused"
        assert result[0].downloaded_bytes == 1000


def test_media_urls_and_unknown_dynamic_attributes_are_not_persisted(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    with TaskStore(path) as store:
        task = make_task()
        task.media_url = "https://cdn.invalid/stream?token=private-token"
        task.message = "失败 https://cdn.invalid/stream?token=private-token"
        store.save(task)
    with sqlite3.connect(path) as connection:
        payload = connection.execute("SELECT payload FROM tasks").fetchone()[0]
    assert "private-token" not in payload
    assert "media_url" not in payload
    assert "https://www.bilibili.com/video/BV12jup6AEVi?p=1" in payload
    assert json.loads(payload)["message"] == "失败 [链接已隐藏]"


def test_concurrent_writes_are_serialized(tmp_path):
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        def save(index):
            task = make_task(f"task-{index}")
            task.downloaded_bytes = index
            store.save(task)
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(save, range(40)))
        tasks = store.load_all()
        assert len(tasks) == 40
        assert {task.id for task in tasks} == {f"task-{i}" for i in range(40)}


def test_closed_store_reports_error_and_close_is_idempotent(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.close()
    store.close()
    with pytest.raises(StoreError, match="关闭"):
        store.load_all()
    with pytest.raises(StoreError, match="关闭"):
        store.save(make_task())


def test_corrupt_record_is_reported_without_removing_it(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    with TaskStore(path) as store:
        store.save(make_task())
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE tasks SET payload = ? WHERE id = ?",
                ('{"part": "broken"}', "task-1"),
            )
        with pytest.raises(StoreError, match="无法读取"):
            store.load_all()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_future_schema_is_rejected(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(StoreError, match="版本不兼容"):
        TaskStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99


def test_same_second_tasks_keep_insertion_order_not_uuid_order(tmp_path):
    identifiers = ["f" * 32, "a" * 32, "9" * 32]
    with TaskStore(tmp_path / "tasks.sqlite3") as store:
        for identifier in identifiers:
            store.save(make_task(identifier))
        assert [task.id for task in store.load_all()] == identifiers
