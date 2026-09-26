"""领域服务规则测试。"""

import threading
import unittest

from src.pottery import (
    Clock,
    CraftStep,
    KilnStatus,
    WorkDisposition,
    WorkStatus,
)
from src.pottery.errors import (
    BatchFull,
    BatchNotEditable,
    Conflict,
    DuplicateUpload,
    Forbidden,
    GlazeIncompatible,
    MaterialForbidden,
    NotFound,
    PrerequisiteNotMet,
    StepOutOfOrder,
    ValidationFailed,
    WorkNotEligible,
)

from _fixtures import build_world, make_ready_work, run_kiln_to_passed


class CraftVersionTests(unittest.TestCase):
    def setUp(self):
        self.w = build_world()
        self.coord = self.w.actor("coord")
        self.teacher = self.w.actor("teacher")
        self.master = self.w.actor("master")

    def test_grade_without_craft_rejects_work(self):
        # 新建一个未配置工艺的年级
        self.w.svc.create_student("S-9", "周小新", "九年级", "九(1)班", self.coord)
        with self.assertRaises(NotFound):
            self.w.svc.create_work(
                "S-9", self.teacher, "D", "h", craft_code="JUNIOR-POTTERY", safety_acks=[])

    def test_safety_prerequisites_must_all_be_acknowledged(self):
        with self.assertRaises(PrerequisiteNotMet):
            self.w.svc.create_work(
                "S-1", self.teacher, "D", "h",
                craft_code="JUNIOR-POTTERY", safety_acks=[0])

    def test_new_version_pins_existing_works(self):
        work = self.w.svc.create_work(
            "S-1", self.teacher, "D", "h1",
            craft_code="JUNIOR-POTTERY", safety_acks=[0, 1])
        self.assertEqual(work["craft_version"], 1)
        # 传承人发布 v2（干燥要求改为 72 小时）
        self.w.svc.create_craft_version(
            "JUNIOR-POTTERY", "七年级", "初中陶艺基础(修订)",
            72.0, "stoneware", ["佩戴护具", "保持工位整洁", "检查窑炉线路"],
            self.master)
        old = self.w.svc.get_work(work["id"], self.teacher)
        self.assertEqual(old["craft_version"], 1)  # 旧作品钉选 v1
        new = self.w.svc.create_work(
            "S-2", self.teacher, "D", "h2",
            craft_code="JUNIOR-POTTERY", safety_acks=[0, 1, 2])
        self.assertEqual(new["craft_version"], 3)
        self.assertEqual(new["craft_status"], "active")
        # 八年级的 v2 不受同年级失效影响
        work8 = self.w.svc.create_work(
            "S-3", self.teacher, "D", "h8",
            craft_code="JUNIOR-POTTERY", safety_acks=[0])
        self.assertEqual(work8["craft_version"], 2)


