"""Re-derive routing fields for mail that is already captured, from the stored .eml files.

Use after changing routing rules (config.yaml sender_overrides, category_feeds.csv) or the
source_key / is_issue logic. Nothing is downloaded and no files move: .eml paths stay as they
are; only the DB columns segment, source_key and is_issue are updated (and the same columns
on already-split items).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import db
from .config import Config
from .parse_email import build_row


@dataclass
class ReindexResult:
    scanned: int = 0
    changed: int = 0
    missing_file: int = 0
    moves: dict[str, int] = field(default_factory=dict)     # "old -> new" segment counts


def run_reindex(cfg: Config, dry_run: bool = False) -> ReindexResult:
    conn = db.connect(cfg.db_path)
    res = ReindexResult()
    rows = conn.execute(
        "SELECT gmail_id, segment, source_key, is_issue, raw_eml_path FROM messages "
        "WHERE raw_eml_path IS NOT NULL").fetchall()
    try:
        for r in rows:
            res.scanned += 1
            path = cfg.archive_dir / r["raw_eml_path"]
            if not path.exists():
                res.missing_file += 1
                continue
            new = build_row(path.read_bytes(), r["gmail_id"], inbox_address=cfg.inbox_address,
                            tag_to_segment=cfg.tag_to_segment, sender_overrides=cfg.sender_overrides)
            if (new["segment"], new["source_key"], new["is_issue"]) == (r["segment"], r["source_key"], r["is_issue"]):
                continue
            res.changed += 1
            if new["segment"] != r["segment"]:
                k = f"{r['segment']} -> {new['segment']}"
                res.moves[k] = res.moves.get(k, 0) + 1
            if dry_run:
                continue
            with conn:
                conn.execute("UPDATE messages SET segment = ?, source_key = ?, is_issue = ? WHERE gmail_id = ?",
                             (new["segment"], new["source_key"], new["is_issue"], r["gmail_id"]))
                conn.execute("UPDATE items SET segment = ?, source_key = ? WHERE gmail_id = ?",
                             (new["segment"], new["source_key"], r["gmail_id"]))
    finally:
        conn.close()
    return res
