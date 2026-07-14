import hashlib
import inspect
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

# 重试策略配置：仅对 5xx 和连接级异常重试，4xx 视为客户端/上游语义错误立即返回
_RETRY_MAX_ATTEMPTS = 3        # 最多重试次数（首次失败后最多再重试 3 次，总请求数 ≤ 4）
_RETRY_MAX_DURATION = 5.0      # 单次请求耗时上限（秒）：超过则不再重试，避免对已处理很久的错误做无谓重试
_RETRY_DELAY = 1.0             # 重试间隔（秒）


def _post_with_retry(url, max_retries=_RETRY_MAX_ATTEMPTS, retry_delay=_RETRY_DELAY, retry_max_duration=_RETRY_MAX_DURATION, **kwargs):
    """发起 HTTP POST 请求，仅对 5xx 和连接级异常按策略自动重试。

    重试触发条件（需同时满足）：
      1. 请求出错：连接级异常（DNS 失败 / 连接被上游中止 / 握手超时 / ChunkedEncodingError 等）
                  或 HTTP 5xx 错误码；
      2. 单次请求耗时 < retry_max_duration 秒（上游已处理很久的错误重试代价高，不再重试）；
      3. 未达到最大重试次数 max_retries。

    4xx 视为客户端/上游语义错误（如 401/403/404 等），重试无意义，立即返回 resp 交由调用方处理
    （避免浪费上游配额并放大延迟）。

    该函数内部循环重试，仅返回最终结果（成功响应或最后一次的失败响应/异常），
    调用方在外层据此只记录一次日志——天然满足"只记录最后一次请求日志"。
    """
    last_err = None
    for attempt in range(max_retries + 1):
        start = time.time()
        try:
            resp = http_requests.post(url, **kwargs)
            elapsed = time.time() - start
            # HTTP 5xx：仅在耗时较短且仍有重试机会时关闭并重试
            if resp.status_code >= 500 and elapsed < retry_max_duration and attempt < max_retries:
                resp.close()
                time.sleep(retry_delay)
                continue
            # HTTP 4xx：客户端/上游语义错误，立即返回，不重试
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


def _role_replace_rules(mapping):
    """从模型映射 dict 中提取角色替换规则列表，返回 list[dict]，每项形如 {"from":..., "to":...}。

    role_mappings 在数据库中存为 JSON 字符串数组；非法或空时返回 []。
    """
    if not mapping:
        return []
    raw = mapping.get("role_mappings", "[]")
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return []
    rules = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                rf = item.get("from") or item.get("role_from")
                rt = item.get("to") or item.get("role_to")
                if rf and rt and rf != rt:
                    rules.append({"from": rf, "to": rt})
    return rules


def _apply_role_replacement(body, rules):
    """按 rules 列表依次替换请求体 messages 中的角色。

    rules 为 list[dict]，每项 {"from": 原角色, "to": 目标角色}。
    对 OpenAI（/v1、/openai/responses）和 Anthropic（/anthropic）链路均适用。
    注意：按列表顺序应用，先替换的结果会被后续规则再次处理（如需避免链式替换可一次性映射）。
    """
    if not rules:
        return
    # 一次性映射：相同原角色只取第一条规则，避免链式替换（A→B, B→C 把 A 变成 C）
    mapping_table = {}
    for r in rules:
        f, t = r.get("from"), r.get("to")
        if f and t and f != t and f not in mapping_table:
            mapping_table[f] = t
    if not mapping_table:
        return
    for msg in body.get("messages", []):
        if isinstance(msg, dict):
            new_role = mapping_table.get(msg.get("role"))
            if new_role:
                msg["role"] = new_role


def _apply_reasoning_effort(body, target_model, reasoning_effort_supported=False):
    """按模型映射的 reasoning_effort_supported 开关决定是否透传 reasoning_effort 字段。

    从 body.pop('_codex_reasoning_effort') 取出 _convert_responses_to_chat 暂存的 effort
    （如 'high'/'medium'/'low'，OpenAI Responses 三档）。无值或非字符串则直接返回
    （保持请求体干净，私有键仍被 pop 掉）。

    行为：
      - reasoning_effort_supported=False（默认）-> pop 私有键、return（保守跳过，不发任何字段）
      - reasoning_effort_supported=True            -> body['reasoning_effort'] = effort（原值透传，不翻译）

    值不翻译：认该字段的国产上游（GLM/DeepSeek/MiniMax）都用 low/medium/high 同语义，
    原样透传即对，开关只回答「该上游认不认 reasoning_effort 字段名」。

    必须在 failover 循环内、target_model 已知后调用（与 _apply_role_replacement 同一位置）。
    仅 Responses->Chat 路径的请求体携带 _codex_reasoning_effort 私有键；Chat Completions
    直通请求体无此键，调用为空操作，不污染直通路径。
    """
    effort = body.pop("_codex_reasoning_effort", None)
    if not isinstance(effort, str) or not effort:
        return
    if not reasoning_effort_supported:
        return
    body["reasoning_effort"] = effort


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
# {(client_ip, alias): round_robin_index}
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
    """释放并发资源：减少信号量槽位 + 递减活跃请求计数。

    幂等设计：可被多次调用，但日志会记录异常情况（活跃计数为 0 仍调用）
    以便排查重复释放问题。
    """
    if sem:
        try:
            sem.release()
        except ValueError:
            # 信号量已释放到上限（重复释放），记录但不影响主流程
            print(f"[并发] 警告: provider_id={provider_id} 信号量重复释放（已忽略）")
    with _active_lock:
        count = _active_requests.get(provider_id, 0)
        if count > 0:
            _active_requests[provider_id] = count - 1
        else:
            # 活跃计数已为 0，说明出现重复释放或未配对的 start
            _active_requests[provider_id] = 0
            print(f"[并发] 警告: provider_id={provider_id} 活跃计数已为 0，疑似重复释放")


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


# ---- 服务降级状态管理 ----
# 按 mapping_id（即 model_mappings 表的 id）粒度维护降级状态。
# 同一 alias 下不同 provider/目标模型各自独立降级，避免一个故障上游牵连正常上游。
# _post_with_retry 内部已做 3 次重试，调用方捕获到异常或收到错误响应时
# 表示真正重试耗尽，直接 mark_degraded，无需额外累积计数。
_degradation_lock = threading.Lock()
_degradation_until = {}  # {mapping_id: degraded_until_timestamp}


def get_degradation_config():
    return config.get_degradation_config()


def mark_degraded(mapping_id, duration_seconds):
    """标记某 mapping_id 进入降级状态，duration_seconds 秒内不再被选中。"""
    if not mapping_id:
        return
    with _degradation_lock:
        _degradation_until[mapping_id] = time.time() + duration_seconds


def clear_degraded(mapping_id):
    """清除某 mapping_id 的降级状态（成功调用后立即恢复）。"""
    if not mapping_id:
        return
    with _degradation_lock:
        _degradation_until.pop(mapping_id, None)


def is_degraded(mapping_id):
    """检查某 mapping_id 是否处于降级中。过期自动清除。"""
    if not mapping_id:
        return False
    with _degradation_lock:
        until = _degradation_until.get(mapping_id, 0)
        if until <= time.time():
            _degradation_until.pop(mapping_id, None)
            return False
        return True


def get_degradation_status():
    """返回所有当前降级中的 mapping_id 状态，供 API 查询。

    返回 dict: {mapping_id: {"degraded": True, "remaining": int}}
    同时清理已过期的条目。
    """
    now = time.time()
    result = {}
    with _degradation_lock:
        expired = [mid for mid, until in _degradation_until.items() if until <= now]
        for mid in expired:
            _degradation_until.pop(mid, None)
        for mid, until in _degradation_until.items():
            result[mid] = {"degraded": True, "remaining": max(0, int(until - now))}
    return result


def _filter_candidates_by_degradation(candidates, cfg=None):
    """按降级状态过滤候选池。

    candidates 中每项必须含 "id" 键（Anthropic 路径）或 "mapping_id" 键（OpenAI 路径）。
    降级未启用时原样返回；有非降级候选时只保留非降级的；全部降级时回退到全量池。
    """
    if cfg is None:
        cfg = get_degradation_config()
    if not cfg["enabled"]:
        return candidates
    non_degraded = [c for c in candidates if not is_degraded(c.get("id") or c.get("mapping_id"))]
    if non_degraded:
        return non_degraded
    # 全部候选都已降级：回退到原始池（仍可被选中），打 warning 便于排查
    degraded_ids = sorted({c.get("id") or c.get("mapping_id") for c in candidates if (c.get("id") or c.get("mapping_id"))})
    print(
        f"[降级] 所有候选均已降级，回退到原始池。mapping_ids={degraded_ids} "
        f"duration={cfg.get('duration')}s"
    )
    return candidates


# ---- 响应判断辅助 ----

def _is_streaming_response(response):
    content, _, _ = response
    return inspect.isgenerator(content)

def _is_success_response(response):
    if _is_streaming_response(response):
        return True
    _, status_code, _ = response
    return status_code < 400


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