class StepRuleTests(unittest.TestCase):
    def setUp(self):
        self.w = build_world(min_drying_hours=24)
        self.t = self.w.actor("teacher")
        work = self.w.svc.create_work(
            "S-1", self.t, "D", "h", craft_code="JUNIOR-POTTERY", safety_acks=[0, 1])
        self.wid = work["id"]

    def test_steps_cannot_skip(self):
        with self.assertRaises(StepOutOfOrder):
            self.w.svc.sign_step(self.wid, "throwing", self.t)

    def test_wedging_requires_clay_lot(self):
        with self.assertRaises(ValidationFailed):
            self.w.svc.sign_step(self.wid, "wedging", self.t)

    def test_glaze_requires_glaze_lot(self):
        self.w.svc.sign_step(self.wid, "wedging", self.t,
                             material_code="CLAY-A", material_lot="L2026-01")
        self.w.svc.sign_step(self.wid, "throwing", self.t)
        self.w.svc.sign_step(self.wid, "trimming", self.t)
        self.w.clock.advance(hours=25)
        with self.assertRaises(MaterialForbidden):
            self.w.svc.sign_step(self.wid, "glazing", self.t,
                                 material_code="CLAY-A", material_lot="L2026-01")

    def test_drying_time_enforced_with_controlled_clock(self):
        self.w.svc.sign_step(self.wid, "wedging", self.t,
                             material_code="CLAY-A", material_lot="L2026-01")
        self.w.svc.sign_step(self.wid, "throwing", self.t)
        self.w.svc.sign_step(self.wid, "trimming", self.t)
        # 只过 10 小时，不足 24 小时
        self.w.clock.advance(hours=10)
        with self.assertRaises(PrerequisiteNotMet):
            self.w.svc.sign_step(self.wid, "glazing", self.t,
                                 material_code="GZ-STD", material_lot="L2026-02")
        # 再等 15 小时，累计 25 小时，通过
        self.w.clock.advance(hours=15)
        self.w.svc.sign_step(self.wid, "glazing", self.t,
                             material_code="GZ-STD", material_lot="L2026-02")
        self.assertEqual(self.w.svc.get_work(self.wid, self.t)["status"],
                         WorkStatus.AWAITING_REVIEW.value)

    def test_glaze_family_incompatible(self):
        self.w.svc.sign_step(self.wid, "wedging", self.t,
                             material_code="CLAY-A", material_lot="L2026-01")
        self.w.svc.sign_step(self.wid, "throwing", self.t)
        self.w.svc.sign_step(self.wid, "trimming", self.t)
        self.w.clock.advance(hours=25)
        # porcelain 族釉料与七年级 stoneware 工艺不相容
        with self.assertRaises(GlazeIncompatible):
            self.w.svc.sign_step(self.wid, "glazing", self.t,
                                 material_code="GZ-PORC", material_lot="L2026-03")
        # 登记跨族兼容后放行
        self.w.svc.add_glaze_compat("stoneware", "GZ-SPECIAL", self.w.actor("coord"))
        self.w.svc.sign_step(self.wid, "glazing", self.t,
                             material_code="GZ-SPECIAL", material_lot="L2026-04")

    def test_material_forbidden_for_grade(self):
        self.w.svc.sign_step(self.wid, "wedging", self.t,
                             material_code="CLAY-A", material_lot="L2026-01")
        # 七年级禁用 CLAY-B
        with self.assertRaises(MaterialForbidden):
            self.w.svc.sign_step(
                self.wid, "throwing", self.t,
                material_code="CLAY-B", material_lot="L2026-05")
        # 八年级可用
        work8 = self.w.svc.create_work(
            "S-3", self.t, "D", "h8", craft_code="JUNIOR-POTTERY", safety_acks=[0])
        self.w.svc.sign_step(work8["id"], "wedging", self.t,
                             material_code="CLAY-B", material_lot="L2026-05")

    def test_deactivated_lot_blocked(self):
        self.w.svc.deactivate_material_lot("CLAY-A", "L2026-01", self.w.actor("master"))
        with self.assertRaises(MaterialForbidden):
            self.w.svc.sign_step(self.wid, "wedging", self.t,
                                 material_code="CLAY-A", material_lot="L2026-01")

    def test_duplicate_upload_rejected_same_device(self):
        self.w.svc.create_work(
            "S-1", self.t, "DEV-TAB-7", "same-hash",
            craft_code="JUNIOR-POTTERY", safety_acks=[0, 1])
        with self.assertRaises(DuplicateUpload):
            self.w.svc.create_work(
                "S-2", self.t, "DEV-TAB-7", "same-hash",
                craft_code="JUNIOR-POTTERY", safety_acks=[0, 1])
        # 同设备不同内容允许
        second = self.w.svc.create_work(
            "S-2", self.t, "DEV-TAB-7", "different-hash",
            craft_code="JUNIOR-POTTERY", safety_acks=[0, 1])
        self.assertTrue(second["id"])

    def test_proxy_signature_requires_another_teacher(self):
        with self.assertRaises(ValidationFailed):
            self.w.svc.sign_step(
                self.wid, "wedging", self.t,
                material_code="CLAY-A", material_lot="L2026-01",
                signed_for=self.t["id"])
        with self.assertRaises(ValidationFailed):
            self.w.svc.sign_step(
                self.wid, "wedging", self.t,
                material_code="CLAY-A", material_lot="L2026-01",
                signed_for="U-G1")  # 监护人不是教师
        self.w.svc.sign_step(
            self.wid, "wedging", self.t,
            material_code="CLAY-A", material_lot="L2026-01",
            signed_for="U-T2")
        steps = self.w.svc.get_work(self.wid, self.t)["steps"]
        self.assertEqual(steps[0]["signed_for"], "U-T2")
        self.assertEqual(steps[0]["signed_for_name"], "陈老师")


