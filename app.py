import json
import sys
import threading
import time as _time
from flask import Flask, request, jsonify, Response, render_template, session, redirect, url_for
import config
import proxy
import mcp_server
from version import APP_VERSION, RELEASES_URL, GITHUB_LATEST_API

# Windows 控制台默认用 GBK 编码，直接 print 中文会乱码；强制 stdout/stderr 用 UTF-8。
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # 关闭命令行窗口（点右上角 X / 系统注销 / 关机）时，系统向所有附着进程发送
    # CTRL_CLOSE / CTRL_LOGOFF / CTRL_SHUTDOWN 事件。这不是 Unix signal，Python 的
    # signal 模块和 Werkzeug 的 SIGINT handler 都收不到；Werkzeug 的 socket.accept()
    # 阻塞在 C 层时也不主动响应，于是窗口关闭后 Python 进程残留、继续占用端口
    # （PyInstaller 打包的 console exe 尤为明显）。注册一个控制台 handler，在收到上述
    # 事件时 os._exit(0) 立即强制退出，保证"关闭窗口=进程退出"。
    # 注：系统对 CTRL_CLOSE 的处理时间只有 ~5 秒，超时会被强制 terminate，
    # 所以不能用 sys.exit()/raise SystemExit 等优雅退出路径（会跑 finally，可能卡住）。
    import ctypes as _ctypes
    import os as _os

    def _on_console_ctrl(ctrl_type):
        # CTRL_C_EVENT=0, CTRL_BREAK_EVENT=1, CTRL_CLOSE_EVENT=2,
        # CTRL_LOGOFF_EVENT=5, CTRL_SHUTDOWN_EVENT=6
        if ctrl_type in (2, 5, 6):
            _os._exit(0)
        return False  # CTRL_C/BREAK 交给默认 handler，保留 Ctrl+C 中断行为

    _HandlerRoutine = _ctypes.WINFUNCTYPE(_ctypes.c_int, _ctypes.c_uint)
    _console_ctrl_handler = _HandlerRoutine(_on_console_ctrl)
    # handler 对象必须模块级保活，否则 ctypes 回调对象被 GC 后 C 层函数指针悬空
    _ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_ctrl_handler, True)

app = Flask(__name__)
config.init_db()

# 持久化 session 密钥（数据库中生成一次，重启不丢失）
if not config.get_setting("secret_key"):
    import secrets as _secrets
    config.set_setting("secret_key", _secrets.token_hex(16))
app.secret_key = "ai-api-proxy-secret-" + config.get_setting("secret_key")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True


# ---- 认证中间件 ----

# 不需要登录就能访问的路由前缀
_PUBLIC_PREFIXES = ("/anthropic", "/v1", "/openai", "/mcp")
# 登录相关路由
_AUTH_ROUTES = ("/api/auth/login",)


@app.before_request
def check_auth():
    path = request.path

    # 静态资源放行
    if path.startswith("/static"):
        return None

    # 代理路由走 API Key 校验（不走 session）
    if any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return _check_api_key()

    # 登录页面和登录接口放行
    if path == "/login" or path in _AUTH_ROUTES:
        return None

    # 管理页面和API路由需要session认证
    if path == "/" or path.startswith("/api/"):
        if not session.get("admin_user"):
            # 如果是API请求返回401，否则重定向到登录页
            if path.startswith("/api/"):
                return jsonify({"error": "未登录"}), 401
            return redirect("/login")

    return None


