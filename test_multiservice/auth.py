"""认证模块 — Python 实现。

提供 JWT token 生成与验证。
"""

import hashlib
import time
import json
import base64

SECRET_KEY = "super_secret_key_123"
TOKEN_EXPIRY = 3600  # 秒


def generate_token(username: str) -> str:
    """生成 JWT token。"""
    payload = {
        "username": username,
        "exp": int(time.time()) + TOKEN_EXPIRY,
    }
    payload_json = json.dumps(payload)
    payload_b64 = base64.b64encode(payload_json.encode()).decode()
    signature = hashlib.sha256(f"{payload_b64}.{SECRET_KEY}".encode()).hexdigest()
    return f"{payload_b64}.{signature}"


def verify_token(token: str) -> str | None:
    """验证 JWT token，返回用户名或 None。"""
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        
        payload_b64, signature = parts
        expected_sig = hashlib.sha256(f"{payload_b64}.{SECRET_KEY}".encode()).hexdigest()
        
        if signature != expected_sig:
            return None
        
        payload_json = base64.b64decode(payload_b64).decode()
        payload = json.loads(payload_json)
        
        if payload["exp"] < time.time():
            return None
        
        return payload["username"]
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


def hash_password(password: str) -> str:
    """密码哈希。"""
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(password: str, hashed: str) -> bool:
    """验证密码。"""
    return hash_password(password) == hashed
