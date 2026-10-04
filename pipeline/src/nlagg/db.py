"""SQLite access: schema setup and the few writes capture needs."""
from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 4   # v1 original, v2 items/stories/issues_out, v3 M2 columns, v4 duplicate_of

# Columns added after a table was first created: (table, column, type/default).
# CREATE TABLE IF NOT EXISTS does not touch existing tables, so older DBs get them via ALTER.
ADDED_COLUMNS = [
    ("messages", "is_promo", "INTEGER DEFAULT 0"),
    ("messages", "split_shape", "TEXT"),
    ("messages", "split_error", "TEXT"),
    ("items", "kind", "TEXT DEFAULT 'story'"),
    ("items", "word_count", "INTEGER"),
    ("messages", "duplicate_of", "TEXT"),
]


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    # Upgrade existing tables first, so indexes in schema.sql may reference newer columns.
    for table, col, decl in ADDED_COLUMNS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if have and col not in have:          # empty = table not created yet (schema.sql will)
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    sql = resources.files("nlagg").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def known_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT gmail_id FROM messages")}


MESSAGE_COLUMNS = [
    "gmail_id", "thread_id", "segment", "source_key", "newsletter", "publisher",
    "sender_name", "sender_email", "to_address", "subject", "sent_date", "received_date",
    "is_issue", "labels", "snippet", "list_id", "list_unsubscribe", "sending_platform",
    "has_html", "has_text", "raw_eml_path", "clean_text_path", "raw_html_path",
    "word_count", "reading_minutes", "has_attachments", "content_hash", "capture_backend",
    "ingested_at", "split_status",
]


def insert_message(conn: sqlite3.Connection, row: dict) -> bool:
    """Insert one message; returns False if gmail_id already exists (idempotent)."""
    cols = [c for c in MESSAGE_COLUMNS if c in row]
    sql = (f"INSERT OR IGNORE INTO messages ({', '.join(cols)}) "
           f"VALUES ({', '.join('?' for _ in cols)})")
    cur = conn.execute(sql, [row[c] for c in cols])
    return cur.rowcount == 1


def last_successful_sync(conn: sqlite3.Connection, backend: str) -> str | None:
    r = conn.execute(
        "SELECT run_at FROM sync_log WHERE status = 'ok' AND backend = ? ORDER BY run_at DESC LIMIT 1",
        (backend,),
    ).fetchone()
    return r[0] if r else None


def log_sync(conn: sqlite3.Connection, **kw) -> None:
    cols = list(kw)
    conn.execute(
        f"INSERT INTO sync_log ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [kw[c] for c in cols],
    )
    conn.commit()
