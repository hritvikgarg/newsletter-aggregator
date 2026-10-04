"""M2 orchestration: for each captured issue -> clean .md next to the .eml + `items` rows."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import db
from .clean import clean, domain_of, to_markdown
from .config import Config
from .parse_email import bodies, parse_raw
from .split import SplitResult, split_blocks

log = logging.getLogger("nlagg.split")


@dataclass
class SplitRunResult:
    processed: int = 0
    items: int = 0
    sponsors: int = 0
    promos: int = 0
    failed: int = 0
    shapes: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def split_raw(raw: bytes, subject: str) -> tuple[SplitResult, str]:
    msg = parse_raw(raw)
    html, text, _ = bodies(msg)
    blocks = clean(html, text)
    return split_blocks(blocks, subject), to_markdown(blocks)


def _select(conn, segments: list[str] | None, redo: bool, limit: int | None, gmail_ids: list[str] | None):
    sql = ("SELECT gmail_id, segment, source_key, subject, sent_date, raw_eml_path FROM messages "
           "WHERE is_issue = 1 AND raw_eml_path IS NOT NULL AND duplicate_of IS NULL")
    args: list = []
    if gmail_ids:
        sql += f" AND gmail_id IN ({','.join('?' * len(gmail_ids))})"
        args += gmail_ids
    else:
        if not redo:
            sql += " AND split_status IN ('pending', 'failed')"
        if segments:
            sql += f" AND segment IN ({','.join('?' * len(segments))})"
            args += segments
    sql += " ORDER BY sent_date"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, args).fetchall()


def _write_items(conn, row, res: SplitResult) -> None:
    gid = row["gmail_id"]
    conn.execute("DELETE FROM links WHERE gmail_id = ? AND item_id IS NOT NULL", (gid,))
    conn.execute("DELETE FROM items WHERE gmail_id = ?", (gid,))
    for it in res.items:
        cur = conn.execute(
            """INSERT INTO items (gmail_id, position, section, title, body, url, is_sponsor, kind,
                                  word_count, segment, source_key, sent_date)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (gid, it.position, it.section, it.title, it.body, it.url, it.is_sponsor, it.kind,
             it.word_count, row["segment"], row["source_key"], row["sent_date"]),
        )
        item_id = cur.lastrowid
        seen = set()
        for ln in it.links:
            if ln.url in seen or ln.kind == "mailto":
                continue
            seen.add(ln.url)
            lt = "sponsor" if it.is_sponsor and ln.kind in ("source", "tracked") else ln.kind
            conn.execute(
                "INSERT INTO links (gmail_id, item_id, url, anchor_text, domain, link_type) VALUES (?,?,?,?,?,?)",
                (gid, item_id, ln.url, ln.text, domain_of(ln.url), lt),
            )


def run_split(cfg: Config, *, segments: list[str] | None = None, redo: bool = False,
              limit: int | None = None, gmail_ids: list[str] | None = None) -> SplitRunResult:
    conn = db.connect(cfg.db_path)
    out = SplitRunResult()
    try:
        for row in _select(conn, segments, redo, limit, gmail_ids):
            gid = row["gmail_id"]
            eml = cfg.archive_dir / row["raw_eml_path"]
            try:
                res, md = split_raw(eml.read_bytes(), row["subject"] or "")
                md_path = Path(row["raw_eml_path"]).with_suffix(".md")
                header = f"# {row['subject']}\n\n_{row['source_key']} · {row['sent_date']}_\n\n"
                (cfg.archive_dir / md_path).write_text(header + md, encoding="utf-8")
                with conn:  # one transaction per message: items and status change together
                    _write_items(conn, row, res)
                    conn.execute(
                        """UPDATE messages SET split_status = 'done', split_shape = ?, is_promo = ?,
                                  clean_text_path = ?, split_error = NULL WHERE gmail_id = ?""",
                        (res.shape, res.is_promo, md_path.as_posix(), gid),
                    )
                out.processed += 1
                out.items += len(res.items)
                out.sponsors += sum(it.is_sponsor for it in res.items)
                out.promos += res.is_promo
                out.shapes[res.shape] = out.shapes.get(res.shape, 0) + 1
            except Exception as e:  # one bad email must not stop the run; retried next run
                out.failed += 1
                out.errors.append(f"{gid}: {type(e).__name__}: {e}")
                log.warning("split failed %s: %s", gid, e)
                with conn:
                    conn.execute("UPDATE messages SET split_status = 'failed', split_error = ? WHERE gmail_id = ?",
                                 (f"{type(e).__name__}: {e}"[:500], gid))
    finally:
        conn.close()
    return out


def write_review(cfg: Config, out_path: Path, *, segment: str, n: int = 20) -> int:
    """Write a hand-check sheet: the N most recent split issues of a segment, item by item.

    This is the M2 'done' check from the plan: mark each issue OK / not OK; target >= 90% OK.
    """
    conn = db.connect(cfg.db_path)
    msgs = conn.execute(
        """SELECT gmail_id, subject, source_key, sent_date, split_shape, is_promo, raw_eml_path
           FROM messages WHERE segment = ? AND split_status = 'done'
           ORDER BY sent_date DESC LIMIT ?""", (segment, n)).fetchall()
    lines = [f"# Split review — {segment} — {date.today().isoformat()}",
             "", f"{len(msgs)} issues. For each issue tick OK if every story is its own item, titles are",
             "right, nothing important is missing, and sponsors are flagged. Target: ≥ 90% OK.", ""]
    for k, m in enumerate(msgs, 1):
        items = conn.execute(
            "SELECT position, kind, section, title, url, is_sponsor, word_count, body FROM items "
            "WHERE gmail_id = ? ORDER BY position", (m["gmail_id"],)).fetchall()
        lines += [f"## {k}. {m['subject']}",
                  f"`{m['source_key']}` · {m['sent_date']} · shape **{m['split_shape']}**"
                  f"{' · PROMO' if m['is_promo'] else ''} · {len(items)} items · "
                  f"file `{Path(m['raw_eml_path']).with_suffix('.md').as_posix()}`",
                  "", "- [ ] OK   - [ ] Not OK — note:", ""]
        for it in items:
            flag = " **[SPONSOR]**" if it["is_sponsor"] else ""
            sec = f"_{it['section']}_ › " if it["section"] else ""
            title = it["title"] or "(no title)"
            lines.append(f"{it['position']}. [{it['kind']}]{flag} {sec}**{title}** ({it['word_count']}w)")
            if it["url"]:
                lines.append(f"   <{it['url']}>")
            snippet = " ".join((it["body"] or "").split()[:30])
            if snippet:
                lines.append(f"   > {snippet}…")
        lines.append("")
    conn.close()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return len(msgs)
