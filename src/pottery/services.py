"""领域服务层：陶艺课程与窑炉管控的全部业务规则。

所有写操作都在单个 BEGIN IMMEDIATE 事务内完成并追加事件日志，
事件日志是作品沿革与审计的唯一事实来源。
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime

from .clock import Clock, parse_iso
from .contracts import (
    STEP_ORDER,
    CraftStep,
    KilnStatus,
    MaterialKind,
    Role,
    WorkDisposition,
    WorkStatus,
    validate_batch_code,
)
from .errors import (
    BatchFull,
    BatchNotEditable,
    Conflict,
    DuplicateUpload,
    Forbidden,
    GlazeIncompatible,
    InvalidDisposition,
    MaterialForbidden,
    NotFound,
    PrerequisiteNotMet,
    StepOutOfOrder,
    Unauthorized,
    ValidationFailed,
    WorkNotEligible,
)
from .storage import Database, log_event

# 允许教师在失败/取消后安排返工时重做的起始工序。
REWORK_ENTRY_STEPS = (CraftStep.TRIMMING, CraftStep.GLAZING)

# 终态作品不再接受任何记录修改。
_TERMINAL_STATUSES = {WorkStatus.FINISHED, WorkStatus.DISCARDED}

# 各角色可写维护的工艺主数据。
_CRAFT_ROLES = {Role.COORDINATOR, Role.MASTER}


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _device_fingerprint(device_id: str, content_hash: str) -> str:
    return f"{device_id}:{content_hash}"


class PotteryService:
    def __init__(self, db: Database, clock: Clock | None = None):
        self.db = db
        self.clock = clock or Clock()

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return self.clock.iso()

    @staticmethod
    def _require_actor(actor: sqlite3.Row, roles: set[Role]) -> None:
        if actor is None:
            raise Unauthorized("缺少身份凭据")
        if Role(actor["role"]) not in roles:
            raise Forbidden("当前角色无权执行该操作")

    def authenticate(self, token: str) -> sqlite3.Row:
        if not token:
            raise Unauthorized("缺少身份凭据")
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM users WHERE token = ?", (token,)).fetchone()
        if row is None:
            raise Unauthorized("凭据无效")
        return row

    # ------------------------------------------------------------ 用户与学籍

    def create_user(
        self, user_id: str, role: str, display_name: str, token: str | None = None
    ) -> dict:
        role = Role(role)
        token = token or secrets.token_urlsafe(24)
        with self.db.transaction() as conn:
            exists = conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone()
            if exists:
                raise Conflict("用户已存在")
            conn.execute(
                "INSERT INTO users(id, role, display_name, token) VALUES (?, ?, ?, ?)",
                (user_id, role.value, display_name, token),
            )
            log_event(conn, self._now(), user_id, "user.created", "user", user_id,
                      {"role": role.value, "display_name": display_name})
        return {"id": user_id, "role": role.value, "display_name": display_name, "token": token}

    def create_student(
        self, student_id: str, name: str, grade: str, class_group: str, actor: sqlite3.Row
    ) -> dict:
        self._require_actor(actor, {Role.COORDINATOR})
        with self.db.transaction() as conn:
            exists = conn.execute("SELECT 1 FROM students WHERE id = ?", (student_id,)).fetchone()
            if exists:
                raise Conflict("学生已存在")
            conn.execute(
                "INSERT INTO students(id, name, grade, class_group, active, created_at)"
                " VALUES (?, ?, ?, ?, 1, ?)",
                (student_id, name, grade, class_group, self._now()),
            )
            log_event(conn, self._now(), actor["id"], "student.created", "student", student_id,
                      {"grade": grade, "class_group": class_group})
        return {"id": student_id, "name": name, "grade": grade, "class_group": class_group}

    def link_guardian(self, guardian_id: str, student_id: str, actor: sqlite3.Row) -> None:
        self._require_actor(actor, {Role.COORDINATOR})
        with self.db.transaction() as conn:
            guardian = conn.execute(
                "SELECT * FROM users WHERE id = ? AND role = ?",
                (guardian_id, Role.GUARDIAN.value),
            ).fetchone()
            if guardian is None:
                raise NotFound("监护人账户不存在")
            if conn.execute("SELECT 1 FROM students WHERE id = ?", (student_id,)).fetchone() is None:
                raise NotFound("学生不存在")
            conn.execute(
                "INSERT OR IGNORE INTO guardian_links(guardian_id, student_id) VALUES (?, ?)",
                (guardian_id, student_id),
            )
            log_event(conn, self._now(), actor["id"], "guardian.linked", "student", student_id,
                      {"guardian_id": guardian_id})

    def transfer_student(
        self, student_id: str, new_class_group: str, actor: sqlite3.Row
    ) -> dict:
        """学生转班：学籍变更立即生效；历史作品保留原班级快照，新作品使用新班级。"""
        self._require_actor(actor, {Role.COORDINATOR})
        with self.db.transaction() as conn:
            student = conn.execute(
                "SELECT * FROM students WHERE id = ? AND active = 1", (student_id,)
            ).fetchone()
            if student is None:
                raise NotFound("学生不存在")
            old_class = student["class_group"]
            if old_class == new_class_group:
                raise ValidationFailed("新班级与原班级相同")
            conn.execute(
                "UPDATE students SET class_group = ? WHERE id = ?",
                (new_class_group, student_id),
            )
            log_event(conn, self._now(), actor["id"], "student.transferred", "student", student_id,
                      {"from_class": old_class, "to_class": new_class_group})
        return {"id": student_id, "class_group": new_class_group}

    # ------------------------------------------------------ 工艺版本与材料目录

    def create_craft_version(
        self,
        code: str,
        grade: str,
        name: str,
        min_drying_hours: float,
        glaze_family: str,
        safety_requirements: list[str],
        actor: sqlite3.Row,
    ) -> dict:
        """发布工艺新版本；同 code 旧版本自动失效，既有作品继续钉选旧版本。"""
        self._require_actor(actor, _CRAFT_ROLES)
        if min_drying_hours < 0:
            raise ValidationFailed("干燥时长不能为负")
        if not safety_requirements:
            raise ValidationFailed("至少配置一条安全前置条件")
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS next_version"
                " FROM craft_versions WHERE code = ?",
                (code,),
            ).fetchone()
            version = row["next_version"]
            conn.execute(
                "UPDATE craft_versions SET status = 'superseded' WHERE code = ? AND grade = ?",
                (code, grade),
            )
            conn.execute(
                "INSERT INTO craft_versions(code, version, grade, name, min_drying_hours,"
                " glaze_family, status, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (code, version, grade, name, float(min_drying_hours), glaze_family,
                 actor["id"], self._now()),
            )
            for seq, requirement in enumerate(safety_requirements):
                conn.execute(
                    "INSERT INTO craft_safety(code, version, seq, requirement) VALUES (?, ?, ?, ?)",
                    (code, version, seq, requirement),
                )
            log_event(conn, self._now(), actor["id"], "craft.version_created", "craft",
                      f"{code}:{version}", {"code": code, "version": version, "grade": grade})
        return {"code": code, "version": version, "grade": grade, "name": name}

    def add_glaze_compat(self, family: str, material_code: str, actor: sqlite3.Row) -> None:
        """登记经验证可跨族使用的釉料放行规则。"""
        self._require_actor(actor, _CRAFT_ROLES)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO glaze_compat(family, material_code) VALUES (?, ?)",
                (family, material_code),
            )
            log_event(conn, self._now(), actor["id"], "craft.glaze_compat", "craft",
                      f"{family}:{material_code}", {"family": family, "material_code": material_code})

    def register_material_lot(
        self,
        material_code: str,
        lot_no: str,
        kind: str,
        name: str,
        glaze_family: str | None,
        forbidden_grades: list[str] | None,
        supplier: str,
        actor: sqlite3.Row,
    ) -> dict:
        self._require_actor(actor, _CRAFT_ROLES)
        kind = MaterialKind(kind)
        if kind is MaterialKind.GLAZE and not glaze_family:
            raise ValidationFailed("釉料必须声明釉料族")
        with self.db.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM material_lots WHERE material_code = ? AND lot_no = ?",
                (material_code, lot_no),
            ).fetchone()
            if exists:
                raise Conflict("材料批号已登记")
            conn.execute(
                "INSERT INTO material_lots(material_code, lot_no, kind, name, glaze_family,"
                " forbidden_grades, supplier, received_at, active)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (material_code, lot_no, kind.value, name, glaze_family,
                 ",".join(forbidden_grades or []), supplier, self._now()),
            )
            log_event(conn, self._now(), actor["id"], "material.registered", "material",
                      f"{material_code}:{lot_no}", {"kind": kind.value, "lot_no": lot_no})
        return {"material_code": material_code, "lot_no": lot_no, "kind": kind.value, "name": name}

    def deactivate_material_lot(
        self, material_code: str, lot_no: str, actor: sqlite3.Row
    ) -> None:
        self._require_actor(actor, _CRAFT_ROLES)
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE material_lots SET active = 0 WHERE material_code = ? AND lot_no = ?",
                (material_code, lot_no),
            )
            if cur.rowcount == 0:
                raise NotFound("材料批号不存在")
            log_event(conn, self._now(), actor["id"], "material.deactivated", "material",
                      f"{material_code}:{lot_no}", {})

    def _get_active_craft(self, conn: sqlite3.Connection, code: str, grade: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM craft_versions WHERE code = ? AND grade = ? AND status = 'active'",
            (code, grade),
        ).fetchone()
        if row is None:
            raise NotFound(f"年级 {grade} 没有可用的工艺版本 {code}")
        return row

    # ------------------------------------------------------------- 作品建档

    def create_work(
        self,
        student_id: str,
        actor: sqlite3.Row,
        device_id: str,
        content_hash: str,
        craft_code: str,
        safety_acks: list[int] | None = None,
        storage_location: str | None = None,
    ) -> dict:
        """教师为学生作品建档；同一设备重复上传相同内容将被拒绝。"""
        self._require_actor(actor, {Role.TEACHER})
        if not device_id or not content_hash:
            raise ValidationFailed("设备标识与内容指纹必填")
        fingerprint = _device_fingerprint(device_id, content_hash)
        with self.db.transaction() as conn:
            dup = conn.execute(
                "SELECT entity_id FROM idempotency_keys WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if dup is not None:
                raise DuplicateUpload(f"重复上传，作品已存在：{dup['entity_id']}")
            student = conn.execute(
                "SELECT * FROM students WHERE id = ? AND active = 1", (student_id,)
            ).fetchone()
            if student is None:
                raise NotFound("学生不存在")
            if craft_code is None:
                row = conn.execute(
                    "SELECT * FROM craft_versions WHERE grade = ? AND status = 'active'"
                    " ORDER BY version DESC LIMIT 1",
                    (student["grade"],),
                ).fetchone()
            else:
                row = self._get_active_craft(conn, craft_code, student["grade"])
            if row is None:
                raise NotFound(f"年级 {student['grade']} 尚未配置有效工艺版本")
            craft = row
            required = conn.execute(
                "SELECT seq FROM craft_safety WHERE code = ? AND version = ? ORDER BY seq",
                (craft["code"], craft["version"]),
            ).fetchall()
            acks = set(safety_acks or [])
            missing = [r["seq"] for r in required if r["seq"] not in acks]
            if missing:
                raise PrerequisiteNotMet(f"未确认全部安全前置条件，缺少：{missing}")

            work_id = "W" + secrets.token_hex(6)
            public_code = "P" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
            now = self._now()
            conn.execute(
                "INSERT INTO works(id, public_code, student_id, grade_snapshot, class_snapshot,"
                " craft_code, craft_version, current_step, status, storage_location,"
                " created_by, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                (work_id, public_code, student_id, student["grade"], student["class_group"],
                 craft["code"], craft["version"], WorkStatus.IN_PROGRESS.value,
                 storage_location, actor["id"], now, now),
            )
            for seq in acks:
                conn.execute(
                    "INSERT INTO work_safety_acks(work_id, seq, acked_by, acked_at)"
                    " VALUES (?, ?, ?, ?)",
                    (work_id, seq, actor["id"], now),
                )
            conn.execute(
                "INSERT INTO idempotency_keys(fingerprint, device_id, entity_type, entity_id, at)"
                " VALUES (?, ?, 'work', ?, ?)",
                (fingerprint, device_id, work_id, now),
            )
            log_event(conn, now, actor["id"], "work.created", "work", work_id,
                      {"student_id": student_id, "craft": f"{craft['code']}:{craft['version']}",
                       "device_id": device_id})
        return self.get_work(work_id, actor)

    def update_storage(self, work_id: str, location: str, actor: sqlite3.Row) -> dict:
        self._require_actor(actor, {Role.TEACHER})
        with self.db.transaction() as conn:
            work = self._locked_work(conn, work_id)
            if WorkStatus(work["status"]) in _TERMINAL_STATUSES:
                raise WorkNotEligible("终态作品不再变更保管位置")
            conn.execute(
                "UPDATE works SET storage_location = ?, updated_at = ? WHERE id = ?",
                (location, self._now(), work_id),
            )
            log_event(conn, self._now(), actor["id"], "work.storage_moved", "work", work_id,
                      {"location": location})
        return self.get_work(work_id, actor)

    # ------------------------------------------------------------- 工序签认

    def sign_step(
        self,
        work_id: str,
        step: str,
        actor: sqlite3.Row,
        material_code: str | None = None,
        material_lot: str | None = None,
        signed_for: str | None = None,
        note: str = "",
    ) -> dict:
        """记录一道工序签认。须严格按工序顺序，且满足干燥/釉料/材料限制。"""
        self._require_actor(actor, {Role.TEACHER})
        step = CraftStep(step)
        if step is CraftStep.FIRING:
            raise ValidationFailed("烧制工序由窑次流程自动签认")

        with self.db.transaction() as conn:
            work = self._locked_work(conn, work_id)
            status = WorkStatus(work["status"])
            if status in _TERMINAL_STATUSES or status in (
                WorkStatus.AWAITING_REVIEW, WorkStatus.READY,
                WorkStatus.SCHEDULED, WorkStatus.FIRED, WorkStatus.QC_FAILED,
            ):
                raise WorkNotEligible(f"作品处于 {status.value} 状态，不能记录工序")

            expected_index = 0 if work["current_step"] is None else (
                STEP_ORDER.index(CraftStep(work["current_step"])) + 1
            )
            if STEP_ORDER.index(step) != expected_index:
                raise StepOutOfOrder(
                    f"工序越级：当前应记录 {STEP_ORDER[expected_index].value}，而非 {step.value}"
                )

            # 代签：必须指向另一位在岗教师，实际责任人写入 signed_for。
            if signed_for is not None:
                if signed_for == actor["id"]:
                    raise ValidationFailed("不能为自己办理代签")
                target = conn.execute(
                    "SELECT 1 FROM users WHERE id = ? AND role = ?",
                    (signed_for, Role.TEACHER.value),
                ).fetchone()
                if target is None:
                    raise ValidationFailed("代签对象必须是教师账户")

            craft = conn.execute(
                "SELECT * FROM craft_versions WHERE code = ? AND version = ?",
                (work["craft_code"], work["craft_version"]),
            ).fetchone()

            material_payload = None
            if material_code or material_lot:
                if not (material_code and material_lot):
                    raise ValidationFailed("材料编码与批号必须同时提供")
                lot = conn.execute(
                    "SELECT * FROM material_lots WHERE material_code = ? AND lot_no = ?",
                    (material_code, material_lot),
                ).fetchone()
                if lot is None:
                    raise NotFound("材料批号不存在")
                if not lot["active"]:
                    raise MaterialForbidden("该材料批号已停用，不得使用")
                forbidden = [g for g in lot["forbidden_grades"].split(",") if g]
                if work["grade_snapshot"] in forbidden:
                    raise MaterialForbidden(
                        f"材料 {material_code} 禁止用于 {work['grade_snapshot']} 年级"
                    )
                if step in (CraftStep.WEDGING, CraftStep.THROWING, CraftStep.TRIMMING):
                    if lot["kind"] != MaterialKind.CLAY.value:
                        raise MaterialForbidden("该工序只能使用陶泥材料")
                if step is CraftStep.GLAZING:
                    if lot["kind"] != MaterialKind.GLAZE.value:
                        raise MaterialForbidden("施釉工序只能使用釉料")
                    compatible = (
                        lot["glaze_family"] == craft["glaze_family"]
                        or conn.execute(
                            "SELECT 1 FROM glaze_compat WHERE family = ? AND material_code = ?",
                            (craft["glaze_family"], material_code),
                        ).fetchone() is not None
                    )
                    if not compatible:
                        raise GlazeIncompatible(
                            f"釉料族 {lot['glaze_family']} 与工艺要求 {craft['glaze_family']} 不相容"
                        )
                material_payload = {
                    "material_code": material_code,
                    "lot_no": material_lot,
                    "name": lot["name"],
                    "supplier": lot["supplier"],
                }
            elif step in (CraftStep.WEDGING, CraftStep.GLAZING):
                raise ValidationFailed(f"{step.value} 工序必须记录材料批号")

            # 施釉前校验干燥时间：距修坯完成不少于工艺版本要求。
            if step is CraftStep.GLAZING:
                trim = conn.execute(
                    "SELECT signed_at FROM work_steps WHERE work_id = ? AND step = ?",
                    (work_id, CraftStep.TRIMMING.value),
                ).fetchone()
                if trim is None:
                    raise StepOutOfOrder("缺少修坯记录")
                drying = self.clock.now() - parse_iso(trim["signed_at"])
                required_hours = float(craft["min_drying_hours"])
                if drying.total_seconds() < required_hours * 3600:
                    waited = drying.total_seconds() / 3600
                    raise PrerequisiteNotMet(
                        f"干燥时间不足：已等待 {waited:.1f} 小时，要求 {required_hours:g} 小时"
                    )

            now = self._now()
            conn.execute(
                "INSERT INTO work_steps(work_id, step, seq, material_code, material_lot,"
                " signed_by, signed_for, signed_at, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (work_id, step, STEP_ORDER.index(step), material_code, material_lot,
                 actor["id"], signed_for, now, note),
            )
            new_status = (
                WorkStatus.AWAITING_REVIEW.value
                if step is CraftStep.GLAZING
                else work["status"]
            )
            conn.execute(
                "UPDATE works SET current_step = ?, status = ?, updated_at = ? WHERE id = ?",
                (step.value, new_status, now, work_id),
            )
            log_event(conn, now, actor["id"], "step.signed", "work", work_id,
                      {"step": step.value, "signed_for": signed_for, **(material_payload or {})})
        return self.get_work(work_id, actor)

    def review_work(self, work_id: str, actor: sqlite3.Row) -> dict:
        """教师复核通过后方可排批；复核人不得是施釉签认人（四眼原则）。"""
        self._require_actor(actor, {Role.TEACHER})
        with self.db.transaction() as conn:
            work = self._locked_work(conn, work_id)
            if WorkStatus(work["status"]) is not WorkStatus.AWAITING_REVIEW:
                raise WorkNotEligible("只有待复核作品可以复核")
            glazing = conn.execute(
                "SELECT signed_by FROM work_steps WHERE work_id = ? AND step = ?",
                (work_id, CraftStep.GLAZING.value),
            ).fetchone()
            if glazing and glazing["signed_by"] == actor["id"]:
                raise Forbidden("复核教师不能是施釉签认人，请由另一位教师复核")
            now = self._now()
            conn.execute(
                "UPDATE works SET status = ?, review_passed = 1, reviewed_by = ?,"
                " reviewed_at = ?, updated_at = ? WHERE id = ?",
                (WorkStatus.READY.value, actor["id"], now, now, work_id),
            )
            log_event(conn, now, actor["id"], "work.reviewed", "work", work_id, {})
        return self.get_work(work_id, actor)

    # ------------------------------------------------------------- 展示授权

    def _guardian_of(self, conn: sqlite3.Connection, actor_id: str, student_id: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM guardian_links WHERE guardian_id = ? AND student_id = ?",
            (actor_id, student_id),
        ).fetchone() is not None

    def grant_consent(self, work_id: str, actor: sqlite3.Row) -> dict:
        self._require_actor(actor, {Role.GUARDIAN})
        with self.db.transaction() as conn:
            work = self._locked_work(conn, work_id)
            if not self._guardian_of(conn, actor["id"], work["student_id"]):
                raise Forbidden("只能为自己监护的学生作品授权")
            now = self._now()
            conn.execute(
                "INSERT INTO consents(work_id, status, granted_by, granted_at, updated_at)"
                " VALUES (?, 'granted', ?, ?, ?)"
                " ON CONFLICT(work_id) DO UPDATE SET status = 'granted', granted_by = excluded.granted_by,"
                " granted_at = excluded.granted_at, withdrawn_at = NULL, updated_at = excluded.updated_at",
                (work_id, now, now, now),
            )
            log_event(conn, now, actor["id"], "consent.granted", "work", work_id, {})
        return {"work_id": work_id, "consent": "granted"}

    def withdraw_consent(self, work_id: str, actor: sqlite3.Row, reason: str = "") -> dict:
        """撤回展示授权：下一次公开视图查询立即排除该作品。"""
        self._require_actor(actor, {Role.GUARDIAN})
        with self.db.transaction() as conn:
            work = self._locked_work(conn, work_id)
            if not self._guardian_of(conn, actor["id"], work["student_id"]):
                raise Forbidden("只能撤回自己监护学生的作品授权")
            consent = conn.execute(
                "SELECT * FROM consents WHERE work_id = ?", (work_id,)
            ).fetchone()
            if consent is None or consent["status"] != "granted":
                raise Conflict("该作品当前不存在有效授权")
            now = self._now()
            conn.execute(
                "UPDATE consents SET status = 'withdrawn', withdrawn_at = ?, updated_at = ?"
                " WHERE work_id = ?",
                (now, now, work_id),
            )
            log_event(conn, now, actor["id"], "consent.withdrawn", "work", work_id,
                      {"reason": reason})
        return {"work_id": work_id, "consent": "withdrawn"}

    # ------------------------------------------------------------- 窑次与排批

    def create_kiln(self, batch_code: str, capacity: int, actor: sqlite3.Row, note: str = "") -> dict:
        self._require_actor(actor, {Role.TEACHER})
        try:
            validate_batch_code(batch_code)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if capacity <= 0:
            raise ValidationFailed("窑次容量必须为正整数")
        with self.db.transaction() as conn:
            exists = conn.execute("SELECT 1 FROM kilns WHERE batch_code = ?", (batch_code,)).fetchone()
            if exists:
                raise Conflict("窑次编号已存在")
            now = self._now()
            conn.execute(
                "INSERT INTO kilns(batch_code, capacity, status, note, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (batch_code, capacity, KilnStatus.PLANNED.value, note, actor["id"], now),
            )
            log_event(conn, now, actor["id"], "kiln.created", "kiln", batch_code,
                      {"capacity": capacity})
        return self.get_kiln(batch_code)

    def add_to_kiln(self, batch_code: str, work_id: str, actor: sqlite3.Row) -> dict:
        """把复核通过的作品排入窑次；事务内计数，并发下保证不超容量。"""
        self._require_actor(actor, {Role.TEACHER})
        with self.db.transaction() as conn:
            kiln = conn.execute("SELECT * FROM kilns WHERE batch_code = ?", (batch_code,)).fetchone()
            if kiln is None:
                raise NotFound("窑次不存在")
            if KilnStatus(kiln["status"]) is not KilnStatus.PLANNED:
                raise BatchNotEditable(f"窑次处于 {kiln['status']} 状态，不能调整排批")
            work = self._locked_work(conn, work_id)
            if WorkStatus(work["status"]) is not WorkStatus.READY or not work["review_passed"]:
                raise WorkNotEligible("只有复核通过的作品可以排批")
            if work["current_step"] != CraftStep.GLAZING.value:
                raise WorkNotEligible("作品尚未完成施釉")
            active_elsewhere = conn.execute(
                "SELECT 1 FROM kiln_members km JOIN kilns k ON km.batch_code = k.batch_code"
                " WHERE km.work_id = ? AND km.removed_at IS NULL"
                " AND k.status IN (?, ?, ?)",
                (work_id, KilnStatus.PLANNED.value, KilnStatus.FIRING.value, KilnStatus.DONE.value),
            ).fetchone()
            if active_elsewhere is not None:
                raise WorkNotEligible("作品已在其他未结束窑次中")

            count = conn.execute(
                "SELECT COUNT(*) AS c FROM kiln_members WHERE batch_code = ? AND removed_at IS NULL",
                (batch_code,),
            ).fetchone()["c"]
            if count >= kiln["capacity"]:
                raise BatchFull(f"窑次 {batch_code} 已满（容量 {kiln['capacity']}）")

            now = self._now()
            conn.execute(
                "INSERT INTO kiln_members(batch_code, work_id, added_by, added_at, removed_at)"
                " VALUES (?, ?, ?, ?, NULL)"
                " ON CONFLICT(batch_code, work_id) DO UPDATE SET"
                " removed_at = NULL, added_by = excluded.added_by, added_at = excluded.added_at",
                (batch_code, work_id, actor["id"], now),
            )
            conn.execute(
                "UPDATE works SET status = ?, updated_at = ? WHERE id = ?",
                (WorkStatus.SCHEDULED.value, now, work_id),
            )
            log_event(conn, now, actor["id"], "kiln.work_added", "work", work_id,
                      {"batch_code": batch_code})
            log_event(conn, now, actor["id"], "kiln.work_added", "kiln", batch_code,
                      {"work_id": work_id})
        return self.get_kiln(batch_code)

    def remove_from_kiln(self, batch_code: str, work_id: str, actor: sqlite3.Row) -> dict:
        self._require_actor(actor, {Role.TEACHER})
        with self.db.transaction() as conn:
            kiln = conn.execute("SELECT * FROM kilns WHERE batch_code = ?", (batch_code,)).fetchone()
            if kiln is None:
                raise NotFound("窑次不存在")
            if KilnStatus(kiln["status"]) is not KilnStatus.PLANNED:
                raise BatchNotEditable("窑次已开始，不能移出作品")
            member = conn.execute(
                "SELECT * FROM kiln_members WHERE batch_code = ? AND work_id = ? AND removed_at IS NULL",
                (batch_code, work_id),
            ).fetchone()
            if member is None:
                raise NotFound("作品不在该窑次中")
            now = self._now()
            conn.execute(
                "UPDATE kiln_members SET removed_at = ? WHERE id = ?", (now, member["id"])
            )
            conn.execute(
                "UPDATE works SET status = ?, updated_at = ? WHERE id = ?",
                (WorkStatus.READY.value, now, work_id),
            )
            log_event(conn, now, actor["id"], "kiln.work_removed", "work", work_id,
                      {"batch_code": batch_code})
        return self.get_kiln(batch_code)

    def start_firing(self, batch_code: str, actor: sqlite3.Row) -> dict:
        self._require_actor(actor, {Role.TEACHER})
        with self.db.transaction() as conn:
            kiln = self._locked_kiln(conn, batch_code)
            if KilnStatus(kiln["status"]) is not KilnStatus.PLANNED:
                raise BatchNotEditable("只有待烧窑次可以点火")
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM kiln_members WHERE batch_code = ? AND removed_at IS NULL",
                (batch_code,),
            ).fetchone()["c"]
            if count == 0:
                raise BatchNotEditable("空窑次不能点火")
            now = self._now()
            conn.execute(
                "UPDATE kilns SET status = ?, started_at = ? WHERE batch_code = ?",
                (KilnStatus.FIRING.value, now, batch_code),
            )
            log_event(conn, now, actor["id"], "kiln.started", "kiln", batch_code, {})
        return self.get_kiln(batch_code)

    def mark_done(self, batch_code: str, actor: sqlite3.Row) -> dict:
        """烧制结束：系统为每件作品自动补签 firing 工序，等待质检。"""
        self._require_actor(actor, {Role.TEACHER})
        with self.db.transaction() as conn:
            kiln = self._locked_kiln(conn, batch_code)
            if KilnStatus(kiln["status"]) is not KilnStatus.FIRING:
                raise BatchNotEditable("只有烧制中的窑次可以结束烧制")
            now = self._now()
            members = conn.execute(
                "SELECT work_id FROM kiln_members WHERE batch_code = ? AND removed_at IS NULL",
                (batch_code,),
            ).fetchall()
            for m in members:
                work_id = m["work_id"]
                conn.execute(
                    "INSERT INTO work_steps(work_id, step, seq, signed_by, signed_for, signed_at, note)"
                    " VALUES (?, ?, ?, ?, NULL, ?, ?)",
                    (work_id, CraftStep.FIRING.value, STEP_ORDER.index(CraftStep.FIRING),
                     actor["id"], now, f"窑次 {batch_code} 烧成自动签认"),
                )
                conn.execute(
                    "UPDATE works SET current_step = ?, status = ?, updated_at = ? WHERE id = ?",
                    (CraftStep.FIRING.value, WorkStatus.FIRED.value, now, work_id),
                )
                log_event(conn, now, actor["id"], "step.signed", "work", work_id,
                          {"step": CraftStep.FIRING.value, "batch_code": batch_code})
            conn.execute(
                "UPDATE kilns SET status = ?, finished_at = ? WHERE batch_code = ?",
                (KilnStatus.DONE.value, now, batch_code),
            )
            log_event(conn, now, actor["id"], "kiln.done", "kiln", batch_code, {})
        return self.get_kiln(batch_code)

    def quality_check(self, batch_code: str, passed: bool, actor: sqlite3.Row) -> dict:
        """质检：通过则作品成品；失败则窑次与证据保留，逐件等待处置。"""
        self._require_actor(actor, {Role.MASTER, Role.COORDINATOR})
        with self.db.transaction() as conn:
            kiln = self._locked_kiln(conn, batch_code)
            if KilnStatus(kiln["status"]) is not KilnStatus.DONE:
                raise BatchNotEditable("只有烧制结束的窑次可以质检")
            now = self._now()
            result = "passed" if passed else "failed"
            members = conn.execute(
                "SELECT work_id FROM kiln_members WHERE batch_code = ? AND removed_at IS NULL",
                (batch_code,),
            ).fetchall()
            if passed:
                new_kiln_status = KilnStatus.QC_PASSED.value
                for m in members:
                    conn.execute(
                        "UPDATE works SET status = ?, updated_at = ? WHERE id = ?",
                        (WorkStatus.FINISHED.value, now, m["work_id"]),
                    )
                    conn.execute(
                        "UPDATE kiln_members SET removed_at = ?"
                        " WHERE batch_code = ? AND work_id = ? AND removed_at IS NULL",
                        (now, batch_code, m["work_id"]),
                    )
                    log_event(conn, now, actor["id"], "work.finished", "work", m["work_id"],
                              {"batch_code": batch_code})
            else:
                new_kiln_status = KilnStatus.QC_FAILED.value
                for m in members:
                    conn.execute(
                        "UPDATE works SET status = ?, updated_at = ? WHERE id = ?",
                        (WorkStatus.QC_FAILED.value, now, m["work_id"]),
                    )
                    log_event(conn, now, actor["id"], "work.qc_failed", "work", m["work_id"],
                              {"batch_code": batch_code})
            conn.execute(
                "UPDATE kilns SET status = ?, qc_by = ?, qc_at = ?, qc_result = ? WHERE batch_code = ?",
                (new_kiln_status, actor["id"], now, result, batch_code),
            )
            log_event(conn, now, actor["id"], "kiln.qc", "kiln", batch_code, {"result": result})
        return self.get_kiln(batch_code)

    def cancel_kiln(self, batch_code: str, actor: sqlite3.Row, reason: str = "") -> dict:
        """窑炉取消：窑次、成员与签认全部保留为证据，在制作品转待处置。"""
        self._require_actor(actor, {Role.TEACHER, Role.COORDINATOR})
        with self.db.transaction() as conn:
            kiln = self._locked_kiln(conn, batch_code)
            state = KilnStatus(kiln["status"])
            if state not in (KilnStatus.PLANNED, KilnStatus.FIRING):
                raise BatchNotEditable(f"窑次处于 {state.value}，不能取消")
            now = self._now()
            members = conn.execute(
                "SELECT work_id FROM kiln_members WHERE batch_code = ? AND removed_at IS NULL",
                (batch_code,),
            ).fetchall()
            for m in members:
                conn.execute(
                    "UPDATE works SET status = ?, updated_at = ? WHERE id = ?",
                    (WorkStatus.QC_FAILED.value, now, m["work_id"]),
                )
                log_event(conn, now, actor["id"], "work.held_after_cancel", "work", m["work_id"],
                          {"batch_code": batch_code})
            conn.execute(
                "UPDATE kilns SET status = ?, note = ? WHERE batch_code = ?",
                (KilnStatus.CANCELED.value, reason or kiln["note"], batch_code),
            )
            log_event(conn, now, actor["id"], "kiln.canceled", "kiln", batch_code,
                      {"reason": reason})
        return self.get_kiln(batch_code)

    def dispose_work(
        self,
        batch_code: str,
        work_id: str,
        decision: str,
        actor: sqlite3.Row,
        reentry_step: str | None = None,
        note: str = "",
    ) -> dict:
        """对失败/取消窑次中的作品逐件决定返工、报废或重新排批。"""
        self._require_actor(actor, {Role.TEACHER, Role.MASTER, Role.COORDINATOR})
        decision = WorkDisposition(decision)
        with self.db.transaction() as conn:
            kiln = conn.execute("SELECT * FROM kilns WHERE batch_code = ?", (batch_code,)).fetchone()
            if kiln is None:
                raise NotFound("窑次不存在")
            if KilnStatus(kiln["status"]) not in (KilnStatus.QC_FAILED, KilnStatus.CANCELED):
                raise InvalidDisposition("只有质检失败或已取消的窑次可以处置作品")
            member = conn.execute(
                "SELECT * FROM kiln_members WHERE batch_code = ? AND work_id = ? AND removed_at IS NULL",
                (batch_code, work_id),
            ).fetchone()
            if member is None:
                raise NotFound("作品不在该窑次的待处置清单中")
            work = self._locked_work(conn, work_id)
            if WorkStatus(work["status"]) is not WorkStatus.QC_FAILED:
                raise InvalidDisposition("作品当前状态不需要处置")

            already = conn.execute(
                "SELECT 1 FROM dispositions WHERE batch_code = ? AND work_id = ?",
                (batch_code, work_id),
            ).fetchone()
            if already is not None:
                raise InvalidDisposition("该作品在此窑次已有处置决定")

            now = self._now()
            if decision is WorkDisposition.REWORK:
                if reentry_step is None:
                    raise ValidationFailed("返工必须指定重做起始工序")
                entry = CraftStep(reentry_step)
                if entry not in REWORK_ENTRY_STEPS:
                    raise InvalidDisposition(
                        "返工只能从修坯(trimming)或施釉(glazing)重新开始"
                    )
                self._void_steps_from(conn, work_id, entry, actor["id"], now, batch_code)
                conn.execute(
                    "UPDATE works SET status = ?, review_passed = 0, reviewed_by = NULL,"
                    " reviewed_at = NULL, updated_at = ? WHERE id = ?",
                    (WorkStatus.REWORK.value, now, work_id),
                )
                new_status = WorkStatus.REWORK.value
            elif decision is WorkDisposition.DISCARD:
                conn.execute(
                    "UPDATE works SET status = ?, updated_at = ? WHERE id = ?",
                    (WorkStatus.DISCARDED.value, now, work_id),
                )
                new_status = WorkStatus.DISCARDED.value
            else:  # RESCHEDULE
                # 已自动签认的 firing 记录作废留痕，作品带着原复核结论回到待排批。
                firing = conn.execute(
                    "SELECT * FROM work_steps WHERE work_id = ? AND step = ?",
                    (work_id, CraftStep.FIRING.value),
                ).fetchone()
                if firing is not None:
                    self._void_steps_from(conn, work_id, CraftStep.FIRING, actor["id"], now, batch_code)
                conn.execute(
                    "UPDATE works SET status = ?, updated_at = ? WHERE id = ?",
                    (WorkStatus.READY.value, now, work_id),
                )
                new_status = WorkStatus.READY.value

            conn.execute(
                "INSERT INTO dispositions(batch_code, work_id, decision, reentry_step,"
                " decided_by, decided_at, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (batch_code, work_id, decision.value, reentry_step, actor["id"], now, note),
            )
            conn.execute(
                "UPDATE kiln_members SET removed_at = ? WHERE id = ?", (now, member["id"])
            )
            log_event(conn, now, actor["id"], "work.disposed", "work", work_id,
                      {"batch_code": batch_code, "decision": decision.value,
                       "reentry_step": reentry_step})
        return self.get_work(work_id, actor)

    def _void_steps_from(
        self,
        conn: sqlite3.Connection,
        work_id: str,
        from_step: CraftStep,
        actor_id: str,
        now: str,
        batch_code: str,
    ) -> None:
        """作废从指定工序起的签认记录；删除前转写事件日志，证据不丢。"""
        threshold = STEP_ORDER.index(from_step)
        rows = conn.execute(
            "SELECT * FROM work_steps WHERE work_id = ? AND seq >= ?", (work_id, threshold)
        ).fetchall()
        for r in rows:
            log_event(conn, now, actor_id, "step.voided", "work", work_id,
                      {"step": r["step"], "signed_by": r["signed_by"],
                       "signed_at": r["signed_at"], "material_code": r["material_code"],
                       "material_lot": r["material_lot"], "batch_code": batch_code})
            conn.execute(
                "DELETE FROM work_steps WHERE work_id = ? AND step = ?",
                (work_id, r["step"]),
            )
        kept = conn.execute(
            "SELECT step FROM work_steps WHERE work_id = ? ORDER BY seq DESC LIMIT 1", (work_id,)
        ).fetchone()
        current = kept["step"] if kept else None
        conn.execute("UPDATE works SET current_step = ? WHERE id = ?", (current, work_id))

    # ------------------------------------------------------------- 查询

    @staticmethod
    def _locked_work(conn: sqlite3.Connection, work_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM works WHERE id = ?", (work_id,)).fetchone()
        if row is None:
            raise NotFound("作品不存在")
        return row

    @staticmethod
    def _locked_kiln(conn: sqlite3.Connection, batch_code: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM kilns WHERE batch_code = ?", (batch_code,)).fetchone()
        if row is None:
            raise NotFound("窑次不存在")
        return row

    def _work_steps(self, conn: sqlite3.Connection, work_id: str) -> list[dict]:
        rows = conn.execute(
            "SELECT ws.*, u.display_name AS signer_name,"
            " u2.display_name AS signed_for_name, ml.name AS material_name,"
            " ml.supplier AS material_supplier"
            " FROM work_steps ws"
            " JOIN users u ON ws.signed_by = u.id"
            " LEFT JOIN users u2 ON ws.signed_for = u2.id"
            " LEFT JOIN material_lots ml ON ws.material_code = ml.material_code"
            " AND ws.material_lot = ml.lot_no"
            " WHERE ws.work_id = ? ORDER BY ws.seq",
            (work_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_work(self, work_id: str, actor: sqlite3.Row) -> dict:
        self._require_actor(actor, {Role.TEACHER, Role.COORDINATOR, Role.MASTER})
        with self.db.read() as conn:
            work = conn.execute(
                "SELECT w.*, s.name AS student_name, s.grade AS student_grade,"
                " cv.name AS craft_name, cv.glaze_family, cv.status AS craft_status"
                " FROM works w JOIN students s ON w.student_id = s.id"
                " JOIN craft_versions cv ON w.craft_code = cv.code AND w.craft_version = cv.version"
                " WHERE w.id = ?",
                (work_id,),
            ).fetchone()
            if work is None:
                raise NotFound("作品不存在")
            data = dict(work)
            data["steps"] = self._work_steps(conn, work_id)
            consent = conn.execute(
                "SELECT status, withdrawn_at FROM consents WHERE work_id = ?", (work_id,)
            ).fetchone()
            data["consent"] = dict(consent) if consent else None
            return data

    def list_works(
        self, actor: sqlite3.Row, status: str | None = None, student_id: str | None = None
    ) -> list[dict]:
        self._require_actor(actor, {Role.TEACHER, Role.COORDINATOR, Role.MASTER})
        sql = (
            "SELECT w.*, s.name AS student_name FROM works w"
            " JOIN students s ON w.student_id = s.id WHERE 1=1"
        )
        params: list = []
        if status:
            sql += " AND w.status = ?"
            params.append(WorkStatus(status).value)
        if student_id:
            sql += " AND w.student_id = ?"
            params.append(student_id)
        sql += " ORDER BY w.created_at"
        with self.db.read() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_kiln(self, batch_code: str) -> dict:
        with self.db.read() as conn:
            kiln = conn.execute("SELECT * FROM kilns WHERE batch_code = ?", (batch_code,)).fetchone()
            if kiln is None:
                raise NotFound("窑次不存在")
            data = dict(kiln)
            members = conn.execute(
                "SELECT km.*, w.public_code, w.status AS work_status, s.id AS student_id,"
                " s.name AS student_name, d.decision, d.reentry_step"
                " FROM kiln_members km"
                " JOIN works w ON km.work_id = w.id"
                " JOIN students s ON w.student_id = s.id"
                " LEFT JOIN dispositions d ON d.batch_code = km.batch_code AND d.work_id = km.work_id"
                " WHERE km.batch_code = ? ORDER BY km.id",
                (batch_code,),
            ).fetchall()
            data["members"] = [dict(m) for m in members]
            data["active_count"] = sum(1 for m in members if m["removed_at"] is None)
            return data

    def list_kilns(self, actor: sqlite3.Row) -> list[dict]:
        self._require_actor(actor, {Role.TEACHER, Role.COORDINATOR, Role.MASTER})
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT k.*, (SELECT COUNT(*) FROM kiln_members km"
                " WHERE km.batch_code = k.batch_code AND km.removed_at IS NULL) AS active_count"
                " FROM kilns k ORDER BY k.created_at"
            ).fetchall()
            return [dict(r) for r in rows]

    def public_works(self) -> list[dict]:
        """公开视图：仅成品且授权有效。白名单序列化，绝不包含任何未成年人身份字段。"""
        with self.db.read() as conn:
            works = conn.execute(
                "SELECT w.id, w.public_code, w.craft_code, w.craft_version, cv.name AS craft_name,"
                " cv.status AS craft_status, cv.glaze_family, w.updated_at"
                " FROM works w"
                " JOIN craft_versions cv ON w.craft_code = cv.code AND w.craft_version = cv.version"
                " JOIN consents c ON c.work_id = w.id AND c.status = 'granted'"
                " WHERE w.status = ? ORDER BY w.updated_at DESC",
                (WorkStatus.FINISHED.value,),
            ).fetchall()
            result = []
            for w in works:
                steps = conn.execute(
                    "SELECT ws.step, ws.signed_at, ws.material_code, ws.material_lot,"
                    " ml.name AS material_name, ml.supplier, u.display_name AS signer_name"
                    " FROM work_steps ws"
                    " JOIN users u ON ws.signed_by = u.id"
                    " LEFT JOIN material_lots ml ON ws.material_code = ml.material_code"
                    " AND ws.material_lot = ml.lot_no"
                    " WHERE ws.work_id = ? ORDER BY ws.seq",
                    (w["id"],),
                ).fetchall()
                batch = conn.execute(
                    "SELECT km.batch_code FROM kiln_members km"
                    " JOIN kilns k ON km.batch_code = k.batch_code"
                    " WHERE km.work_id = ? AND k.status = ? ORDER BY km.id DESC LIMIT 1",
                    (w["id"], KilnStatus.QC_PASSED.value),
                ).fetchone()
                result.append({
                    "public_code": w["public_code"],
                    "craft": {
                        "code": w["craft_code"],
                        "version": w["craft_version"],
                        "name": w["craft_name"],
                        "effective": w["craft_status"] == "active",
                    },
                    "batch_code": batch["batch_code"] if batch else None,
                    "finished_at": w["updated_at"],
                    "provenance": [
                        {
                            "step": s["step"],
                            "signed_at": s["signed_at"],
                            "signer": s["signer_name"],
                            "material_code": s["material_code"],
                            "material_lot": s["material_lot"],
                            "material_name": s["material_name"],
                            "supplier": s["supplier"],
                        }
                        for s in steps
                    ],
                })
            return result

    def work_history(self, work_id: str, actor: sqlite3.Row) -> dict:
        """作品沿革导出：工艺版本、全部签认（含作废）、授权、窑次与处置、事件流。"""
        self._require_actor(actor, {Role.TEACHER, Role.COORDINATOR, Role.MASTER})
        with self.db.read() as conn:
            work = conn.execute(
                "SELECT w.*, s.name AS student_name, s.grade AS student_grade,"
                " s.class_group AS student_class, cv.name AS craft_name,"
                " cv.min_drying_hours, cv.glaze_family, cv.status AS craft_status"
                " FROM works w JOIN students s ON w.student_id = s.id"
                " JOIN craft_versions cv ON w.craft_code = cv.code AND w.craft_version = cv.version"
                " WHERE w.id = ?",
                (work_id,),
            ).fetchone()
            if work is None:
                raise NotFound("作品不存在")
            safety = conn.execute(
                "SELECT cs.seq, cs.requirement, sa.acked_by, sa.acked_at FROM craft_safety cs"
                " LEFT JOIN work_safety_acks sa ON sa.work_id = ? AND sa.seq = cs.seq"
                " WHERE cs.code = ? AND cs.version = ? ORDER BY cs.seq",
                (work_id, *[work["craft_code"], work["craft_version"]]),
            ).fetchall()
            memberships = conn.execute(
                "SELECT km.batch_code, km.added_at, km.removed_at, k.status AS batch_status,"
                " d.decision, d.reentry_step, d.decided_at, d.note AS disposition_note"
                " FROM kiln_members km JOIN kilns k ON km.batch_code = k.batch_code"
                " LEFT JOIN dispositions d ON d.batch_code = km.batch_code AND d.work_id = km.work_id"
                " WHERE km.work_id = ? ORDER BY km.id",
                (work_id,),
            ).fetchall()
            consent = conn.execute("SELECT * FROM consents WHERE work_id = ?", (work_id,)).fetchone()
            events = conn.execute(
                "SELECT at, actor, action, payload FROM events"
                " WHERE entity_type = 'work' AND entity_id = ? ORDER BY id",
                (work_id,),
            ).fetchall()
            import json
            return {
                "work": dict(work),
                "steps": self._work_steps(conn, work_id),
                "safety_acknowledgements": [dict(r) for r in safety],
                "consent": dict(consent) if consent else None,
                "kiln_memberships": [dict(m) for m in memberships],
                "events": [
                    {"at": e["at"], "actor": e["actor"], "action": e["action"],
                     "payload": json.loads(e["payload"])}
                    for e in events
                ],
            }
