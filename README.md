# Vibe Coding 服务转发

一个基于 Flask 的多供应商 AI API 代理服务，统一对外暴露 **Anthropic Messages API**、**OpenAI Chat Completions API**、**OpenAI Responses API** 三种接口，底层可对接任意兼容的 LLM 提供商。配套 Web 管理界面，可对提供商、模型映射（含角色映射）、错误码、日志、计费、API Key 等进行可视化配置。

---
[软件使用手册](manual/directions/软件使用手册.md)

[通过cc-switch配置Claude Code](manual/directions/通过cc-switch配置ClaudeCode.md)

[通过cc-switch配置Codex Cli](manual/directions/通过cc-switch配置CodexCli.md)

---


## 一、项目作用

- **统一代理入口**：把多家上游 LLM（Anthropic 官方、自建网关、OpenAI 兼容服务等）抽象成统一的 Anthropic / OpenAI 接口，下游客户端无需关心上游差异。
- **模型路由**：按客户端请求的 `model` 字段（别名）路由到真实的目标模型和提供商，同名别名多映射时按客户端 IP 隔离的加权轮询选择。
- **请求改写**：支持在转发前进行**角色映射**（如 `developer → system`）、强制覆盖 `max_tokens`、系统消息归入顶层字段等改写，解决不同客户端与上游协议的细微差异。
- **资源治理**：内置并发限流、错误码映射、计费限额（5h / 周 / 月 / 余额）以及自动日志清理，避免某个提供商被滥用或意外超支。
- **可观测性**：完整记录每次请求的时间、IP、源模型、目标模型、provider、token、缓存 token、耗时、状态码、请求/响应体，便于排查问题。
- **自动重试与容错**：请求出错或上游返回 5xx 时自动重试，连接级异常（超时、断开、Chunked 错误）全覆盖；4xx 视为客户端/上游语义错误立即返回不重试。
- **服务降级与故障转移**：同一别名下某个上游转发失败后，会被临时"降级"一段时间，后续请求自动绕过它、切换到其他健康上游；全部上游都降级时仍会尝试使用，任一上游成功即立即恢复正常。
- **兼容性强**：自动处理多模态降级（不支持图片的模型替换为占位符）、兼容 dashscope 风格 SSE（无空格）、自动清理 MiniMax 系列无效 `tool_use`、适配 DeepSeek 思考模式等边角情况。

---

## 二、核心功能

### 1. 多协议代理

- `POST /anthropic/v1/messages`（及子路径）— Anthropic Messages API 透传
- `POST /v1/chat/completions`（及子路径）— OpenAI Chat Completions 透传
- `POST /openai/responses` — OpenAI **Responses API** 端点，供 Codex CLI 等只认 Responses 协议的客户端使用；本服务会自动把 Responses 请求转换为 Chat Completions 格式，转发到对应 provider 的 `openai_url`，再把上游的 Chat 响应转回 Responses 格式（同时支持 `stream: true` 的 SSE 与 `stream: false` 的整包返回）
- 支持流式响应（`stream: true`）
- 支持从请求头中读取 `x-api-key` 或 `Authorization: Bearer ...`

### 2. 提供商（Provider）管理

- 每个 provider 可同时配置 Anthropic URL 与 OpenAI URL 两个地址（至少配置一个）：
  - 名称、Anthropic URL、OpenAI URL、API Key
  - 启用/禁用（手动或自动）
  - 最大并发数（`max_concurrency`，0 表示不限）
  - "禁用原因"区分（自动禁用时记录原因）
  - **「完整路径」开关（`full_path`）**：控制转发时是否自动拼接路径后缀
    - **完整路径（默认，`full_path=1`）**：配置的 URL 原样使用，不拼接任何后缀。适用于 URL 已含完整端点的情况（如 `https://api.anthropic.com/v1/messages`）。
    - **Base 路径（`full_path=0`）**：转发时自动在 `anthropic_url` 后拼接 `/v1/messages`，在 `openai_url` 后拼接 `/chat/completions`。适用于只填到 base 路径的情况（如 `https://api.anthropic.com`）。
- 请求转发时按**请求协议**路由到对应的 URL：Anthropic 协议请求（`/anthropic/*`）走 `anthropic_url`，OpenAI 协议请求（`/v1/*`、`/openai/*`）走 `openai_url`，不做协议格式转换（Responses 除外，它会先与 Chat Completions 互转）
- 三种协议接口（Anthropic / OpenAI Chat / OpenAI Responses）的容错策略一致
- 若某 provider 未配置对应协议的 URL，则该协议的请求会自动排除该 provider（包括模型映射负载均衡池与兜底转发）
- 后台自动统计每个 provider 的实时并发使用量

