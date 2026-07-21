import sqlite3
import configparser
import json
import os
import sys
import secrets
import threading
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash

# Windows 控制台默认用 GBK 编码，直接 print 中文会乱码；强制 stdout/stderr 用 UTF-8。
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 打包成 exe（PyInstaller onefile）后，__file__ 指向临时解压目录 _MEIPASS，
# 数据库必须放在 exe 同目录下，否则每次启动数据都会丢失。
if getattr(sys, "frozen", False):
    _BASE_DIR = os.path.dirname(sys.executable)
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_BASE_DIR, "proxy.db")


# ---- proxy.ini 外部配置 ----
# 配置文件与 proxy.db 同目录（打包后位于 exe 同目录），用于配置启动端口与默认管理员账号。
# 文件可选：不存在或某项未配置时使用以下内置默认值，行为与历史版本一致。
_CONFIG_PATH = os.path.join(_BASE_DIR, "proxy.ini")

_DEFAULT_SERVER_PORT = 5000
_DEFAULT_ADMIN_USERNAME = "admin"
_DEFAULT_ADMIN_PASSWORD = "admin123"


def _read_ini():
    """读取 proxy.ini。文件不存在或解析失败时返回空 ConfigParser，由调用方走默认值。"""
    parser = configparser.ConfigParser(
        interpolation=None,  # 关闭插值，避免密码中 % 被当作特殊字符
        inline_comment_prefixes=("#", ";"),
    )
    if os.path.exists(_CONFIG_PATH):
        try:
            parser.read(_CONFIG_PATH, encoding="utf-8")
        except Exception as e:
            print(f"[config] 读取 proxy.ini 失败，使用默认配置: {e}")
    return parser


def get_server_port():
    """返回服务启动端口。proxy.ini 未配置或非法时返回默认 5000。"""
    parser = _read_ini()
    try:
        port = parser.getint("server", "port", fallback=_DEFAULT_SERVER_PORT)
    except Exception as e:
        print(f"[config] 解析 [server].port 失败，使用默认 {_DEFAULT_SERVER_PORT}: {e}")
        return _DEFAULT_SERVER_PORT
    if not (1 <= port <= 65535):
        print(f"[config] [server].port={port} 越界(1-65535)，使用默认 {_DEFAULT_SERVER_PORT}")
        return _DEFAULT_SERVER_PORT
    return port


def get_default_admin_credentials():
    """返回 proxy.ini 配置的管理员 (username, password)，未配置时为 admin/admin123。

    作为管理员账户的唯一事实来源：每次启动 init_db() 都会据此校准 admin_users 表，
    修改 proxy.ini 的账号/密码后重启即生效。
    """
    parser = _read_ini()
    username = parser.get("admin", "username", fallback=_DEFAULT_ADMIN_USERNAME) or _DEFAULT_ADMIN_USERNAME
    password = parser.get("admin", "password", fallback=_DEFAULT_ADMIN_PASSWORD) or _DEFAULT_ADMIN_PASSWORD
    return username, password


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
    用 settings 键 baseline_url_full_endpoint_done='1' 做一次性标记，确保只执行一次，
    避免误改用户后续手动填入的非标准端点。

    旧版曾用 PRAGMA user_version=1 做此标记，但新版 user_version 由版本迁移框架统一管理
    （v1 = provider_billing_config 有 cache_read_price_per_million 列），两者语义冲突，
    故改用 settings 键。过渡时 _baseline_migrate_to_v0 会把旧 user_version>=1 的库
    迁移为 baseline_url_full_endpoint_done='1'，本函数据此跳过、绝不重改用户端点。
    """
    done_row = conn.execute(
        "SELECT value FROM settings WHERE key = 'baseline_url_full_endpoint_done'"
    ).fetchone()
    if done_row and done_row[0] == "1":
        return  # 已执行过此迁移（settings 键标记）
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
    # 完成一次性标记：无论是否有行被更新都写入，保证只跑一次
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('baseline_url_full_endpoint_done', '1')"
    )
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


def _migrate_billing_config_add_cache_read_price(conn):
    """检查 provider_billing_config 表是否缺少 cache_read_price_per_million 列，若缺少则按迁移规则重建表"""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(provider_billing_config)").fetchall()]
    if "cache_read_price_per_million" in cols:
        return  # 已有缓存命中价格列，无需迁移
    # 1. 备份现有数据
    old_rows = conn.execute("SELECT * FROM provider_billing_config").fetchall()
    old_col_names = [desc[0] for desc in conn.execute("SELECT * FROM provider_billing_config LIMIT 0").description]
    # 2. 删除旧表
    conn.execute("DROP TABLE provider_billing_config")
    # 3. 创建新表（含 cache_read_price_per_million 列）
    conn.execute("""
        CREATE TABLE provider_billing_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL UNIQUE,
            billing_mode TEXT NOT NULL DEFAULT 'request_count',
            limit_5h INTEGER DEFAULT NULL,
            limit_week INTEGER DEFAULT NULL,
            limit_month INTEGER DEFAULT NULL,
            balance REAL DEFAULT 0,
            input_price_per_million REAL DEFAULT 0,
            output_price_per_million REAL DEFAULT 0,
            cache_read_price_per_million REAL DEFAULT 0,
            expiration_date TEXT DEFAULT NULL,
            warning_threshold REAL NOT NULL DEFAULT 0.8,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (provider_id) REFERENCES providers(id)
        )
    """)
    # 4. 回填数据（新列填 0）
    new_cols = ["id", "provider_id", "billing_mode", "limit_5h", "limit_week", "limit_month",
                "balance", "input_price_per_million", "output_price_per_million",
                "cache_read_price_per_million", "expiration_date", "warning_threshold",
                "created_at", "updated_at"]
    for row in old_rows:
        values = [row[c] if c in old_col_names else 0 for c in new_cols]
        placeholders = ",".join("?" * len(values))
        conn.execute(f"INSERT INTO provider_billing_config ({','.join(new_cols)}) VALUES ({placeholders})", values)
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


def _migrate_model_mappings_add_reasoning(conn):
    """v1->v2：model_mappings 表新增 reasoning_effort_supported 列，默认 1（打开/透传）。

    0 = 不透传（保守跳过：_apply_reasoning_effort 只 pop 私有键、不发任何字段）。
    1 = 透传（按 OpenAI 兼容格式发 reasoning_effort=<effort>，原值 low/medium/high 不翻译）。

    严格遵循 SQLite Migration Rule：备份 -> DROP -> CREATE(新结构) -> 回填 -> 重建索引 -> 完整性校验。
    model_mappings 表当前规模 ~14 行，备份-重建开销可忽略。

    回填统一 1（打开/透传）：GLM/DeepSeek 原生认字段；MiniMax 接受但不调深度、无害；
    火山/讯飞/Kimi 参数名不同，传过去多半被上游忽略（不报错），故「默认打开」对绝大多数
    上游都是正确选择。个别上游若实测出现 400，可在 UI 关闭该映射的开关。
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

    # 4. 重建索引（与 _create_latest_schema / _migrate_model_mappings 同名，保持一致）
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
                1,  # 默认打开：迁移后老映射也默认透传 reasoning_effort，运维可按需关闭
            ),
        )
    conn.commit()

    # 6. 完整性校验（与 _migrate_model_mappings 同模式）
    mm_count_after = conn.execute("SELECT COUNT(*) FROM model_mappings").fetchone()[0]
    if mm_count_after != mm_count_before:
        raise RuntimeError(f"model_mappings 迁移后行数不一致：{mm_count_before} -> {mm_count_after}")
    dangling_after = conn.execute(
        "SELECT COUNT(*) FROM model_mappings WHERE provider_id NOT IN (SELECT id FROM providers)"
    ).fetchone()[0]
    if dangling_after != dangling_before:
        raise RuntimeError(f"model_mappings 迁移后悬空 provider_id 数量变化：{dangling_before} -> {dangling_after}")


