# 发布标题

```
v1.0.0：Vibe Coding 服务转发 首个正式发布版本（Windows 免安装版）
```

---

# 发布说明

## 🎉 Vibe Coding 服务转发 v1.0.0

基于 Flask 的多供应商 AI API 代理服务，把多家上游 LLM 统一抽象成 **Anthropic Messages API**、**OpenAI Chat Completions API**、**OpenAI Responses API** 三种接口对外暴露，并配套可视化 Web 管理界面。本版本首次提供 **Windows 免安装 exe**，双击即可运行。

### 📦 下载

| 文件 | 说明 |
| --- | --- |
| `VibeCodingProxy-v1.0.0.exe` | Windows 64 位免安装单文件，双击运行 |

- **大小**：约 17 MB
- **SHA256**：`c5089441388a1da1b936d8a55f6164719079173ba05bce9eefcfe784f0992bad`
- **系统要求**：Windows 10 / 11（64 位），无需预装 Python 或任何依赖

### 🚀 快速开始

1. 下载 `VibeCodingProxy-v1.0.0.exe`，放到任意目录（建议新建一个空文件夹，数据库文件会生成在同目录）。
2. 双击运行，弹出的控制台窗口会显示各端点地址。
3. 浏览器打开 **http://localhost:5000**，首次启动会自动初始化数据库并创建管理员账号。
4. 在 Web 界面配置上游提供商、模型映射、API Key 即可开始转发。

### ✨ 核心功能

- **多协议统一代理**
  - `POST /anthropic/v1/messages` — Anthropic Messages API
  - `POST /v1/chat/completions` — OpenAI Chat Completions API
  - `POST /openai/responses` — OpenAI Responses API（供 Codex CLI 等只认 Responses 协议的客户端，自动与 Chat Completions 互转）
  - 完整支持流式（`stream: true`）与非流式响应

- **提供商管理**
  - 每个提供商可同时配置 Anthropic URL 与 OpenAI URL，按请求协议自动路由
  - 「完整路径 / Base 路径」开关，支持 URL 原样转发或自动拼接路径后缀
  - 最大并发数限制、启用/禁用、自动禁用原因记录、实时并发统计

- **模型映射与负载均衡**
  - 别名（alias）= 对外模型名，按客户端 IP 隔离的加权轮询
  - 同别名多映射构成负载均衡池，按 `priority` 加权选择
  - **角色映射（role_mappings）**：转发前对 messages 角色做一次性替换（如 `developer → system`）
  - 强制覆盖 `max_tokens`、多模态优先选择

- **服务降级与故障转移**
  - 同一别名下某上游转发失败后被临时「降级」，后续请求自动绕过、切换到健康上游
  - 全部上游降级时仍会尝试，任一成功即立即恢复

- **自动重试与容错**
  - 上游 5xx 或连接级异常（超时、断开、Chunked 错误）自动重试
  - 4xx 视为语义错误立即返回不重试

- **资源治理**
  - 计费限额（5h / 周 / 月 / 余额）、超额自动禁用、窗口自动重置
  - 错误码映射、自动日志清理、启动及每日自动 VACUUM 压缩数据库

- **OAuth 2.1 支持**
  - PKCE、动态客户端注册（RFC 7591）、JWT 访问令牌
  - RFC 8414 授权服务器元数据、RFC 9728 受保护资源元数据端点，供 Claude Code 等客户端 OAuth 登录

- **可观测性**：完整记录每次请求的 IP、源/目标模型、provider、token、缓存 token、耗时、状态码、请求/响应体。

### 📝 本版本打包说明

- 使用 PyInstaller `--onefile` 打包，所有 Python 依赖已内嵌，开箱即用。
- 数据库文件 `proxy.db` 生成在 **exe 同目录**，便于备份与迁移。
- 控制台窗口支持 UTF-8 中文输出。

### ⚠️ 已知限制

- 内置的是 Flask 开发服务器，适合个人/小团队使用；高并发生产场景建议自行用 Gunicorn / waitress 等 WSGI 服务器运行源码。
- 当前仅提供 Windows 64 位版本。

---

> 源码与完整文档见仓库 README.md。
