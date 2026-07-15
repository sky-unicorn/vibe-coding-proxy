# 发布标题

```
v1.2.2：Vibe Coding 服务转发 彻底修复 codex 卡死、新增 Codex 配置弹窗（思考过程注入三开关 + 原生 Responses 透传）、Responses 路径 SSE 心跳保活与缓存命中计费修复（Windows 免安装版）
```

---

# 发布说明

## 🎉 Vibe Coding 服务转发 v1.2.2

基于 Flask 的多供应商 AI API 代理服务，把多家上游 LLM 统一抽象成 **Anthropic Messages API**、**OpenAI Chat Completions API**、**OpenAI Responses API** 三种接口对外暴露，并配套可视化 Web 管理界面。本版本延续 **Windows 免安装 exe** 双击即运行的形态，集中攻克了 **codex CLI 经代理卡死**的各类根因（流式心跳缺失、连接中断静默吞错、思考链被丢弃），并为模型映射新增了 **Codex 配置弹窗**，把「丢弃 / think 标签 / reasoning_content 字段 / 原生 Responses 透传」四种 Responses↔Chat 转换策略统一为可视化开关。

### 📦 下载

| 文件 | 说明 |
| --- | --- |
| `VibeCodingProxy-v1.2.2.exe` | Windows 64 位免安装单文件，双击运行 |

- **大小**：约 15 MB
- **SHA256**：`2da3d8fb6c9591ae802ace80553acee58ca69accb944d972597e875394b0dc9b`
- **系统要求**：Windows 10 / 11（64 位），无需预装 Python 或任何依赖

### ✨ 本版本更新（v1.2.1 → v1.2.2）

- **彻底修复 codex CLI 经代理卡死**（本轮多条提交协同解决，覆盖心跳、错误终结、思考链三个层面）
  - **Responses 流式路径 SSE 心跳保活**：codex 走 OpenAI Responses 协议，请求国产上游时其 reasoning 阶段可能数十秒甚至数百秒不产出任何 SSE 数据，代理在阻塞读取上游期间不发任何事件，codex SSE 客户端的 idle timeout（硬上限 300s）触发后报 `idle timeout waiting for SSE` 断连，表现为「请求成功但卡死」。修复把上游 `iter_lines()` 阻塞读取移到后台 daemon 线程，主生成器带 5s 超时拉取，超时则 `yield response.ping` **真正的 SSE event**；心跳必须是真事件而不能是 `:` 注释行（codex 的 eventsource_stream 会丢弃注释行、不重置计时）
  - **流式中断不再被静默记 success**：流式 `generate()` 的连接级异常分支原本 `pass` 静默吞掉，`error_msg` 保持空串、`finally` 误记 `status=success` 且不发任何终结事件，codex 收到半截 reasoning 流后 EOF，报 `stream closed before response.completed` 并卡死，与日志 success 自相矛盾。现改为：上游侧异常时发 `response.failed` 真 SSE 事件让 codex 走错误重试路径，连接级异常一律记 error，并新增流式诊断变量（`exit_reason`/`completed_sent`/`upstream_done` 等）在 `finally` 拼成摘要输出到控制台与 `error_msg`，便于从 Web UI / SQLite / 控制台定位卡死退出路径
  - **思考链注入修复多轮工具调用决策退化**：codex 经代理请求 MiniMax 等推理模型时，Responses→Chat 转换原本直接丢弃历史 reasoning 思考内容，破坏 MiniMax Interleaved Thinking 思维链，导致多轮工具调用后模型决策退化、不再调用工具而返回纯文本（`finish_reason=stop`）。详见下方「Codex 配置弹窗」