class ReviewAndConsentTests(unittest.TestCase):
    def setUp(self):
        self.w = build_world()
        self.t1 = self.w.actor("teacher")
        self.t2 = self.w.actor("teacher2")
        self.g = self.w.actor("guardian")
        self.wid = make_ready_work(self.w)

    def test_reviewer_must_differ_from_glazing_signer(self):
        # 已由 t2 复核完成；重复复核被拒
        with self.assertRaises(WorkNotEligible):
            self.w.svc.review_work(self.wid, self.t2)

    def test_four_eyes_principle(self):
        # 停在待复核：施釉人 t1 不能复核，t2 可以
        wid = make_ready_work(self.w, device="DEV-2", content="photo-2",
                              reviewer_key=None)
        with self.assertRaises(Forbidden):
            self.w.svc.review_work(wid, self.t1)
        self.w.svc.review_work(wid, self.t2)

    def test_consent_withdraw_immediately_removes_public_view(self):
        self.assertNotIn(self.wid, [p["public_code"] for p in self.w.svc.public_works()])
        # 作品还未烧成，授权也不会公开；走完出窑
        kiln = self.w.svc.create_kiln("KILN-2026-001", 10, self.t1)
        run_kiln_to_passed(self.w, kiln["batch_code"], [self.wid])
        self.w.svc.grant_consent(self.wid, self.g)
        public = self.w.svc.public_works()
        self.assertEqual(len(public), 1)

        entry = public[0]
        # 公开视图不得含任何未成年人身份字段
        serialized = repr(entry)
        for pii in ("赵小明", "S-1", "student", "七(1)班"):
            self.assertNotIn(pii, serialized)
        # 但必须能说明材料来源、经手人与有效工艺版本
        self.assertEqual(entry["craft"]["code"], "JUNIOR-POTTERY")
        steps = {p["step"]: p for p in entry["provenance"]}
        self.assertEqual(steps["wedging"]["material_lot"], "L2026-01")
        self.assertEqual(steps["wedging"]["supplier"], "南山陶土厂")
        self.assertTrue(steps["firing"]["signer"])

        # 撤回授权：立即生效
        self.w.svc.withdraw_consent(self.wid, self.g, reason="家长临时撤回")
        self.assertEqual(self.w.svc.public_works(), [])

        # 重新授权后再次出现
        self.w.svc.grant_consent(self.wid, self.g)
        self.assertEqual(len(self.w.svc.public_works()), 1)

    def test_guardian_cannot_consent_for_unrelated_student(self):
        wid = make_ready_work(self.w, student_id="S-2", device="DEV-3", content="photo-3")
        with self.assertRaises(Forbidden):
            self.w.svc.grant_consent(wid, self.g)

    def test_withdraw_without_grant_conflicts(self):
        wid = make_ready_work(self.w, student_id="S-1", device="DEV-4", content="photo-4")
        with self.assertRaises(Conflict):
            self.w.svc.withdraw_consent(wid, self.g)


class TransferTests(unittest.TestCase):
    def test_transfer_keeps_historical_snapshot(self):
        w = build_world()
        coord = w.actor("coord")
        t = w.actor("teacher")
        work = w.svc.create_work(
            "S-1", t, "D", "h", craft_code="JUNIOR-POTTERY", safety_acks=[0, 1])
        self.assertEqual(work["class_snapshot"], "七(1)班")
        w.svc.transfer_student("S-1", "七(3)班", coord)
        # 历史作品班级快照不变
        self.assertEqual(w.svc.get_work(work["id"], t)["class_snapshot"], "七(1)班")
        new_work = w.svc.create_work(
            "S-1", t, "D", "h2", craft_code="JUNIOR-POTTERY", safety_acks=[0, 1])
        self.assertEqual(new_work["class_snapshot"], "七(3)班")


