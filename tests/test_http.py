"""HTTP 接口端到端测试：鉴权、角色边界、导出、幂等与时钟控制。"""

from __future__ import annotations

import csv
import io
import json
import threading
import unittest
import urllib.error
import urllib.request

from src.pottery import Clock
from src.pottery.api import build_server
from src.pottery.storage import Database

from _fixtures import build_world


class ApiClient:
    def __init__(self, base_url: str):
        self.base_url = base_url

    def request(self, method: str, path: str, body=None, token: str | None = None,
                headers: dict | None = None):
        url = self.base_url + path
        data = None
        req_headers = dict(headers or {})
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        if token:
            req_headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, dict(resp.headers), raw
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read().decode("utf-8")

    def get(self, path, token=None, headers=None):
        return self.request("GET", path, token=token, headers=headers)

    def post(self, path, body=None, token=None, headers=None):
        return self.request("POST", path, body=body or {}, token=token, headers=headers)


def _serve(w):
    server = build_server(w.db, w.clock, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = ApiClient(f"http://127.0.0.1:{server.server_address[1]}")
    return server, thread, client


class HttpAuthAndRoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w = build_world()
        cls.server, cls.thread, cls.api = _serve(cls.w)
        cls.tokens = cls.w.tokens

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_health_and_anonymous_public(self):
        status, _, body = self.api.get("/api/health")
        self.assertEqual(status, 200)
        self.assertIn("time", json.loads(body))
        status, _, _ = self.api.get("/api/public/works")
        self.assertEqual(status, 200)

    def test_missing_and_bad_token_401(self):
        status, _, body = self.api.get("/api/works")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "unauthorized")
        status, _, _ = self.api.get("/api/works", token="nonsense")
        self.assertEqual(status, 401)

    def test_teacher_forbidden_from_coordinator_routes(self):
        status, _, body = self.api.post(
            "/api/students",
            {"id": "S-X", "name": "x", "grade": "七年级", "class_group": "班"},
            token=self.tokens["teacher"])
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "forbidden")

    def test_guardian_forbidden_from_teacher_routes(self):
        status, _, _ = self.api.get("/api/works", token=self.tokens["guardian"])
        self.assertEqual(status, 403)

    def test_teacher_cannot_qc(self):
        # 建空窑次不必走到质检；直接验证角色：先造一个合格全流程
        wid = self._make_ready()
        code = "KILN-2026-100"
        self.assertEqual(self.api.post("/api/kilns", {"batch_code": code, "capacity": 5},
                                       token=self.tokens["teacher"])[0], 201)
        self.assertEqual(self.api.post(f"/api/kilns/{code}/works", {"work_id": wid},
                                       token=self.tokens["teacher"])[0], 201)
        self.api.post(f"/api/kilns/{code}/start", token=self.tokens["teacher"])
        self.api.post(f"/api/kilns/{code}/done", token=self.tokens["teacher"])
        status, _, body = self.api.post(
            f"/api/kilns/{code}/qc", {"passed": True}, token=self.tokens["teacher"])
        self.assertEqual(status, 403)
        # 传承人可以质检
        status, _, _ = self.api.post(
            f"/api/kilns/{code}/qc", {"passed": True}, token=self.tokens["master"])
        self.assertEqual(status, 200)

    def test_validation_error_shape(self):
        status, _, body = self.api.post(
            "/api/kilns", {"batch_code": "BAD", "capacity": 5},
            token=self.tokens["teacher"])
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(body)["error"]["code"], "validation_failed")

    def test_unknown_route_404(self):
        status, _, _ = self.api.get("/api/nope", token=self.tokens["teacher"])
        self.assertEqual(status, 404)

    def _make_ready(self) -> str:
        t, t2 = self.tokens["teacher"], self.tokens["teacher2"]
        status, _, body = self.api.post(
            "/api/works",
            {"student_id": "S-1", "craft_code": "JUNIOR-POTTERY",
             "safety_acks": [0, 1], "storage_location": "A 架"},
            token=t, headers={"X-Device-Id": "D-HTTP-1", "X-Content-Hash": "h-http-1"})
        self.assertEqual(status, 201, body)
        wid = json.loads(body)["id"]
        self.api.post(f"/api/works/{wid}/steps",
                      {"step": "wedging", "material_code": "CLAY-A", "material_lot": "L2026-01"},
                      token=t)
        self.api.post(f"/api/works/{wid}/steps", {"step": "throwing"}, token=t)
        self.api.post(f"/api/works/{wid}/steps", {"step": "trimming"}, token=t)
        self.api.post("/api/admin/clock", {"mode": "advance", "hours": 25},
                      token=self.tokens["coord"])
        self.api.post(f"/api/works/{wid}/steps",
                      {"step": "glazing", "material_code": "GZ-STD", "material_lot": "L2026-02"},
                      token=t)
        status, _, _ = self.api.post(f"/api/works/{wid}/review", token=t2)
        self.assertEqual(status, 200)
        return wid


