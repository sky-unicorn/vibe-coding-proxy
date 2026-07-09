import sqlite3
import json
import os
import secrets
import threading
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _migrate_providers_add_disable_reason(conn):
    """为 providers 表添加 disable_reason 列，用于区分手动禁用与自动禁用"""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(providers)").fetchall()]
    if "disable_reason" in cols:
        return  # 已有 disable_reason 列，无需迁移
    # SQLite 不支持 ADD COLUMN 带 NOT NULL DEFAULT ''，需用重建表方式
    # 1. 备份现有数据
    old_rows = conn.execute("SELECT * FROM providers").fetchall()
    old_col_names = [desc[0] for desc in conn.execute("SELECT * FROM providers LIMIT 0").description]
    # 2. 删除旧表
    conn.execute("DROP TABLE providers")
    # 3. 创建新表（含 disable_reason 列）
    conn.execute("""
        CREATE TABLE providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            api_key TEXT NOT NULL,
            provider_type TEXT NOT NULL DEFAULT 'anthropic',
            enabled INTEGER NOT NULL DEFAULT 1,
            max_concurrency INTEGER NOT NULL DEFAULT 0,
            disable_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    # 4. 将备份数据写回新表
    if old_rows:
        placeholders = ",".join(["?"] * len(old_col_names + ["disable_reason"]))
        for row in old_rows:
            conn.execute(
                f"INSERT INTO providers ({', '.join(old_col_names)}, disable_reason) VALUES ({placeholders})",
                (*row, ""),
            )
    conn.commit()


def _migrate_providers_dual_url(conn):
    """将 providers 表从 provider_type/base_url 结构迁移为 anthropic_url/openai_url 结构"""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(providers)").fetchall()]
    if "anthropic_url" in cols and "openai_url" in cols:
        return  # 已是新结构，无需迁移

    # 1. 备份现有数据
    old_rows = conn.execute("SELECT * FROM providers").fetchall()
    old_col_names = [desc[0] for desc in conn.execute("SELECT * FROM providers LIMIT 0").description]

    # 2. 删除旧表
    conn.execute("DROP TABLE providers")

    # 3. 创建新表（同时支持 anthropic_url 与 openai_url）
    conn.execute("""
        CREATE TABLE providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            anthropic_url TEXT NOT NULL DEFAULT '',
            openai_url TEXT NOT NULL DEFAULT '',
            api_key TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            max_concurrency INTEGER NOT NULL DEFAULT 0,
            disable_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)

    # 4. 将备份数据按旧 provider_type 映射到新 URL 字段
    if old_rows:
        col_to_idx = {name: idx for idx, name in enumerate(old_col_names)}
        for row in old_rows:
            row_dict = dict(zip(old_col_names, row))
            old_base_url = row_dict.get("base_url", "")
            provider_type = row_dict.get("provider_type", "anthropic")
            anthropic_url = old_base_url if provider_type == "anthropic" else ""
            openai_url = old_base_url if provider_type == "openai" else ""
            conn.execute(
                """
                INSERT INTO providers
                (id, name, anthropic_url, openai_url, api_key, enabled, max_concurrency, disable_reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row_dict.get("id"),
                    row_dict.get("name", ""),
                    anthropic_url,
                    openai_url,
                    row_dict.get("api_key", ""),
                    row_dict.get("enabled", 1),
                    row_dict.get("max_concurrency", 0),
                    row_dict.get("disable_reason", ""),
                    row_dict.get("created_at", ""),
                ),
            )
    conn.commit()


def _migrate_providers_url_to_full_endpoint(conn):
    """把 providers 的 anthropic_url/openai_url 从 base 路径补全为完整端点地址（一次性迁移）。

    背景：早期代码在转发时会自动拼接 /v1/messages 或 /v1/chat/completions，但许多上游
    的 base 路径已含版本号（如 ***/v4、***/v2），拼接 /v1/... 会得到 /v4/v1/... 的错误路径。
    改为不拼接后，需把历史 base 路径补全为完整端点：
      - anthropic_url（如 .../anthropic）追加 /v1/messages
      - openai_url（如 .../v4，版本号已在 URL 中）追加 /chat/completions
    用 PRAGMA user_version 标记确保只执行一次，避免误改用户后续手动填入的非标准端点。
    """
    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if user_version >= 1:
        return  # 已执行过此迁移
    rows = conn.execute("SELECT id, anthropic_url, openai_url FROM providers").fetchall()
    for row in rows:
        pid = row["id"]
        anth = (row["anthropic_url"] or "").rstrip("/")
        oai = (row["openai_url"] or "").rstrip("/")
        updates = {}
        # anthropic 端点标准路径为 /v1/messages
        if anth and not anth.endswith("/v1/messages"):
            updates["anthropic_url"] = anth + "/v1/messages"
        # openai 端点：版本号已在 URL 中（如 /v4、/v1），仅追加 /chat/completions
        if oai and not oai.endswith("/chat/completions"):
            updates["openai_url"] = oai + "/chat/completions"
        if updates:
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE providers SET {sets} WHERE id = ?", (*updates.values(), pid))
    conn.execute("PRAGMA user_version = 1")
    conn.commit()


def _migrate_providers_add_full_path(conn):
    """为 providers 表添加 full_path 列，用于控制转发时是否自动拼接路径后缀。

    full_path=1（默认，完整路径）：配置的地址原样使用，转发时不拼接任何后缀。
    full_path=0（base 路径）：转发时自动在 anthropic_url 后拼接 /v1/messages，
                             在 openai_url 后拼接 /chat/completions。

    迁移策略：对所有历史 provider 一律默认 full_path=1（保留旧「不拼接路径」行为）。
    旧代码行为是 URL 原样使用、不拼接任何后缀，因此迁移时不按 URL 形态推断为 base 路径，
    否则非标准自定义端点会被误判为 full_path=0，转发时拼接后缀导致端点不可用。
    full_path=0 仅作为用户新建/编辑时主动选择的模式。必须在 _migrate_providers_url_to_full_endpoint 之后调用。
    """
    cols = [row[1] for row in conn.execute("PRAGMA table_info(providers)").fetchall()]
    if "full_path" in cols:
        return  # 已有 full_path 列，无需迁移
    # 备份子表行数，迁移后做完整性校验（防止外键悬空隐患）
    mm_count_before = conn.execute("SELECT COUNT(*) FROM model_mappings").fetchone()[0]
    # 1. 备份现有数据
    old_rows = conn.execute("SELECT * FROM providers").fetchall()
    old_col_names = [desc[0] for desc in conn.execute("SELECT * FROM providers LIMIT 0").description]
    # 2. 删除旧表
    conn.execute("DROP TABLE providers")
    # 3. 创建新表（含 full_path 列）
    conn.execute("""
        CREATE TABLE providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            anthropic_url TEXT NOT NULL DEFAULT '',
            openai_url TEXT NOT NULL DEFAULT '',
            api_key TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            max_concurrency INTEGER NOT NULL DEFAULT 0,
            disable_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            full_path INTEGER NOT NULL DEFAULT 1
        )
    """)
    # 4. 回填数据，对所有历史 provider 一律默认 full_path=1（保留旧「不拼接路径」行为）
    # 旧代码行为是 URL 原样使用、不拼接任何后缀，因此迁移时不应按 URL 形态推断为 base 路径，
    # 否则非标准自定义端点（如 https://my-proxy.com/api/messages）会被误判为 full_path=0，
    # 转发时拼接后缀导致端点不可用。full_path=0 仅作为用户新建/编辑时主动选择的模式。
    if old_rows:
        for row in old_rows:
            row_dict = dict(zip(old_col_names, row))
            # NOT NULL 列兜底，避免历史 NULL 值导致回填失败
            conn.execute(
                "INSERT INTO providers (id, name, anthropic_url, openai_url, api_key, enabled, max_concurrency, disable_reason, created_at, full_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row_dict.get("id"),
                    row_dict.get("name", "") or "",
                    row_dict.get("anthropic_url", "") or "",
                    row_dict.get("openai_url", "") or "",
                    row_dict.get("api_key", "") or "",
                    int(row_dict.get("enabled") or 1),
                    int(row_dict.get("max_concurrency") or 0),
                    row_dict.get("disable_reason") or "",
                    row_dict.get("created_at") or datetime.now().isoformat(),
                    1,
                ),
            )
    conn.commit()
    # 5. 完整性校验：确认子表 model_mappings 无悬空 provider_id（防止迁移后外键失效）
    mm_count_after = conn.execute("SELECT COUNT(*) FROM model_mappings").fetchone()[0]
    if mm_count_after != mm_count_before:
        raise RuntimeError(
            f"providers 表 full_path 迁移导致 model_mappings 行数变化：{mm_count_before} -> {mm_count_after}"
        )
    dangling = conn.execute(
        "SELECT COUNT(*) FROM model_mappings WHERE provider_id NOT IN (SELECT id FROM providers)"
    ).fetchone()[0]
    if dangling:
        raise RuntimeError(
            f"providers 表 full_path 迁移后 model_mappings 出现 {dangling} 条悬空 provider_id"
        )


def _migrate_logs_add_source_model(conn):
    """检查 logs 表是否缺少 source_model 列，若缺少则按迁移规则重建表"""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(logs)").fetchall()]
    if "source_model" in cols:
        return  # 已有 source_model 列，无需迁移
    # 1. 备份现有数据
    old_rows = conn.execute("SELECT * FROM logs").fetchall()
    old_col_names = [desc[0] for desc in conn.execute("SELECT * FROM logs LIMIT 0").description]
    # 2. 删除旧表
    conn.execute("DROP TABLE logs")
    # 3. 创建新表（含 source_model 列）
    conn.execute("""
        CREATE TABLE logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_time TEXT NOT NULL,
            provider TEXT DEFAULT '',
            source_model TEXT DEFAULT '',
            model TEXT DEFAULT '',
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_input_tokens INTEGER DEFAULT 0,
            cache_creation_input_tokens INTEGER DEFAULT 0,
            status TEXT DEFAULT '',
            duration_ms INTEGER DEFAULT 0,
            error_msg TEXT DEFAULT '',
            request_body TEXT DEFAULT '',
            response_body TEXT DEFAULT '',
            original_status_code INTEGER DEFAULT 0,
            mapped_status_code INTEGER DEFAULT 0,
            client_ip TEXT DEFAULT ''
        )
    """)
    # 4. 回填数据（用列名映射，新列填空字符串）
    new_cols = ["id", "request_time", "provider", "source_model", "model", "input_tokens", "output_tokens",
                "cache_read_input_tokens", "cache_creation_input_tokens", "status", "duration_ms",
                "error_msg", "request_body", "response_body", "original_status_code", "mapped_status_code", "client_ip"]
    for row in old_rows:
        values = [row[c] if c in old_col_names else ("" if c == "source_model" else 0) for c in new_cols]
        placeholders = ",".join("?" * len(values))
        conn.execute(f"INSERT INTO logs ({','.join(new_cols)}) VALUES ({placeholders})", values)
    conn.commit()


def _migrate_logs_add_cache_tokens(conn):
    """检查 logs 表是否缺少 cache token 列，若缺少则按迁移规则重建表"""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(logs)").fetchall()]
    if "cache_read_input_tokens" in cols:
        return  # 已有 cache token 列，无需迁移
    # 1. 备份现有数据
    old_rows = conn.execute("SELECT * FROM logs").fetchall()
    old_col_names = [desc[0] for desc in conn.execute("SELECT * FROM logs LIMIT 0").description]
    # 2. 删除旧表
    conn.execute("DROP TABLE logs")
    # 3. 创建新表（含 cache token 列）
    conn.execute("""
        CREATE TABLE logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_time TEXT NOT NULL,
            provider TEXT DEFAULT '',
            model TEXT DEFAULT '',
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_input_tokens INTEGER DEFAULT 0,
            cache_creation_input_tokens INTEGER DEFAULT 0,
            status TEXT DEFAULT '',
            duration_ms INTEGER DEFAULT 0,
            error_msg TEXT DEFAULT '',
            request_body TEXT DEFAULT '',
            response_body TEXT DEFAULT '',
            original_status_code INTEGER DEFAULT 0,
            mapped_status_code INTEGER DEFAULT 0,
            client_ip TEXT DEFAULT ''
        )
    """)
    # 4. 回填数据（用列名映射，新列填 0）
    new_cols = ["id", "request_time", "provider", "model", "input_tokens", "output_tokens",
                "cache_read_input_tokens", "cache_creation_input_tokens", "status", "duration_ms",
                "error_msg", "request_body", "response_body", "original_status_code", "mapped_status_code", "client_ip"]
    for row in old_rows:
        values = [row[c] if c in old_col_names else 0 for c in new_cols]
        placeholders = ",".join("?" * len(values))
        conn.execute(f"INSERT INTO logs ({','.join(new_cols)}) VALUES ({placeholders})", values)
    conn.commit()


def _migrate_provider_usage_add_cache_tokens(conn):
    """检查 provider_usage 表是否缺少 cache token 列，若缺少则按迁移规则重建表"""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(provider_usage)").fetchall()]
    if "cache_read_input_tokens" in cols:
        return  # 已有 cache token 列，无需迁移
    # 1. 备份现有数据
    old_rows = conn.execute("SELECT * FROM provider_usage").fetchall()
    old_col_names = [desc[0] for desc in conn.execute("SELECT * FROM provider_usage LIMIT 0").description]
    # 2. 删除旧表
    conn.execute("DROP TABLE provider_usage")
    # 3. 创建新表（含 cache token 列）
    conn.execute("""
        CREATE TABLE provider_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL,
            window_type TEXT NOT NULL,
            window_start TEXT NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
            cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
            balance_used REAL NOT NULL DEFAULT 0,
            UNIQUE (provider_id, window_type, window_start),
            FOREIGN KEY (provider_id) REFERENCES providers(id)
        )
    """)
    # 4. 回填数据（用列名映射，新列填 0）
    new_cols = ["id", "provider_id", "window_type", "window_start", "request_count",
                "input_tokens", "output_tokens", "cache_read_input_tokens",
                "cache_creation_input_tokens", "balance_used"]
    for row in old_rows:
        values = [row[c] if c in old_col_names else 0 for c in new_cols]
        placeholders = ",".join("?" * len(values))
        conn.execute(f"INSERT INTO provider_usage ({','.join(new_cols)}) VALUES ({placeholders})", values)
    conn.commit()


def _migrate_error_mappings(conn):
    """检查 error_mappings 表是否缺少 provider 列，若缺少则按迁移规则重建表"""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(error_mappings)").fetchall()]
    if "provider" in cols:
        return  # 已有 provider 列，无需迁移
    # 1. 备份现有数据
    old_rows = conn.execute("SELECT id, original_code, mapped_code, enabled FROM error_mappings").fetchall()
    # 2. 删除旧表
    conn.execute("DROP TABLE error_mappings")
    # 3. 创建新表（含 provider 列）
    conn.execute("""
        CREATE TABLE error_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL DEFAULT '',
            original_code INTEGER NOT NULL,
            mapped_code INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1
        )
    """)
    # 4. 回填数据（provider 默认为空字符串）
    for row in old_rows:
        conn.execute(
            "INSERT INTO error_mappings (id, provider, original_code, mapped_code, enabled) VALUES (?, '', ?, ?, ?)",
            (row[0], row[1], row[2], row[3]),
        )
    conn.commit()


def _migrate_model_mappings(conn):
    """检查 model_mappings 表是否有 group_name 列，若有则按迁移规则重建表（将 group_name 合并到 alias）"""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(model_mappings)").fetchall()]
    if "group_name" not in cols:
        return  # 已迁移（无 group_name 列），无需执行
    # 1. 备份现有数据
    old_rows = conn.execute("SELECT * FROM model_mappings").fetchall()
    old_col_names = [desc[0] for desc in conn.execute("SELECT * FROM model_mappings LIMIT 0").description]
    mm_count_before = len(old_rows)
    # 统计迁移前悬空 provider_id（完整性校验用）
    dangling_before = conn.execute(
        "SELECT COUNT(*) FROM model_mappings WHERE provider_id NOT IN (SELECT id FROM providers)"
    ).fetchone()[0]
    # 2. 删除旧表
    conn.execute("DROP TABLE model_mappings")
    # 3. 创建新表（移除 group_name 列）
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
            FOREIGN KEY (provider_id) REFERENCES providers(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_model_mappings_alias_enabled ON model_mappings(alias, enabled)")
    # 4. 回填数据（合并规则：group_name 优先于 alias）
    for row in old_rows:
        row_dict = dict(zip(old_col_names, row))
        old_group = (row_dict.get("group_name") or "").strip()
        old_alias = (row_dict.get("alias") or "").strip()
        new_alias = old_group if old_group else old_alias
        if not new_alias:
            raise RuntimeError(f"迁移失败：id={row_dict.get('id')} 行 alias 与 group_name 均为空，无法生成对外模型名")
        # 兼容旧版单条角色映射（role_replace_enabled/role_from/role_to）→ 转 JSON 数组
        role_mappings = row_dict.get("role_mappings") or "[]"
        if not role_mappings or role_mappings == "[]":
            rules = []
            if row_dict.get("role_replace_enabled") and row_dict.get("role_from") and row_dict.get("role_to"):
                rules.append({"from": row_dict["role_from"], "to": row_dict["role_to"]})
            role_mappings = json.dumps(rules, ensure_ascii=False) if rules else "[]"
        conn.execute(
            "INSERT INTO model_mappings (id, alias, target_model, provider_id, priority, model_type, max_tokens, enabled, role_mappings) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row_dict["id"], new_alias, row_dict["target_model"], row_dict["provider_id"],
             int(row_dict.get("priority") or 1), row_dict.get("model_type") or "text",
             int(row_dict.get("max_tokens") or 0), int(row_dict.get("enabled") or 1), role_mappings),
        )
    conn.commit()
    # 5. 完整性校验
    mm_count_after = conn.execute("SELECT COUNT(*) FROM model_mappings").fetchone()[0]
    dangling_after = conn.execute(
        "SELECT COUNT(*) FROM model_mappings WHERE provider_id NOT IN (SELECT id FROM providers)"
    ).fetchone()[0]
    empty_alias_count = conn.execute("SELECT COUNT(*) FROM model_mappings WHERE alias IS NULL OR alias = ''").fetchone()[0]
    if mm_count_after != mm_count_before:
        raise RuntimeError(f"model_mappings 迁移后行数不一致：{mm_count_before} -> {mm_count_after}")
    if dangling_after != dangling_before:
        raise RuntimeError(f"model_mappings 迁移后悬空 provider_id 数量变化：{dangling_before} -> {dangling_after}")
    if empty_alias_count > 0:
        raise RuntimeError(f"model_mappings 迁移后存在 {empty_alias_count} 条空 alias 记录")


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            anthropic_url TEXT NOT NULL DEFAULT '',
            openai_url TEXT NOT NULL DEFAULT '',
            api_key TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            max_concurrency INTEGER NOT NULL DEFAULT 0,
            disable_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            full_path INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS model_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT NOT NULL,
            target_model TEXT NOT NULL,
            provider_id INTEGER NOT NULL,
            priority INTEGER NOT NULL DEFAULT 1,
            model_type TEXT NOT NULL DEFAULT 'text',
            max_tokens INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            role_mappings TEXT NOT NULL DEFAULT '[]',
            FOREIGN KEY (provider_id) REFERENCES providers(id)
        );

        CREATE INDEX IF NOT EXISTS idx_model_mappings_alias_enabled ON model_mappings(alias, enabled);

        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_time TEXT NOT NULL,
            provider TEXT DEFAULT '',
            source_model TEXT DEFAULT '',
            model TEXT DEFAULT '',
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_input_tokens INTEGER DEFAULT 0,
            cache_creation_input_tokens INTEGER DEFAULT 0,
            status TEXT DEFAULT '',
            duration_ms INTEGER DEFAULT 0,
            error_msg TEXT DEFAULT '',
            request_body TEXT DEFAULT '',
            response_body TEXT DEFAULT '',
            original_status_code INTEGER DEFAULT 0,
            mapped_status_code INTEGER DEFAULT 0,
            client_ip TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS error_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL DEFAULT '',
            original_code INTEGER NOT NULL,
            mapped_code INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_name TEXT NOT NULL,
            api_key TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_used_at TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_cleanup_enabled', '0');
        INSERT OR IGNORE INTO settings (key, value) VALUES ('cleanup_retention_days', '7');
        INSERT OR IGNORE INTO settings (key, value) VALUES ('cleanup_interval_hours', '1');
        INSERT OR IGNORE INTO settings (key, value) VALUES ('last_cleanup_time', '');

        CREATE TABLE IF NOT EXISTS provider_billing_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL UNIQUE,
            billing_mode TEXT NOT NULL DEFAULT 'request_count',
            limit_5h INTEGER DEFAULT NULL,
            limit_week INTEGER DEFAULT NULL,
            limit_month INTEGER DEFAULT NULL,
            balance REAL DEFAULT 0,
            input_price_per_million REAL DEFAULT 0,
            output_price_per_million REAL DEFAULT 0,
            expiration_date TEXT DEFAULT NULL,
            warning_threshold REAL NOT NULL DEFAULT 0.8,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (provider_id) REFERENCES providers(id)
        );

        CREATE TABLE IF NOT EXISTS provider_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL,
            window_type TEXT NOT NULL,
            window_start TEXT NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
            cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
            balance_used REAL NOT NULL DEFAULT 0,
            UNIQUE (provider_id, window_type, window_start),
            FOREIGN KEY (provider_id) REFERENCES providers(id)
        );

        CREATE TABLE IF NOT EXISTS oauth_clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT NOT NULL UNIQUE,
            client_name TEXT NOT NULL,
            client_secret TEXT DEFAULT NULL,
            application_type TEXT NOT NULL DEFAULT 'native',
            redirect_uris TEXT NOT NULL DEFAULT '[]',
            grant_types TEXT NOT NULL DEFAULT '["authorization_code","refresh_token"]',
            response_types TEXT NOT NULL DEFAULT '["code"]',
            token_endpoint_auth_method TEXT NOT NULL DEFAULT 'none',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS oauth_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            client_id TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            scope TEXT DEFAULT '',
            code_verifier TEXT NOT NULL,
            user_id INTEGER DEFAULT NULL,
            resource TEXT DEFAULT '',
            expires_at TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS oauth_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT NOT NULL,
            access_token TEXT NOT NULL,
            access_token_expires_at TEXT NOT NULL,
            refresh_token TEXT NOT NULL,
            refresh_token_expires_at TEXT NOT NULL,
            scope TEXT DEFAULT '',
            user_id INTEGER DEFAULT NULL,
            created_at TEXT NOT NULL
        );
    """)
    conn.commit()

    # 创建默认管理员账户（如果不存在）
    c.execute("SELECT COUNT(*) FROM admin_users")
    if c.fetchone()[0] == 0:
        c.execute(
            "INSERT INTO admin_users (username, password_hash, created_at) VALUES (?, ?, ?)",
            ("admin", generate_password_hash("admin123"), datetime.now().isoformat()),
        )
        conn.commit()

    _migrate_logs_add_source_model(conn)
    _migrate_logs_add_cache_tokens(conn)
    _migrate_provider_usage_add_cache_tokens(conn)
    _migrate_model_mappings(conn)
    _migrate_error_mappings(conn)
    _migrate_providers_add_disable_reason(conn)
    _migrate_providers_dual_url(conn)
    _migrate_providers_url_to_full_endpoint(conn)
    _migrate_providers_add_full_path(conn)
    _drop_legacy_mcp_image_config(conn)
    conn.close()