def _check_api_key():
    """对代理路由进行API Key校验"""
    path = request.path

    # /mcp 路由：对所有 HTTP 方法强制校验（防御性，即便只暴露 POST）
    if path.startswith("/mcp"):
        api_key = request.headers.get("x-api-key", "")
        if not api_key:
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                api_key = auth[7:]
        if not config.validate_api_key(api_key):
            return jsonify({"error": "无效的 API Key"}), 401
        return None

    # 代理路由：非POST不需要API Key
    if request.method != "POST":
        return None

    # 从请求头提取API Key
    api_key = None

    if path.startswith("/anthropic"):
        # Anthropic: 优先 x-api-key，其次 Authorization: Bearer
        api_key = request.headers.get("x-api-key", "")
        if not api_key:
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                api_key = auth[7:]
        if not config.validate_api_key(api_key):
            return jsonify({"error": "无效的 API Key"}), 401
    elif path.startswith("/v1") or path.startswith("/openai"):
        # OpenAI Chat Completions (/v1) 与 Responses (/openai): Authorization: Bearer sk-xxx
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            api_key = auth[7:]
        if not config.validate_api_key(api_key):
            return jsonify({"error": "无效的 API Key"}), 401


# ---- Web UI ----

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/login")
def login_page():
    return render_template("login.html")


# ---- Auth API ----

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True)
    username = data.get("username", "")
    password = data.get("password", "")
    user = config.verify_admin_login(username, password)
    if user:
        session["admin_user"] = user
        return jsonify({"ok": True, "username": user["username"]})
    return jsonify({"error": "用户名或密码错误"}), 401


@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.pop("admin_user", None)
    return jsonify({"ok": True})


@app.route("/api/auth/status", methods=["GET"])
def api_auth_status():
    user = session.get("admin_user")
    if user:
        return jsonify({"logged_in": True, "username": user["username"]})
    return jsonify({"logged_in": False}), 401


# ---- API Keys ----

@app.route("/api/keys", methods=["GET"])
def api_get_keys():
    return jsonify(config.get_api_keys())


@app.route("/api/keys", methods=["POST"])
def api_add_key():
    data = request.get_json(force=True)
    key_name = data.get("key_name", "")
    if not key_name.strip():
        return jsonify({"error": "Key名称不能为空"}), 400
    result = config.add_api_key(key_name.strip())
    return jsonify(result), 201


@app.route("/api/keys/<int:key_id>", methods=["GET"])
def api_get_key(key_id):
    key = config.get_api_key_by_id(key_id)
    if key is None:
        return jsonify({"error": "Key not found"}), 404
    return jsonify(key)


@app.route("/api/keys/<int:key_id>", methods=["DELETE"])
def api_delete_key(key_id):
    config.delete_api_key(key_id)
    return jsonify({"ok": True})


@app.route("/api/keys/<int:key_id>/toggle", methods=["PUT"])
def api_toggle_key(key_id):
    data = request.get_json(force=True)
    config.toggle_api_key(key_id, data.get("enabled", True))
    return jsonify({"ok": True})


# ---- Anthropic Messages API 代理 ----

@app.route("/anthropic", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE"])
@app.route("/anthropic/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE"])
@app.route("/anthropic/<path:path>", methods=["GET", "POST", "PUT", "DELETE"])
def anthropic_proxy(path):
    if request.method != "POST":
        return jsonify({"status": "ok", "message": "Anthropic proxy endpoint"}), 200

    try:
        body = request.get_json(force=True)
    except Exception:
        return jsonify({"type": "error", "error": {"type": "invalid_request", "message": "无效的 JSON 请求"}}), 400

    body["_anthropic_version"] = request.headers.get("anthropic-version", "2023-06-01")
    body["_anthropic_path"] = path  # 透转子路径

    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    stream = body.get("stream", False)
    content, status_code, headers = proxy.handle_proxy_request(body, client_ip)

    # 流式响应时 content 是 generator；Flask Response 对 generator 和 bytes/str 都能正确处理，
    # 按类型统一传入即可，不再用 callable(content) 判断（生成器对象不可调用）。
    return Response(content, status=status_code, headers=headers)


# ---- OpenAI Chat Completions API 代理 ----