def _migrate_model_mappings_add_think_injection(conn):
    """v2->v3：model_mappings 表新增 think_injection 列，默认 0（关闭/不注入 <think>）。

    0 = 不注入：reasoning item 在 Responses->Chat 转换时按原行为 continue 丢弃。
    1 = 注入：reasoning 文本以 <think>...</think> 形式 prepend 到对应 assistant 消息 content 前缀
        （MiniMax-M3 Interleaved Thinking 必需；其他上游把 <think> 当普通文本，无害）。

    严格遵循 SQLite Migration Rule：备份 -> DROP -> CREATE(新结构) -> 回填 -> 重建索引 -> 完整性校验。
    model_mappings 表当前规模 ~14 行，备份-重建开销可忽略。

    回填统一 0（关闭）：「默认不注入」对所有上游都是安全选择（维持原行为），仅 MiniMax
    等依赖 Interleaved Thinking 的上游需在 UI 显式开启。避免对老映射默认开开关导致
    历史请求历史携带额外 token（虽然无害，但避免无谓的语义变更）。
    """
    cols = [row[1] for row in conn.execute("PRAGMA table_info(model_mappings)").fetchall()]
    if "think_injection" in cols:
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
            think_injection INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (provider_id) REFERENCES providers(id)
        )
    """)

    # 4. 重建索引
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_model_mappings_alias_enabled "
        "ON model_mappings(alias, enabled)"
    )

    # 5. 回填（统一 0，默认关闭/不注入 <think>）
    for row in old_rows:
        rd = dict(zip(old_col_names, row))
        conn.execute(
            "INSERT INTO model_mappings (id, alias, target_model, provider_id, priority, "
            "model_type, max_tokens, enabled, role_mappings, reasoning_effort_supported, think_injection) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rd["id"], rd["alias"], rd["target_model"], rd["provider_id"],
                int(rd.get("priority") or 1), rd.get("model_type") or "text",
                int(rd.get("max_tokens") or 0), int(rd.get("enabled", 1)),
                rd.get("role_mappings") or "[]",
                int(rd.get("reasoning_effort_supported", 1)),
                0,  # 默认关闭：迁移后老映射维持原行为（不注入），MiniMax 等需手动开启
            ),
        )
    conn.commit()

    # 6. 完整性校验
    mm_count_after = conn.execute("SELECT COUNT(*) FROM model_mappings").fetchone()[0]
    if mm_count_after != mm_count_before:
        raise RuntimeError(f"model_mappings 迁移后行数不一致：{mm_count_before} -> {mm_count_after}")
    dangling_after = conn.execute(
        "SELECT COUNT(*) FROM model_mappings WHERE provider_id NOT IN (SELECT id FROM providers)"
    ).fetchone()[0]
    if dangling_after != dangling_before:
        raise RuntimeError(f"model_mappings 迁移后悬空 provider_id 数量变化：{dangling_before} -> {dangling_after}")


def _migrate_model_mappings_add_reasoning_content_field(conn):
    """v3->v4：model_mappings 表新增 reasoning_content_field 列，默认 1（开启/以字段注入）。

    0 = 不注入：reasoning item 在 Responses->Chat 转换时按原行为 continue 丢弃。
    1 = 注入：reasoning 文本以独立 reasoning_content 字段注入对应 assistant 消息
        （DeepSeek/GLM/Kimi 思考模式原生字段，多轮工具调用必需，缺失会 400）。
        其他上游忽略未知字段，无害。

    与 think_injection 互斥（前端 UI 保证不同时开启）：think_injection 以标签注入
    content（MiniMax 专用），reasoning_content_field 以独立字段注入（DS/GLM/Kimi 专用）。
    两者都关 = 丢弃 reasoning。

    严格遵循 SQLite Migration Rule：备份 -> DROP -> CREATE(新结构) -> 回填 -> 重建索引 -> 完整性校验。

    回填统一 1（开启）：reasoning_content 是 OpenAI 兼容上游更通用的思考承载方式
    （DS/GLM/Kimi 原生认），其他上游忽略未知字段不报错，故默认开启安全。与 think_injection
    回填 0（保守，仅 MiniMax 必需）形成对照。
    """
    cols = [row[1] for row in conn.execute("PRAGMA table_info(model_mappings)").fetchall()]
    if "reasoning_content_field" in cols:
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
            think_injection INTEGER NOT NULL DEFAULT 0,
            reasoning_content_field INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (provider_id) REFERENCES providers(id)
        )
    """)

    # 4. 重建索引
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_model_mappings_alias_enabled "
        "ON model_mappings(alias, enabled)"
    )

    # 5. 回填（统一 1，默认开启/以 reasoning_content 字段注入）
    for row in old_rows:
        rd = dict(zip(old_col_names, row))
        conn.execute(
            "INSERT INTO model_mappings (id, alias, target_model, provider_id, priority, "
            "model_type, max_tokens, enabled, role_mappings, reasoning_effort_supported, "
            "think_injection, reasoning_content_field) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rd["id"], rd["alias"], rd["target_model"], rd["provider_id"],
                int(rd.get("priority") or 1), rd.get("model_type") or "text",
                int(rd.get("max_tokens") or 0), int(rd.get("enabled", 1)),
                rd.get("role_mappings") or "[]",
                int(rd.get("reasoning_effort_supported", 1)),
                int(rd.get("think_injection", 0)),
                1,  # 默认开启：DS/GLM/Kimi 多轮工具调用必需，其他上游忽略未知字段无害
            ),
        )
    conn.commit()

    # 6. 完整性校验
    mm_count_after = conn.execute("SELECT COUNT(*) FROM model_mappings").fetchone()[0]
    if mm_count_after != mm_count_before:
        raise RuntimeError(f"model_mappings 迁移后行数不一致：{mm_count_before} -> {mm_count_after}")
    dangling_after = conn.execute(
        "SELECT COUNT(*) FROM model_mappings WHERE provider_id NOT IN (SELECT id FROM providers)"
    ).fetchone()[0]
    if dangling_after != dangling_before:
        raise RuntimeError(f"model_mappings 迁移后悬空 provider_id 数量变化：{dangling_before} -> {dangling_after}")


