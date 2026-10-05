"""Small SQLite task store. Expiring stream URLs are deliberately not stored."""
from __future__ import annotations

from pathlib import Path
import json
import sqlite3
import threading

from .domain import AppError, StoreError, Task, redact_message


class TaskStore:
    """Serialize database operations across UI and worker threads."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._closed = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(
                str(self.path), timeout=15, check_same_thread=False,
            )
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, self.SCHEMA_VERSION):
                raise StoreError("任务数据库版本不兼容，请保留数据库并使用兼容版本")
            with self._connection:
                self._connection.execute("PRAGMA busy_timeout = 15000")
                self._connection.execute("PRAGMA journal_mode = WAL")
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS tasks ("
                    "id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)"
                )
                self._connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
        except (OSError, sqlite3.Error, StoreError) as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            if isinstance(exc, StoreError):
                raise
            raise StoreError(f"无法打开任务数据库：{redact_message(str(exc))}") from exc

    def _ensure_open(self) -> None:
        if self._closed:
            raise StoreError("任务数据库已经关闭")

    def save(self, task: Task) -> None:
        # Whitelist the record structure rather than persisting extraction results.
        # Part.__post_init__ canonicalizes the only persisted URL to a webpage.
        payload = Task.from_dict(task.to_dict()).to_dict()
        payload["message"] = redact_message(payload.get("message", ""))
        with self._lock:
            self._ensure_open()
            try:
                encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
                with self._connection:
                    self._connection.execute(
                        "INSERT INTO tasks(id, created_at, payload) VALUES (?, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET "
                        "created_at = excluded.created_at, payload = excluded.payload",
                        (task.id, task.created_at, encoded),
                    )
            except (sqlite3.Error, TypeError, ValueError) as exc:
                raise StoreError(f"无法保存任务：{redact_message(str(exc))}") from exc

    def load_all(self) -> list[Task]:
        with self._lock:
            self._ensure_open()
            try:
                records = self._connection.execute(
                    "SELECT payload FROM tasks ORDER BY created_at, rowid"
                ).fetchall()
                return [Task.from_dict(json.loads(record[0])) for record in records]
            except (sqlite3.Error, ValueError, TypeError, AppError) as exc:
                raise StoreError(
                    "任务数据库包含无法读取的记录，请保留原文件并检查日志"
                ) from exc

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self) -> TaskStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
