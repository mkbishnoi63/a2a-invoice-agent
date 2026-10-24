from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._sql_lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init()
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._key_guard = asyncio.Lock()

    def _init(self) -> None:
        with self._sql_lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    principal TEXT NOT NULL,
                    context_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_principal ON tasks(principal, created_at);
                CREATE TABLE IF NOT EXISTS messages (
                    principal TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    message_hash TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    PRIMARY KEY (principal, message_id)
                );
                CREATE TABLE IF NOT EXISTS decision_cache (
                    content_hash TEXT PRIMARY KEY,
                    decision TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._sql_lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    async def lock(self, key: str):
        async with self._key_guard:
            lock = self._key_locks.setdefault(key, asyncio.Lock())
        return lock

    def get_task(self, principal: str, task_id: str) -> dict[str, Any] | None:
        with self._sql_lock:
            row = self._conn.execute(
                "SELECT payload FROM tasks WHERE task_id = ? AND principal = ?",
                (task_id, principal),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["payload"])

    def task_exists_any(self, task_id: str) -> bool:
        with self._sql_lock:
            row = self._conn.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return row is not None

    def list_tasks(self, principal: str) -> list[dict[str, Any]]:
        with self._sql_lock:
            rows = self._conn.execute(
                "SELECT payload FROM tasks WHERE principal = ? ORDER BY created_at ASC",
                (principal,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def put_task(self, principal: str, task: dict[str, Any], created_at: str) -> None:
        payload = json.dumps(task, ensure_ascii=False, separators=(",", ":"))
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO tasks(task_id, principal, context_id, state, payload, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    state = excluded.state,
                    payload = excluded.payload
                """,
                (
                    task["id"],
                    principal,
                    task.get("contextId") or "",
                    (task.get("status") or {}).get("state") or "",
                    payload,
                    created_at,
                ),
            )

    def get_message_record(self, principal: str, message_id: str) -> dict[str, str] | None:
        with self._sql_lock:
            row = self._conn.execute(
                "SELECT message_hash, task_id FROM messages WHERE principal = ? AND message_id = ?",
                (principal, message_id),
            ).fetchone()
        if not row:
            return None
        return {"message_hash": row["message_hash"], "task_id": row["task_id"]}

    def put_message_record(
        self, principal: str, message_id: str, message_hash: str, task_id: str
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO messages(principal, message_id, message_hash, task_id)
                VALUES(?, ?, ?, ?)
                """,
                (principal, message_id, message_hash, task_id),
            )

    def get_cached_decision(self, content_hash: str) -> dict[str, Any] | None:
        with self._sql_lock:
            row = self._conn.execute(
                "SELECT decision FROM decision_cache WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["decision"])

    def put_cached_decision(self, content_hash: str, decision: dict[str, Any]) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO decision_cache(content_hash, decision)
                VALUES(?, ?)
                ON CONFLICT(content_hash) DO UPDATE SET decision = excluded.decision
                """,
                (content_hash, json.dumps(decision, ensure_ascii=False, separators=(",", ":"))),
            )
