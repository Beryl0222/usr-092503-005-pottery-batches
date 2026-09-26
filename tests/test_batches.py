"""烧制批次：容量、关闭、异常处置与证据保留。"""

import unittest

from src.pottery import Conflict, Validation

from helpers import (
    ADMIN,
    STUDENT_S1,
    STUDENT_S2,
    TEACHER_C1,
    TEACHER_C1_B,
    make_firable,
    make_service,
    make_version,
)


class BatchCase(unittest.TestCase):
    def setUp(self):
        self.svc, self.store, self.clock = make_service()
        self.version_id = make_version(self.svc)
        self._code_seq = 0

    def _batch(self, capacity: int) -> str:
        self._code_seq += 1
        return self.svc.create_batch(
            ADMIN, {"code": f"KILN-2026-{100 + self._code_seq}",
                    "capacity": capacity})["batch_id"]

    def _firable(self, student=STUDENT_S1, **kw) -> str:
        return make_firable(self.svc, self.clock, self.version_id,
                            student, **kw)


class CapacityTests(BatchCase):
    def test_capacity_enforced(self):
        batch_id = self._batch(capacity=1)
        w1 = self._firable()
        w2 = self._firable(student=STUDENT_S2)
        self.svc.admit_to_batch(TEACHER_C1, batch_id, {"work_id": w1})
        with self.assertRaises(Conflict):
            self.svc.admit_to_batch(TEACHER_C1, batch_id, {"work_id": w2})
        count = self.store.one(
            "SELECT COUNT(*) AS n FROM batch_items WHERE batch_id = ?",
            (batch_id,))["n"]
        self.assertEqual(count, 1)

    def test_invalid_batch_code_and_capacity(self):
        with self.assertRaises(Validation):
            self.svc.create_batch(ADMIN, {"code": "窑-1", "capacity": 2})
        with self.assertRaises(Validation):
            self.svc.create_batch(ADMIN, {"code": "KILN-2026-001",
                                          "capacity": 0})
        with self.assertRaises(Conflict):
            self.svc.create_batch(ADMIN, {"code": "KILN-2026-001",
                                          "capacity": 2})
            self.svc.create_batch(ADMIN, {"code": "KILN-2026-001",
                                          "capacity": 2})