def _migrate_model_mappings_add_native_responses(conn):
    """v4->v5：model_mappings 表新增 native_responses 列，默认 0（关闭/走 Responses→Chat 转换）。

    0 = 关闭：/openai 端点对该 mapping 走原 Responses→Chat 双向转换路径（原行为）。
    1 = 开启：/openai 端点对该 mapping 跳过转换，按 provider.openai_url 派生 /responses
        端点直接转发原 Responses body（仿 Anthropic handler 直转语义）。

    与 think_injection / reasoning_content_field 三方互斥（前端 UI 保证同一时间最多开一个）：
    开启 native_responses 后，Responses↔Chat 转换被完全跳过，think_injection 与
    reasoning_content_field 都失去作用对象，故三者互斥。三个都关 = 走转换并丢弃 reasoning。

    严格遵循 SQLite Migration Rule：备份 -> DROP -> CREATE(新结构) -> 回填 -> 重建索引 -> 完整性校验。

    回填统一 0（保守，默认关闭）：native_responses 透传要求 provider 实际暴露 /responses 端点，
    多数 OpenAI 兼容上游仅暴露 /chat/completions；自动开启会误把这类上游打到 /responses 404，
    故默认关闭，由用户对确知支持 Responses 的上游显式开启。
    """
    cols = [row[1] for row in conn.execute("PRAGMA table_info(model_mappings)").fetchall()]
    if "native_responses" in cols:
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
            think_injection INTEGER NOT NULL DEFAULT 0,
            reasoning_content_field INTEGER NOT NULL DEFAULT 1,
            native_responses INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (provider_id) REFERENCES providers(id)
        )
    """)

    # 4. 重建索引
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_model_mappings_alias_enabled "
        "ON model_mappings(alias, enabled)"
    )

    # 5. 回填（统一 0，默认关闭/走 Responses→Chat 转换）
    for row in old_rows:
        rd = dict(zip(old_col_names, row))
        conn.execute(
            "INSERT INTO model_mappings (id, alias, target_model, provider_id, priority, "
            "model_type, max_tokens, enabled, role_mappings, reasoning_effort_supported, "
            "think_injection, reasoning_content_field, native_responses) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rd["id"], rd["alias"], rd["target_model"], rd["provider_id"],
                int(rd.get("priority") or 1), rd.get("model_type") or "text",
                int(rd.get("max_tokens") or 0), int(rd.get("enabled", 1)),
                rd.get("role_mappings") or "[]",
                int(rd.get("reasoning_effort_supported", 1)),
                int(rd.get("think_injection", 0)),
                int(rd.get("reasoning_content_field", 1)),
                0,  # 默认关闭：多数上游仅暴露 /chat/completions，需用户显式开启
            ),
        )
    conn.commit()

    # 6. 完整性校验
    mm_count_after = conn.execute("SELECT COUNT(*) FROM model_mappings").fetchone()[0]
    if mm_count_after != mm_count_before:
        raise RuntimeError(f"model_mappings 迁移后行数不一致：{mm_count_before} -> {mm_count_after}")
    dangling_after = conn.execute(
        "SELECT COUNT(*) FROM model_mappings WHERE provider_id NOT IN (SELECT id FROM providers)"
    ).fetchone()[0]
    if dangling_after != dangling_before:
        raise RuntimeError(f"model_mappings 迁移后悬空 provider_id 数量变化：{dangling_before} -> {dangling_after}")


def _migrate_model_mappings_add_disable_reason(conn):
    """v5->v6：model_mappings 表新增 disable_reason 列，与 providers.disable_reason 对齐。

    用途：区分 mapping 的禁用来源。
      ''                    —— 未禁用（或联动启用后归位）
      'manual'              —— 用户在 UI 上手动禁用该 mapping（toggleModel）
      'provider_disabled'   —— 因所属 provider 被禁用而联动禁用（级联）

    联动逻辑：
      provider 禁用 → 只把当前 enabled=1 的 mapping 标记为 'provider_disabled' 并禁用
                      （已 'manual' 的不动，避免把用户手动禁用改写成联动禁用）
      provider 启用 → 只恢复 'provider_disabled' 的 mapping（保留 'manual' 的禁用状态）

    回填策略（严格按 SQLite Migration Rule：备份 -> DROP -> CREATE(新结构) -> 回填 -> 重建索引 -> 完整性校验）：
      历史 enabled=1 的行 → disable_reason=''（未禁用）
      历史 enabled=0 的行 → disable_reason='manual'（历史禁用一律视为用户手动禁用，
                          因为 v6 之前从无 provider 级联逻辑，所有禁用都是用户自己设的）
    """
    cols = [row[1] for row in conn.execute("PRAGMA table_info(model_mappings)").fetchall()]
    if "disable_reason" in cols:
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
            think_injection INTEGER NOT NULL DEFAULT 0,
            reasoning_content_field INTEGER NOT NULL DEFAULT 1,
            native_responses INTEGER NOT NULL DEFAULT 0,
            disable_reason TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (provider_id) REFERENCES providers(id)
        )
    """)

    # 4. 重建索引
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_model_mappings_alias_enabled "
        "ON model_mappings(alias, enabled)"
    )

    # 5. 回填
    for row in old_rows:
        rd = dict(zip(old_col_names, row))
        old_enabled = int(rd.get("enabled", 1))
        # 历史 enabled=0 视为用户手动禁用；enabled=1 视为未禁用
        disable_reason = "manual" if old_enabled == 0 else ""
        conn.execute(
            "INSERT INTO model_mappings (id, alias, target_model, provider_id, priority, "
            "model_type, max_tokens, enabled, role_mappings, reasoning_effort_supported, "
            "think_injection, reasoning_content_field, native_responses, disable_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rd["id"], rd["alias"], rd["target_model"], rd["provider_id"],
                int(rd.get("priority") or 1), rd.get("model_type") or "text",
                int(rd.get("max_tokens") or 0), old_enabled,
                rd.get("role_mappings") or "[]",
                int(rd.get("reasoning_effort_supported", 1)),
                int(rd.get("think_injection", 0)),
                int(rd.get("reasoning_content_field", 1)),
                int(rd.get("native_responses", 0)),
                disable_reason,
            ),
        )
    conn.commit()

    # 6. 完整性校验
    mm_count_after = conn.execute("SELECT COUNT(*) FROM model_mappings").fetchone()[0]
    if mm_count_after != mm_count_before:
        raise RuntimeError(f"model_mappings 迁移后行数不一致：{mm_count_before} -> {mm_count_after}")
    dangling_after = conn.execute(
        "SELECT COUNT(*) FROM model_mappings WHERE provider_id NOT IN (SELECT id FROM providers)"
    ).fetchone()[0]
    if dangling_after != dangling_before:
        raise RuntimeError(f"model_mappings 迁移后悬空 provider_id 数量变化：{dangling_before} -> {dangling_after}")
    empty_alias = conn.execute("SELECT COUNT(*) FROM model_mappings WHERE alias IS NULL OR alias = ''").fetchone()[0]
    if empty_alias != 0:
        raise RuntimeError(f"model_mappings 迁移后存在 {empty_alias} 行空 alias")


