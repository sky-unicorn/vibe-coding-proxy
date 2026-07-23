# 发布标题

```
v3.0.0：Vibe Coding 服务转发 前端全面迁移至 Vue 3 + Element Plus、移除 OAuth 整套认证代码（攻击面归零）、新增 Nacos MCP Server（9 工具远程管理命名空间与配置）、转发重试次数与间隔可配置、修复关闭窗口进程残留、请求日志面板与 MCP 广场多项 UI 优化（Windows 免安装版）
```

---

# 发布说明

## 🎉 Vibe Coding 服务转发 v3.0.0

基于 Flask 的多供应商 AI API 代理服务，把多家上游 LLM 统一抽象成 **Anthropic Messages API**、**OpenAI Chat Completions API**、**OpenAI Responses API** 三种接口对外暴露，并配套可视化 Web 管理界面。延续 **Windows 免安装 exe** 双击即运行的形态。

主版本号升至 **3.0.0**：本轮完成两件大事——其一，把沿用原生 HTML/JS 的整套前端**彻底重构为 Vue 3 + Element Plus 的组件化岛屿架构**（API Key / 错误码映射 / 提供商 / 模型映射 / 计费 / 请求日志 / MCP 广场 / 公共外壳 / 登录页九大模块全部迁移收尾），交互一致性、暗色主题、可访问性全面对齐现代组件库；其二，**移除 OAuth 整套认证代码与数据库表**（攻击面归零），把对外鉴权收敛回单一 API Key 机制。此外新增可远程管理 Nacos 3.x 的 **MCP Server**，并把转发重试次数 / 间隔做成可配置项。

### 📦 下载

| 文件 | 说明 |
| --- | --- |
| `VibeCodingProxy-v3.0.0.exe` | Windows 64 位免安装单文件，双击运行（**带应用图标**） |

- **大小**：约 15 MB
- **系统要求**：Windows 10 / 11（64 位），无需预装 Python 或任何依赖

### ✨ 本版本更新（v2.0.0 → v3.0.0）

- **前端全面迁移至 Vue 3 + Element Plus**
  - 引入 Vue 3（全局构建版）+ Element Plus + Element Plus Icons + 暗色主题样式，按「面板 = Vue 岛屿」的方式逐模块重构，所有面板共享一套 `globalProperties` 工具函数与全局桥接
  - 完成九大模块的迁移收尾：**API Key 面板** → **错误码映射** → **提供商管理** → **模型映射** → **计费管理 + 请求日志** → **MCP 广场 / 公共外壳 / 登录页**；`templates/index.html` 从臃肿的单体模板瘦身，废弃的原生按钮 / 弹窗 / 开关 / 分页 / badge 等 CSS / JS 全部清理
  - **公共外壳**迁移为 Vue 岛屿：`el-tag` 版本徽章三态（🆕 有新版 / ✓ 已是最新 / 灰色降级）+ `el-tabs` 路由替换原生 `switchTab`，URL hash 双向同步，`_suppressReload` 标志避免初始化双倍请求且保持激活态视觉同步；`showFeatureToast` 退役，统一用 `ElMessage` / `ElNotification`
  - 提供商启用 / 停用、刷新等操作的 toast 提示统一改用 `ElMessage`
  - 视觉与交互一致性显著提升：暗色主题统一、表单校验与加载态对齐组件库、响应式布局更稳健

- **移除 OAuth 整套代码，攻击面归零**
  - 删除 `oauth.py`（-276 行）及 OAuth 相关的 `oauth_clients` / `oauth_codes` / `oauth_tokens` 三张数据库表，对外鉴权收敛回单一的 **API Key** 机制，认证攻击面与维护负担降到最低
  - 配套 schema 迁移：v7 → **v8**（先给 `oauth_tokens.access_token` 加命名唯一索引，堵 `alg=none` JWT 伪造认证绕过）、v8 → **v9**（删除三张 OAuth 表），**幂等**执行，老库平滑迁移不丢其余数据

- **新增 Nacos MCP Server（9 工具，零新依赖）**
  - 让 Claude Code / Codex 等 AI 客户端通过 **MCP 协议（Streamable HTTP）** 远程管理 **Nacos 3.x** 的命名空间与配置，`/mcp` 路由嵌入 Flask 单进程，复用现有 API Key 鉴权，手写 JSON-RPC 2.0，**零新第三方依赖**
  - 工具共 9 个（`nacos_` 前缀）：命名空间 `list` / `create` / `update` / `delete`；配置 `list` / `get` / `publish` / `delete` / `get_history`
  - **连接参数设计（关键决策）**：Nacos 的 `console_url` / `auth_url` / 账号 / 密码**不在本项目配置**，而是由 MCP 客户端在 `X-Nacos-Console-Url` / `X-Nacos-Auth-Url` / `X-Nacos-Username` / `X-Nacos-Password` 四个 HTTP headers 携带，服务端按请求读取、**零落盘**。任意 Nacos 集群都能接入、不锁定地址，且天然支持多实例；`accessToken` 按 `(console_url, username)` 做进程级缓存
  - 前端 MCP / Nacos 面板改为纯展示：端点 URL、API Key、客户端 headers 配置示例（Claude Code / Codex）、9 工具清单；更新 Nacos 3.x 授权地址样例

