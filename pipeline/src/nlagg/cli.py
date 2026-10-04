"""Command line: `python -m nlagg <command>` (or `nlagg <command>` after pip install).

  init-db                       create/upgrade data/archive/index.db
  capture [--backend api|imap|file] [--backfill] [--since YYYY-MM-DD] [--limit N] [--dry-run] [--from DIR]
  stats [--days N]              per-segment / per-sender counts, last sync runs
  split [--segment S ...|--all-segments] [--redo] [--limit N] [--id GMAIL_ID ...]
                                M2: clean .md + story items for captured issues
  reindex [--dry-run]           re-derive segment/source_key/is_issue from stored .eml (after rule changes)
  split-review [--segment S] [--n 20] [--out FILE]
                                write a hand-check sheet for the M2 >=90% check
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import db
from .capture import run_capture
from .config import load_config


def _backend(name: str, cfg, src: str | None):
    if name == "api":
        from .fetchers import GmailApiBackend
        return GmailApiBackend(cfg)
    if name == "imap":
        from .fetchers import ImapBackend
        return ImapBackend(cfg)
    if name == "file":
        if not src:
            raise SystemExit("--backend file needs --from <folder of .eml files>")
        from .fetchers import EmlDirBackend
        return EmlDirBackend(Path(src))
    raise SystemExit(f"unknown backend {name}")


def cmd_init_db(cfg, _a) -> int:
    db.connect(cfg.db_path).close()
    print(f"DB ready: {cfg.db_path} (schema v{db.SCHEMA_VERSION})")
    return 0


def cmd_capture(cfg, a) -> int:
    backend = _backend(a.backend, cfg, a.src)
    since = date.fromisoformat(a.since) if a.since else None
    try:
        r = run_capture(cfg, backend, backfill=a.backfill, since=since, limit=a.limit, dry_run=a.dry_run)
    finally:
        getattr(backend, "close", lambda: None)()
    verb = "would add" if a.dry_run else "added"
    print(f"scanned={r.scanned} {verb}={r.new_added} already_had={r.skipped_existing} "
          f"ignored={r.ignored_sender} failed={r.failed} duplicates={r.duplicates}")
    for seg, n in sorted(r.by_segment.items()):
        print(f"  {seg:<18} +{n}")
    for e in r.errors[:10]:
        print("  ERROR", e, file=sys.stderr)
    return 1 if r.failed else 0


def cmd_reindex(cfg, a) -> int:
    from .reindex import run_reindex
    r = run_reindex(cfg, dry_run=a.dry_run)
    verb = "would change" if a.dry_run else "changed"
    print(f"scanned={r.scanned} {verb}={r.changed} missing_file={r.missing_file} duplicates={r.duplicates}")
    for k, n in sorted(r.moves.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<36} {n}")
    return 0


def cmd_stats(cfg, a) -> int:
    conn = db.connect(cfg.db_path)
    if a.unmatched:
        print("== unmatched mail by sender (who it is, which address it came to, a sample subject)")
        for r in conn.execute(
            """SELECT sender_name, sender_email, to_address, COUNT(*) n, MAX(subject) sample
               FROM messages WHERE segment = 'unmatched'
               GROUP BY sender_email, sender_name, to_address ORDER BY n DESC"""):
            print(f"  {r['n']:>4}  {r['sender_name']} <{r['sender_email']}>  to={r['to_address']}")
            print(f"        e.g. {(r['sample'] or '')[:90]}")
        conn.close()
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=a.days)).isoformat()
    print(f"== messages per segment (all time | issues in last {a.days}d)")
    for r in conn.execute(
        """SELECT segment, COUNT(*) n,
                  SUM(CASE WHEN is_issue=1 AND sent_date >= ? THEN 1 ELSE 0 END) recent
           FROM messages GROUP BY segment ORDER BY segment""", (cutoff,)):
        print(f"  {r['segment']:<18} {r['n']:>6} | {r['recent'] or 0:>4}")
    print(f"\n== senders with issues in last {a.days}d (source health)")
    for r in conn.execute(
        """SELECT segment, sender_email, COUNT(*) n, MAX(sent_date) last
           FROM messages WHERE is_issue=1 AND sent_date >= ?
           GROUP BY segment, sender_email ORDER BY segment, n DESC""", (cutoff,)):
        print(f"  {r['segment']:<18} {r['n']:>4}  last {r['last'][:10]}  {r['sender_email']}")
    print("\n== last sync runs")
    for r in conn.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 5"):
        print(f"  {r['run_at']} {r['backend']:<5} {r['status']:<8} scanned={r['scanned']} "
              f"new={r['new_added']} failed={r['failed']} {r['query']}")
    conn.close()
    return 0


def cmd_split(cfg, a) -> int:
    from .split_run import run_split
    segs = None if a.all_segments else (a.segment or cfg.active_segments)
    r = run_split(cfg, segments=segs, redo=a.redo, limit=a.limit, gmail_ids=a.ids)
    print(f"split {r.processed} issues -> {r.items} items ({r.sponsors} sponsor) "
          f"promo_mails={r.promos} failed={r.failed}  segments={'all' if segs is None else ','.join(segs)}")
    for shape, n in sorted(r.shapes.items()):
        print(f"  {shape:<8} {n}")
    for e in r.errors[:10]:
        print("  ERROR", e, file=sys.stderr)
    return 1 if r.failed else 0


def cmd_split_review(cfg, a) -> int:
    from .split_run import write_review
    seg = a.segment or (cfg.active_segments[0] if cfg.active_segments else "1-TechAI")
    out = a.out or (cfg.repo_root / "pipeline" / "out" / "review" / f"split-review-{seg}-{date.today()}.md")
    n = write_review(cfg, Path(out), segment=seg, n=a.n)
    print(f"wrote {n} issues to {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="nlagg", description="Newsletter aggregator pipeline")
    p.add_argument("--config", type=Path, help="path to config.yaml (default: pipeline/config.yaml)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="create/upgrade the SQLite index")

    c = sub.add_parser("capture", help="M1: pull new mail into .eml + messages rows")
    c.add_argument("--backend", choices=["api", "imap", "file"], default="api")
    c.add_argument("--from", dest="src", help="folder of .eml files (backend=file)")
    c.add_argument("--backfill", action="store_true", help="scan the whole mailbox, not just since last sync")
    c.add_argument("--since", help="YYYY-MM-DD; overrides incremental window")
    c.add_argument("--limit", type=int, help="max new messages to fetch this run")
    c.add_argument("--dry-run", action="store_true", help="list what would be fetched, write nothing")

    s = sub.add_parser("stats", help="counts per segment/sender + recent sync runs")
    s.add_argument("--days", type=int, default=14)
    s.add_argument("--unmatched", action="store_true", help="list unmatched mail by sender + address")

    ri = sub.add_parser("reindex", help="re-apply routing rules to already-captured mail (no download)")
    ri.add_argument("--dry-run", action="store_true")

    sp = sub.add_parser("split", help="M2: clean + split captured issues into story items")
    sp.add_argument("--segment", action="append", help="segment id (repeatable); default: config segments.active")
    sp.add_argument("--all-segments", action="store_true")
    sp.add_argument("--redo", action="store_true", help="re-split issues already done (e.g. after a rule change)")
    sp.add_argument("--limit", type=int)
    sp.add_argument("--id", dest="ids", action="append", help="split only this gmail_id (repeatable)")

    rv = sub.add_parser("split-review", help="write a hand-check sheet for split quality")
    rv.add_argument("--segment")
    rv.add_argument("--n", type=int, default=20)
    rv.add_argument("--out", type=Path)

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config(a.config)
    return {"init-db": cmd_init_db, "capture": cmd_capture, "stats": cmd_stats,
            "split": cmd_split, "split-review": cmd_split_review, "reindex": cmd_reindex}[a.cmd](cfg, a)


if __name__ == "__main__":
    sys.exit(main())
