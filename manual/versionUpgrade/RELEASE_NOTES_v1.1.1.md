# 发布标题（复制到 Release name）

```
v1.1.1：Vibe Coding 服务转发 支持 proxy.ini 外部配置与缓存命中 Token 展示（Windows 免安装版）
```

---

# 发布说明（复制到 Describe this release 正文）

## 🎉 Vibe Coding 服务转发 v1.1.1

基于 Flask 的多供应商 AI API 代理服务，把多家上游 LLM 统一抽象成 **Anthropic Messages API**、**OpenAI Chat Completions API**、**OpenAI Responses API** 三种接口对外暴露，并配套可视化 Web 管理界面。本版本延续 **Windows 免安装 exe** 双击即运行的形态，新增外部配置文件并补全缓存命中 Token 的可观测性。

### 📦 下载

| 文件 | 说明 |
| --- | --- |
| `VibeCodingProxy-v1.1.1.exe` | Windows 64 位免安装单文件，双击运行 |

- **大小**：约 17 MB
- **系统要求**：Windows 10 / 11（64 位），无需预装 Python 或任何依赖

### ✨ 本版本更新

- **支持 `proxy.ini` 外部配置**
  - 在 exe 同目录（源码方式为项目根目录）放置 `proxy.ini` 即可配置**启动端口**与**管理员账号/密码**，无需修改源码
  - `[admin]` 段是管理员账户的唯一事实来源，每次启动都会校准 `admin_users` 表，修改用户名/密码后重启即生效（改用户名会自动清理旧账号）
  - 没有 `proxy.ini` 时使用内置默认值（端口 `5000`、管理员 `admin / admin123`），与 v1.0.0 行为一致

- **日志补全缓存命中 Token 展示**
  - OpenAI 直转路径（流式与非流式）此前未采集缓存命中 token，现已从 `usage.prompt_tokens_details.cached_tokens` 提取并记录，与 Anthropic 路径的 `cache_read_input_tokens` 统一
  - 前端将「输入 Token」「输出 Token」两列合并为「入/缓/出(token)」一列，以 入 / 缓存命中 / 出 三段紧凑展示

### 🚀 快速开始

1. 下载 `VibeCodingProxy-v1.1.1.exe`，放到任意目录（建议新建一个空文件夹，数据库文件会生成在同目录）。
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

- 从 v1.0.0 的 exe 升级：把旧版本同目录的 `proxy.db` 复制到新 exe 同目录，即可保留历史数据。
- `proxy.ini` 是可选文件；管理员账号/密码以该文件为准，修改后重启会覆盖库中已有管理员，日常改密请直接编辑该文件。

---

> 源码与完整文档见仓库 README.md。