@app.route("/v1", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE"])
@app.route("/v1/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE"])
@app.route("/v1/<path:path>", methods=["GET", "POST", "PUT", "DELETE"])
def openai_proxy(path):
    if request.method != "POST":
        return jsonify({"status": "ok", "message": "OpenAI proxy endpoint"}), 200

    try:
        body = request.get_json(force=True)
    except Exception:
        return jsonify({"error": {"message": "无效的 JSON 请求", "type": "invalid_request"}}), 400

    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    stream = body.get("stream", False)
    content, status_code, headers = proxy.handle_openai_proxy_request(body, client_ip)

    # 流式响应时 content 是 generator；Flask Response 对 generator 和 bytes/str 都能正确处理，
    # 按类型统一传入即可，不再用 callable(content) 判断（生成器对象不可调用）。
    return Response(content, status=status_code, headers=headers)


# ---- OpenAI Responses API 代理 ----

@app.route("/openai/responses", methods=["POST"])
def openai_responses_proxy():
    """OpenAI Responses API 端点，内部转换为 Chat Completions 格式转发到 openai_url"""
    try:
        body = request.get_json(force=True)
    except Exception:
        return jsonify({"error": {"type": "invalid_request", "message": "无效的 JSON 请求"}}), 400

    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    stream = body.get("stream", False)
    content, status_code, headers = proxy.handle_openai_responses_request(body, client_ip)

    # 流式响应时 content 是 generator；Flask Response 对 generator 和 bytes/str 都能正确处理，
    # 按类型统一传入即可，不再用 callable(content) 判断（生成器对象不可调用）。
    return Response(content, status=status_code, headers=headers)


# ---- MCP Server 端点 ----

@app.route("/mcp", methods=["POST"])
def mcp_endpoint():
    """MCP JSON-RPC 2.0 端点。

    解析单条 JSON-RPC 请求并分发；批量请求（数组）返回 -32600 不支持；
    notification（无 id）返回 202 空体。
    """
    try:
        req = request.get_json(force=True)
    except Exception:
        return jsonify({
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "Parse error"},
        }), 400

    # 批量请求首版不支持
    if isinstance(req, list):
        return jsonify({
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request: 批量请求暂不支持"},
        }), 400

    # Nacos 连接参数由 MCP 客户端每次请求通过 HTTP headers 携带，服务端不落盘
    nacos_conn = {
        "console_url": (request.headers.get("X-Nacos-Console-Url") or "").strip(),
        "auth_url": (request.headers.get("X-Nacos-Auth-Url") or "").strip(),
        "username": (request.headers.get("X-Nacos-Username") or "").strip(),
        "password": (request.headers.get("X-Nacos-Password") or "").strip(),
    }
    resp = mcp_server.handle_jsonrpc(req, nacos_conn)
    if resp is None:
        # notification（notifications/initialized 等），无响应体
        return "", 202
    return jsonify(resp)

@app.route("/api/providers", methods=["GET"])
def api_get_providers():
    return jsonify(config.get_providers())


@app.route("/api/providers", methods=["POST"])
def api_add_provider():
    data = request.get_json(force=True)
    pid = config.add_provider(
        name=data["name"],
        anthropic_url=data.get("anthropic_url", ""),
        openai_url=data.get("openai_url", ""),
        api_key=data["api_key"],
        enabled=data.get("enabled", True),
        max_concurrency=data.get("max_concurrency", 0),
        full_path=int(data.get("full_path", 1)),
    )
    return jsonify({"id": pid}), 201


@app.route("/api/providers/<int:provider_id>", methods=["PUT"])
def api_update_provider(provider_id):
    data = request.get_json(force=True)
    config.update_provider(provider_id, **data)
    return jsonify({"ok": True})


@app.route("/api/providers/<int:provider_id>", methods=["DELETE"])
def api_delete_provider(provider_id):
    config.delete_provider(provider_id)
    return jsonify({"ok": True})


# ---- Model Mappings API ----

@app.route("/api/models", methods=["GET"])
def api_get_models():
    return jsonify(config.get_model_mappings())


@app.route("/api/models", methods=["POST"])
def api_add_model():
    data = request.get_json(force=True)
    mid = config.add_model_mapping(
        alias=data["alias"],
        target_model=data["target_model"],
        provider_id=data["provider_id"],
        enabled=data.get("enabled", True),
        priority=data.get("priority", 1),
        model_type=data.get("model_type", "text"),
        max_tokens=data.get("max_tokens", 0),
        role_mappings=data.get("role_mappings", "[]"),
        think_injection=data.get("think_injection", 0),
        reasoning_content_field=data.get("reasoning_content_field", 1),
        native_responses=data.get("native_responses", 0),
    )
    return jsonify({"id": mid}), 201


