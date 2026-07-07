import hashlib
import json
import re
import time
import random
import threading
import requests as http_requests
from requests.exceptions import ConnectionError as _ReqConnError, ChunkedEncodingError, Timeout as _ReqTimeout
import config

# 连接级异常：上游断开或客户端断开，属于正常中断，不按业务错误处理
_CONN_ABORT_ERRORS = (_ReqConnError, ChunkedEncodingError, _ReqTimeout, ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError)

# 重试策略配置：所有请求错误都参与重试，但有耗时与次数上限
_RETRY_MAX_ATTEMPTS = 3        # 最多重试次数（首次失败后最多再重试 3 次，总请求数 ≤ 4）
_RETRY_MAX_DURATION = 5.0      # 单次请求耗时上限（秒）：超过则不再重试，避免对已处理很久的错误做无谓重试
_RETRY_DELAY = 1.0             # 重试间隔（秒）


def _post_with_retry(url, max_retries=_RETRY_MAX_ATTEMPTS, retry_delay=_RETRY_DELAY, retry_max_duration=_RETRY_MAX_DURATION, **kwargs):
    """发起 HTTP POST 请求，遇到任何错误时按策略自动重试。

    重试触发条件（需同时满足）：
      1. 请求出错：连接级异常（DNS 失败 / 连接被上游中止 / 握手超时 / ChunkedEncodingError 等）
                  或 HTTP 4xx/5xx 错误码；
      2. 单次请求耗时 < retry_max_duration 秒（上游已处理很久的错误重试代价高，不再重试）；
      3. 未达到最大重试次数 max_retries。

    该函数内部循环重试，仅返回最终结果（成功响应或最后一次的失败响应/异常），
    调用方在外层据此只记录一次日志——天然满足"只记录最后一次请求日志"。
    """
    last_err = None
    for attempt in range(max_retries + 1):
        start = time.time()
        try:
            resp = http_requests.post(url, **kwargs)
            elapsed = time.time() - start
            # HTTP 4xx/5xx：仅在耗时较短且仍有重试机会时关闭并重试
            if resp.status_code >= 400 and elapsed < retry_max_duration and attempt < max_retries:
                resp.close()
                time.sleep(retry_delay)
                continue
            return resp
        except _CONN_ABORT_ERRORS as e:
            last_err = e
            elapsed = time.time() - start
            if elapsed < retry_max_duration and attempt < max_retries:
                time.sleep(retry_delay)
                continue
            raise last_err
    # 理论不可达：最后一次迭代必然走 return 或 raise 分支
    raise last_err if last_err else RuntimeError("_post_with_retry reached unreachable state")



_IMAGE_PLACEHOLDER = "[图片内容已省略，当前模型不支持多模态输入]"


def _has_images_anthropic(body):
    """检测 Anthropic 格式请求体中是否包含图片"""
    for msg in body.get("messages", []):
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "image":
                        return True
                    if block.get("type") == "tool_result":
                        tool_content = block.get("content")
                        if isinstance(tool_content, list):
                            for c in tool_content:
                                if isinstance(c, dict) and c.get("type") == "image":
                                    return True
    return False


def _has_images_openai(body):
    """检测 OpenAI 格式请求体中是否包含图片"""
    for msg in body.get("messages", []):
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False


def _parse_sse_data(line):
    """从 SSE 行中提取 data 后的 JSON 字符串。

    兼容两种格式：
    - 标准 SSE：'data: {"key": "val"}'  （冒号后有空格）
    - dashscope：'data:{"key": "val"}'  （冒号后无空格）
    """
    data_str = line[5:]  # 去掉 "data:"
    if data_str.startswith(" "):
        data_str = data_str[1:]  # 去掉可选的空格
    return data_str


def _strip_images_anthropic(body):
    """将 Anthropic 格式请求体中的图片内容替换为文本提示"""
    for msg in body.get("messages", []):
        content = msg.get("content")
        if isinstance(content, list):
            new_content = []
            for block in content:
                if block.get("type") == "image":
                    new_content.append({"type": "text", "text": _IMAGE_PLACEHOLDER})
                elif block.get("type") == "tool_result":
                    # tool_result 的 content 中也可能包含图片
                    tool_content = block.get("content")
                    if isinstance(tool_content, list):
                        new_tool_content = []
                        for c in tool_content:
                            if c.get("type") == "image":
                                new_tool_content.append({"type": "text", "text": _IMAGE_PLACEHOLDER})
                            else:
                                new_tool_content.append(c)
                        block = dict(block, content=new_tool_content)
                    new_content.append(block)
                else:
                    new_content.append(block)
            msg["content"] = new_content


