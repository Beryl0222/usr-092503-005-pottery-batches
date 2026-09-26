"""测试夹具：构造一个含全部主数据的陶艺教学世界。"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field

from src.pottery import Clock, Database, PotteryService


def _file_db() -> Database:
    # 文件库 + WAL：BEGIN IMMEDIATE 与 busy_timeout 可真实串行化并发写事务。
    fd, path = tempfile.mkstemp(prefix="pottery-test-", suffix=".db")
    os.close(fd)
    return Database(path)


@dataclass
class World:
    db: Database
    clock: Clock
    svc: PotteryService
    tokens: dict = field(default_factory=dict)
    users: dict = field(default_factory=dict)

    def actor(self, key: str):
        return self.svc.authenticate(self.tokens[key])


def build_world(min_drying_hours: float = 24.0) -> World:
    """初始化：4 类账户、学生、工艺版本（2 条安全前置）、陶泥与两种釉料。"""
    db = _file_db()
    clock = Clock()
    clock.freeze(datetime_safe(2026, 9, 1, 8, 0))
    svc = PotteryService(db, clock)
    w = World(db=db, clock=clock, svc=svc)

    def add_user(key, uid, role, name):
        token = f"tok-{key}"
        svc.create_user(uid, role, name, token)
        w.tokens[key] = token
        w.users[key] = uid

    add_user("coord", "U-COORD", "coordinator", "王教务")
    add_user("master", "U-MASTER", "master", "李传承人")
    add_user("teacher", "U-T1", "teacher", "张老师")
    add_user("teacher2", "U-T2", "teacher", "陈老师")
    add_user("guardian", "U-G1", "guardian", "赵监护")

    coord = w.actor("coord")
    svc.create_student("S-1", "赵小明", "七年级", "七(1)班", coord)
    svc.create_student("S-2", "钱小芳", "七年级", "七(1)班", coord)
    svc.create_student("S-3", "孙小磊", "八年级", "八(2)班", coord)
    svc.link_guardian("U-G1", "S-1", coord)

    svc.create_craft_version(
        "JUNIOR-POTTERY", "七年级", "初中陶艺基础",
        min_drying_hours, "stoneware", ["佩戴护具", "保持工位整洁"],
        coord,
    )
    svc.create_craft_version(
        "JUNIOR-POTTERY", "八年级", "初中陶艺进阶",
        12.0, "porcelain", ["佩戴护具"],
        coord,
    )
    svc.add_glaze_compat("stoneware", "GZ-SPECIAL", coord)

    svc.register_material_lot(
        "CLAY-A", "L2026-01", "clay", "粗陶泥", None, [], "南山陶土厂", coord)
    svc.register_material_lot(
        "GZ-STD", "L2026-02", "glaze", "标准炻器釉", "stoneware", [], "江南釉坊", coord)
    svc.register_material_lot(
        "GZ-PORC", "L2026-03", "glaze", "透明瓷釉", "porcelain", [], "江南釉坊", coord)
    svc.register_material_lot(
        "GZ-SPECIAL", "L2026-04", "glaze", "兼容艺术釉", "porcelain", [], "外来样品", coord)
    svc.register_material_lot(
        "CLAY-B", "L2026-05", "clay", "试验陶泥", None, ["七年级"], "试验供应商", coord)
    return w


def datetime_safe(year, month, day, hour=0, minute=0):
    from datetime import datetime, timezone

    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def make_ready_work(w: World, student_id: str = "S-1", teacher_key: str = "teacher",
                    reviewer_key: str | None = "teacher2", device: str = "DEV-1",
                    content: str = "photo-1") -> str:
    """走完建档→揉泥→拉坯→修坯→(干燥)→施釉→复核全流程，返回作品 ID。

    reviewer_key=None 时停在待复核状态。
    """
    svc = w.svc
    t = w.actor(teacher_key)
    work = svc.create_work(
        student_id, t, device, f"hash-{content}",
        craft_code="JUNIOR-POTTERY", safety_acks=[0, 1], storage_location="A 架 1 层")
    wid = work["id"]
    svc.sign_step(wid, "wedging", t, material_code="CLAY-A", material_lot="L2026-01")
    svc.sign_step(wid, "throwing", t)
    svc.sign_step(wid, "trimming", t)
    w.clock.advance(hours=25)
    svc.sign_step(wid, "glazing", t, material_code="GZ-STD", material_lot="L2026-02")
    if reviewer_key is not None:
        svc.review_work(wid, w.actor(reviewer_key))
    return wid


def run_kiln_to_passed(w: World, batch_code: str, work_ids: list[str]) -> None:
    t = w.actor("teacher")
    m = w.actor("master")
    for wid in work_ids:
        w.svc.add_to_kiln(batch_code, wid, t)
    w.svc.start_firing(batch_code, t)
    w.svc.mark_done(batch_code, t)
    w.svc.quality_check(batch_code, True, m)