def _migrate_reconcile_disabled_provider_mappings(conn):
    """v6->v7：修正历史数据--provider 禁用但其下 mapping 仍 enabled=1 的假启用残留。

    背景：v6 引入 disable_reason 列前，provider 禁用不会级联到 mapping（当时无级联
    机制）。老库迁移到 v6 时回填只看 mapping 自身 enabled，未参考 provider 状态，
    导致「provider 禁用但 mapping enabled=1 disable_reason=''」的假启用残留：UI 显示
    启用，却因路由层 get_model_mapping_by_alias 的 p.enabled=1 过滤而实际无效。
    本迁移把这些 mapping 修正为 enabled=0 disable_reason='provider_disabled'，与
    _cascade_provider_enabled_to_mappings 的级联禁用语义对齐。

    纯数据修正，不改表结构。幂等保证：
      - settings 标记键做一次性守卫，已执行则直接 return；
      - UPDATE 本身也只命中 enabled=1 且 provider 禁用的不一致行，重复执行无副作用。
    特征检测用 settings 标记键（纯数据迁移无列/索引/表结构特征可检测）。
    """
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'migrate_reconcile_disabled_provider_mappings_done'"
    ).fetchone()
    if row:
        return  # 幂等：已执行过

    conn.execute(
        "UPDATE model_mappings SET enabled = 0, disable_reason = 'provider_disabled' "
        "WHERE enabled = 1 AND provider_id IN (SELECT id FROM providers WHERE enabled = 0)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value) "
        "VALUES ('migrate_reconcile_disabled_provider_mappings_done', '1')"
    )
    conn.commit()


def _migrate_oauth_tokens_access_token_unique(conn):
    """v7->v8：oauth_tokens.access_token 新增命名唯一索引，堵 alg=none JWT 伪造（BE-02）。

    背景：原 access_token 为 alg=none JWT，validate_access_token 仅 base64 解码查 exp、
    不查库，任何掌握 client_id 的客户端都能伪造令牌绕过 /mcp 的 API Key 校验。修复后
    access_token 改为不透明随机串，validate_access_token 强制查 oauth_tokens 表。查表
    需在 access_token 上建索引避免全表扫描，且唯一索引可防止历史 JWT 生成路径在极端
    时序下（同一秒同一 client_id+scope，jti 仍不同故实际不冲突）产生的潜在重复。

    用命名显式唯一索引 idx_oauth_tokens_access_token，不用列级 UNIQUE 约束：
      _self_check_schema 排除 sqlite_autoindex_*，列级 UNIQUE 在老库上无法被自检兜底；
      命名显式索引可被自检与 _calibrate_user_version 特征检测可靠捕获（查 sqlite_master）。

    幂等保证：
      - sqlite_master 查索引名做存在性守卫，已存在则直接 return；
      - 建索引前清理潜在重复 access_token（保留每组 id 最大者），否则 CREATE UNIQUE INDEX
        会抛 IntegrityError；历史 alg=none JWT 因含 uuid4 jti 实际不会重复，清理仅作防御。
    不改表结构（列定义不变），仅加索引，无需 backup-drop-recreate。
    """
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_oauth_tokens_access_token'"
    ).fetchone()
    if existing:
        return  # 幂等：索引已存在则跳过

    # 防御性去重：保留每组 access_token 的 id 最大者，删除旧重复行（token 可经 refresh 重新获取）
    conn.execute(
        "DELETE FROM oauth_tokens "
        "WHERE id NOT IN (SELECT MAX(id) FROM oauth_tokens GROUP BY access_token)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX idx_oauth_tokens_access_token ON oauth_tokens(access_token)"
    )
    conn.commit()


def _migrate_drop_oauth_tables(conn):
    """v8->v9：删除 OAuth 相关三张表（oauth_clients / oauth_codes / oauth_tokens）。

    背景：OAuth 功能自项目初始化起随脚手架带入，但从未被实际使用（三表均为空行），
    /mcp 路由已无处理函数（历史 MCP 图片理解功能已移除），前端管理界面无 OAuth 入口。
    保留这些代码带来一整批安全缺陷（BE-02 alg=none 伪造、BE-05 撞名注册、BE-07 授权码
    不校验 redirect_uri、BE-10 refresh 不轮换等），删除后攻击面归零。

    幂等保证：DROP TABLE IF EXISTS 对不存在的表是空操作。
    """
    conn.execute("DROP TABLE IF EXISTS oauth_clients")
    conn.execute("DROP TABLE IF EXISTS oauth_codes")
    conn.execute("DROP TABLE IF EXISTS oauth_tokens")
    conn.commit()