def _pick_weighted_round_robin(candidates, client_ip, alias):
    """按客户端 IP 和别名隔离的加权轮询选择"""
    sequence = _build_weighted_sequence(candidates)
    if not sequence:
        return None
    # 如果序列长度为 1（只有一个模型），直接返回
    if len(sequence) == 1:
        return sequence[0]
    key = (client_ip, alias)
    with _rr_lock:
        idx = _rr_index.get(key, -1)
        idx = (idx + 1) % len(sequence)
        _rr_index[key] = idx
        return sequence[idx]


def handle_proxy_request(request_body, client_ip=""):
    """处理 Anthropic Messages API 代理请求，返回 (response_generator, status_code, headers)"""
    model = request_body.get("model", "")
    stream = request_body.get("stream", False)

    # 查找模型映射（新契约：返回 list[dict] 或 None）
    mappings = config.get_model_mapping_by_alias(model)
    if not mappings:
        # 没有映射，尝试找默认的 anthropic provider 直接转发（无 mapping_id，不参与降级）
        providers = config.get_providers()
        anthropic_providers = [p for p in providers if p["enabled"] and p.get("anthropic_url", "")]
        if not anthropic_providers:
            return _error_response(f"未找到模型 '{model}' 的映射，也没有可用的 Anthropic 提供商", 404)
        # 与 OpenAI fallback 保持一致：显式构造 provider dict
        p0 = anthropic_providers[0]
        provider = {
            "id": p0["id"],
            "name": p0["name"],
            "anthropic_url": p0["anthropic_url"],
            "api_key": p0["api_key"],
            "max_concurrency": p0.get("max_concurrency", 0),
            "full_path": p0.get("full_path", 1),
        }
        # 单次尝试，无降级逻辑
        start_time = time.time()
        provider_id = provider.get("id", 0)
        max_concurrency = provider.get("max_concurrency", 0)
        billing_check = config.check_provider_billing(provider_id)
        if not billing_check["allowed"]:
            return _error_response(
                f"提供商 '{provider['name']}' 已被限制: {billing_check['reason']}", 429
            )
        if billing_check["near_limit"]:
            print(f"[计费警告] 提供商 '{provider['name']}' 使用量已达 {billing_check['usage_percent']:.0%}")
        sem = _get_semaphore(provider_id, max_concurrency)
        if sem:
            sem.acquire()
        _track_request_start(provider_id)
        try:
            response = _proxy_anthropic(request_body, provider, model, stream, sem, client_ip, start_time, "text", model, 0, [])
        except Exception as e:
            _release_concurrency(provider_id, sem)
            duration_ms = int((time.time() - start_time) * 1000)
            if isinstance(e, _CONN_ABORT_ERRORS):
                msg = f"连接中断: {type(e).__name__}"
                config.add_log(
                    provider=provider["name"], model=model, source_model=model,
                    input_tokens=0, output_tokens=0,
                    status="error", duration_ms=duration_ms,
                    error_msg=msg, request_body=json.dumps(request_body, ensure_ascii=False),
                    client_ip=client_ip,
                )
                return _error_response(msg, 502)
            config.add_log(
                provider=provider["name"], model=model, source_model=model,
                input_tokens=0, output_tokens=0,
                status="error", duration_ms=duration_ms,
                error_msg=str(e), request_body=json.dumps(request_body, ensure_ascii=False),
                client_ip=client_ip,
            )
            return _error_response(str(e), 502)
        # 成功：非流式需释放；流式由 _stream_response/generate() 自行释放
        if not stream:
            _release_concurrency(provider_id, sem)
        return response

    # ---- 命中映射列表：failover 循环 ----
    # 过滤掉没有 anthropic_url 的提供商（计费超限在循环内逐个判断，便于 failover）
    candidates = [m for m in mappings if m.get("anthropic_url", "")]
    if not candidates:
        return _error_response(f"模型 '{model}' 的所有可用 Anthropic 提供商均不可用", 429)

    # 请求含图片时，优先选择多模态模型；无多模态候选项则回退到全部候选项
    if _has_images_anthropic(request_body):
        multimodal = [m for m in candidates if m.get("model_type") == "multimodal"]
        if multimodal:
            candidates = multimodal

    cfg = get_degradation_config()
    attempted = set()  # 已尝试的 mapping id（同一请求内不重复尝试）
    last_response = None  # 最近一次失败响应，全部尝试完后返回
    _max_loop_iterations = 100  # 防御性兜底：防止极端退化场景下的无限循环

    for _loop_iter in range(_max_loop_iterations):
        # 从未尝试过的候选中按降级状态过滤，为空则回退到未过滤列表
        untried = [c for c in candidates if c.get("id") not in attempted]
        if not untried:
            break
        filtered = _filter_candidates_by_degradation(untried, cfg)
        if not filtered:
            break
        chosen = _pick_weighted_round_robin(filtered, client_ip, model)
        if chosen is None:
            break
        mid = chosen.get("id")
        attempted.add(mid)
        alias = chosen.get("alias")

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
        role_rules = _role_replace_rules(chosen)
        provider_id = provider.get("id", 0)
        max_concurrency = provider.get("max_concurrency", 0)

        # 计费检查：超限则跳过（不降级，仅加入 attempted）
        billing_check = config.check_provider_billing(provider_id)
        if not billing_check["allowed"]:
            last_response = _error_response(
                f"提供商 '{provider['name']}' 已被限制: {billing_check['reason']}", 429
            )
            continue
        if billing_check["near_limit"]:
            print(f"[计费警告] 提供商 '{provider['name']}' 使用量已达 {billing_check['usage_percent']:.0%}")

        start_time = time.time()
        sem = _get_semaphore(provider_id, max_concurrency)
        if sem:
            sem.acquire()
        _track_request_start(provider_id)

        # 信号量释放由外部处理器统一负责：
        # - 非流式：_proxy_anthropic 不再在内部 finally 释放，由本处 except/成功/失败响应 路径释放
        # - 流式：_stream_response 在 status>=400 时内部释放，或由 generate() finally 释放；
        #   仅当 _post_with_retry 抛出异常时由本处 except 释放
        try:
            response = _proxy_anthropic(request_body, provider, target_model, stream, sem, client_ip, start_time, model_type, model, model_max_tokens, role_rules, mid, cfg.get("duration", 30))
        except _CONN_ABORT_ERRORS as e:
            _release_concurrency(provider_id, sem)
            duration_ms = int((time.time() - start_time) * 1000)
            msg = f"连接中断: {type(e).__name__}"
            config.add_log(
                provider=provider["name"], model=target_model, source_model=model,
                input_tokens=0, output_tokens=0,
                status="error", duration_ms=duration_ms,
                error_msg=msg, request_body=json.dumps(request_body, ensure_ascii=False),
                client_ip=client_ip,
            )
            last_response = _error_response(msg, 502)
            mark_degraded(mid, cfg.get("duration", 30))
            continue
        except Exception as e:
            _release_concurrency(provider_id, sem)
            duration_ms = int((time.time() - start_time) * 1000)
            config.add_log(
                provider=provider["name"], model=target_model, source_model=model,
                input_tokens=0, output_tokens=0,
                status="error", duration_ms=duration_ms,
                error_msg=str(e), request_body=json.dumps(request_body, ensure_ascii=False),
                client_ip=client_ip,
            )
            last_response = _error_response(str(e), 502)
            mark_degraded(mid, cfg.get("duration", 30))
            continue

        # 成功（含流式已开始）：非流式需在此释放；流式由 _stream_response/generate() 自行释放
        if _is_success_response(response):
            if not stream:
                _release_concurrency(provider_id, sem)
            clear_degraded(mid)
            return response

        # 失败响应：非流式需在此释放；流式由 _stream_response 内部释放
        if not stream:
            _release_concurrency(provider_id, sem)
        last_response = response
        mark_degraded(mid, cfg.get("duration", 30))
        continue
    else:
        # 防御性兜底：达到最大循环次数仍未结束，记录并退出
        print(f"[failover] 警告: 达到最大循环次数 {_max_loop_iterations} 仍未结束，强制退出。model={model}")

    # 所有候选尝试完仍失败
    if last_response is not None:
        return last_response
    return _error_response(f"模型 '{model}' 没有可用的 Anthropic 提供商", 503)