# ---- Providers CRUD ----

def get_providers():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM providers ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_provider(provider_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM providers WHERE id = ?", (provider_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def add_provider(name, anthropic_url="", openai_url="", api_key="", enabled=True, max_concurrency=0, full_path=1):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO providers (name, anthropic_url, openai_url, api_key, enabled, max_concurrency, disable_reason, created_at, full_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, anthropic_url, openai_url, api_key, int(enabled), int(max_concurrency), "", datetime.now().isoformat(), int(full_path)),
    )
    conn.commit()
    provider_id = c.lastrowid
    conn.close()
    return provider_id


def update_provider(provider_id, **kwargs):
    allowed = {"name", "anthropic_url", "openai_url", "api_key", "enabled", "max_concurrency", "disable_reason", "full_path"}
    fields = []
    values = []
    for k, v in kwargs.items():
        if k in allowed:
            fields.append(f"{k} = ?")
            values.append(int(v) if k in ("enabled", "max_concurrency", "full_path") else v)
    # 手动启用时，清除 disable_reason；手动禁用时，标记为 manual
    if "enabled" in kwargs:
        if kwargs["enabled"]:
            if "disable_reason" not in kwargs:
                fields.append("disable_reason = ?")
                values.append("")
        else:
            if "disable_reason" not in kwargs:
                fields.append("disable_reason = ?")
                values.append("manual")
    if not fields:
        return
    values.append(provider_id)
    conn = get_conn()
    conn.execute(f"UPDATE providers SET {', '.join(fields)} WHERE id = ?", values)
    conn.commit()
    conn.close()


