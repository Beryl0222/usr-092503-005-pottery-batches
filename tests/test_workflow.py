"""作品登记、工序顺序、复核与排批前置条件。"""

import unittest

from src.pottery import Conflict, Validation

from helpers import (
    STUDENT_S1,
    STUDENT_S3,
    TEACHER_C1,
    TEACHER_C1_B,
    make_service,
    make_version,
    make_work,
    sign_all_steps,
)


class WorkRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.store, self.clock = make_service()
        self.version_id = make_version(self.svc)

    def test_missing_material_batch(self):
        with self.assertRaises(Validation):
            make_work(self.svc, self.version_id, material_batch_no="")

    def test_missing_storage_location(self):
        with self.assertRaises(Validation):
            make_work(self.svc, self.version_id, storage_location="")

    def test_safety_prerequisites_must_be_confirmed(self):
        with self.assertRaises(Validation):
            make_work(self.svc, self.version_id,
                      safety_confirmed=["apron"])

    def test_grade_mismatch_rejects_version(self):
        with self.assertRaises(Validation):
            make_work(self.svc, self.version_id, student=STUDENT_S3)

    def test_clay_must_be_allowed(self):
        with self.assertRaises(Validation):
            make_work(self.svc, self.version_id, clay_code="CLAY-Z")

    def test_incompatible_glazes_rejected(self):
        with self.assertRaises(Validation):
            make_work(self.svc, self.version_id,
                      glaze_codes=["GL-1", "GL-2"])


class StepOrderTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.store, self.clock = make_service()
        self.version_id = make_version(self.svc)
        self.work_id = make_work(self.svc, self.version_id)

    def test_steps_cannot_skip(self):
        with self.assertRaises(Validation):
            self.svc.sign_step(TEACHER_C1, self.work_id, {"step": "throwing"})
        with self.assertRaises(Validation):
            self.svc.sign_step(TEACHER_C1, self.work_id, {"step": "glazing"})

    def test_firing_step_cannot_be_signed_manually(self):
        with self.assertRaises(Validation):
            self.svc.sign_step(TEACHER_C1, self.work_id, {"step": "firing"})

    def test_steps_in_order(self):
        sign_all_steps(self.svc, self.work_id)
        steps = [r["step"] for r in self.store.all(
            "SELECT step FROM step_records WHERE work_id = ?"
            " ORDER BY id", (self.work_id,))]
        self.assertEqual(steps, ["wedging", "throwing", "trimming", "glazing"])

    def test_duplicate_step_signature(self):
        self.svc.sign_step(TEACHER_C1, self.work_id, {"step": "wedging"})
        with self.assertRaises(Conflict):
            self.svc.sign_step(TEACHER_C1, self.work_id, {"step": "wedging"})

    def test_cannot_review_until_steps_complete(self):
        with self.assertRaises(Validation):
            self.svc.review_work(TEACHER_C1_B, self.work_id,
                                 {"approved": True})

    def test_reviewer_must_differ_from_glazing_signer(self):
        sign_all_steps(self.svc, self.work_id)
        with self.assertRaises(Exception) as ctx:
            self.svc.review_work(TEACHER_C1, self.work_id, {"approved": True})
        self.assertEqual(ctx.exception.status, 403)

    def test_status_becomes_awaiting_only_after_independent_review(self):
        sign_all_steps(self.svc, self.work_id)
        self.assertEqual(self.svc.get_work(TEACHER_C1, self.work_id)["status"],
                         "in_progress")
        self.svc.review_work(TEACHER_C1_B, self.work_id, {"approved": True})
        self.assertEqual(self.svc.get_work(TEACHER_C1, self.work_id)["status"],
                         "awaiting_firing")

    def test_rejected_review_keeps_in_progress(self):
        sign_all_steps(self.svc, self.work_id)
        self.svc.review_work(TEACHER_C1_B, self.work_id,
                             {"approved": False, "note": "釉面不均"})
        self.assertEqual(self.svc.get_work(TEACHER_C1, self.work_id)["status"],
                         "in_progress")


class DryingGateTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.store, self.clock = make_service()
        self.version_id = make_version(self.svc)
        self.work_id = make_work(self.svc, self.version_id)
        sign_all_steps(self.svc, self.work_id)
        self.svc.review_work(TEACHER_C1_B, self.work_id, {"approved": True})

    def _batch(self, capacity: int = 5):
        return self.svc.create_batch(
            "admin1", {"code": "KILN-2026-010", "capacity": capacity})

    def test_cannot_admit_before_drying_hours(self):
        batch = self._batch()
        with self.assertRaises(Validation):
            self.svc.admit_to_batch(TEACHER_C1, batch["batch_id"],
                                    {"work_id": self.work_id})

    def test_admit_after_drying_hours(self):
        self.clock.advance(hours=24)
        batch = self._batch()
        result = self.svc.admit_to_batch(TEACHER_C1, batch["batch_id"],
                                         {"work_id": self.work_id})
        self.assertEqual(result["item_count"], 1)
        self.assertEqual(self.svc.get_work(TEACHER_C1, self.work_id)["status"],
                         "scheduled")

    def test_work_cannot_enter_two_active_batches(self):
        self.clock.advance(hours=25)
        b1 = self._batch()
        b2 = self.svc.create_batch("admin1",
                                   {"code": "KILN-2026-011", "capacity": 5})
        self.svc.admit_to_batch(TEACHER_C1, b1["batch_id"],
                                {"work_id": self.work_id})
        with self.assertRaises(Validation):
            self.svc.admit_to_batch(TEACHER_C1, b2["batch_id"],
                                    {"work_id": self.work_id})


if __name__ == "__main__":
    unittest.main()
