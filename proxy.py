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


def _post_with_retry(url, max_retries=1, retry_delay=1.0, **kwargs):
    """发起 HTTP POST 请求，遇到连接级异常时自动重试一次。

    网络/连接级异常（DNS 失败、连接被上游中止、握手超时）通常为瞬时问题，
    重试一次有较大概率成功。非连接级异常（HTTP 4xx/5xx、JSON 解析等）不重试。
    """
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            return http_requests.post(url, **kwargs)
        except _CONN_ABORT_ERRORS as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(retry_delay)
    raise last_err


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


def get_concurrency_status():
    """获取所有提供商的并发状态"""
    result = {}
    providers = config.get_providers()
    with _concurrency_lock:
        for p in providers:
            pid = p["id"]
            max_c = p.get("max_concurrency", 0)
            sem = _concurrency_semaphores.get(pid)
            if sem and max_c > 0:
                used = max(0, max_c - sem._value)
                result[pid] = {"used": used, "max": max_c, "waiting": 0}
            else:
                result[pid] = {"used": 0, "max": max_c, "waiting": 0}
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
        anthropic_providers = [p for p in providers if p["enabled"] and p["provider_type"] == "anthropic"]
        if anthropic_providers:
            provider = anthropic_providers[0]
            target_model = model
        else:
            return _error_response(f"未找到模型 '{model}' 的映射，也没有可用的 Anthropic 提供商", 404)
        model_type = "text"
        model_max_tokens = 0
    elif isinstance(mapping, list):
        # group 匹配：过滤掉计费超限的提供商，再按优先级加权轮询
        available = [m for m in mapping if config.check_provider_billing(m["provider_id"])["allowed"]]
        if not available:
            return _error_response(f"模型 '{model}' 的所有提供商均已超限或不可用", 429)
        # 请求含图片时，优先选择多模态模型；无多模态候选项则回退到全部候选项
        if _has_images_anthropic(request_body):
            multimodal = [m for m in available if m.get("model_type") == "multimodal"]
            if multimodal:
                available = multimodal
        chosen = _pick_weighted_round_robin(available, client_ip, model)
        provider = {
            "id": chosen["provider_id"],
            "name": chosen["provider_name"],
            "base_url": chosen["base_url"],
            "api_key": chosen["api_key"],
            "provider_type": chosen["provider_type"],
            "max_concurrency": chosen.get("provider_max_concurrency", 0),
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
                              and config.check_provider_billing(m["provider_id"])["allowed"]]
                if multimodal:
                    mapping = multimodal[0]
        provider = {
            "id": mapping["provider_id"],
            "name": mapping["provider_name"],
            "base_url": mapping["base_url"],
            "api_key": mapping["api_key"],
            "provider_type": mapping["provider_type"],
            "max_concurrency": mapping.get("provider_max_concurrency", 0),
        }
        target_model = mapping["target_model"]
        model_type = mapping.get("model_type", "text")
        model_max_tokens = mapping.get("max_tokens", 0)

    start_time = time.time()
    provider_type = provider["provider_type"]
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

    try:
        if provider_type == "anthropic":
            return _proxy_anthropic(request_body, provider, target_model, stream, sem, client_ip, start_time, model_type, model, model_max_tokens)
        elif provider_type == "openai":
            return _proxy_openai(request_body, provider, target_model, stream, sem, client_ip, start_time, model_type, model, model_max_tokens)
        else:
            if sem: sem.release()
            return _error_response(f"不支持的提供商类型: {provider_type}", 400)

    except Exception as e:
        if sem: sem.release()
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
        openai_providers = [p for p in providers if p["enabled"] and p["provider_type"] == "openai"]
        if openai_providers:
            provider = {
                "id": openai_providers[0]["id"],
                "name": openai_providers[0]["name"],
                "base_url": openai_providers[0]["base_url"],
                "api_key": openai_providers[0]["api_key"],
                "provider_type": openai_providers[0]["provider_type"],
                "max_concurrency": openai_providers[0].get("max_concurrency", 0),
            }
            target_model = model
        else:
            return _error_response_openai(f"未找到模型 '{model}' 的映射，也没有可用的 OpenAI 提供商", 404)
        model_type = "text"
        model_max_tokens = 0
    elif isinstance(mapping, list):
        # group 匹配：过滤掉计费超限的提供商，再按优先级加权轮询
        available = [m for m in mapping if config.check_provider_billing(m["provider_id"])["allowed"]]
        if not available:
            return _error_response_openai(f"模型 '{model}' 的所有提供商均已超限或不可用", 429)
        # 请求含图片时，优先选择多模态模型；无多模态候选项则回退到全部候选项
        if _has_images_openai(request_body):
            multimodal = [m for m in available if m.get("model_type") == "multimodal"]
            if multimodal:
                available = multimodal
        chosen = _pick_weighted_round_robin(available, client_ip, model)
        provider = {
            "id": chosen["provider_id"],
            "name": chosen["provider_name"],
            "base_url": chosen["base_url"],
            "api_key": chosen["api_key"],
            "provider_type": chosen["provider_type"],
            "max_concurrency": chosen.get("provider_max_concurrency", 0),
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
                              and config.check_provider_billing(m["provider_id"])["allowed"]]
                if multimodal:
                    mapping = multimodal[0]
        provider = {
            "id": mapping["provider_id"],
            "name": mapping["provider_name"],
            "base_url": mapping["base_url"],
            "api_key": mapping["api_key"],
            "provider_type": mapping["provider_type"],
            "max_concurrency": mapping.get("provider_max_concurrency", 0),
        }
        target_model = mapping["target_model"]
        model_type = mapping.get("model_type", "text")
        model_max_tokens = mapping.get("max_tokens", 0)

    start_time = time.time()
    provider_type = provider["provider_type"]
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

    try:
        if provider_type == "openai":
            # OpenAI → OpenAI：直接转发
            return _proxy_openai_direct(request_body, provider, target_model, stream, sem, client_ip, start_time, model_type, model, model_max_tokens)
        elif provider_type == "anthropic":
            # OpenAI → Anthropic：转换格式
            return _proxy_openai_to_anthropic(request_body, provider, target_model, stream, sem, client_ip, start_time, model_type, model, model_max_tokens)
        else:
            if sem: sem.release()
            return _error_response_openai(f"不支持的提供商类型: {provider_type}", 400)

    except Exception as e:
        if sem: sem.release()
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
    """OpenAI 格式直接转发到 OpenAI 提供商"""
    url = provider["base_url"].rstrip("/") + "/v1/chat/completions"
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
            if sem: sem.release()