@app.route("/api/models/<int:mapping_id>", methods=["PUT"])
def api_update_model(mapping_id):
    data = request.get_json(force=True)
    try:
        config.update_model_mapping(mapping_id, **data)
    except ValueError as e:
        # 业务校验失败（如 provider 禁用时尝试开启 mapping），返回 400 + 错误信息
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/models/<int:mapping_id>", methods=["DELETE"])
def api_delete_model(mapping_id):
    config.delete_model_mapping(mapping_id)
    return jsonify({"ok": True})


@app.route("/api/models/degradation", methods=["GET"])
def api_get_models_degradation():
    return jsonify(proxy.get_degradation_status())


# ---- Logs API ----

@app.route("/api/logs", methods=["GET"])
def api_get_logs():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    status_filter = request.args.get("status", "")
    model_filter = request.args.get("model", "")
    ip_filter = request.args.get("ip", "")
    provider_filter = request.args.get("provider", "")
    return jsonify(config.get_logs(page, per_page, status_filter or None, model_filter or None, ip_filter or None, provider_filter or None))


@app.route("/api/logs/providers", methods=["GET"])
def api_log_providers():
    return jsonify(config.get_distinct_providers())


@app.route("/api/logs/<int:log_id>", methods=["GET"])
def api_get_log(log_id):
    """单条日志详情（含 request_body/response_body/error_msg），供详情弹窗按需拉取。"""
    row = config.get_log(log_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(row)


@app.route("/api/logs", methods=["DELETE"])
def api_clear_logs():
    config.clear_logs()
    return jsonify({"ok": True})


# ---- Error Mappings API ----

@app.route("/api/error-mappings", methods=["GET"])
def api_get_error_mappings():
    return jsonify(config.get_error_mappings())


@app.route("/api/error-mappings", methods=["POST"])
def api_add_error_mapping():
    data = request.get_json(force=True)
    mid = config.add_error_mapping(
        provider=data.get("provider", ""),
        original_code=data["original_code"],
        mapped_code=data["mapped_code"],
        enabled=data.get("enabled", True),
    )
    return jsonify({"id": mid}), 201


@app.route("/api/error-mappings/<int:mapping_id>", methods=["PUT"])
def api_update_error_mapping(mapping_id):
    data = request.get_json(force=True)
    config.update_error_mapping(mapping_id, **data)
    return jsonify({"ok": True})


@app.route("/api/error-mappings/<int:mapping_id>", methods=["DELETE"])
def api_delete_error_mapping(mapping_id):
    config.delete_error_mapping(mapping_id)
    return jsonify({"ok": True})


# ---- Settings API ----

@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    return jsonify(config.get_all_settings(mask_secrets=True))


@app.route("/api/settings", methods=["PUT"])
def api_update_settings():
    data = request.get_json(force=True)
    for k, v in data.items():
        config.set_setting(k, v)
    return jsonify({"ok": True})


# ---- Version / Update Check ----

# 版本检查请求串行化，避免多 tab 并发重复打 GitHub API
_version_lock = threading.Lock()
_VERSION_CACHE_TTL = 3600        # 正常缓存 1 小时
_VERSION_RATELIMIT_TTL = 86400   # 命中 403 限流时缓存 24 小时，避免反复撞墙


def _parse_version(tag):
    """'v1.2.2' 或 '1.2.2' -> (1, 2, 2)；非法返回空元组 ()"""
    if not tag:
        return ()
    s = str(tag).strip().lstrip("vV")
    parts = []
    for p in s.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            return ()
    return tuple(parts)


def _fetch_latest_release():
    """请求 GitHub /releases/latest，3s 超时。

    返回：
      - (version_str, html_url)：成功，version_str 已去掉 v 前缀
      - ("__rate_limited__", None)：命中 403 限流
      - None：网络错误 / 非 200 / 解析失败
    """
    import requests
    try:
        r = requests.get(
            GITHUB_LATEST_API,
            timeout=3,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "vibe-coding-proxy",
            },
        )
    except Exception:
        return None
    if r.status_code == 403:
        return ("__rate_limited__", None)
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    tag = data.get("tag_name", "")
    url = data.get("html_url", RELEASES_URL)
    v = _parse_version(tag)
    return (tag.lstrip("vV"), url) if v else None


