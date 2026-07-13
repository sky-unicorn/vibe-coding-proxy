# Project Rules

## SQLite Migration Rule

修改 SQLite 表结构时，必须遵循以下流程：先备份表中现有数据 → 删除旧表 → 重新创建新表 → 将备份的数据写回新表。禁止直接删除表而不保留历史数据。

**Why:** 项目使用 SQLite 数据库，修改表结构（如新增/删除列、修改字段类型）通常需要 DROP TABLE 后重建，无法像 MySQL 那样用 ALTER TABLE 灵活变更。直接删除会导致历史数据永久丢失。

**How to apply:** 每次涉及 SQLite 表结构变更（migration）时，在代码中或操作步骤中必须包含数据备份和回填逻辑。例如：
1. `SELECT * FROM old_table` → 保存到临时变量/临时表
2. `DROP TABLE old_table`
3. `CREATE TABLE new_table (...)` 使用新结构
4. 将备份数据 `INSERT INTO new_table` 回填（字段映射到新结构）

## SQLite Direct Access Rule

项目使用 SQLite 数据库，数据库文件为 `proxy.db`。所有数据库操作必须直接操作 `proxy.db` 文件，禁止调用任何名称以 `-db` 结尾的 MCP 服务（如 `anal-business-db`、`anal-system-db`、`gridfoundation-db`、`jnfs-db` 等）。

**Why:** 项目的数据库是本地 SQLite 文件，MCP 数据库服务连接的是其他数据库实例，与项目无关，调用会导致操作错误的数据库。

**How to apply:** 需要查询或修改数据库时，通过代码中已有的数据库操作模块或使用 `sqlite3` 命令行工具直接操作 `proxy.db`，绝不使用任何名称以 `-db` 结尾的 MCP 服务。

## Schema 版本迁移规则（Versioned Migration Rule）

**凡是对数据库表结构有任何变更 —— 新增/删除/重命名列、修改列类型或约束（NOT NULL/DEFAULT/UNIQUE）、新增/删除索引、新增/删除表 —— 都必须新增一个版本迁移，绝不能只改 `init_db` 里的 `CREATE TABLE`。** 仅修改种子数据（`INSERT` 默认值）或纯数据更新（如清理 settings 键）不算结构变更，不需要版本迁移。

