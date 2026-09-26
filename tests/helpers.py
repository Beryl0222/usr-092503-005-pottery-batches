"""测试共享夹具：内存级临时库 + 种子数据。"""

from __future__ import annotations

import os
import tempfile

from src.pottery import ManualClock, PotteryService, Store

ADMIN = "admin1"
REGISTRAR = "reg1"
INHERITOR = "inh1"
TEACHER_C1 = "t1"
TEACHER_C1_B = "t2"
TEACHER_C2 = "t3"
GUARDIAN_S1 = "g1"
GUARDIAN_S2 = "g2"
STUDENT_S1 = "s1"
STUDENT_S2 = "s2"
STUDENT_S3 = "s3"  # G4 年级，C2 班

VERSION_PAYLOAD = {
    "name": "基础陶艺V1",
    "grade": "G3",
    "min_drying_hours": 24,
    "allowed_clays": ["CLAY-A", "CLAY-B"],
    "glaze_blocklist": [["GL-1", "GL-2"]],
    "safety_requirements": ["apron", "briefing"],
}


def make_service(path: str | None = None):
    """返回 (service, store, clock)；path 为空时使用临时文件。"""
    if path is None:
        path = os.path.join(tempfile.mkdtemp(prefix="pottery-test-"), "test.db")
    store = Store(path)
    store.add_user(ADMIN, "课程负责人", "course_admin")
    store.add_user(REGISTRAR, "教务员", "registrar")
    store.add_user(INHERITOR, "传承人", "inheritor")
    store.add_user(TEACHER_C1, "王老师", "teacher", class_id="C1")
    store.add_user(TEACHER_C1_B, "李老师", "teacher", class_id="C1")
    store.add_user(TEACHER_C2, "赵老师", "teacher", class_id="C2")
    store.add_user(GUARDIAN_S1, "家长甲", "guardian")
    store.add_user(GUARDIAN_S2, "家长乙", "guardian")
    store.add_student(STUDENT_S1, "学生甲", "G3", "C1")
    store.add_student(STUDENT_S2, "学生乙", "G3", "C1")
    store.add_student(STUDENT_S3, "学生丙", "G4", "C2")
    store.add_guardianship(GUARDIAN_S1, STUDENT_S1)
    store.add_guardianship(GUARDIAN_S2, STUDENT_S2)
    clock = ManualClock()
    return PotteryService(store, clock), store, clock


def make_version(svc) -> str:
    return svc.create_craft_version(ADMIN, dict(VERSION_PAYLOAD))["version_id"]


def make_work(svc, version_id: str, student: str = STUDENT_S1,
              teacher: str = TEACHER_C1, **overrides) -> str:
    payload = {
        "student_id": student,
        "version_id": version_id,
        "material_batch_no": "MB-2026-001",
        "clay_code": "CLAY-A",
        "glaze_codes": ["GL-1"],
        "storage_location": "陶艺教室-A架",
        "safety_confirmed": ["apron", "briefing"],
    }
    payload.update(overrides)
    return svc.register_work(teacher, payload)["work_id"]


def sign_all_steps(svc, work_id: str, teacher: str = TEACHER_C1) -> None:
    for step in ("wedging", "throwing", "trimming", "glazing"):
        svc.sign_step(teacher, work_id, {"step": step})


def make_firable(svc, clock, version_id: str, student: str = STUDENT_S1,
                 reviewer: str = TEACHER_C1_B, **overrides) -> str:
    """完成全部工序与复核并越过干燥时间的作品。"""
    work_id = make_work(svc, version_id, student, **overrides)
    sign_all_steps(svc, work_id)
    svc.review_work(reviewer, work_id, {"approved": True})
    clock.advance(hours=25)
    return work_id