def _strip_images_openai(body):
    """将 OpenAI 格式请求体中的图片内容替换为文本提示"""
    for msg in body.get("messages", []):
        content = msg.get("content")
        if isinstance(content, list):
            new_content = []
            for part in content:
                if part.get("type") == "image_url":
                    new_content.append({"type": "text", "text": _IMAGE_PLACEHOLDER})
                else:
                    new_content.append(part)
            msg["content"] = new_content


def _track_usage(provider_id, input_tokens, output_tokens, cache_read_input_tokens=0, cache_creation_input_tokens=0):
    """Record billing usage. Failure must not affect the response."""
    try:
        config.increment_provider_usage(provider_id, input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens)
    except Exception:
        pass


# ---- 并发控制 ----
# {provider_id: threading.Semaphore}
_concurrency_semaphores = {}
# {provider_id: initial_capacity} 记录信号量创建时的容量，避免用 _value 误判
_semaphore_capacities = {}
_concurrency_lock = threading.Lock()

# {provider_id: active_count} 记录每个提供商的活跃请求数（包含无限制提供商）
_active_requests = {}
_active_lock = threading.Lock()


# ---- 按客户端隔离的加权轮询 ----
# {(client_ip, group_name): round_robin_index}
_rr_index = {}
_rr_lock = threading.Lock()


def _get_semaphore(provider_id, max_concurrency):
    """获取或创建提供商的并发信号量"""
    if max_concurrency <= 0:
        return None  # 不限制
    with _concurrency_lock:
        sem = _concurrency_semaphores.get(provider_id)
        # 用记录的初始容量比较，而不是 sem._value（剩余槽位会随 acquire 变化）
        current_cap = _semaphore_capacities.get(provider_id, 0)
        if sem is None or current_cap != max_concurrency:
            sem = threading.Semaphore(max_concurrency)
            _concurrency_semaphores[provider_id] = sem
            _semaphore_capacities[provider_id] = max_concurrency
        return sem


def _track_request_start(provider_id):
    """记录请求开始（递增活跃请求计数）"""
    with _active_lock:
        _active_requests[provider_id] = _active_requests.get(provider_id, 0) + 1


def _track_request_end(provider_id):
    """记录请求结束（递减活跃请求计数）"""
    with _active_lock:
        count = _active_requests.get(provider_id, 0)
        if count > 0:
            _active_requests[provider_id] = count - 1
        else:
            _active_requests[provider_id] = 0


def _release_concurrency(provider_id, sem=None):
    """释放并发资源：减少信号量槽位 + 递减活跃请求计数"""
    if sem:
        sem.release()
    _track_request_end(provider_id)


def get_concurrency_status():
    """获取所有提供商的并发状态"""
    result = {}
    providers = config.get_providers()
    with _active_lock:
        active = dict(_active_requests)
    for p in providers:
        pid = p["id"]
        max_c = p.get("max_concurrency", 0)
        used = active.get(pid, 0)
        result[pid] = {"used": used, "max": max_c, "waiting": 0}
    return result


def _pick_least_concurrent(candidates):
    """从候选项中选并发使用量最小的，返回单个 mapping dict"""
    best = None
    best_used = float("inf")
    conc = get_concurrency_status()
    for m in candidates:
        pid = m["provider_id"]
        max_c = m.get("provider_max_concurrency", 0)
        if max_c <= 0:
            # 不限制并发的优先级最高
            return m
        c = conc.get(pid, {"used": 0, "max": max_c})
        used = c["used"]
        if used < best_used:
            best_used = used
            best = m
    return best or candidates[0]


def _build_weighted_sequence(candidates):
    """根据优先级构建加权序列（打乱后返回），高优先级模型出现次数更多"""
    if not candidates:
        return []
    sorted_c = sorted(candidates, key=lambda m: m.get("priority", 1))
    max_p = max(m.get("priority", 1) for m in sorted_c)
    sequence = []
    for m in sorted_c:
        p = m.get("priority", 1)
        weight = max(1, max_p - p + 1)
        sequence.extend([m] * weight)
    # Fisher-Yates 打乱，避免连续命中同一模型
    for i in range(len(sequence) - 1, 0, -1):
        j = random.randint(0, i)
        sequence[i], sequence[j] = sequence[j], sequence[i]
    return sequence


def _pick_weighted_round_robin(candidates, client_ip, group_name):
    """按客户端 IP 隔离的加权轮询选择"""
    sequence = _build_weighted_sequence(candidates)
    if not sequence:
        return candidates[0]
    # 如果序列长度为 1（只有一个模型），直接返回
    if len(sequence) == 1:
        return sequence[0]
    key = (client_ip, group_name)
    with _rr_lock:
        idx = _rr_index.get(key, -1)
        idx = (idx + 1) % len(sequence)
        _rr_index[key] = idx
        return sequence[idx]


