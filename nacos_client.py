"""Nacos 3.x Console API 封装。

提供命名空间与配置的 CRUD 操作，内部管理 accessToken 缓存与自动刷新。
模块级函数设计，不使用类。

连接参数（console_url / auth_url / username / password）由调用方显式通过
conn dict 传入；服务端不持久化这些参数，conn 来源通常是 MCP 客户端的 HTTP
请求头（X-Nacos-Console-Url / X-Nacos-Auth-Url / X-Nacos-Username / X-Nacos-Password）。

token 缓存按 (console_url, username) 做 key 进程级缓存，不同连接互不串扰。

API 路径基于 Nacos 3.0/3.1/3.2 官方文档核对：
  - 命名空间：/v3/console/core/namespace
  - 配置：/v3/console/cs/config
  - 认证：/v3/auth/user/login（Server 侧，非 Console 侧）
  - 统一信封：{code, message, data}，仅 code==0 判成功
"""

import threading
import time

import requests


class NacosError(Exception):
    """Nacos API 业务错误或网络异常"""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


# ---- Token 缓存 ----

# key: (console_url, username) -> {"token": str, "expires_at": float}
_TOKEN_CACHE = {}
_TOKEN_LOCK = threading.Lock()


def _extract_token(body):
    """从 Nacos 登录响应中提取 accessToken。

    Nacos 3.x 不同小版本的登录响应结构有差异，按以下优先级 fallback：
      1. 顶层 accessToken：{accessToken: "...", tokenTtl: 18000, ...}
         （Nacos 3.2+ 直接在顶层返回）
      2. data.accessToken：{data: {accessToken: "...", tokenTtl: 18000}, ...}
         （Nacos 3.0/3.1 在 data 对象内返回）
      3. data 本身即 token 对象：{data: {accessToken: "..."}, ...}
         （兜底：data 是 dict 且含 accessToken）

    找不到时抛 NacosError。
    """
    # 优先级 1：顶层 accessToken
    token = body.get("accessToken")
    if token:
        return token

    # 优先级 2：data.accessToken（data 是 dict）
    data = body.get("data")
    if isinstance(data, dict):
        token = data.get("accessToken")
        if token:
            return token

    # 优先级 3：data 本身可能是整个 token 对象（兜底）
    if isinstance(data, dict):
        # 已在优先级 2 中检查过 accessToken，此处不再重复
        pass

    raise NacosError(f"登录响应中未找到 accessToken: {str(body)[:200]}")


def _extract_token_ttl(body):
    """从 Nacos 登录响应中提取 tokenTtl（秒），默认 18000。"""
    # 顶层 tokenTtl
    ttl = body.get("tokenTtl")
    if ttl is not None:
        try:
            return int(ttl)
        except (ValueError, TypeError):
            pass

    # data.tokenTtl
    data = body.get("data")
    if isinstance(data, dict):
        ttl = data.get("tokenTtl")
        if ttl is not None:
            try:
                return int(ttl)
            except (ValueError, TypeError):
                pass

    return 18000


def _login(conn, force=False):
    """登录 Nacos 获取 accessToken。force=True 时丢弃缓存强制重登。

    使用 Server 侧认证端点 POST {conn['auth_url']}/v3/auth/user/login，
    Content-Type: application/x-www-form-urlencoded。
    """
    auth_url = (conn or {}).get("auth_url", "")
    username = (conn or {}).get("username", "")
    password = (conn or {}).get("password", "")
    console_url = (conn or {}).get("console_url", "")

    if not username:
        raise NacosError("未配置 Nacos 账号")

    cache_key = (console_url, username)

    if not force:
        with _TOKEN_LOCK:
            entry = _TOKEN_CACHE.get(cache_key)
            if entry and entry.get("token") and time.time() < entry.get("expires_at", 0):
                return entry["token"]

    # 登录请求：必须用 data= 而非 json=，因为 Nacos 要求 form-urlencoded
    login_url = auth_url.rstrip("/") + "/v3/auth/user/login"
    try:
        resp = requests.post(
            login_url,
            data={"username": username, "password": password},
            timeout=10,
        )
    except requests.RequestException as e:
        raise NacosError(f"连接 Nacos 认证服务失败: {e}")

    if resp.status_code != 200:
        raise NacosError(
            f"Nacos 登录失败 (HTTP {resp.status_code}): {resp.text[:200]}"
        )

    try:
        body = resp.json()
    except (ValueError, TypeError):
        raise NacosError(f"Nacos 登录响应解析失败: {resp.text[:200]}")

    token = _extract_token(body)
    token_ttl = _extract_token_ttl(body)

    expires_at = time.time() + token_ttl - 60  # 提前 60 秒过期

    with _TOKEN_LOCK:
        _TOKEN_CACHE[cache_key] = {"token": token, "expires_at": expires_at}

    return token


def _get_valid_token(conn):
    """获取有效的 accessToken，过期则自动重登。"""
    console_url = (conn or {}).get("console_url", "")
    username = (conn or {}).get("username", "")
    cache_key = (console_url, username)
    with _TOKEN_LOCK:
        entry = _TOKEN_CACHE.get(cache_key)
        if entry and entry.get("token") and time.time() < entry.get("expires_at", 0):
            return entry["token"]
    return _login(conn, force=False)