@app.route("/api/version", methods=["GET"])
def api_get_version():
    """返回当前版本 + 最新版本对比结果。

    命中 settings 缓存优先（1h TTL，限流时 24h），缓存过期才拉 GitHub。
    force=1 跳过缓存立即重查。GitHub 不通时返回旧缓存 + has_update=null，
    前端降级只显示当前版本号，永卡不了 UI。
    """
    import time as _t
    now = _t.time()
    force = request.args.get("force") == "1"

    cached_at = float(config.get_setting("latest_release_checked_at", "0") or 0)
    cached_ver = config.get_setting("latest_release_version", "")
    cached_url = config.get_setting("latest_release_url", "")
    cached_rl = config.get_setting("latest_release_rate_limited", "0") == "1"

    ttl = _VERSION_RATELIMIT_TTL if cached_rl else _VERSION_CACHE_TTL
    fresh = (not force) and bool(cached_at) and ((now - cached_at) < ttl)

    latest = cached_ver
    latest_url = cached_url or RELEASES_URL
    checked_at = cached_at

    if not fresh:
        with _version_lock:
            # double-check：拿到锁后缓存可能已被其他并发请求刷新
            cached_at2 = float(config.get_setting("latest_release_checked_at", "0") or 0)
            cached_rl2 = config.get_setting("latest_release_rate_limited", "0") == "1"
            ttl2 = _VERSION_RATELIMIT_TTL if cached_rl2 else _VERSION_CACHE_TTL
            if (not force) and cached_at2 and ((now - cached_at2) < ttl2) and cached_at2 > cached_at:
                latest = config.get_setting("latest_release_version", "")
                latest_url = config.get_setting("latest_release_url", "") or RELEASES_URL
                checked_at = cached_at2
            else:
                res = _fetch_latest_release()
                if res is None:
                    pass  # 网络失败：保留旧缓存值，has_update 用旧值算
                elif res[0] == "__rate_limited__":
                    config.set_setting("latest_release_checked_at", str(now))
                    config.set_setting("latest_release_rate_limited", "1")
                    checked_at = now
                else:
                    latest, latest_url = res
                    config.set_setting("latest_release_version", latest)
                    config.set_setting("latest_release_url", latest_url)
                    config.set_setting("latest_release_checked_at", str(now))
                    config.set_setting("latest_release_rate_limited", "0")
                    checked_at = now

    cur, lat = _parse_version(APP_VERSION), _parse_version(latest)
    if latest and cur and lat:
        has_update = lat > cur
    else:
        has_update = None

    return jsonify({
        "current": APP_VERSION,
        "latest": latest or None,
        "latest_url": latest_url,
        "has_update": has_update,        # True=有新版 / False=已是最新 / None=未知（没网或无缓存）
        "checked_at": checked_at or None,
        "releases_url": RELEASES_URL,
    })


@app.route("/api/logs/stats", methods=["GET"])
def api_log_stats():
    return jsonify(config.get_log_stats())


@app.route("/api/providers/stats", methods=["GET"])
def api_provider_stats():
    return jsonify(config.get_provider_stats())


# ---- Billing API ----

_VALID_BILLING_MODES = ("request_count", "token_count", "balance")


@app.route("/api/providers/<int:provider_id>/billing", methods=["GET"])
def api_get_billing(provider_id):
    cfg = config.get_billing_config(provider_id)
    if not cfg:
        return jsonify({"error": "未找到计费配置"}), 404
    return jsonify(cfg)


