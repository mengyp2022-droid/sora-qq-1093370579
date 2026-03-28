import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from app.config import settings

DB_PATH = os.path.join(settings.data_dir, "admin.db")


def ensure_data_dir():
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)


def get_conn():
    ensure_data_dir()
    return sqlite3.connect(DB_PATH, check_same_thread=False)


@contextmanager
def get_db():
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    ensure_data_dir()
    with get_db() as conn:
        c = conn.cursor()
        # 系统设置（key-value）
        c.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # 账号表（注册结果）
        c.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                password TEXT,
                status TEXT,
                registered_at TEXT,
                has_sora INTEGER DEFAULT 0,
                has_plus INTEGER DEFAULT 0,
                phone_bound INTEGER DEFAULT 0,
                proxy TEXT,
                refresh_token TEXT,
                access_token TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_accounts_email ON accounts(email)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status)")
        try:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_email_unique ON accounts(LOWER(TRIM(email)))")
        except Exception:
            pass
        # 邮箱管理
        c.execute("""
            CREATE TABLE IF NOT EXISTS emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                password TEXT,
                uuid TEXT,
                token TEXT,
                remark TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # 手机号管理（activation_id 为 Hero-SMS 激活 ID，用于拉取验证码状态）
        c.execute("""
            CREATE TABLE IF NOT EXISTS phone_numbers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                activation_id INTEGER,
                max_use_count INTEGER DEFAULT 1,
                used_count INTEGER DEFAULT 0,
                remark TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_phone_numbers_phone ON phone_numbers(phone)")
        # 银行卡管理
        c.execute("""
            CREATE TABLE IF NOT EXISTS bank_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_number_masked TEXT,
                card_data TEXT,
                max_use_count INTEGER DEFAULT 1,
                used_count INTEGER DEFAULT 0,
                remark TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # 运行日志（按任务或按条）
        c.execute("""
            CREATE TABLE IF NOT EXISTS run_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                level TEXT,
                message TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_run_logs_task_id ON run_logs(task_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_run_logs_created ON run_logs(created_at)")
        # 管理员密码（可覆盖 config 的初始密码，存 bcrypt hash）
        c.execute("""
            CREATE TABLE IF NOT EXISTS admin_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT,
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Sora 调用 API Key（仅保存 hash，明文只在创建时返回一次）
        c.execute("""
            CREATE TABLE IF NOT EXISTS sora_api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                name TEXT,
                key_hash TEXT NOT NULL UNIQUE,
                key_prefix TEXT,
                key_mask TEXT,
                created_by TEXT,
                scope TEXT DEFAULT 'text_to_video',
                is_active INTEGER DEFAULT 1,
                last_used_at TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_sora_api_keys_account_id ON sora_api_keys(account_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sora_api_keys_active ON sora_api_keys(is_active)")
        # Sora 视频任务归属（池模式下让 task_id 跟随创建它的账号）
        c.execute("""
            CREATE TABLE IF NOT EXISTS sora_video_tasks (
                task_id TEXT PRIMARY KEY,
                account_id INTEGER NOT NULL,
                api_key_id INTEGER,
                task_family TEXT DEFAULT 'video_gen',
                raw_status TEXT,
                normalized_status TEXT,
                is_active INTEGER DEFAULT 1,
                lease_expires_at TEXT,
                succeeded_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_sora_video_tasks_account_id ON sora_video_tasks(account_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sora_video_tasks_api_key_id ON sora_video_tasks(api_key_id)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS sora_media_assets (
                media_id TEXT PRIMARY KEY,
                account_id INTEGER NOT NULL,
                api_key_id INTEGER,
                media_type TEXT,
                filename TEXT,
                mime_type TEXT,
                width INTEGER,
                height INTEGER,
                source_url TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_sora_media_assets_account_id ON sora_media_assets(account_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sora_media_assets_api_key_id ON sora_media_assets(api_key_id)")
        try:
            c.execute("ALTER TABLE phone_numbers ADD COLUMN activation_id INTEGER")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE phone_numbers ADD COLUMN expired_at TEXT")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE accounts ADD COLUMN access_token TEXT")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE accounts ADD COLUMN sora_enabled INTEGER DEFAULT 1")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE accounts ADD COLUMN sora_quota_exhausted INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE accounts ADD COLUMN sora_quota_note TEXT")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE accounts ADD COLUMN sora_quota_updated_at TEXT")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE accounts ADD COLUMN sora_last_error TEXT")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE sora_api_keys ADD COLUMN scope TEXT DEFAULT 'text_to_video'")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE sora_video_tasks ADD COLUMN task_family TEXT DEFAULT 'video_gen'")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE sora_video_tasks ADD COLUMN raw_status TEXT")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE sora_video_tasks ADD COLUMN normalized_status TEXT")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE sora_video_tasks ADD COLUMN is_active INTEGER DEFAULT 1")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE sora_video_tasks ADD COLUMN lease_expires_at TEXT")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE sora_video_tasks ADD COLUMN succeeded_at TEXT")
        except Exception:
            pass
        try:
            c.execute("UPDATE accounts SET sora_enabled = 1 WHERE sora_enabled IS NULL")
            c.execute("UPDATE accounts SET sora_quota_exhausted = 0 WHERE sora_quota_exhausted IS NULL")
            c.execute("UPDATE sora_api_keys SET scope = 'text_to_video' WHERE COALESCE(scope, '') = ''")
        except Exception:
            pass
        try:
            c.execute("UPDATE sora_video_tasks SET task_family = 'video_gen' WHERE COALESCE(task_family, '') = ''")
            c.execute("UPDATE sora_video_tasks SET is_active = 1 WHERE is_active IS NULL")
            c.execute(
                "UPDATE sora_video_tasks SET succeeded_at = updated_at "
                "WHERE normalized_status = 'succeeded' AND COALESCE(succeeded_at, '') = ''"
            )
        except Exception:
            pass
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_sora_api_keys_scope ON sora_api_keys(scope)")
        except Exception:
            pass
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_sora_video_tasks_active ON sora_video_tasks(is_active)")
        except Exception:
            pass
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_sora_video_tasks_lease_expires_at ON sora_video_tasks(lease_expires_at)")
        except Exception:
            pass
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_sora_video_tasks_succeeded_at ON sora_video_tasks(succeeded_at)")
        except Exception:
            pass
        # 插入默认设置键
        defaults = [
            "sms_api_url", "sms_api_key", "thread_count", "proxy_url", "proxy_api_url",
            "bank_card_api_url", "bank_card_api_key", "email_api_url", "email_api_key",
            "card_use_limit", "phone_bind_limit",
            "oauth_client_id", "oauth_redirect_uri",
            "sora_daily_video_quota_per_account",
            "sora_auto_rotate_cursor",
        ]
        for key in defaults:
            c.execute(
                "INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?)",
                (key, "")
            )
        c.execute(
            "INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?)",
            ("thread_count", "1")
        )
        c.execute(
            "INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?)",
            ("card_use_limit", "1")
        )
        c.execute(
            "INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?)",
            ("phone_bind_limit", "1")
        )
        # 首次运行：插入默认管理员 admin / admin123
        c.execute("SELECT COUNT(*) FROM admin_users")
        if c.fetchone()[0] == 0:
            from app.security import get_password_hash
            _hash = get_password_hash("admin123")
            c.execute(
                "INSERT INTO admin_users (username, password_hash) VALUES (?, ?)",
                ("admin", _hash)
            )
        # 账号表为空时插入测试数据（便于查看列表效果）
        c.execute("SELECT COUNT(*) FROM accounts")
        if c.fetchone()[0] == 0:
            from datetime import datetime, timedelta
            now = datetime.utcnow()
            test_rows = [
                ("user1@temp-mail.test", "Pass123!a", "Registered+Sora", (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M"), 1, 0, 0, "http://127.0.0.1:7890", "rt_xxx1"),
                ("user2@temp-mail.test", "Pass123!b", "Registered", (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M"), 0, 0, 0, None, "rt_xxx2"),
                ("user3@temp-mail.test", "Pass123!c", "Plus activated", (now - timedelta(hours=12)).strftime("%Y-%m-%d %H:%M"), 1, 1, 0, "http://127.0.0.1:7891", "rt_xxx3"),
                ("user4@temp-mail.test", "Pass123!d", "Registered+Sora", (now - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M"), 1, 0, 1, None, None),
                ("user5@temp-mail.test", "Pass123!e", "Finish setup (check email)", (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"), 0, 0, 0, None, None),
                ("alice.demo@example.com", "Demo#456", "Registered+Sora", now.strftime("%Y-%m-%d %H:%M"), 1, 1, 1, "socks5://proxy:1080", "rt_xxx6"),
            ]
            for email, pwd, status, reg_at, sora, plus, phone, proxy, rt in test_rows:
                c.execute(
                    """INSERT INTO accounts (email, password, status, registered_at, has_sora, has_plus, phone_bound, proxy, refresh_token, access_token)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (email, pwd, status, reg_at, sora, plus, phone, proxy or None, rt or None, None)
                )
