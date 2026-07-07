import json
import secrets
import threading
import time as _time
import urllib.parse
from flask import Flask, request, jsonify, Response, render_template, session, redirect, url_for
import config
import proxy
import oauth

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
_PUBLIC_PREFIXES = ("/anthropic", "/v1", "/openai", "/oauth", "/.well-known")
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

    # 代理路由：非POST不需要API Key
    # MCP 路由：所有请求（包括 GET SSE）都需要 API Key
    if not path.startswith("/mcp") and request.method != "POST":
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
    elif path.startswith("/mcp"):
        # MCP: 优先 OAuth Bearer token，其次 API Key
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
            # 尝试作为 OAuth access_token 验证
            payload = oauth.validate_access_token(token)
            if payload:
                return None
            # 否则尝试作为 API Key 验证
            if config.validate_api_key(token):
                return None
            # 两者都失败，返回带 WWW-Authenticate 头的 401
            base = request.host_url.rstrip("/")
            return Response(
                json.dumps({"error": {"message": "无效的认证凭证", "type": "authentication_error", "code": "invalid_token"}}),
                status=401,
                mimetype="application/json",
                headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource/mcp", scope="mcp:read mcp:write"'
                }
            )
        # 无 Bearer token，尝试 x-api-key
        api_key = request.headers.get("x-api-key", "")
        if api_key and config.validate_api_key(api_key):
            return None
        return Response(
            json.dumps({"error": {"message": "需要认证", "type": "authentication_error", "code": "missing_token"}}),
            status=401,
            mimetype="application/json",
            headers={
                "WWW-Authenticate": f'Bearer resource_metadata="{request.host_url.rstrip("/")}/.well-known/oauth-protected-resource/mcp", scope="mcp:read mcp:write"'
            }
        )


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
        # 检查是否有待完成的 OAuth 授权请求
        oauth_next = session.pop("oauth_next", None)
        if oauth_next:
            # 从 session 恢复 OAuth 参数并重定向到授权端点
            params = [("response_type", "code"), ("client_id", oauth_next.get("client_id", "")),
                      ("redirect_uri", oauth_next.get("redirect_uri", "")),
                      ("code_challenge", oauth_next.get("code_challenge", "")),
                      ("code_challenge_method", oauth_next.get("code_challenge_method", "S256")),
                      ("state", oauth_next.get("state", "")),
                      ("resource", oauth_next.get("resource", "")),
                      ("scope", oauth_next.get("scope", ""))]
            qs = urllib.parse.urlencode({k: v for k, v in params if v})
            return redirect("/oauth/authorize?" + qs)
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


# ---- Providers API ----

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
        group_name=data.get("group_name", ""),
        priority=data.get("priority", 1),
        model_type=data.get("model_type", "text"),
        max_tokens=data.get("max_tokens", 0),
    )
    return jsonify({"id": mid}), 201


@app.route("/api/models/<int:mapping_id>", methods=["PUT"])
def api_update_model(mapping_id):
    data = request.get_json(force=True)
    config.update_model_mapping(mapping_id, **data)
    return jsonify({"ok": True})


@app.route("/api/models/<int:mapping_id>", methods=["DELETE"])
def api_delete_model(mapping_id):
    config.delete_model_mapping(mapping_id)
    return jsonify({"ok": True})


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
    return jsonify(config.get_all_settings())


@app.route("/api/settings", methods=["PUT"])
def api_update_settings():
    data = request.get_json(force=True)
    for k, v in data.items():
        config.set_setting(k, v)
    return jsonify({"ok": True})


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
    for key in ("balance", "input_price_per_million", "output_price_per_million"):
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


# ---- OAuth 2.1 端点 (RFC 8414 / RFC 9728) ----

@app.route("/.well-known/oauth-protected-resource", methods=["GET"], defaults={"path": ""})
@app.route("/.well-known/oauth-protected-resource/<path:path>", methods=["GET"])
def oauth_protected_resource(path):
    """Protected Resource Metadata (RFC 9728)"""
    resource_path = "/" + path if path else "/"
    return jsonify(oauth.get_oauth_protected_resource_metadata(resource_path))


@app.route("/.well-known/oauth-authorization-server", methods=["GET"])
def oauth_authorization_server_metadata():
    """Authorization Server Metadata (RFC 8414)"""
    return jsonify(oauth.get_oauth_authorization_server_metadata())


@app.route("/.well-known/jwks.json", methods=["GET"])
def oauth_jwks():
    """JWKS 端点 (空实现，alg=none 不需要密钥)"""
    return jsonify({"keys": []})


# ---- OAuth 动态客户端注册 (RFC 7591) ----

@app.route("/oauth/register", methods=["POST"])
def oauth_register():
    """动态注册 OAuth 客户端"""
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "invalid_request", "error_description": "无效的 JSON"}), 400

    client_name = data.get("client_name", "Claude Code")
    redirect_uris = data.get("redirect_uris", [])
    grant_types = data.get("grant_types", ["authorization_code", "refresh_token"])
    response_types = data.get("response_types", ["code"])
    token_endpoint_auth_method = data.get("token_endpoint_auth_method", "none")
    application_type = data.get("application_type", "native")

    if not redirect_uris:
        return jsonify({"error": "invalid_request", "error_description": "redirect_uris 是必填项"}), 400

    # 检查是否支持 PKCE (必须 S256)
    if token_endpoint_auth_method == "none":
        # 允许 public client
        pass

    client_id, client_secret = oauth.register_oauth_client(
        application_type=application_type,
        client_name=client_name,
        redirect_uris=redirect_uris,
        grant_types=grant_types,
        response_types=response_types,
        token_endpoint_auth_method=token_endpoint_auth_method,
    )

    resp = {
        "client_id": client_id,
        "client_id_issued_at": int(_time.time()),
        "grant_types": grant_types,
        "redirect_uris": redirect_uris,
        "response_types": response_types,
    }
    if client_secret:
        resp["client_secret"] = client_secret
    return jsonify(resp), 201


