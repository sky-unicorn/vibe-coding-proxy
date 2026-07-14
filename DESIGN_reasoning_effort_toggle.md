# 设计方案：模型映射级推理强度透传开关

> 状态：待审核
> 关联：上一轮 codex 卡死 Bug 修复（`_apply_reasoning_effort`）、cc-switch `supports_reasoning_effort` 启发式
> 范围：仅 Responses->Chat 路径（codex CLI 等）；**不动** Anthropic->Anthropic 路径（Claude Code）

---

## 1. 背景

上一轮修复了 codex CLI 经代理"请求成功但卡死"的 Bug，根因之一是 Responses↔Chat 转换层**请求侧丢弃 reasoning**。已用 `_apply_reasoning_effort` 兜底，但该函数用**目标模型名包含 "minimax" 子串**来判定是否跳过透传：

```python
# proxy.py:214-216（当前实现）
model_lower = (target_model or "").lower()
if "minimax" in model_lower:
    return  # 跳过透传
body["reasoning_effort"] = effort
```

这种按字符串猜测上游兼容性的做法**极其脆弱**：

- 上游可任意重命名（DB 实测：讯飞 MaaS 的 `xopglm52` / `xopdeepseekv4pro`，火山转发的 `minimax-m3` 等）-> 判定直接失效
- `mmm3` 这种被改名的 MiniMax -> `"minimax" in model_lower` 命中失败 -> 被错误地透传（与 MiniMax 的真实行为相反）
- 无法表达"该上游用 `enable_thinking` / `thinking` 而非 `reasoning_effort`"等差异

应改造为**模型映射表上的显式开关**，由运维按上游实际能力配置。

---

## 2. 各上游 reasoning_effort 兼容性事实（查证于官方文档）

本节是本次重写的核心依据。`reasoning_effort`（OpenAI/Chat）的三个取值 `low/medium/high` 在**认这个字段名**的国产模型里，语义就是字面的"思考链预算/深度档位"，无需翻译；各家差别在"认不认字段名"与"认到什么程度"。

> 项目当前实际配置的上游（DB 实测）：GLM(id=2)、MiniMax(id=3)、讯飞 MaaS(id=8, model 前缀 `xop`)、火山 Ark(id=12)。

| 上游 | 认 `reasoning_effort` 字段名 | 参数 & 取值 | 透传 `low/medium/high` 的实际效果 |
|---|---|---|---|
| **GLM 智谱**（`glm-5.1/5.2`、`xopglm51/52`） | ✅ 原生支持 | `reasoning_effort`，取值 `max/xhigh/high/medium/low/minimal`（6 档），默认 `max` | low/medium/high 是合法子集，**直接透传即生效** |
| **DeepSeek**（`xopdeepseekv4*`） | ✅ OpenAI 格式支持 | OpenAI 格式 `reasoning_effort`；Anthropic 格式 `output_config.effort`（max/high） | OpenAI 路径直接透传 OK |
| **MiniMax**（`MiniMax-M3/M2.7`） | ⚠️ 接受字段但**不调深度** | `reasoning_effort` 值 `minimal/low/medium/high` 仅触发 "Adaptive Thinking"；对 M3 推理深度**无影响**；M2.x 推理根本关不掉 | 传过去**不会报 400**，只是不改变深度（无害） |
| **火山 Ark/Doubao** | ❌ 用 `thinking` 参数 | 深度思考用 `thinking.type`，`reasoning_tokens` 是输出字段 | 直接传 `reasoning_effort` 大概率被忽略 |
| **讯飞星火 MaaS**（`xop*`） | ❓ 官方无 `reasoning_effort` 文档 | 讯飞自家用"思考内容"概念，参数名不统一 | OpenAI 兼容层可能忽略未知字段，需实测 |
| **Kimi**（`kimi-k2.7-code`） | ❓ 参数名待定 | `kimi-thinking-preview` 用独立 model id 控制思考 | 直接传可能被忽略 |

### 2.1 由此推导的三条设计约束

1. **值映射层不需要**。认这个字段的模型（GLM/DeepSeek/MiniMax）都用同一套 `low/medium/high` 语义，代理**原样透传**就是对的。开关只回答"认/不认字段名"，不回答"怎么翻译值"。-> 设计文档第 3 节"简单开关"的决策被坐实，三态/值映射都不必要。