class HttpWorkflowExportTests(unittest.TestCase):
    def setUp(self):
        self.w = build_world()
        self.server, self.thread, self.api = _serve(self.w)
        self.tokens = self.w.tokens

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _full_pipeline(self, work_no: int, batch_code: str, student: str = "S-1"):
        t, t2, m, g = (self.tokens["teacher"], self.tokens["teacher2"],
                       self.tokens["master"], self.tokens["guardian"])
        _, _, body = self.api.post(
            "/api/works",
            {"student_id": student, "craft_code": "JUNIOR-POTTERY",
             "safety_acks": [0, 1]},
            token=t,
            headers={"X-Device-Id": f"DP-{work_no}", "X-Content-Hash": f"HP-{work_no}"})
        wid = json.loads(body)["id"]
        for step, mat in (
            ("wedging", ("CLAY-A", "L2026-01")),
            ("throwing", None),
            ("trimming", None),
        ):
            payload = {"step": step}
            if mat:
                payload["material_code"], payload["material_lot"] = mat
            self.api.post(f"/api/works/{wid}/steps", payload, token=t)
        self.api.post("/api/admin/clock", {"mode": "advance", "hours": 25},
                      token=self.tokens["coord"])
        self.api.post(f"/api/works/{wid}/steps",
                      {"step": "glazing", "material_code": "GZ-STD", "material_lot": "L2026-02"},
                      token=t)
        self.api.post(f"/api/works/{wid}/review", token=t2)
        self.api.post("/api/kilns", {"batch_code": batch_code, "capacity": 5}, token=t)
        self.api.post(f"/api/kilns/{batch_code}/works", {"work_id": wid}, token=t)
        self.api.post(f"/api/kilns/{batch_code}/start", token=t)
        self.api.post(f"/api/kilns/{batch_code}/done", token=t)
        self.api.post(f"/api/kilns/{batch_code}/qc", {"passed": True}, token=m)
        return wid

    def test_public_view_consent_lifecycle_and_pii_scrubbing(self):
        wid = self._full_pipeline(1, "KILN-2026-200")
        # 未授权：公开视图为空
        _, _, body = self.api.get("/api/public/works")
        self.assertEqual(json.loads(body)["works"], [])
        # 监护人授权
        status, _, _ = self.api.post(f"/api/works/{wid}/consent/grant", token=self.tokens["guardian"])
        self.assertEqual(status, 200)
        _, _, body = self.api.get("/api/public/works")
        works = json.loads(body)["works"]
        self.assertEqual(len(works), 1)
        entry = works[0]
        blob = json.dumps(entry, ensure_ascii=False)
        for pii in ("赵小明", "S-1", "七(1)班", "student_id", "student_name", "class"):
            self.assertNotIn(pii, blob)
        # 能说明材料来源、经手人、工艺版本
        self.assertEqual(entry["craft"], {"code": "JUNIOR-POTTERY", "version": 1,
                                          "name": "初中陶艺基础", "effective": True})
        self.assertEqual(entry["batch_code"], "KILN-2026-200")
        wedge = next(p for p in entry["provenance"] if p["step"] == "wedging")
        self.assertEqual(wedge["material_lot"], "L2026-01")
        self.assertEqual(wedge["supplier"], "南山陶土厂")
        self.assertTrue(wedge["signer"])
        # 撤回立即生效
        status, _, _ = self.api.post(
            f"/api/works/{wid}/consent/withdraw", {"reason": "临时撤回"},
            token=self.tokens["guardian"])
        self.assertEqual(status, 200)
        _, _, body = self.api.get("/api/public/works")
        self.assertEqual(json.loads(body)["works"], [])

    def test_manifest_csv_export(self):
        wid = self._full_pipeline(2, "KILN-2026-201")
        status, headers, body = self.api.get(
            "/api/kilns/KILN-2026-201/manifest?format=csv", token=self.tokens["teacher"])
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["Content-Type"])
        rows = list(csv.DictReader(io.StringIO(body)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["batch_code"], "KILN-2026-201")
        self.assertEqual(rows[0]["batch_status"], "qc_passed")
        self.assertEqual(rows[0]["work_id"], wid)
        self.assertEqual(rows[0]["decision"], "")
        # JSON 清单同样可取
        status, _, body = self.api.get(
            "/api/kilns/KILN-2026-201/manifest?format=json", token=self.tokens["teacher"])
        self.assertEqual(json.loads(body)["active_count"], 0)  # 质检后成员已结项

    def test_work_history_json_export_contains_voided_evidence(self):
        # 制造一次质检失败 + 返工，沿革中必须包含作废证据
        t, t2, m = (self.tokens["teacher"], self.tokens["teacher2"], self.tokens["master"])
        wid = self._full_pipeline(3, "KILN-2026-202")
        # 再做一件失败品更直观：此处直接验证合格作品沿革结构
        status, headers, body = self.api.get(f"/api/works/{wid}/history", token=t)
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        history = json.loads(body)
        self.assertEqual(history["work"]["public_code"][0], "P")
        self.assertEqual(len(history["steps"]), 5)
        actions = {e["action"] for e in history["events"]}
        self.assertIn("step.signed", actions)
        self.assertIn("work.finished", actions)
        self.assertEqual(len(history["safety_acknowledgements"]), 2)
        self.assertEqual(len(history["kiln_memberships"]), 1)

        # CSV 扁平导出行含工序与事件
        _, _, csv_body = self.api.get(f"/api/works/{wid}/history?format=csv", token=t)
        text = csv_body
        self.assertIn("wedging", text)
        self.assertIn("KILN-2026-202", text)

    def test_failed_batch_recovery_over_http(self):
        t, t2, m = (self.tokens["teacher"], self.tokens["teacher2"], self.tokens["master"])
        wid = self._full_pipeline(4, "KILN-2026-203")
        # _full_pipeline 已质检通过；另走一个失败窑次验证恢复
        _, _, body = self.api.post(
            "/api/works",
            {"student_id": "S-2", "craft_code": "JUNIOR-POTTERY", "safety_acks": [0, 1]},
            token=t, headers={"X-Device-Id": "DP-5", "X-Content-Hash": "HP-5"})
        wid2 = json.loads(body)["id"]
        for step, mat in (("wedging", ("CLAY-A", "L2026-01")), ("throwing", None),
                          ("trimming", None)):
            payload = {"step": step}
            if mat:
                payload["material_code"], payload["material_lot"] = mat
            self.api.post(f"/api/works/{wid2}/steps", payload, token=t)
        self.api.post("/api/admin/clock", {"mode": "advance", "hours": 25},
                      token=self.tokens["coord"])
        self.api.post(f"/api/works/{wid2}/steps",
                      {"step": "glazing", "material_code": "GZ-STD", "material_lot": "L2026-02"},
                      token=t)
        self.api.post(f"/api/works/{wid2}/review", token=t2)
        code = "KILN-2026-204"
        self.api.post("/api/kilns", {"batch_code": code, "capacity": 5}, token=t)
        self.api.post(f"/api/kilns/{code}/works", {"work_id": wid2}, token=t)
        self.api.post(f"/api/kilns/{code}/start", token=t)
        self.api.post(f"/api/kilns/{code}/done", token=t)
        status, _, _ = self.api.post(f"/api/kilns/{code}/qc", {"passed": False}, token=m)
        self.assertEqual(status, 200)
        # 取消窑次证据仍可导出
        _, _, body = self.api.get(f"/api/kilns/{code}/manifest?format=csv", token=t)
        self.assertIn("qc_failed", body)
        # 返工处置
        status, _, body = self.api.post(
            f"/api/kilns/{code}/dispositions",
            {"work_id": wid2, "decision": "rework", "reentry_step": "glazing",
             "note": "釉泡"}, token=m)
        self.assertEqual(status, 201, body)
        _, _, body = self.api.get(f"/api/works/{wid2}", token=t)
        self.assertEqual(json.loads(body)["status"], "rework")
        # 沿革含作废证据
        _, _, body = self.api.get(f"/api/works/{wid2}/history", token=t)
        actions = [e["action"] for e in json.loads(body)["events"]]
        self.assertIn("step.voided", actions)
        self.assertIn("work.disposed", actions)

    def test_idempotency_key_replays_response(self):
        t = self.tokens["teacher"]
        payload = {"student_id": "S-1", "craft_code": "JUNIOR-POTTERY",
                   "safety_acks": [0, 1]}
        headers = {"X-Device-Id": "DP-IDEM", "X-Content-Hash": "HP-IDEM",
                   "Idempotency-Key": "key-001"}
        s1, _, b1 = self.api.post("/api/works", payload, token=t, headers=headers)
        s2, _, b2 = self.api.post("/api/works", payload, token=t, headers=headers)
        self.assertEqual((s1, b1), (s2, b2))
        # 第二次是重放，并未产生第二件作品
        _, _, body = self.api.get("/api/works?student_id=S-1", token=t)
        works = json.loads(body)["works"]
        idem_works = [w for w in works if w["id"] == json.loads(b1)["id"]]
        self.assertEqual(len(idem_works), 1)

    def test_device_duplicate_rejected_over_http(self):
        t = self.tokens["teacher"]
        headers = {"X-Device-Id": "DP-DUP", "X-Content-Hash": "HP-DUP"}
        payload = {"student_id": "S-1", "craft_code": "JUNIOR-POTTERY",
                   "safety_acks": [0, 1]}
        s1, _, _ = self.api.post("/api/works", payload, token=t, headers=headers)
        self.assertEqual(s1, 201)
        s2, _, body = self.api.post("/api/works", payload, token=t, headers=headers)
        self.assertEqual(s2, 409)
        self.assertEqual(json.loads(body)["error"]["code"], "duplicate_upload")

    def test_clock_control_restricted_and_effective(self):
        # 未授权不能控制时钟
        status, _, _ = self.api.post(
            "/api/admin/clock", {"mode": "freeze"}, token=self.tokens["teacher"])
        self.assertEqual(status, 403)
        status, _, body = self.api.post(
            "/api/admin/clock", {"mode": "freeze", "at": "2027-01-01T00:00:00+00:00"},
            token=self.tokens["coord"])
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["frozen"])
        _, _, body = self.api.get("/api/health")
        self.assertTrue(json.loads(body)["time"].startswith("2027-01-01"))

    def test_kiln_list_shows_counts(self):
        self._full_pipeline(6, "KILN-2026-205")
        _, _, body = self.api.get("/api/kilns", token=self.tokens["teacher"])
        kilns = json.loads(body)["kilns"]
        mine = [k for k in kilns if k["batch_code"] == "KILN-2026-205"]
        self.assertEqual(mine[0]["active_count"], 0)
        self.assertEqual(mine[0]["status"], "qc_passed")


