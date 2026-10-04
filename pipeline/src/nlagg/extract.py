"""M3 — per-item LLM extraction (Groq / local Qwen; never Claude).

For each story item (not sponsors, not intros) of the active segments in a time window:
  item text -> LLM (JSON) -> validated + grounded -> item_enrichment / entities / claims / stats / quotes / topics.
Grounding: an entity, number or quote the model returns is kept only if it appears in the item text, so
later stages (clustering, the composer's citation check) only ever see facts the source actually wrote.
Re-runnable: items already enriched with the current prompt_version are skipped; LLM replies are cached.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from importlib import resources

from . import db
from .config import Config
from .llm import ChatClient, LLMError, LLMSettings

log = logging.getLogger("nlagg.extract")

CATEGORIES = {"launch", "funding", "research", "policy", "business", "security", "product", "people",
              "opinion", "tutorial", "other"}
ENTITY_TYPES = {"company", "person", "product", "model", "place", "org", "other"}
CLAIM_TYPES = {"fact", "number", "prediction", "opinion"}
MAX_INPUT_WORDS = 1800        # long essays are cut (8B model, free-tier token limits); headings are kept
MIN_ITEM_WORDS = 12


@dataclass
class Extraction:
    summary: str
    category: str
    importance: int
    entities: list[dict] = field(default_factory=list)
    claims: list[dict] = field(default_factory=list)
    stats: list[dict] = field(default_factory=list)
    quotes: list[dict] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    dropped: int = 0          # ungrounded values removed


@dataclass
class ExtractResult:
    selected: int = 0
    done: int = 0
    failed: int = 0
    skipped_short: int = 0
    dropped_values: int = 0
    api_calls: int = 0
    cache_hits: int = 0
    tokens: int = 0
    errors: list[str] = field(default_factory=list)


def prompt_text(version: str = "v1") -> str:
    return resources.files("nlagg").joinpath(f"prompts/extract_{version}.md").read_text(encoding="utf-8")


def norm(s: str) -> str:
    """Lowercase, unify quotes/dashes/spaces — for 'does this appear in the text' checks."""
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip()


_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


def number_in_text(value: str, text_norm: str) -> bool:
    """Every digit group of the value must occur in the text ('$25M' -> '25'; '1,299' -> '1,299' or '1299')."""
    nums = _NUM_RE.findall(value or "")
    if not nums:
        return False
    flat = text_norm.replace(",", "")
    return all(n in text_norm or n.replace(",", "") in flat for n in nums)


def _clip(s, n: int) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def validate(data: dict, source_text: str) -> Extraction:
    """Coerce the model JSON into an Extraction; drop anything not grounded in the source text."""
    t = norm(source_text)
    dropped = 0
    summary = _clip(data.get("summary"), 400)
    if not summary:
        raise LLMError("extraction has no summary")
    cat = str(data.get("category") or "other").strip().lower()
    cat = cat if cat in CATEGORIES else "other"
    try:
        imp = int(round(float(data.get("importance", 3))))
    except (TypeError, ValueError):
        imp = 3
    imp = max(1, min(5, imp))

    ents, seen = [], set()
    for e in (data.get("entities") or [])[:12]:
        name = _clip(e.get("name") if isinstance(e, dict) else e, 80)
        if not name or norm(name) in seen:
            continue
        if norm(name) not in t:
            dropped += 1
            continue
        seen.add(norm(name))
        typ = str((e.get("type") if isinstance(e, dict) else "") or "other").lower()
        ents.append({"name": name, "type": typ if typ in ENTITY_TYPES else "other"})

    claims = []
    for c in (data.get("claims") or [])[:6]:
        text = _clip(c.get("text") if isinstance(c, dict) else c, 300)
        if not text:
            continue
        nums = _NUM_RE.findall(text)
        if nums and not number_in_text(text, t):       # a claim with a number the source never wrote
            dropped += 1
            continue
        typ = str((c.get("type") if isinstance(c, dict) else "") or "fact").lower()
        claims.append({"text": text, "type": typ if typ in CLAIM_TYPES else "fact"})

    stats = []
    for s in (data.get("stats") or [])[:6]:
        if not isinstance(s, dict):
            continue
        val = _clip(s.get("value"), 40)
        if not val or not number_in_text(val, t):
            dropped += 1
            continue
        stats.append({"value": val, "unit": _clip(s.get("unit"), 40), "description": _clip(s.get("description"), 120)})

    quotes = []
    for q in (data.get("quotes") or [])[:3]:
        if not isinstance(q, dict):
            continue
        qt = _clip(str(q.get("text") or "").strip(" \"'“”"), 400)
        if len(qt.split()) < 3 or norm(qt)[:60] not in t:      # must be the source's own words
            dropped += 1
            continue
        quotes.append({"text": qt, "speaker": _clip(q.get("speaker"), 80)})

    topics = []
    for tp in (data.get("topics") or [])[:4]:
        tp = _clip(str(tp).lower(), 40)
        if tp and tp not in topics:
            topics.append(tp)

    return Extraction(summary, cat, imp, ents[:8], claims[:5], stats[:5], quotes[:2], topics, dropped)


def item_text(row) -> str:
    """Title + body, essays cut to MAX_INPUT_WORDS (keeps the beginning, where the news is)."""
    body = row["body"] or ""
    if len(body.split()) > MAX_INPUT_WORDS:
        body = " ".join(body.split()[:MAX_INPUT_WORDS]) + " …"
    head = f"Section: {row['section']}\n" if row["section"] else ""
    return f"{head}Title: {row['title'] or '(untitled)'}\n\n{body}".strip()


def build_messages(row, newsletter: str, version: str) -> list[dict]:
    return [{"role": "system", "content": prompt_text(version)},
            {"role": "user", "content": f"Newsletter: {newsletter}\n\n{item_text(row)}"}]


def select_items(conn, segments: list[str], since_iso: str | None, prompt_version: str, redo: bool,
                 limit: int | None, item_ids: list[int] | None = None):
    sql = """SELECT i.id, i.gmail_id, i.section, i.title, i.body, i.word_count, i.kind, i.segment,
                    m.sender_name, m.subject
             FROM items i JOIN messages m ON m.gmail_id = i.gmail_id
             WHERE i.is_sponsor = 0 AND i.kind IN ('story', 'essay', 'teaser')
               AND m.duplicate_of IS NULL AND m.is_promo = 0"""
    args: list = []
    if item_ids:
        sql += f" AND i.id IN ({','.join('?' * len(item_ids))})"
        args += item_ids
    else:
        if segments:
            sql += f" AND i.segment IN ({','.join('?' * len(segments))})"
            args += segments
        if since_iso:
            sql += " AND i.sent_date >= ?"
            args.append(since_iso)
        if not redo:
            sql += """ AND NOT EXISTS (SELECT 1 FROM item_enrichment e WHERE e.item_id = i.id
                                        AND e.prompt_version = ? AND e.error IS NULL)"""
            args.append(prompt_version)
    sql += " ORDER BY i.sent_date DESC, i.position"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, args).fetchall()


def save(conn, row, ex: Extraction, st: LLMSettings, raw: dict) -> None:
    iid, gid = row["id"], row["gmail_id"]
    for table in ("entities", "claims", "stats", "quotes", "topics"):
        conn.execute(f"DELETE FROM {table} WHERE item_id = ?", (iid,))
    conn.execute(
        """INSERT OR REPLACE INTO item_enrichment (item_id, summary, category, importance, llm_provider, llm_model,
                                                   prompt_version, raw_json, extracted_at, error)
           VALUES (?,?,?,?,?,?,?,?,?,NULL)""",
        (iid, ex.summary, ex.category, ex.importance, st.provider, st.extract_model, st.prompt_version,
         json.dumps(raw, ensure_ascii=False), datetime.now(timezone.utc).isoformat(timespec="seconds")))
    for e in ex.entities:
        conn.execute("INSERT INTO entities (gmail_id, item_id, name, entity_type, mention_count) VALUES (?,?,?,?,1)",
                     (gid, iid, e["name"], e["type"]))
    for c in ex.claims:
        conn.execute("INSERT INTO claims (gmail_id, item_id, claim_text, claim_type) VALUES (?,?,?,?)",
                     (gid, iid, c["text"], c["type"]))
    for s in ex.stats:
        conn.execute("INSERT INTO stats (gmail_id, item_id, value, unit, description) VALUES (?,?,?,?,?)",
                     (gid, iid, s["value"], s["unit"], s["description"]))
    for q in ex.quotes:
        conn.execute("INSERT INTO quotes (gmail_id, item_id, quote_text, speaker) VALUES (?,?,?,?)",
                     (gid, iid, q["text"], q["speaker"]))
    for tp in ex.topics:
        conn.execute("INSERT INTO topics (gmail_id, item_id, topic) VALUES (?,?,?)", (gid, iid, tp))


def run_extract(cfg: Config, *, days: float | None = 3, segments: list[str] | None = None, redo: bool = False,
                limit: int | None = None, item_ids: list[int] | None = None, dry_run: bool = False,
                client: ChatClient | None = None, now: datetime | None = None) -> ExtractResult:
    st = LLMSettings.from_config(cfg.llm)
    conn = db.connect(cfg.db_path)
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat() if days else None
    rows = select_items(conn, segments or cfg.active_segments, since, st.prompt_version, redo, limit, item_ids)
    res = ExtractResult(selected=len(rows))
    if dry_run:
        words = sum(min(r["word_count"] or 0, MAX_INPUT_WORDS) for r in rows)
        res.tokens = int(words * 1.4) + 450 * len(rows)       # rough: text + prompt + reply
        conn.close()
        return res
    client = client or ChatClient(st, conn)
    client.conn = conn
    for row in rows:
        if (row["word_count"] or 0) < MIN_ITEM_WORDS:
            res.skipped_short += 1
            continue
        try:
            raw = client.chat_json(st.extract_model, build_messages(row, newsletter_name(row), st.prompt_version),
                                   temperature=0.1, max_tokens=900)
            ex = validate(raw, f"{row['title'] or ''}\n{row['body'] or ''}")
            with conn:
                save(conn, row, ex, st, raw)
            res.done += 1
            res.dropped_values += ex.dropped
        except LLMError as e:
            res.failed += 1
            res.errors.append(f"item {row['id']}: {e}")
            with conn:
                conn.execute(
                    """INSERT OR REPLACE INTO item_enrichment (item_id, llm_provider, llm_model, prompt_version,
                                                               extracted_at, error) VALUES (?,?,?,?,?,?)""",
                    (row["id"], st.provider, st.extract_model, st.prompt_version,
                     datetime.now(timezone.utc).isoformat(timespec="seconds"), str(e)[:500]))
            if "API error 401" in str(e) or "API error 403" in str(e):
                break                                          # bad key: stop instead of failing every item
    res.api_calls, res.cache_hits, res.tokens = client.calls, client.cache_hits, client.tokens
    conn.close()
    return res


def newsletter_name(row) -> str:
    """Readable newsletter name from the sender display name ('Superhuman – Zain Kahn' -> 'Superhuman')."""
    name = (row["sender_name"] or "").strip()
    name = re.split(r"\s+[–—-]\s+|\s+from\s+|\s*@\s*", name, maxsplit=1)[0].strip(' "')
    return name or "a newsletter"