class KilnCapacityConcurrencyTests(unittest.TestCase):
    def test_concurrent_scheduling_never_exceeds_capacity(self):
        w = build_world()
        t = w.actor("teacher")
        t2 = w.actor("teacher2")
        capacity = 3
        total = 12
        kiln = w.svc.create_kiln("KILN-2026-050", capacity, t)

        ready_ids = []
        for i in range(total):
            wid = make_ready_work(
                w, student_id=("S-1" if i % 2 == 0 else "S-2"),
                device=f"DEV-{i}", content=f"photo-{i}")
            ready_ids.append(wid)

        successes: list[str] = []
        failures: list[Exception] = []
        barrier = threading.Barrier(total)
        lock = threading.Lock()

        def add(work_id):
            barrier.wait()  # 尽量放大竞争
            try:
                w.svc.add_to_kiln(kiln["batch_code"], work_id, t)
                with lock:
                    successes.append(work_id)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    failures.append(exc)

        threads = [threading.Thread(target=add, args=(wid,)) for wid in ready_ids]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        kiln_after = w.svc.get_kiln(kiln["batch_code"])
        self.assertEqual(len(successes), capacity)
        self.assertEqual(kiln_after["active_count"], capacity)
        self.assertEqual(len(failures), total - capacity)
        self.assertTrue(all(isinstance(f, BatchFull) for f in failures))

        # 未排进的作品仍是 ready，可排入别的窑次
        leftover = [wid for wid in ready_ids if wid not in successes]
        kiln2 = w.svc.create_kiln("KILN-2026-051", 20, t)
        w.svc.add_to_kiln(kiln2["batch_code"], leftover[0], t)

    def test_work_cannot_be_in_two_active_kilns(self):
        w = build_world()
        t = w.actor("teacher")
        wid = make_ready_work(w)
        k1 = w.svc.create_kiln("KILN-2026-060", 5, t)
        k2 = w.svc.create_kiln("KILN-2026-061", 5, t)
        w.svc.add_to_kiln(k1["batch_code"], wid, t)
        with self.assertRaises(WorkNotEligible):
            w.svc.add_to_kiln(k2["batch_code"], wid, t)

    def test_only_ready_works_schedule(self):
        w = build_world()
        t = w.actor("teacher")
        kiln = w.svc.create_kiln("KILN-2026-062", 5, t)
        work = w.svc.create_work(
            "S-1", t, "D", "h", craft_code="JUNIOR-POTTERY", safety_acks=[0, 1])
        with self.assertRaises(WorkNotEligible):
            w.svc.add_to_kiln(kiln["batch_code"], work["id"], t)

    def test_kiln_locked_after_start(self):
        w = build_world()
        t = w.actor("teacher")
        wid = make_ready_work(w)
        kiln = w.svc.create_kiln("KILN-2026-063", 5, t)
        w.svc.add_to_kiln(kiln["batch_code"], wid, t)
        w.svc.start_firing(kiln["batch_code"], t)
        with self.assertRaises(BatchNotEditable):
            w.svc.start_firing(kiln["batch_code"], t)
        wid2 = make_ready_work(w, device="D2", content="c2")
        with self.assertRaises(BatchNotEditable):
            w.svc.add_to_kiln(kiln["batch_code"], wid2, t)


