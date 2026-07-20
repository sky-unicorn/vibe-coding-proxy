# 发布标题

```
v2.0.0：Vibe Coding 服务转发 新增严格优先级（逐级下放）模型选择模式、provider 启停级联同步其下映射模型状态、版本号徽章自动检测 GitHub 最新版、刷新保持当前 Tab、程序图标、多项 UI 优化与 codex 误报修复（Windows 免安装版）
```

---

# 发布说明

## 🎉 Vibe Coding 服务转发 v2.0.0

基于 Flask 的多供应商 AI API 代理服务，把多家上游 LLM 统一抽象成 **Anthropic Messages API**、**OpenAI Chat Completions API**、**OpenAI Responses API** 三种接口对外暴露，并配套可视化 Web 管理界面。延续 **Windows 免安装 exe** 双击即运行的形态。

主版本号升至 **2.0.0**：本轮在降级调度、供应商生命周期管理、版本自检三个核心维度补齐了「主备语义」与「可运维性」，从「能转发」走向「能稳定运维」；并对 provider 禁用 → mapping 联动这条链路做了**前后端 + 历史数据**三层闭环，彻底消除「mapping 显示启用却实际无效」的假启用状态。

### 📦 下载

| 文件 | 说明 |
| --- | --- |
| `VibeCodingProxy-v2.0.0.exe` | Windows 64 位免安装单文件，双击运行（**带应用图标**） |

- **大小**：约 15 MB
- **系统要求**：Windows 10 / 11（64 位），无需预装 Python 或任何依赖

### ✨ 本版本更新（v1.2.2 → v2.0.0）

- **新增严格优先级（逐级下放）模型选择模式**
  - 原降级机制为全池加权轮询，低优先级模型仍按概率被选中，「主备」语义不成立。新增 `degradation_strict_priority` 辅助开关（依赖主降级开关）：开启后仅在当前最高优先级层级内选择候选，整层降级才逐级下放到更低优先级层，全部层级降级则回退全量池（不拒服务）
  - **纯加法改动，默认关闭**：关闭时 Anthropic / OpenAI Chat / Codex Responses 三条转发路径的过滤行为与改动前逐字节等价（走原 `_filter_candidates_by_degradation` 分支）；层内选择沿用 `_pick_weighted_round_robin`，降级状态管理与 duration 复用现有机制

- **provider 启停级联同步其下映射模型状态（含假启用闭环）**
  - provider 停用时其下所有 `model_mappings` 联动停用，启用时联动启用，避免上游已下线却仍把请求路由过去
  - 为避免覆盖用户单独禁用的 mapping，新增 `model_mappings.disable_reason` 列（与 `providers` 表对齐），区分手动禁用（`manual`）与因 provider 禁用而联动禁用（`provider_disabled`）。**启用 provider 时只恢复联动禁用的 mapping，保留用户手动禁用的状态**
  - v6 schema 迁移：`model_mappings` 新增 `disable_reason` 列，历史禁用一律回填 `manual`；手动启停与计费恢复两处入口均触发级联，同一事务保证原子性
  - 前端 `toggleProvider` 切换后同步刷新模型列表，并弹出 2 秒自动关闭的 toast 提示
  - **前后端闭环「假启用」**：原级联机制虽会在 provider 禁用时同步把 mapping 置为 disabled，但前端 toggle 仍可点击、后端不拦截，造成 mapping「显示启用却因路由层 `p.enabled=1` 过滤而实际无效」的假启用状态。本次闭环该语义——`get_model_mappings` 返回 `provider_enabled` 字段；`update_model_mapping` 在所属 provider 禁用时拒绝开启（抛 `ValueError`，禁用操作不受影响），`/api/models/<id>` 捕获后返回 400 + 错误信息；前端列表 toggle 按 `provider_enabled` **灰色锁定并加 tooltip**，编辑弹窗联动锁定「启用」开关，操作失败弹 toast 提示原因

