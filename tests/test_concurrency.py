"""并发与持久化验证：容量不超卖、设备去重、重开库后数据仍在。"""

import os
import tempfile
import threading
import unittest

from src.pottery import Conflict, PotteryService, Store, ManualClock

from helpers import (
    ADMIN,
    STUDENT_S1,
    STUDENT_S2,
    TEACHER_C1,
    TEACHER_C1_B,
    VERSION_PAYLOAD,
    make_firable,
    make_service,
    make_version,
)


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.store, self.clock = make_service()
        self.version_id = make_version(self.svc)

    def test_concurrent_admission_never_exceeds_capacity(self):
        capacity = 3
        workers = 8
        works = []
        for i in range(workers):
            student = STUDENT_S1 if i % 2 == 0 else STUDENT_S2
            works.append(make_firable(self.svc, self.clock, self.version_id,
                                      student))
        batch_id = self.svc.create_batch(
            ADMIN, {"code": "KILN-2026-200", "capacity": capacity})["batch_id"]

        admitted: list[str] = []
        conflicts: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(workers)

        def attempt(work_id):
            barrier.wait(timeout=10)
            try:
                self.svc.admit_to_batch(TEACHER_C1, batch_id,
                                        {"work_id": work_id})
                with lock:
                    admitted.append(work_id)
            except Conflict:
                with lock:
                    conflicts.append(work_id)

        threads = [threading.Thread(target=attempt, args=(w,)) for w in works]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(admitted), capacity)
        self.assertEqual(len(conflicts), workers - capacity)
        count = self.store.one(
            "SELECT COUNT(*) AS n FROM batch_items WHERE batch_id = ?",
            (batch_id,))["n"]
        self.assertEqual(count, capacity)

    def test_concurrent_same_device_upload_creates_one_work(self):
        payload = {
            "student_id": STUDENT_S1, "version_id": self.version_id,
            "material_batch_no": "MB-1", "clay_code": "CLAY-A",
            "glaze_codes": [], "storage_location": "A架",
            "safety_confirmed": ["apron", "briefing"],
            "device_id": "pad-9", "client_record_id": "rec-9",
        }
        results: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(4)

        def upload():
            barrier.wait(timeout=10)
            res = self.svc.register_work(TEACHER_C1, dict(payload))
            with lock:
                results.append(res["work_id"])

        threads = [threading.Thread(target=upload) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(set(results)), 1)
        count = self.store.one("SELECT COUNT(*) AS n FROM works")["n"]
        self.assertEqual(count, 1)


class PersistenceTests(unittest.TestCase):
    def test_data_survives_reopen(self):
        path = os.path.join(tempfile.mkdtemp(prefix="pottery-persist-"),
                            "data.db")
        svc, store, clock = make_service(path)
        version_id = make_version(svc)
        work_id = make_firable(svc, clock, version_id)
        batch_id = svc.create_batch(
            ADMIN, {"code": "KILN-2026-300", "capacity": 2})["batch_id"]
        svc.admit_to_batch(TEACHER_C1, batch_id, {"work_id": work_id})
        store.close()

        # 重新打开同一数据库文件，所有记录仍可读取
        store2 = Store(path)
        svc2 = PotteryService(store2, ManualClock())
        history = svc2.work_history(ADMIN, work_id)
        self.assertEqual(history["work"]["material_batch_no"], "MB-2026-001")
        self.assertEqual(len(history["steps"]), 4)
        self.assertEqual(history["batches"][0]["code"], "KILN-2026-300")
        self.assertEqual(svc2.get_work(ADMIN, work_id)["status"], "scheduled")
        events = store2.one("SELECT COUNT(*) AS n FROM events")["n"]
        self.assertGreater(events, 0)
        store2.close()


if __name__ == "__main__":
    unittest.main()
