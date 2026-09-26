"""SQLite 持久化层：建表脚本与事务管理。

写事务统一使用 BEGIN IMMEDIATE，立刻获取 RESERVED 锁，
保证并发排批时容量计数在所有事务间串行化。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

-- 平台账户（教职工与监护人）。学生不登录。
CREATE TABLE IF NOT EXISTS users (
    id           TEXT PRIMARY KEY,
    role         TEXT NOT NULL,
    display_name TEXT NOT NULL,
    token        TEXT NOT NULL UNIQUE
);

-- 监护关系：监护人只能操作对应学生的授权。
CREATE TABLE IF NOT EXISTS guardian_links (
    guardian_id TEXT NOT NULL REFERENCES users(id),
    student_id  TEXT NOT NULL REFERENCES students(id),
    PRIMARY KEY (guardian_id, student_id)
);

-- 学生（未成年人 PII 仅限鉴权后的内部视图）。
CREATE TABLE IF NOT EXISTS students (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    grade       TEXT NOT NULL,
    class_group TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

-- 工艺版本：按年级适用，作品钉选创建时的有效精确版本。
CREATE TABLE IF NOT EXISTS craft_versions (
    code             TEXT NOT NULL,
    version          INTEGER NOT NULL,
    grade            TEXT NOT NULL,
    name             TEXT NOT NULL,
    min_drying_hours REAL NOT NULL,
    glaze_family     TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active',  -- active / superseded
    created_by       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (code, version)
);

-- 有序安全前置条件。
CREATE TABLE IF NOT EXISTS craft_safety (
    code        TEXT NOT NULL,
    version     INTEGER NOT NULL,
    seq         INTEGER NOT NULL,
    requirement TEXT NOT NULL,
    PRIMARY KEY (code, version, seq),
    FOREIGN KEY (code, version) REFERENCES craft_versions(code, version)
);

-- 釉料兼容的额外放行规则（族不同但经验证相容时由传承人登记）。
CREATE TABLE IF NOT EXISTS glaze_compat (
    family        TEXT NOT NULL,
    material_code TEXT NOT NULL,
    PRIMARY KEY (family, material_code)
);

-- 材料批号（陶泥/釉料），含按年级的材料禁忌。
CREATE TABLE IF NOT EXISTS material_lots (
    material_code   TEXT NOT NULL,
    lot_no          TEXT NOT NULL,
    kind            TEXT NOT NULL,
    name            TEXT NOT NULL,
    glaze_family    TEXT,
    forbidden_grades TEXT NOT NULL DEFAULT '',  -- 逗号分隔
    supplier        TEXT NOT NULL DEFAULT '',
    received_at     TEXT NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (material_code, lot_no)
);

-- 作品：钉选工艺版本与创建时年级快照。
CREATE TABLE IF NOT EXISTS works (
    id               TEXT PRIMARY KEY,
    public_code      TEXT NOT NULL UNIQUE,
    student_id       TEXT NOT NULL REFERENCES students(id),
    grade_snapshot   TEXT NOT NULL,
    class_snapshot   TEXT NOT NULL,
    craft_code       TEXT NOT NULL,
    craft_version    INTEGER NOT NULL,
    current_step     TEXT,                  -- 已完成的最高工序
    status           TEXT NOT NULL,
    storage_location TEXT,
    review_passed    INTEGER NOT NULL DEFAULT 0,
    reviewed_by      TEXT,
    reviewed_at      TEXT,
    created_by       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    FOREIGN KEY (craft_code, craft_version) REFERENCES craft_versions(code, version)
);

-- 每道工序的签认记录（含材料批号、代签信息与复核）。
CREATE TABLE IF NOT EXISTS work_steps (
    work_id        TEXT NOT NULL REFERENCES works(id),
    step           TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    material_code  TEXT,
    material_lot   TEXT,
    signed_by      TEXT NOT NULL,
    signed_for     TEXT,                    -- 代签时的实际责任人
    signed_at      TEXT NOT NULL,
    note           TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (work_id, step)
);

-- 作品创建时的安全前置确认留痕。
CREATE TABLE IF NOT EXISTS work_safety_acks (
    work_id     TEXT NOT NULL REFERENCES works(id),
    seq         INTEGER NOT NULL,
    acked_by    TEXT NOT NULL,
    acked_at    TEXT NOT NULL,
    PRIMARY KEY (work_id, seq)
);

-- 展示授权（监护人授予/撤回），撤回立即影响公开视图。
CREATE TABLE IF NOT EXISTS consents (
    work_id       TEXT PRIMARY KEY REFERENCES works(id),
    status        TEXT NOT NULL,             -- granted / withdrawn
    granted_by    TEXT NOT NULL,
    granted_at    TEXT NOT NULL,
    withdrawn_at  TEXT,
    updated_at    TEXT NOT NULL
);

-- 窑次。取消/失败后行与成员记录全部保留作为证据。
CREATE TABLE IF NOT EXISTS kilns (
    batch_code   TEXT PRIMARY KEY,
    capacity     INTEGER NOT NULL,
    status       TEXT NOT NULL,              -- planned/firing/done/qc_passed/canceled/qc_failed
    note         TEXT NOT NULL DEFAULT '',
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    qc_by        TEXT,
    qc_at        TEXT,
    qc_result    TEXT
);

-- 窑次成员（追加保留，不随取消/失败删除）。
CREATE TABLE IF NOT EXISTS kiln_members (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_code TEXT NOT NULL REFERENCES kilns(batch_code),
    work_id    TEXT NOT NULL REFERENCES works(id),
    added_by   TEXT NOT NULL,
    added_at   TEXT NOT NULL,
    removed_at TEXT,                         -- 处置完成后填写
    UNIQUE (batch_code, work_id)
);

-- 失败/取消窑次的逐件处置决定。
CREATE TABLE IF NOT EXISTS dispositions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_code     TEXT NOT NULL REFERENCES kilns(batch_code),
    work_id        TEXT NOT NULL REFERENCES works(id),
    decision       TEXT NOT NULL,            -- rework / discard / reschedule
    reentry_step   TEXT,                     -- 返工时重做的起始工序
    decided_by     TEXT NOT NULL,
    decided_at     TEXT NOT NULL,
    note           TEXT NOT NULL DEFAULT '',
    UNIQUE (batch_code, work_id)
);

-- 追加式事件日志：审计与作品沿革的唯一事实来源。
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}'
);

-- 设备幂等：同一设备同一内容指纹不得重复建档/上传。
CREATE TABLE IF NOT EXISTS idempotency_keys (
    fingerprint TEXT PRIMARY KEY,            -- device_id + ':' + 内容哈希
    device_id   TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    at          TEXT NOT NULL
);

-- HTTP Idempotency-Key 重放缓存（持久化，进程重启后仍生效）。
CREATE TABLE IF NOT EXISTS request_keys (
    request_key TEXT PRIMARY KEY,
    status      INTEGER NOT NULL,
    body        TEXT NOT NULL,
    at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_work ON events(entity_type, entity_id, id);
CREATE INDEX IF NOT EXISTS idx_members_work ON kiln_members(work_id);
CREATE INDEX IF NOT EXISTS idx_steps_work ON work_steps(work_id);
"""


class Database:
    def __init__(self, path: str = ":memory:", timeout: float = 30.0):
        self.path = path
        self.timeout = timeout
        self._persistent = path == ":memory:"
        if self._persistent:
            # 共享缓存内存库：所有连接通过同一 URI 访问同一库，
            # 并保留一个常驻连接防止库在最后一个连接关闭时被删除。
            import secrets as _secrets

            self._db_name = f"pottery-mem-{_secrets.token_hex(8)}"
            self._uri = f"file:{self._db_name}?mode=memory&cache=shared&uri=true"
            self._keepalive = self.connect()
        else:
            self._uri = path
        self._init_schema()
        if not self._persistent:
            conn = self.connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            finally:
                conn.close()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._uri if self._persistent else self.path,
            timeout=self.timeout,
            isolation_level=None,  # 手工管理事务边界
            check_same_thread=False,
            uri=self._persistent,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务：BEGIN IMMEDIATE 立即加锁，提交或回滚。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()


def log_event(
    conn: sqlite3.Connection,
    at: str,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str,
    payload: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events(at, actor, action, entity_type, entity_id, payload)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (at, actor, action, entity_type, entity_id, json.dumps(payload or {}, ensure_ascii=False)),
    )