- **转发重试次数与间隔改为可配置**
  - 原本硬编码的「最多重试 3 次、间隔 1 秒」做成**降级设置**里可调的配置项（`degradation_retry_count` 默认 3 / `degradation_retry_delay` 默认 1.0 秒），便于按上游稳定性灵活调优
  - `_post_with_retry` 默认参数改为 `None`，运行时动态从降级配置读取，**UI 改完无需重启即时生效**，所有转发路径零改动；对非法负值自动兜底回退默认值；全新库与老库均补默认种子值

- **修复关闭命令行窗口后进程残留问题**
  - 之前关闭 exe 弹出的控制台窗口，后台 Flask 进程可能残留继续占用端口。本次修复关闭窗口即干净退出

- **MCP 广场布局优化**
  - Tab 改为**卡片式布局**；默认收起并改为**面板内滚动**，避免长内容撑开整个页面

- **请求日志面板详情优化**
  - 优化详情展示与 **SSE 解析**；日志详情改为**点击时按需查询**（而非一次性拉全量）；详情弹窗**增高并随视口自适应**

### 🚀 快速开始

1. 下载 `VibeCodingProxy-v3.0.0.exe`，放到任意目录（建议新建一个空文件夹，数据库文件会生成在同目录）。
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
5. 在 Web 界面配置上游提供商、模型映射、API Key 即可开始转发；需要「主备」语义时在设置里开启「严格优先级（逐级下放）」开关；需要按上游稳定性调优时在降级设置里调整「转发重试次数」与「重试间隔秒数」。
6. （可选）让 Claude Code / Codex 等 MCP 客户端接入 Nacos 管理：在客户端配置 `/mcp` 端点 URL 与本服务 API Key，并通过 `X-Nacos-*` headers 携带目标 Nacos 集群连接参数（详见 MCP 广场面板的配置示例）。

### ⚠️ 升级说明（v2.0.0 → v3.0.0）

- **从任意旧版本 exe 升级**：把旧版本同目录的 `proxy.db` 复制到新 exe 同目录，即可保留历史数据。
- **从 v2.0.0 升级**：库已在 v7，首次启动 v3.0.0 会从 v7 依次升到 **v8 → v9**：
  - v7 → v8：给 `oauth_tokens.access_token` 加命名唯一索引（堵 `alg=none` JWT 伪造认证绕过）；
  - v8 → v9：删除 OAuth 相关三张表（`oauth_clients` / `oauth_codes` / `oauth_tokens`），攻击面归零；
  - **OAuth 表被删除是预期行为**——本版本起 OAuth 认证代码已整套移除，对外鉴权统一为 API Key，相关历史表无实际用途；
  - 其余历史映射 / 设置 / 日志字段不变、不丢失。
- **升级后新增的降级配置项**：`degradation_retry_count`（默认 3）与 `degradation_retry_delay`（默认 1.0 秒）会由迁移与种子值自动补上，默认值与旧版硬编码行为（重试 3 次 / 间隔 1 秒）完全一致，**升级后转发重试行为零变化**；需要调优时在降级设置弹窗修改，改完即时生效。
- **升级后行为变化（重要）**：
  - 前端整体换肤为 Vue 3 + Element Plus，**外观与交互方式有较大变化**，但所有原有功能（提供商 / 模型映射 / API Key / 错误码 / 计费 / 日志 / 降级 / MCP 广场）均保留，配置数据互通。
  - OAuth 认证入口已移除，若旧版本曾依赖 OAuth 登录，请改用 **API Key** 或内置管理员账号登录。
  - 标题旁版本徽章仍会访问 GitHub API 检查最新版；断网或 GitHub 不可达时降级为灰色静态版本号，不影响使用。
- `proxy.ini` 是可选文件；管理员账号 / 密码以该文件为准，修改后重启会覆盖库中已有管理员，日常改密请直接编辑该文件。

### 📝 本版本变更清单（v2.0.0..v3.0.0）

| 类型 | 变更 |
| --- | --- |
| ✨ 特性 | 前端全面迁移至 Vue 3 + Element Plus：九大模块（API Key / 错误码 / 提供商 / 模型映射 / 计费 / 日志 / MCP 广场 / 公共外壳 / 登录页）组件化重构收尾，暗色主题与交互一致性对齐组件库 |
| ✨ 特性 | 新增 Nacos MCP Server：`/mcp` 嵌入 Flask，9 个 `nacos_` 工具远程管理 Nacos 3.x 命名空间与配置，连接参数经客户端 `X-Nacos-*` headers 携带、零落盘、零新依赖 |
| ✨ 特性 | 转发重试次数 / 间隔可配置（`degradation_retry_count` 默认 3 / `degradation_retry_delay` 默认 1.0），UI 改完即时生效，默认值与旧版一致 |
| 🔒 安全 | 移除 OAuth 整套代码（`oauth.py` -276 行）与三张 OAuth 表，对外鉴权收敛回单一 API Key，攻击面归零 |
| 🐛 修复 | 关闭命令行窗口后后台进程残留占用端口的问题 |
| 🎨 UI | MCP 广场改卡片式布局、默认收起并面板内滚动；请求日志详情优化 SSE 解析、改为点击按需查询、详情弹窗增高自适应；提供商启停提示统一用 `ElMessage` |
| 🔧 架构 | 数据库 schema 升级到 v9：v8 给 `oauth_tokens.access_token` 加命名唯一索引；v9 删除 OAuth 三张表；新增 `degradation_retry_count` / `degradation_retry_delay` 降级配置项（迁移 + 种子值幂等补全） |

---

> 源码与完整文档见仓库 README.md。
