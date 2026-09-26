"""展示授权、公开视图匿名化与教师代签、设备去重、转班规则。"""

import unittest

from src.pottery import Forbidden, NotFound, Validation

from helpers import (
    ADMIN,
    GUARDIAN_S1,
    GUARDIAN_S2,
    REGISTRAR,
    STUDENT_S1,
    TEACHER_C1,
    TEACHER_C1_B,
    TEACHER_C2,
    make_firable,
    make_service,
    make_version,
    make_work,
)


def _fire_and_pass(svc, clock, work_id, seq=1):
    batch_id = svc.create_batch(
        ADMIN, {"code": f"KILN-2026-{seq:03d}", "capacity": 5})["batch_id"]
    svc.admit_to_batch(TEACHER_C1, batch_id, {"work_id": work_id})
    svc.finish_batch(ADMIN, batch_id,
                     {"result": "completed", "qc": {work_id: "pass"}})


class ConsentPublicTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.store, self.clock = make_service()
        self.version_id = make_version(self.svc)
        self.work_id = make_firable(self.svc, self.clock, self.version_id)
        _fire_and_pass(self.svc, self.clock, self.work_id)

    def test_public_view_requires_consent(self):
        self.assertEqual(self.svc.public_exhibits(), [])
        with self.assertRaises(NotFound):
            self.svc.public_work(self.work_id)
        self.svc.set_consent(GUARDIAN_S1, self.work_id, {"granted": True})
        self.assertEqual(len(self.svc.public_exhibits()), 1)

    def test_withdrawal_takes_effect_immediately(self):
        self.svc.set_consent(GUARDIAN_S1, self.work_id, {"granted": True})
        self.assertEqual(len(self.svc.public_exhibits()), 1)
        self.svc.set_consent(GUARDIAN_S1, self.work_id, {"granted": False})
        self.assertEqual(self.svc.public_exhibits(), [])
        with self.assertRaises(NotFound):
            self.svc.public_work(self.work_id)

    def test_guardian_cannot_consent_for_others_child(self):
        with self.assertRaises(Forbidden):
            self.svc.set_consent(GUARDIAN_S2, self.work_id, {"granted": True})

    def test_public_card_hides_minor_identity(self):
        self.svc.set_consent(GUARDIAN_S1, self.work_id, {"granted": True})
        card = self.svc.public_work(self.work_id)
        for key in ("student_id", "student_name", "class_id", "guardian"):
            self.assertNotIn(key, card)
        self.assertNotIn("学生甲", str(card))
        # 展品可说明材料来源、经手人与有效工艺版本
        self.assertEqual(card["material_batch_no"], "MB-2026-001")
        self.assertEqual(card["craft_version"]["version_id"], self.version_id)
        signer_names = {h["name"] for h in card["handlers"]}
        self.assertIn("王老师", signer_names)
        self.assertIn("李老师", signer_names)


class ProxySignTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.store, self.clock = make_service()
        self.version_id = make_version(self.svc)
        self.work_id = make_work(self.svc, self.version_id)

    def test_other_class_teacher_needs_proxy_fields(self):
        with self.assertRaises(Forbidden):
            self.svc.sign_step(TEACHER_C2, self.work_id, {"step": "wedging"})

    def test_proxy_sign_records_delegator(self):
        rec = self.svc.sign_step(TEACHER_C2, self.work_id, {
            "step": "wedging", "proxy_for": TEACHER_C1,
            "proxy_reason": "王老师外出培训"})
        self.assertEqual(rec["signed_by"], TEACHER_C2)
        self.assertEqual(rec["proxy_for"], TEACHER_C1)
        self.assertEqual(rec["proxy_reason"], "王老师外出培训")

    def test_proxy_target_must_be_class_teacher(self):
        with self.assertRaises(Validation):
            self.svc.sign_step(TEACHER_C2, self.work_id, {
                "step": "wedging", "proxy_for": TEACHER_C2,
                "proxy_reason": "自代签无效"})

    def test_own_class_teacher_cannot_mark_proxy(self):
        with self.assertRaises(Validation):
            self.svc.sign_step(TEACHER_C1, self.work_id, {
                "step": "wedging", "proxy_for": TEACHER_C1_B,
                "proxy_reason": "本班无需代签"})


class DeviceDedupTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.store, self.clock = make_service()
        self.version_id = make_version(self.svc)

    def test_same_device_same_record_deduplicates(self):
        payload = {
            "student_id": STUDENT_S1, "version_id": self.version_id,
            "material_batch_no": "MB-1", "clay_code": "CLAY-A",
            "glaze_codes": [], "storage_location": "A架",
            "safety_confirmed": ["apron", "briefing"],
            "device_id": "pad-7", "client_record_id": "rec-1",
        }
        first = self.svc.register_work(TEACHER_C1, payload)
        again = self.svc.register_work(TEACHER_C1, payload)
        self.assertFalse(first["deduplicated"])
        self.assertTrue(again["deduplicated"])
        self.assertEqual(first["work_id"], again["work_id"])
        count = self.store.one("SELECT COUNT(*) AS n FROM works")["n"]
        self.assertEqual(count, 1)

    def test_step_upload_deduplicates(self):
        work_id = make_work(self.svc, self.version_id)
        args = {"step": "wedging", "device_id": "pad-7",
                "client_record_id": "step-1"}
        first = self.svc.sign_step(TEACHER_C1, work_id, dict(args))
        again = self.svc.sign_step(TEACHER_C1, work_id, dict(args))
        self.assertFalse(first["deduplicated"])
        self.assertTrue(again["deduplicated"])
        count = self.store.one(
            "SELECT COUNT(*) AS n FROM step_records WHERE work_id = ?",
            (work_id,))["n"]
        self.assertEqual(count, 1)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.store, self.clock = make_service()
        self.version_id = make_version(self.svc)

    def test_sign_permission_follows_current_class(self):
        self.svc.transfer_student(REGISTRAR, STUDENT_S1, {"to_class": "C2"})
        work_id = make_work(self.svc, self.version_id)
        # 原班级教师不再能直接签认
        with self.assertRaises(Forbidden):
            self.svc.sign_step(TEACHER_C1, work_id, {"step": "wedging"})
        # 新班级教师可以直接签认
        rec = self.svc.sign_step(TEACHER_C2, work_id, {"step": "wedging"})
        self.assertIsNone(rec["proxy_for"])

    def test_transfer_history_in_work_lineage(self):
        self.svc.transfer_student(REGISTRAR, STUDENT_S1, {"to_class": "C2"})
        work_id = make_work(self.svc, self.version_id)
        history = self.svc.work_history(ADMIN, work_id)
        self.assertEqual(history["student"]["current_class_id"], "C2")
        self.assertEqual(history["student_transfers"][0]["from"], "C1")
        self.assertEqual(history["student_transfers"][0]["to"], "C2")


if __name__ == "__main__":
    unittest.main()