### 3. 模型映射（Model Mapping）

- **别名（alias）= 对外模型名**：请求中的 `model` 字段即别名，系统据此查找映射并转发到对应的真实模型 + provider
- **负载均衡池**：同一别名配置多条映射即构成一个负载均衡池，系统按 `priority` 加权轮询选择，**按客户端 IP 隔离**
  - 别名唯一（仅一条映射）时自然退化为精确匹配
  - 别名重名（多条映射）时按优先级加权轮询，并自动过滤计费超限/未配置对应协议 URL 的 provider
  - 含图片的请求会优先选择池中的多模态成员，无多模态成员时回退到全部成员
- **模型类型**：标记 `text` / 多模态，便于后续扩展
- **max_tokens**：可强制覆盖请求的 `max_tokens` 字段（0 = 不覆盖）
- **角色映射（role_mappings）**：在转发前对请求中的 `messages` 角色进行替换，以 JSON 数组形式配置多条规则
  - 每条规则形如 `{"from": "developer", "to": "system"}`
  - 以**一次性映射**方式应用（相同原角色只取第一条规则，避免链式替换 A→B、B→C 把 A 变成 C）
  - Anthropic 链路中**在 system 消息提取之前**执行，因此 `developer → system` 的替换结果能正确归入顶层 `system` 字段
  - 典型用途：让只认 `system` 角色的上游接收 `developer` / `system` 角色消息（如 Claude Code 的 `developer` 消息）
- 启用/禁用

### 4. 错误码映射（Error Mapping）

- 将上游返回的 HTTP 错误码（如 429、5xx）映射为另一个错误码返回给客户端
- 日志中保留原始码，客户端只看到映射后的码
- 支持为某个 provider 单独配置（provider 留空 = 全局规则）

### 5. 请求日志

- 记录：时间、客户端 IP、provider、**源模型（别名）**、目标模型、输入/输出 token、缓存 token（读/建）、耗时、状态、原始/映射错误码、请求/响应体、错误信息
- 按状态、模型、IP、provider 多条件过滤
- 按时间分页
- 支持**自动清理**：保留最近 N 天（可在设置中开启，默认 7 天），并按设定间隔（默认 1 小时）检查清理

### 6. 计费与限额（Billing）

每个 provider 可独立配置：

- **3 种计费模式**：
  - `request_count` — 按请求数限速
  - `token_count` — 按 token 用量限速
  - `balance` — 预付费余额模式（按 input/output 单价扣费）
- **3 个时间窗口限额**：5 小时、周、月（任意可空）
- **告警阈值**：达到阈值（默认 80%）时打印警告日志
- **自动禁用**：超限后自动禁用 provider 并标记 `disable_reason`
- **到期日期**：可设置 `expiration_date`，到期后自动禁用
- **窗口重置**：后台线程定期重置过期窗口并自动重新启用已恢复的 provider
- **缓存计费折扣**：`balance` 模式下，缓存写入 token（`cache_creation_input_tokens`）按 input 全额价格计费（与 `input_tokens` 合并），缓存读取 token（`cache_read_input_tokens`）按 10% 的 input 价格计费（即 90% 折扣）
- **余额保护**：`balance` 模式下更新计费配置（PUT）时，若原始请求体未显式提供 `balance` 键，则保留数据库中已扣减后的当前余额，避免被覆盖；该保护仅作用于更新，新建 provider（POST）不触发

### 7. API Key 管理

- 后台可生成多个 API Key，格式为 `sk-` + 48 位十六进制随机串
- 设置名称、启用/禁用、记录最后使用时间
- 完整 Key 仅在创建时返回一次，列表查询只显示前缀（如 `sk-xxxxxxxx...`）
- 代理路由鉴权规则：仅 `POST` 方法需校验 API Key，`GET`/`PUT`/`DELETE` 到代理前缀（`/anthropic`、`/v1`、`/openai`）不需要 Key；非法 Key 返回 401
- 未匹配路由（如 `GET /api/xxx`）走 Flask 默认 404；对代理前缀的 `GET` 探活请求（如 `GET /v1`）返回 `{status: ok}` 作为健康检查响应

### 8. Web 管理界面

`http://localhost:5000/`（默认），登录后包含 6 个 Tab：