def _resolve_openai_provider(request_body, client_ip=""):
    """解析 OpenAI 协议请求的候选池（Chat Completions 与 Responses 入口共用）。

    返回 (candidates, error_message)：
      - 成功：candidates 为 list[dict]，每项含 provider / target_model / model_type /
              model_max_tokens / role_rules / mapping_id；error_message 为 None。
              fallback（无映射）场景只有一个候选，mapping_id 为 None（不参与降级）。
      - 失败：candidates 为 None，error_message 为错误描述（调用方按自身错误格式包装）。
    解析规则：
      - 别名无映射 → 取首个配置了 openai_url 的启用 provider 作兜底（单候选，无 mapping_id）
      - 别名命中映射列表 → 过滤 openai_url 可用项，含图片时优先多模态
    计费检查放在 failover 循环内逐候选判断，便于超限时切换到下一个。
    """
    model = request_body.get("model", "")
    mappings = config.get_model_mapping_by_alias(model)
    if not mappings:
        providers = config.get_providers()
        openai_providers = [p for p in providers if p["enabled"] and p.get("openai_url", "")]
        if openai_providers:
            p0 = openai_providers[0]
            provider = {
                "id": p0["id"],
                "name": p0["name"],
                "openai_url": p0["openai_url"],
                "api_key": p0["api_key"],
                "max_concurrency": p0.get("max_concurrency", 0),
                "full_path": p0.get("full_path", 1),
            }
            return [{
                "provider": provider,
                "target_model": model,
                "model_type": "text",
                "model_max_tokens": 0,
                "role_rules": [],
                "mapping_id": None,
                "priority": 1,
                "reasoning_effort_supported": 1,  # 无映射场景默认透传，与新建映射默认值一致
            }], None
        return None, f"未找到模型 '{model}' 的映射，也没有可用的 OpenAI 提供商"
    else:
        # 命中映射列表：过滤掉没有 openai_url 的提供商（计费超限在循环内逐个判断）
        available = [m for m in mappings if m.get("openai_url", "")]
        if not available:
            return None, f"模型 '{model}' 的所有可用 OpenAI 提供商均不可用"
        # 请求含图片时，优先选择多模态模型；无多模态候选项则回退到全部候选项
        if _has_images_openai(request_body):
            multimodal = [m for m in available if m.get("model_type") == "multimodal"]
            if multimodal:
                available = multimodal
        candidates = []
        for m in available:
            provider = {
                "id": m["provider_id"],
                "name": m["provider_name"],
                "openai_url": m["openai_url"],
                "api_key": m["api_key"],
                "max_concurrency": m.get("provider_max_concurrency", 0),
                "full_path": m.get("full_path", 1),
            }
            candidates.append({
                "provider": provider,
                "target_model": m["target_model"],
                "model_type": m.get("model_type", "text"),
                "model_max_tokens": m.get("max_tokens", 0),
                "role_rules": _role_replace_rules(m),
                "mapping_id": m.get("id"),
                "alias": m.get("alias"),
                "priority": m.get("priority", 1),
                "reasoning_effort_supported": m.get("reasoning_effort_supported", 0),
            })
        return candidates, None


def handle_openai_proxy_request(request_body, client_ip=""):
    """处理 OpenAI Chat Completions API 代理请求"""
    model = request_body.get("model", "")
    stream = request_body.get("stream", False)

    # 解析候选池（与 Responses 入口共用同一套逻辑）
    candidates, error_message = _resolve_openai_provider(request_body, client_ip)
    if candidates is None:
        return _error_response_openai(error_message, 404 if "未找到" in error_message or "未配置" in error_message else 429)

    cfg = get_degradation_config()
    attempted = set()  # 已尝试的 mapping id（同一请求内不重复尝试）
    last_response = None  # 最近一次失败响应，全部尝试完后返回
    _max_loop_iterations = 100  # 防御性兜底：防止极端退化场景下的无限循环

    for _loop_iter in range(_max_loop_iterations):
        # 从未尝试过的候选中按降级状态过滤，为空则回退到未过滤列表
        untried = [c for c in candidates if c["mapping_id"] not in attempted]
        if not untried:
            break
        filtered = _filter_candidates_by_degradation(untried, cfg)
        if not filtered:
            break
        chosen = _pick_weighted_round_robin(filtered, client_ip, model)
        if chosen is None:
            break
        mid = chosen.get("mapping_id")
        attempted.add(mid)
        alias = chosen.get("alias")

        provider = chosen["provider"]
        target_model = chosen["target_model"]
        model_type = chosen["model_type"]
        model_max_tokens = chosen["model_max_tokens"]
        role_rules = chosen["role_rules"]
        reasoning_effort_supported = chosen.get("reasoning_effort_supported", 0)
        provider_id = provider.get("id", 0)
        max_concurrency = provider.get("max_concurrency", 0)

        # 计费检查：超限则跳过（不降级，仅加入 attempted）
        billing_check = config.check_provider_billing(provider_id)
        if not billing_check["allowed"]:
            last_response = _error_response_openai(
                f"提供商 '{provider['name']}' 已被限制: {billing_check['reason']}", 429
            )
            continue
        if billing_check["near_limit"]:
            print(f"[计费警告] 提供商 '{provider['name']}' 使用量已达 {billing_check['usage_percent']:.0%}")

        start_time = time.time()
        sem = _get_semaphore(provider_id, max_concurrency)
        if sem:
            sem.acquire()
        _track_request_start(provider_id)

        # 信号量释放由本处统一负责：
        # - 非流式：_proxy_openai_direct 不再在内部 finally 释放，由 except/成功/失败响应 路径释放
        # - 流式：_stream_response_openai 在 status>=400 时内部释放，或由 generate() finally 释放；
        #   仅当 _post_with_retry 抛出异常时由本处 except 释放
        try:
            response = _proxy_openai_direct(request_body, provider, target_model, stream, sem, client_ip, start_time, model_type, model, model_max_tokens, role_rules, mid, cfg.get("duration", 30), reasoning_effort_supported=reasoning_effort_supported)
        except _CONN_ABORT_ERRORS as e:
            _release_concurrency(provider_id, sem)
            duration_ms = int((time.time() - start_time) * 1000)
            msg = f"连接中断: {type(e).__name__}"
            config.add_log(
                provider=provider["name"], model=target_model, source_model=model,
                input_tokens=0, output_tokens=0,
                status="error", duration_ms=duration_ms,
                error_msg=msg, request_body=json.dumps(request_body, ensure_ascii=False),
                client_ip=client_ip,
            )
            last_response = _error_response_openai(msg, 502)
            mark_degraded(mid, cfg.get("duration", 30))
            continue
        except Exception as e:
            _release_concurrency(provider_id, sem)
            duration_ms = int((time.time() - start_time) * 1000)
            config.add_log(
                provider=provider["name"], model=target_model, source_model=model,
                input_tokens=0, output_tokens=0,
                status="error", duration_ms=duration_ms,
                error_msg=str(e), request_body=json.dumps(request_body, ensure_ascii=False),
                client_ip=client_ip,
            )
            last_response = _error_response_openai(str(e), 502)
            mark_degraded(mid, cfg.get("duration", 30))
            continue

        # 成功（含流式已开始）：非流式需在此释放；流式由 _stream_response_openai/generate() 自行释放
        if _is_success_response(response):
            if not stream:
                _release_concurrency(provider_id, sem)
            clear_degraded(mid)
            return response

        # 失败响应：非流式需在此释放；流式由 _stream_response_openai 内部释放
        if not stream:
            _release_concurrency(provider_id, sem)
        last_response = response
        mark_degraded(mid, cfg.get("duration", 30))
        continue
    else:
        # 防御性兜底：达到最大循环次数仍未结束，记录并退出
        print(f"[failover] 警告: 达到最大循环次数 {_max_loop_iterations} 仍未结束，强制退出。model={model}")

    # 所有候选尝试完仍失败
    if last_response is not None:
        return last_response
    return _error_response_openai(f"模型 '{model}' 没有可用的 OpenAI 提供商", 503)


