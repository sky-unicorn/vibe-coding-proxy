"""MCP 协议层：JSON-RPC 2.0 解析、工具注册与分发。

实现 MCP Streamable HTTP 传输（protocol version 2025-06-18），
仅支持 tools-only 场景，不做 session 管理。

工具参数用 snake_case（对 AI 友好），nacos_client 内部做 camelCase 映射。
"""

import json

import nacos_client
from version import APP_VERSION


# ---- 工具注册表 ----

TOOL_REGISTRY = {
    "nacos_list_namespaces": {
        "description": "列出 Nacos 全部命名空间（含 public）",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": nacos_client.list_namespaces,
    },
    "nacos_create_namespace": {
        "description": "创建 Nacos 命名空间；namespace_id 省略时由服务端自动生成",
        "inputSchema": {
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "命名空间 ID（可选，省略则服务端自动生成）",
                },
                "namespace_name": {
                    "type": "string",
                    "description": "命名空间名称（必填）",
                },
                "namespace_desc": {
                    "type": "string",
                    "description": "命名空间描述（可选）",
                },
            },
            "required": ["namespace_name"],
        },
        "handler": nacos_client.create_namespace,
    },
    "nacos_update_namespace": {
        "description": "修改 Nacos 命名空间名称或描述（namespace_id 不可改）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "命名空间 ID（必填）",
                },
                "namespace_name": {
                    "type": "string",
                    "description": "命名空间名称（必填）",
                },
                "namespace_desc": {
                    "type": "string",
                    "description": "命名空间描述（可选）",
                },
            },
            "required": ["namespace_id", "namespace_name"],
        },
        "handler": nacos_client.update_namespace,
    },
    "nacos_delete_namespace": {
        "description": "删除 Nacos 命名空间（public 不可删，Nacos 侧拦截）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "命名空间 ID（必填）",
                },
            },
            "required": ["namespace_id"],
        },
        "handler": nacos_client.delete_namespace,
    },
    "nacos_list_configs": {
        "description": "分页查询 Nacos 配置列表，支持按 data_id/group 模糊或精确搜索",
        "inputSchema": {
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "命名空间 ID（空串或 public 表示 public 命名空间）",
                },
                "data_id": {
                    "type": "string",
                    "description": "配置 Data ID（可选，用于搜索过滤）",
                },
                "group": {
                    "type": "string",
                    "description": "配置 Group（可选，用于搜索过滤）",
                },
                "page_no": {
                    "type": "integer",
                    "description": "页码，默认 1",
                    "default": 1,
                },
                "page_size": {
                    "type": "integer",
                    "description": "每页条数，默认 10",
                    "default": 10,
                },
                "search": {
                    "type": "string",
                    "description": "搜索模式：blur（模糊，默认）或 accurate（精确）",
                    "enum": ["blur", "accurate"],
                    "default": "blur",
                },
            },
            "required": [],
        },
        "handler": nacos_client.list_configs,
    },
    "nacos_get_config": {
        "description": "读取单个 Nacos 配置的完整内容（含 type、md5）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "命名空间 ID（空串或 public 表示 public 命名空间）",
                },
                "data_id": {
                    "type": "string",
                    "description": "配置 Data ID（必填）",
                },
                "group": {
                    "type": "string",
                    "description": "配置 Group（必填）",
                },
            },
            "required": ["data_id", "group"],
        },
        "handler": nacos_client.get_config,
    },
    "nacos_publish_config": {
        "description": "发布 Nacos 配置（存在即覆盖，一个接口同时覆盖新增与修改）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "命名空间 ID（空串或 public 表示 public 命名空间）",
                },
                "data_id": {
                    "type": "string",
                    "description": "配置 Data ID（必填）",
                },
                "group": {
                    "type": "string",
                    "description": "配置 Group（必填）",
                },
                "content": {
                    "type": "string",
                    "description": "配置内容（必填，可能是大段 YAML/JSON/Properties 等）",
                },
                "type": {
                    "type": "string",
                    "description": "配置格式：text/json/yaml/properties/xml/html，默认 text",
                    "enum": ["text", "json", "yaml", "properties", "xml", "html"],
                    "default": "text",
                },
                "app_name": {
                    "type": "string",
                    "description": "应用名（可选）",
                },
                "desc": {
                    "type": "string",
                    "description": "配置描述（可选）",
                },
                "tags": {
                    "type": "string",
                    "description": "配置标签（可选，逗号分隔）",
                },
            },
            "required": ["data_id", "group", "content"],
        },
        "handler": nacos_client.publish_config,
    },
    "nacos_delete_config": {
        "description": "删除单个 Nacos 配置",
        "inputSchema": {
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "命名空间 ID（空串或 public 表示 public 命名空间）",
                },
                "data_id": {
                    "type": "string",
                    "description": "配置 Data ID（必填）",
                },
                "group": {
                    "type": "string",
                    "description": "配置 Group（必填）",
                },
            },
            "required": ["data_id", "group"],
        },
        "handler": nacos_client.delete_config,
    },
    "nacos_get_config_history": {
        "description": "查询 Nacos 配置历史版本（便于回滚定位）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "namespace_id": {
                    "type": "string",
                    "description": "命名空间 ID（空串或 public 表示 public 命名空间）",
                },
                "data_id": {
                    "type": "string",
                    "description": "配置 Data ID（必填）",
                },
                "group": {
                    "type": "string",
                    "description": "配置 Group（必填）",
                },
                "page_no": {
                    "type": "integer",
                    "description": "页码，默认 1",
                    "default": 1,
                },
                "page_size": {
                    "type": "integer",
                    "description": "每页条数，默认 10",
                    "default": 10,
                },
            },
            "required": ["data_id", "group"],
        },
        "handler": nacos_client.get_config_history,
    },
}