def _request(conn, method, path, params=None, data=None):
    """统一请求封装。

    URL = {conn['console_url']}{path}；自动加 Authorization: Bearer header；
    超时 10s；解析信封仅看 code==0；401 重登重试一次，403 不重登。
    """
    console_url = (conn or {}).get("console_url", "")
    if not console_url:
        raise NacosError("未配置 Nacos Console URL")

    url = console_url.rstrip("/") + path

    # 获取 token（username 为空时 _login 会抛 NacosError）
    token = _get_valid_token(conn)

    headers = {"Authorization": f"Bearer {token}"}

    retries_on_401 = 1
    for attempt in range(retries_on_401 + 1):
        try:
            resp = requests.request(
                method,
                url,
                params=params,
                data=data,
                headers=headers,
                timeout=10,
            )
        except requests.RequestException as e:
            raise NacosError(f"请求 Nacos 失败: {e}")

        if resp.status_code == 401 and attempt < retries_on_401:
            # 401：token 过期，强制重登后重试一次
            token = _login(conn, force=True)
            headers["Authorization"] = f"Bearer {token}"
            continue

        if resp.status_code == 403:
            # 403：权限不足，不重登
            try:
                msg = resp.json().get("message", resp.text[:200])
            except (ValueError, TypeError):
                msg = resp.text[:200]
            raise NacosError(f"Nacos 权限不足: {msg}", code=403)

        break

    # 解析响应
    try:
        body = resp.json()
    except (ValueError, TypeError):
        if not resp.ok:
            raise NacosError(f"Nacos 请求失败 (HTTP {resp.status_code}): {resp.text[:200]}", code=resp.status_code)
        raise NacosError(f"Nacos 响应解析失败 (HTTP {resp.status_code}): {resp.text[:200]}")

    # 信封判定：仅 code == 0 判成功
    code = body.get("code")
    if code != 0:
        message = body.get("message", "未知错误")
        raise NacosError(f"Nacos 错误: {message}", code=code)

    return body.get("data")


# ---- 命名空间 CRUD ----

def list_namespaces(conn):
    """列出全部命名空间。返回 [{namespace, namespaceShowName, namespaceDesc, ...}]。"""
    return _request(conn, "GET", "/v3/console/core/namespace/list")


def create_namespace(conn, namespace_id=None, namespace_name=None, namespace_desc=None):
    """创建命名空间。namespace_id 省略时由 Nacos 服务端生成。返回 bool。"""
    form = {}
    if namespace_id:
        form["customNamespaceId"] = namespace_id
    form["namespaceName"] = namespace_name or ""
    if namespace_desc:
        form["namespaceDesc"] = namespace_desc
    return _request(conn, "POST", "/v3/console/core/namespace", data=form)


def update_namespace(conn, namespace_id=None, namespace_name=None, namespace_desc=None):
    """更新命名空间。namespace_id 必填。返回 bool。"""
    form = {"namespaceId": namespace_id or ""}
    form["namespaceName"] = namespace_name or ""
    if namespace_desc:
        form["namespaceDesc"] = namespace_desc
    return _request(conn, "PUT", "/v3/console/core/namespace", data=form)


def delete_namespace(conn, namespace_id=None):
    """删除命名空间。namespace_id 必填。返回 bool。"""
    return _request(conn, "DELETE", "/v3/console/core/namespace", params={"namespaceId": namespace_id or ""})


# ---- 配置 CRUD ----

def list_configs(conn, namespace_id=None, group=None, data_id=None, page_no=1, page_size=10, search="blur"):
    """分页查询配置列表。"""
    params = {
        "pageNo": page_no,
        "pageSize": page_size,
        "search": search or "blur",
    }
    # namespace_id 空串或 "public" 时不传（默认查 public）
    if namespace_id and namespace_id != "public":
        params["namespaceId"] = namespace_id
    if data_id:
        params["dataId"] = data_id
    if group:
        params["groupName"] = group  # tool schema 的 group -> Nacos 的 groupName
    return _request(conn, "GET", "/v3/console/cs/config/list", params=params)


def get_config(conn, namespace_id=None, data_id=None, group=None):
    """读取单个配置的完整内容。"""
    params = {
        "dataId": data_id or "",
        "groupName": group or "",  # tool schema 的 group -> Nacos 的 groupName
    }
    if namespace_id and namespace_id != "public":
        params["namespaceId"] = namespace_id
    return _request(conn, "GET", "/v3/console/cs/config", params=params)


def publish_config(conn, namespace_id=None, data_id=None, group=None, content=None,
                   type=None, app_name=None, desc=None, tags=None):
    """发布配置（Nacos 语义：存在即覆盖）。返回 bool。"""
    form = {
        "dataId": data_id or "",
        "groupName": group or "",  # tool schema 的 group -> Nacos 的 groupName
        "content": content or "",
    }
    if namespace_id and namespace_id != "public":
        form["namespaceId"] = namespace_id
    if type:
        form["type"] = type
    if app_name:
        form["appName"] = app_name
    if desc:
        form["desc"] = desc
    if tags:
        form["configTags"] = tags  # tool schema 的 tags -> Nacos 的 configTags
    return _request(conn, "POST", "/v3/console/cs/config", data=form)


def delete_config(conn, namespace_id=None, data_id=None, group=None):
    """删除单个配置。返回 bool。"""
    params = {
        "dataId": data_id or "",
        "groupName": group or "",
    }
    if namespace_id and namespace_id != "public":
        params["namespaceId"] = namespace_id
    return _request(conn, "DELETE", "/v3/console/cs/config", params=params)


def get_config_history(conn, namespace_id=None, data_id=None, group=None, page_no=1, page_size=10):
    """查询配置历史版本。"""
    params = {
        "dataId": data_id or "",
        "groupName": group or "",
        "pageNo": page_no,
        "pageSize": page_size,
    }
    if namespace_id and namespace_id != "public":
        params["namespaceId"] = namespace_id
    return _request(conn, "GET", "/v3/console/cs/history/list", params=params)
