"""API Gateway — Python 实现的网关服务。

负责路由分发、认证、限流。
"""

from typing import Any
from auth import verify_token, generate_token
from ratelimit import RateLimiter
from proxy import forward_request


class Request:
    def __init__(self, method: str, path: str, headers: dict[str, str], body: bytes = b""):
        self.method = method
        self.path = path
        self.headers = headers
        self.body = body
        self.user_id: str | None = None


class Response:
    def __init__(self, status: int = 200, body: bytes = b"", headers: dict[str, str] | None = None):
        self.status = status
        self.body = body
        self.headers = headers or {}


def handle_request(request: Request) -> Response:
    """处理传入请求：认证 → 限流 → 转发。"""
    token = request.headers.get("Authorization", "")
    
    if not token:
        return Response(401, b'{"error": "missing token"}')
    
    user = verify_token(token)
    if user is None:
        return Response(403, b'{"error": "invalid token"}')
    
    request.user_id = user
    
    limiter = RateLimiter()
    if not limiter.allow(user):
        return Response(429, b'{"error": "rate limited"}')
    
    return forward_request(request)


def health_check() -> dict[str, Any]:
    """健康检查端点。"""
    return {"status": "ok", "service": "api-gateway"}


def login(username: str, password: str) -> str:
    """登录接口，返回 JWT token。"""
    if username == "admin" and password == "secret":
        return generate_token(username)
    raise ValueError("invalid credentials")


# 路由表
ROUTES = {
    "/api/users": "user-service",
    "/api/orders": "order-service",
    "/api/products": "product-service",
}