- **新增模型映射「Codex 配置」弹窗，统一四种思考回传 / 转换策略为开关**
  - 模型映射操作列新增「Codex 配置」按钮与独立弹窗，收纳并扩展原先散落的 Responses 转换开关；编辑弹窗移除原 `reasoning_effort` 透传开关（已迁入 Codex 配置弹窗）
  - 弹窗内含四个开关，前三个为「思考过程回传方式」、第四个为「转换路径」：
    1. **透传思考强度参数（`reasoning_effort`）** —— v1.2.1 已有，迁入弹窗，默认打开
    2. **思考过程注入：think 标签**（`think_injection`）—— 把 codex 回传的历史 reasoning 以 `<think>...</think>` 注入对应 assistant 消息 `content` 前缀，保持思考链连续；MiniMax 识别为思维链，DeepSeek/GLM/Kimi 等当普通文本无害。默认关闭
    3. **思考过程注入：reasoning_content 字段**（`reasoning_content_field`）—— DeepSeek/GLM/Kimi 思考模式官方要求 reasoning 以独立 `reasoning_content` 字段回传 assistant 消息，多轮工具调用缺失该字段会返回 400；`think_injection` 的标签形式只对 MiniMax 有效，对 DeepSeek 等仍 400，故新增此字段注入方式。**默认开启**（DeepSeek 用户最多）
    4. **原生 Responses 透传**（`native_responses`）—— 开启后 `/openai` 端点对该 mapping 跳过 Responses↔Chat 双向转换，按 `provider.openai_url` 派生 `/responses` 端点原样转发（仿 Anthropic handler 直转语义），适用于原生支持 Responses API 的上游；避免协议转换造成的保真度损失（`previous_response_id`/`store`/原生 reasoning 摘要被丢弃）、额外延迟与转换层心跳维护复杂度。默认关闭
  - **互斥规则**：`think_injection` 与 `reasoning_content_field` 在 UI 上互斥（前端 `onchange` 强制），同一时间只能开一个；`native_responses` 与前两者三方互斥，开启原生透传自动取消另两个；三个全关则走原转换路径并丢弃 reasoning（与改动前逐字节一致，零回归）。`reasoning_effort` 透传开关独立、不参与互斥

- **修复 Responses 路径缓存命中 token 未记录**
  - 通过 OpenAI Responses 端点请求时，日志的 `cache_read_input_tokens` 始终为 0，无法查看模型缓存命中数量、影响缓存命中计费
  - 根因：原生 Responses 透传路径（流式 / 非流式）与 Responses→Chat 转换流式路径只提取了 `input/output_tokens`，未解析上游 usage 中的缓存命中字段；转换路径返回给客户端的响应也漏了 `input_tokens_details`
  - 修复后：原生透传从 `usage.input_tokens_details.cached_tokens` 提取并记账，转换流式从 `prompt_tokens_details.cached_tokens` 提取并记账，转换响应（非流式 / 流式）均补全 `input_tokens_details.cached_tokens` 字段；日志、计费与客户端可见的缓存命中口径与 Chat Completions 直通路径一致

- **思考过程注入开关命名与描述优化**
  - 统一两个思考过程注入开关的命名风格为「思考过程注入：think 标签」与「思考过程注入：reasoning_content 字段」，让用户一眼看出是同一功能的两种注入方式
  - 精简描述：去掉互斥关系的冗余重复说明（`onchange` 已强制保证）、去掉跨适用场景的「无害」备注
  - 新增 `原生Responses透传说明.md`：文档化转换路径中图片剥离、reasoning 重整、Responses↔Chat 字段映射、`_responses_input_to_chat_messages` 调用四个操作的作用与原生透传跳过它们的原因

### 🚀 快速开始

1. 下载 `VibeCodingProxy-v1.2.2.exe`，放到任意目录（建议新建一个空文件夹，数据库文件会生成在同目录）。
2. 如需自定义端口或管理员，在同目录新建 `proxy.ini`：

    ```ini
    [server]
    port = 5000

    [admin]
    username = admin
    password = admin123
    ```