- **提供商管理**：增删改查，表格列含并行（`max_concurrency`）、并发（实时）、Token、调用次数、计费（模式徽章）、状态、完整路径开关等
- **模型映射**：增删改查，表格列含别名、优先级、目标模型、模型类型、max_tokens、提供商、角色映射（`from→to` 徽章）、状态、降级状态、操作；同名别名以徽章 + 色点 + 行底色做隐式分组（负载均衡池），系统按优先级加权轮询（按客户端 IP 隔离）从候选中选择
- **错误码映射**：全局 / 单 provider 规则
- **请求日志**：多条件过滤、查看请求/响应详情、清空
- **API Key**：生成与管理
- **计费管理**：计费概览 + 单 provider 详细配置

外加：自动清理设置（保留天数、清理间隔）、使用说明。

### 9. 自动重试与故障转移

为保证转发的高可用，系统有两层容错机制，依次生效：

**第一层：同一上游的自动重试**

选定一个上游发送请求后，如果失败会在**同一个上游上**再重试最多 3 次（即第一次加上最多 3 次重试，单次转发累计最多发送 4 次请求）：

- 主要应对偶发的网络抖动或上游临时不可用（连接超时、上游断开、HTTP 5xx 等）
- 客户端或上游语义错误（4xx，如 401/403/404）不会重试，立即返回
- 单次请求耗时已超过 5 秒时不再重试，避免对长时间排队后返回的错误做无谓重试
- 连续多次仍然失败，才会进入第二层

**第二层：切换到其他上游（故障转移）**

当某个上游重试后仍然失败，系统会把它临时"降级"一段时间（期间不再使用），并自动切换到同一模型名下的其他上游继续处理本次请求：

- 切换的前提是你为该模型名配置了多个上游（即构成了"负载均衡池"）
- 降级持续时长可配置，默认 30 秒
- 被降级的上游一旦有一次成功调用，会立即恢复正常使用
- 如果所有上游都被降级（极端情况），系统会回退到正常选择，不再跳过任何上游，保证至少还有上游被使用。但此时所有上游都可能仍然不可用，本轮请求大概率会失败，且可能耗时较长；请等待降级时长（默认 30 秒）过后再试
- 单次请求内，如果切换过多个上游，每个失败的尝试都会记录在请求日志中，最终成功的那次也会记录——方便事后排查

**什么时候需要开启这个功能？**

- 当你为一个模型名配置了多个上游（做负载均衡或容灾）时，强烈建议开启。某个上游故障时，请求会自动落到健康的上游，客户端几乎无感
- 如果你每个模型名只配了一个上游，开启降级意义不大：故障后没有可切换的健康上游，本次请求仍会失败；并且因为只有一个候选，降级回退逻辑仍会再次尝试它（并不会真正跳过）。建议配合多上游使用

**流式响应的特别说明**

流式响应的容错策略分两个层面：

- **本次请求内（保护客户端不收残缺响应）**：只要上游已经开始返回内容（哪怕只返回了一个字），就不会再切换到其他上游，避免给客户端拼出半截响应。也就是说，一旦流式输出启动，本次请求只会在这一个上游上完成或中断
- **后续请求（避免再次打到故障节点）**：如果流式过程中途因连接中断（上游断开、超时、客户端主动断开等）或上游出错而异常结束，该上游会被临时降级一段时间，**下一次请求会先绕过它、改走其他健康上游**，等降级时长过去后才恢复尝试

**进程重启的影响**

- 降级状态保存在内存中，进程重启后会被清空。重启后所有上游都会以"正常"状态开始