def handle_proxy_request(request_body, client_ip=""):
    """处理 Anthropic Messages API 代理请求，返回 (response_generator, status_code, headers)"""
    model = request_body.get("model", "")
    stream = request_body.get("stream", False)

    # 查找模型映射
    mapping = config.get_model_mapping_by_alias(model)
    if not mapping:
        # 没有映射，尝试找默认的 anthropic provider 直接转发
        providers = config.get_providers()
        anthropic_providers = [p for p in providers if p["enabled"] and p.get("anthropic_url", "")]
        if anthropic_providers:
            # 与 OpenAI fallback 保持一致：显式构造 provider dict，
            # 避免依赖 get_providers() 的返回字段集合（未来裁剪列时 full_path 不致丢失）
            p0 = anthropic_providers[0]
            provider = {
                "id": p0["id"],
                "name": p0["name"],
                "anthropic_url": p0["anthropic_url"],
                "api_key": p0["api_key"],
                "max_concurrency": p0.get("max_concurrency", 0),
                "full_path": p0.get("full_path", 1),
            }
            target_model = model
        else:
            return _error_response(f"未找到模型 '{model}' 的映射，也没有可用的 Anthropic 提供商", 404)
        model_type = "text"
        model_max_tokens = 0
    elif isinstance(mapping, list):
        # group 匹配：过滤掉没有 anthropic_url 或计费超限的提供商，再按优先级加权轮询
        available = [m for m in mapping if m.get("anthropic_url", "") and config.check_provider_billing(m["provider_id"])["allowed"]]
        if not available:
            return _error_response(f"模型 '{model}' 的所有可用 Anthropic 提供商均已超限或不可用", 429)
        # 请求含图片时，优先选择多模态模型；无多模态候选项则回退到全部候选项
        if _has_images_anthropic(request_body):
            multimodal = [m for m in available if m.get("model_type") == "multimodal"]
            if multimodal:
                available = multimodal
        chosen = _pick_weighted_round_robin(available, client_ip, model)
        provider = {
            "id": chosen["provider_id"],
            "name": chosen["provider_name"],
            "anthropic_url": chosen["anthropic_url"],
            "api_key": chosen["api_key"],
            "max_concurrency": chosen.get("provider_max_concurrency", 0),
            "full_path": chosen.get("full_path", 1),
        }
        target_model = chosen["target_model"]
        model_type = chosen.get("model_type", "text")
        model_max_tokens = chosen.get("max_tokens", 0)
    else:
        # 单个精确匹配：如果请求含图片但模型不是多模态，尝试从同 group 中找多模态替代
        if _has_images_anthropic(request_body) and mapping.get("model_type") != "multimodal":
            alt = config.get_model_mapping_by_alias(mapping.get("group_name", "") or "")
            if isinstance(alt, list):
                multimodal = [m for m in alt if m.get("model_type") == "multimodal"
                              and m.get("anthropic_url", "")
                              and config.check_provider_billing(m["provider_id"])["allowed"]]
                if multimodal:
                    mapping = multimodal[0]
        if not mapping.get("anthropic_url", ""):
            return _error_response(f"模型 '{model}' 的提供商未配置 Anthropic URL", 404)
        provider = {
            "id": mapping["provider_id"],
            "name": mapping["provider_name"],
            "anthropic_url": mapping["anthropic_url"],
            "api_key": mapping["api_key"],
            "max_concurrency": mapping.get("provider_max_concurrency", 0),
            "full_path": mapping.get("full_path", 1),
        }
        target_model = mapping["target_model"]
        model_type = mapping.get("model_type", "text")
        model_max_tokens = mapping.get("max_tokens", 0)

    start_time = time.time()
    provider_id = provider.get("id", 0)
    max_concurrency = provider.get("max_concurrency", 0)

    # 计费检查：provider 是否超限或过期
    billing_check = config.check_provider_billing(provider_id)
    if not billing_check["allowed"]:
        return _error_response(
            f"提供商 '{provider['name']}' 已被限制: {billing_check['reason']}", 429
        )
    if billing_check["near_limit"]:
        print(f"[计费警告] 提供商 '{provider['name']}' 使用量已达 {billing_check['usage_percent']:.0%}")

    # 获取并发信号量（如果配置了限制）
    sem = _get_semaphore(provider_id, max_concurrency)

    if sem:
        # 阻塞等待获取信号量，对 Claude Code 来说只是响应慢了
        sem.acquire()
    _track_request_start(provider_id)

    try:
        return _proxy_anthropic(request_body, provider, target_model, stream, sem, client_ip, start_time, model_type, model, model_max_tokens)

    except Exception as e:
        _release_concurrency(provider_id, sem)
        duration_ms = int((time.time() - start_time) * 1000)
        if isinstance(e, _CONN_ABORT_ERRORS):
            # 连接级异常：上游断开/超时/DNS解析失败，提供更友好的错误信息
            msg = f"连接中断: {type(e).__name__}"
            config.add_log(
                provider=provider["name"], model=target_model, source_model=model,
                input_tokens=0, output_tokens=0,
                status="error", duration_ms=duration_ms,
                error_msg=msg, request_body=json.dumps(request_body, ensure_ascii=False),
                client_ip=client_ip,
            )
            return _error_response(msg, 502)
        config.add_log(
            provider=provider["name"], model=target_model, source_model=model,
            input_tokens=0, output_tokens=0,
            status="error", duration_ms=duration_ms,
            error_msg=str(e), request_body=json.dumps(request_body, ensure_ascii=False),
            client_ip=client_ip,
        )
        return _error_response(str(e), 502)