def _validate_billing_data(data):
    """Validate and sanitize billing config data, returns (cleaned_data, error_msg)"""
    billing_mode = data.get("billing_mode", "request_count")
    if billing_mode not in _VALID_BILLING_MODES:
        return None, f"无效的计费模式，可选值: {', '.join(_VALID_BILLING_MODES)}"

    # Validate limits are non-negative integers or null
    for key in ("limit_5h", "limit_week", "limit_month"):
        val = data.get(key)
        if val is not None:
            try:
                val = int(val)
                if val < 0:
                    return None, f"{key} 不能为负数"
            except (ValueError, TypeError):
                return None, f"{key} 必须为整数"
            data[key] = val

    # Validate balance and prices are non-negative
    for key in ("balance", "input_price_per_million", "output_price_per_million", "cache_read_price_per_million"):
        val = data.get(key, 0)
        try:
            val = float(val)
            if val < 0:
                return None, f"{key} 不能为负数"
        except (ValueError, TypeError):
            return None, f"{key} 必须为数字"
        data[key] = val

    # Validate warning_threshold
    threshold = data.get("warning_threshold", 0.8)
    try:
        threshold = float(threshold)
        if not (0 < threshold <= 1):
            return None, "告警阈值必须在 0-1 之间"
    except (ValueError, TypeError):
        return None, "告警阈值必须为数字"
    data["warning_threshold"] = threshold

    return data, None


@app.route("/api/providers/<int:provider_id>/billing", methods=["POST"])
def api_create_billing(provider_id):
    if not config.get_provider(provider_id):
        return jsonify({"error": "提供商不存在"}), 404
    if config.get_billing_config(provider_id):
        return jsonify({"error": "计费配置已存在，请使用 PUT 更新"}), 409
    data = request.get_json(force=True)
    data, err = _validate_billing_data(data)
    if err:
        return jsonify({"error": err}), 400
    config.save_billing_config(
        provider_id=provider_id,
        billing_mode=data.get("billing_mode", "request_count"),
        limit_5h=data.get("limit_5h"),
        limit_week=data.get("limit_week"),
        limit_month=data.get("limit_month"),
        balance=data.get("balance", 0),
        input_price_per_million=data.get("input_price_per_million", 0),
        output_price_per_million=data.get("output_price_per_million", 0),
        cache_read_price_per_million=data.get("cache_read_price_per_million", 0),
        expiration_date=data.get("expiration_date"),
        warning_threshold=data.get("warning_threshold", 0.8),
    )
    return jsonify({"ok": True}), 201


@app.route("/api/providers/<int:provider_id>/billing", methods=["PUT"])
def api_update_billing(provider_id):
    existing = config.get_billing_config(provider_id)
    if not existing:
        return jsonify({"error": "计费配置不存在，请使用 POST 创建"}), 404
    raw_data = request.get_json(force=True)
    data, err = _validate_billing_data(raw_data)
    if err:
        return jsonify({"error": err}), 400
    # For balance mode: preserve current balance if not explicitly provided
    # This prevents accidentally overwriting balance decremented by usage
    if data.get("billing_mode") == "balance" and "balance" not in raw_data:
        data["balance"] = existing["balance"]
    config.save_billing_config(
        provider_id=provider_id,
        billing_mode=data.get("billing_mode", "request_count"),
        limit_5h=data.get("limit_5h"),
        limit_week=data.get("limit_week"),
        limit_month=data.get("limit_month"),
        balance=data.get("balance", 0),
        input_price_per_million=data.get("input_price_per_million", 0),
        output_price_per_million=data.get("output_price_per_million", 0),
        cache_read_price_per_million=data.get("cache_read_price_per_million", 0),
        expiration_date=data.get("expiration_date"),
        warning_threshold=data.get("warning_threshold", 0.8),
    )
    return jsonify({"ok": True})


@app.route("/api/providers/<int:provider_id>/billing", methods=["DELETE"])
def api_delete_billing(provider_id):
    config.delete_billing_config(provider_id)
    return jsonify({"ok": True})