def _proxy_openai_direct(request_body, provider, target_model, stream, sem=None, client_ip="", start_time=None, model_type="text", source_model="", model_max_tokens=0, role_rules=None, mapping_id=None, degradation_duration=0, reasoning_effort_supported=False):
    """OpenAI 格式直接转发到 provider 的 openai_url。

    full_path=1（默认）：配置的 openai_url 原样使用，不拼接任何后缀。
    full_path=0：配置的 openai_url 视为 base 路径，自动拼接 /chat/completions。

    role_rules: 来自模型映射的角色替换规则列表（如 developer→system），
                仅对当前 mapping 生效，在转发前应用到 messages。
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

    # 应用模型映射配置的角色替换（只对当前提供商/模型生效）
    _apply_role_replacement(body, role_rules)

    # 透传 reasoning_effort（仅 Responses→Chat 转换的请求体携带 _codex_reasoning_effort 私有键时生效；
    # 直接 Chat Completions 请求体无此键，调用为空操作，不污染直通路径）
    _apply_reasoning_effort(body, target_model, reasoning_effort_supported)

    # 文本模型需替换图片内容为文本提示，多模态模型保留图片
    if model_type != "multimodal":
        _strip_images_openai(body)

    # 模型配置了 max_tokens 时作为上限钳制：客户端未指定则填配置值，
    # 客户端传入超过配置值则裁剪到配置值，避免 provider 因超限返回 400
    if model_max_tokens > 0:
        cur = body.get("max_tokens")
        if not cur or cur <= 0:
            body["max_tokens"] = model_max_tokens
        elif cur > model_max_tokens:
            body["max_tokens"] = model_max_tokens

    if stream:
        return _stream_response_openai(url, headers, body, provider, target_model, sem, client_ip, start_time, source_model, mapping_id, degradation_duration)
    else:
        # 非流式：信号量由调用方（handle_openai_proxy_request）统一释放，本函数不再在 finally 中释放，
        # 避免 except 子句双重释放导致并发计数超出初始值
        resp = _post_with_retry(url, headers=headers, json=body, timeout=120)
        resp_json = resp.json()
        usage = resp_json.get("usage", {})
        # OpenAI 缓存命中 token 位于 prompt_tokens_details.cached_tokens
        prompt_details = usage.get("prompt_tokens_details") or {}
        cache_read_input_tokens = prompt_details.get("cached_tokens", 0)
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
            cache_read_input_tokens=cache_read_input_tokens,
        )
        if original_code == 200:
            _track_usage(provider.get("id", 0), usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), cache_read_input_tokens)
        return json.dumps(resp_json, ensure_ascii=False).encode("utf-8"), mapped_code, {"Content-Type": "application/json"}


def _stream_response_openai(url, headers, body, provider, target_model, sem=None, client_ip="", start_time=None, source_model="", mapping_id=None, degradation_duration=0):
    """OpenAI 流式直通转发

    alias / cfg 用于流式半失败时（上游在流中途出错，非连接级中断）的失败计数与降级触发。
    """
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
                            prompt_details = usage.get("prompt_tokens_details") or {}
                            cache_read_input_tokens = prompt_details.get("cached_tokens", 0)
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
                cache_read_input_tokens=cache_read_input_tokens,
            )
            if input_tokens > 0 or output_tokens > 0 or cache_read_input_tokens > 0 or cache_creation_input_tokens > 0:
                _track_usage(provider["id"], input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens)
            # 流式半失败：上游已开始 yield 但中途断开/出错，直接 mark_degraded 该 mapping。
            if status == "error" and mapping_id:
                mark_degraded(mapping_id, degradation_duration)

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


def _proxy_anthropic(request_body, provider, target_model, stream, sem=None, client_ip="", start_time=None, model_type="text", source_model="", model_max_tokens=0, role_rules=None, mapping_id=None, degradation_duration=0):
    """直接转发 Anthropic 格式请求到 provider 的 anthropic_url。

    full_path=1（默认）：配置的 anthropic_url 原样使用，不拼接任何后缀。
    full_path=0：配置的 anthropic_url 视为 base 路径，自动拼接 /v1/messages。

    role_rules: 来自模型映射的角色替换规则列表，在 system 提取前应用到 messages，
                使 developer→system 等替换结果能被正确归入顶层 system 字段。
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

    # 应用模型映射配置的角色替换（必须在 system 提取前执行）
    _apply_role_replacement(body, role_rules)

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

    # max_tokens 兜底与上限钳制：
    # - 客户端未传或 <=0：Claude Code 在 thinking 模式下不传 max_tokens，多数 Anthropic 兼容端点要求该字段；
    #   优先用模型配置值，否则用默认值 128000 兜底。
    # - 客户端传了超过模型配置值：裁剪到配置值，避免 provider 因超限返回 400
    cur = body.get("max_tokens")
    if not cur or cur <= 0:
        body["max_tokens"] = model_max_tokens if model_max_tokens > 0 else 128000
    elif model_max_tokens > 0 and cur > model_max_tokens:
        body["max_tokens"] = model_max_tokens

    # DeepSeek / MIMO 等兼容端点的思考模式参数适配
    provider_name = provider.get("name", "").lower()
    if "deepseek" in provider_name or "mimo" in provider_name:
        _adapt_deepseek_anthropic(body)

    # MiniMax 系列模型：清理 assistant 消息中 name 为空的无效 tool_use 块
    if "minimax" in target_model.lower():
        _adapt_minimax_anthropic(body)

    if stream:
        return _stream_response(url, headers, body, provider, target_model, sem, client_ip, start_time, source_model, mapping_id, degradation_duration)
    else:
        # 非流式：信号量由调用方（handle_proxy_request）统一释放，本函数不再在 finally 中释放，
        # 避免 except 子句双重释放导致并发计数超出初始值
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






# ---- 流式响应 ----

def _stream_response(url, headers, body, provider, target_model, sem=None, client_ip="", start_time=None, source_model="", mapping_id=None, degradation_duration=0):
    """返回一个生成器用于 SSE 流式转发（Anthropic 协议直转，原样转发上游 Anthropic SSE）

    alias / cfg 用于流式半失败时（上游在流中途出错，非连接级中断）的失败计数与降级触发。
    """
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
            # 流式半失败：上游已开始 yield 但中途断开/出错，直接 mark_degraded 该 mapping。
            if status == "error" and mapping_id:
                mark_degraded(mapping_id, degradation_duration)

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


# ---- OpenAI Responses API → Chat Completions 双向转换 ----

def _generate_response_id():
    """生成 Responses API 格式的响应 ID（resp_ 前缀 + 24 位随机字母数字）"""
    chars = "abcdefghijklmnopqrstuvwxyz0123456789"
    rand = "".join(random.choice(chars) for _ in range(24))
    return f"resp_{rand}"


def _error_response_responses(message, status_code):
    """构造 Responses API 格式的错误响应体，与 OpenAI 官方一致：{error:{type, code, message, param}}"""
    body = json.dumps({
        "error": {
            "type": "api_error",
            "code": str(status_code),
            "message": message,
            "param": None,
        },
    }, ensure_ascii=False).encode("utf-8")
    return body, status_code, {"Content-Type": "application/json"}


def _responses_content_part_to_openai(part):
    """将 Responses API 的 content_part 对象映射为 OpenAI Chat 格式的 content 元素。

    Responses content_part 类型与映射：
      - input_text → {type: "text", text: ...}
      - input_image → {type: "image_url", image_url: {url: ...}}
      - output_text → {type: "text", text: ...}（assistant 历史输出）
    """
    part_type = part.get("type", "")
    if part_type == "input_text":
        return {"type": "text", "text": part.get("text", "")}
    elif part_type == "input_image":
        image_url = part.get("image_url") or part.get("source") or {}
        if isinstance(image_url, dict):
            url = image_url.get("url", "")
        elif isinstance(image_url, str):
            url = image_url
        else:
            url = ""
        # 尝试 data:image 格式（detail 可选）
        detail = part.get("detail", "auto")
        return {"type": "image_url", "image_url": {"url": url, "detail": detail}}
    elif part_type == "output_text":
        return {"type": "text", "text": part.get("text", "")}
    else:
        # 未知类型 → 尝试按文本处理
        return {"type": "text", "text": json.dumps(part, ensure_ascii=False)}


def _responses_input_to_chat_messages(input_val, instructions=None):
    """将 Responses API 的 input 字段 + instructions 转为 Chat Completions 的 messages 列表。

    input 可以是：
      - 字符串 → 单条 user 消息
      - 数组 → 逐项映射：message 对象、function_call、function_call_output

    instructions → messages 开头插入 system 消息。
    返回 list[dict]。
    """
    messages = []

    # instructions → system 消息放在最前面
    # instructions 可能是字符串或 content_part 数组，统一拼接为字符串（Chat system content 多数上游仅接受 str）
    if instructions:
        if isinstance(instructions, list):
            text_parts = []
            for part in instructions:
                if isinstance(part, dict):
                    txt = part.get("text")
                    if isinstance(txt, str):
                        text_parts.append(txt)
                elif isinstance(part, str):
                    text_parts.append(part)
            instructions = "\n".join(text_parts) if text_parts else ""
        if instructions:
            messages.append({"role": "system", "content": instructions})

    if isinstance(input_val, str):
        messages.append({"role": "user", "content": input_val})
    elif isinstance(input_val, list):
        # 遍历 input 数组，维护当前 assistant 消息的 tool_calls 累积器
        pending_tool_calls = []  # 当前 assistant 消息的工具调用列表
        for item in input_val:
            if not isinstance(item, dict):
                continue

            item_type = item.get("type", "")

            # 跳过 Responses 专用、Chat 中无对应的类型（reasoning / web_search / file_search 等）
            if item_type in ("reasoning", "reasoning_summary", "web_search_call",
                             "file_search_call", "computer_call"):
                continue

            # function_call → 累积到当前 assistant 消息的 tool_calls
            if item_type == "function_call":
                pending_tool_calls.append({
                    "id": item.get("call_id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", ""),
                    },
                })
                continue

            # function_call_output → role:tool 消息
            if item_type == "function_call_output":
                # 先 flush 累积的 tool_calls（如果有的话）
                if pending_tool_calls:
                    messages.append({"role": "assistant", "tool_calls": pending_tool_calls})
                    pending_tool_calls = []
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id", ""),
                    "content": item.get("output", ""),
                })
                continue

            # 常规 message 对象：{role, content}
            # 先 flush 累积的 tool_calls
            if pending_tool_calls:
                messages.append({"role": "assistant", "tool_calls": pending_tool_calls})
                pending_tool_calls = []

            # 保留原始 role（含 system / user / assistant / developer 等）。
            # developer 等不兼容角色的归一化由模型映射的「角色映射」配置在转发层统一处理，
            # 这样将来上游恢复支持时无需改代码，只需在 UI 关闭替换即可。
            role = item.get("role", "user")
            content = item.get("content", "")

            # content 可能是 content_part 数组
            if isinstance(content, list):
                mapped_content = [_responses_content_part_to_openai(c) for c in content]
                messages.append({"role": role, "content": mapped_content})
            else:
                messages.append({"role": role, "content": content})

        # 遍历结束后 flush 剩余的 tool_calls
        if pending_tool_calls:
            messages.append({"role": "assistant", "tool_calls": pending_tool_calls})

    return messages