2. **MiniMax"跳过"的判断过度保守**。原代码 `if "minimax" in model_lower: return` 的预设是"传无效字段可能触发 400"，但官方文档显示 M3 **接受** `reasoning_effort`、不会报错（只是不调深度）。所以开关交给运维后，MiniMax 映射**默认也可以打开**（打开了也无害），不必因名字含 minimax 强制关。

3. **开关语义应正名为"透传 reasoning_effort 字段"**，而非"支持高强度思考"。强度由调用方（codex/claude code）决定，代理只做兼容性透传--开关回答的是"该上游接不接 `reasoning_effort` 这个字段名"。

### 2.2 术语澄清（架构师备注）

| 术语 | 含义 |
|---|---|
| `reasoning.effort`（Responses API） | Codex CLI 用，取值 `low`/`medium`/`high` |
| `reasoning_effort`（Chat API） | OpenAI 推理参数，控制思考 token 预算；GLM/DeepSeek 沿用同名字段 |
| `thinking.budget_tokens`（Anthropic API） | Claude Code 用，预算式思考控制 |
| `output_config.effort`（DeepSeek Anthropic 端点） | DeepSeek 的 Anthropic 格式思考强度参数，取值 `high`/`max` |

**核心定位**：思考强度（low/medium/high）由**调用方**（codex / claude code）决定，**代理只做兼容性透传**--开关回答的是"该上游接不接 `reasoning_effort` 字段名"。

### 2.3 为什么 Claude Code 没遇到同样的问题（论证开关作用域）

Claude Code 走 Anthropic 协议 -> 代理 `_proxy_anthropic` **原样转发**到上游 `anthropic_url`（同协议直转），`thinking` 是原生参数：

- **DeepSeek**：`_adapt_deepseek_anthropic` 把 `thinking.budget_tokens` 转为 `output_config.effort`
- **GLM / MiniMax / 讯飞 / 火山**：无适配器，`thinking` 原样透传

codex 则必须 Responses->Chat 跨协议转换，`reasoning_effort` 跨过协议边界才丢失。
**结论**：开关作用域限定在 **Responses->Chat 路径**（`reasoning_effort`），不动 Anthropic 路径。

---

## 3. 设计决策（已与用户确认 + 本轮文档查证修正）

| 决策点 | 选择 | 理由 |
|---|---|---|
| 字段形态 | **简单开关**（二进制） | 用户明确选择。第 2.1 节证实值无需映射，三态/值翻译都不必要；开关就是"透传/不透传 `reasoning_effort` 字段名" |
| 开关语义命名 | **透传 `reasoning_effort` 字段** | 正名：不是"支持高强度思考"，而是"接不接该字段名"。强度由调用方决定，代理只做兼容性透传 |
| 默认值 | **1（pass / 透传）** | 「默认打开」：GLM/DeepSeek 原生认字段、透传有效；MiniMax 接受字段、不调深度但无害；火山/讯飞/Kimi 参数名不同、传过去多半被上游忽略（不报错）。故「默认透传」对绝大多数上游都是正确选择；个别上游若实测出现 400，运维可在 UI 逐条关闭 |
| 字段名 | `reasoning_effort_supported` | 明确表达"是否支持 reasoning_effort 参数"，与请求参数 `reasoning_effort`（值）区分 |

### 3.1 迁移回填策略（基于第 2 节事实）

代码回填统一 **1（打开/透传）**。依据：第 2 节矩阵显示，认 `reasoning_effort` 的上游（GLM/DeepSeek）透传有效，接受但忽略的上游（MiniMax）无害，参数名不同的上游（火山/讯飞/Kimi）传过去也多半被忽略不报错——「默认打开」对绝大多数上游都是正确选择。个别上游若实测出现 400，运维在 UI 逐条关闭即可。

| 上游分组 | 回填默认 | 实测后建议 |
|---|---|---|
| GLM（`glm-5.*`、`xopglm*`） | 1（打开） | 保持打开：原生认 `reasoning_effort`，6 档，low/medium/high 合法 |
| DeepSeek（`xopdeepseekv4*`） | 1（打开） | 保持打开：OpenAI 格式认 `reasoning_effort` |
| MiniMax（`MiniMax-M*`、火山转的 `minimax-m3`） | 1（打开，无害） | 保持打开：接受字段、不报错，只是不调深度 |
| 火山 Ark / 讯飞 / Kimi | 1（打开） | 视实测：官方参数名非 `reasoning_effort`，传过去大概率被忽略；若实测出现 400 再关闭 |