# ---- JSON-RPC 处理 ----

def _tool_error(msg, request_id):
    """构造 tools/call 错误响应（isError: true）。"""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [{"type": "text", "text": f"[Nacos 错误] {msg}"}],
            "isError": True,
        },
    }


def _handle_tools_call(request_id, params, nacos_conn):
    """处理 tools/call 请求。"""
    tool_name = params.get("name", "")
    arguments = params.get("arguments") or {}

    # 可用性检查：nacos_conn 由调用方从 HTTP 请求头构造，未传或缺 console_url 视为未配置
    if not nacos_conn or not nacos_conn.get("console_url"):
        return _tool_error(
            "未配置 Nacos 连接参数，请在 MCP 客户端配置中携带 "
            "X-Nacos-Console-Url / X-Nacos-Auth-Url / X-Nacos-Username / X-Nacos-Password 请求头",
            request_id,
        )

    # 查找工具
    tool_def = TOOL_REGISTRY.get(tool_name)
    if not tool_def:
        return _tool_error(f"未知工具: {tool_name}", request_id)

    # 必填参数校验
    schema = tool_def["inputSchema"]
    required = schema.get("required", [])
    missing = [r for r in required if r not in arguments or arguments[r] is None]
    if missing:
        return _tool_error(f"缺少必填参数: {', '.join(missing)}", request_id)

    # 调用 handler：nacos_conn 作为首参传入
    try:
        result = tool_def["handler"](nacos_conn, **arguments)
        # 成功响应
        text = json.dumps(result, ensure_ascii=False, indent=2) if result is not None else "true"
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            },
        }
    except nacos_client.NacosError as e:
        return _tool_error(str(e), request_id)
    except Exception as e:
        return _tool_error(f"内部错误: {e}", request_id)


def list_tools():
    """返回工具清单（供 tools/list 方法使用）。"""
    tools = []
    for name, defn in TOOL_REGISTRY.items():
        tools.append({
            "name": name,
            "description": defn["description"],
            "inputSchema": defn["inputSchema"],
        })
    return tools


def handle_jsonrpc(request_dict, nacos_conn):
    """处理单个 JSON-RPC 请求，返回响应 dict 或 None（notification）。

    nacos_conn: 由调用方从 HTTP 请求头构造的 dict，包含 console_url /
    auth_url / username / password；None 表示未携带任何 Nacos 头。

    None 返回值表示 notification，调用方应回 202。
    """
    # 基本格式校验
    if not isinstance(request_dict, dict):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request: 期望 JSON 对象"},
        }

    jsonrpc_ver = request_dict.get("jsonrpc")
    if jsonrpc_ver != "2.0":
        return {
            "jsonrpc": "2.0",
            "id": request_dict.get("id"),
            "error": {"code": -32600, "message": "Invalid Request: jsonrpc 必须为 '2.0'"},
        }

    method = request_dict.get("method", "")
    request_id = request_dict.get("id")
    params = request_dict.get("params") or {}

    # notification（无 id）的处理
    if request_id is None:
        # notifications/initialized -> 无响应
        if method == "notifications/initialized":
            return None
        # 其他 notification 也忽略
        return None

    # 请求-响应类方法
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "ai-api-proxy-nacos",
                    "version": APP_VERSION,
                },
            },
        }

    if method == "ping":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {},
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": list_tools()},
        }

    if method == "tools/call":
        return _handle_tools_call(request_id, params, nacos_conn)

    # 未知方法
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }
