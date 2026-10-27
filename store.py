"""SQLite 存储层。

改进点：
- 自动迁移旧库（原来那版表结构缺字段，直接 ALTER，已有数据不丢）；
- 用 UPSERT 代替 INSERT OR IGNORE：重跑时能刷新正文/已读状态，但不会覆盖人工改过的分类；
- 记录每封邮件的失败次数，避免一封坏邮件每次运行都卡住；
- 用 sync_state 表保存 historyId，支持增量同步。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import config

logger = logging.getLogger("aide.store")

SCHEMA_VERSION = 2

_CREATE_EMAILS = """
CREATE TABLE IF NOT EXISTS emails (
    id              TEXT PRIMARY KEY,
    thread_id       TEXT,
    subject         TEXT,
    from_email      TEXT,
    from_raw        TEXT,
    to_field        TEXT,
    date            TEXT,
    snippet         TEXT,
    body            TEXT,
    web_link        TEXT,
    label_ids       TEXT,
    is_unread       INTEGER DEFAULT 0,
    is_important    INTEGER DEFAULT 0,
    has_attachment  INTEGER DEFAULT 0,
    size            INTEGER DEFAULT 0,
    priority        TEXT,
    reason          TEXT,
    labeled         INTEGER DEFAULT 0,
    error           TEXT,
    error_count     INTEGER DEFAULT 0,
    processed_at    TEXT,
    updated_at      TEXT
)
"""

_CREATE_STATE = """
CREATE TABLE IF NOT EXISTS sync_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
)
"""

# 老库缺的列：列名 -> DDL
_MIGRATIONS = {
    "has_attachment": "INTEGER DEFAULT 0",
    "size": "INTEGER DEFAULT 0",
    "error": "TEXT",
    "error_count": "INTEGER DEFAULT 0",
    "updated_at": "TEXT",
}

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_emails_priority ON emails(priority)",
    "CREATE INDEX IF NOT EXISTS idx_emails_date ON emails(date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_emails_pending ON emails(priority, error_count)",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path else config.DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(path: Path | str | None = None) -> sqlite3.Connection:
    conn = connect(path)

    with conn:  # 事务：建表 + 迁移要么全成要么全败
        conn.execute(_CREATE_EMAILS)
        conn.execute(_CREATE_STATE)

        existing = _columns(conn, "emails")
        for column, ddl in _MIGRATIONS.items():
            if column not in existing:
                logger.info("Migrating database: adding column emails.%s", column)
                conn.execute(f"ALTER TABLE emails ADD COLUMN {column} {ddl}")

        for statement in _INDEXES:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    return conn


_UPSERT = """
INSERT INTO emails (
    id, thread_id, subject, from_email, from_raw, to_field, date, snippet, body,
    web_link, label_ids, is_unread, is_important, has_attachment, size, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    thread_id      = excluded.thread_id,
    subject        = excluded.subject,
    from_email     = excluded.from_email,
    from_raw       = excluded.from_raw,
    to_field       = excluded.to_field,
    date           = excluded.date,
    snippet        = excluded.snippet,
    -- a failed fetch may send an empty body; keep the stored one in that case
    body           = CASE WHEN excluded.body <> '' THEN excluded.body ELSE emails.body END,
    web_link       = excluded.web_link,
    label_ids      = excluded.label_ids,
    is_unread      = excluded.is_unread,
    is_important   = excluded.is_important,
    has_attachment = excluded.has_attachment,
    size           = excluded.size,
    updated_at     = excluded.updated_at
"""


def save_emails(conn: sqlite3.Connection, emails: list[dict]) -> int:
    """写入或更新邮件，返回变更行数。分类字段（priority/reason）不会被覆盖。"""
    if not emails:
        return 0

    stamp = _now()
    rows = [
        (
            e["id"],
            e.get("thread_id"),
            e.get("subject"),
            e.get("from_email"),
            e.get("from_raw"),
            e.get("to"),
            e.get("date"),
            e.get("snippet"),
            (e.get("body") or "")[: config.BODY_STORE_LIMIT],
            e.get("web_link"),
            ",".join(e.get("label_ids", []) or []),
            int(bool(e.get("is_unread"))),
            int(bool(e.get("is_important"))),
            int(bool(e.get("has_attachment"))),
            int(e.get("size") or 0),
            stamp,
        )
        for e in emails
        if e.get("id")
    ]
    before = conn.total_changes
    with conn:
        conn.executemany(_UPSERT, rows)
    return conn.total_changes - before


def update_classification(
    conn: sqlite3.Connection,
    email_id: str,
    priority: str | None,
    reason: str | None,
    labeled: bool,
) -> None:
    """记录分类结果；成功后清空错误状态。"""
    with conn:
        conn.execute(
            """
            UPDATE emails
               SET priority = ?, reason = COALESCE(?, reason), labeled = ?, error = NULL,
                   error_count = 0, processed_at = ?, updated_at = ?
             WHERE id = ?
            """,
            (priority, reason, int(bool(labeled)), _now(), _now(), email_id),
        )


def mark_failed(conn: sqlite3.Connection, email_id: str, error: str) -> None:
    """记录一次失败；error_count 累加，达到上限后不再重试。"""
    with conn:
        conn.execute(
            """
            UPDATE emails
               SET error = ?, error_count = COALESCE(error_count, 0) + 1, updated_at = ?
             WHERE id = ?
            """,
            (error[:1000], _now(), email_id),
        )


def get_unprocessed(conn: sqlite3.Connection, limit: int = 30, max_attempts: int = 3) -> list[dict]:
    """待分类邮件：还没有优先级的，或之前失败但没超过重试上限的。"""
    rows = conn.execute(
        """
        SELECT id, subject, from_email, from_raw, snippet, body, date,
               has_attachment, is_unread
          FROM emails
         WHERE priority IS NULL
           AND COALESCE(error_count, 0) < ?
         ORDER BY date DESC
         LIMIT ?
        """,
        (max_attempts, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def get_unlabeled(conn: sqlite3.Connection, limit: int = 100, max_attempts: int = 3) -> list[dict]:
    """已判定优先级但上次打标签失败的邮件：重试时直接补标签，不用再花钱问模型。"""
    rows = conn.execute(
        """
        SELECT id, subject, priority
          FROM emails
         WHERE priority IS NOT NULL
           AND COALESCE(labeled, 0) = 0
           AND COALESCE(error_count, 0) < ?
         ORDER BY date DESC
         LIMIT ?
        """,
        (max_attempts, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, str(value), _now()),
        )


def reset_classifications(conn: sqlite3.Connection) -> int:
    """清空全部分类结果，下次运行会重新判定（用于改过 rubric 之后重跑）。"""
    with conn:
        cursor = conn.execute(
            """
            UPDATE emails
               SET priority = NULL, reason = NULL, labeled = 0, error = NULL,
                   error_count = 0, processed_at = NULL
            """
        )
    return cursor.rowcount


def stats(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    pending = conn.execute(
        "SELECT COUNT(*) FROM emails WHERE priority IS NULL AND COALESCE(error_count, 0) < 3"
    ).fetchone()[0]
    failed = conn.execute("SELECT COUNT(*) FROM emails WHERE error IS NOT NULL AND priority IS NULL").fetchone()[0]

    by_priority = {
        row["priority"]: row["n"]
        for row in conn.execute(
            "SELECT priority, COUNT(*) AS n FROM emails WHERE priority IS NOT NULL GROUP BY priority"
        )
    }
    return {"total": total, "pending": pending, "failed": failed, "by_priority": by_priority}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    connection = init_db()
    print("Database:", config.DB_PATH)
    print("Stats:", stats(connection))
