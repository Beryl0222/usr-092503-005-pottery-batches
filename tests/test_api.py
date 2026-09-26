"""HTTP 接口：认证、角色限制、公开匿名视图与 CSV 导出。"""

import http.client
import json
import threading
import unittest

from src.pottery.api import make_server

from helpers import (
    ADMIN,
    GUARDIAN_S1,
    INHERITOR,
    TEACHER_C1,
    TEACHER_C1_B,
    VERSION_PAYLOAD,
    make_service,
    make_version,
)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc, cls.store, cls.clock = make_service()
        cls.server = make_server(cls.svc, port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def req(self, method, path, body=None, user=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if user:
            headers["X-User-Id"] = user
        conn.request(method, path,
                     body=json.dumps(body) if body is not None else None,
                     headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        ctype = resp.getheader("Content-Type") or ""
        if "json" in ctype:
            return resp.status, json.loads(raw.decode("utf-8")), ctype
        return resp.status, raw.decode("utf-8"), ctype

    def test_unauthenticated_and_unknown_user(self):
        status, body, _ = self.req("GET", "/api/batches")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")
        status, _, _ = self.req("GET", "/api/batches", user="ghost")
        self.assertEqual(status, 401)

    def test_role_forbidden(self):
        status, body, _ = self.req(
            "POST", "/api/craft-versions", user=TEACHER_C1,
            body={"name": "x", "grade": "G3"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

    def test_unknown_route(self):
        status, _, _ = self.req("GET", "/api/nope", user=ADMIN)
        self.assertEqual(status, 404)

    def test_full_lifecycle_over_http(self):
        # 课程负责人配置工艺版本
        status, version, _ = self.req(
            "POST", "/api/craft-versions", user=ADMIN, body=VERSION_PAYLOAD)
        self.assertEqual(status, 200)
        version_id = version["version_id"]

        # 教师登记作品（含材料批号与保管位置）
        status, work, _ = self.req("POST", "/api/works", user=TEACHER_C1, body={
            "student_id": "s1", "version_id": version_id,
            "material_batch_no": "MB-HTTP-1", "clay_code": "CLAY-A",
            "glaze_codes": ["GL-1"], "storage_location": "A架",
            "safety_confirmed": ["apron", "briefing"]})
        self.assertEqual(status, 200)
        work_id = work["work_id"]

        # 工序不能越级
        status, body, _ = self.req("POST", f"/api/works/{work_id}/steps",
                                   user=TEACHER_C1, body={"step": "glazing"})
        self.assertEqual(status, 422)
        self.assertIn("越级", body["error"]["message"])

        for step in ("wedging", "throwing", "trimming", "glazing"):
            status, _, _ = self.req("POST", f"/api/works/{work_id}/steps",
                                    user=TEACHER_C1, body={"step": step})
            self.assertEqual(status, 200)

        # 施釉签认教师不能复核自己的作品
        status, _, _ = self.req("POST", f"/api/works/{work_id}/review",
                                user=TEACHER_C1, body={"approved": True})
        self.assertEqual(status, 403)
        status, _, _ = self.req("POST", f"/api/works/{work_id}/review",
                                user=TEACHER_C1_B, body={"approved": True})
        self.assertEqual(status, 200)

        # 排批：干燥时间不足被拒，推进时钟后成功
        status, batch, _ = self.req("POST", "/api/batches", user=ADMIN,
                                    body={"code": "KILN-2026-501",
                                          "capacity": 2})
        batch_id = batch["batch_id"]
        status, body, _ = self.req("POST", f"/api/batches/{batch_id}/items",
                                   user=TEACHER_C1, body={"work_id": work_id})
        self.assertEqual(status, 422)
        self.assertIn("干燥", body["error"]["message"])
        self.clock.advance(hours=25)
        status, _, _ = self.req("POST", f"/api/batches/{batch_id}/items",
                                user=TEACHER_C1, body={"work_id": work_id})
        self.assertEqual(status, 200)

        # 完成批次并质检合格
        status, _, _ = self.req("POST", f"/api/batches/{batch_id}/finish",
                                user=ADMIN,
                                body={"result": "completed",
                                      "qc": {work_id: "pass"}})
        self.assertEqual(status, 200)

        # 未授权时公开视图不可见
        status, exhibits, _ = self.req("GET", "/api/public/exhibits")
        self.assertEqual(status, 200)
        self.assertEqual(exhibits, [])

        # 监护人授权后立即可见，撤回后立即消失
        self.req("POST", f"/api/works/{work_id}/consent", user=GUARDIAN_S1,
                 body={"granted": True})
        status, exhibits, _ = self.req("GET", "/api/public/exhibits")
        self.assertEqual(len(exhibits), 1)
        card = exhibits[0]
        self.assertNotIn("student_id", card)
        self.assertNotIn("学生甲", json.dumps(card, ensure_ascii=False))
        self.assertEqual(card["material_batch_no"], "MB-HTTP-1")
        self.assertEqual(card["craft_version"]["version_id"], version_id)
        self.assertTrue(any(h["name"] == "王老师" for h in card["handlers"]))
        self.req("POST", f"/api/works/{work_id}/consent", user=GUARDIAN_S1,
                 body={"granted": False})
        status, exhibits, _ = self.req("GET", "/api/public/exhibits")
        self.assertEqual(exhibits, [])

        # 作品沿革与窑次清单导出
        status, history, _ = self.req("GET", f"/api/works/{work_id}/history",
                                      user=INHERITOR)
        self.assertEqual(status, 200)
        self.assertEqual(len(history["steps"]), 4)
        self.assertEqual(history["batches"][0]["outcome"], "pass")
        status, csv_text, ctype = self.req(
            "GET", f"/api/works/{work_id}/history?format=csv", user=ADMIN)
        self.assertEqual(status, 200)
        self.assertIn("text/csv", ctype)
        self.assertIn("MB-HTTP-1", csv_text)
        status, manifest, _ = self.req(
            "GET", f"/api/batches/{batch_id}/manifest", user=INHERITOR)
        self.assertEqual(status, 200)
        self.assertEqual(manifest["code"], "KILN-2026-501")
        self.assertEqual(manifest["items"][0]["work_id"], work_id)
        status, csv_text, ctype = self.req(
            "GET", f"/api/batches/{batch_id}/manifest?format=csv", user=ADMIN)
        self.assertIn("KILN-2026-501", csv_text)

        # 监护人无权查看内部沿革
        status, _, _ = self.req("GET", f"/api/works/{work_id}/history",
                                user=GUARDIAN_S1)
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