class FinishTests(BatchCase):
    def test_completed_requires_full_qc(self):
        batch_id = self._batch(capacity=2)
        w1 = self._firable()
        w2 = self._firable(student=STUDENT_S2)
        for w in (w1, w2):
            self.svc.admit_to_batch(TEACHER_C1, batch_id, {"work_id": w})
        with self.assertRaises(Validation):
            self.svc.finish_batch(ADMIN, batch_id,
                                  {"result": "completed", "qc": {w1: "pass"}})
        # 原子性：批次仍未关闭
        self.assertEqual(self.svc.batch_view(
            self.svc._get_batch(batch_id))["status"], "scheduled")

    def test_completed_with_qc_fail_needs_disposition(self):
        batch_id = self._batch(capacity=2)
        w1 = self._firable()
        w2 = self._firable(student=STUDENT_S2)
        for w in (w1, w2):
            self.svc.admit_to_batch(TEACHER_C1, batch_id, {"work_id": w})
        with self.assertRaises(Validation):
            self.svc.finish_batch(ADMIN, batch_id, {
                "result": "completed", "qc": {w1: "pass", w2: "fail"}})
        self.svc.finish_batch(ADMIN, batch_id, {
            "result": "completed",
            "qc": {w1: "pass", w2: "fail"},
            "dispositions": [{"work_id": w2, "decision": "discard",
                              "reason": "窑裂无法修复"}]})
        self.assertEqual(self.svc.get_work(ADMIN, w1)["status"], "fired")
        self.assertEqual(self.svc.get_work(ADMIN, w2)["status"], "discarded")

    def test_discarded_work_is_terminal(self):
        batch_id = self._batch(capacity=1)
        w1 = self._firable()
        self.svc.admit_to_batch(TEACHER_C1, batch_id, {"work_id": w1})
        self.svc.finish_batch(ADMIN, batch_id, {
            "result": "completed", "qc": {w1: "fail"},
            "dispositions": [{"work_id": w1, "decision": "discard",
                              "reason": "炸裂"}]})
        batch2 = self._batch(capacity=1)
        with self.assertRaises(Validation):
            self.svc.admit_to_batch(TEACHER_C1, batch2, {"work_id": w1})
        with self.assertRaises(Conflict):
            self.svc.move_storage(TEACHER_C1, w1, {"to_location": "仓库"})

    def test_cancelled_batch_keeps_evidence_and_reschedules(self):
        batch_id = self._batch(capacity=2)
        w1 = self._firable()
        self.svc.admit_to_batch(TEACHER_C1, batch_id, {"work_id": w1})
        self.svc.finish_batch(ADMIN, batch_id, {
            "result": "cancelled", "reason": "窑炉检修",
            "dispositions": [{"work_id": w1, "decision": "reschedule",
                              "reason": "作品完好，等待下一窑"}]})
        batch = self.svc._get_batch(batch_id)
        self.assertEqual(batch["status"], "cancelled")
        self.assertEqual(batch["close_reason"], "窑炉检修")
        # 原批次证据保留在作品沿革中
        history = self.svc.work_history(ADMIN, w1)
        self.assertEqual(history["batches"][0]["batch_status"], "cancelled")
        self.assertEqual(history["batches"][0]["outcome"], "cancelled")
        self.assertEqual(history["dispositions"][0]["decision"], "reschedule")
        # 可重新排批
        self.assertEqual(self.svc.get_work(ADMIN, w1)["status"],
                         "awaiting_firing")
        batch2 = self._batch(capacity=1)
        self.svc.admit_to_batch(TEACHER_C1, batch2, {"work_id": w1})
        self.assertEqual(self.svc.get_work(ADMIN, w1)["status"], "scheduled")

    def test_rework_requires_fresh_review(self):
        batch_id = self._batch(capacity=1)
        w1 = self._firable()
        self.svc.admit_to_batch(TEACHER_C1, batch_id, {"work_id": w1})
        self.svc.finish_batch(ADMIN, batch_id, {
            "result": "failed", "reason": "窑温不足",
            "dispositions": [{"work_id": w1, "decision": "rework",
                              "reason": "釉色未熟，需补釉复烧"}]})
        self.assertEqual(self.svc.get_work(ADMIN, w1)["status"], "rework")
        batch2 = self._batch(capacity=1)
        # 返工后未复核，不能排批
        with self.assertRaises(Validation):
            self.svc.admit_to_batch(TEACHER_C1, batch2, {"work_id": w1})
        self.svc.review_work(TEACHER_C1_B, w1, {"approved": True,
                                                "note": "补釉完成"})
        self.assertEqual(self.svc.get_work(ADMIN, w1)["status"],
                         "awaiting_firing")
        self.svc.admit_to_batch(TEACHER_C1, batch2, {"work_id": w1})

    def test_closed_batch_rejects_changes(self):
        batch_id = self._batch(capacity=1)
        w1 = self._firable()
        self.svc.admit_to_batch(TEACHER_C1, batch_id, {"work_id": w1})
        self.svc.finish_batch(ADMIN, batch_id, {
            "result": "completed", "qc": {w1: "pass"}})
        with self.assertRaises(Conflict):
            self.svc.admit_to_batch(TEACHER_C1, batch_id, {"work_id": w1})
        with self.assertRaises(Conflict):
            self.svc.finish_batch(ADMIN, batch_id, {"result": "cancelled"})

    def test_disposition_must_reference_batch_member(self):
        batch_id = self._batch(capacity=2)
        w1 = self._firable()
        outsider = self._firable(student=STUDENT_S2)
        self.svc.admit_to_batch(TEACHER_C1, batch_id, {"work_id": w1})
        with self.assertRaises(Validation):
            self.svc.finish_batch(ADMIN, batch_id, {
                "result": "cancelled",
                "dispositions": [
                    {"work_id": w1, "decision": "reschedule", "reason": "x"},
                    {"work_id": outsider, "decision": "discard",
                     "reason": "不在本批次"}]})


if __name__ == "__main__":
    unittest.main()