def handle_openai_proxy_request(request_body, client_ip=""):
    """处理 OpenAI Chat Completions API 代理请求"""
    model = request_body.get("model", "")
    stream = request_body.get("stream", False)

    # 查找模型映射（与 Anthropic 入口共用同一套映射）
    mapping = config.get_model_mapping_by_alias(model)
    if not mapping:
        providers = config.get_providers()
        openai_providers = [p for p in providers if p["enabled"] and p.get("openai_url", "")]
        if openai_providers:
            provider = {
                "id": openai_providers[0]["id"],
                "name": openai_providers[0]["name"],
                "openai_url": openai_providers[0]["openai_url"],
                "api_key": openai_providers[0]["api_key"],
                "max_concurrency": openai_providers[0].get("max_concurrency", 0),
                "full_path": openai_providers[0].get("full_path", 1),
            }
            target_model = model
        else:
            return _error_response_openai(f"未找到模型 '{model}' 的映射，也没有可用的 OpenAI 提供商", 404)
        model_type = "text"
        model_max_tokens = 0
    elif isinstance(mapping, list):
        # group 匹配：过滤掉没有 openai_url 或计费超限的提供商，再按优先级加权轮询
        available = [m for m in mapping if m.get("openai_url", "") and config.check_provider_billing(m["provider_id"])["allowed"]]
        if not available:
            return _error_response_openai(f"模型 '{model}' 的所有可用 OpenAI 提供商均已超限或不可用", 429)
        # 请求含图片时，优先选择多模态模型；无多模态候选项则回退到全部候选项
        if _has_images_openai(request_body):
            multimodal = [m for m in available if m.get("model_type") == "multimodal"]
            if multimodal:
                available = multimodal
        chosen = _pick_weighted_round_robin(available, client_ip, model)
        provider = {
            "id": chosen["provider_id"],
            "name": chosen["provider_name"],
            "openai_url": chosen["openai_url"],
            "api_key": chosen["api_key"],
            "max_concurrency": chosen.get("provider_max_concurrency", 0),
            "full_path": chosen.get("full_path", 1),
        }
        target_model = chosen["target_model"]
        model_type = chosen.get("model_type", "text")
        model_max_tokens = chosen.get("max_tokens", 0)
    else:
        # 单个精确匹配：如果请求含图片但模型不是多模态，尝试从同 group 中找多模态替代
        if _has_images_openai(request_body) and mapping.get("model_type") != "multimodal":
            alt = config.get_model_mapping_by_alias(mapping.get("group_name", "") or "")
            if isinstance(alt, list):
                multimodal = [m for m in alt if m.get("model_type") == "multimodal"
                              and m.get("openai_url", "")
                              and config.check_provider_billing(m["provider_id"])["allowed"]]
                if multimodal:
                    mapping = multimodal[0]
        if not mapping.get("openai_url", ""):
            return _error_response_openai(f"模型 '{model}' 的提供商未配置 OpenAI URL", 404)
        provider = {
            "id": mapping["provider_id"],
            "name": mapping["provider_name"],
            "openai_url": mapping["openai_url"],
            "api_key": mapping["api_key"],
            "max_concurrency": mapping.get("provider_max_concurrency", 0),
            "full_path": mapping.get("full_path", 1),
        }
        target_model = mapping["target_model"]
        model_type = mapping.get("model_type", "text")
        model_max_tokens = mapping.get("max_tokens", 0)

    start_time = time.time()
    provider_id = provider.get("id", 0)
    max_concurrency = provider.get("max_concurrency", 0)

    # 计费检查：provider 是否超限或过期
    billing_check = config.check_provider_billing(provider_id)
    if not billing_check["allowed"]:
        return _error_response_openai(
            f"提供商 '{provider['name']}' 已被限制: {billing_check['reason']}", 429
        )
    if billing_check["near_limit"]:
        print(f"[计费警告] 提供商 '{provider['name']}' 使用量已达 {billing_check['usage_percent']:.0%}")

    sem = _get_semaphore(provider_id, max_concurrency)
    if sem:
        sem.acquire()
    _track_request_start(provider_id)

    try:
        # OpenAI → OpenAI：直接转发到 openai_url
        return _proxy_openai_direct(request_body, provider, target_model, stream, sem, client_ip, start_time, model_type, model, model_max_tokens)

    except Exception as e:
        _release_concurrency(provider_id, sem)
        duration_ms = int((time.time() - start_time) * 1000)
        if isinstance(e, _CONN_ABORT_ERRORS):
            msg = f"连接中断: {type(e).__name__}"
            config.add_log(
                provider=provider["name"], model=target_model, source_model=model,
                input_tokens=0, output_tokens=0,
                status="error", duration_ms=duration_ms,
                error_msg=msg, request_body=json.dumps(request_body, ensure_ascii=False),
                client_ip=client_ip,
            )
            return _error_response_openai(msg, 502)
        config.add_log(
            provider=provider["name"], model=target_model, source_model=model,
            input_tokens=0, output_tokens=0,
            status="error", duration_ms=duration_ms,
            error_msg=str(e), request_body=json.dumps(request_body, ensure_ascii=False),
            client_ip=client_ip,
        )
        return _error_response_openai(str(e), 502)


