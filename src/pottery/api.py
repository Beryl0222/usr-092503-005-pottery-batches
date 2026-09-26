"""HTTP 接口层：基于标准库 http.server 的角色受限 JSON API。

认证方式：请求头 ``X-User-Id`` 标识操作者，服务端据其角色判定权限；
``/api/public/*`` 无需认证，输出已匿名化。
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import AppError, Validation
from .services import PotteryService

# (方法, 路径模式, 服务方法名, 是否公开)
ROUTES: list[tuple[str, str, str, bool]] = [
    ("POST", "/api/craft-versions", "create_craft_version", False),
    ("GET", "/api/craft-versions", "list_craft_versions", False),
    ("POST", "/api/works", "register_work", False),
    ("GET", "/api/works/{work_id}", "get_work", False),
    ("POST", "/api/works/{work_id}/steps", "sign_step", False),
    ("POST", "/api/works/{work_id}/review", "review_work", False),
    ("POST", "/api/works/{work_id}/storage", "move_storage", False),
    ("POST", "/api/works/{work_id}/consent", "set_consent", False),
    ("GET", "/api/works/{work_id}/history", "work_history", False),
    ("POST", "/api/batches", "create_batch", False),
    ("GET", "/api/batches", "list_batches", False),
    ("POST", "/api/batches/{batch_id}/items", "admit_to_batch", False),
    ("POST", "/api/batches/{batch_id}/finish", "finish_batch", False),
    ("GET", "/api/batches/{batch_id}/manifest", "batch_manifest", False),
    ("POST", "/api/students/{student_id}/transfer", "transfer_student", False),
    ("GET", "/api/public/exhibits", "public_exhibits", True),
    ("GET", "/api/public/works/{work_id}", "public_work", True),
]

# 导出类端点支持 ?format=csv
CSV_CAPABLE = {"work_history", "batch_manifest"}


def _compile(pattern: str) -> re.Pattern:
    regex = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern)
    return re.compile(f"^{regex}$")


COMPILED = [(m, _compile(p), name, public) for m, p, name, public in ROUTES]


def make_handler(service: PotteryService):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "PotteryServer/0.1"

        def log_message(self, fmt, *args):  # 静默默认访问日志
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, payload) -> None:
            self._send(status,
                       json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise Validation("请求体必须是合法 JSON") from None
            if not isinstance(data, dict):
                raise Validation("请求体必须是 JSON 对象")
            return data

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            route = None
            match = None
            for route_method, regex, name, public in COMPILED:
                if route_method != method:
                    continue
                match = regex.match(parsed.path)
                if match:
                    route = (name, public)
                    break
            if route is None:
                self._send_json(404, {"error": {"code": "not_found",
                                                "message": "接口不存在"}})
                return
            name, _public = route
            try:
                actor_id = self.headers.get("X-User-Id")
                kwargs = match.groupdict()
                if name in CSV_CAPABLE and \
                        (query.get("format") or [""])[0] == "csv":
                    text = getattr(service, f"{name}_csv")(actor_id, **kwargs)
                    self._send(200, text.encode("utf-8"),
                               "text/csv; charset=utf-8")
                    return
                if method == "POST":
                    body = self._read_body()
                    if name in ("sign_step", "review_work", "move_storage",
                                "set_consent", "admit_to_batch", "finish_batch",
                                "transfer_student"):
                        result = getattr(service, name)(actor_id, *kwargs.values(),
                                                        body)
                    else:
                        result = getattr(service, name)(actor_id, body)
                else:
                    if name in ("public_exhibits", "list_batches"):
                        result = getattr(service, name)() \
                            if name == "public_exhibits" \
                            else getattr(service, name)(actor_id)
                    elif name == "list_craft_versions":
                        result = service.list_craft_versions(
                            actor_id, grade=(query.get("grade") or [None])[0])
                    elif name == "public_work":
                        result = service.public_work(kwargs["work_id"])
                    else:
                        result = getattr(service, name)(actor_id, *kwargs.values())
                self._send_json(200, result)
            except AppError as exc:
                self._send_json(exc.status, exc.payload())
            except Exception as exc:  # pragma: no cover - 防御性兜底
                self._send_json(500, {"error": {"code": "internal",
                                                "message": str(exc)}})

    return Handler


def make_server(service: PotteryService, host: str = "127.0.0.1",
                port: int = 8080) -> ThreadingHTTPServer:
    """构建服务实例；port=0 时由系统分配端口。"""
    return ThreadingHTTPServer((host, port), make_handler(service))


def run(store_path: str = "pottery.db", host: str = "127.0.0.1",
        port: int = 8080) -> None:  # pragma: no cover
    from .clock import SystemClock
    from .store import Store

    service = PotteryService(Store(store_path), SystemClock())
    server = make_server(service, host, port)
    print(f"listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":  # pragma: no cover
    run()