@app.route("/api/providers/<int:provider_id>/usage", methods=["GET"])
def api_get_usage(provider_id):
    usages = config.get_provider_usage(provider_id)
    billing_check = config.check_provider_billing(provider_id)
    return jsonify({
        "usages": usages,
        "billing_status": billing_check,
    })


@app.route("/api/providers/billing/overview", methods=["GET"])
def api_billing_overview():
    return jsonify(config.get_all_billing_overview())


@app.route("/api/concurrency", methods=["GET"])
def api_concurrency_status():
    return jsonify(proxy.get_concurrency_status())


# ---- 后台自动清理线程 ----

_last_vacuum_date = None  # 记录上次 VACUUM 的日期，用于每天执行一次


def _cleanup_loop():
    """后台线程：按配置间隔自动清理过期日志，并管理计费窗口"""
    global _last_vacuum_date
    while True:
        try:
            settings = config.get_all_settings()
            enabled = settings.get("auto_cleanup_enabled", "0") == "1"
            interval_hours = int(settings.get("cleanup_interval_hours", "1") or "1")
            retention_days = int(settings.get("cleanup_retention_days", "7") or "7")

            if enabled:
                deleted = config.cleanup_old_logs(retention_days)
                config.set_setting("last_cleanup_time", _time.strftime("%Y-%m-%d %H:%M:%S"))
                if deleted > 0:
                    print(f"[自动清理] 删除了 {deleted} 条超过 {retention_days} 天的日志")
        except Exception as e:
            print(f"[自动清理] 错误: {e}")

        # 计费窗口管理（每次循环都执行）
        try:
            expired_count, reenabled = config.reset_expired_windows_and_reenable()
            if expired_count > 0:
                print(f"[计费] 重置了 {expired_count} 个过期使用窗口")
            if reenabled:
                print(f"[计费] 自动重新启用了提供商: {', '.join(reenabled)}")

            disabled = config.auto_disable_over_limit_providers()
            if disabled:
                print(f"[计费] 自动禁用了超限提供商: {', '.join(disabled)}")
        except Exception as e:
            print(f"[计费] 错误: {e}")

        # 每天执行一次 VACUUM 压缩数据库（跨天时触发）
        try:
            today = _time.strftime("%Y-%m-%d")
            if _last_vacuum_date != today:
                _last_vacuum_date = today
                config.vacuum_db()
        except Exception as e:
            print(f"[VACUUM] 错误: {e}")

        _time.sleep(interval_hours * 3600)


# 启动后台清理线程（daemon 模式，主进程退出时自动结束）
_cleanup_thread = threading.Thread(target=_cleanup_loop, daemon=True)
_cleanup_thread.start()

# 启动时在后台执行一次 VACUUM（异步，避免阻塞服务启动）
def _startup_vacuum():
    global _last_vacuum_date
    try:
        config.vacuum_db()
        _last_vacuum_date = _time.strftime("%Y-%m-%d")
    except Exception as e:
        print(f"[VACUUM] 启动压缩错误: {e}")

_startup_vacuum_thread = threading.Thread(target=_startup_vacuum, daemon=True)
_startup_vacuum_thread.start()


if __name__ == "__main__":
    server_port = config.get_server_port()
    print("=" * 50)
    print("  Vibe Coding 服务转发 已启动")
    print(f"  Web 管理界面:  http://localhost:{server_port}")
    print(f"  Anthropic 代理: http://localhost:{server_port}/anthropic")
    print(f"  OpenAI 代理:    http://localhost:{server_port}/v1")
    print(f"  Responses 代理: http://localhost:{server_port}/openai")
    print(f"  MCP / Nacos:    http://localhost:{server_port}/mcp")
    print("=" * 50)
    # 打包后必须关闭 debug/reloader：reloader 会 fork 子进程，但 PyInstaller 打包后
    # 子进程无法找到原入口文件，会立即挂掉。
    app.run(host="0.0.0.0", port=server_port, debug=False, use_reloader=False)
