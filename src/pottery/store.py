"""SQLite 持久化层：表结构、线程本地连接与事务助手。"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    role    TEXT NOT NULL,
    class_id TEXT
);

CREATE TABLE IF NOT EXISTS students (
    student_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    grade      TEXT NOT NULL,
    class_id   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS guardianships (
    guardian_id TEXT NOT NULL REFERENCES users(user_id),
    student_id  TEXT NOT NULL REFERENCES students(student_id),
    PRIMARY KEY (guardian_id, student_id)
);

CREATE TABLE IF NOT EXISTS craft_versions (
    version_id      TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    grade           TEXT NOT NULL,
    min_drying_hours INTEGER NOT NULL,
    allowed_clays   TEXT NOT NULL,          -- JSON 数组
    glaze_blocklist TEXT NOT NULL,          -- JSON 数组，元素为 [a, b] 互斥釉料对
    safety_requirements TEXT NOT NULL,      -- JSON 数组，作品登记时须逐项确认
    status          TEXT NOT NULL,          -- active / retired
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS works (
    work_id           TEXT PRIMARY KEY,
    student_id        TEXT NOT NULL REFERENCES students(student_id),
    class_id          TEXT NOT NULL,        -- 登记时班级快照
    version_id        TEXT NOT NULL REFERENCES craft_versions(version_id),
    material_batch_no TEXT NOT NULL,        -- 材料批号
    clay_code         TEXT NOT NULL,
    glaze_codes       TEXT NOT NULL,        -- JSON 数组
    storage_location  TEXT NOT NULL,
    safety_confirmed  TEXT NOT NULL,        -- JSON 数组，已确认的安全前置条件
    status            TEXT NOT NULL,
    created_by        TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    device_id         TEXT,
    client_record_id  TEXT
);
-- 同一设备重复上传同一客户端记录号时去重
CREATE UNIQUE INDEX IF NOT EXISTS uq_works_device_record
    ON works(device_id, client_record_id)
    WHERE device_id IS NOT NULL AND client_record_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS step_records (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id    TEXT NOT NULL REFERENCES works(work_id),
    step       TEXT NOT NULL,
    signed_by  TEXT NOT NULL,
    signed_at  TEXT NOT NULL,
    proxy_for  TEXT,
    proxy_reason TEXT,
    device_id  TEXT,
    client_record_id TEXT,
    UNIQUE (work_id, step)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_step_device_record
    ON step_records(device_id, client_record_id)
    WHERE device_id IS NOT NULL AND client_record_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id     TEXT NOT NULL REFERENCES works(work_id),
    reviewed_by TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    approved    INTEGER NOT NULL,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS consents (
    work_id    TEXT PRIMARY KEY REFERENCES works(work_id),
    granted    INTEGER NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    note       TEXT
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id   TEXT PRIMARY KEY,
    code       TEXT NOT NULL UNIQUE,
    capacity   INTEGER NOT NULL,
    scheduled_at TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at  TEXT,
    close_reason TEXT
);

CREATE TABLE IF NOT EXISTS batch_items (
    batch_id    TEXT NOT NULL REFERENCES batches(batch_id),
    work_id     TEXT NOT NULL REFERENCES works(work_id),
    admitted_at TEXT NOT NULL,
    outcome     TEXT,                          -- pass / fail / cancelled / failed
    PRIMARY KEY (batch_id, work_id)
);

CREATE TABLE IF NOT EXISTS dispositions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id    TEXT NOT NULL REFERENCES works(work_id),
    batch_id   TEXT NOT NULL REFERENCES batches(batch_id),
    decision   TEXT NOT NULL,                  -- rework / discard / reschedule
    reason     TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    -- 返工处置生效时已有的最新复核 id；之后的复核才算“返工后复核”
    reviews_before INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transfers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT NOT NULL REFERENCES students(student_id),
    from_class TEXT NOT NULL,
    to_class   TEXT NOT NULL,
    moved_by   TEXT NOT NULL,
    moved_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS storage_moves (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id    TEXT NOT NULL REFERENCES works(work_id),
    from_loc   TEXT NOT NULL,
    to_loc     TEXT NOT NULL,
    moved_by   TEXT NOT NULL,
    moved_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    entity    TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    kind      TEXT NOT NULL,
    payload   TEXT NOT NULL,
    actor     TEXT NOT NULL,
    at        TEXT NOT NULL
);
"""


class Store:
    """线程安全的 SQLite 存取入口。

    每个线程持有独立连接；写操作一律走 ``tx()``（BEGIN IMMEDIATE），
    依靠数据库锁串行化，保证容量类检查在并发下不失效。
    """

    def __init__(self, path: str = "pottery.db"):
        self.path = str(path)
        self._local = threading.local()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """立即事务：进入即取写锁，冲突时短暂退避重试。"""
        conn = self.conn
        last: sqlite3.OperationalError | None = None
        for attempt in range(6):
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower():
                    last = exc
                    time.sleep(0.02 * (attempt + 1))
                    continue
                raise
        else:
            raise last  # type: ignore[misc]
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def next_id(self, conn: sqlite3.Connection, name: str, prefix: str) -> str:
        conn.execute(
            "INSERT INTO counters(name, value) VALUES(?, 0) "
            "ON CONFLICT(name) DO NOTHING",
            (name,),
        )
        conn.execute("UPDATE counters SET value = value + 1 WHERE name = ?", (name,))
        value = conn.execute(
            "SELECT value FROM counters WHERE name = ?", (name,)
        ).fetchone()["value"]
        return f"{prefix}-{value:06d}"

    # -- 基础档案写入（供启动种子数据与测试使用） --

    def add_user(self, user_id: str, name: str, role: str, class_id: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO users(user_id, name, role, class_id) VALUES(?,?,?,?)",
            (user_id, name, role, class_id),
        )

    def add_student(self, student_id: str, name: str, grade: str, class_id: str) -> None:
        self.conn.execute(
            "INSERT INTO students(student_id, name, grade, class_id) VALUES(?,?,?,?)",
            (student_id, name, grade, class_id),
        )

    def add_guardianship(self, guardian_id: str, student_id: str) -> None:
        self.conn.execute(
            "INSERT INTO guardianships(guardian_id, student_id) VALUES(?,?)",
            (guardian_id, student_id),
        )

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