# ---- OAuth 授权端点 ----

@app.route("/oauth/authorize", methods=["GET", "POST"])
def oauth_authorize():
    """OAuth 2.1 授权端点 (Authorization Code + PKCE)"""
    # 如果未登录，重定向到登录页面
    if not session.get("admin_user"):
        # 保存原始请求参数到 session
        session["oauth_next"] = {
            "response_type": request.args.get("response_type", "code"),
            "client_id": request.args.get("client_id", ""),
            "redirect_uri": request.args.get("redirect_uri", ""),
            "code_challenge": request.args.get("code_challenge", ""),
            "code_challenge_method": request.args.get("code_challenge_method", ""),
            "state": request.args.get("state", ""),
            "resource": request.args.get("resource", ""),
            "scope": request.args.get("scope", ""),
        }
        return redirect("/login?next=" + request.path)

    if request.method == "GET":
        # 显示授权确认页面（简化处理：自动批准）
        client_id = request.args.get("client_id", "")
        redirect_uri = request.args.get("redirect_uri", "")
        scope = request.args.get("scope", "")
        state = request.args.get("state", "")
        resource = request.args.get("resource", "")
        code_challenge = request.args.get("code_challenge", "")

        # 简化处理：自动授权（用户已登录即视为已授权）
        code = secrets.token_urlsafe(32)
        oauth.create_authorization_code(
            client_id=client_id,
            code=code,
            redirect_uri=redirect_uri,
            scope=scope,
            code_challenge=code_challenge,
            user_id=1,
            resource=resource,
        )
        params = {"code": code}
        if state:
            params["state"] = state
        if redirect_uri:
            return redirect(redirect_uri + "?" + urllib.parse.urlencode(params))
        else:
            return jsonify({"code": code, "state": state})

    # POST: 处理表单提交（授权确认）
    data = request.form
    client_id = data.get("client_id", "")
    redirect_uri = data.get("redirect_uri", "")
    scope = data.get("scope", "")
    state = data.get("state", "")
    resource = data.get("resource", "")
    code_challenge = data.get("code_challenge", "")

    # 简化处理：自动授权
    code = secrets.token_urlsafe(32)
    oauth.create_authorization_code(
        client_id=client_id,
        code=code,
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=code_challenge,
        user_id=1,
        resource=resource,
    )

    params = {"code": code}
    if state:
        params["state"] = state
    if redirect_uri:
        return redirect(redirect_uri + "?" + urllib.parse.urlencode(params))
    else:
        return jsonify({"code": code, "state": state})


# ---- OAuth 令牌端点 ----

@app.route("/oauth/token", methods=["POST"])
def oauth_token():
    """OAuth 2.1 令牌端点"""
    data = request.form if request.form else request.get_json(force=True)
    grant_type = data.get("grant_type", "")

    client_id = data.get("client_id", "")
    redirect_uri = data.get("redirect_uri", "")
    resource = data.get("resource", "")

    if grant_type == "authorization_code":
        code = data.get("code", "")
        code_verifier = data.get("code_verifier", "")

        # 验证授权码
        code_info = oauth.consume_authorization_code(code, client_id)
        if not code_info:
            return jsonify({"error": "invalid_grant", "error_description": "授权码无效或已过期"}), 400

        # 验证 PKCE: code_verifier 的 SHA256 摘要应与存储的 code_challenge 匹配
        saved_challenge = code_info.get("code_verifier", "")
        if saved_challenge and oauth.generate_code_challenge(code_verifier) != saved_challenge:
            return jsonify({"error": "invalid_grant", "error_description": "PKCE 验证失败"}), 400

        # 创建访问令牌
        access_token, refresh_token = oauth.create_token(
            client_id=client_id,
            scope=code_info.get("scope", ""),
            user_id=code_info.get("user_id"),
        )
        return jsonify({
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": refresh_token,
            "scope": code_info.get("scope", ""),
        })

    elif grant_type == "refresh_token":
        refresh_token_val = data.get("refresh_token", "")
        if not refresh_token_val:
            return jsonify({"error": "invalid_request", "error_description": "缺少 refresh_token"}), 400

        result = oauth.refresh_access_token(refresh_token_val, client_id)
        if not result:
            return jsonify({"error": "invalid_grant", "error_description": "refresh_token 无效或已过期"}), 400

        access_token, new_refresh_token = result
        return jsonify({
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": new_refresh_token,
        })

    else:
        return jsonify({"error": "unsupported_grant_type", "error_description": f"不支持的 grant_type: {grant_type}"}), 400


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
    print("=" * 50)
    print("  Vibe Coding 服务转发 已启动")
    print("  Web 管理界面:  http://localhost:5000")
    print("  Anthropic 代理: http://localhost:5000/anthropic")
    print("  OpenAI 代理:    http://localhost:5000/v1")
    print("  Responses 代理: http://localhost:5000/openai/responses")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=True)
