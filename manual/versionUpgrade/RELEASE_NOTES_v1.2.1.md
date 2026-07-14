# 发布标题

```
v1.2.1：Vibe Coding 服务转发 修复 codex 经代理卡死、新增模型映射级 reasoning_effort 透传开关（Windows 免安装版）
```

---

# 发布说明

## 🎉 Vibe Coding 服务转发 v1.2.1

基于 Flask 的多供应商 AI API 代理服务，把多家上游 LLM 统一抽象成 **Anthropic Messages API**、**OpenAI Chat Completions API**、**OpenAI Responses API** 三种接口对外暴露，并配套可视化 Web 管理界面。本版本延续 **Windows 免安装 exe** 双击即运行的形态，重点修复了 codex CLI 经代理「请求成功但卡死」的问题，并把 `reasoning_effort` 思考强度透传改为模型映射表上的显式开关。

### 📦 下载

| 文件 | 说明 |
| --- | --- |
| `VibeCodingProxy-v1.2.1.exe` | Windows 64 位免安装单文件，双击运行 |

- **大小**：约 17 MB
- **SHA256**：`4daf5f7dc74d07b5d61dfb410eb9a0908324eda0ee66bad9e1867a79288905a8`
- **系统要求**：Windows 10 / 11（64 位），无需预装 Python 或任何依赖

### ✨ 本版本更新（v1.2.0 → v1.2.1）

- **修复 codex CLI 经代理「请求成功但卡死」的问题**（Responses↔Chat 转换层 reasoning 双向透传与空输出兜底）
  - codex CLI 走 OpenAI Responses 协议，代理需在 Responses↔Chat 之间做跨协议转换；此前转换层在请求侧与响应侧分别丢失了推理（reasoning）信息，导致 codex 收到响应后无限等待、界面卡死
  - **请求侧**：Responses 请求的 `reasoning.effort`（codex 的 `model_reasoning_effort`）被提取并暂存为私有键，在 failover 循环内、目标模型确定后透传为 Chat 上游的 `reasoning_effort` 字段
  - **响应侧**：上游返回的 `reasoning_content` 流式字段被转换为 OpenAI Responses API 的 `reasoning_summary_*` 事件序列回传给 codex，codex 会在 UI 中展示推理过程；同时补齐空输出兜底与 `call_id` 合成，避免无效响应触发卡死
  - 修复后 codex CLI 经代理可正常完成推理请求，不再卡死

- **新增模型映射级 `reasoning_effort` 透传开关**
  - 上一版修复中，是否透传 `reasoning_effort` 用「目标模型名包含 `minimax` 子串」启发式判定是否跳过；这种按字符串猜测上游兼容性的做法极其脆弱（上游可任意重命名，如讯飞 MaaS 的 `xopglm52`、火山转发的 `minimax-m3`，判定直接失效）
  - 现改为模型映射表上的**显式开关** `reasoning_effort_supported`，由运维按上游实际能力配置：开关回答「该上游认不认 `reasoning_effort` 字段名」，强度（`low`/`medium`/`high`）由调用方决定，代理只做兼容性透传
  - 模型映射弹窗新增「透传思考强度参数（`reasoning_effort`）」开关；**默认打开**——GLM / DeepSeek 原生认此字段，MiniMax 接受但不调深度（无害），火山 / 讯飞 / Kimi 参数名不同、传过去多半被上游忽略（不报错）；若个别上游实测出现 400，可在该映射行关闭开关
  - 值 `low` / `medium` / `high` 原样透传、不翻译：认该字段的国产上游都用同一套语义，原样转发即对
  - schema 升级到 v2：`model_mappings` 表新增 `reasoning_effort_supported` 列，**老库升级与全新库默认均为打开**（迁移幂等回填 1），按备份 → DROP → CREATE → 回填流程执行，历史映射数据不丢失

### 🚀 快速开始

1. 下载 `VibeCodingProxy-v1.2.1.exe`，放到任意目录（建议新建一个空文件夹，数据库文件会生成在同目录）。
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
5. 在 Web 界面配置上游提供商、模型映射、API Key 即可开始转发。

### ⚠️ 升级说明

- **从 v1.0.0 / v1.1.1 / v1.2.0 的 exe 升级**：把旧版本同目录的 `proxy.db` 复制到新 exe 同目录，即可保留历史数据。
- 首次启动 v1.2.1 时，迁移框架会自动把库从 v1 升到 v2，为 `model_mappings` 补齐 `reasoning_effort_supported` 列（默认回填 1 / 打开），按备份 → 重建 → 回填的流程执行，历史模型映射不丢失；老映射升级后默认即透传 `reasoning_effort`，无需逐条手动打开。
- 旧版代理对 MiniMax 等「按名字跳过」的模型，升级后行为变化：开关默认打开，`reasoning_effort` 会原样透传给 MiniMax（接受但不调深度，无害）；若你的某个上游实测因此出现 400，到该映射行的「透传思考强度参数」开关关闭即可。
- `proxy.ini` 是可选文件；管理员账号 / 密码以该文件为准，修改后重启会覆盖库中已有管理员，日常改密请直接编辑该文件。

### 📝 本版本变更清单（v1.2.0..v1.2.1）

| 类型 | 变更 |
| --- | --- |
| 🐛 修复 | codex CLI 经代理「请求成功但卡死」：Responses↔Chat 转换层补齐 reasoning 请求侧透传、响应侧事件序列、空输出兜底与 call_id 合成 |
| ✨ 特性 | 模型映射新增 `reasoning_effort_supported` 透传开关，替换按上游名字猜测的启发式判定 |
| 🔧 架构 | 数据库 schema 升级到 v2：`model_mappings` 新增 `reasoning_effort_supported` 列，迁移幂等回填，老库升级与全新库默认均打开 |
| 📄 文档 | 新增设计文档 `DESIGN_reasoning_effort_toggle.md`；codex 接入手册同步说明透传开关与兼容性参考 |

---

> 源码与完整文档见仓库 README.md。
