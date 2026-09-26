"""业务服务层：工艺版本、作品流转、烧制批次、授权与导出。"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .contracts import CraftStep, WorkDisposition, validate_batch_code
from .errors import Conflict, Forbidden, NotFound, Unauthorized, Validation
from .models import (
    DRYING_ANCHOR_STEP,
    ORDERED_STEPS,
    BatchStatus,
    Role,
    WorkStatus,
)
from .store import Store

STAFF_ROLES = (Role.COURSE_ADMIN, Role.TEACHER, Role.REGISTRAR, Role.INHERITOR)


def _json_list(row_value: str) -> list:
    return json.loads(row_value)


class PotteryService:
    """所有写操作都在 ``store.tx()`` 中完成，时钟可注入。"""

    def __init__(self, store: Store, clock):
        self.store = store
        self.clock = clock

    # ---------------------------------------------------------------- 基础

    def _now(self) -> datetime:
        return self.clock.now()

    def _now_iso(self) -> str:
        return self._now().isoformat()

    def _actor(self, user_id: str | None) -> sqlite3.Row:
        if not user_id:
            raise Unauthorized("缺少 X-User-Id 请求头")
        row = self.store.one("SELECT * FROM users WHERE user_id = ?", (user_id,))
        if row is None:
            raise Unauthorized("未知用户")
        return row

    @staticmethod
    def _require(actor: sqlite3.Row, *roles: Role) -> None:
        if actor["role"] not in [r.value for r in roles]:
            raise Forbidden("当前角色无权执行此操作")

    def _event(self, conn, entity: str, entity_id: str, kind: str,
               payload: dict, actor: str) -> None:
        conn.execute(
            "INSERT INTO events(entity, entity_id, kind, payload, actor, at)"
            " VALUES(?,?,?,?,?,?)",
            (entity, entity_id, kind, json.dumps(payload, ensure_ascii=False),
             actor, self._now_iso()),
        )

    def _get_work(self, work_id: str) -> sqlite3.Row:
        row = self.store.one("SELECT * FROM works WHERE work_id = ?", (work_id,))
        if row is None:
            raise NotFound("作品不存在")
        return row

    def _get_student(self, student_id: str) -> sqlite3.Row:
        row = self.store.one(
            "SELECT * FROM students WHERE student_id = ?", (student_id,))
        if row is None:
            raise NotFound("学生不存在")
        return row

    def _get_version(self, version_id: str) -> sqlite3.Row:
        row = self.store.one(
            "SELECT * FROM craft_versions WHERE version_id = ?", (version_id,))
        if row is None:
            raise NotFound("工艺版本不存在")
        return row

    def _get_batch(self, batch_id: str) -> sqlite3.Row:
        row = self.store.one("SELECT * FROM batches WHERE batch_id = ?", (batch_id,))
        if row is None:
            raise NotFound("烧制批次不存在")
        return row

    # ------------------------------------------------------- 工艺版本（课程负责人）

    def create_craft_version(self, actor_id: str, payload: dict) -> dict:
        actor = self._actor(actor_id)
        self._require(actor, Role.COURSE_ADMIN)
        name = (payload.get("name") or "").strip()
        grade = (payload.get("grade") or "").strip()
        min_drying_hours = payload.get("min_drying_hours")
        allowed_clays = payload.get("allowed_clays") or []
        glaze_blocklist = payload.get("glaze_blocklist") or []
        safety_requirements = payload.get("safety_requirements") or []
        if not name or not grade:
            raise Validation("工艺版本必须包含名称与适用年级")
        if not isinstance(min_drying_hours, int) or min_drying_hours < 0:
            raise Validation("干燥时间必须是非负整数小时")
        if not allowed_clays:
            raise Validation("至少允许一种陶土")
        for pair in glaze_blocklist:
            if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
                raise Validation("釉料互斥表须为二元组列表")
        with self.store.tx() as conn:
            version_id = self.store.next_id(conn, "craft_version", "CV")
            conn.execute(
                "INSERT INTO craft_versions(version_id, name, grade,"
                " min_drying_hours, allowed_clays, glaze_blocklist,"
                " safety_requirements, status, created_by, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (version_id, name, grade, min_drying_hours,
                 json.dumps(allowed_clays), json.dumps(glaze_blocklist),
                 json.dumps(safety_requirements), "active",
                 actor["user_id"], self._now_iso()),
            )
            self._event(conn, "craft_version", version_id, "created",
                        {"name": name, "grade": grade}, actor["user_id"])
        return self.craft_version_view(self._get_version(version_id))

    def list_craft_versions(self, actor_id: str, grade: str | None = None) -> list[dict]:
        actor = self._actor(actor_id)
        self._require(actor, *STAFF_ROLES)
        if grade:
            rows = self.store.all(
                "SELECT * FROM craft_versions WHERE grade = ? ORDER BY created_at",
                (grade,))
        else:
            rows = self.store.all("SELECT * FROM craft_versions ORDER BY created_at")
        return [self.craft_version_view(r) for r in rows]

    @staticmethod
    def craft_version_view(row: sqlite3.Row) -> dict:
        return {
            "version_id": row["version_id"],
            "name": row["name"],
            "grade": row["grade"],
            "min_drying_hours": row["min_drying_hours"],
            "allowed_clays": _json_list(row["allowed_clays"]),
            "glaze_blocklist": _json_list(row["glaze_blocklist"]),
            "safety_requirements": _json_list(row["safety_requirements"]),
            "status": row["status"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    # ------------------------------------------------------------- 作品登记

    def register_work(self, actor_id: str, payload: dict) -> dict:
        actor = self._actor(actor_id)
        self._require(actor, Role.TEACHER)
        student = self._get_student(payload.get("student_id") or "")
        version = self._get_version(payload.get("version_id") or "")
        if version["status"] != "active":
            raise Validation("工艺版本已停用")
        if version["grade"] != student["grade"]:
            raise Validation("工艺版本不适用于该学生所在年级")
        material_batch_no = (payload.get("material_batch_no") or "").strip()
        if not material_batch_no:
            raise Validation("必须填写材料批号")
        clay_code = (payload.get("clay_code") or "").strip()
        if clay_code not in _json_list(version["allowed_clays"]):
            raise Validation("陶土不在工艺版本允许范围内")
        glaze_codes = payload.get("glaze_codes") or []
        self._check_glaze_compatible(version, glaze_codes)
        storage_location = (payload.get("storage_location") or "").strip()
        if not storage_location:
            raise Validation("必须填写保管位置")
        required = set(_json_list(version["safety_requirements"]))
        confirmed = set(payload.get("safety_confirmed") or [])
        missing = sorted(required - confirmed)
        if missing:
            raise Validation(f"安全前置条件未全部确认: {', '.join(missing)}")
        device_id = payload.get("device_id")
        client_record_id = payload.get("client_record_id")
        if device_id and client_record_id:
            existing = self.store.one(
                "SELECT work_id FROM works WHERE device_id = ? AND client_record_id = ?",
                (device_id, client_record_id))
            if existing is not None:
                # 同一设备重复上传：返回原记录，不产生重复作品
                return {**self.work_view(self._get_work(existing["work_id"])),
                        "deduplicated": True}
        try:
            with self.store.tx() as conn:
                work_id = self.store.next_id(conn, "work", "W")
                conn.execute(
                    "INSERT INTO works(work_id, student_id, class_id, version_id,"
                    " material_batch_no, clay_code, glaze_codes, storage_location,"
                    " safety_confirmed, status, created_by, created_at,"
                    " device_id, client_record_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (work_id, student["student_id"], student["class_id"],
                     version["version_id"], material_batch_no, clay_code,
                     json.dumps(glaze_codes), storage_location,
                     json.dumps(sorted(confirmed)), WorkStatus.IN_PROGRESS.value,
                     actor["user_id"], self._now_iso(), device_id, client_record_id),
                )
                self._event(conn, "work", work_id, "registered",
                            {"material_batch_no": material_batch_no,
                             "version_id": version["version_id"]},
                            actor["user_id"])
        except sqlite3.IntegrityError:
            # 并发下同设备同记录号：唯一索引兜底，按去重处理
            row = self.store.one(
                "SELECT work_id FROM works WHERE device_id = ?"
                " AND client_record_id = ?", (device_id, client_record_id))
            if row is None:
                raise
            return {**self.work_view(self._get_work(row["work_id"])),
                    "deduplicated": True}
        return {**self.work_view(self._get_work(work_id)), "deduplicated": False}

    @staticmethod
    def _check_glaze_compatible(version: sqlite3.Row, glaze_codes: list[str]) -> None:
        used = set(glaze_codes)
        for a, b in _json_list(version["glaze_blocklist"]):
            if a in used and b in used:
                raise Validation(f"釉料 {a} 与 {b} 在该工艺版本下不兼容")

    def work_view(self, row: sqlite3.Row) -> dict:
        return {
            "work_id": row["work_id"],
            "student_id": row["student_id"],
            "class_id": row["class_id"],
            "version_id": row["version_id"],
            "material_batch_no": row["material_batch_no"],
            "clay_code": row["clay_code"],
            "glaze_codes": _json_list(row["glaze_codes"]),
            "storage_location": row["storage_location"],
            "status": row["status"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def get_work(self, actor_id: str, work_id: str) -> dict:
        actor = self._actor(actor_id)
        self._require(actor, *STAFF_ROLES)
        return self.work_view(self._get_work(work_id))

    # ------------------------------------------------------------- 工序签认

    def sign_step(self, actor_id: str, work_id: str, payload: dict) -> dict:
        actor = self._actor(actor_id)
        self._require(actor, Role.TEACHER)
        work = self._get_work(work_id)
        step_value = payload.get("step") or ""
        try:
            step = CraftStep(step_value)
        except ValueError:
            raise Validation("未知工序") from None
        if step not in ORDERED_STEPS:
            raise Validation("烧制工序由批次完成，不能手工签认")
        if work["status"] != WorkStatus.IN_PROGRESS.value:
            raise Conflict("当前状态不允许工序签认")
        device_id = payload.get("device_id")
        client_record_id = payload.get("client_record_id")
        if device_id and client_record_id:
            dup = self.store.one(
                "SELECT id FROM step_records WHERE device_id = ?"
                " AND client_record_id = ?", (device_id, client_record_id))
            if dup is not None:
                row = self.store.one(
                    "SELECT * FROM step_records WHERE id = ?", (dup["id"],))
                return {**self.step_view(row), "deduplicated": True}
        student = self._get_student(work["student_id"])
        proxy_for = payload.get("proxy_for")
        proxy_reason = (payload.get("proxy_reason") or "").strip()
        if actor["class_id"] == student["class_id"]:
            if proxy_for:
                raise Validation("本班教师直接签认，无需代签信息")
        else:
            # 教师代签：必须指明被代签的本班教师与原因
            if not proxy_for or not proxy_reason:
                raise Forbidden("非本班教师须填写代签对象与代签原因")
            delegator = self.store.one(
                "SELECT * FROM users WHERE user_id = ? AND role = ?",
                (proxy_for, Role.TEACHER.value))
            if delegator is None or delegator["class_id"] != student["class_id"]:
                raise Validation("代签对象必须是该学生当前班级的教师")
        try:
            with self.store.tx() as conn:
                signed = {
                    r["step"] for r in conn.execute(
                        "SELECT step FROM step_records WHERE work_id = ?", (work_id,))
                }
                idx = ORDERED_STEPS.index(step)
                missing = [s.value for s in ORDERED_STEPS[:idx] if s.value not in signed]
                if missing:
                    raise Validation(f"工序不能越级，请先完成: {', '.join(missing)}")
                if step.value in signed:
                    raise Conflict("该工序已签认")
                cur = conn.execute(
                    "INSERT INTO step_records(work_id, step, signed_by, signed_at,"
                    " proxy_for, proxy_reason, device_id, client_record_id)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (work_id, step.value, actor["user_id"], self._now_iso(),
                     proxy_for, proxy_reason or None, device_id, client_record_id),
                )
                self._event(conn, "work", work_id, "step_signed",
                            {"step": step.value, "proxy_for": proxy_for},
                            actor["user_id"])
                self._refresh_status(conn, work_id)
                row = conn.execute(
                    "SELECT * FROM step_records WHERE id = ?",
                    (cur.lastrowid,)).fetchone()
        except sqlite3.IntegrityError:
            # 并发下唯一索引兜底：设备去重优先，否则视为重复签认
            if device_id and client_record_id:
                dup = self.store.one(
                    "SELECT * FROM step_records WHERE device_id = ?"
                    " AND client_record_id = ?", (device_id, client_record_id))
                if dup is not None:
                    return {**self.step_view(dup), "deduplicated": True}
            raise Conflict("该工序已签认") from None
        return {**self.step_view(row), "deduplicated": False}

    @staticmethod
    def step_view(row: sqlite3.Row) -> dict:
        return {
            "work_id": row["work_id"],
            "step": row["step"],
            "signed_by": row["signed_by"],
            "signed_at": row["signed_at"],
            "proxy_for": row["proxy_for"],
            "proxy_reason": row["proxy_reason"],
        }

    # ------------------------------------------------------------- 教师复核

    def review_work(self, actor_id: str, work_id: str, payload: dict) -> dict:
        actor = self._actor(actor_id)
        self._require(actor, Role.TEACHER)
        work = self._get_work(work_id)
        if work["status"] not in (WorkStatus.IN_PROGRESS.value,
                                  WorkStatus.AWAITING_FIRING.value,
                                  WorkStatus.REWORK.value):
            raise Conflict("当前状态不允许复核")
        steps = {r["step"] for r in self.store.all(
            "SELECT step FROM step_records WHERE work_id = ?", (work_id,))}
        if any(s.value not in steps for s in ORDERED_STEPS):
            raise Validation("全部工序签认完成后才能复核")
        glazing = self.store.one(
            "SELECT signed_by FROM step_records WHERE work_id = ? AND step = ?",
            (work_id, DRYING_ANCHOR_STEP.value))
        if glazing and glazing["signed_by"] == actor["user_id"]:
            raise Forbidden("复核教师须独立于施釉签认教师")
        approved = 1 if payload.get("approved") else 0
        note = payload.get("note")
        with self.store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO reviews(work_id, reviewed_by, reviewed_at, approved, note)"
                " VALUES(?,?,?,?,?)",
                (work_id, actor["user_id"], self._now_iso(), approved, note),
            )
            self._event(conn, "work", work_id, "reviewed",
                        {"approved": bool(approved)}, actor["user_id"])
            self._refresh_status(conn, work_id)
            row = conn.execute(
                "SELECT * FROM reviews WHERE id = ?", (cur.lastrowid,)).fetchone()
        return {"work_id": work_id, "reviewed_by": row["reviewed_by"],
                "reviewed_at": row["reviewed_at"],
                "approved": bool(row["approved"]), "note": row["note"]}

    def _refresh_status(self, conn, work_id: str) -> None:
        work = conn.execute(
            "SELECT status FROM works WHERE work_id = ?", (work_id,)).fetchone()
        status = work["status"]
        if status not in (WorkStatus.IN_PROGRESS.value,
                          WorkStatus.AWAITING_FIRING.value,
                          WorkStatus.REWORK.value):
            return
        steps = {r["step"] for r in conn.execute(
            "SELECT step FROM step_records WHERE work_id = ?", (work_id,))}
        steps_done = all(s.value in steps for s in ORDERED_STEPS)
        review = conn.execute(
            "SELECT id, approved FROM reviews WHERE work_id = ?"
            " ORDER BY id DESC LIMIT 1", (work_id,)).fetchone()
        boundary = conn.execute(
            "SELECT MAX(reviews_before) AS b FROM dispositions"
            " WHERE work_id = ? AND decision = ?",
            (work_id, WorkDisposition.REWORK.value)).fetchone()["b"] or 0
        review_ok = bool(review and review["approved"]
                         and review["id"] > boundary)
        if steps_done and review_ok:
            new_status = WorkStatus.AWAITING_FIRING.value
        elif status == WorkStatus.REWORK.value:
            new_status = WorkStatus.REWORK.value
        else:
            new_status = WorkStatus.IN_PROGRESS.value
        if new_status != status:
            conn.execute("UPDATE works SET status = ? WHERE work_id = ?",
                         (new_status, work_id))

    # ------------------------------------------------------------- 保管位置

    def move_storage(self, actor_id: str, work_id: str, payload: dict) -> dict:
        actor = self._actor(actor_id)
        self._require(actor, Role.TEACHER)
        work = self._get_work(work_id)
        if work["status"] == WorkStatus.DISCARDED.value:
            raise Conflict("作品已报废")
        to_location = (payload.get("to_location") or "").strip()
        if not to_location:
            raise Validation("保管位置不能为空")
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO storage_moves(work_id, from_loc, to_loc, moved_by, moved_at)"
                " VALUES(?,?,?,?,?)",
                (work_id, work["storage_location"], to_location,
                 actor["user_id"], self._now_iso()))
            conn.execute("UPDATE works SET storage_location = ? WHERE work_id = ?",
                         (to_location, work_id))
            self._event(conn, "work", work_id, "storage_moved",
                        {"to": to_location}, actor["user_id"])
        return {"work_id": work_id, "storage_location": to_location}

    # ------------------------------------------------------------- 烧制批次

    def create_batch(self, actor_id: str, payload: dict) -> dict:
        actor = self._actor(actor_id)
        self._require(actor, Role.COURSE_ADMIN)
        code = (payload.get("code") or "").strip()
        try:
            validate_batch_code(code)
        except ValueError as exc:
            raise Validation(str(exc)) from None
        capacity = payload.get("capacity")
        if not isinstance(capacity, int) or capacity <= 0:
            raise Validation("批次容量必须是正整数")
        scheduled_at = payload.get("scheduled_at") or self._now_iso()
        with self.store.tx() as conn:
            batch_id = self.store.next_id(conn, "batch", "B")
            try:
                conn.execute(
                    "INSERT INTO batches(batch_id, code, capacity, scheduled_at,"
                    " status, created_by, created_at) VALUES(?,?,?,?,?,?,?)",
                    (batch_id, code, capacity, scheduled_at,
                     BatchStatus.SCHEDULED.value, actor["user_id"], self._now_iso()),
                )
            except sqlite3.IntegrityError:
                raise Conflict("批次编号已存在") from None
            self._event(conn, "batch", batch_id, "created",
                        {"code": code, "capacity": capacity}, actor["user_id"])
        return self.batch_view(self._get_batch(batch_id))

    @staticmethod
    def batch_view(row: sqlite3.Row, item_count: int | None = None) -> dict:
        view = {
            "batch_id": row["batch_id"],
            "code": row["code"],
            "capacity": row["capacity"],
            "scheduled_at": row["scheduled_at"],
            "status": row["status"],
            "created_by": row["created_by"],
            "closed_at": row["closed_at"],
            "close_reason": row["close_reason"],
        }
        if item_count is not None:
            view["item_count"] = item_count
        return view

    def list_batches(self, actor_id: str) -> list[dict]:
        actor = self._actor(actor_id)
        self._require(actor, *STAFF_ROLES)
        rows = self.store.all(
            "SELECT b.*, (SELECT COUNT(*) FROM batch_items i"
            " WHERE i.batch_id = b.batch_id) AS n"
            " FROM batches b ORDER BY b.created_at")
        return [self.batch_view(r, r["n"]) for r in rows]

    def _firable_problems(self, conn, work: sqlite3.Row) -> list[str]:
        """返回作品当前不满足排批条件的原因列表（空列表表示可排批）。"""
        problems: list[str] = []
        if work["status"] != WorkStatus.AWAITING_FIRING.value:
            problems.append("作品未处于待烧状态（工序、复核或返工复核未完成）")
            return problems
        version = conn.execute(
            "SELECT * FROM craft_versions WHERE version_id = ?",
            (work["version_id"],)).fetchone()
        try:
            self._check_glaze_compatible(version, _json_list(work["glaze_codes"]))
        except Validation as exc:
            problems.append(exc.message)
        anchor = conn.execute(
            "SELECT signed_at FROM step_records WHERE work_id = ? AND step = ?",
            (work_id := work["work_id"], DRYING_ANCHOR_STEP.value)).fetchone()
        if anchor is None:
            problems.append("缺少施釉签认，无法判定干燥时间")
        else:
            ready_at = (datetime.fromisoformat(anchor["signed_at"])
                        + timedelta(hours=version["min_drying_hours"]))
            if self._now() < ready_at:
                problems.append(f"干燥时间不足，{ready_at.isoformat()} 后方可入窑")
        active = conn.execute(
            "SELECT 1 FROM batch_items i JOIN batches b ON b.batch_id = i.batch_id"
            " WHERE i.work_id = ? AND b.status = ?",
            (work_id, BatchStatus.SCHEDULED.value)).fetchone()
        if active is not None:
            problems.append("作品已在其他待烧批次中")
        return problems

    def admit_to_batch(self, actor_id: str, batch_id: str, payload: dict) -> dict:
        actor = self._actor(actor_id)
        self._require(actor, Role.COURSE_ADMIN, Role.TEACHER)
        work_id = payload.get("work_id") or ""
        with self.store.tx() as conn:
            batch = conn.execute(
                "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
            if batch is None:
                raise NotFound("烧制批次不存在")
            if batch["status"] != BatchStatus.SCHEDULED.value:
                raise Conflict("批次已关闭，不能再排入作品")
            work = conn.execute(
                "SELECT * FROM works WHERE work_id = ?", (work_id,)).fetchone()
            if work is None:
                raise NotFound("作品不存在")
            problems = self._firable_problems(conn, work)
            if problems:
                raise Validation("；".join(problems))
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM batch_items WHERE batch_id = ?",
                (batch_id,)).fetchone()["n"]
            if count >= batch["capacity"]:
                raise Conflict("批次容量已满")
            conn.execute(
                "INSERT INTO batch_items(batch_id, work_id, admitted_at)"
                " VALUES(?,?,?)",
                (batch_id, work_id, self._now_iso()))
            conn.execute("UPDATE works SET status = ? WHERE work_id = ?",
                         (WorkStatus.SCHEDULED.value, work_id))
            self._event(conn, "batch", batch_id, "work_admitted",
                        {"work_id": work_id}, actor["user_id"])
            count += 1
        batch = self._get_batch(batch_id)
        return {**self.batch_view(batch, count), "work_id": work_id}

    # ------------------------------------------------- 批次关闭与异常处置

    def finish_batch(self, actor_id: str, batch_id: str, payload: dict) -> dict:
        actor = self._actor(actor_id)
        self._require(actor, Role.COURSE_ADMIN)
        result = payload.get("result") or ""
        if result not in (BatchStatus.COMPLETED.value,
                          BatchStatus.CANCELLED.value,
                          BatchStatus.FAILED.value):
            raise Validation("result 必须是 completed / cancelled / failed")
        qc = payload.get("qc") or {}
        dispositions = payload.get("dispositions") or []
        reason = (payload.get("reason") or "").strip()
        with self.store.tx() as conn:
            batch = conn.execute(
                "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
            if batch is None:
                raise NotFound("烧制批次不存在")
            if batch["status"] != BatchStatus.SCHEDULED.value:
                raise Conflict("批次已关闭")
            items = [r["work_id"] for r in conn.execute(
                "SELECT work_id FROM batch_items WHERE batch_id = ?", (batch_id,))]
            disp_by_work: dict[str, dict] = {}
            for d in dispositions:
                wid = d.get("work_id") or ""
                decision = d.get("decision") or ""
                if wid not in items:
                    raise Validation(f"作品 {wid} 不在本批次中")
                if decision not in [x.value for x in WorkDisposition]:
                    raise Validation("处置决定必须是 rework / discard / reschedule")
                if not (d.get("reason") or "").strip():
                    raise Validation("处置决定必须填写原因")
                disp_by_work[wid] = d
            outcomes: dict[str, str] = {}
            if result == BatchStatus.COMPLETED.value:
                missing_qc = [w for w in items if qc.get(w) not in ("pass", "fail")]
                if missing_qc:
                    raise Validation(
                        f"缺少质检结果: {', '.join(sorted(missing_qc))}")
                for w in items:
                    outcomes[w] = qc[w]
                need = [w for w in items if qc[w] == "fail" and w not in disp_by_work]
            else:
                for w in items:
                    outcomes[w] = result
                need = [w for w in items if w not in disp_by_work]
            if need:
                raise Validation(
                    f"以下作品缺少处置决定: {', '.join(sorted(need))}")
            now = self._now_iso()
            for wid, d in disp_by_work.items():
                reviews_before = 0
                if d["decision"] == WorkDisposition.REWORK.value:
                    reviews_before = conn.execute(
                        "SELECT COALESCE(MAX(id), 0) AS m FROM reviews"
                        " WHERE work_id = ?", (wid,)).fetchone()["m"]
                conn.execute(
                    "INSERT INTO dispositions(work_id, batch_id, decision, reason,"
                    " decided_by, decided_at, reviews_before)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (wid, batch_id, d["decision"], d["reason"].strip(),
                     actor["user_id"], now, reviews_before))
                self._event(conn, "work", wid, "disposition",
                            {"batch_id": batch_id, "decision": d["decision"],
                             "reason": d["reason"].strip()}, actor["user_id"])
            for wid, outcome in outcomes.items():
                conn.execute(
                    "UPDATE batch_items SET outcome = ?"
                    " WHERE batch_id = ? AND work_id = ?",
                    (outcome, batch_id, wid))
                if outcome == "pass":
                    new_status = WorkStatus.FIRED.value
                elif wid in disp_by_work:
                    new_status = {
                        WorkDisposition.REWORK.value: WorkStatus.REWORK.value,
                        WorkDisposition.DISCARD.value: WorkStatus.DISCARDED.value,
                        WorkDisposition.RESCHEDULE.value: WorkStatus.AWAITING_FIRING.value,
                    }[disp_by_work[wid]["decision"]]
                else:
                    new_status = WorkStatus.AWAITING_FIRING.value
                conn.execute("UPDATE works SET status = ? WHERE work_id = ?",
                             (new_status, wid))
            conn.execute(
                "UPDATE batches SET status = ?, closed_at = ?, close_reason = ?"
                " WHERE batch_id = ?",
                (result, now, reason or None, batch_id))
            self._event(conn, "batch", batch_id, "closed",
                        {"result": result, "reason": reason}, actor["user_id"])
        batch = self._get_batch(batch_id)
        count = self.store.one(
            "SELECT COUNT(*) AS n FROM batch_items WHERE batch_id = ?",
            (batch_id,))["n"]
        return self.batch_view(batch, count)

    # ------------------------------------------------------------- 学籍与授权

    def transfer_student(self, actor_id: str, student_id: str, payload: dict) -> dict:
        actor = self._actor(actor_id)
        self._require(actor, Role.REGISTRAR)
        student = self._get_student(student_id)
        to_class = (payload.get("to_class") or "").strip()
        if not to_class:
            raise Validation("目标班级不能为空")
        if to_class == student["class_id"]:
            raise Conflict("学生已在目标班级")
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO transfers(student_id, from_class, to_class, moved_by,"
                " moved_at) VALUES(?,?,?,?,?)",
                (student_id, student["class_id"], to_class,
                 actor["user_id"], self._now_iso()))
            conn.execute("UPDATE students SET class_id = ? WHERE student_id = ?",
                         (to_class, student_id))
            self._event(conn, "student", student_id, "transferred",
                        {"from": student["class_id"], "to": to_class},
                        actor["user_id"])
        return {"student_id": student_id, "class_id": to_class}

    def set_consent(self, actor_id: str, work_id: str, payload: dict) -> dict:
        actor = self._actor(actor_id)
        self._require(actor, Role.GUARDIAN)
        work = self._get_work(work_id)
        link = self.store.one(
            "SELECT 1 FROM guardianships WHERE guardian_id = ? AND student_id = ?",
            (actor["user_id"], work["student_id"]))
        if link is None:
            raise Forbidden("只能为本人监护的学生作品授权")
        granted = 1 if payload.get("granted") else 0
        note = payload.get("note")
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO consents(work_id, granted, updated_by, updated_at, note)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(work_id) DO UPDATE SET granted = excluded.granted,"
                " updated_by = excluded.updated_by,"
                " updated_at = excluded.updated_at, note = excluded.note",
                (work_id, granted, actor["user_id"], self._now_iso(), note))
            self._event(conn, "work", work_id,
                        "consent_granted" if granted else "consent_withdrawn",
                        {"note": note}, actor["user_id"])
        return {"work_id": work_id, "granted": bool(granted)}

    # ------------------------------------------------------------- 公开展示

    def _exhibit_rows(self, work_id: str | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT w.* FROM works w"
            " JOIN consents c ON c.work_id = w.work_id AND c.granted = 1"
            " WHERE w.status = ?")
        params: list[Any] = [WorkStatus.FIRED.value]
        if work_id is not None:
            sql += " AND w.work_id = ?"
            params.append(work_id)
        return self.store.all(sql, params)

    def _exhibit_card(self, work: sqlite3.Row) -> dict:
        """公开视图：不含学生姓名、学号、班级等未成年人身份信息。"""
        student = self._get_student(work["student_id"])
        version = self._get_version(work["version_id"])
        handlers = []
        seen = set()
        for r in self.store.all(
                "SELECT signed_by AS uid, step AS role FROM step_records"
                " WHERE work_id = ? UNION ALL"
                " SELECT reviewed_by, 'review' FROM reviews WHERE work_id = ?"
                " ORDER BY 1", (work["work_id"], work["work_id"])):
            if (r["uid"], r["role"]) in seen:
                continue
            seen.add((r["uid"], r["role"]))
            user = self.store.one(
                "SELECT name FROM users WHERE user_id = ?", (r["uid"],))
            handlers.append({"name": user["name"] if user else r["uid"],
                             "role": r["role"]})
        fired = self.store.one(
            "SELECT b.closed_at FROM batch_items i JOIN batches b"
            " ON b.batch_id = i.batch_id WHERE i.work_id = ? AND i.outcome = 'pass'"
            " ORDER BY b.closed_at DESC LIMIT 1", (work["work_id"],))
        return {
            "work_id": work["work_id"],  # 作品编号即公开标识
            "grade": student["grade"],
            "craft_version": {"version_id": version["version_id"],
                              "name": version["name"]},
            "material_batch_no": work["material_batch_no"],
            "clay_code": work["clay_code"],
            "glaze_codes": _json_list(work["glaze_codes"]),
            "handlers": handlers,
            "fired_at": fired["closed_at"] if fired else None,
        }

    def public_exhibits(self) -> list[dict]:
        return [self._exhibit_card(w) for w in self._exhibit_rows()]

    def public_work(self, work_id: str) -> dict:
        rows = self._exhibit_rows(work_id)
        if not rows:
            raise NotFound("展品不存在或未获展示授权")
        return self._exhibit_card(rows[0])

    # ------------------------------------------------------------- 沿革与导出

    def work_history(self, actor_id: str, work_id: str) -> dict:
        actor = self._actor(actor_id)
        self._require(actor, *STAFF_ROLES)
        return self._history(work_id)

    def _history(self, work_id: str) -> dict:
        work = self._get_work(work_id)
        student = self._get_student(work["student_id"])
        version = self._get_version(work["version_id"])
        steps = []
        for r in self.store.all(
                "SELECT s.*, u.name AS signer_name FROM step_records s"
                " LEFT JOIN users u ON u.user_id = s.signed_by"
                " WHERE s.work_id = ? ORDER BY s.id", (work_id,)):
            steps.append({**self.step_view(r), "signed_by_name": r["signer_name"]})
        reviews = [
            {"reviewed_by": r["reviewed_by"], "reviewed_at": r["reviewed_at"],
             "approved": bool(r["approved"]), "note": r["note"]}
            for r in self.store.all(
                "SELECT * FROM reviews WHERE work_id = ? ORDER BY id", (work_id,))]
        storage = [
            {"from": r["from_loc"], "to": r["to_loc"], "moved_by": r["moved_by"],
             "moved_at": r["moved_at"]}
            for r in self.store.all(
                "SELECT * FROM storage_moves WHERE work_id = ? ORDER BY id",
                (work_id,))]
        batches = [
            {"batch_id": r["batch_id"], "code": r["code"],
             "batch_status": r["status"], "admitted_at": r["admitted_at"],
             "outcome": r["outcome"]}
            for r in self.store.all(
                "SELECT i.*, b.code, b.status FROM batch_items i"
                " JOIN batches b ON b.batch_id = i.batch_id"
                " WHERE i.work_id = ? ORDER BY i.admitted_at", (work_id,))]
        dispositions = [
            {"batch_id": r["batch_id"], "decision": r["decision"],
             "reason": r["reason"], "decided_by": r["decided_by"],
             "decided_at": r["decided_at"]}
            for r in self.store.all(
                "SELECT * FROM dispositions WHERE work_id = ? ORDER BY id",
                (work_id,))]
        consent = self.store.one(
            "SELECT * FROM consents WHERE work_id = ?", (work_id,))
        transfers = [
            {"from": r["from_class"], "to": r["to_class"], "moved_at": r["moved_at"]}
            for r in self.store.all(
                "SELECT * FROM transfers WHERE student_id = ? ORDER BY id",
                (student["student_id"],))]
        return {
            "work": self.work_view(work),
            "student": {"student_id": student["student_id"],
                        "grade": student["grade"],
                        "current_class_id": student["class_id"]},
            "craft_version": self.craft_version_view(version),
            "steps": steps,
            "reviews": reviews,
            "storage_moves": storage,
            "batches": batches,
            "dispositions": dispositions,
            "consent": ({"granted": bool(consent["granted"]),
                         "updated_at": consent["updated_at"]}
                        if consent else None),
            "student_transfers": transfers,
        }

    def work_history_csv(self, actor_id: str, work_id: str) -> str:
        history = self.work_history(actor_id, work_id)
        out = io.StringIO()
        writer = csv.writer(out)
        w = history["work"]
        writer.writerow(["作品编号", w["work_id"]])
        writer.writerow(["材料批号", w["material_batch_no"]])
        writer.writerow(["陶土", w["clay_code"]])
        writer.writerow(["釉料", " ".join(w["glaze_codes"])])
        writer.writerow(["工艺版本", history["craft_version"]["version_id"],
                         history["craft_version"]["name"]])
        writer.writerow(["当前状态", w["status"]])
        writer.writerow([])
        writer.writerow(["工序", "签认人", "签认时间", "代签对象", "代签原因"])
        for s in history["steps"]:
            writer.writerow([s["step"], s["signed_by"], s["signed_at"],
                             s["proxy_for"] or "", s["proxy_reason"] or ""])
        writer.writerow([])
        writer.writerow(["批次", "批次状态", "入批时间", "结果"])
        for b in history["batches"]:
            writer.writerow([b["code"], b["batch_status"], b["admitted_at"],
                             b["outcome"] or ""])
        writer.writerow([])
        writer.writerow(["处置批次", "决定", "原因", "决定人", "决定时间"])
        for d in history["dispositions"]:
            writer.writerow([d["batch_id"], d["decision"], d["reason"],
                             d["decided_by"], d["decided_at"]])
        return out.getvalue()

    def batch_manifest(self, actor_id: str, batch_id: str) -> dict:
        actor = self._actor(actor_id)
        self._require(actor, *STAFF_ROLES)
        batch = self._get_batch(batch_id)
        items = []
        for r in self.store.all(
                "SELECT i.*, w.material_batch_no, w.version_id, w.clay_code"
                " FROM batch_items i JOIN works w ON w.work_id = i.work_id"
                " WHERE i.batch_id = ? ORDER BY i.admitted_at", (batch_id,)):
            items.append({
                "work_id": r["work_id"],
                "material_batch_no": r["material_batch_no"],
                "version_id": r["version_id"],
                "clay_code": r["clay_code"],
                "admitted_at": r["admitted_at"],
                "outcome": r["outcome"],
            })
        return {**self.batch_view(batch, len(items)), "items": items}

    def batch_manifest_csv(self, actor_id: str, batch_id: str) -> str:
        manifest = self.batch_manifest(actor_id, batch_id)
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["批次编号", manifest["code"]])
        writer.writerow(["状态", manifest["status"]])
        writer.writerow(["容量", manifest["capacity"]])
        writer.writerow(["已排作品数", manifest["item_count"]])
        writer.writerow([])
        writer.writerow(["作品编号", "材料批号", "工艺版本", "陶土",
                         "入批时间", "结果"])
        for item in manifest["items"]:
            writer.writerow([item["work_id"], item["material_batch_no"],
                             item["version_id"], item["clay_code"],
                             item["admitted_at"], item["outcome"] or ""])
        return out.getvalue()
