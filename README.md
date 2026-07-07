# Vibe Coding 服务转发

一个基于 Flask 的多供应商 AI API 代理服务，统一对外暴露 Anthropic Messages API 与 OpenAI Chat Completions API 两种接口，底层可对接任意兼容的 LLM 提供商。配套 Web 管理界面，可对提供商、模型映射、错误码、日志、计费、API Key 等进行可视化配置。

---

## 一、项目作用

- **统一代理入口**：把多家上游 LLM（Anthropic 官方、自建网关、OpenAI 兼容服务等）抽象成统一的 Anthropic / OpenAI 接口，下游客户端无需关心上游差异。
- **模型路由**：按客户端请求的 `model` 字段（别名）路由到真实的目标模型和提供商，支持多候选分组、加权轮询、自动跳过超限节点。
- **资源治理**：内置并发限流、错误码映射、计费限额（5h / 周 / 月 / 余额）以及自动日志清理，避免某个提供商被滥用或意外超支。
- **可观测性**：完整记录每次请求的时间、IP、模型、token、耗时、状态码、请求/响应体，便于排查问题。
- **兼容性强**：自动处理多模态降级（不支持图片的模型替换为占位符）、兼容 dashscope 风格 SSE（无空格）、自动清理 MiniMax 系列无效 `tool_use` 等边角情况。

---

## 二、核心功能

### 1. 多协议代理

- `POST /anthropic/v1/messages`（及子路径）— Anthropic Messages API 透传
- `POST /v1/chat/completions`（及子路径）— OpenAI Chat Completions 透传
- `POST /openai/responses` — OpenAI **Responses API** 端点，供 Codex CLI 等只认 Responses 协议的客户端使用；本服务会自动把 Responses 请求转换为 Chat Completions 格式，转发到对应 provider 的 `openai_url`，再把上游的 Chat 响应转回 Responses 格式（同时支持 `stream: true` 的 SSE 与 `stream: false` 的整包返回）
- 支持流式响应（`stream: true`）
- 支持从请求体中读取 `x-api-key` 或 `Authorization: Bearer ...`

### 2. 提供商（Provider）管理

- 每个 provider 可同时配置 Anthropic URL 与 OpenAI URL 两个地址（至少配置一个）：
  - 名称、Anthropic URL、OpenAI URL、API Key
  - 启用/禁用（手动或自动）
  - 最大并发数（`max_concurrency`，0 表示不限）
  - "禁用原因"区分（自动禁用时记录原因）
- 请求转发时按**请求协议**路由到对应的 URL：Anthropic 协议请求（`/anthropic/*`）走 `anthropic_url`，OpenAI 协议请求（`/v1/*`）走 `openai_url`，不做协议格式转换；URL 需填到完整端点（如 `.../v1/messages`、`.../v4/chat/completions`），系统不会自动拼接路径
- 若某 provider 未配置对应协议的 URL，则该协议的请求会自动排除该 provider（包括模型映射分组与兜底转发）
- 后台自动统计每个 provider 的实时并发使用量

### 3. 模型映射（Model Mapping）

- 别名（alias）→ 真实模型名 + provider
- **分组名**：同一个 `group_name` 下的多个映射共享同一个总模型名，系统会按 `priority` 加权轮询选择，**按客户端 IP 隔离**，并自动选当前并发最低的 provider
- **模型类型**：标记 `text` / 多模态，便于后续扩展
- **max_tokens**：可强制覆盖请求的 `max_tokens` 字段
- 启用/禁用

### 4. 错误码映射（Error Mapping）

- 将上游返回的 HTTP 错误码（如 429、5xx）映射为另一个错误码返回给客户端
- 日志中保留原始码，客户端只看到映射后的码
- 支持为某个 provider 单独配置（provider 留空 = 全局规则）

### 5. 请求日志

- 记录：时间、客户端 IP、provider、源模型、目标模型、输入/输出 token、缓存 token、耗时、状态、原始/映射错误码、请求/响应体、错误信息
- 按状态、模型、IP、provider 多条件过滤
- 按时间分页
- 支持**自动清理**：保留最近 N 天（可在设置中开启，默认 7 天）

### 6. 计费与限额（Billing）

每个 provider 可独立配置：

- **3 种计费模式**：
  - `request_count` — 按请求数限速
  - `token_count` — 按 token 用量限速
  - `balance` — 预付费余额模式（按 input/output 单价扣费）