3. 双击运行，弹出的控制台窗口会显示各端点地址。
4. 浏览器打开 **http://localhost:5000**（端口随 `proxy.ini` 变化），首次启动会自动初始化数据库并创建管理员账号。
5. 在 Web 界面配置上游提供商、模型映射、API Key 即可开始转发；codex CLI 接入请在对应模型映射行点「Codex 配置」按上游能力选择思考回传方式。

### ⚠️ 升级说明（v1.2.0 / v1.2.1 → v1.2.2）

- **从 v1.0.0 / v1.1.1 / v1.2.0 / v1.2.1 的 exe 升级**：把旧版本同目录的 `proxy.db` 复制到新 exe 同目录，即可保留历史数据。
- **从 v1.2.0 升级**：首次启动 v1.2.2 时，迁移框架会自动把库从 v1 升到 v5，依次补齐 `reasoning_effort_supported`（v1.2.1 引入）、`think_injection`、`reasoning_content_field`、`native_responses` 四列，按备份 → 重建 → 回填的流程执行，历史模型映射不丢失。
- **从 v1.2.1 升级**：库已在 v2，首次启动 v1.2.2 会从 v2 升到 v5，补齐 `think_injection`（默认 0 / 关）、`reasoning_content_field`（默认 1 / 开）、`native_responses`（默认 0 / 关）三列。
- **升级后行为变化（重要）**：
  - 老映射升级后 `reasoning_content_field` **默认打开**——若你的上游是 DeepSeek / GLM / Kimi，这正是所需行为（修复多轮工具调用 400）；若上游是 MiniMax，建议改用「思考过程注入：think 标签」开关（与 `reasoning_content_field` 互斥，前端会自动切换）。
  - 原本因思考链被丢弃导致 codex 多轮工具调用后「不调工具只回纯文本」的模型，升级后开启对应注入开关即可恢复。
  - 原生支持 Responses API 的上游，可在 Codex 配置弹窗开启「原生 Responses 透传」获得更高保真度（与思考注入开关三方互斥）。
  - Responses 端点的缓存命中 token 现已正确记录到日志与计费，`cache_read_input_tokens` 不再恒为 0。
- `proxy.ini` 是可选文件；管理员账号 / 密码以该文件为准，修改后重启会覆盖库中已有管理员，日常改密请直接编辑该文件。

### 📝 本版本变更清单（v1.2.1..v1.2.2）

| 类型 | 变更 |
| --- | --- |
| 🐛 修复 | codex 经代理卡死：Responses 流式路径 SSE 心跳保活（后台线程 + `response.ping` 真 event） |
| 🐛 修复 | 流式中断被静默记 success：上游异常发 `response.failed` 终结事件、连接级异常一律记 error、新增流式诊断摘要 |
| 🐛 修复 | Responses 路径缓存命中 token 未记录：原生透传与转换路径均提取 `cached_tokens` 并补全 `input_tokens_details` |
| ✨ 特性 | 模型映射新增「Codex 配置」弹窗，收纳 `reasoning_effort` 透传并新增 think 标签 / reasoning_content 字段 / 原生 Responses 透传开关 |
| ✨ 特性 | `think_injection`（think 标签注入，默认关）修复 MiniMax 多轮工具调用思考链断裂 |
| ✨ 特性 | `reasoning_content_field`（字段注入，默认开）修复 DeepSeek/GLM/Kimi 多轮工具调用 400 |
| ✨ 特性 | `native_responses`（原生 Responses 透传，默认关）跳过协议转换直转，提升保真度 |
| 🔧 架构 | 数据库 schema 升级到 v5：`model_mappings` 新增 `think_injection`、`reasoning_content_field`、`native_responses` 三列，迁移幂等回填，老库升级与全新库默认值一致 |
| 📄 文档 | 新增 `原生Responses透传说明.md`；优化思考过程注入开关命名与描述 |

---

> 源码与完整文档见仓库 README.md。
