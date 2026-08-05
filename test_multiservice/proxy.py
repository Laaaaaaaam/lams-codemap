"""请求转发模块 — Python 实现。

将请求转发到后端微服务。
"""

import http.client
import json
from typing import Any


def forward_request(request) -> Any:
    """将请求转发到后端服务。

    根据路径选择后端，使用 HTTP 连接转发。
    """
    backend = _select_backend(request.path)
    if backend is None:
        return _error_response(404, "no backend for path")

    host, port = backend
    try:
        conn = http.client.HTTPConnection(host, port, timeout=30)
        headers = dict(request.headers)
        headers["X-User-Id"] = request.user_id or ""
        
        conn.request(request.method, request.path, request.body, headers)
        resp = conn.getresponse()
        body = resp.read()
        
        return _build_response(resp.status, body, dict(resp.getheaders()))
    except (http.client.HTTPException, ConnectionError, OSError) as e:
        return _error_response(502, f"backend error: {e}")


def _select_backend(path: str) -> tuple[str, int] | None:
    """根据路径选择后端服务。"""
    if path.startswith("/api/users"):
        return ("user-service", 8080)
    elif path.startswith("/api/orders"):
        return ("order-service", 8081)
    elif path.startswith("/api/products"):
        return ("product-service", 8082)
    return None


def _build_response(status: int, body: bytes, headers: dict) -> Any:
    """构建响应对象。"""
    # 延迟导入避免循环依赖
    from gateway import Response
    return Response(status, body, headers)


def _error_response(status: int, message: str) -> Any:
    """构建错误响应。"""
    from gateway import Response
    return Response(status, json.dumps({"error": message}).encode())