- **3 个时间窗口限额**：5 小时、周、月（任意可空）
- **告警阈值**：达到 80%（可配）时打印警告日志
- **自动禁用**：超限后自动禁用 provider 并标记 `disable_reason`
- **到期日期**：可设置 `expiration_date`，到期后自动禁用
- **窗口重置**：后台线程定期重置过期窗口并自动重新启用已恢复的 provider

### 7. API Key 管理

- 后台可生成多个 API Key（`sk-xxx` 格式），设置名称、启用/禁用、记录最后使用时间
- 代理路由校验 Key 合法性，非法 Key 返回 401

### 8. Web 管理界面

`http://localhost:5000/`（默认），登录后包含 5 个 Tab：

- **提供商管理**：增删改查、查看调用次数与 Token、用量
- **模型映射**：增删改查、分组配置
- **错误码映射**：全局 / 单 provider 规则
- **请求日志**：多条件过滤、查看请求/响应详情、清空
- **计费管理**：计费概览 + 单 provider 详细配置

外加：API Key 管理、自动清理设置、使用说明。

### 9. 兼容性与稳定性

- **dashscope SSE 兼容**：正确解析 `data:{...}`（无空格）格式以统计流式 token
- **MiniMax 系列**：自动清理无效 `tool_use` 消息，避免 400 报错
- **多模态降级**：当目标模型不支持图片时，自动把 `image` block 替换为占位文本
- **持久化 session**：密钥存 SQLite，重启不丢登录态

---

## 三、安装与启动

### 1. 准备环境

- Python 3.10+
- 操作系统：Windows / Linux / macOS 均可

### 2. 克隆与安装依赖

```bash
git clone <your-repo-url> ai-api-proxy
cd ai-api-proxy
pip install -r requirements.txt
```

依赖只有两个：

```
flask>=3.0
requests>=2.31
```

### 3. 启动服务

```bash
python app.py
```

启动成功后会看到：

```
==================================================
  AI API Proxy 已启动
  Web 管理界面:  http://localhost:5000
  Anthropic 代理: http://localhost:5000/anthropic
  OpenAI 代理:    http://localhost:5000/v1
==================================================
```

首次启动会自动创建本地 SQLite 数据库 `proxy.db`（已加入 `.gitignore`）。

### 4. 修改监听地址/端口

编辑 `app.py` 文件末尾：

```python
app.run(host="0.0.0.0", port=5000, debug=True)
```

`debug=True` 仅推荐开发环境使用；生产请关闭并使用 `gunicorn` / `waitress` 等 WSGI 服务器。

---

## 四、使用说明

### 1. 登录管理界面

浏览器访问 `http://localhost:5000/`，首次启动会要求登录：