def _proxy_openai_to_anthropic(request_body, provider, target_model, stream, sem=None, client_ip="", start_time=None, model_type="text", source_model="", model_max_tokens=0):
    """OpenAI 格式请求转发到 Anthropic 提供商，需双向转换"""
    url = provider["base_url"].rstrip("/") + "/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": provider["api_key"],
        "anthropic-version": "2023-06-01",
    }
    default_max_tokens = model_max_tokens if model_max_tokens > 0 else 4096
    anthropic_body = _openai_to_anthropic_request(request_body, target_model, default_max_tokens)

    # 文本模型需替换图片内容为文本提示，多模态模型保留图片
    if model_type != "multimodal":
        _strip_images_anthropic(anthropic_body)

    if stream:
        return _stream_response_anthropic_to_openai(url, headers, anthropic_body, provider, target_model, sem, client_ip, start_time, source_model)
    else:
        try:
            resp = _post_with_retry(url, headers=headers, json=anthropic_body, timeout=120)
            resp_json = resp.json()
            openai_resp = _anthropic_to_openai_response(resp_json, target_model)
            usage = openai_resp.get("usage", {})
            # Extract cache tokens from original Anthropic response
            anthropic_usage = resp_json.get("usage", {})
            cache_read_input_tokens = anthropic_usage.get("cache_read_input_tokens", 0)
            cache_creation_input_tokens = anthropic_usage.get("cache_creation_input_tokens", 0)
            original_code = resp.status_code
            mapped_code = config.get_mapped_code(original_code, provider["name"])
            config.add_log(
                provider=provider["name"], model=target_model, source_model=source_model,
                input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0),
                status="success" if original_code == 200 else "error",
                duration_ms=int((time.time() - start_time) * 1000),
                error_msg="" if original_code == 200 else json.dumps(resp_json, ensure_ascii=False)[:500],
                request_body=json.dumps(anthropic_body, ensure_ascii=False),
                response_body=json.dumps(resp_json, ensure_ascii=False),
                original_status_code=original_code, mapped_status_code=mapped_code,
                client_ip=client_ip,
                cache_read_input_tokens=cache_read_input_tokens,
                cache_creation_input_tokens=cache_creation_input_tokens,
            )
            if original_code == 200:
                _track_usage(provider.get("id", 0), usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), cache_read_input_tokens, cache_creation_input_tokens)
            return json.dumps(openai_resp, ensure_ascii=False).encode("utf-8"), mapped_code, {"Content-Type": "application/json"}
        finally:
            if sem: sem.release()


