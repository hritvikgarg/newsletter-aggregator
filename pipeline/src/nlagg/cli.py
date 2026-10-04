"""Command line: `python -m nlagg <command>` (or `nlagg <command>` after pip install).

  init-db                       create/upgrade data/archive/index.db
  capture [--backend api|imap|file] [--backfill] [--since YYYY-MM-DD] [--limit N] [--dry-run] [--from DIR]
  stats [--days N]              per-segment / per-sender counts, last sync runs
  split [--segment S ...|--all-segments] [--redo] [--limit N] [--id GMAIL_ID ...]
                                M2: clean .md + story items for captured issues
  reindex [--dry-run]           re-derive segment/source_key/is_issue from stored .eml (after rule changes)
  split-review [--segment S] [--n 20] [--out FILE]
                                write a hand-check sheet for the M2 >=90% check
  extract [--days 3] [--limit N] [--redo] [--dry-run] [--item ID ...]
                                M3: per-item LLM extraction (Groq / local Qwen — never Claude)
  cluster [--segment S] [--hours 48] [--end ISO] [--no-analyze]
                                M4: group items into stories; consensus vs divergence for multi-source stories
  compose [--segment S] [--date YYYY-MM-DD] [--no-resolve] [--force]
                                M5: write our issue (draft) -> pipeline/out/issues/<segment>/<date>.md/.html
  models                        list the LLM models your key can use (checks config llm.*_model)
  issues [--segment S]          list our issues (draft / approved / sent / rejected)
  approve [--date D] [--dry-run]  M6: approve the latest draft and email it to delivery.recipients
  reject [--date D] [--reason R]  M6: reject a draft (its stories can be used again)
  run-daily [--skip-capture]    M7: capture -> split -> extract -> cluster -> compose (draft) + health check
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


def cmd_extract(cfg, a) -> int:
    from .extract import run_extract
    r = run_extract(cfg, days=a.days, segments=a.segment, redo=a.redo, limit=a.limit,
                    item_ids=a.items, dry_run=a.dry_run)
    if a.dry_run:
        print(f"would extract {r.selected} items (~{r.tokens:,} tokens)")
        return 0
    print(f"extracted {r.done}/{r.selected} items  failed={r.failed} short={r.skipped_short} "
          f"ungrounded_values_dropped={r.dropped_values}  api_calls={r.api_calls} cache_hits={r.cache_hits} "
          f"tokens={r.tokens:,}")
    for e in r.errors[:10]:
        print("  ERROR", e, file=sys.stderr)
    return 1 if r.failed and not r.done else 0


def cmd_cluster(cfg, a) -> int:
    from .cluster import run_cluster
    end = datetime.fromisoformat(a.end) if a.end else None
    if end is not None and end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    r = run_cluster(cfg, segment=a.segment, end=end, hours=a.hours, analyze=not a.no_analyze)
    print(f"clustered {r.items} items -> {r.stories} stories ({r.multi_source} covered by 2+ newsletters)  "
          f"analyzed={r.analyzed} failed={r.failed_analysis}  window {r.window_start[:16]} .. {r.window_end}")
    return 0


def cmd_compose(cfg, a) -> int:
    from .compose import run_compose
    from .llm import LLMError
    try:
        r = run_compose(cfg, segment=a.segment, issue_date=a.date, resolve_links=not a.no_resolve, force=a.force)
    except LLMError as e:
        print(f"compose failed: {e}", file=sys.stderr)
        return 1
    if r.skipped_reason:
        print(f"nothing composed: {r.skipped_reason}")
        return 0
    print(f"draft issue #{r.issue_id}: \"{r.subject}\" from {r.stories} stories"
          f"{' (after 1 retry)' if r.retried else ''}; {len(r.dropped)} sentence(s) dropped by the checker")
    for d in r.dropped[:10]:
        print("  dropped:", d)
    print(f"  {r.html_path}\n  {r.md_path}")
    return 0


def cmd_models(cfg, a) -> int:
    from .llm import ChatClient, LLMSettings
    st = LLMSettings.from_config(cfg.llm)
    ids = ChatClient(st).list_models()
    print(f"{len(ids)} models available to this key at {st.base_url}:")
    for m in ids:
        mark = "  <- extract_model" if m == st.extract_model else ("  <- write_model" if m == st.write_model else "")
        print(f"  {m}{mark}")
    missing = [m for m in (st.extract_model, st.write_model) if m not in ids]
    if missing:
        print(f"NOT available: {', '.join(missing)} — change llm.* in config.yaml")
        return 1
    return 0


def cmd_issues(cfg, a) -> int:
    conn = db.connect(cfg.db_path)
    seg = a.segment or (cfg.active_segments[0] if cfg.active_segments else "1-TechAI")
    import json as _json
    for r in conn.execute("SELECT * FROM issues_out WHERE segment = ? ORDER BY issue_date DESC LIMIT 20", (seg,)):
        rep = _json.loads(r["validator_report"] or "{}")
        print(f"  {r['issue_date']}  {r['status']:<9} {rep.get('subject', '')[:60]:<60}  "
              f"dropped={len(rep.get('problems_dropped', []))}  {r['html_path']}")
    conn.close()
    return 0


def cmd_approve(cfg, a) -> int:
    from .deliver import approve
    r = approve(cfg, segment=a.segment, issue_date=a.date, dry_run=a.dry_run)
    print(r.message)
    return 0 if r.status in ("approved", "sent", "draft") else 1


def cmd_reject(cfg, a) -> int:
    from .deliver import reject
    print(reject(cfg, segment=a.segment, issue_date=a.date, reason=a.reason or "").message)
    return 0


def cmd_run_daily(cfg, a) -> int:
    from .daily import run_daily
    r = run_daily(cfg, skip_capture=a.skip_capture)
    for k, v in r.steps.items():
        print(f"  {k:<8} {v}")
    for al in r.alerts:
        print(f"  ALERT {al}")
    if r.issue_path:
        print(f"draft: {r.issue_path}\napprove + send: python -m nlagg approve")
    print(f"log: {r.log_path}")
    return 0 if r.ok else 1


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

    ex = sub.add_parser("extract", help="M3: per-item LLM extraction")
    ex.add_argument("--days", type=float, default=3, help="only items sent in the last N days (0 = all)")
    ex.add_argument("--segment", action="append")
    ex.add_argument("--limit", type=int)
    ex.add_argument("--redo", action="store_true", help="re-extract items already done with this prompt version")
    ex.add_argument("--item", dest="items", type=int, action="append", help="only this item id (repeatable)")
    ex.add_argument("--dry-run", action="store_true", help="count items + estimate tokens, no API calls")

    cl = sub.add_parser("cluster", help="M4: group items into stories")
    cl.add_argument("--segment")
    cl.add_argument("--hours", type=float, default=48)
    cl.add_argument("--end", help="window end, ISO time (default: now)")
    cl.add_argument("--no-analyze", action="store_true", help="skip the LLM consensus/divergence step")

    co = sub.add_parser("compose", help="M5: write today's issue as a draft")
    co.add_argument("--segment")
    co.add_argument("--date", help="issue date YYYY-MM-DD (default: today, UTC) = the cluster window end")
    co.add_argument("--no-resolve", action="store_true", help="don't resolve click-tracker links")
    co.add_argument("--force", action="store_true", help="recompose even if already approved/sent")

    sub.add_parser("models", help="list the LLM models your API key can use")
    li = sub.add_parser("issues", help="list our issues")
    li.add_argument("--segment")
    ap = sub.add_parser("approve", help="M6: approve a draft and email it to delivery.recipients")
    ap.add_argument("--segment")
    ap.add_argument("--date", help="issue date (default: latest draft)")
    ap.add_argument("--dry-run", action="store_true", help="show who would get it, send nothing")
    rj = sub.add_parser("reject", help="M6: reject a draft")
    rj.add_argument("--segment")
    rj.add_argument("--date")
    rj.add_argument("--reason")
    rd = sub.add_parser("run-daily", help="M7: the whole daily pipeline (draft only)")
    rd.add_argument("--skip-capture", action="store_true")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config(a.config)
    return {"init-db": cmd_init_db, "capture": cmd_capture, "stats": cmd_stats,
            "split": cmd_split, "split-review": cmd_split_review, "reindex": cmd_reindex,
            "extract": cmd_extract, "cluster": cmd_cluster, "compose": cmd_compose,
            "issues": cmd_issues, "models": cmd_models, "approve": cmd_approve, "reject": cmd_reject, "run-daily": cmd_run_daily}[a.cmd](cfg, a)


if __name__ == "__main__":
    sys.exit(main())