def _create_latest_schema(conn):
    """用最新 schema 创建所有表（CREATE TABLE IF NOT EXISTS）。

    这是期望 schema 的「单一事实来源」，全新库直接获得最新结构；
    老库因 IF NOT EXISTS 对已存在表是空操作，差异需通过版本迁移补齐。
    自检函数 _self_check_schema 会基于此函数派生期望结构做反射对比。
    """
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
            reasoning_effort_supported INTEGER NOT NULL DEFAULT 1,
            think_injection INTEGER NOT NULL DEFAULT 0,
            reasoning_content_field INTEGER NOT NULL DEFAULT 1,
            native_responses INTEGER NOT NULL DEFAULT 0,
            disable_reason TEXT NOT NULL DEFAULT '',
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
        INSERT OR IGNORE INTO settings (key, value) VALUES ('degradation_enabled', '0');
        INSERT OR IGNORE INTO settings (key, value) VALUES ('degradation_duration', '30');
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
            cache_read_price_per_million REAL DEFAULT 0,
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
    """)
    conn.commit()


def _sync_admin_user(conn):
    """同步管理员账户，使其与 proxy.ini 的 [admin] 段保持一致。

    语义：proxy.ini 为唯一事实来源 —— 每次启动都会校准账号与密码。
      - 不存在则创建
      - 用户名相同则按需更新密码（仅当 ini 密码与库中不同才写）
      - 清理掉 ini 之外的残留管理员（用于改用户名）
    未配置 proxy.ini 时使用默认 admin/admin123，行为与历史版本一致。
    """
    admin_username, admin_password = get_default_admin_credentials()
    existing = {row["username"]: row for row in conn.execute(
        "SELECT id, username, password_hash FROM admin_users"
    ).fetchall()}
    changed = False
    if admin_username in existing:
        # 同名账户：用 check_password_hash 判断 ini 密码是否已与库一致，避免无谓刷新
        if not check_password_hash(existing[admin_username]["password_hash"], admin_password):
            conn.execute(
                "UPDATE admin_users SET password_hash = ? WHERE username = ?",
                (generate_password_hash(admin_password), admin_username),
            )
            changed = True
    else:
        # ini 用户名不存在：新建（保留原账户，稍后统一清理）
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (admin_username, generate_password_hash(admin_password), datetime.now().isoformat()),
        )
        changed = True
    # 清理 ini 之外的残留管理员（用于改用户名的场景）
    stale = [u for u in existing if u != admin_username]
    for u in stale:
        conn.execute("DELETE FROM admin_users WHERE username = ?", (u,))
    if stale or changed:
        conn.commit()


# ============================================================================
# Schema 版本号线性迁移框架（Versioned Migration Framework）
# ============================================================================
#
# 版本语义：
#   v0 = provider_billing_config 表【没有】cache_read_price_per_million 列
#        （balance 模式下 cache_read 回退到 input 价格的 0.1x 兜底）。
#   v1 = provider_billing_config 表【有】cache_read_price_per_million 列
#        （balance 模式下 cache_read 按用户配置的独立价格计费）。
# 版本边界严格锚定这一列；其余表的结构演进属于 v0 基线建设，不计入版本边界。
#
# 流程：init_db -> 全新库直跳 / 老库走 基线(_baseline_migrate_to_v0)
#       -> 校准(_calibrate_user_version) -> 版本迁移(run_migrations)
#       -> 自检(_self_check_schema)。
# ============================================================================


# 当前最新 schema 版本号 = _MIGRATIONS 注册表长度。新增迁移时只追加注册表项，勿手改此值。
_MIGRATIONS = [
    # (目标版本号, 人类可读描述, 迁移函数)
    # 第 i 项 (version=N, desc, fn) 表示「把库从 v(N-1) 升到 vN」的迁移；
    # fn 必须自身幂等（开头做列/索引存在性检测，已存在则直接 return）。
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
    (
        3,
        "添加模型映射思考链注入开关：model_mappings 表新增 think_injection 列",
        _migrate_model_mappings_add_think_injection,
    ),
    (
        4,
        "添加模型映射 reasoning_content 字段注入开关：model_mappings 表新增 reasoning_content_field 列",
        _migrate_model_mappings_add_reasoning_content_field,
    ),
    (
        5,
        "添加模型映射原生 Responses 透传开关：model_mappings 表新增 native_responses 列",
        _migrate_model_mappings_add_native_responses,
    ),
    (
        6,
        "model_mappings 新增 disable_reason 列以区分手动禁用与 provider 联动禁用",
        _migrate_model_mappings_add_disable_reason,
    ),
    (
        7,
        "修正 provider 禁用但其下 mapping 仍启用的历史假启用数据",
        _migrate_reconcile_disabled_provider_mappings,
    ),
    (
        8,
        "oauth_tokens.access_token 加命名唯一索引，堵 alg=none JWT 伪造认证绕过（BE-02）",
        _migrate_oauth_tokens_access_token_unique,
    ),
    (
        9,
        "删除 OAuth 相关三张表（oauth_clients/oauth_codes/oauth_tokens），攻击面归零",
        _migrate_drop_oauth_tables,
    ),
]

CURRENT_SCHEMA_VERSION = len(_MIGRATIONS)


def run_migrations(conn):
    """按序执行所有 version > user_version 的迁移，每步成功立即递增并 commit。

    用 PRAGMA user_version 做全局单调版本号，只前向执行；保证崩溃后可断点续跑
    （不会重复执行已完成的步骤）。
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for target_version, desc, migrate_fn in _MIGRATIONS:
        if target_version <= current:
            continue  # 已达到或超过该版本，跳过
        print(f"[migration] 执行 v{target_version - 1} -> v{target_version}: {desc}")
        try:
            migrate_fn(conn)  # 迁移函数内部自管 commit + 幂等 + backup-drop-recreate
        except Exception:
            # 捕获后补充定位信息再上抛，便于管理员从控制台快速识别是哪个迁移步骤失败
            print(f"[migration] 迁移 v{target_version - 1} -> v{target_version} 失败: {desc}")
            raise
        conn.execute(f"PRAGMA user_version = {target_version}")
        conn.commit()
        current = target_version


def _baseline_migrate_to_v0(conn):
    """把任意老库幂等拉到 v0 基线（在版本框架外、按历史依赖顺序调用）。

    含一次性过渡清污：旧版 _migrate_providers_url_to_full_endpoint 曾用
    PRAGMA user_version=1 做「URL 补全」标记，与新框架「v1 = cache_read_price 列」
    语义冲突。过渡时把旧标记迁移到 settings 键 baseline_url_full_endpoint_done='1'，
    重置 user_version=0，交由 _calibrate_user_version 按实际 schema 重定。
    过渡用 settings 键 schema_version_framework_initialized 守卫，只执行一次。
    """
    # ---- 一次性过渡清污（只跑一次）----
    inited_row = conn.execute(
        "SELECT value FROM settings WHERE key = 'schema_version_framework_initialized'"
    ).fetchone()
    if not inited_row:
        # 旧版若 user_version>=1，说明 URL 补全已跑过，迁移该一次性标记到 settings 键
        old_user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if old_user_version >= 1:
            conn.execute(
                "INSERT OR IGNORE INTO settings (key,value) VALUES ('baseline_url_full_endpoint_done','1')"
            )
        # 重置 user_version=0（清除旧语义污染，交给校准按实际 schema 重定）
        conn.execute("PRAGMA user_version = 0")
        # 写入框架初始化标记，后续启动不再走过渡
        conn.execute(
            "INSERT OR REPLACE INTO settings (key,value) VALUES ('schema_version_framework_initialized','1')"
        )
        conn.commit()

    # ---- v0 基线建设：11 个幂等迁移，按历史依赖顺序调用 ----
    # 顺序与原 init_db 一致（仅剔除已移入 _MIGRATIONS 的 v0->v1 项），
    # 保留所有已声明的依赖关系（如 full_path 依赖 url_to_full_endpoint）。
    _migrate_logs_add_source_model(conn)
    _migrate_logs_add_cache_tokens(conn)
    _migrate_provider_usage_add_cache_tokens(conn)
    _migrate_model_mappings(conn)
    _migrate_error_mappings(conn)
    _migrate_providers_add_disable_reason(conn)
    _migrate_providers_dual_url(conn)
    _migrate_providers_url_to_full_endpoint(conn)   # 依赖 dual_url；改读 settings 键做一次性守卫
    _migrate_providers_add_full_path(conn)          # 依赖 url_to_full_endpoint
    _drop_legacy_mcp_image_config(conn)
    _migrate_degradation_settings(conn)


