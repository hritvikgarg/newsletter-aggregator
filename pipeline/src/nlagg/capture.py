"""M1 — Capture: mirror every newsletter from Gmail into .eml files + `messages` rows.

Guarantees:
- Idempotent on gmail_id: re-running never duplicates; anything missed is picked up next run.
- File first, then DB row: a crash can leave an orphan .eml (harmless, overwritten next run)
  but never a DB row pointing at a missing file.
- No AI anywhere in this stage.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import db
from .config import Config
from .fetchers import Backend
from .parse_email import build_row, slugify

log = logging.getLogger("nlagg.capture")


@dataclass
class CaptureResult:
    scanned: int = 0
    new_added: int = 0
    skipped_existing: int = 0
    ignored_sender: int = 0
    failed: int = 0
    by_segment: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def eml_relpath(row: dict) -> Path:
    day = (row.get("sent_date") or row.get("received_date") or "unknown")[:10]
    name = f"{slugify(row.get('sender_name') or row.get('sender_email'))}__{row['gmail_id']}.eml"
    return Path(row["segment"]) / day / name


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def resolve_since(conn, cfg: Config, backend: str, backfill: bool, since: date | None) -> date | None:
    if backfill:
        return None
    if since:
        return since
    last = db.last_successful_sync(conn, backend)
    if not last:
        return None  # first run = full backfill
    return datetime.fromisoformat(last).date() - timedelta(days=cfg.overlap_days)


def run_capture(cfg: Config, backend: Backend, *, backfill: bool = False, since: date | None = None,
                limit: int | None = None, dry_run: bool = False) -> CaptureResult:
    conn = db.connect(cfg.db_path)
    res = CaptureResult()
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    start = resolve_since(conn, cfg, backend.name, backfill, since)
    status, notes = "ok", ""
    try:
        ids = backend.list_ids(start)
        res.scanned = len(ids)
        have = db.known_ids(conn)
        todo = [i for i in ids if i not in have]
        res.skipped_existing = len(ids) - len(todo)
        if limit:
            todo = todo[:limit]
        log.info("since=%s scanned=%d new=%d", start or "beginning", len(ids), len(todo))
        if dry_run:
            res.new_added = len(todo)
            return res

        for f in backend.fetch(todo):
            try:
                row = build_row(
                    f.raw, f.gmail_id,
                    inbox_address=cfg.inbox_address,
                    tag_to_segment=cfg.tag_to_segment,
                    sender_overrides=cfg.sender_overrides,
                    thread_id=f.thread_id, labels=f.labels, snippet=f.snippet,
                    backend=backend.name,
                )
                if row["sender_email"] in cfg.ignore_senders:
                    # Record it (no file) so it is not re-fetched on every run.
                    row.update(segment="ignored", is_issue=0, split_status="skipped")
                    db.insert_message(conn, row)
                    conn.commit()
                    res.ignored_sender += 1
                    continue
                rel = eml_relpath(row)
                _write_atomic(cfg.archive_dir / rel, f.raw)
                row["raw_eml_path"] = rel.as_posix()
                if db.insert_message(conn, row):
                    res.new_added += 1
                    res.by_segment[row["segment"]] = res.by_segment.get(row["segment"], 0) + 1
                conn.commit()
            except Exception as e:  # keep going; failures are logged and retried next run
                res.failed += 1
                res.errors.append(f"{f.gmail_id}: {type(e).__name__}: {e}")
                log.warning("failed %s: %s", f.gmail_id, e)
        if res.failed:
            status, notes = "partial", "; ".join(res.errors[:10])
    except Exception as e:
        status, notes = "error", f"{type(e).__name__}: {e}"
        raise
    finally:
        if not dry_run:
            db.log_sync(
                conn, run_at=run_at, backend=backend.name,
                query=f"since={start}" if start else "full",
                scanned=res.scanned, new_added=res.new_added,
                skipped_existing=res.skipped_existing, enriched=0, failed=res.failed,
                status=status, notes=notes,
            )
        conn.close()
    return res