---

## 4. Schema 变更

### 4.1 新列定义

```sql
-- 在 _create_latest_schema 的 model_mappings CREATE TABLE 中新增：
reasoning_effort_supported INTEGER NOT NULL DEFAULT 1
-- 0 = 不透传（保守跳过：pop 私有键，不发任何字段）
-- 1 = 透传（按 OpenAI 兼容格式发 reasoning_effort=<effort>，原值 low/medium/high 不翻译）
-- 默认 1（打开）：见第 3.1 节，对绝大多数上游透传即正确；个别上游实测 400 再关闭
```

### 4.2 特征检测（`_calibrate_user_version`）

```python
# 现有 v1 检测保持不变
cols_pbc = [r[1] for r in conn.execute("PRAGMA table_info(provider_billing_config)").fetchall()]
cols_mm  = [r[1] for r in conn.execute("PRAGMA table_info(model_mappings)").fetchall()]
if "cache_read_price_per_million" in cols_pbc:
    detected = 1
if "reasoning_effort_supported" in cols_mm:
    detected = 2  # v2 隐含 v1
```

**注意**：本迁移**不**使用 settings 键做一次性守卫（仅用 `PRAGMA table_info` 列存在性自检），故 `init_db` 的 `is_fresh` 分支**无需**新增 settings 键。

---

## 5. 迁移实现（CLAUDE.md 严格遵循）

### 5.1 新迁移函数

```python
def _migrate_model_mappings_add_reasoning(conn):
    """v1->v2：model_mappings 表新增 reasoning_effort_supported 列，默认 1（打开/透传）。

    严格遵循 CLAUDE.md SQLite 迁移流程：备份 -> DROP -> CREATE -> 回填 -> 重建索引 -> 完整性校验。
    model_mappings 表当前规模 ~14 行，备份-重建开销可忽略。

    回填统一 1（默认打开/透传）：见 DESIGN 第 3.1 节，对绝大多数上游透传即正确；
    个别上游若实测出现 400，运维在 UI 逐条关闭。
    """
    cols = [row[1] for row in conn.execute("PRAGMA table_info(model_mappings)").fetchall()]
    if "reasoning_effort_supported" in cols:
        return  # 幂等：列已存在则跳过

    # 1. 备份
    mm_count_before = conn.execute("SELECT COUNT(*) FROM model_mappings").fetchone()[0]
    old_rows = conn.execute("SELECT * FROM model_mappings").fetchall()
    old_col_names = [d[0] for d in conn.execute("SELECT * FROM model_mappings LIMIT 0").description]
    dangling_before = conn.execute(
        "SELECT COUNT(*) FROM model_mappings WHERE provider_id NOT IN (SELECT id FROM providers)"
    ).fetchone()[0]

    # 2. DROP
    conn.execute("DROP TABLE model_mappings")

    # 3. CREATE（带新列）
    conn.execute("""
        CREATE TABLE model_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT NOT NULL,
            target_model TEXT NOT NULL,
            provider_id INTEGER NOT NULL,
            priority INTEGER NOT NULL DEFAULT 1,
            model_type TEXT NOT NULL DEFAULT 'text',
            max_tokens INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            role_mappings TEXT NOT NULL DEFAULT '[]',
            reasoning_effort_supported INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (provider_id) REFERENCES providers(id)
        )
    """)

    # 4. 重建索引（参考 _migrate_model_mappings 的模式）
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_model_mappings_alias_enabled "
        "ON model_mappings(alias, enabled)"
    )

    # 5. 回填（统一 1，默认打开/透传）
    for row in old_rows:
        rd = dict(zip(old_col_names, row))
        conn.execute(
            "INSERT INTO model_mappings (id, alias, target_model, provider_id, priority, "
            "model_type, max_tokens, enabled, role_mappings, reasoning_effort_supported) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rd["id"], rd["alias"], rd["target_model"], rd["provider_id"],
                int(rd.get("priority") or 1), rd.get("model_type") or "text",
                int(rd.get("max_tokens") or 0), int(rd.get("enabled", 1)),
                rd.get("role_mappings") or "[]",
                1,  # 默认打开：对绝大多数上游透传即正确；个别上游实测 400 再关闭
            ),
        )

    # 6. 完整性校验（与 _migrate_model_mappings 同模式）
    mm_count_after = conn.execute("SELECT COUNT(*) FROM model_mappings").fetchone()[0]
    if mm_count_after != mm_count_before:
        raise RuntimeError(f"model_mappings 迁移后行数不一致：{mm_count_before} -> {mm_count_after}")
    dangling_after = conn.execute(
        "SELECT COUNT(*) FROM model_mappings WHERE provider_id NOT IN (SELECT id FROM providers)"
    ).fetchone()[0]
    if dangling_after != dangling_before:
        raise RuntimeError(f"model_mappings 迁移后悬空 provider_id 数量变化")
```