def _openai_to_anthropic_request(body, target_model, default_max_tokens=4096):
    """将 OpenAI 格式请求转为 Anthropic 格式"""
    messages = body.get("messages", [])
    system = ""
    anthropic_msgs = []

    for msg in messages:
        if msg["role"] == "system":
            system += msg.get("content", "") + "\n"
        else:
            content = msg.get("content", "")
            if isinstance(content, list):
                blocks = []
                for part in content:
                    if part.get("type") == "text":
                        blocks.append({"type": "text", "text": part.get("text", "")})
                    elif part.get("type") == "image_url":
                        blocks.append({"type": "text", "text": _IMAGE_PLACEHOLDER})
                anthropic_msgs.append({"role": msg["role"], "content": blocks})
            else:
                anthropic_msgs.append({"role": msg["role"], "content": str(content)})

    # tools
    tools = body.get("tools", [])
    anthropic_tools = []
    for t in tools:
        if t.get("type") == "function":
            fn = t.get("function", {})
            anthropic_tools.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {}),
            })

    result = {
        "model": target_model,
        "messages": anthropic_msgs,
        "max_tokens": body.get("max_tokens", default_max_tokens),
        "stream": body.get("stream", False),
    }
    if system.strip():
        result["system"] = system.strip()
    if body.get("temperature") is not None:
        result["temperature"] = body["temperature"]
    if anthropic_tools:
        result["tools"] = anthropic_tools
    return result


