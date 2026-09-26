"""角色受限的 HTTP 接口。

- Bearer 令牌鉴权，按角色限制端点；
- 写接口支持 Idempotency-Key 请求重放；
- 公开视图 /api/public/works 无需登录，只输出白名单字段；
- 提供窑次清单 CSV 与作品沿革 JSON/CSV 导出；
- 时钟可由课程负责人冻结/快进/恢复，便于验证时间相关规则。
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .clock import Clock
from .contracts import Role
from .errors import DomainError, Unauthorized
from .services import PotteryService
from .storage import Database, log_event

STAFF = {Role.TEACHER, Role.COORDINATOR, Role.MASTER}


def _csv_response(rows: list[dict], filename: str) -> tuple[int, str, dict]:
    buf = io.StringIO()
    if rows:
        columns = list(rows[0].keys())
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    else:
        buf.write("")
    headers = {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": f'attachment; filename="{filename}"',
    }
    return HTTPStatus.OK, buf.getvalue(), headers


class PotteryHandler(BaseHTTPRequestHandler):
    service: PotteryService
    server_version = "CampusPottery/1.0"

    # 关闭默认访问日志，避免污染测试输出。
    def log_message(self, fmt, *args):  # noqa: A003
        pass

    # ------------------------------------------------------------ 基础收发

    def _send(self, status: int, body, headers: dict | None = None) -> None:
        if isinstance(body, (dict, list)):
            payload = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        elif body is None:
            payload = b""
            ctype = "application/json"
        else:
            payload = body.encode("utf-8") if isinstance(body, str) else body
            ctype = (headers or {}).get("Content-Type", "text/plain; charset=utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        for k, v in (headers or {}).items():
            if k != "Content-Type":
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _send_error(self, exc: Exception) -> None:
        if isinstance(exc, DomainError):
            status = exc.http_status
            code = exc.code
        else:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            code = "internal_error"
        self._send(status, {"error": {"code": code, "message": str(exc)}})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError("请求体不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return data

    def _actor(self, roles: set[Role] | None = None) -> sqlite3.Row:
        header = self.headers.get("Authorization", "")
        token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
        actor = self.service.authenticate(token)
        if roles is not None and Role(actor["role"]) not in roles:
            from .errors import Forbidden

            raise Forbidden("当前角色无权访问该资源")
        return actor

    # ------------------------------------------------------------ 路由

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            body = self._read_json() if method in ("POST", "DELETE") else {}
            self._route(method, path, query, body)
        except Exception as exc:  # noqa: BLE001 - 统一错误出口
            self._send_error(exc)

    def _route(self, method: str, path: str, query: dict, body: dict) -> None:
        svc = self.service

        if method == "GET" and path == "/api/health":
            return self._send(200, {"status": "ok", "time": svc.clock.iso()})

        if method == "GET" and path == "/api/public/works":
            return self._send(200, {"works": svc.public_works()})

        # ---- 时钟管理（课程负责人）----
        if method == "POST" and path == "/api/admin/clock":
            self._actor({Role.COORDINATOR})
            return self._send(200, self._control_clock(body))

        # ---- 账户与学籍 ----
        if method == "POST" and path == "/api/users":
            actor = self._actor({Role.COORDINATOR})
            return self._idempotent(lambda: svc.create_user(
                body["id"], body["role"], body["display_name"], body.get("token"), ), actor)

        if method == "POST" and path == "/api/students":
            actor = self._actor({Role.COORDINATOR})
            return self._idempotent(lambda: svc.create_student(
                body["id"], body["name"], body["grade"], body["class_group"], actor), actor)

        m = re.fullmatch(r"/api/students/([^/]+)/transfer", path)
        if method == "POST" and m:
            actor = self._actor({Role.COORDINATOR})
            return self._send(200, svc.transfer_student(m.group(1), body["class_group"], actor))

        if method == "POST" and path == "/api/guardian-links":
            actor = self._actor({Role.COORDINATOR})
            svc.link_guardian(body["guardian_id"], body["student_id"], actor)
            return self._send(204, None)

        # ---- 工艺与材料主数据 ----
        if method == "POST" and path == "/api/craft-versions":
            actor = self._actor({Role.COORDINATOR, Role.MASTER})
            result = svc.create_craft_version(
                body["code"], body["grade"], body["name"],
                float(body["min_drying_hours"]), body["glaze_family"],
                list(body["safety_requirements"]), actor,
            )
            return self._send(201, result)

        if method == "POST" and path == "/api/glaze-compat":
            actor = self._actor({Role.COORDINATOR, Role.MASTER})
            svc.add_glaze_compat(body["family"], body["material_code"], actor)
            return self._send(204, None)

        if method == "POST" and path == "/api/materials":
            actor = self._actor({Role.COORDINATOR, Role.MASTER})
            result = svc.register_material_lot(
                body["material_code"], body["lot_no"], body["kind"], body["name"],
                body.get("glaze_family"), body.get("forbidden_grades"),
                body.get("supplier", ""), actor,
            )
            return self._send(201, result)

        m = re.fullmatch(r"/api/materials/([^/]+)/([^/]+)/deactivate", path)
        if method == "POST" and m:
            actor = self._actor({Role.COORDINATOR, Role.MASTER})
            svc.deactivate_material_lot(m.group(1), m.group(2), actor)
            return self._send(204, None)

        # ---- 作品 ----
        if method == "POST" and path == "/api/works":
            actor = self._actor({Role.TEACHER})
            device_id = self.headers.get("X-Device-Id", "")
            content_hash = self.headers.get("X-Content-Hash", "")
            return self._idempotent(lambda: svc.create_work(
                body["student_id"], actor, device_id, content_hash,
                craft_code=body["craft_code"],
                safety_acks=body.get("safety_acks"),
                storage_location=body.get("storage_location"),
            ), actor, status=201)

        if method == "GET" and path == "/api/works":
            actor = self._actor(STAFF)
            return self._send(200, {"works": svc.list_works(
                actor, status=query.get("status"), student_id=query.get("student_id"))})

        m = re.fullmatch(r"/api/works/([^/]+)", path)
        if method == "GET" and m:
            actor = self._actor(STAFF)
            return self._send(200, svc.get_work(m.group(1), actor))

        m = re.fullmatch(r"/api/works/([^/]+)/steps", path)
        if method == "POST" and m:
            actor = self._actor({Role.TEACHER})
            work_id = m.group(1)
            result = svc.sign_step(
                work_id, body["step"], actor,
                material_code=body.get("material_code"),
                material_lot=body.get("material_lot"),
                signed_for=body.get("signed_for"),
                note=body.get("note", ""),
            )
            return self._send(201, result)

        m = re.fullmatch(r"/api/works/([^/]+)/review", path)
        if method == "POST" and m:
            actor = self._actor({Role.TEACHER})
            return self._send(200, svc.review_work(m.group(1), actor))

        m = re.fullmatch(r"/api/works/([^/]+)/storage", path)
        if method == "POST" and m:
            actor = self._actor({Role.TEACHER})
            return self._send(200, svc.update_storage(m.group(1), body["location"], actor))

        # ---- 展示授权（监护人）----
        m = re.fullmatch(r"/api/works/([^/]+)/consent/(grant|withdraw)", path)
        if method == "POST" and m:
            actor = self._actor({Role.GUARDIAN})
            work_id, action = m.group(1), m.group(2)
            if action == "grant":
                return self._send(200, svc.grant_consent(work_id, actor))
            return self._send(200, svc.withdraw_consent(
                work_id, actor, reason=body.get("reason", "")))

        # ---- 作品沿革导出 ----
        m = re.fullmatch(r"/api/works/([^/]+)/history", path)
        if method == "GET" and m:
            actor = self._actor(STAFF)
            history = svc.work_history(m.group(1), actor)
            fmt = query.get("format", "json")
            if fmt == "csv":
                rows = self._flatten_history(history)
                return self._send(*_csv_response(rows, f"work-{m.group(1)}-history.csv"))
            headers = {
                "Content-Disposition": f'attachment; filename="work-{m.group(1)}-history.json"'
            }
            return self._send(200, history, headers)

        # ---- 窑次 ----
        if method == "POST" and path == "/api/kilns":
            actor = self._actor({Role.TEACHER})
            result = svc.create_kiln(
                body["batch_code"], int(body["capacity"]), actor, body.get("note", ""))
            return self._send(201, result)

        if method == "GET" and path == "/api/kilns":
            actor = self._actor(STAFF)
            return self._send(200, {"kilns": svc.list_kilns(actor)})

        m = re.fullmatch(r"/api/kilns/([^/]+)", path)
        if method == "GET" and m:
            self._actor(STAFF)
            return self._send(200, svc.get_kiln(m.group(1)))

        m = re.fullmatch(r"/api/kilns/([^/]+)/manifest", path)
        if method == "GET" and m:
            self._actor(STAFF)
            kiln = svc.get_kiln(m.group(1))
            fmt = query.get("format", "csv")
            if fmt == "json":
                return self._send(200, kiln)
            rows = self._manifest_rows(kiln)
            return self._send(*_csv_response(rows, f"{m.group(1)}-manifest.csv"))

        m = re.fullmatch(r"/api/kilns/([^/]+)/works", path)
        if method == "POST" and m:
            actor = self._actor({Role.TEACHER})
            return self._send(201, svc.add_to_kiln(m.group(1), body["work_id"], actor))

        m = re.fullmatch(r"/api/kilns/([^/]+)/works/([^/]+)", path)
        if method == "DELETE" and m:
            actor = self._actor({Role.TEACHER})
            return self._send(200, svc.remove_from_kiln(m.group(1), m.group(2), actor))

        for action, handler_name in (
            ("start", "start_firing"), ("done", "mark_done"),
        ):
            m = re.fullmatch(rf"/api/kilns/([^/]+)/{action}", path)
            if method == "POST" and m:
                actor = self._actor({Role.TEACHER})
                func = getattr(svc, handler_name)
                return self._send(200, func(m.group(1), actor))

        m = re.fullmatch(r"/api/kilns/([^/]+)/qc", path)
        if method == "POST" and m:
            actor = self._actor({Role.MASTER, Role.COORDINATOR})
            return self._send(200, svc.quality_check(
                m.group(1), bool(body["passed"]), actor))

        m = re.fullmatch(r"/api/kilns/([^/]+)/cancel", path)
        if method == "POST" and m:
            actor = self._actor({Role.TEACHER, Role.COORDINATOR})
            return self._send(200, svc.cancel_kiln(
                m.group(1), actor, reason=body.get("reason", "")))

        m = re.fullmatch(r"/api/kilns/([^/]+)/dispositions", path)
        if method == "POST" and m:
            actor = self._actor({Role.TEACHER, Role.MASTER, Role.COORDINATOR})
            return self._send(201, svc.dispose_work(
                m.group(1), body["work_id"], body["decision"], actor,
                reentry_step=body.get("reentry_step"), note=body.get("note", "")))

        self._send(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found", "message": "接口不存在"}})

    # ------------------------------------------------------------ 辅助

    def _control_clock(self, body: dict) -> dict:
        clock = self.service.clock
        mode = body.get("mode", "freeze")
        if mode == "resume":
            clock.resume()
        elif mode == "freeze":
            moment = None
            if body.get("at"):
                moment = datetime.fromisoformat(body["at"])
            clock.freeze(moment)
        elif mode == "advance":
            clock.advance(
                days=int(body.get("days", 0)),
                hours=int(body.get("hours", 0)),
                minutes=int(body.get("minutes", 0)),
            )
        else:
            raise DomainError("时钟模式必须是 freeze / advance / resume")
        return {"now": clock.iso(), "frozen": mode != "resume"}

    def _idempotent(self, fn, actor: sqlite3.Row, status: int = 200):
        """按 Idempotency-Key 头缓存并原样重放响应。"""
        key = self.headers.get("Idempotency-Key")
        if not key:
            result = fn()
            return self._send(status, result)
        db = self.service.db
        now = self.service.clock.iso()
        with db.transaction() as conn:
            cached = conn.execute(
                "SELECT status, body FROM request_keys WHERE request_key = ?", (key,)
            ).fetchone()
            if cached is not None:
                if cached["status"] == 0:
                    raise Conflict("相同 Idempotency-Key 的请求仍在处理中")
                return self._send(cached["status"], json.loads(cached["body"]))
            try:
                conn.execute(
                    "INSERT INTO request_keys(request_key, status, body, at)"
                    " VALUES (?, 0, '', ?)",
                    (key, now),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("相同 Idempotency-Key 的请求仍在处理中") from exc
        try:
            result = fn()
        except Exception:
            with db.transaction() as conn:
                conn.execute("DELETE FROM request_keys WHERE request_key = ?", (key,))
            raise
        body_json = json.dumps(result, ensure_ascii=False)
        with db.transaction() as conn:
            conn.execute(
                "UPDATE request_keys SET status = ?, body = ? WHERE request_key = ?",
                (status, body_json, key),
            )
            log_event(conn, now, actor["id"], "request.replayed_store", "request_key", key, {})
        return self._send(status, result)

    @staticmethod
    def _manifest_rows(kiln: dict) -> list[dict]:
        rows = []
        for m in kiln["members"]:
            rows.append({
                "batch_code": kiln["batch_code"],
                "batch_status": kiln["status"],
                "capacity": kiln["capacity"],
                "active_count": kiln["active_count"],
                "work_id": m["work_id"],
                "public_code": m["public_code"],
                "student_id": m["student_id"],
                "student_name": m["student_name"],
                "work_status": m["work_status"],
                "added_at": m["added_at"],
                "removed_at": m["removed_at"] or "",
                "decision": m["decision"] or "",
                "reentry_step": m["reentry_step"] or "",
            })
        if not rows:
            rows.append({
                "batch_code": kiln["batch_code"], "batch_status": kiln["status"],
                "capacity": kiln["capacity"], "active_count": kiln["active_count"],
                "work_id": "", "public_code": "", "student_id": "", "student_name": "",
                "work_status": "", "added_at": "", "removed_at": "",
                "decision": "", "reentry_step": "",
            })
        return rows

    @staticmethod
    def _flatten_history(history: dict) -> list[dict]:
        work = history["work"]
        rows = []
        for step in history["steps"]:
            rows.append({
                "work_id": work["id"], "public_code": work["public_code"],
                "kind": "step", "at": step["signed_at"], "actor": step["signer_name"],
                "detail": f"{step['step']} material={step['material_code'] or ''}"
                          f":{step['material_lot'] or ''} signed_for={step['signed_for_name'] or ''}",
            })
        for membership in history["kiln_memberships"]:
            rows.append({
                "work_id": work["id"], "public_code": work["public_code"],
                "kind": "kiln", "at": membership["added_at"], "actor": "",
                "detail": f"{membership['batch_code']} status={membership['batch_status']}"
                          f" decision={membership['decision'] or ''}",
            })
        consent = history["consent"]
        if consent:
            rows.append({
                "work_id": work["id"], "public_code": work["public_code"],
                "kind": "consent",
                "at": consent["withdrawn_at"] or consent["granted_at"],
                "actor": consent["granted_by"],
                "detail": consent["status"],
            })
        for event in history["events"]:
            rows.append({
                "work_id": work["id"], "public_code": work["public_code"],
                "kind": "event", "at": event["at"], "actor": event["actor"],
                "detail": f"{event['action']} {json.dumps(event['payload'], ensure_ascii=False)}",
            })
        rows.sort(key=lambda r: r["at"] or "")
        return rows


def build_server(db: Database, clock: Clock | None = None, host: str = "127.0.0.1",
                 port: int = 8080) -> ThreadingHTTPServer:
    service = PotteryService(db, clock)

    class _Handler(PotteryHandler):
        pass

    _Handler.service = service
    server = ThreadingHTTPServer((host, port), _Handler)
    server.service = service  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="校园陶艺烧制批次管控服务")
    parser.add_argument("--db", default="pottery.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)

    db = Database(args.db)
    server = build_server(db, Clock(), args.host, args.port)
    print(f"服务已启动：http://{args.host}:{args.port}（数据库 {args.db}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