### 5.2 注册到 `_MIGRATIONS`

```python
_MIGRATIONS = [
    (
        1,
        "添加缓存命中计费：provider_billing_config 表新增 cache_read_price_per_million 列",
        _migrate_billing_config_add_cache_read_price,
    ),
    (
        2,
        "添加模型映射推理强度透传开关：model_mappings 表新增 reasoning_effort_supported 列",
        _migrate_model_mappings_add_reasoning,
    ),
]

CURRENT_SCHEMA_VERSION = len(_MIGRATIONS)  # 自动 = 2
```

### 5.3 同步 `_create_latest_schema`

在 `model_mappings` 的 `CREATE TABLE`（L580-591）末尾 `role_mappings` 之后加：
```sql
reasoning_effort_supported INTEGER NOT NULL DEFAULT 1,
```

---

## 6. 后端代码改动（`proxy.py`）

### 6.1 `_apply_reasoning_effort`：去名字判定、改为显式开关

```python
def _apply_reasoning_effort(body, target_model, reasoning_effort_supported=False):
    """按模型映射的 reasoning_effort_supported 开关决定是否透传 reasoning_effort 字段。

    从 body.pop('_codex_reasoning_effort') 取出 _convert_responses_to_chat 暂存的
    effort（如 'high'/'medium'/'low'，OpenAI Responses 三档）。无值或非字符串则直接返回。

    行为：
      - reasoning_effort_supported=False（默认）-> pop 私有键、return（保守跳过，不发任何字段）
      - reasoning_effort_supported=True            -> body['reasoning_effort'] = effort（原值透传，不翻译）

    值不映射：见 DESIGN 第 2.1 节，GLM/DeepSeek/MiniMax 认该字段的模型都用 low/medium/high 同语义。

    必须在 failover 循环内、target_model 已知后调用（与 _apply_role_replacement 同一位置）。
    """
    effort = body.pop("_codex_reasoning_effort", None)
    if not isinstance(effort, str) or not effort:
        return
    if not reasoning_effort_supported:
        return
    body["reasoning_effort"] = effort
```

**变更点**：
- 新增形参 `reasoning_effort_supported=False`（默认 False，最保守）
- **删除** `model_lower = (target_model or "").lower()` 与 `if "minimax" in model_lower: return`（名字判定彻底移除）

### 6.2 `_resolve_openai_provider`：把开关挂到候选 dict 上

候选构造两处（fallback 路径 L654-662 + 命中映射路径 L684-693）：

```python
# fallback 单候选（无 mapping）
{
    "provider": provider,
    "target_model": model,
    "model_type": "text",
    "model_max_tokens": 0,
    "role_rules": [],
    "mapping_id": None,
    "priority": 1,
    "reasoning_effort_supported": 1,   # ← 新增：无映射场景默认透传，与新建映射默认值一致
}

# 命中映射的候选
{
    "provider": provider,
    "target_model": m["target_model"],
    "model_type": m.get("model_type", "text"),
    "model_max_tokens": m.get("max_tokens", 0),
    "role_rules": _role_replace_rules(m),
    "mapping_id": m.get("id"),
    "alias": m.get("alias"),
    "priority": m.get("priority", 1),
    "reasoning_effort_supported": m.get("reasoning_effort_supported", 0),  # ← 新增
}
```