def _anthropic_to_openai_response(anthropic_resp, model):
    """将 Anthropic 响应转为 OpenAI 格式"""
    content = anthropic_resp.get("content", [])
    content_text = ""
    tool_calls = []
    for block in content:
        if block.get("type") == "text":
            content_text += block.get("text", "")
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                },
            })

    message = {"role": "assistant", "content": content_text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls

    usage = anthropic_resp.get("usage", {})
    stop_reason = anthropic_resp.get("stop_reason", "end_turn")
    finish_reason = "stop"
    if stop_reason == "tool_use":
        finish_reason = "tool_calls"
    elif stop_reason == "max_tokens":
        finish_reason = "length"

    return {
        "id": anthropic_resp.get("id", "chatcmpl-proxy"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    }


def _stream_response_openai(url, headers, body, provider, target_model, sem=None, client_ip="", start_time=None, source_model=""):
    """OpenAI 流式直通转发"""
    resp = _post_with_retry(url, headers=headers, json=body, stream=True, timeout=300)
    original_status_code = resp.status_code

    # 上游返回非 2xx，不作为流式转发，直接返回错误
    if original_status_code >= 400:
        error_body = resp.text
        resp.close()
        mapped_code = config.get_mapped_code(original_status_code, provider["name"])
        if sem: sem.release()
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
            if sem: sem.release()
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


def _stream_response_anthropic_to_openai(url, headers, body, provider, target_model, sem=None, client_ip="", start_time=None, source_model=""):
    """将 Anthropic SSE 流转为 OpenAI SSE 流"""
    resp = _post_with_retry(url, headers=headers, json=body, stream=True, timeout=300)
    original_status_code = resp.status_code

    # 上游返回非 2xx，不作为流式转发，直接返回错误
    if original_status_code >= 400:
        error_body = resp.text
        resp.close()
        mapped_code = config.get_mapped_code(original_status_code, provider["name"])
        if sem: sem.release()
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
        sent_role = False
        input_tokens = 0
        output_tokens = 0
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0
        error_msg = ""
        try:
            for line in resp.iter_lines():
                if line:
                    decoded = line.decode("utf-8", errors="replace")
                    response_chunks.append(decoded)

                    if not decoded.startswith("data:"):
                        continue
                    data_str = _parse_sse_data(decoded)
                    try:
                        d = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    event_type = d.get("type", "")

                    if event_type == "message_start":
                        # 从 message_start 提取 input_tokens 和 cache tokens
                        msg = d.get("message", {})
                        msg_usage = msg.get("usage", {})
                        input_tokens = msg_usage.get("input_tokens", 0)
                        cache_read_input_tokens = msg_usage.get("cache_read_input_tokens", 0)
                        cache_creation_input_tokens = msg_usage.get("cache_creation_input_tokens", 0)
                        # 发送 OpenAI 格式的初始 chunk
                        if not sent_role:
                            chunk = {"id": msg.get("id", "chatcmpl-proxy"), "object": "chat.completion.chunk", "created": int(time.time()), "model": target_model, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}
                            yield f"data: {json.dumps(chunk)}\n\n"
                            sent_role = True

                    elif event_type == "content_block_delta":
                        delta = d.get("delta", {})
                        text = delta.get("text", "")
                        if text:
                            chunk = {"id": "chatcmpl-proxy", "object": "chat.completion.chunk", "created": int(time.time()), "model": target_model, "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
                            yield f"data: {json.dumps(chunk)}\n\n"

                    elif event_type == "message_delta":
                        # 从 message_delta 提取 output_tokens 和覆盖 cache/input tokens
                        delta_usage = d.get("usage", {})
                        output_tokens = delta_usage.get("output_tokens", 0)
                        # 覆盖 message_start 中可能为 0 的 input_tokens
                        if delta_usage.get("input_tokens", 0) > 0:
                            input_tokens = delta_usage["input_tokens"]
                        if delta_usage.get("cache_read_input_tokens", 0) > 0:
                            cache_read_input_tokens = delta_usage["cache_read_input_tokens"]
                        if delta_usage.get("cache_creation_input_tokens", 0) > 0:
                            cache_creation_input_tokens = delta_usage["cache_creation_input_tokens"]
                        stop_reason = d.get("delta", {}).get("stop_reason", "end_turn")
                        fr = "stop"
                        if stop_reason == "tool_use":
                            fr = "tool_calls"
                        elif stop_reason == "max_tokens":
                            fr = "length"
                        chunk = {"id": "chatcmpl-proxy", "object": "chat.completion.chunk", "created": int(time.time()), "model": target_model, "choices": [{"index": 0, "delta": {}, "finish_reason": fr}]}
                        yield f"data: {json.dumps(chunk)}\n\n"
                        yield "data: [DONE]\n\n"

                    elif event_type == "message_stop":
                        yield "data: [DONE]\n\n"

        except _CONN_ABORT_ERRORS:
            # 连接级异常（上游断开/超时/客户端断开），属于正常中断
            pass
        except Exception as e:
            error_msg = str(e)
        finally:
            resp.close()
            if sem: sem.release()
            status = "error" if error_msg else "success"
            config.add_log(
                provider=provider["name"], model=target_model, source_model=source_model,
                input_tokens=input_tokens, output_tokens=output_tokens,
                status=status, duration_ms=int((time.time() - start_time) * 1000),
                error_msg=error_msg[:500],
                request_body=json.dumps(body, ensure_ascii=False),
                response_body="\n".join(response_chunks[-50:]),
                client_ip=client_ip,
                cache_read_input_tokens=cache_read_input_tokens,
                cache_creation_input_tokens=cache_creation_input_tokens,
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
    """直接转发 Anthropic 格式请求"""
    url = provider["base_url"].rstrip("/") + "/v1/messages"
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
        return _stream_response(url, headers, body, provider, target_model, "anthropic", sem, client_ip, start_time, source_model)
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
            if sem: sem.release()


def _proxy_openai(request_body, provider, target_model, stream, sem=None, client_ip="", start_time=None, model_type="text", source_model="", model_max_tokens=0):
    """转换 Anthropic 格式为 OpenAI 格式并转发"""
    url = provider["base_url"].rstrip("/") + "/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider['api_key']}",
    }
    default_max_tokens = model_max_tokens if model_max_tokens > 0 else 4096
    openai_body = _anthropic_to_openai(request_body, target_model, default_max_tokens)

    # 文本模型需替换图片内容为文本提示，多模态模型保留图片
    if model_type != "multimodal":
        _strip_images_openai(openai_body)

    if stream:
        return _stream_response(url, headers, openai_body, provider, target_model, "openai", sem, client_ip, start_time, source_model)
    else:
        try:
            resp = _post_with_retry(url, headers=headers, json=openai_body, timeout=120)
            resp_json = resp.json()
            anthropic_resp = _openai_to_anthropic_response(resp_json, target_model)
            usage = anthropic_resp.get("usage", {})
            original_code = resp.status_code
            mapped_code = config.get_mapped_code(original_code, provider["name"])
            # OpenAI path: cache tokens not applicable, pass 0
            config.add_log(
                provider=provider["name"], model=target_model, source_model=source_model,
                input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0),
                status="success" if original_code == 200 else "error",
                duration_ms=int((time.time() - start_time) * 1000),
                error_msg="" if original_code == 200 else json.dumps(resp_json, ensure_ascii=False)[:500],
                request_body=json.dumps(openai_body, ensure_ascii=False),
                response_body=json.dumps(resp_json, ensure_ascii=False),
                original_status_code=original_code, mapped_status_code=mapped_code,
                client_ip=client_ip,
            )
            if original_code == 200:
                _track_usage(provider.get("id", 0), usage.get("input_tokens", 0), usage.get("output_tokens", 0))
            content = json.dumps(anthropic_resp, ensure_ascii=False).encode("utf-8")
            return content, mapped_code, {"Content-Type": "application/json"}
        finally:
            if sem: sem.release()


# ---- 格式转换: Anthropic → OpenAI ----

def _anthropic_to_openai(body, target_model, default_max_tokens=4096):
    messages = []

    # system
    system = body.get("system", "")
    if system:
        if isinstance(system, list):
            sys_text = " ".join(
                b.get("text", "") for b in system if b.get("type") == "text"
            )
        else:
            sys_text = str(system)
        if sys_text:
            messages.append({"role": "system", "content": sys_text})

    # messages
    for msg in body.get("messages", []):
        role = msg["role"]
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = []
            for block in content:
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    parts.append(json.dumps({"tool_use": block}, ensure_ascii=False))
                elif block.get("type") == "tool_result":
                    tool_content = block.get("content", "")
                    if isinstance(tool_content, list):
                        tool_content = " ".join(
                            c.get("text", "") for c in tool_content if c.get("type") == "text"
                        )
                    parts.append(json.dumps({"tool_result": {"tool_use_id": block.get("tool_use_id", ""), "content": tool_content}}, ensure_ascii=False))
                elif block.get("type") == "image":
                    parts.append("[image]")
            messages.append({"role": role, "content": "\n".join(parts)})
        else:
            messages.append({"role": role, "content": str(content)})

    # tools → function tools (简化映射)
    tools = body.get("tools", [])
    openai_tools = []
    for t in tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        })

    result = {
        "model": target_model,
        "messages": messages,
        "max_tokens": body.get("max_tokens", default_max_tokens),
        "temperature": body.get("temperature", 1.0),
        "stream": body.get("stream", False),
    }
    if openai_tools:
        result["tools"] = openai_tools

    return result


