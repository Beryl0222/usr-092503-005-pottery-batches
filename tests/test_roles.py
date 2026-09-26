"""角色权限与认证规则。"""

import unittest

from src.pottery import Forbidden, Unauthorized

from helpers import (
    ADMIN,
    GUARDIAN_S1,
    INHERITOR,
    REGISTRAR,
    STUDENT_S1,
    TEACHER_C1,
    make_service,
    make_version,
    make_work,
)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.svc, _, _ = make_service()

    def test_requires_request_identity(self):
        with self.assertRaises(Unauthorized):
            self.svc.list_batches(None)
        with self.assertRaises(Unauthorized):
            self.svc.list_batches("nobody")

    def test_teacher_cannot_create_craft_version(self):
        with self.assertRaises(Forbidden):
            self.svc.create_craft_version(TEACHER_C1, {"name": "x", "grade": "G3"})

    def test_admin_cannot_register_work(self):
        version_id = make_version(self.svc)
        with self.assertRaises(Forbidden):
            make_work(self.svc, version_id, teacher=ADMIN)

    def test_registrar_cannot_sign_steps(self):
        version_id = make_version(self.svc)
        work_id = make_work(self.svc, version_id)
        with self.assertRaises(Forbidden):
            self.svc.sign_step(REGISTRAR, work_id, {"step": "wedging"})

    def test_guardian_cannot_view_internal_work_detail(self):
        version_id = make_version(self.svc)
        work_id = make_work(self.svc, version_id)
        with self.assertRaises(Forbidden):
            self.svc.work_history(GUARDIAN_S1, work_id)

    def test_inheritor_has_read_only_access(self):
        version_id = make_version(self.svc)
        work_id = make_work(self.svc, version_id)
        self.assertEqual(self.svc.list_batches(INHERITOR), [])
        self.assertEqual(self.svc.work_history(INHERITOR, work_id)["work"]["work_id"],
                         work_id)
        with self.assertRaises(Forbidden):
            self.svc.create_batch(INHERITOR, {"code": "KILN-2026-001",
                                              "capacity": 2})

    def test_teacher_cannot_transfer_student(self):
        with self.assertRaises(Forbidden):
            self.svc.transfer_student(TEACHER_C1, STUDENT_S1, {"to_class": "C9"})

    def test_registrar_transfer_updates_class(self):
        result = self.svc.transfer_student(
            REGISTRAR, STUDENT_S1, {"to_class": "C9"})
        self.assertEqual(result["class_id"], "C9")
        self.assertEqual(
            self.svc._get_student(STUDENT_S1)["class_id"], "C9")


if __name__ == "__main__":
    unittest.main()