def _responses_tools_to_chat_tools(tools):
    """将 Responses API 的 tools 列表转为 Chat Completions 的 tools 列表。

    Responses 的 function 工具是扁平结构 {type:"function", name, description, parameters}，
    Chat Completions 是嵌套结构 {type:"function", function:{name, description, parameters}}。
    同时兼容少数客户端直接发送的嵌套形式（含 function 子键）。
    web_search 等类型在 Chat 中无对应，丢弃并打印警告。
    """
    if not tools:
        return None
    result = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        t_type = t.get("type", "function")
        if t_type == "function":
            # 优先取 Responses 扁平结构里的 name/description/parameters
            func = {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("parameters", {}),
            }
            # 兼容个别客户端发送的 Chat 嵌套形式
            if not func["name"] and isinstance(t.get("function"), dict):
                func = t["function"]
            if "strict" in t:
                func["strict"] = t["strict"]
            result.append({"type": "function", "function": func})
        else:
            print(f"[Responses→Chat] 丢弃不支持的工具类型: {t_type}")
    return result if result else None


def _convert_responses_to_chat(responses_body):
    """将 OpenAI Responses API 请求体转换为 Chat Completions 格式。

    映射规则：
      - instructions → messages[0] = {role: "system", content: instructions}
      - input(字符串) → [{role: "user", content: 字符串}]
      - input(数组) → 逐项映射为 messages；content_part 映射；function_call→tool_calls
      - max_output_tokens → max_tokens
      - reasoning / previous_response_id / store → 丢弃
      - model / stream / temperature / top_p / tools / tool_choice → 直接保留
    返回 dict（Chat Completions 格式的请求体）。
    """
    chat_body = {}

    # model / stream / temperature / top_p 直接保留
    for key in ("model", "stream", "temperature", "top_p"):
        if key in responses_body:
            chat_body[key] = responses_body[key]

    # max_output_tokens → max_tokens
    if "max_output_tokens" in responses_body:
        chat_body["max_tokens"] = responses_body["max_output_tokens"]

    # instructions + input → messages
    instructions = responses_body.get("instructions", "")
    input_val = responses_body.get("input", "")
    chat_body["messages"] = _responses_input_to_chat_messages(input_val, instructions)

    # tools（仅保留 function 类型）
    tools = _responses_tools_to_chat_tools(responses_body.get("tools"))
    if tools:
        chat_body["tools"] = tools

    # tool_choice 格式转换
    # Responses 扁平 {type:"function", name:"xxx"} → Chat 嵌套 {type:"function", function:{name:"xxx"}}
    # 字符串 "auto"/"none"/"required" 直接透传
    if "tool_choice" in responses_body:
        tc = responses_body["tool_choice"]
        if isinstance(tc, dict) and tc.get("type") == "function" and "name" in tc:
            chat_body["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
        else:
            chat_body["tool_choice"] = tc

    # 提取 reasoning.effort（如 "high"/"medium"/"low"）。
    # Codex CLI 会带 reasoning={effort:"high"}，但 Chat Completions 无对应顶层字段。
    # 此处无法确定 target_model（provider 尚未解析），故暂存到私有键，
    # 由 handle_openai_responses_request 的 failover 循环在每个候选 body=dict(chat_body)
    # 后按 target_model 映射为实际参数并删除该私有键。
    # 下划线前缀防止上游误收。
    reasoning_obj = responses_body.get("reasoning")
    if isinstance(reasoning_obj, dict):
        effort = reasoning_obj.get("effort")
        if isinstance(effort, str) and effort:
            chat_body["_codex_reasoning_effort"] = effort

    # 丢弃 previous_response_id / store
    # （Chat API 无对应字段，本代理无状态，不支持响应链持久化）

    return chat_body


def _convert_chat_to_responses(chat_json, request_model, response_id):
    """将 Chat Completions 非流式响应 JSON 转换为 Responses API 格式。

    映射规则（与流式 _stream_response_openai_to_responses 保持字段口径一致）：
      - reasoning：msg.reasoning_content / msg.reasoning → output[0] reasoning 项
      - choices[0].message.content → message 项 content[{type:"output_text"}]
      - choices[0].message.tool_calls → output 追加 function_call 项
      - call_id 兜底：上游无 id 时合成 call_{response_id}_{i}（与流式对齐）
      - status：finish_reason=='length' -> 'incomplete' + incomplete_details；否则 'completed'
      - end_turn：finish_reason=='tool_calls' -> False；否则 True
      - usage → {input_tokens, output_tokens, output_tokens_details{reasoning_tokens}, total_tokens}
      - 空输出兜底：output 为空时合成 status=failed 或空 message 项
    返回 dict。
    """
    now = int(time.time())
    choices = chat_json.get("choices", [])
    first = choices[0] if choices else {}
    msg = first.get("message", {}) if isinstance(first, dict) else {}
    finish_reason = first.get("finish_reason", "") if isinstance(first, dict) else ""

    # 构造 output 数组
    output = []

    # reasoning 输出项（推理模型在非流式 message.reasoning_content 中携带推理）
    reasoning_content = msg.get("reasoning_content")
    if not reasoning_content:
        # 兼容 msg.reasoning（字符串或对象）
        r = msg.get("reasoning")
        if isinstance(r, str):
            reasoning_content = r
        elif isinstance(r, dict):
            reasoning_content = r.get("content") or r.get("text") or ""
    if reasoning_content:
        output.append({
            "type": "reasoning",
            "id": response_id + "_rs",
            "summary": [{"type": "summary_text", "text": reasoning_content}],
            "status": "completed",
        })

    # 消息文本输出
    text_content = msg.get("content") or ""
    text_parts = []
    if isinstance(text_content, str) and text_content:
        text_parts.append({"type": "output_text", "text": text_content, "annotations": []})
    elif isinstance(text_content, list):
        # Chat 响应中 content 可能是 content_part 数组（少见，但兼容处理）
        for p in text_content:
            if isinstance(p, dict) and p.get("type") == "text":
                text_parts.append({"type": "output_text", "text": p.get("text", ""), "annotations": []})

    if text_parts:
        output.append({
            "type": "message",
            "id": response_id + "_msg",
            "role": "assistant",
            "status": "completed",
            "content": text_parts,
        })

    # 工具调用 → function_call 项
    # call_id 用上游 tool_call 的真实 id（客户端下一轮 function_call_output 据此引用）；
    # item.id 用枚举下标保证多个工具调用之间唯一（Chat 非流式响应的 tool_calls 没有 index 字段）。
    # call_id 兜底：上游无 id 时合成 call_{response_id}_{i}（与流式对齐，确保跨轮唯一）。
    tool_calls = msg.get("tool_calls") or []
    for i, tc in enumerate(tool_calls):
        func = tc.get("function", {})
        call_id = tc.get("id") or f"call_{response_id}_{i}"
        output.append({
            "type": "function_call",
            "id": f"{response_id}_fc_{i}",
            "call_id": call_id,
            "name": func.get("name", ""),
            "arguments": func.get("arguments", ""),
        })

    # usage 字段映射 + output_tokens_details
    usage = chat_json.get("usage", {})
    completion_tokens_details = usage.get("completion_tokens_details") or {}
    reasoning_tokens = completion_tokens_details.get("reasoning_tokens", 0)
    resp_usage = {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
        "total_tokens": usage.get("total_tokens", 0),
    }

    # status 映射：finish_reason=='length' -> 'incomplete' + incomplete_details
    if finish_reason == "length":
        status = "incomplete"
        incomplete_details = {"reason": "max_output_tokens"}
    else:
        status = "completed"
        incomplete_details = None

    # end_turn：finish_reason=='tool_calls' -> False（codex 需继续 follow_up）；否则 True
    end_turn = finish_reason != "tool_calls"

    # 空输出兜底
    if not output:
        if finish_reason in ("stop", "", None):
            # 上游返回空输出且无异常 finish_reason，返回 failed 让 codex 走错误路径
            return {
                "id": response_id,
                "object": "response",
                "created_at": now,
                "model": request_model,
                "status": "failed",
                "output": [],
                "error": {
                    "message": "Upstream returned empty output",
                    "type": "empty_output",
                    "code": "empty_output",
                },
                "usage": resp_usage,
            }
        # 其他 finish_reason 合成空 message 项保持 completed/incomplete
        output.append({
            "type": "message",
            "id": response_id + "_msg",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "", "annotations": []}],
        })

    result = {
        "id": response_id,
        "object": "response",
        "created_at": now,
        "model": request_model,
        "status": status,
        "output": output,
        "usage": resp_usage,
        "end_turn": end_turn,
    }
    if incomplete_details:
        result["incomplete_details"] = incomplete_details

    return result