def _proxy_openai_direct(request_body, provider, target_model, stream, sem=None, client_ip="", start_time=None, model_type="text", source_model="", model_max_tokens=0):
    """OpenAI 格式直接转发到 provider 的 openai_url。

    full_path=1（默认）：配置的 openai_url 原样使用，不拼接任何后缀。
    full_path=0：配置的 openai_url 视为 base 路径，自动拼接 /chat/completions。
    """
    url = provider["openai_url"].rstrip("/")
    if not provider.get("full_path", 1) and not url.endswith("/chat/completions"):
        url += "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider['api_key']}",
    }
    body = dict(request_body)
    body["model"] = target_model

    # 文本模型需替换图片内容为文本提示，多模态模型保留图片
    if model_type != "multimodal":
        _strip_images_openai(body)

    # 如果模型配置了 max_tokens 且客户端未指定，使用模型配置值
    if model_max_tokens > 0 and not body.get("max_tokens"):
        body["max_tokens"] = model_max_tokens

    if stream:
        return _stream_response_openai(url, headers, body, provider, target_model, sem, client_ip, start_time, source_model)
    else:
        try:
            resp = _post_with_retry(url, headers=headers, json=body, timeout=120)
            resp_json = resp.json()
            usage = resp_json.get("usage", {})
            original_code = resp.status_code
            mapped_code = config.get_mapped_code(original_code, provider["name"])
            config.add_log(
                provider=provider["name"], model=target_model, source_model=source_model,
                input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0),
                status="success" if original_code == 200 else "error",
                duration_ms=int((time.time() - start_time) * 1000),
                error_msg="" if original_code == 200 else json.dumps(resp_json, ensure_ascii=False)[:500],
                request_body=json.dumps(body, ensure_ascii=False),
                response_body=json.dumps(resp_json, ensure_ascii=False),
                original_status_code=original_code, mapped_status_code=mapped_code,
                client_ip=client_ip,
            )
            if original_code == 200:
                _track_usage(provider.get("id", 0), usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
            return json.dumps(resp_json, ensure_ascii=False).encode("utf-8"), mapped_code, {"Content-Type": "application/json"}
        finally:
            _release_concurrency(provider["id"], sem)


def _stream_response_openai(url, headers, body, provider, target_model, sem=None, client_ip="", start_time=None, source_model=""):
    """OpenAI 流式直通转发"""
    resp = _post_with_retry(url, headers=headers, json=body, stream=True, timeout=300)
    original_status_code = resp.status_code

    # 上游返回非 2xx，不作为流式转发，直接返回错误
    if original_status_code >= 400:
        error_body = resp.text
        resp.close()
        mapped_code = config.get_mapped_code(original_status_code, provider["name"])
        _release_concurrency(provider["id"], sem)
        config.add_log(
            provider=provider["name"], model=target_model, source_model=source_model,
            input_tokens=0, output_tokens=0,
            status="error", duration_ms=int((time.time() - start_time) * 1000),
            error_msg=error_body[:500],
            request_body=json.dumps(body, ensure_ascii=False),
            response_body=error_body,
            original_status_code=original_status_code, mapped_status_code=mapped_code,
            client_ip=client_ip,
        )
        return error_body.encode("utf-8"), mapped_code, {"Content-Type": "application/json"}

    def generate():
        response_chunks = []
        input_tokens = 0
        output_tokens = 0
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0
        error_msg = ""
        try:
            for line in resp.iter_lines():
                if not line:
                    # 空行是 SSE 事件分隔符，必须保留以保证客户端正确解析事件边界
                    yield "\n"
                    continue
                decoded = line.decode("utf-8", errors="replace")
                response_chunks.append(decoded)
                yield decoded + "\n"

                # 从 OpenAI SSE chunk 中提取 usage（通常在最后一个 chunk）
                if decoded.startswith("data:") and not decoded.strip().endswith("[DONE]"):
                    try:
                        chunk = json.loads(_parse_sse_data(decoded))
                        usage = chunk.get("usage")
                        if usage:
                            input_tokens = usage.get("prompt_tokens", 0)
                            output_tokens = usage.get("completion_tokens", 0)
                    except (json.JSONDecodeError, IndexError):
                        pass
        except _CONN_ABORT_ERRORS:
            # 连接级异常（上游断开/超时/客户端断开），属于正常中断，不记录 error_msg
            pass
        except Exception as e:
            error_msg = str(e)
        finally:
            resp.close()
            _release_concurrency(provider["id"], sem)
            status = "error" if error_msg else "success"
            config.add_log(
                provider=provider["name"], model=target_model, source_model=source_model,
                input_tokens=input_tokens, output_tokens=output_tokens,
                status=status, duration_ms=int((time.time() - start_time) * 1000),
                error_msg=error_msg[:500],
                request_body=json.dumps(body, ensure_ascii=False),
                response_body="\n".join(response_chunks[-50:]),
                client_ip=client_ip,
            )
            if input_tokens > 0 or output_tokens > 0 or cache_read_input_tokens > 0 or cache_creation_input_tokens > 0:
                _track_usage(provider["id"], input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens)

    return generate(), resp.status_code, {"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _adapt_deepseek_anthropic(body):
    """适配 DeepSeek Anthropic 格式请求的思考模式参数

    DeepSeek Anthropic 端点与原生 Anthropic 的差异：
    - 不接受 thinking 参数（思考模式默认启用，无需显式开启）
    - 不支持 budget_tokens，使用 output_config.effort 控制思考强度
    - 思考强度: "high" 或 "max" (budget >= 10000 映射为 max)
    - DS 会自动检测 Claude Code 并将 effort 设为 max
    - 消息中的 content[].thinking 块必须保留，否则 DS 在思考模式下
      会报错 "must be passed back to the API"
    参考: https://api-docs.deepseek.com/zh-cn/guides/thinking_mode
    """
    # 移除顶层 thinking 参数，避免触发 DS 的严格校验模式
    thinking = body.pop("thinking", None)
    if thinking and isinstance(thinking, dict):
        # 映射 budget_tokens → output_config.effort
        budget = thinking.get("budget_tokens")
        if budget is not None:
            effort = "max" if budget >= 10000 else "high"
            body.setdefault("output_config", {})["effort"] = effort

    # DeepSeek 要求 user_id 匹配 ^[a-zA-Z0-9_-]+$，但 Claude Code 发送的
    # metadata.user_id 是 JSON 字符串（含 {}" 等特殊字符），会被拒绝。
    # 将不合法的 user_id 替换为 SHA256 截断值，保证唯一且合法。
    metadata = body.get("metadata", {})
    uid = metadata.get("user_id", "")
    if uid and not re.match(r'^[a-zA-Z0-9_-]+$', uid):
        metadata["user_id"] = hashlib.sha256(uid.encode()).hexdigest()[:32]

    # DeepSeek 思考模式下，所有 assistant 消息必须包含 thinking 块，
    # 否则报错 "content[].thinking must be passed back to the API"
    for msg in body.get("messages", []):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if isinstance(content, list) and not any(
            c.get("type") == "thinking" for c in content
        ):
            content.insert(0, {"type": "thinking", "thinking": ""})


def _adapt_minimax_anthropic(body):
    """清理 MiniMax 模型返回的无效 tool_use 块

    MiniMax 模型在 assistant 消息中会产生 name 为空的 tool_use 块，
    这些是无用的垃圾数据。需要：
    1. 移除包含此类块的整条 assistant 消息
    2. 移除引用了被删除 tool_use id 的整条 user 消息
    否则上游 API 会拒绝请求。
    """
    # 收集所有无效 tool_use 的 id
    invalid_ids = set()
    for msg in body.get("messages", []):
        if msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
            continue
        for block in msg["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_use" and not block.get("name"):
                invalid_ids.add(block.get("id"))

    if not invalid_ids:
        return

    # 移除包含无效 tool_use 的整条 assistant 消息，以及引用了这些 id 的整条 user 消息
    body["messages"] = [
        msg for msg in body.get("messages", [])
        if not (
            msg.get("role") == "assistant"
            and isinstance(msg.get("content"), list)
            and any(
                isinstance(block, dict) and block.get("type") == "tool_use" and not block.get("name")
                for block in msg["content"]
            )
        ) and not (
            msg.get("role") == "user"
            and isinstance(msg.get("content"), list)
            and any(
                isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") in invalid_ids
                for block in msg["content"]
            )
        )
    ]


def _proxy_anthropic(request_body, provider, target_model, stream, sem=None, client_ip="", start_time=None, model_type="text", source_model="", model_max_tokens=0):
    """直接转发 Anthropic 格式请求到 provider 的 anthropic_url。

    full_path=1（默认）：配置的 anthropic_url 原样使用，不拼接任何后缀。
    full_path=0：配置的 anthropic_url 视为 base 路径，自动拼接 /v1/messages。
    """
    url = provider["anthropic_url"].rstrip("/")
    if not provider.get("full_path", 1) and not url.endswith("/v1/messages"):
        url += "/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": provider["api_key"],
        "anthropic-version": request_body.get("_anthropic_version", "2023-06-01"),
    }
    body = {k: v for k, v in request_body.items() if not k.startswith("_")}
    body["model"] = target_model

    # 将 messages 中 role=system 的消息提取到 system 字段（Anthropic 要求 system 在顶层字段）
    system_messages = [m for m in body.get("messages", []) if m.get("role") == "system"]
    if system_messages:
        body["messages"] = [m for m in body["messages"] if m.get("role") != "system"]
        existing_system = body.get("system")
        new_system_parts = []
        # 保留已有的 system 字段内容
        if isinstance(existing_system, list):
            new_system_parts.extend(existing_system)
        elif isinstance(existing_system, str) and existing_system.strip():
            new_system_parts.append({"type": "text", "text": existing_system.strip()})
        # 将 messages 中提取的 system 消息追加
        for sm in system_messages:
            content = sm.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        new_system_parts.append({"type": "text", "text": block.get("text", "")})
                    elif isinstance(block, str):
                        new_system_parts.append({"type": "text", "text": block})
            elif isinstance(content, str) and content.strip():
                new_system_parts.append({"type": "text", "text": content})
        if new_system_parts:
            body["system"] = new_system_parts

    # 文本模型需替换图片内容为文本提示，多模态模型保留图片
    if model_type != "multimodal":
        _strip_images_anthropic(body)

    # 通用兜底：保证 max_tokens 存在且为正整数
    # Claude Code 在 thinking 模式下不传 max_tokens，而多数 Anthropic 兼容端点要求该字段
    # 优先使用模型配置的 max_tokens，否则使用默认值 128000
    if not body.get("max_tokens") or body["max_tokens"] <= 0:
        body["max_tokens"] = model_max_tokens if model_max_tokens > 0 else 128000

    # DeepSeek / MIMO 等兼容端点的思考模式参数适配
    provider_name = provider.get("name", "").lower()
    if "deepseek" in provider_name or "mimo" in provider_name:
        _adapt_deepseek_anthropic(body)

    # MiniMax 系列模型：清理 assistant 消息中 name 为空的无效 tool_use 块
    if "minimax" in target_model.lower():
        _adapt_minimax_anthropic(body)

    if stream:
        return _stream_response(url, headers, body, provider, target_model, sem, client_ip, start_time, source_model)
    else:
        try:
            resp = _post_with_retry(url, headers=headers, json=body, timeout=120)
            resp_json = resp.json()
            usage = resp_json.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            cache_read_input_tokens = usage.get("cache_read_input_tokens", 0)
            cache_creation_input_tokens = usage.get("cache_creation_input_tokens", 0)
            original_code = resp.status_code
            mapped_code = config.get_mapped_code(original_code, provider["name"])
            config.add_log(
                provider=provider["name"], model=target_model, source_model=source_model,
                input_tokens=input_tokens, output_tokens=output_tokens,
                status="success" if original_code == 200 else "error",
                duration_ms=int((time.time() - start_time) * 1000),
                error_msg="" if original_code == 200 else json.dumps(resp_json, ensure_ascii=False)[:500],
                request_body=json.dumps(body, ensure_ascii=False),
                response_body=json.dumps(resp_json, ensure_ascii=False),
                original_status_code=original_code, mapped_status_code=mapped_code,
                client_ip=client_ip,
                cache_read_input_tokens=cache_read_input_tokens,
                cache_creation_input_tokens=cache_creation_input_tokens,
            )
            if original_code == 200:
                _track_usage(provider.get("id", 0), input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens)
            return resp.content, mapped_code, {"Content-Type": "application/json"}
        finally:
            _release_concurrency(provider["id"], sem)






# ---- 流式响应 ----

def _stream_response(url, headers, body, provider, target_model, sem=None, client_ip="", start_time=None, source_model=""):
    """返回一个生成器用于 SSE 流式转发（Anthropic 协议直转，原样转发上游 Anthropic SSE）"""
    resp = _post_with_retry(url, headers=headers, json=body, stream=True, timeout=300)
    original_status_code = resp.status_code

    # 上游返回非 2xx，不作为流式转发，直接返回错误
    if original_status_code >= 400:
        error_body = resp.text
        resp.close()
        mapped_code = config.get_mapped_code(original_status_code, provider["name"])
        _release_concurrency(provider["id"], sem)
        config.add_log(
            provider=provider["name"], model=target_model, source_model=source_model,
            input_tokens=0, output_tokens=0,
            status="error", duration_ms=int((time.time() - start_time) * 1000),
            error_msg=error_body[:500],
            request_body=json.dumps(body, ensure_ascii=False),
            response_body=error_body,
            original_status_code=original_status_code, mapped_status_code=mapped_code,
            client_ip=client_ip,
        )
        return error_body.encode("utf-8"), mapped_code, {"Content-Type": "application/json"}

    def generate():
        input_tokens = 0
        output_tokens = 0
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0
        response_chunks = []
        error_msg = ""

        try:
            for line in resp.iter_lines():
                if not line:
                    # 空行是 SSE 事件分隔符，必须保留以保证客户端正确解析事件边界
                    yield "\n"
                    continue
                decoded = line.decode("utf-8", errors="replace")
                response_chunks.append(decoded)

                yield decoded + "\n"
                # 从 Anthropic SSE 事件提取 token 统计
                if decoded.startswith("data:"):
                    try:
                        d = json.loads(_parse_sse_data(decoded))
                        event_type = d.get("type", "")
                        if event_type == "message_start":
                            msg = d.get("message", {})
                            msg_usage = msg.get("usage", {})
                            input_tokens = msg_usage.get("input_tokens", 0)
                            cache_read_input_tokens = msg_usage.get("cache_read_input_tokens", 0)
                            cache_creation_input_tokens = msg_usage.get("cache_creation_input_tokens", 0)
                        elif event_type == "message_delta":
                            delta_usage = d.get("usage", {})
                            output_tokens = delta_usage.get("output_tokens", 0)
                            # 覆盖 message_start 中可能为 0 的 input_tokens
                            if delta_usage.get("input_tokens", 0) > 0:
                                input_tokens = delta_usage["input_tokens"]
                            if delta_usage.get("cache_read_input_tokens", 0) > 0:
                                cache_read_input_tokens = delta_usage["cache_read_input_tokens"]
                            if delta_usage.get("cache_creation_input_tokens", 0) > 0:
                                cache_creation_input_tokens = delta_usage["cache_creation_input_tokens"]
                    except (json.JSONDecodeError, IndexError):
                        pass

        except _CONN_ABORT_ERRORS:
            # 连接级异常（上游断开/超时/客户端断开），属于正常中断
            pass
        except Exception as e:
            error_msg = str(e)
        finally:
            resp.close()
            _release_concurrency(provider["id"], sem)
            status = "error" if error_msg else "success"
            config.add_log(
                provider=provider["name"], model=target_model, source_model=source_model,
                input_tokens=input_tokens, output_tokens=output_tokens,
                status=status, duration_ms=int((time.time() - start_time) * 1000),
                error_msg=error_msg[:500],
                request_body=json.dumps(body, ensure_ascii=False),
                response_body="\n".join(response_chunks[-50:]),
                original_status_code=original_status_code, mapped_status_code=original_status_code,
                client_ip=client_ip,
                cache_read_input_tokens=cache_read_input_tokens,
                cache_creation_input_tokens=cache_creation_input_tokens,
            )
            if input_tokens > 0 or output_tokens > 0 or cache_read_input_tokens > 0 or cache_creation_input_tokens > 0:
                _track_usage(provider["id"], input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens)

    return generate(), resp.status_code, {"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _error_response(message, status_code):
    body = json.dumps({
        "type": "error",
        "error": {"type": "api_error", "message": message},
    }, ensure_ascii=False).encode("utf-8")
    return body, status_code, {"Content-Type": "application/json"}


def _error_response_openai(message, status_code):
    body = json.dumps({
        "error": {"message": message, "type": "api_error", "code": status_code},
    }, ensure_ascii=False).encode("utf-8")
    return body, status_code, {"Content-Type": "application/json"}