def delete_provider(provider_id):
    conn = get_conn()
    conn.execute("DELETE FROM model_mappings WHERE provider_id = ?", (provider_id,))
    conn.execute("DELETE FROM provider_usage WHERE provider_id = ?", (provider_id,))
    conn.execute("DELETE FROM provider_billing_config WHERE provider_id = ?", (provider_id,))
    conn.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
    conn.commit()
    conn.close()


# ---- Model Mappings CRUD ----

def get_model_mappings():
    conn = get_conn()
    rows = conn.execute(
        "SELECT m.*, p.name as provider_name, p.anthropic_url, p.openai_url, p.api_key "
        "FROM model_mappings m LEFT JOIN providers p ON m.provider_id = p.id "
        "ORDER BY m.alias ASC, m.priority ASC, m.id ASC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_model_mapping_by_alias(alias):
    """按别名返回所有启用的映射（多条同名别名即负载均衡池），找不到返回 None。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT m.*, p.name as provider_name, p.anthropic_url, p.openai_url, p.api_key, "
        "p.max_concurrency as provider_max_concurrency, p.full_path "
        "FROM model_mappings m LEFT JOIN providers p ON m.provider_id = p.id "
        "WHERE m.alias = ? AND m.enabled = 1 AND p.enabled = 1 "
        "ORDER BY m.priority ASC, m.id ASC",
        (alias,),
    ).fetchall()
    conn.close()
    if not rows:
        return None
    return [dict(r) for r in rows]


def add_model_mapping(alias, target_model, provider_id, enabled=True, priority=1, model_type="text", max_tokens=0, role_mappings=None):
    conn = get_conn()
    c = conn.cursor()
    if role_mappings is None:
        role_mappings = "[]"
    elif isinstance(role_mappings, (list, dict)):
        role_mappings = json.dumps(role_mappings, ensure_ascii=False)
    c.execute(
        "INSERT INTO model_mappings (alias, target_model, provider_id, priority, model_type, max_tokens, enabled, role_mappings) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (alias, target_model, provider_id, int(priority), model_type, int(max_tokens), int(enabled), role_mappings),
    )
    conn.commit()
    mapping_id = c.lastrowid
    conn.close()
    return mapping_id


def update_model_mapping(mapping_id, **kwargs):
    allowed = {"alias", "target_model", "provider_id", "enabled", "priority", "model_type", "max_tokens", "role_mappings"}
    fields = []
    values = []
    for k, v in kwargs.items():
        if k in allowed:
            fields.append(f"{k} = ?")
            if k == "role_mappings":
                # 允许传 list/dict 或已序列化的 JSON 字符串
                if isinstance(v, (list, dict)):
                    v = json.dumps(v, ensure_ascii=False)
                values.append(v)
            else:
                values.append(int(v) if k in ("enabled", "priority", "max_tokens") else v)
    if not fields:
        return
    values.append(mapping_id)
    conn = get_conn()
    conn.execute(f"UPDATE model_mappings SET {', '.join(fields)} WHERE id = ?", values)
    conn.commit()
    conn.close()


def delete_model_mapping(mapping_id):
    conn = get_conn()
    conn.execute("DELETE FROM model_mappings WHERE id = ?", (mapping_id,))
    conn.commit()
    conn.close()


# ---- Logs ----

def add_log(provider, model, input_tokens, output_tokens, status, duration_ms, error_msg="", request_body="", response_body="", original_status_code=0, mapped_status_code=0, client_ip="", cache_read_input_tokens=0, cache_creation_input_tokens=0, source_model=""):
    conn = get_conn()
    conn.execute(
        "INSERT INTO logs (request_time, provider, source_model, model, input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, status, duration_ms, error_msg, request_body, response_body, original_status_code, mapped_status_code, client_ip) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.now().isoformat(), provider, source_model, model, input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, status, duration_ms, error_msg, request_body, response_body, original_status_code, mapped_status_code, client_ip),
    )
    conn.commit()
    conn.close()


def get_logs(page=1, per_page=50, status_filter=None, model_filter=None, ip_filter=None, provider_filter=None):
    conn = get_conn()
    where_clauses = []
    params = []
    if status_filter:
        where_clauses.append("status = ?")
        params.append(status_filter)
    if model_filter:
        where_clauses.append("(model LIKE ? OR source_model LIKE ?)")
        params.append(f"%{model_filter}%")
        params.append(f"%{model_filter}%")
    if ip_filter:
        where_clauses.append("client_ip LIKE ?")
        params.append(f"%{ip_filter}%")
    if provider_filter:
        where_clauses.append("provider = ?")
        params.append(provider_filter)
    where = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    total = conn.execute(f"SELECT COUNT(*) FROM logs {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM logs {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [per_page, (page - 1) * per_page],
    ).fetchall()
    conn.close()
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "logs": [dict(r) for r in rows],
    }


def get_distinct_providers():
    conn = get_conn()
    rows = conn.execute("SELECT DISTINCT provider FROM logs WHERE provider != '' ORDER BY provider").fetchall()
    conn.close()
    return [r["provider"] for r in rows]


def clear_logs():
    conn = get_conn()
    conn.execute("DELETE FROM logs")
    conn.commit()
    conn.close()


# ---- Error Mappings CRUD ----

def get_error_mappings():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM error_mappings ORDER BY provider, original_code").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_mapped_code(original_code, provider=""):
    """查找映射后的错误码，优先匹配指定 provider，其次匹配全局，未找到则返回原始值"""
    conn = get_conn()
    # 优先查找该 provider 的映射
    row = conn.execute(
        "SELECT mapped_code FROM error_mappings WHERE original_code = ? AND provider = ? AND enabled = 1",
        (original_code, provider),
    ).fetchone()
    if not row:
        # 回退到全局映射（provider 为空）
        row = conn.execute(
            "SELECT mapped_code FROM error_mappings WHERE original_code = ? AND provider = '' AND enabled = 1",
            (original_code,),
        ).fetchone()
    conn.close()
    return row["mapped_code"] if row else original_code


def add_error_mapping(provider, original_code, mapped_code, enabled=True):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO error_mappings (provider, original_code, mapped_code, enabled) VALUES (?, ?, ?, ?)",
        (provider, original_code, mapped_code, int(enabled)),
    )
    conn.commit()
    mid = c.lastrowid
    conn.close()
    return mid


def update_error_mapping(mapping_id, **kwargs):
    allowed = {"provider", "original_code", "mapped_code", "enabled"}
    fields = []
    values = []
    for k, v in kwargs.items():
        if k in allowed:
            fields.append(f"{k} = ?")
            values.append(int(v) if k in ("original_code", "mapped_code", "enabled") else v)
    if not fields:
        return
    values.append(mapping_id)
    conn = get_conn()
    conn.execute(f"UPDATE error_mappings SET {', '.join(fields)} WHERE id = ?", values)
    conn.commit()
    conn.close()


def delete_error_mapping(mapping_id):
    conn = get_conn()
    conn.execute("DELETE FROM error_mappings WHERE id = ?", (mapping_id,))
    conn.commit()
    conn.close()


# ---- Settings ----

def get_setting(key, default=""):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()


def get_all_settings():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}



# ---- Admin Users ----

def verify_admin_login(username, password):
    """验证管理员登录，返回用户信息或None"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM admin_users WHERE username = ?", (username,)).fetchone()
    conn.close()
    if row and check_password_hash(row["password_hash"], password):
        return {"id": row["id"], "username": row["username"]}
    return None