`config.get_model_mapping_by_alias` 已用 `m.*` 透传所有列（含新列），**无需改**。

### 6.3 failover 循环入口两处：从 `chosen` 取标志传给下游

**`handle_openai_responses_request`**（proxy.py:2274 附近 failover 循环）：

```python
provider = chosen["provider"]
target_model = chosen["target_model"]
model_type = chosen["model_type"]
model_max_tokens = chosen["model_max_tokens"]
role_rules = chosen["role_rules"]
reasoning_effort_supported = chosen.get("reasoning_effort_supported", 0)  # ← 新增

# 流式分支
body = dict(chat_body)
_apply_role_replacement(body, role_rules)
body["model"] = target_model
_apply_reasoning_effort(body, target_model, reasoning_effort_supported)  # ← 传标志

# 非流式分支（_proxy_openai_direct 同样要传）
response = _proxy_openai_direct(
    chat_body, provider, target_model, False,
    sem=sem, ...,
    model_type=model_type, source_model=model,
    model_max_tokens=model_max_tokens,
    role_rules=role_rules,
    reasoning_effort_supported=reasoning_effort_supported,  # ← 新增
    ...
)
```

**`handle_openai_proxy_request`**（Chat Completions 入口，proxy.py:727 附近，同改法）：
- 从 `chosen` 取 `reasoning_effort_supported`
- 传给 `_proxy_openai_direct`（L756 那次调用）

> 注意：Chat Completions 直通请求体不含 `_codex_reasoning_effort` 私有键，`_apply_reasoning_effort` 调用为空操作，标志透传不影响直通路径。

### 6.4 `_proxy_openai_direct`：新增形参

```python
def _proxy_openai_direct(request_body, provider, target_model, stream, sem=None, client_ip="",
                        start_time=None, model_type="text", source_model="",
                        model_max_tokens=0, role_rules=None, mapping_id=None,
                        degradation_duration=0,
                        reasoning_effort_supported=False):  # ← 新增
    # ...
    body["model"] = target_model
    _apply_role_replacement(body, role_rules)
    _apply_reasoning_effort(body, target_model, reasoning_effort_supported)  # ← 传标志
    # ...
```

> 该函数当前是**位置参数链**（L808 签名、L756/L848 调用全按位置传）。新增形参必须放末尾并带默认值，两处调用点同步加 `reasoning_effort_supported=...` 关键字传参，避免错位。

### 6.5 CRUD（`config.py`）

```python
def add_model_mapping(alias, target_model, provider_id, enabled=True, priority=1,
                      model_type="text", max_tokens=0, role_mappings=None,
                      reasoning_effort_supported=1):  # ← 新增，默认打开
    # INSERT 加列：
    # "INSERT INTO model_mappings (alias, target_model, provider_id, priority, model_type,
    #  max_tokens, enabled, role_mappings, reasoning_effort_supported)
    #  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    # 多一个 int(reasoning_effort_supported)

def update_model_mapping(mapping_id, **kwargs):
    allowed = {"alias", "target_model", "provider_id", "enabled", "priority",
               "model_type", "max_tokens", "role_mappings",
               "reasoning_effort_supported"}  # ← 新增
    # int(v) 判定集合需加 "reasoning_effort_supported"：
    # values.append(int(v) if k in ("enabled", "priority", "max_tokens", "reasoning_effort_supported") else v)
```

`get_model_mapping_by_alias` 已用 `m.*` 透传所有列，**无需改**。

---

## 7. UI 改动（`templates/index.html`）

### 7.1 模型映射弹窗（`#model-modal`）

在"启用"开关附近插入新开关（与现有 `mf-enabled` 同模式）：

```html
<div class="form-group" style="display:flex;align-items:center;gap:8px">
  <label class="toggle">
    <input type="checkbox" id="mf-reasoning" checked>
    <span class="toggle-slider"></span>
  </label>
  <span style="font-size:13px">
    透传思考强度参数（<code>reasoning_effort</code>）
  </span>
</div>
<div style="font-size:11px;color:var(--text2);margin-top:-4px;margin-bottom:8px">
  开启后，调用方传来的思考强度（如 codex 的 <code>reasoning_effort=high</code>）会作为
  <code>reasoning_effort</code> 原值透传给该上游；关闭则保守跳过（不发该字段）。
  <b>兼容性参考</b>：GLM/DeepSeek 原生认此字段；MiniMax 接受但不调深度；火山/讯飞/Kimi
  参数名不同，传过去多半被上游忽略（不报错），默认开启，若实测出现 400 再关闭。
  值 <code>low/medium/high</code> 不做翻译。
</div>
```