def _stream_response_openai_to_responses(url, headers, body, provider, target_model,
                                          sem=None, client_ip="", start_time=None,
                                          source_model="", request_model="", mapping_id=None, degradation_duration=0):
    """消费上游 Chat Completions SSE 流，实时转换为 Responses API SSE 事件序列。

    事件序列：
      response.created
      → (有文本输出时) response.output_item.added(message) → response.content_part.added
      → response.output_text.delta (×N) → response.output_text.done → response.content_part.done → response.output_item.done
      → (有工具调用时) response.output_item.added(function_call) → response.function_call_arguments.delta (×N)
      → response.function_call_arguments.done → response.output_item.done
      → response.completed

    工具调用作为独立的 output_item（type:function_call）发送，而非塞进文本流，
    这样 Codex CLI 才能解析为结构化工具调用。

    关于 GeneratorExit：当客户端中途断开连接时，WSGI 会调用生成器的 close()，
    向当前 yield 点注入 GeneratorExit。在 finally 块中继续 yield 会抛
    "generator ignored GeneratorExit"。因此 finally 仅做资源清理（resp.close、
    信号量释放、日志），不再 yield；done/completed 事件在正常结束路径中发送。

    返回 (generator, status_code, headers)。
    """
    resp = _post_with_retry(url, headers=headers, json=body, stream=True, timeout=300)
    original_status_code = resp.status_code

    # 上游返回非 2xx，不作为流式转发，直接返回 Responses 格式错误
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
        # 解析上游 Chat 格式错误，包装为 Responses 错误格式
        try:
            err_json = json.loads(error_body)
            err_msg = err_json.get("error", {}).get("message", error_body[:200])
        except Exception:
            err_msg = error_body[:200]
        return _error_response_responses(err_msg, mapped_code)

    # 生成唯一的 response / item ID
    response_id = _generate_response_id()
    msg_item_id = response_id + "_msg"
    now = int(time.time())

    def _sse_event(event_type, data):
        """格式化一条 SSE 事件：event: <type>\\ndata: <json>\\n\\n"""
        return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def generate():
        response_chunks = []
        input_tokens = 0
        output_tokens = 0
        error_msg = ""
        full_text = ""
        # 文本 message 输出项是否已发送 output_item.added / content_part.added
        text_item_opened = False
        # 文本 message 输出项是否曾产生过（用于 final_output 组装，与 opened 区分）
        text_item_created = False
        # 工具调用累积器：{index: {"id","name","arguments","opened","closed","item_id"}}
        tool_calls_acc = {}
        tool_call_order = []  # 按 index 出现顺序记录，便于有序输出

        # ---- reasoning 状态变量 ----
        reasoning_item_id = response_id + "_rs"
        reasoning_text = ""
        reasoning_opened = False
        reasoning_output_index = None  # reasoning 项的 output_index，opened 时分配

        # ---- finish_reason 捕获 ----
        finish_reason = None

        # ---- 动态 output_index 计数器 ----
        # 替换原来 text=0 / tools=1+idx 的硬编码，确保 reasoning/text/tools 交叉场景下 index 连续递增
        next_output_index = 0

        # ---- usage 详情（reasoning_tokens 等）----
        usage_data_ref = {}  # 保存最近一次 usage chunk，供 completed 时提取详情

        try:
            # 1) response.created
            yield _sse_event("response.created", {
                "type": "response.created",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "created_at": now,
                    "model": request_model,
                    "status": "in_progress",
                    "output": [],
                    "usage": None,
                },
            })

            # 2) 读取上游 Chat SSE 流
            for line in resp.iter_lines():
                if not line:
                    continue
                decoded = line.decode("utf-8", errors="replace")
                response_chunks.append(decoded)

                if not decoded.startswith("data:"):
                    continue
                if decoded.strip().endswith("[DONE]"):
                    break
                try:
                    chunk = json.loads(_parse_sse_data(decoded))
                except (json.JSONDecodeError, IndexError):
                    continue

                # 提取 usage（通常在最后一个 chunk）
                usage_data = chunk.get("usage")
                if usage_data:
                    input_tokens = usage_data.get("prompt_tokens", 0)
                    output_tokens = usage_data.get("completion_tokens", 0)
                    usage_data_ref = usage_data

                choices = chunk.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {})

                # 捕获 finish_reason
                fr = choices[0].get("finish_reason")
                if fr:
                    finish_reason = fr

                # ---- reasoning_content 分支（在 content 之前处理）----
                # 兼容多种上游字段名：delta.reasoning_content / delta.reasoning(字符串)
                reasoning_delta = delta.get("reasoning_content")
                if not reasoning_delta and isinstance(delta.get("reasoning"), str):
                    reasoning_delta = delta["reasoning"]
                if reasoning_delta:
                    if not reasoning_opened:
                        # 首次有 reasoning 输出，发送 reasoning 输出项的 added 事件
                        reasoning_output_index = next_output_index
                        next_output_index += 1
                        yield _sse_event("response.output_item.added", {
                            "type": "response.output_item.added",
                            "output_index": reasoning_output_index,
                            "item": {
                                "type": "reasoning",
                                "id": reasoning_item_id,
                                "summary": [],
                                "status": "in_progress",
                            },
                        })
                        yield _sse_event("response.reasoning_summary_part.added", {
                            "type": "response.reasoning_summary_part.added",
                            "item_id": reasoning_item_id,
                            "output_index": reasoning_output_index,
                            "summary_index": 0,
                            "part": {"type": "summary_text", "text": ""},
                        })
                        reasoning_opened = True
                    reasoning_text += reasoning_delta
                    yield _sse_event("response.reasoning_summary_text.delta", {
                        "type": "response.reasoning_summary_text.delta",
                        "item_id": reasoning_item_id,
                        "output_index": reasoning_output_index,
                        "summary_index": 0,
                        "delta": reasoning_delta,
                    })

                # ---- 文本内容 delta ----
                content_delta = delta.get("content", "")
                if content_delta:
                    # 若 reasoning 项尚未关闭，先 flush reasoning
                    if reasoning_opened:
                        yield _sse_event("response.reasoning_summary_text.done", {
                            "type": "response.reasoning_summary_text.done",
                            "item_id": reasoning_item_id,
                            "output_index": reasoning_output_index,
                            "summary_index": 0,
                            "text": reasoning_text,
                        })
                        yield _sse_event("response.reasoning_summary_part.done", {
                            "type": "response.reasoning_summary_part.done",
                            "item_id": reasoning_item_id,
                            "output_index": reasoning_output_index,
                            "summary_index": 0,
                            "part": {"type": "summary_text", "text": reasoning_text},
                        })
                        yield _sse_event("response.output_item.done", {
                            "type": "response.output_item.done",
                            "output_index": reasoning_output_index,
                            "item": {
                                "type": "reasoning",
                                "id": reasoning_item_id,
                                "summary": [{"type": "summary_text", "text": reasoning_text}],
                                "status": "completed",
                            },
                        })
                        reasoning_opened = False

                    if not text_item_opened:
                        # 首次有文本输出，发送 message 输出项的 added 事件
                        text_output_index = next_output_index
                        next_output_index += 1
                        yield _sse_event("response.output_item.added", {
                            "type": "response.output_item.added",
                            "output_index": text_output_index,
                            "item": {
                                "type": "message",
                                "id": msg_item_id,
                                "role": "assistant",
                                "status": "in_progress",
                                "content": [],
                            },
                        })
                        yield _sse_event("response.content_part.added", {
                            "type": "response.content_part.added",
                            "item_id": msg_item_id,
                            "output_index": text_output_index,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": "", "annotations": []},
                        })
                        text_item_opened = True
                        text_item_created = True
                        # 记住 text 项的 output_index，后续 delta/done 需一致
                        text_output_index_ref = text_output_index
                    else:
                        text_output_index_ref = text_output_index_ref  # 保持已有值
                    full_text += content_delta
                    yield _sse_event("response.output_text.delta", {
                        "type": "response.output_text.delta",
                        "item_id": msg_item_id,
                        "output_index": text_output_index_ref,
                        "content_index": 0,
                        "delta": content_delta,
                    })

                # ---- 工具调用 delta ----
                tc_delta = delta.get("tool_calls")
                if tc_delta:
                    # 若 reasoning 项尚未关闭，先 flush reasoning
                    if reasoning_opened:
                        yield _sse_event("response.reasoning_summary_text.done", {
                            "type": "response.reasoning_summary_text.done",
                            "item_id": reasoning_item_id,
                            "output_index": reasoning_output_index,
                            "summary_index": 0,
                            "text": reasoning_text,
                        })
                        yield _sse_event("response.reasoning_summary_part.done", {
                            "type": "response.reasoning_summary_part.done",
                            "item_id": reasoning_item_id,
                            "output_index": reasoning_output_index,
                            "summary_index": 0,
                            "part": {"type": "summary_text", "text": reasoning_text},
                        })
                        yield _sse_event("response.output_item.done", {
                            "type": "response.output_item.done",
                            "output_index": reasoning_output_index,
                            "item": {
                                "type": "reasoning",
                                "id": reasoning_item_id,
                                "summary": [{"type": "summary_text", "text": reasoning_text}],
                                "status": "completed",
                            },
                        })
                        reasoning_opened = False

                    # 若 text 项尚未关闭，先 flush text（场景 C：文本+工具交叉）
                    if text_item_opened:
                        yield _sse_event("response.output_text.done", {
                            "type": "response.output_text.done",
                            "item_id": msg_item_id,
                            "output_index": text_output_index_ref,
                            "content_index": 0,
                            "text": full_text,
                        })
                        yield _sse_event("response.content_part.done", {
                            "type": "response.content_part.done",
                            "item_id": msg_item_id,
                            "output_index": text_output_index_ref,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": full_text, "annotations": []},
                        })
                        yield _sse_event("response.output_item.done", {
                            "type": "response.output_item.done",
                            "output_index": text_output_index_ref,
                            "item": {
                                "type": "message",
                                "id": msg_item_id,
                                "role": "assistant",
                                "status": "completed",
                                "content": [
                                    {"type": "output_text", "text": full_text, "annotations": []}
                                ],
                            },
                        })
                        text_item_opened = False

                    for tc in tc_delta:
                        idx = tc.get("index", 0)
                        func = tc.get("function", {}) or {}
                        tc_name = func.get("name", "")
                        tc_args_delta = func.get("arguments", "")
                        tc_id = tc.get("id", "")

                        if idx not in tool_calls_acc:
                            # 新工具调用，发送 output_item.added(function_call)
                            tc_item_id = f"{response_id}_fc_{idx}"
                            # call_id 兜底：非 OpenAI 上游首帧常不带 id，合成确保非空
                            if not tc_id:
                                tc_id = f"call_{response_id}_{idx}"
                            fc_output_index = next_output_index
                            next_output_index += 1
                            tool_calls_acc[idx] = {
                                "id": tc_item_id,
                                "call_id": tc_id,
                                "call_id_synthesized": True,  # 标记标记：合成 id 不被后续真实 id 覆盖
                                "name": tc_name,
                                "arguments": "",
                                "opened": True,
                                "output_index": fc_output_index,
                            }
                            tool_call_order.append(idx)
                            yield _sse_event("response.output_item.added", {
                                "type": "response.output_item.added",
                                "output_index": fc_output_index,
                                "item": {
                                    "type": "function_call",
                                    "id": tc_item_id,
                                    "call_id": tc_id,
                                    "name": tc_name,
                                    "arguments": "",
                                    "status": "in_progress",
                                },
                            })
                        else:
                            acc = tool_calls_acc[idx]
                            # 若已有合成 call_id，保留首个合成 id 不被后续真实 id 覆盖
                            # （若上游后续帧带了真实 id，则用真实 id 替换合成 id）
                            if tc_id:
                                if acc.get("call_id_synthesized"):
                                    # 合成 id 被真实 id 替换
                                    acc["call_id"] = tc_id
                                    acc["call_id_synthesized"] = False
                                elif not acc["call_id"]:
                                    acc["call_id"] = tc_id
                            if tc_name and not acc["name"]:
                                acc["name"] = tc_name

                        # arguments 增量
                        if tc_args_delta:
                            tool_calls_acc[idx]["arguments"] += tc_args_delta
                            yield _sse_event("response.function_call_arguments.delta", {
                                "type": "response.function_call_arguments.delta",
                                "item_id": tool_calls_acc[idx]["id"],
                                "output_index": tool_calls_acc[idx]["output_index"],
                                "delta": tc_args_delta,
                            })

            # 3) 流正常结束：依次关闭各输出项（reasoning → text → tools），最后发 completed

            # 关闭 reasoning 输出项
            if reasoning_opened:
                yield _sse_event("response.reasoning_summary_text.done", {
                    "type": "response.reasoning_summary_text.done",
                    "item_id": reasoning_item_id,
                    "output_index": reasoning_output_index,
                    "summary_index": 0,
                    "text": reasoning_text,
                })
                yield _sse_event("response.reasoning_summary_part.done", {
                    "type": "response.reasoning_summary_part.done",
                    "item_id": reasoning_item_id,
                    "output_index": reasoning_output_index,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": reasoning_text},
                })
                yield _sse_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": reasoning_output_index,
                    "item": {
                        "type": "reasoning",
                        "id": reasoning_item_id,
                        "summary": [{"type": "summary_text", "text": reasoning_text}],
                        "status": "completed",
                    },
                })
                reasoning_opened = False

            # 关闭文本 message 输出项
            if text_item_opened:
                yield _sse_event("response.output_text.done", {
                    "type": "response.output_text.done",
                    "item_id": msg_item_id,
                    "output_index": text_output_index_ref,
                    "content_index": 0,
                    "text": full_text,
                })
                yield _sse_event("response.content_part.done", {
                    "type": "response.content_part.done",
                    "item_id": msg_item_id,
                    "output_index": text_output_index_ref,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": full_text, "annotations": []},
                })
                yield _sse_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": text_output_index_ref,
                    "item": {
                        "type": "message",
                        "id": msg_item_id,
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": full_text, "annotations": []}
                        ],
                    },
                })
                text_item_opened = False

            # 关闭各工具调用输出项
            for idx in tool_call_order:
                acc = tool_calls_acc[idx]
                fc_output_index = acc["output_index"]
                yield _sse_event("response.function_call_arguments.done", {
                    "type": "response.function_call_arguments.done",
                    "item_id": acc["id"],
                    "output_index": fc_output_index,
                    "arguments": acc["arguments"],
                })
                yield _sse_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": fc_output_index,
                    "item": {
                        "type": "function_call",
                        "id": acc["id"],
                        "call_id": acc["call_id"],
                        "name": acc["name"],
                        "arguments": acc["arguments"],
                        "status": "completed",
                    },
                })

            # 组装最终 output 数组
            final_output = []
            if reasoning_text:
                final_output.append({
                    "type": "reasoning",
                    "id": reasoning_item_id,
                    "summary": [{"type": "summary_text", "text": reasoning_text}],
                    "status": "completed",
                })
            if text_item_created:
                final_output.append({
                    "type": "message",
                    "id": msg_item_id,
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": full_text, "annotations": []}
                    ],
                })
            for idx in tool_call_order:
                acc = tool_calls_acc[idx]
                final_output.append({
                    "type": "function_call",
                    "id": acc["id"],
                    "call_id": acc["call_id"],
                    "name": acc["name"],
                    "arguments": acc["arguments"],
                    "status": "completed",
                })

            # 空输出兜底：上游只产 reasoning（无 content 无 tool_calls）且 reasoning 已 flush 到 final_output，
            # 此时 final_output 非空（含 reasoning 项），不算空输出。
            # 真正的空输出：final_output 为空且无 reasoning_text 且无 full_text 且无 tool_call_order
            if not final_output and not reasoning_text and not full_text and not tool_call_order:
                if finish_reason in ("stop", None):
                    # 上游返回空输出且无异常 finish_reason，发 response.failed 让 codex 走错误路径
                    yield _sse_event("response.failed", {
                        "type": "response.failed",
                        "response": {
                            "id": response_id,
                            "object": "response",
                            "created_at": now,
                            "model": request_model,
                            "status": "failed",
                            "output": [],
                            "error": {
                                "message": "Upstream returned empty output",
                                "type": "empty_output",
                                "code": "empty_output",
                            },
                            "usage": {
                                "input_tokens": input_tokens,
                                "output_tokens": output_tokens,
                                "total_tokens": input_tokens + output_tokens,
                            },
                        },
                    })
                    # 跳过 response.completed
                    return
                # 其他 finish_reason（如 length）走 completed + incomplete

            # 4) response.completed
            # end_turn: finish_reason=='tool_calls' -> False（codex 需继续 follow_up）；否则 True
            end_turn = finish_reason != "tool_calls" if finish_reason else True

            # status 映射：finish_reason=='length' -> 'incomplete' + incomplete_details
            if finish_reason == "length":
                completed_status = "incomplete"
                incomplete_details = {"reason": "max_output_tokens"}
            else:
                completed_status = "completed"
                incomplete_details = None

            # output_tokens_details: 提取 reasoning_tokens
            completion_tokens_details = usage_data_ref.get("completion_tokens_details") or {}
            reasoning_tokens = completion_tokens_details.get("reasoning_tokens", 0)

            completed_response = {
                "id": response_id,
                "object": "response",
                "created_at": now,
                "model": request_model,
                "status": completed_status,
                "output": final_output,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
                    "total_tokens": input_tokens + output_tokens,
                },
            }
            if end_turn is not None:
                completed_response["end_turn"] = end_turn
            if incomplete_details:
                completed_response["incomplete_details"] = incomplete_details

            yield _sse_event("response.completed", {
                "type": "response.completed",
                "response": completed_response,
            })

        except GeneratorExit:
            # 客户端断开连接，WSGI 调 close() 注入 GeneratorExit。
            # 此时不能 yield（会抛 "generator ignored GeneratorExit"），只做资源清理后重新抛出。
            raise
        except _CONN_ABORT_ERRORS:
            # 连接级异常（上游断开/超时/客户端断开），属于正常中断
            pass
        except Exception as e:
            error_msg = str(e)
        finally:
            # 仅清理资源，不 yield（避免在 GeneratorExit 上下文中 yield 抛 RuntimeError）
            try:
                resp.close()
            except Exception:
                pass
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
            )
            if input_tokens > 0 or output_tokens > 0:
                _track_usage(provider["id"], input_tokens, output_tokens)
            # 流式半失败：上游已开始 yield 但中途断开/出错，直接 mark_degraded 该 mapping。
            if status == "error" and mapping_id:
                mark_degraded(mapping_id, degradation_duration)

    return generate(), resp.status_code, {"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def handle_openai_responses_request(request_body, client_ip=""):
    """OpenAI Responses API 代理主入口。

    流程：
      1. 提取 model / stream
      2. 调用 _convert_responses_to_chat 将 Responses 请求体转为 Chat Completions 格式
      3. 复用 _resolve_openai_provider 查找候选池（在转换请求体后才解析 provider）
      4. failover 循环：计费检查 + 并发信号量 + 调用上游 + 响应转换
      5. 返回 (content, status_code, headers)
    """
    model = request_body.get("model", "")
    stream = request_body.get("stream", False)
    request_model = model  # 保留原始请求的 model，用于响应中回写

    # 将 Responses 请求体转为 Chat Completions 格式
    chat_body = _convert_responses_to_chat(request_body)

    # 校验 messages 非空（input 缺失或为空时，上游会因缺少 user 消息而报 400，此处提前拦截）
    if not chat_body.get("messages"):
        return _error_response_responses("input 字段不能为空", 400)

    # 复用 provider 解析逻辑（以转换后的 chat_body 作为请求体，保证图片检测正确）
    candidates, error_message = _resolve_openai_provider(chat_body, client_ip)
    if candidates is None:
        return _error_response_responses(error_message, 404 if "未找到" in error_message or "未配置" in error_message else 429)

    cfg = get_degradation_config()
    attempted = set()  # 已尝试的 mapping id（同一请求内不重复尝试）
    last_response = None  # 最近一次失败响应，全部尝试完后返回
    _max_loop_iterations = 100  # 防御性兜底：防止极端退化场景下的无限循环

    for _loop_iter in range(_max_loop_iterations):
        # 从未尝试过的候选中按降级状态过滤，为空则回退到未过滤列表
        untried = [c for c in candidates if c["mapping_id"] not in attempted]
        if not untried:
            break
        filtered = _filter_candidates_by_degradation(untried, cfg)
        if not filtered:
            break
        chosen = _pick_weighted_round_robin(filtered, client_ip, model)
        if chosen is None:
            break
        mid = chosen.get("mapping_id")
        attempted.add(mid)
        alias = chosen.get("alias")

        provider = chosen["provider"]
        target_model = chosen["target_model"]
        model_type = chosen["model_type"]
        model_max_tokens = chosen["model_max_tokens"]
        role_rules = chosen["role_rules"]
        reasoning_effort_supported = chosen.get("reasoning_effort_supported", 0)
        provider_id = provider.get("id", 0)
        max_concurrency = provider.get("max_concurrency", 0)

        # 计费检查：超限则跳过（不降级，仅加入 attempted）
        billing_check = config.check_provider_billing(provider_id)
        if not billing_check["allowed"]:
            last_response = _error_response_responses(
                f"提供商 '{provider['name']}' 已被限制: {billing_check['reason']}", 429
            )
            continue
        if billing_check["near_limit"]:
            print(f"[计费警告] 提供商 '{provider['name']}' 使用量已达 {billing_check['usage_percent']:.0%}")

        start_time = time.time()
        sem = _get_semaphore(provider_id, max_concurrency)
        if sem:
            sem.acquire()
        _track_request_start(provider_id)

        # 信号量释放由本处统一负责：
        # - 非流式：_proxy_openai_direct 不再在内部 finally 释放，由 except/成功/失败响应 路径释放
        # - 流式：_stream_response_openai_to_responses 在 status>=400 时内部释放，或由 generate() finally 释放；
        #   仅当 _post_with_retry 抛出异常时由本处 except 释放
        sem_released = False

        try:
            if stream:
                # 流式：按当前候选构造请求体（每个候选可能有不同的角色映射/模型限制）
                body = dict(chat_body)
                _apply_role_replacement(body, role_rules)
                body["model"] = target_model
                # 透传 reasoning_effort（Codex CLI 的 model_reasoning_effort=high 在 _convert_responses_to_chat 已暂存到私有键）
                _apply_reasoning_effort(body, target_model, reasoning_effort_supported)
                if model_type != "multimodal":
                    _strip_images_openai(body)
                # 模型 max_tokens 上限钳制
                if model_max_tokens > 0:
                    cur = body.get("max_tokens")
                    if not cur or cur <= 0:
                        body["max_tokens"] = model_max_tokens
                    elif cur > model_max_tokens:
                        body["max_tokens"] = model_max_tokens
                # 流式模式注入 stream_options.include_usage，确保上游在末帧带 usage
                body["stream"] = True
                body["stream_options"] = {"include_usage": True}

                url = provider["openai_url"].rstrip("/")
                if not provider.get("full_path", 1) and not url.endswith("/chat/completions"):
                    url += "/chat/completions"
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {provider['api_key']}",
                }
                # 流式：generate() finally 或 status>=400 分支负责释放
                # 注意：sem_released = True 必须放在调用之后，否则 _post_with_retry 抛错时
                # 外层 except 会因 sem_released=True 而跳过信号量兜底释放，导致资源泄漏。
                response = _stream_response_openai_to_responses(
                    url, headers, body, provider, target_model,
                    sem=sem, client_ip=client_ip, start_time=start_time,
                    source_model=model, request_model=request_model,
                    mapping_id=mid, degradation_duration=cfg.get("duration", 30),
                )
                # 调用返回即代表信号量已被 generate() finally 或 status>=400 分支接管
                sem_released = True
            else:
                # 非流式：_proxy_openai_direct 不再在内部 finally 释放，本处负责释放
                content, status_code, _ = _proxy_openai_direct(
                    chat_body, provider, target_model, False,
                    sem=sem, client_ip=client_ip, start_time=start_time,
                    model_type=model_type, source_model=model,
                    model_max_tokens=model_max_tokens,
                    role_rules=role_rules,
                    mapping_id=mid, degradation_duration=cfg.get("duration", 30),
                    reasoning_effort_supported=reasoning_effort_supported,
                )
                # 非流式调用返回即代表不再持有信号量
                sem_released = True
                _release_concurrency(provider_id, sem)
                if status_code >= 400:
                    # 上游返回错误（任何 4xx/5xx），将 Chat 格式错误转为 Responses 格式
                    try:
                        err_json = json.loads(content.decode("utf-8"))
                        err_msg = err_json.get("error", {}).get("message", content.decode("utf-8")[:200])
                    except Exception:
                        err_msg = content.decode("utf-8", errors="replace")[:200]
                    response = _error_response_responses(err_msg, status_code)
                else:
                    # 上游成功，将 Chat 响应转为 Responses 格式
                    response_id = _generate_response_id()
                    chat_json = json.loads(content.decode("utf-8"))
                    responses_json = _convert_chat_to_responses(chat_json, request_model, response_id)
                    response = (json.dumps(responses_json, ensure_ascii=False).encode("utf-8"), 200, {"Content-Type": "application/json"})

        except Exception as e:
            # 仅在信号量尚未被释放时兜底（调用前的代码路径抛错）
            if not sem_released:
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
                last_response = _error_response_responses(msg, 502)
            else:
                config.add_log(
                    provider=provider["name"], model=target_model, source_model=model,
                    input_tokens=0, output_tokens=0,
                    status="error", duration_ms=duration_ms,
                    error_msg=str(e), request_body=json.dumps(request_body, ensure_ascii=False),
                    client_ip=client_ip,
                )
                last_response = _error_response_responses(str(e), 502)
            mark_degraded(mid, cfg.get("duration", 30))
            continue

        # 成功（含流式已开始）：清除降级状态并返回
        if _is_success_response(response):
            clear_degraded(mid)
            return response

        # 失败响应：降级并继续循环
        last_response = response
        mark_degraded(mid, cfg.get("duration", 30))
        continue
    else:
        # 防御性兜底：达到最大循环次数仍未结束，记录并退出
        print(f"[failover] 警告: 达到最大循环次数 {_max_loop_iterations} 仍未结束，强制退出。model={model}")

    # 所有候选尝试完仍失败
    if last_response is not None:
        return last_response
    return _error_response_responses(f"模型 '{model}' 没有可用的 OpenAI 提供商", 503)