# ---- 格式转换: OpenAI → Anthropic ----

def _openai_to_anthropic_response(openai_resp, model):
    choices = openai_resp.get("choices", [])
    if not choices:
        return {"error": "空响应"}

    choice = choices[0]
    msg = choice.get("message", {})
    content_text = msg.get("content", "") or ""
    tool_calls = msg.get("tool_calls", [])

    content = []
    if content_text:
        content.append({"type": "text", "text": content_text})
    for tc in tool_calls:
        fn = tc.get("function", {})
        content.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": fn.get("name", ""),
            "input": json.loads(fn.get("arguments", "{}")),
        })
    if not content:
        content.append({"type": "text", "text": ""})

    usage = openai_resp.get("usage", {})
    stop_reason = "end_turn"
    if choice.get("finish_reason") == "tool_calls":
        stop_reason = "tool_use"
    elif choice.get("finish_reason") == "length":
        stop_reason = "max_tokens"

    return {
        "id": openai_resp.get("id", "msg_proxy"),
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# ---- 流式响应 ----

def _stream_response(url, headers, body, provider, target_model, provider_type, sem=None, client_ip="", start_time=None, source_model=""):
    """返回一个生成器用于 SSE 流式转发"""
    resp = _post_with_retry(url, headers=headers, json=body, stream=True, timeout=300)
    original_status_code = resp.status_code

    # 上游返回非 2xx，不作为流式转发，直接返回错误
    if original_status_code >= 400:
        error_body = resp.text
        resp.close()
        mapped_code = config.get_mapped_code(original_status_code, provider["name"])
        if sem: sem.release()
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
                    if provider_type != "openai":
                        yield "\n"
                    continue
                decoded = line.decode("utf-8", errors="replace")
                response_chunks.append(decoded)

                if provider_type == "openai":
                    # 转换 OpenAI SSE 为 Anthropic SSE
                    if decoded.startswith("data:"):
                        data_str = _parse_sse_data(decoded)
                        if data_str.strip() == "[DONE]":
                            yield "event: message_stop\ndata: {}\n\n"
                            continue
                        try:
                            chunk = json.loads(data_str)
                            for event in _convert_openai_chunk_to_anthropic_events(chunk, target_model):
                                yield event
                            # 从 OpenAI chunk 提取 usage（通常在最后一个 chunk）
                            usage = chunk.get("usage")
                            if usage:
                                input_tokens = usage.get("prompt_tokens", 0)
                                output_tokens = usage.get("completion_tokens", 0)
                        except json.JSONDecodeError:
                            pass
                else:
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
            if sem: sem.release()
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


def _convert_openai_chunk_to_anthropic_events(chunk, model):
    """将一个 OpenAI streaming chunk 转为 Anthropic SSE events"""
    events = []
    choices = chunk.get("choices", [])
    if not choices:
        return events

    choice = choices[0]
    delta = choice.get("delta", {})
    finish_reason = choice.get("finish_reason")

    if choice.get("index", 0) == 0 and not delta.get("content") and not delta.get("tool_calls"):
        # message_start
        usage = chunk.get("usage", {})
        msg_start = {
            "type": "message_start",
            "message": {
                "id": chunk.get("id", "msg_proxy"),
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "usage": {
                    "input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0),
                },
            },
        }
        events.append(f"event: message_start\ndata: {json.dumps(msg_start)}\n\n")
        # content_block_start
        events.append(f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n")

    if delta.get("content"):
        cb_delta = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": delta["content"]}}
        events.append(f"event: content_block_delta\ndata: {json.dumps(cb_delta)}\n\n")

    if delta.get("tool_calls"):
        for tc in delta["tool_calls"]:
            fn = tc.get("function", {})
            tool_input = fn.get("arguments", "")
            tool_event = {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": json.dumps({"tool_use": {"id": tc.get("id", ""), "name": fn.get("name", ""), "input": tool_input}})},
            }
            events.append(f"event: content_block_delta\ndata: {json.dumps(tool_event)}\n\n")

    if finish_reason:
        events.append(f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n")
        stop_reason = "end_turn"
        if finish_reason == "tool_calls":
            stop_reason = "tool_use"
        # Extract output_tokens from usage if available in this chunk
        usage = chunk.get("usage", {})
        out_tokens = usage.get("completion_tokens", 0)
        msg_delta = {"type": "message_delta", "delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": out_tokens}}
        events.append(f"event: message_delta\ndata: {json.dumps(msg_delta)}\n\n")

    return events


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


