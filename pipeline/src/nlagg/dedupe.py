"""Mark duplicate copies of the same newsletter issue.

Some picks arrive twice (subscribed on the +tag address AND the plain address: TLDR, Trends.vc,
National Law Review in the first real capture). Both copies are kept in the archive; the extra
copy gets messages.duplicate_of = <canonical gmail_id> and is skipped by split (its items, if
any, are removed), so a story is never counted twice.

Two messages are the same issue when ALL hold:
- same sender address and same subject (whitespace/case-insensitive),
- sent within DUP_WINDOW_HOURS of each other,
- similar length (word counts within DUP_WORDS_TOLERANCE).
Canonical copy: the one routed to a real segment (not unmatched/ignored), then the earliest sent.
Recomputed from scratch each run, so rule changes and reindexing are always reflected.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

DUP_WINDOW_HOURS = 3
DUP_WORDS_TOLERANCE = 0.10


def _norm_subject(s: str | None) -> str:
    s = re.sub(r"[​-‏⁠﻿͏­]", "", s or "")
    return re.sub(r"\s+", " ", s).strip().lower()


def _dt(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def _rank(row) -> tuple:
    real = row["segment"] not in ("unmatched", "ignored")
    return (0 if real else 1, row["sent_date"] or "9999", row["gmail_id"])


def mark_duplicates(conn) -> int:
    """Recompute duplicate_of for all messages. Returns the number of duplicates marked."""
    rows = conn.execute(
        "SELECT gmail_id, sender_email, subject, sent_date, word_count, segment FROM messages "
        "WHERE raw_eml_path IS NOT NULL AND sender_email IS NOT NULL").fetchall()
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["sender_email"], _norm_subject(r["subject"])), []).append(r)

    dup_of: dict[str, str] = {}
    window = timedelta(hours=DUP_WINDOW_HOURS)
    for members in groups.values():
        if len(members) < 2:
            continue
        members = sorted(members, key=_rank)
        canon: list = []                       # canonical copies found so far in this group
        for m in members:
            t, w = _dt(m["sent_date"]), m["word_count"] or 0
            match = None
            for c in canon:
                ct, cw = _dt(c["sent_date"]), c["word_count"] or 0
                if t is None or ct is None or abs(t - ct) > window:
                    continue
                if max(w, cw) and abs(w - cw) / max(w, cw) > DUP_WORDS_TOLERANCE:
                    continue
                match = c
                break
            if match is None:
                canon.append(m)
            else:
                dup_of[m["gmail_id"]] = match["gmail_id"]

    with conn:
        conn.execute("UPDATE messages SET duplicate_of = NULL WHERE duplicate_of IS NOT NULL")
        for gid, canon_id in dup_of.items():
            conn.execute("UPDATE messages SET duplicate_of = ?, split_status = 'skipped' WHERE gmail_id = ?",
                         (canon_id, gid))
            conn.execute("DELETE FROM links WHERE gmail_id = ? AND item_id IS NOT NULL", (gid,))
            conn.execute("DELETE FROM items WHERE gmail_id = ?", (gid,))
        # a message that is no longer a duplicate but was skipped for being one -> back to pending
        conn.execute("""UPDATE messages SET split_status = 'pending'
                        WHERE duplicate_of IS NULL AND split_status = 'skipped'
                          AND segment NOT IN ('ignored') AND is_issue = 1""")
    return len(dup_of)