- 用户名 / 密码存储在 `admin_users` 表中
- 启动后没有任何管理员账号，需要通过 `config.py` 或直接操作 SQLite 手动创建（参见 [附录 A](#附录-a-创建首个管理员账号)）

### 2. 添加 API Provider

进入 **提供商管理** Tab → **添加提供商**：

| 字段 | 说明 |
| --- | --- |
| 名称 | 显示名（例：`AWS Claude`） |
| Anthropic URL | Anthropic 协议**完整端点地址**（例：`https://api.anthropic.com/v1/messages`），系统不自动拼接路径，留空则不转发 Anthropic 协议请求 |
| OpenAI URL | OpenAI 协议**完整端点地址**（例：`https://api.openai.com/v1/chat/completions`），系统不自动拼接路径，留空则不转发 OpenAI 协议请求 |
| API Key | 上游 Key |
| 最大并发 | 0 = 不限；N = 同时最多 N 个请求 |

### 3. 创建 API Key

进入 **API Key 管理** → 输入 Key 名称 → 添加。系统会返回形如 `sk-xxxxxxxx` 的 Key，**仅显示一次**，请妥善保存。

### 4. 创建模型映射

进入 **模型映射** Tab → **添加模型映射**：

- **别名**：客户端请求时的 `model` 字段值（例：`claude-sonnet-4`）
- **目标模型**：上游真实模型名（例：`claude-sonnet-4-5-20250929`）
- **提供商**：选一个
- **分组名**（可选）：填了之后，多条同分组的映射会按优先级+加权轮询选择
- **优先级**：数字越小优先级越高
- **max_tokens**：0 = 不覆盖；>0 = 强制覆盖请求的 `max_tokens`

### 5. 客户端接入

#### Anthropic 协议

```bash
curl -X POST http://localhost:5000/anthropic/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-你的Key" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-sonnet-4",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

也支持 `Authorization: Bearer sk-xxx` 方式。

#### OpenAI 协议

```bash
curl -X POST http://localhost:5000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-你的Key" \
  -d '{
    "model": "claude-sonnet-4",
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

#### 在 Claude Code 中配置

将 `ANTHROPIC_BASE_URL` 指向本服务：

```bash
export ANTHROPIC_BASE_URL=http://localhost:5000/anthropic
export ANTHROPIC_AUTH_TOKEN=sk-你的Key
```

#### OpenAI 兼容客户端

```bash
export OPENAI_BASE_URL=http://localhost:5000/v1
export OPENAI_API_KEY=sk-你的Key
```

#### 接入 Codex CLI

Codex CLI 使用 OpenAI **Responses API**（`/v1/responses`），而非 Chat Completions。本服务提供 `POST /openai/responses` 端点，自动完成 Responses 与 Chat Completions 之间的双向格式转换，无需上游支持 Responses 协议。

配置方法：

```bash
export OPENAI_BASE_URL=http://localhost:5000/openai
export OPENAI_API_KEY=sk-你的Key
```

Codex CLI 会自动请求 `OPENAI_BASE_URL/responses`（即 `http://localhost:5000/openai/responses`），本服务接收后：
1. 将 Responses 格式请求（`input` / `instructions` 等）转换为 Chat Completions 格式（`messages` / `system` 等）
2. 转发到对应 provider 的 `openai_url`
3. 将上游返回的 Chat Completions 响应转换回 Responses 格式返回

也可直接用 curl 测试：

```bash
curl -X POST http://localhost:5000/openai/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-你的Key" \
  -d '{
    "model": "claude-sonnet-4",
    "input": "你好",
    "stream": false
  }'
```

### 6. 错误码映射示例

把上游 503 映射成 429，避免客户端反复重试：

| 提供商 | 原始错误码 | 映射为 | 启用 |
| --- | --- | --- | --- |
| （空 = 全局） | 503 | 429 | ✅ |

### 7. 计费配置示例

提供商详情页 → 计费：

- **计费模式**：`balance`
- **余额**：100
- **输入价格**（每百万 token）：3.0
- **输出价格**（每百万 token）：15.0
- **5h 限额**：1000（可选）
- **告警阈值**：0.8

配置后，余额按真实使用量自动扣减；超限时自动禁用该 provider。

### 8. 自动清理日志

设置 → **自动清理** → 开启、选择保留天数（默认 7 天）和清理间隔（默认 1 小时）。

---

## 五、目录结构

```
ai-api-proxy/
├── app.py              # Flask 入口、路由、后台任务
├── config.py           # SQLite 封装、表迁移、所有数据库操作
├── proxy.py            # 核心代理逻辑（路由、并发、流式、多模态降级等）
├── oauth.py            # OAuth 2.1 / PKCE / JWT 实现
├── requirements.txt    # 依赖
├── proxy.db            # SQLite 数据库（自动创建，已 gitignore）
├── templates/
│   ├── index.html      # 管理界面 SPA
│   └── login.html      # 登录页
├── static/lib/         # 前端依赖
└── docs/
    └── billing-ui-spec.md
```

---

## 六、注意事项

1. **SQLite 表结构变更**：本项目遵循"先备份 → DROP → 重建 → 回填"的迁移流程（见 `config.py` 中各 `_migrate_*` 函数），**禁止直接 `DROP TABLE` 而不保留数据**。
2. **API Key 安全**：管理员账号与 API Key 均以哈希/随机串形式存于 SQLite，请妥善保管 `proxy.db`。
3. **生产部署**：建议关闭 `debug=True`，配合反向代理（Nginx/Caddy）并启用 HTTPS。

---

## 附录 A：创建首个管理员账号

`proxy.db` 中 `admin_users` 表是空的，需手动创建一条记录：

```python
import sqlite3, secrets
from werkzeug.security import generate_password_hash

conn = sqlite3.connect("proxy.db")
username = "admin"
password = "你的密码"  # 请改
pw_hash = generate_password_hash(password)
conn.execute(
    "INSERT INTO admin_users (username, password_hash, created_at) VALUES (?, ?, datetime('now'))",
    (username, pw_hash),
)
conn.commit()
conn.close()
print(f"已创建管理员: {username}")
```

之后即可在登录页使用该账号登录。