# ---- API Keys ----

def generate_api_key():
    """生成 sk- 开头的 API Key"""
    return "sk-" + secrets.token_hex(24)


def get_api_keys():
    """获取所有API Key（不返回完整key，只返回前缀）"""
    conn = get_conn()
    rows = conn.execute("SELECT id, key_name, api_key, enabled, created_at, last_used_at FROM api_keys ORDER BY id DESC").fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["api_key_prefix"] = d["api_key"][:11] + "..."
        del d["api_key"]
        result.append(d)
    return result


def add_api_key(key_name):
    """创建新的API Key，返回完整key信息"""
    key = generate_api_key()
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO api_keys (key_name, api_key, enabled, created_at) VALUES (?, ?, 1, ?)",
        (key_name, key, datetime.now().isoformat()),
    )
    conn.commit()
    kid = c.lastrowid
    conn.close()
    return {"id": kid, "key_name": key_name, "api_key": key}


def get_api_key_by_id(key_id):
    """根据ID获取完整API Key信息"""
    conn = get_conn()
    row = conn.execute("SELECT id, key_name, api_key, enabled, created_at, last_used_at FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


def delete_api_key(key_id):
    """删除API Key"""
    conn = get_conn()
    conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    conn.commit()
    conn.close()


def toggle_api_key(key_id, enabled):
    """启用/禁用API Key"""
    conn = get_conn()
    conn.execute("UPDATE api_keys SET enabled = ? WHERE id = ?", (int(enabled), key_id))
    conn.commit()
    conn.close()


def validate_api_key(key):
    """校验API Key是否有效，返回True/False"""
    if not key:
        return False
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM api_keys WHERE api_key = ? AND enabled = 1", (key,)
    ).fetchone()
    if row:
        # 更新最后使用时间
        conn.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
            (datetime.now().isoformat(), row["id"]),
        )
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False