def _calibrate_user_version(conn):
    """反射实际 schema，仅前向校准 user_version（只升不降）。

    现有库上一轮已加 cache_read_price_per_million 列（实际已是 v1 实态），但旧代码把
    user_version 设成了 1（URL 补全旧含义）。过渡清污已重置为 0，此处按实际列存在性
    重新校准到正确版本，确保 run_migrations 不会误判重跑或漏跑。

    设计说明（只升不降是有意为之）：降版本会触发 backup-drop-recreate 重跑迁移函数，
    可能误删数据；因此 detected <= current 时绝不降版本。若某版本应有的列缺失
    （如外部脚本误删列），由 _self_check_schema 兜底报错（fail-closed），错误信息会
    精确指出缺失的列与疑似遗漏的迁移。

    特征检测范式（新增版本时在此追加对应检测）：
      - 加列类迁移：用 PRAGMA table_info 检测标志性列是否存在（如本函数对 v1 的检测）。
      - 新增表类迁移：用 SELECT name FROM sqlite_master WHERE type='table' AND name='新表名'
        检测表是否存在。注意：PRAGMA table_info 对不存在的表返回空列表，无法区分
        『表不存在』与『表存在但无该列』，因此新增表迁移务必用 sqlite_master 而非
        table_info 做特征检测。

    注意：未来新增 v2/v3 等版本时，特征检测必须按版本号升序排列（先检 v1 再检 v2 再检
    v3），且每条 if 分支必须用赋值语句设置 detected 为该版本号（如 `detected = 2`），不能
    写成仅布尔判断；否则在 v1+v2 特征同时存在时被低版本分支覆盖，导致校准错位。当前 v1
    检测已正确书写，新增版本时务必保持此约定。
    """
    detected = 0
    cols = [r[1] for r in conn.execute("PRAGMA table_info(provider_billing_config)").fetchall()]
    if "cache_read_price_per_million" in cols:
        detected = 1  # v1 标志列存在；后续每加一个版本，在此追加一个特征列/索引检测
    cols_mm = [r[1] for r in conn.execute("PRAGMA table_info(model_mappings)").fetchall()]
    if "reasoning_effort_supported" in cols_mm:
        detected = 2  # v2 标志列存在；v2 隐含 v1，故无需再判 v1
    if "think_injection" in cols_mm:
        detected = 3  # v3 标志列存在；v3 隐含 v2/v1，故无需再判前置版本
    if "reasoning_content_field" in cols_mm:
        detected = 4  # v4 标志列存在；v4 隐含 v3/v2/v1
    if "native_responses" in cols_mm:
        detected = 5  # v5 标志列存在；v5 隐含 v4/v3/v2/v1
    if "disable_reason" in cols_mm:
        detected = 6  # v6 标志列存在；v6 隐含 v5/v4/v3/v2/v1
    # v7 是纯数据迁移（无列/索引/表结构变更），特征痕迹只有 settings 标记键。
    # 用 sqlite_master 查 settings 不适用，settings 是普通表，直接 SELECT 即可。
    v7_done = conn.execute(
        "SELECT value FROM settings WHERE key = 'migrate_reconcile_disabled_provider_mappings_done'"
    ).fetchone()
    if v7_done:
        detected = 7  # v7 标记键存在；v7 隐含 v6/v5/v4/v3/v2/v1
    # v8 标志：oauth_tokens.access_token 命名唯一索引（查 sqlite_master，不用 PRAGMA table_info）
    v8_idx = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_oauth_tokens_access_token'"
    ).fetchone()
    if v8_idx:
        detected = 8  # v8 索引存在；v8 隐含 v7/v6/v5/v4/v3/v2/v1
    # v9 标志：oauth_tokens 表已不存在（删表类迁移，用 sqlite_master 检测表存在性）
    v9_gone = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='oauth_tokens'"
    ).fetchone()
    if not v9_gone:
        detected = 9  # oauth_tokens 表已删除；v9 隐含 v8/v7/v6/v5/v4/v3/v2/v1
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if detected > current:
        conn.execute(f"PRAGMA user_version = {detected}")
        conn.commit()


def _self_check_schema(conn):
    """启动时 schema 自检兜底：把「忘了写迁移」从静默故障变成启动即失败。

    在一个临时内存库上执行 _create_latest_schema，用 PRAGMA table_info/index_list
    反射出期望结构，再与 proxy.db 实际结构逐表逐列/逐索引对比。期望 schema 永远跟
    CREATE TABLE 同源，零漂移（不手写第二份 schema 清单）。
    覆盖「列新增/删除、索引新增」两类最易遗漏的变更。
    """
    tmp = sqlite3.connect(":memory:")
    try:
        _create_latest_schema(tmp)
        expected_tables = [r[0] for r in tmp.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        errors = []
        for tbl in expected_tables:
            exp_cols = [r[1] for r in tmp.execute(f"PRAGMA table_info({tbl})").fetchall()]
            act_cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()]
            missing = [c for c in exp_cols if c not in act_cols]
            if missing:
                errors.append(
                    f"表 {tbl} 缺少列 {missing}（疑似遗漏版本迁移：改了 CREATE TABLE 但未注册 _MIGRATIONS）"
                )
            # 索引检测：排除 sqlite_autoindex_*（PRIMARY KEY/UNIQUE 约束自动产生的索引）。
            # 原因：_create_latest_schema 的 CREATE TABLE IF NOT EXISTS 不会给老表补 UNIQUE 约束，
            # 老库若最初无 UNIQUE 约束，内存库（新 schema）有 sqlite_autoindex_*、老库没有，
            # 笼统对比会把自动索引缺失误报为「遗漏迁移」并硬阻塞启动（回归）。
            # 排除后自检只对显式索引（CREATE INDEX 产生）做对比，保留对显式索引遗漏的检测能力，
            # UNIQUE/PRIMARY KEY 约束差异不再误报。
            exp_idx = [r[1] for r in tmp.execute(f"PRAGMA index_list({tbl})").fetchall()
                       if not r[1].startswith("sqlite_autoindex_")]
            act_idx = [r[1] for r in conn.execute(f"PRAGMA index_list({tbl})").fetchall()
                       if not r[1].startswith("sqlite_autoindex_")]
            missing_idx = [i for i in exp_idx if i not in act_idx]
            if missing_idx:
                errors.append(f"表 {tbl} 缺少索引 {missing_idx}（疑似遗漏迁移）")
        if errors:
            raise RuntimeError("Schema 自检失败，检测到遗漏迁移：\n" + "\n".join(errors))
    finally:
        tmp.close()