- **修正历史假启用 mapping（v7 数据迁移）**
  - v6 引入 `disable_reason` 列**之前**，provider 禁用并不会级联到 mapping（当时无级联机制），老库迁移到 v6 时的回填只看 mapping 自身的 `enabled`、未参考 provider 状态，导致「provider 已禁用但 mapping `enabled=1 / disable_reason=''`」的**历史假启用残留**：UI 显示启用却因路由层 `p.enabled=1` 过滤而实际无效
  - 新增 v6 → **v7** 数据修正迁移，把这些 mapping 改写为 `enabled=0 + disable_reason='provider_disabled'`，与运行时级联禁用语义对齐。**幂等**：以 settings 标记键做一次性守卫，`UPDATE` 仅命中不一致行，手动禁用（`manual`）与 provider 启用的 mapping 不受影响

- **新增版本号徽章，自动检测 GitHub 最新版**
  - 新建 `version.py` 作为应用版本号唯一来源（`APP_VERSION` / `RELEASES_URL` / `GITHUB_LATEST_API`）
  - 标题旁版本徽章三种态：🆕 有新版本（橙色，可点击跳转 releases）/ ✓ 已是最新版（绿色）/ `v2.0.0`（灰色，断网降级）；⟳ 按钮手动强制检查
  - `/api/version` 端点：拉取 GitHub releases/latest 对比版本号，settings 表缓存 1 小时（403 限流时 24 小时），`threading.Lock` 防并发重复请求，3s 超时保证 UI 不卡，`force=1` 参数绕过缓存强制刷新

- **刷新页面保持当前 Tab 页**
  - 之前每次 F5 刷新都会回到首个 Tab（提供商管理）。现在通过 URL hash 记录当前 Tab：切换时用 `history.replaceState` 写入 `#<tab>`，初始化时从 hash 恢复。用 `replaceState` 而非 `pushState`，避免切 Tab 污染浏览器历史

- **优化模型映射列表与角色映射弹窗 UI**
  - 模型映射列表「状态」「降级状态」两列互换位置
  - 角色映射弹窗美化：提示信息改为左侧紫色强调边框的提示卡、`code` 标签用主色高亮；每条规则独立卡片包裹、hover 边框泛紫、输入框加聚焦态；箭头改紫色、删除按钮固定 28×28 居中；新增空状态提示（删完规则显示「暂无映射规则」占位）；「+ 添加规则」改为与输入框同宽的虚线按钮、hover 变紫

- **优化模型映射 `max_tokens` 字段说明文案**
  - 表头与编辑窗 label 由 `max_tokens` 改为中文「最大输出 Token 数」；描述明确：仅当上游有上限且调用方传入超限报错时才需配置（以 `kimi-2.7` 上限 32768、调用方传 64000 报错为例）

- **修复 codex 正常请求被日志详情框误显示为错误**
  - codex 经 Responses 流式代理的正常请求（`exit=normal`），日志详情框一直弹红色「错误:」块造成误判。根因两层：
    1. `finally` 调 `resp.close()` 后，后台读取线程的 `iter_lines` 因 `resp.raw` 被置空抛 `AttributeError`，被 `_upstream_reader` 捕获后写入 `upstream_error_type`，正常请求被误报 `upstream_err`。仅在 `exit_reason==normal`（主动关闭的预期异常）时清空 `upstream_error_type`
    2. 流式诊断串 `[exit=...]` 即便 `status=success` 也被写入 DB 的 `error_msg` 字段，Web UI 只要 `error_msg` 非空就以红色「错误:」块渲染。收紧为仅 `status=error` 时写入 `error_msg`，诊断串始终 `print` 到控制台保留排障能力
  - 不影响降级判定（其条件 `status==error` 本就不在成功路径触发）

- **新增程序图标 `ICON_PROMPT.png`**
  - 打包出的 exe 与任务栏图标使用该图，视觉识别更清晰