**Why:** `CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作，老库不会获得新结构；启动时 `_self_check_schema` 反射对比期望 schema，遗漏迁移会直接报错导致启动失败。只改 CREATE TABLE 而不写迁移，等于让所有老库静默停留在旧 schema，运行时才炸。

**How to apply:** 每次涉及表结构变更时，按以下步骤操作：
1. 在 `config.py` 新增幂等迁移函数 `_migrate_xxx(conn)`：开头先做存在性检测 —— 检测"目标列"用 `PRAGMA table_info(表名)`，遍历返回的列名判断目标列是否已存在；检测"目标索引"用 `PRAGMA index_list(表名)` 或 `SELECT name FROM sqlite_master WHERE type='index' AND name='索引名'`，不能用 `PRAGMA table_info`（它只返回列定义，不包含索引信息）。已存在则 `return`；否则按 SQLite Migration Rule 执行 backup → DROP TABLE → CREATE TABLE(新结构) → backfill。绝不直接 DROP 不备份数据。
2. 在 `_MIGRATIONS` 注册表末尾追加一项：`(新版本号, "变更描述", _migrate_xxx)`。版本号 = 上一项版本号 + 1，不得跳号、不得插队、不得复用旧号。
3. 同步更新 `_create_latest_schema` 函数里的 `CREATE TABLE`，使全新库直接获得最新结构；并在 `_calibrate_user_version` 里为该版本追加一个特征检测（检测该版本引入的标志性列/索引是否存在）。特征检测分三种范式：加列类迁移用 `PRAGMA table_info` 检测标志性列是否存在；新增表类迁移用 `SELECT name FROM sqlite_master WHERE type='table' AND name='新表名'` 检测表是否存在（注意：`PRAGMA table_info` 对不存在的表返回空列表，无法区分『表不存在』与『表存在但无该列』，因此新增表迁移务必用 sqlite_master 而非 table_info 做特征检测）；新增索引类迁移用 `PRAGMA index_list(表名)` 检测标志性索引是否存在，或查询 `SELECT name FROM sqlite_master WHERE type='index' AND name='索引名'`（注意：`PRAGMA table_info` 不适用于索引检测，它只返回列定义、不返回任何索引信息）。

**自检函数 `_self_check_schema` 对『显式索引新增』类遗漏无检测能力** -- 因为 `_create_latest_schema` 用 `CREATE INDEX IF NOT EXISTS` 在老库上也会创建该索引，老库自检时实际与期望一致，遗漏的索引迁移无法被自检捕获，仅靠 `_create_latest_schema` 的 `IF NOT EXISTS` 兜底。因此新增显式索引时同样必须在 `_MIGRATIONS` 注册并同步 `_calibrate_user_version` 用 `PRAGMA index_list` 检测（不能用 `PRAGMA table_info`）。说明：上面第 3 步里『新增索引类迁移用 `PRAGMA index_list` 检测标志性索引是否存在』属于「特征检测」用途（供 `_calibrate_user_version` 校准版本号），不是「自检兜底」用途；`_self_check_schema` 的兜底覆盖范围仅限『列新增/删除』与『自动索引（PRIMARY KEY/UNIQUE 约束）缺失』这类 `_create_latest_schema` 的 `IF NOT EXISTS` 无法静默弥补的差异。
4. `CURRENT_SCHEMA_VERSION` 自动等于 `len(_MIGRATIONS)`，无需手改。
5. 本地用老库 + 全新库两种场景各启动一次，确认自检通过、无数据丢失。

**判定速查：** 改了 CREATE TABLE 的任何一行结构定义 → 必须加版本迁移；只改 `INSERT OR IGNORE` 的种子值或纯 UPDATE 数据 → 不需要。拿不准时，默认按「需要」处理并加迁移。

## Schema 版本迁移规则 — 全新库 is_fresh 分支的 baseline 标记规则

**在 `init_db` 的全新库 `is_fresh` 分支里，必须把所有 `_baseline_migrate_to_v0` 用到的一次性 settings 标记键一并写入，与老库分支保持一致。** 当前已知的两个标记键：`schema_version_framework_initialized` 与 `baseline_url_full_endpoint_done`。

**Why:** 全新库 `is_fresh` 分支直接设 `user_version=CURRENT_SCHEMA_VERSION` 并跳过基线与迁移，但首次启动写库后，第二次启动时 `is_fresh` 已为 `False`，会走老库分支 `_baseline_migrate_to_v0`。若某个 baseline 一次性标记在 `is_fresh` 分支漏写，老库分支会判定该迁移未完成并重跑，可能篡改用户在两次启动之间通过表单填入的非标准自定义端点 URL（例如 `https://gateway.example.com/anthropic` 被错误追加 `/v1/messages`，且不可逆、已写入 DB）。`_migrate_providers_url_to_full_endpoint` 的 `endswith` 幂等检查只保护已带标准后缀的 URL，对自定义端点无保护。

**How to apply:** 每当在 `_baseline_migrate_to_v0` 新增一个用 settings 键做一次性守卫的 baseline 迁移（或新增任何一次性 settings 标记），必须同步在 `init_db` 的 `is_fresh` 分支里补写对应标记键 `'1'`，确保全新库二次启动走老库分支时该迁移被标记为已完成、永不重跑。改完后用「全新库 + 配置非标准自定义端点 provider + 重启」场景回归验证 URL 不被修改。

## Schema 版本迁移规则 — 新增表类迁移的自检盲区提示

`_self_check_schema` 对「新增表」类遗漏迁移的检测存在盲区：开发者只改了 `_create_latest_schema` 新增一张表（无种子数据依赖）但忘了注册 `_MIGRATIONS` 时，老库启动时 `_create_latest_schema` 的 `CREATE TABLE IF NOT EXISTS` 会静默把新表建出来，于是期望 schema 与实际 schema 一致、自检通过 —— 等于「忘了写迁移」被 `IF NOT EXISTS` 掩盖，自检失效。

**应对：** 新增表类迁移时，务必同步改 `_calibrate_user_version` 的表存在性特征检测（用 `SELECT name FROM sqlite_master WHERE type='table' AND name='新表名'`），使新增表类迁移若未注册则 `user_version` 校准不到、后续 `run_migrations` 不会误升。仅靠 `_self_check_schema` 无法可靠捕获「新增表遗漏迁移」。