### 7.2 `openModelModal`（回填）

```javascript
// 新映射默认 checked（新模型默认按 OpenAI 方式透传）；老映射按 DB 实际值
document.getElementById('mf-reasoning').checked =
    m ? !!m.reasoning_effort_supported : true;
```

### 7.3 `saveModel`（提交）

```javascript
const data = {
    alias: ...,
    reasoning_effort_supported: document.getElementById('mf-reasoning').checked,  // ← 新增
    // ...
};
```

### 7.4 列表展示（可选改进，本期不做）

模型映射表格目前不展示该字段。后续如需，在 `dataStore` / 表格列里增加"推理透传"列与标签。

---

## 8. 范围边界

### 8.1 明确**不**改动的部分

| 位置 | 现状 | 本次处理 |
|---|---|---|
| `proxy.py:1111` `_proxy_anthropic` 的 `"minimax" in target_model.lower()` | 名字判定调 `_adapt_minimax_anthropic` | **不动**（Claude Code 走同协议直转，thinking 是原生参数，不属本次开关范围） |
| `_adapt_deepseek_anthropic` 的 `thinking.budget_tokens -> output_config.effort` | DeepSeek Anthropic 适配 | **不动**（与 Claude Code / Anthropic 路径独立） |
| `_adapt_minimax_anthropic`（清理无效 tool_use 块） | 名字判定 | **不动**（Anthropic 路径，独立 scope） |
| 上一轮修复的响应侧 reasoning 事件序列、空输出兜底、call_id 合成等 | 已完成 | **不动**（本次仅替换请求侧的启发式） |

### 8.2 未来扩展（**不**在本期）

- 三态下拉（`skip` / `effort` / `thinking`）覆盖 `enable_thinking` / `thinking.type` 等参数名差异（火山/讯飞/Kimi 接入时再考虑）
- Anthropic 路径统一一个"思考强度控制"总开关（同时管 `reasoning_effort` 与 `thinking`）
- 模型映射列表展示"推理透传"列
- 按值映射（如 codex `high` -> GLM `xhigh`）：第 2.1 节证实当前不必要，未来若上游语义出现分叉再加

---

## 9. 验证计划

| 检查项 | 方式 |
|---|---|
| 语法 | `python -c "import ast; ast.parse(open('proxy.py',encoding='utf-8').read())"` 与 config.py 同检 |
| 导入 | `python -c "import proxy; import config"` |
| 老库迁移 | 用真实 `proxy.db` 启动一次，确认 v1->v2 迁移通过、列默认 0、14 行数据完整、`user_version=2` |
| 全新库 | 删 `proxy.db` 启动一次，确认 `_create_latest_schema` 直接含新列、`user_version=2`、`is_fresh` 分支无需新 settings 键 |
| 探针 | 扩展 `_tmp_sse_probe.py` 覆盖：①开关=0 + 上游有 reasoning_content -> 响应侧不受影响；②开关=0 + patch `_post_with_retry` 抓 body 确认**不**含 `reasoning_effort`；③开关=1 + 上游文本 -> body 含 `reasoning_effort=<effort>` 且**值未翻译**（仍是 low/medium/high） |
| UI | 打开模型映射弹窗，看新开关存在；保存后重新打开回填正确；管理面板正常加载 |
| 自检 | `_self_check_schema` 老库 / 全新库各跑一次，确认无"列缺失"报错 |
| 兼容性实测（运维侧） | 按 3.1 节矩阵，对 GLM/DeepSeek/MiniMax 逐条打开开关，用 codex CLI 实跑确认无 400 |

---

## 10. 风险与回退

### 10.1 风险