### 🚀 快速开始

1. 下载 `VibeCodingProxy-v2.0.0.exe`，放到任意目录（建议新建一个空文件夹，数据库文件会生成在同目录）。
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
5. 在 Web 界面配置上游提供商、模型映射、API Key 即可开始转发；需要「主备」语义时在设置里开启「严格优先级（逐级下放）」开关；停用 provider 后其下映射模型会自动联动停用。

### ⚠️ 升级说明（v1.2.2 → v2.0.0）

- **从 v1.0.0 / v1.1.1 / v1.2.0 / v1.2.1 / v1.2.2 的 exe 升级**：把旧版本同目录的 `proxy.db` 复制到新 exe 同目录，即可保留历史数据。
- **从 v1.2.2 升级**：库已在 v5，首次启动 v2.0.0 会从 v5 依次升到 **v6 → v7**：
  - v5 → v6：`model_mappings` 新增 `disable_reason` 列，历史禁用一律回填 `manual`；
  - v6 → v7：数据修正迁移，把「provider 已禁用但 mapping `enabled=1`」的历史假启用残留改写为 `enabled=0 + disable_reason='provider_disabled'`，使其与运行时级联禁用语义对齐。
  - 其余历史映射字段不变、不丢失。
- **升级后行为变化（重要）**：
  - 严格优先级模式 **默认关闭**，升级后转发行为与旧版逐字节等价；需要「主备」语义时手动在设置开启。
  - provider 停用后其下映射模型会 **自动联动停用**（标为 `provider_disabled`）；重新启用 provider 时，**仅恢复因 provider 停用而联动的禁用，保留用户手动禁用的状态**——不会误启你单独关掉的 mapping。
  - provider 处于禁用状态时，其下映射模型的「启用」开关在前端**灰色锁定**、后端拒绝开启，避免再产生假启用；v7 迁移会一次性清掉历史库里已存在的假启用残留。
  - 标题旁新增版本徽章，会访问 GitHub API 检查最新版；断网或 GitHub 不可达时降级为灰色静态版本号，不影响使用。
- `proxy.ini` 是可选文件；管理员账号 / 密码以该文件为准，修改后重启会覆盖库中已有管理员，日常改密请直接编辑该文件。

### 📝 本版本变更清单（v1.2.2..v2.0.0）

| 类型 | 变更 |
| --- | --- |
| ✨ 特性 | 新增严格优先级（逐级下放）模型选择模式 `degradation_strict_priority`，补齐「主备」语义，默认关闭、零回归 |
| ✨ 特性 | provider 启停级联同步其下映射模型状态：新增 `disable_reason` 列区分手动禁用与联动禁用，恢复 provider 时不误启用户手动禁用的 mapping |
| ✨ 特性 | provider 禁用时前后端闭环禁止开启其下 mapping（后端 400 拦截 + 前端灰色锁定 tooltip），消除「显示启用却实际无效」的假启用 |
| ✨ 特性 | 新增版本号徽章自动检测 GitHub 最新版（`version.py` + `/api/version` + settings 缓存，断网降级） |
| ✨ 特性 | 刷新页面保持当前 Tab 页（URL hash + `history.replaceState`） |
| 🎨 UI | 模型映射列表列顺序调整、角色映射弹窗样式美化、`max_tokens` 字段中文说明优化 |
| 🐛 修复 | codex 正常请求被日志详情框误显示为错误（`exit=normal` 清空 `upstream_error_type` + 仅 `status=error` 写 `error_msg`） |
| 🎨 资源 | 新增程序图标 `ICON_PROMPT.png`，exe / 任务栏使用该图 |
| 🔧 架构 | 数据库 schema 升级到 v7：v6 给 `model_mappings` 新增 `disable_reason` 列（迁移幂等回填 `manual`）；v7 数据修正迁移把历史假启用残留改写为 `provider_disabled` |

---

> 源码与完整文档见仓库 README.md。