class FailureRecoveryTests(unittest.TestCase):
    def _fired_failed_kiln(self, n=2):
        w = build_world()
        t = w.actor("teacher")
        m = w.actor("master")
        ids = [make_ready_work(w, device=f"D{i}", content=f"c{i}",
                               student_id=("S-1" if i % 2 == 0 else "S-2"))
               for i in range(n)]
        kiln = w.svc.create_kiln("KILN-2026-070", 10, t)
        for wid in ids:
            w.svc.add_to_kiln(kiln["batch_code"], wid, t)
        w.svc.start_firing(kiln["batch_code"], t)
        w.svc.mark_done(kiln["batch_code"], t)
        w.svc.quality_check(kiln["batch_code"], False, m)
        return w, t, m, kiln, ids

    def test_qc_failure_preserves_evidence_and_requires_disposition(self):
        w, t, m, kiln, ids = self._fired_failed_kiln()
        kiln_after = w.svc.get_kiln(kiln["batch_code"])
        self.assertEqual(kiln_after["status"], KilnStatus.QC_FAILED.value)
        # 成员与 firing 签认仍然保留
        self.assertEqual(len(kiln_after["members"]), 2)
        work = w.svc.get_work(ids[0], t)
        self.assertEqual(work["status"], WorkStatus.QC_FAILED.value)
        self.assertIn(CraftStep.FIRING.value, [s["step"] for s in work["steps"]])
        # 重复处置被拒
        w.svc.dispose_work(kiln["batch_code"], ids[0], "rework", m, reentry_step="glazing")
        with self.assertRaises(Exception):
            w.svc.dispose_work(kiln["batch_code"], ids[0], "discard", m)

    def test_rework_restarts_from_glazing_and_review_required(self):
        w, t, m, kiln, ids = self._fired_failed_kiln()
        wid = ids[0]
        w.svc.dispose_work(kiln["batch_code"], wid, "rework", m, reentry_step="glazing")
        work = w.svc.get_work(wid, t)
        self.assertEqual(work["status"], WorkStatus.REWORK.value)
        # glazing/firing 已作废，trimming 保留
        remaining = [s["step"] for s in work["steps"]]
        self.assertEqual(remaining, ["wedging", "throwing", "trimming"])
        self.assertFalse(work["review_passed"])
        # 作废记录仍可在沿革事件中找到
        history = w.svc.work_history(wid, t)
        voided = [e for e in history["events"] if e["action"] == "step.voided"]
        self.assertEqual({e["payload"]["step"] for e in voided}, {"glazing", "firing"})

        # 重新施釉（仍需满足干燥时间）→ 复核 → 重新排批
        w.clock.advance(hours=30)
        w.svc.sign_step(wid, "glazing", t, material_code="GZ-STD", material_lot="L2026-02")
        w.svc.review_work(wid, w.actor("teacher2"))
        new_kiln = w.svc.create_kiln("KILN-2026-071", 10, t)
        w.svc.add_to_kiln(new_kiln["batch_code"], wid, t)

    def test_rework_entry_must_be_trim_or_glaze(self):
        w, t, m, kiln, ids = self._fired_failed_kiln(n=1)
        with self.assertRaises(Exception):
            w.svc.dispose_work(kiln["batch_code"], ids[0], "rework", m,
                               reentry_step="wedging")

    def test_discard_is_terminal(self):
        w, t, m, kiln, ids = self._fired_failed_kiln(n=1)
        w.svc.dispose_work(kiln["batch_code"], ids[0], "discard", m, note="开裂严重")
        work = w.svc.get_work(ids[0], t)
        self.assertEqual(work["status"], WorkStatus.DISCARDED.value)
        with self.assertRaises(WorkNotEligible):
            w.svc.update_storage(ids[0], "废品柜", t)

    def test_reschedule_goes_back_to_ready_and_passes_next_kiln(self):
        w, t, m, kiln, ids = self._fired_failed_kiln(n=1)
        wid = ids[0]
        w.svc.dispose_work(kiln["batch_code"], wid, "reschedule", m, note="釉色问题可复烧")
        work = w.svc.get_work(wid, t)
        self.assertEqual(work["status"], WorkStatus.READY.value)
        # firing 已作废，但复核结论保留
        self.assertTrue(work["review_passed"])
        self.assertNotIn("firing", [s["step"] for s in work["steps"]])
        # 旧批次证据仍在
        old = w.svc.get_kiln(kiln["batch_code"])
        self.assertEqual(len(old["members"]), 1)
        new_kiln = w.svc.create_kiln("KILN-2026-072", 10, t)
        run_kiln_to_passed(w, new_kiln["batch_code"], [wid])
        self.assertEqual(w.svc.get_work(wid, t)["status"], WorkStatus.FINISHED.value)

    def test_canceled_kiln_preserves_evidence_and_dispose(self):
        w = build_world()
        t = w.actor("teacher")
        coord = w.actor("coord")
        wid = make_ready_work(w)
        kiln = w.svc.create_kiln("KILN-2026-073", 10, t, note="常规批次")
        w.svc.add_to_kiln(kiln["batch_code"], wid, t)
        w.svc.start_firing(kiln["batch_code"], t)
        w.svc.cancel_kiln(kiln["batch_code"], coord, reason="窑炉故障停电")
        after = w.svc.get_kiln(kiln["batch_code"])
        self.assertEqual(after["status"], KilnStatus.CANCELED.value)
        self.assertEqual(len(after["members"]), 1)
        self.assertEqual(w.svc.get_work(wid, t)["status"], WorkStatus.QC_FAILED.value)
        w.svc.dispose_work(kiln["batch_code"], wid, "discard", coord)


class RoleBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.w = build_world()

    def test_teacher_cannot_configure_craft(self):
        t = self.w.actor("teacher")
        with self.assertRaises(Forbidden):
            self.w.svc.create_craft_version(
                "X", "七年级", "x", 1, "stoneware", ["a"], t)

    def test_guardian_cannot_create_student(self):
        g = self.w.actor("guardian")
        with self.assertRaises(Forbidden):
            self.w.svc.create_student("S-X", "x", "七年级", "班", g)

    def test_anonymous_rejected_by_service(self):
        with self.assertRaises(Exception):
            self.w.svc.authenticate("")
        with self.assertRaises(Exception):
            self.w.svc.authenticate("bad-token")


if __name__ == "__main__":
    unittest.main()