# ---- Auto Cleanup ----

def cleanup_old_logs(retention_days):
    """删除超过 retention_days 天的日志，返回删除数量"""
    cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
    conn = get_conn()
    cursor = conn.execute("DELETE FROM logs WHERE request_time < ?", (cutoff,))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


# ---- 数据库压缩（VACUUM） ----
_vacuum_lock = threading.Lock()


def vacuum_db():
    """执行 VACUUM 重建数据库文件，回收被删除日志等遗留的空闲页。

    会获取 EXCLUSIVE 锁，执行期间写请求会短暂等待；建议在后台线程调用。
    使用 threading.Lock 防止与启动时的 VACUUM 并发。
    """
    if not _vacuum_lock.acquire(blocking=False):
        print("[VACUUM] 已有压缩任务在执行，跳过本次")
        return False
    conn = None
    try:
        conn = get_conn()
        conn.execute("VACUUM")
        conn.commit()
        print("[VACUUM] 数据库压缩完成")
        return True
    except Exception as e:
        print(f"[VACUUM] 错误: {e}")
        return False
    finally:
        if conn is not None:
            conn.close()
        _vacuum_lock.release()


def get_log_stats():
    """获取日志统计信息"""
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
    oldest = conn.execute("SELECT MIN(request_time) FROM logs").fetchone()[0] or ""
    conn.close()
    return {"total": total, "oldest": oldest}