> 具体如何开启与配置，见 [四、10. 故障转移配置](#10-故障转移配置)。

### 10. 兼容性与稳定性

- **dashscope SSE 兼容**：正确解析 `data:{...}`（无空格）格式以统计流式 token
- **MiniMax 系列**：自动清理 assistant 消息中 `name` 为空的 `tool_use` 块，并同步移除引用这些 id 的 `tool_result`，避免 400 报错
- **DeepSeek 思考模式适配**：移除上游不接受的 `thinking` 参数；将 Anthropic 的 `budget_tokens` 映射为 `output_config.effort`（≥10000 → `max`，否则 `high`）；修复非法 `metadata.user_id`；必要时自动插入空 `thinking` 块
- **多模态降级**：当目标模型不支持图片（`model_type != multimodal`）时，自动把 `image` block 替换为占位文本；同一别名池内若存在多模态候选，含图片请求会优先选多模态映射
- **系统消息归位**：Anthropic 协议转发前，自动把 `messages` 中 `role=system` 的消息提取到请求体顶层 `system` 字段
- **持久化 session**：Flask 密钥存于 SQLite（`settings.secret_key`，启动时生成一次），重启不丢登录态
- **数据库体积自动维护**：后台每天自动清理一次数据库碎片，防止数据库文件随使用增长过大

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
  Vibe Coding 服务转发 已启动
  Web 管理界面:  http://localhost:5000
  Anthropic 代理: http://localhost:5000/anthropic
  OpenAI 代理:    http://localhost:5000/v1
  Responses 代理: http://localhost:5000/openai
==================================================
```

首次启动会自动创建本地 SQLite 数据库 `proxy.db`（已加入 `.gitignore`）。

### 4. 配置文件 proxy.ini（端口 / 默认管理员）

在项目根目录（打包成 exe 后位于 exe 同目录）放置 `proxy.ini` 即可配置启动端口与默认管理员账号。文件可选，不存在或某项留空时使用内置默认值。

```ini
# proxy.ini
[server]
# 服务启动端口（1-65535），默认 5000
port = 5000

[admin]
# 管理员账号与密码（每次启动都校准，修改后重启即生效）
username = admin
password = admin123
```

说明：

- **端口**：每次启动都会读取 `[server].port`，修改后重启即生效。
- **管理员账号/密码**：`[admin]` 段是管理员账户的**唯一真实来源**。每次启动 `init_db()` 都会校准 `admin_users` 表使其与之一致：
  - 修改密码后重启即生效（仅当 `proxy.ini` 中的密码与数据库已存的哈希不同时才刷新，避免无谓写入）
  - 修改用户名后重启即生效（旧的同名账户会被删除，仅保留 `proxy.ini` 中配置的新账户）
  - 注意：登录后在管理界面或数据库中直接改管理员密码，**会在下次启动时被 `proxy.ini` 的值覆盖回去**。要永久修改管理员账号/密码，请改 `proxy.ini` 后重启
- **首次启动**：全新安装（`admin_users` 表为空）时按 `[admin]` 段写入首个管理员；未配置 `proxy.ini` 时写入默认 `admin / admin123`。
- **监听 host**：固定为 `0.0.0.0`（监听所有网卡）。如需仅本机访问或使用 `gunicorn` / `waitress` 等 WSGI 服务器，再编辑 `app.py` 文件末尾的 `app.run(...)`。

---

## 四、使用说明

### 1. 登录管理界面

浏览器访问 `http://localhost:5000/`（端口以 `proxy.ini` 中 `[server].port` 为准），首次启动会要求登录：

- 用户名 / 密码以 `proxy.ini` 的 `[admin]` 段为唯一真实来源；运行时存储于 `admin_users` 表（密码经 `werkzeug` 哈希后存）
- **首次启动**：`admin_users` 表为空时，按 `[admin]` 段写入首个管理员；未配置 `proxy.ini` 时使用内置默认 `admin / admin123`（**弱口令，请尽快通过 `proxy.ini` 修改并重启**）

### 2. 添加 API Provider

进入 **提供商管理** Tab → **添加提供商**：

| 字段 | 说明 |
| --- | --- |
| 名称 | 显示名（例：`AWS Claude`） |
| Anthropic URL | Anthropic 协议端点地址，留空则不转发 Anthropic 协议请求 |
| OpenAI URL | OpenAI 协议端点地址，留空则不转发 OpenAI 协议请求 |
| 完整路径 | 勾选（默认）：URL 原样使用；取消勾选：自动在 Anthropic URL 后拼 `/v1/messages`、在 OpenAI URL 后拼 `/chat/completions` |
| API Key | 上游 Key |
| 最大并发 | 0 = 不限；N = 同时最多 N 个请求 |

> 示例：完整路径模式填 `https://api.anthropic.com/v1/messages`；base 路径模式填 `https://api.anthropic.com`（系统自动补全后缀）。

### 3. 创建 API Key

进入 **API Key** Tab → 输入 Key 名称 → 添加。系统会返回形如 `sk-` + 48 位的 Key，**仅显示一次**，请妥善保存（列表中之后只显示前缀）。

### 4. 创建模型映射

进入 **模型映射** Tab → **添加模型映射**：

- **别名**：客户端请求时的 `model` 字段值（例：`claude-sonnet-4`）。**同名别名配置多条映射即构成负载均衡池**，系统按优先级 + 加权轮询选择（按客户端 IP 隔离）；别名唯一时即精确匹配
- **目标模型**：上游真实模型名（例：`claude-sonnet-4-5-20250929`）
- **提供商**：选一个
- **优先级**：数字越小优先级越高
- **max_tokens**：0 = 不覆盖；>0 = 强制覆盖请求的 `max_tokens`
- **模型类型**：`text` / 多模态
- **角色映射**：可配置多条 `from → to` 规则（如 `developer → system`），转发前对请求消息角色做一次性替换

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
1. 将 Responses 格式请求（`input` / `instructions` / `tools` / `tool_choice` 等）转换为 Chat Completions 格式（`messages` / `system` 等）
2. 转发到对应 provider 的 `openai_url`
3. 将上游返回的 Chat Completions 响应转换回 Responses 格式返回

**流式响应**会按 Responses SSE 事件序列输出（`response.created` → `response.output_item.added` → `response.output_text.delta` → … → `response.completed`），其中**工具调用作为独立的 `function_call` output_item 发送**，确保 Codex CLI 能正确解析为结构化工具调用。同时系统会自动注入 `stream_options.include_usage`，保证末帧带回 token 用量。

**推理（reasoning）透传**：Codex 在 `model_reasoning_effort` 配置时，代理会按上游目标模型自动把 effort 映射为合适的 `reasoning_effort` 参数透传给上游；上游返回的 `reasoning_content` 流式字段会被转换为 `response.reasoning_summary_*` 事件序列透传给 Codex。建议在 Codex 配置中开启 `disable_response_storage = true`，避免依赖 OpenAI 的服务端存储。详细的 `~/.codex/config.toml` 配置与注意事项见 [通过 cc-switch 配置 Codex CLI](manual/directions/通过cc-switch配置CodexCli.md)。

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

配置后，余额按真实使用量自动扣减（缓存写入 token 按 input 价格计费，缓存读取 token 按 input 价格的 10% 计费）；超限时自动禁用该 provider。

### 8. 角色映射示例

某上游只认 `system` 角色，但 Claude Code 发来的是 `developer` 角色消息。在模型映射中配置角色映射规则：

| from | to |
| --- | --- |
| `developer` | `system` |

转发前，请求中的 `developer` 消息会被替换为 `system`，并自动归入 Anthropic 顶层 `system` 字段。

### 9. 自动清理日志

设置 → **自动清理** → 开启、选择保留天数（默认 7 天）和清理间隔（默认 1 小时）。

### 10. 故障转移配置

进入 **模型映射** Tab → 表格上方有一行降级控制条：

- **启用服务降级**（开关）：打开后，转发失败的上游会被临时"降级"，后续请求自动绕过它、切换到其他健康上游
- **降级持续（秒）**（数字输入框，默认 30，最小 1）：失败上游被降级的持续时长
- **保存设置**：保存后立即生效

降级开启后的效果：

| 场景 | 行为 |
| --- | --- |
| 某个上游转发失败（重试耗尽） | 标记降级，持续 N 秒内被后续请求绕过 |
| 同一模型名下仍有健康上游 | 自动切换到健康上游 |
| 所有上游都已降级 | 不再跳过任何上游，仍会尝试使用 |
| 任一上游调用成功 | 立即恢复正常 |
| 计费超限的上游 | 跳过该上游，但不触发降级 |

模型映射表格中有一列 **降级状态**，实时显示每个上游当前是"正常"（绿色）还是"降级（剩余 X 秒）"（橙色），每 3 秒自动刷新。

---

## 五、目录结构

```
ai-api-proxy/
├── app.py              # Flask 入口、路由、认证中间件、后台任务
├── config.py           # SQLite 封装、表迁移、所有数据库操作
├── proxy.py            # 核心代理逻辑（路由、并发、流式、角色映射、重试、多模态降级等）
├── requirements.txt    # 依赖
├── proxy.db            # SQLite 数据库（自动创建，已 gitignore）
├── templates/
│   ├── index.html      # 管理界面 SPA
│   └── login.html      # 登录页
└── static/lib/         # 前端依赖
```

---

## 六、注意事项

1. **SQLite 表结构变更**：修改 SQLite 表结构时，必须先备份表内现有数据，再删除旧表重建，避免直接 `DROP TABLE` 导致历史数据永久丢失。
2. **API Key 安全**：管理员密码与 API Key 均以哈希 / 随机串形式存于 SQLite，请妥善保管 `proxy.db`。未配置 `proxy.ini` 时内置默认管理员为 `admin / admin123`，**首启即存在弱口令**，请通过 `proxy.ini` 的 `[admin]` 段修改后重启（仅修改数据库中的密码哈希会在下次启动时被 `proxy.ini` 覆盖回 `admin123`）。
3. **生产部署**：建议关闭 `debug=True`，配合反向代理（Nginx/Caddy）并启用 HTTPS，公网部署务必启用 HTTPS 以保护 API Key 和管理后台凭据。
4. **数据库维护**：数据库体积会自动维护（后台每天清理碎片）；通常无需手动处理。如确需手动压缩，可在低峰期用 SQLite 工具对 `proxy.db` 做一次压缩操作。