def init_db():
    """初始化数据库：建表、基线迁移、版本迁移、自检、同步管理员账户。

    流程（版本号线性迁移框架）：
      (a) 全新库检测（CREATE 之前查 providers/logs/settings 是否已存在）
      (b) CREATE TABLE IF NOT EXISTS 用最新 schema（_create_latest_schema）
      (c) 全新库：schema 已是最新，直接设 user_version=CURRENT_SCHEMA_VERSION，跳过基线与迁移
      (d) 老库：基线建设（拉到 v0）-> 校准 user_version -> 版本迁移 run_migrations
      (e) 启动自检兜底（反射对比期望 schema）
      (f) 管理员账户同步
    """
    conn = get_conn()
    try:
        # (a) 全新库检测（CREATE 之前查核心表是否已存在）
        existing = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('providers','logs','settings')"
        ).fetchall()]
        is_fresh = (len(existing) == 0)

        # (b) CREATE TABLE IF NOT EXISTS 用最新 schema
        _create_latest_schema(conn)

        if is_fresh:
            # (c) 全新库：schema 已是最新，直接设版本号，跳过基线与迁移。
            # 全新库的 providers 表由 _create_latest_schema 直接用 full_path=1 的完整端点
            # 列结构建出，用户后续通过表单填入的就是最终端点（add_provider 默认 full_path=1），
            # 不存在「历史 base 路径待补全」的场景，因此必须把所有 baseline 一次性 settings
            # 标记一并写入，防止二次启动走老库分支时误跑 baseline 迁移篡改用户配置的 URL。
            conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
            conn.execute(
                "INSERT OR REPLACE INTO settings (key,value) VALUES ('schema_version_framework_initialized','1')"
            )
            # baseline_url_full_endpoint_done：标记 URL 补全已 done，永不重跑
            # _migrate_providers_url_to_full_endpoint。否则全新安装后配置了非标准自定义端点
            # （URL 不以 /v1/messages 或 /chat/completions 结尾）的 provider，首次重启走老库
            # 分支时会因该键缺失而执行迁移，其 endswith 幂等检查只保护已带标准后缀的 URL，
            # 对自定义端点无保护，会错误追加后缀，永久篡改用户配置的 URL，导致转发失败。
            conn.execute(
                "INSERT OR REPLACE INTO settings (key,value) VALUES ('baseline_url_full_endpoint_done','1')"
            )
            # v7 数据修正迁移标记：与 is_fresh 分支同步补写，保持与老库分支
            # _calibrate_user_version 的特征检测一致。全新库无 provider 禁用的
            # 历史不一致数据，v7 UPDATE 在全新库上不会命中任何行，标记写在这里仅
            # 是为了让 _calibrate_user_version 在二次启动时直接识别为 v7，避免
            # 因标记缺失误判为 v6（虽不影响 run_migrations 行为，但保持版本号校准一致）。
            conn.execute(
                "INSERT OR REPLACE INTO settings (key,value) "
                "VALUES ('migrate_reconcile_disabled_provider_mappings_done','1')"
            )
            conn.commit()
        else:
            # (d) 老库：基线建设（拉到 v0）-> 校准 -> 版本迁移
            _baseline_migrate_to_v0(conn)
            _calibrate_user_version(conn)
            run_migrations(conn)

        # (e) 启动自检兜底（反射对比期望 schema）
        _self_check_schema(conn)

        # (f) 管理员账户同步
        _sync_admin_user(conn)
    finally:
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


def _cascade_provider_enabled_to_mappings(conn, provider_id, new_enabled):
    """级联同步 provider 的 enabled 状态到其下所有 model_mappings，区分联动禁用与手动禁用。

    传入的 conn 必须与 provider 的 UPDATE 在同一连接/事务内（保证原子性）。
    本函数不读取 provider 当前 enabled（时序敏感：调用方可能在 UPDATE providers 之前或
    之后调用），是否真的需要级联（新旧 enabled 是否不同）由调用方负责判断：
      - update_provider 在 UPDATE 之前比较新旧值，仅变化时调用
      - reset_expired_windows_and_reenable 查的是 enabled=0 的 provider 并要改成 1，
        本身保证有变化，无需额外判断

    语义：
    - new_enabled=0（禁用 provider）：把该 provider 下所有 enabled=1 的 mapping 改为
      enabled=0 且 disable_reason='provider_disabled'；不动已经禁用的 mapping（保留其
      原 disable_reason，如 'manual'），避免覆盖用户手动禁用的标记。
    - new_enabled=1（启用 provider）：只恢复 disable_reason='provider_disabled' 的 mapping
      为 enabled=1 且 disable_reason=''；不动 disable_reason='manual' 的 mapping，
      保留用户的手动禁用状态。
    """
    if int(new_enabled) == 0:
        conn.execute(
            "UPDATE model_mappings SET enabled = 0, disable_reason = 'provider_disabled' "
            "WHERE provider_id = ? AND enabled = 1",
            (provider_id,),
        )
    else:
        conn.execute(
            "UPDATE model_mappings SET enabled = 1, disable_reason = '' "
            "WHERE provider_id = ? AND disable_reason = 'provider_disabled'",
            (provider_id,),
        )


def update_provider(provider_id, **kwargs):
    allowed = {"name", "anthropic_url", "openai_url", "api_key", "enabled", "max_concurrency", "disable_reason", "full_path"}
    fields = []
    values = []
    for k, v in kwargs.items():
        if k in allowed:
            fields.append(f"{k} = ?")
            values.append(int(v) if k in ("enabled", "max_concurrency", "full_path") else v)
    # 手动启用时，清除 disable_reason；手动禁用时，标记为 manual。
    # 若调用方显式传了 disable_reason（即使是空串），则尊重调用方值，不自动覆盖。
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
    # 级联更新：仅当 enabled 真正变化时，同步该 provider 下所有 model_mappings。
    # 必须在 UPDATE providers 之前调用——此时 provider 的 enabled 仍是旧值，
    # 可据此判断是否真有变化；级联函数本身不做无变化检测（时序敏感）。
    if "enabled" in kwargs:
        new_enabled = int(kwargs["enabled"])
        old = conn.execute("SELECT enabled FROM providers WHERE id = ?", (provider_id,)).fetchone()
        if old and old["enabled"] != new_enabled:
            _cascade_provider_enabled_to_mappings(conn, provider_id, new_enabled)
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
        "SELECT m.*, p.name as provider_name, p.enabled as provider_enabled, p.anthropic_url, p.openai_url, p.api_key "
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


def add_model_mapping(alias, target_model, provider_id, enabled=True, priority=1, model_type="text", max_tokens=0, role_mappings=None, reasoning_effort_supported=1, think_injection=0, reasoning_content_field=1, native_responses=0):
    conn = get_conn()
    c = conn.cursor()
    if role_mappings is None:
        role_mappings = "[]"
    elif isinstance(role_mappings, (list, dict)):
        role_mappings = json.dumps(role_mappings, ensure_ascii=False)
    # 新增 mapping 时，若 enabled=False 则 disable_reason='manual'（用户手动禁用），
    # 否则 disable_reason=''（正常启用态），与 update_model_mapping 的自动管理逻辑对齐。
    disable_reason = "manual" if not enabled else ""
    c.execute(
        "INSERT INTO model_mappings (alias, target_model, provider_id, priority, model_type, max_tokens, enabled, role_mappings, reasoning_effort_supported, think_injection, reasoning_content_field, native_responses, disable_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (alias, target_model, provider_id, int(priority), model_type, int(max_tokens), int(enabled), role_mappings, int(reasoning_effort_supported), int(think_injection), int(reasoning_content_field), int(native_responses), disable_reason),
    )
    conn.commit()
    mapping_id = c.lastrowid
    conn.close()
    return mapping_id