def get_provider_stats():
    """按 provider 分组统计请求次数和 token 用量"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT provider, COUNT(*) as request_count, "
        "SUM(input_tokens) as total_input_tokens, "
        "SUM(output_tokens) as total_output_tokens, "
        "SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as error_count "
        "FROM logs GROUP BY provider ORDER BY request_count DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---- Provider Billing ----

_WINDOW_DURATIONS = {
    "5h": timedelta(hours=5),
    "week": timedelta(days=7),
    "month": timedelta(days=30),
}


def _get_window_start(window_type):
    """返回给定 window_type 对应的当前窗口起始时间（ISO 格式）"""
    now = datetime.now()
    if window_type == "5h":
        # 以当前小时为起点，整 5 小时窗口
        base = now.replace(minute=0, second=0, microsecond=0)
        hours_since_epoch = int(base.timestamp()) // 3600
        aligned_hours = (hours_since_epoch // 5) * 5
        return datetime.fromtimestamp(aligned_hours * 3600).isoformat()
    elif window_type == "week":
        # 以本周一 00:00 为起点
        monday = now - timedelta(days=now.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    elif window_type == "month":
        # 以当月 1 号 00:00 为起点
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    return now.isoformat()


def _is_window_expired(window_type, window_start):
    """检查给定的窗口是否已过期"""
    try:
        start = datetime.fromisoformat(window_start)
    except (ValueError, TypeError):
        return True
    now = datetime.now()
    duration = _WINDOW_DURATIONS.get(window_type)
    if not duration:
        return True
    return start + duration < now


def _get_applicable_windows(billing_mode):
    """根据计费模式返回需要追踪的时间窗口类型列表"""
    if billing_mode == "request_count":
        return ["5h", "week", "month"]
    elif billing_mode == "token_count":
        return ["5h", "week", "month"]
    return []  # balance 模式不需要时间窗口


def get_billing_config(provider_id):
    """获取 provider 的计费配置"""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM provider_billing_config WHERE provider_id = ?", (provider_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def save_billing_config(provider_id, billing_mode="request_count", limit_5h=None,
                        limit_week=None, limit_month=None, balance=0,
                        input_price_per_million=0, output_price_per_million=0,
                        expiration_date=None, warning_threshold=0.8):
    """创建或更新 provider 的计费配置"""
    now = datetime.now().isoformat()
    conn = get_conn()
    conn.execute(
        "INSERT INTO provider_billing_config "
        "(provider_id, billing_mode, limit_5h, limit_week, limit_month, balance, "
        "input_price_per_million, output_price_per_million, expiration_date, "
        "warning_threshold, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(provider_id) DO UPDATE SET "
        "billing_mode=excluded.billing_mode, limit_5h=excluded.limit_5h, limit_week=excluded.limit_week, "
        "limit_month=excluded.limit_month, balance=excluded.balance, "
        "input_price_per_million=excluded.input_price_per_million, "
        "output_price_per_million=excluded.output_price_per_million, "
        "expiration_date=excluded.expiration_date, warning_threshold=excluded.warning_threshold, "
        "updated_at=excluded.updated_at",
        (provider_id, billing_mode, limit_5h, limit_week, limit_month, balance,
         input_price_per_million, output_price_per_million, expiration_date,
         warning_threshold, now, now),
    )
    conn.commit()
    conn.close()


def delete_billing_config(provider_id):
    """删除 provider 的计费配置（恢复为无限制）"""
    conn = get_conn()
    conn.execute("DELETE FROM provider_billing_config WHERE provider_id = ?", (provider_id,))
    conn.execute("DELETE FROM provider_usage WHERE provider_id = ?", (provider_id,))
    conn.commit()
    conn.close()


def increment_provider_usage(provider_id, input_tokens, output_tokens, cache_read_input_tokens=0, cache_creation_input_tokens=0):
    """增加 provider 的使用计数"""
    config = get_billing_config(provider_id)
    if not config:
        return  # 无计费配置，不追踪

    billing_mode = config["billing_mode"]
    windows = _get_applicable_windows(billing_mode)

    conn = get_conn()
    now_iso = datetime.now().isoformat()

    for window_type in windows:
        window_start = _get_window_start(window_type)
        # Use UPSERT to avoid race condition on UNIQUE constraint
        conn.execute(
            "INSERT INTO provider_usage "
            "(provider_id, window_type, window_start, request_count, input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, balance_used) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?, 0) "
            "ON CONFLICT(provider_id, window_type, window_start) DO UPDATE SET "
            "request_count=request_count+1, "
            "input_tokens=input_tokens+excluded.input_tokens, "
            "output_tokens=output_tokens+excluded.output_tokens, "
            "cache_read_input_tokens=cache_read_input_tokens+excluded.cache_read_input_tokens, "
            "cache_creation_input_tokens=cache_creation_input_tokens+excluded.cache_creation_input_tokens",
            (provider_id, window_type, window_start, input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens),
        )

    # balance 模式：计算费用并扣减余额
    # Anthropic 缓存定价：cache_creation 按全额 input 价格，cache_read 按 0.1x（90%折扣）
    if billing_mode == "balance":
        input_cost = ((input_tokens + cache_creation_input_tokens) / 1_000_000) * config["input_price_per_million"]
        cache_read_cost = (cache_read_input_tokens / 1_000_000) * config["input_price_per_million"] * 0.1  # 90% discount
        output_cost = (output_tokens / 1_000_000) * config["output_price_per_million"]
        total_cost = input_cost + cache_read_cost + output_cost
        conn.execute(
            "UPDATE provider_billing_config SET balance = balance - ?, updated_at = ? WHERE provider_id = ?",
            (total_cost, now_iso, provider_id),
        )

    conn.commit()
    conn.close()


def get_provider_usage(provider_id):
    """获取 provider 当前所有活跃窗口的使用情况"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM provider_usage WHERE provider_id = ?", (provider_id,)
    ).fetchall()
    conn.close()

    active = []
    for row in rows:
        r = dict(row)
        if not _is_window_expired(r["window_type"], r["window_start"]):
            active.append(r)

    return active


