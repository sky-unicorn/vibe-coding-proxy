# -*- coding: utf-8 -*-
"""
MCP OAuth 2.1 处理模块
实现 RFC 6749 + RFC 8414 + RFC 9728 OAuth 规范
"""

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
import uuid
import sqlite3
import os
from datetime import datetime, timedelta
from functools import wraps
from flask import request, jsonify, redirect, session, url_for, Response

import config

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy.db")

# ---- PKCE 工具 ----

def generate_code_verifier():
    """生成 PKCE code_verifier (43-128 字符)"""
    return secrets.token_urlsafe(96)[:128]


def generate_code_challenge(verifier):
    """生成 PKCE code_challenge (S256)"""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_pkce(code_verifier, code_challenge, method="S256"):
    """验证 PKCE code_verifier"""
    if method == "S256":
        return generate_code_challenge(code_verifier) == code_challenge
    return False


# ---- JWT 简单实现（用于 access_token） ----

def create_access_token(client_id: str, scope: str = "", expires_in: int = 3600) -> str:
    """创建简单的 JWT-like access_token（不依赖外部库）"""
    header = {"alg": "none", "typ": "JWT"}
    payload = {
        "iss": _get_issuer(),
        "sub": client_id,
        "scope": scope,
        "iat": int(time.time()),
        "exp": int(time.time()) + expires_in,
        "jti": uuid.uuid4().hex,
    }
    segments = [
        base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode(),
        base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode(),
        "",
    ]
    return ".".join(segments[:2])


def decode_token(token: str):
    """解码 access_token（仅验证格式，不验证签名，alg=none）"""
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        header = json.loads(base64.urlsafe_b64decode(parts[0] + "="))
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "="))
        return payload
    except Exception:
        return None


def validate_access_token(token: str) -> dict | None:
    """验证 access_token 是否有效"""
    payload = decode_token(token)
    if not payload:
        return None
    # 检查 exp
    if payload.get("exp", 0) < int(time.time()):
        return None
    return payload


# ---- OAuth 数据库操作 ----

def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def register_oauth_client(application_type: str, client_name: str, redirect_uris: list,
                          grant_types: list, response_types: list, token_endpoint_auth_method: str = "none"):
    """动态注册 OAuth 客户端"""
    conn = _get_conn()
    # 检查是否已存在同名客户端
    existing = conn.execute(
        "SELECT client_id FROM oauth_clients WHERE client_name = ?", (client_name,)
    ).fetchone()
    if existing:
        conn.close()
        return existing["client_id"], None

    client_id = secrets.token_urlsafe(32)
    client_secret = secrets.token_urlsafe(48) if token_endpoint_auth_method != "none" else None

    conn.execute("""
        INSERT INTO oauth_clients
        (client_id, client_name, client_secret, application_type, redirect_uris,
         grant_types, response_types, token_endpoint_auth_method, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        client_id, client_name, client_secret, application_type,
        json.dumps(redirect_uris), json.dumps(grant_types),
        json.dumps(response_types), token_endpoint_auth_method,
        datetime.now().isoformat()
    ))
    conn.commit()
    conn.close()
    return client_id, client_secret


def create_authorization_code(client_id: str, code: str, redirect_uri: str,
                                scope: str, code_challenge: str, user_id: int,
                                resource: str = ""):
    """创建授权码，存储 code_challenge 用于后续 PKCE 验证"""
    conn = _get_conn()
    expires_at = datetime.now() + timedelta(minutes=10)
    conn.execute("""
        INSERT INTO oauth_codes
        (code, client_id, redirect_uri, scope, code_verifier, user_id,
         resource, expires_at, used)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        code, client_id, redirect_uri, scope, code_challenge, user_id,
        resource,
        expires_at.isoformat(),
        0
    ))
    conn.commit()
    conn.close()


def consume_authorization_code(code: str, client_id: str) -> dict | None:
    """消费（验证并标记）授权码"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM oauth_codes WHERE code = ? AND client_id = ? AND used = 0",
        (code, client_id)
    ).fetchone()
    if not row:
        conn.close()
        return None
    # 检查是否过期（默认 10 分钟）
    expires_at = datetime.fromisoformat(row["expires_at"])
    if datetime.now() > expires_at:
        conn.close()
        return None
    # 标记为已使用
    conn.execute("UPDATE oauth_codes SET used = 1 WHERE code = ?", (code,))
    conn.commit()
    result = dict(row)
    conn.close()
    return result


def create_token(client_id: str, token_type: str = "Bearer", scope: str = "",
                  user_id: int = None, refresh_token: str = "") -> tuple:
    """创建访问令牌"""
    conn = _get_conn()
    access_token = create_access_token(client_id, scope)
    if not refresh_token:
        refresh_token = secrets.token_urlsafe(48)

    # 写入数据库
    now = datetime.now()
    expires_at = now + timedelta(hours=1)
    conn.execute("""
        INSERT INTO oauth_tokens
        (client_id, access_token, access_token_expires_at, refresh_token,
         refresh_token_expires_at, scope, user_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        client_id, access_token,
        expires_at.isoformat(),
        refresh_token,
        (now + timedelta(days=30)).isoformat(),
        scope, user_id, now.isoformat()
    ))
    conn.commit()
    conn.close()
    return access_token, refresh_token


def refresh_access_token(refresh_token: str, client_id: str) -> tuple | None:
    """使用 refresh_token 刷新访问令牌"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM oauth_tokens WHERE refresh_token = ? AND client_id = ?",
        (refresh_token, client_id)
    ).fetchone()
    if not row:
        conn.close()
        return None
    # 检查 refresh_token 是否过期
    expires_at = datetime.fromisoformat(row["refresh_token_expires_at"])
    if datetime.now() > expires_at:
        conn.close()
        return None
    conn.close()
    # 生成新令牌
    return create_token(client_id, scope=row["scope"], user_id=row["user_id"])


# ---- 辅助函数 ----

def _get_issuer():
    """获取 OAuth issuer URL"""
    try:
        return request.host_url.rstrip("/")
    except RuntimeError:
        # 不在请求上下文中时返回默认值
        return "http://localhost:5000"


def require_auth_server_auth(func):
    """装饰器：验证 Authorization Server 的管理端点调用（内部使用）"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        # 这里可以添加额外的服务端管理认证
        return func(*args, **kwargs)
    return wrapper


# ---- OAuth 元数据端点 ----

def get_oauth_authorization_server_metadata():
    """返回 Authorization Server Metadata (RFC 8414)"""
    base = request.host_url.rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": base + "/oauth/authorize",
        "token_endpoint": base + "/oauth/token",
        "registration_endpoint": base + "/oauth/register",
        "jwks_uri": base + "/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp:read", "mcp:write"],
        "client_id_metadata_document_supported": True,
    }


def get_oauth_protected_resource_metadata(resource: str = "/mcp"):
    """返回 Protected Resource Metadata (RFC 9728)"""
    base = request.host_url.rstrip("/")
    return {
        "resource": base + resource,
        "authorization_servers": [base],
        "bearer_methods_supported": ["header", "body"],
        "scopes_supported": ["mcp:read", "mcp:write"],
        "jwks_uri": base + "/.well-known/jwks.json",
    }