| 风险 | 缓解 |
|---|---|
| 迁移回填 1 导致火山/讯飞/Kimi 等参数名不同的上游收到 `reasoning_effort` 字段 | 多半被上游忽略不报错；若个别上游实测出现 400，运维在 UI 逐条关闭。发版说明明示分组建议 |
| 新列未注册迁移 -> `_self_check_schema` 报错 | 严格走 CLAUDE.md 流程，PR 时核对 `_MIGRATIONS` / `_create_latest_schema` / `_calibrate_user_version` 三处同步 |
| UI 默认 `checked` 与老迁移回填 1 一致 | 不冲突：新映射默认 checked、老迁移回填 1，两者语义统一（默认透传）。UI 回填走 DB 实际值（见 7.2） |
| `_proxy_openai_direct` 位置参数链新增形参错位 | 新形参放末尾带默认值；两处调用点（L756、Responses 入口）改关键字传参 |
| 多线程并发下私有键 `_codex_reasoning_effort` 残留 | 现有机制已 pop 处理干净；不改 |

### 10.2 回退

代码改动定位精确（1 个新迁移函数、3 文件签名扩展），可逐文件 git revert。
数据回退：手动 `ALTER TABLE model_mappings DROP COLUMN reasoning_effort_supported`（SQLite 3.35+ 支持），不影响业务列。

---

## 11. 改动文件清单

| 文件 | 变更摘要 |
|---|---|
| `config.py` | 1. `_create_latest_schema` `model_mappings` CREATE TABLE 加列（L580-591）；<br>2. 新增 `_migrate_model_mappings_add_reasoning`；<br>3. `_MIGRATIONS` 追加 v2；<br>4. `_calibrate_user_version` 加 v2 特征检测；<br>5. `add_model_mapping` / `update_model_mapping` 加列处理（含 `int(v)` 判定集合） |
| `proxy.py` | 1. `_apply_reasoning_effort` 去名字判定、加 `reasoning_effort_supported` 形参（L198）；<br>2. `_resolve_openai_provider` 两条候选构造加键（L654/L684）；<br>3. `handle_openai_responses_request` failover 循环取标志并透传；<br>4. `handle_openai_proxy_request` 同上（L727/L756）；<br>5. `_proxy_openai_direct` 加形参并改两处调用为关键字传参（L808） |
| `templates/index.html` | 1. `#model-modal` 新增 `.toggle` 复选框 `mf-reasoning` + 含兼容性参考的帮助文字；<br>2. `openModelModal` 回填；<br>3. `saveModel` 提交 data |

预计代码量：约 +80 行（迁移函数 ~60 行 + 后端 ~10 行 + UI ~10 行），-3 行（删名字判定）。

---

## 12. 实施 checklist

- [ ] `config.py`: 4 处 schema/migration/CRUD 改动
- [ ] `proxy.py`: 5 处函数改动（含形参签名与两处调用点关键字传参）
- [ ] `templates/index.html`: 3 处 UI 改动
- [ ] 老库启动验证（v0/v1 -> v2 迁移通过，14 行完整）
- [ ] 全新库启动验证
- [ ] 探针扩展（开关=0/1 两条路径，并校验值未翻译）
- [ ] UI 弹窗与保存往返验证
- [ ] 更新 `manual/` 中 codex 接入文档（提示新开关 + 第 3.1 节分组建议）
- [ ] 更新 release notes（说明 v2 schema 升级 + 老模型需手动开开关 + 按上游分组建议）

---

## 附：变更触发条件与查证来源

本次重写由用户追问触发："`reasoning.effort` 的三个值对应国产大模型的什么？国产大模型有这个设置么？"。经查证各上游官方文档，确认 `reasoning_effort` 字段名正被国产模型收敛采纳（GLM/DeepSeek 原生认、MiniMax 接受但不调深度、火山/讯飞/Kimi 参数名不同），值 `low/medium/high` 语义通用、无需翻译。由此坐实了"简单开关、原值透传"的决策，并据此修正了开关语义命名与迁移回填的分组建议。

**查证来源：**
- DeepSeek 思考模式：https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/
- GLM 核心参数 reasoning_effort：https://docs.bigmodel.cn/cn/guide/start/concept-param
- MiniMax M3 effort / Adaptive Thinking：https://platform.minimaxi.com/docs/api-reference/responses-input-tokens
- 火山方舟深度思考：https://www.volcengine.com/docs/82379/1449737
- thinking/reasoning 统一开关门面需求讨论：https://github.com/agentscope-ai/agentscope-java/issues/1900