def _calculate_usage_percent(usage_row, config):
    """计算某个窗口的使用百分比"""
    window_type = usage_row["window_type"]
    limit_key = f"limit_{window_type}"
    limit = config.get(limit_key)
    if limit is None or limit <= 0:
        return 0.0  # 无限制

    if config["billing_mode"] == "request_count":
        return usage_row["request_count"] / limit
    elif config["billing_mode"] == "token_count":
        total_tokens = usage_row["input_tokens"] + usage_row["output_tokens"] + usage_row.get("cache_read_input_tokens", 0) + usage_row.get("cache_creation_input_tokens", 0)
        return total_tokens / limit
    return 0.0


def check_provider_billing(provider_id):
    """检查 provider 的计费状态，返回是否允许请求"""
    config = get_billing_config(provider_id)
    if not config:
        return {"allowed": True, "reason": "", "usage_percent": 0.0, "near_limit": False}

    now = datetime.now()

    # 检查过期日期
    if config["expiration_date"]:
        try:
            exp = datetime.fromisoformat(config["expiration_date"])
            if now > exp:
                return {
                    "allowed": False,
                    "reason": "计费已过期",
                    "usage_percent": 1.0,
                    "near_limit": True,
                }
        except (ValueError, TypeError):
            pass

    billing_mode = config["billing_mode"]
    warning_threshold = config["warning_threshold"] or 0.8

    # balance 模式检查
    if billing_mode == "balance":
        balance = config["balance"] or 0
        if balance <= 0:
            return {
                "allowed": False,
                "reason": "余额不足",
                "usage_percent": 1.0,
                "near_limit": True,
            }
        return {"allowed": True, "reason": "", "usage_percent": 0.0, "near_limit": False}

    # request_count / token_count 模式：检查各时间窗口
    usages = get_provider_usage(provider_id)
    max_usage_percent = 0.0
    near_limit = False

    for usage in usages:
        percent = _calculate_usage_percent(usage, config)
        if percent > max_usage_percent:
            max_usage_percent = percent
        if percent >= warning_threshold:
            near_limit = True
        if percent >= 1.0:
            limit_key = f"limit_{usage['window_type']}"
            limit = config.get(limit_key)
            return {
                "allowed": False,
                "reason": f"{usage['window_type']} 窗口使用量已达上限 ({limit})",
                "usage_percent": percent,
                "near_limit": True,
            }

    return {
        "allowed": True,
        "reason": "",
        "usage_percent": max_usage_percent,
        "near_limit": near_limit,
    }