def update_model_mapping(mapping_id, **kwargs):
    allowed = {"alias", "target_model", "provider_id", "enabled", "priority", "model_type", "max_tokens", "role_mappings", "reasoning_effort_supported", "think_injection", "reasoning_content_field", "native_responses", "disable_reason"}
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
                values.append(int(v) if k in ("enabled", "priority", "max_tokens", "reasoning_effort_supported", "think_injection", "reasoning_content_field", "native_responses") else v)
    # 手动启停 mapping 时自动管理 disable_reason（与 providers 表设计对齐）：
    #   手动禁用（enabled=0）且未显式指定 reason → 标记 'manual'，
    #     provider 联动启用时只恢复 'provider_disabled' 的 mapping，保留 'manual' 的禁用状态。
    #   手动启用（enabled=1）→ 清空 disable_reason（无论原值，恢复正常态）。
    # 前端 toggleModel 只需传 enabled，后端自动维护联动语义所需的标记。
    if "enabled" in kwargs:
        if int(kwargs["enabled"]):
            if "disable_reason" not in kwargs:
                fields.append("disable_reason = ?")
                values.append("")
        else:
            if "disable_reason" not in kwargs:
                fields.append("disable_reason = ?")
                values.append("manual")
    if not fields:
        return
    values.append(mapping_id)
    conn = get_conn()
    # 防御性检查：所属 provider 被禁用时，禁止开启 mapping。
    # 与前端 toggle 锁定语义一致——provider 禁用其下 mapping 一律不能为启用态，
    # 否则会出现「toggle 显示启用但路由被 p.enabled=1 过滤、实际无效」的假启用。
    # 禁用（enabled=0）或其他字段更新不受限制。
    if "enabled" in kwargs and int(kwargs["enabled"]):
        prov = conn.execute(
            "SELECT p.enabled, p.name FROM model_mappings m "
            "JOIN providers p ON m.provider_id = p.id WHERE m.id = ?",
            (mapping_id,),
        ).fetchone()
        if prov and int(prov["enabled"]) == 0:
            conn.close()
            raise ValueError(
                f"该映射模型所属提供商「{prov['name']}」已禁用，无法开启，请先启用提供商"
            )
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


def get_all_settings(mask_secrets=False):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM settings").fetchall()
    conn.close()
    result = {r["key"]: r["value"] for r in rows}
    return result


def get_degradation_config():
    """获取服务降级配置。

    返回 dict：
      - enabled: bool，是否启用降级（默认 False，需在 UI 主动打开）。
      - duration: int，单次降级持续秒数（默认 30，UI 可改）。
      - strict_priority: bool，是否启用严格优先级（逐级下放）模式（默认 False）。开启后仅在当前最高优先级层内选择候选，整层降级才下放到更低优先级层；依赖主降级开关 enabled=True 方可生效。

    降级语义：单次转发请求在 _post_with_retry 内部重试 3 次仍失败，
    即按具体 model_mapping（provider+目标模型）粒度标记降级，在 duration 秒内
    该候选不再被选中（除非全部候选都降级，则回退到原有加权轮询）。
    任意一次成功立即清除该候选的降级状态。
    """
    enabled_raw = get_setting("degradation_enabled", "0")
    duration_raw = get_setting("degradation_duration", "30")
    strict_raw = get_setting("degradation_strict_priority", "0")
    try:
        duration = int(duration_raw)
    except (ValueError, TypeError):
        duration = 30
    if duration <= 0:
        duration = 30
    return {
        "enabled": enabled_raw not in ("0", "false", "False", ""),
        "duration": duration,
        "strict_priority": strict_raw not in ("0", "false", "False", ""),
    }



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
                        cache_read_price_per_million=0, expiration_date=None, warning_threshold=0.8):
    """创建或更新 provider 的计费配置"""
    now = datetime.now().isoformat()
    conn = get_conn()
    conn.execute(
        "INSERT INTO provider_billing_config "
        "(provider_id, billing_mode, limit_5h, limit_week, limit_month, balance, "
        "input_price_per_million, output_price_per_million, cache_read_price_per_million, "
        "expiration_date, warning_threshold, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(provider_id) DO UPDATE SET "
        "billing_mode=excluded.billing_mode, limit_5h=excluded.limit_5h, limit_week=excluded.limit_week, "
        "limit_month=excluded.limit_month, balance=excluded.balance, "
        "input_price_per_million=excluded.input_price_per_million, "
        "output_price_per_million=excluded.output_price_per_million, "
        "cache_read_price_per_million=excluded.cache_read_price_per_million, "
        "expiration_date=excluded.expiration_date, warning_threshold=excluded.warning_threshold, "
        "updated_at=excluded.updated_at",
        (provider_id, billing_mode, limit_5h, limit_week, limit_month, balance,
         input_price_per_million, output_price_per_million, cache_read_price_per_million,
         expiration_date, warning_threshold, now, now),
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
    # Anthropic 缓存定价：cache_creation 按全额 input 价格，cache_read 按配置的缓存命中价格（默认 0.1x input）
    if billing_mode == "balance":
        input_cost = ((input_tokens + cache_creation_input_tokens) / 1_000_000) * config["input_price_per_million"]
        cache_read_price = config.get("cache_read_price_per_million", 0) or (config["input_price_per_million"] * 0.1)
        cache_read_cost = (cache_read_input_tokens / 1_000_000) * cache_read_price
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
            # 级联启用该 provider 下所有 model_mappings
            _cascade_provider_enabled_to_mappings(conn, provider_id, 1)
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


def _migrate_degradation_settings(conn):
    """清理旧版降级配置的残留键，确保降级配置符合设计契约。

    旧版实现曾引入 degradation_failure_threshold 跨请求累积失败计数，
    但最终设计不需要该字段（_post_with_retry 内部已做 3 次重试，出来即为最终失败）。
    同时修正默认值：enabled 默认关闭（'0'），duration 默认 30 秒。
    """
    # 删除不再使用的键
    conn.execute("DELETE FROM settings WHERE key='degradation_failure_threshold'")
    # 确保默认值存在（INSERT OR IGNORE 不覆盖已有值，避免覆盖用户自定义设置）
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('degradation_enabled', '0')")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('degradation_duration', '30')")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('degradation_strict_priority', '0')")
    conn.commit()