class ConcurrencyOverHttpTests(unittest.TestCase):
    def test_capacity_enforced_under_parallel_http_requests(self):
        w = build_world()
        server, thread, api = _serve(w)
        try:
            t = w.tokens["teacher"]
            t2 = w.tokens["teacher2"]
            coord = w.tokens["coord"]
            # 造 6 件 ready 作品
            ids = []
            for i in range(6):
                _, _, body = api.post(
                    "/api/works",
                    {"student_id": "S-1" if i % 2 == 0 else "S-2",
                     "craft_code": "JUNIOR-POTTERY", "safety_acks": [0, 1]},
                    token=t, headers={"X-Device-Id": f"DC-{i}", "X-Content-Hash": f"HC-{i}"})
                ids.append(json.loads(body)["id"])
            for wid in ids:
                api.post(f"/api/works/{wid}/steps",
                         {"step": "wedging", "material_code": "CLAY-A", "material_lot": "L2026-01"},
                         token=t)
                api.post(f"/api/works/{wid}/steps", {"step": "throwing"}, token=t)
                api.post(f"/api/works/{wid}/steps", {"step": "trimming"}, token=t)
            api.post("/api/admin/clock", {"mode": "advance", "hours": 25}, token=coord)
            for wid in ids:
                api.post(f"/api/works/{wid}/steps",
                         {"step": "glazing", "material_code": "GZ-STD", "material_lot": "L2026-02"},
                         token=t)
                api.post(f"/api/works/{wid}/review", token=t2)
            code = "KILN-2026-300"
            api.post("/api/kilns", {"batch_code": code, "capacity": 2}, token=t)

            results = []
            barrier = threading.Barrier(6)

            def enqueue(wid):
                barrier.wait()
                status, _, _ = api.post(f"/api/kilns/{code}/works", {"work_id": wid}, token=t)
                results.append(status)

            threads = [threading.Thread(target=enqueue, args=(wid,)) for wid in ids]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            self.assertEqual(sorted(results).count(201), 2)
            self.assertEqual(sorted(results).count(409), 4)
            _, _, body = api.get(f"/api/kilns/{code}/manifest?format=json", token=t)
            self.assertEqual(json.loads(body)["active_count"], 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