def get_all_billing_overview():
    """获取所有 provider 的计费状态概览"""
    providers = get_providers()
    result = []
    for p in providers:
        pid = p["id"]
        config = get_billing_config(pid)
        billing_check = check_provider_billing(pid)
        usages = get_provider_usage(pid)
        result.append({
            "provider_id": pid,
            "provider_name": p["name"],
            "enabled": bool(p["enabled"]),
            "has_billing": config is not None,
            "billing_config": dict(config) if config else None,
            "usage": [dict(u) for u in usages],
            "allowed": billing_check["allowed"],
            "reason": billing_check["reason"],
            "usage_percent": billing_check["usage_percent"],
            "near_limit": billing_check["near_limit"],
        })
    return result


def auto_disable_over_limit_providers():
    """检查所有 provider，自动禁用超限的 provider"""
    overview = get_all_billing_overview()
    disabled = []
    for item in overview:
        if item["has_billing"] and not item["allowed"] and item["enabled"]:
            update_provider(item["provider_id"], enabled=False, disable_reason="billing")
            disabled.append(item["provider_name"])
    return disabled


def reset_expired_windows_and_reenable():
    """Reset expired usage windows and re-enable providers whose limits have cleared."""
    conn = get_conn()
    rows = conn.execute("SELECT id, window_type, window_start FROM provider_usage").fetchall()
    expired_ids = []
    for row in rows:
        if _is_window_expired(row["window_type"], row["window_start"]):
            expired_ids.append(row["id"])
    if expired_ids:
        placeholders = ",".join("?" * len(expired_ids))
        conn.execute(f"DELETE FROM provider_usage WHERE id IN ({placeholders})", expired_ids)
        conn.commit()
    expired_count = len(expired_ids)

    # Re-enable providers that were auto-disabled (billing) but now have cleared windows
    # 手动禁用的提供商（disable_reason='manual'）不会被自动重新启用
    reenabled = []
    disabled_providers = conn.execute(
        "SELECT p.id, p.name FROM providers p "
        "JOIN provider_billing_config bc ON p.id = bc.provider_id "
        "WHERE p.enabled = 0 AND p.disable_reason = 'billing'"
    ).fetchall()

    for row in disabled_providers:
        provider_id = row["id"]
        provider_name = row["name"]
        # Check billing status without updating anything
        billing_check = check_provider_billing(provider_id)
        if billing_check["allowed"]:
            conn.execute(
                "UPDATE providers SET enabled = 1, disable_reason = '' WHERE id = ?",
                (provider_id,),
            )
            conn.commit()
            reenabled.append(provider_name)

    conn.close()
    return expired_count, reenabled


# ---- MCP Image Config (已移除，仅清理旧表) ----

def _drop_legacy_mcp_image_config(conn):
    """MCP 图片理解功能已移除，删除遗留的 mcp_image_config 表"""
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='mcp_image_config'").fetchall()]
    if tables:
        conn.execute("DROP TABLE mcp_image_config")
        conn.commit()
