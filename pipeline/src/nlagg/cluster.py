"""M4 — group items from different newsletters into stories (TF-IDF + shared names; no model download).

Window: items of a segment sent in the `hours` before `end` (default 48 h).
Text per item: title + LLM summary (M3) when present, else title + the first words of the body; the
names M3 extracted count double and also feed a name-overlap (Jaccard) score.
Similarity = 0.75 * cosine(TF-IDF) + 0.25 * name overlap. Average-linkage merging above THRESHOLD, and a
story holds at most ONE item per newsletter (recurring sections like "Treats to Try" look alike day to day,
and salience = distinct newsletters anyway).
Each story: headline, first/last seen, distinct sources (salience), score for ranking.
Then, for stories covered by >= 2 sources, the LLM lists what the sources agree on and where they differ
(every point cites item ids and is checked against those items).
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import db
from .config import Config
from .extract import norm, number_in_text

log = logging.getLogger("nlagg.cluster")

THRESHOLD = 0.18          # tuned on 3 real 48 h Tech/AI windows (2026-09-25 .. 10-02)
NAME_WEIGHT = 0.25
BODY_WORDS = 120
STOP = set("""a an the and or but if of to in on at by for with from as is are was were be been being it its this that
these those their there here he she they we you i our your his her them us not no yes do does did done have has had
will would can could should may might must about into over after before than then so such just also more most
very new now today week this year years day days via up out what which who whom whose why how when where all any
each every some many much few other another one two three first last next own same says said say according per
read minute minutes sponsor github repo website newsletter here get got make made like""".split())
_TOKEN = re.compile(r"[a-z0-9][a-z0-9.+#-]*[a-z0-9+#]|[a-z0-9]")
_READ_TIME = re.compile(r"\(\s*\d+\s*minute read\s*\)|\((github repo|website|sponsor)\)", re.I)


def tokens(text: str) -> list[str]:
    out = []
    for t in _TOKEN.findall(norm(text)):
        if t in STOP or (len(t) < 2 and not t.isdigit()):
            continue
        if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]                       # crude plural folding: models -> model
        out.append(t)
    return out


def clean_title(t: str | None) -> str:
    t = _READ_TIME.sub("", t or "")
    t = re.sub(r"^[^\w\"'“‘$]+", "", t)        # leading emoji / symbols
    return re.sub(r"\s+", " ", t).strip(" :–-")


@dataclass
class Doc:
    item_id: int
    gmail_id: str
    source_key: str
    sent_date: str
    title: str
    importance: int
    names: set[str]
    recurring: bool = False      # a recurring section ("Treats to Try"): many news in one item -> never merged
    tf: Counter = field(default_factory=Counter)
    vec: dict[str, float] = field(default_factory=dict)


@dataclass
class ClusterResult:
    items: int = 0
    stories: int = 0
    multi_source: int = 0
    analyzed: int = 0
    failed_analysis: int = 0
    window_start: str = ""
    window_end: str = ""


def recurring_titles(conn, segment: str, min_issues: int = 3) -> set[tuple[str, str]]:
    """(source_key, normalised title) pairs seen in >= 3 different issues: section names, not headlines."""
    seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in conn.execute("SELECT source_key, gmail_id, title FROM items WHERE segment = ? AND title IS NOT NULL",
                          (segment,)):
        seen[(r["source_key"], norm(clean_title(r["title"])))].add(r["gmail_id"])
    return {k for k, v in seen.items() if len(v) >= min_issues}


def load_docs(conn, segment: str, start_iso: str, end_iso: str) -> list[Doc]:
    rows = conn.execute(
        """SELECT i.id, i.gmail_id, i.source_key, i.sent_date, i.title, i.body, e.summary, e.importance
           FROM items i JOIN messages m ON m.gmail_id = i.gmail_id
           LEFT JOIN item_enrichment e ON e.item_id = i.id AND e.error IS NULL
           WHERE i.segment = ? AND i.sent_date >= ? AND i.sent_date <= ? AND i.is_sponsor = 0
             AND i.kind IN ('story', 'essay', 'teaser') AND m.duplicate_of IS NULL AND m.is_promo = 0
           ORDER BY i.sent_date""", (segment, start_iso, end_iso)).fetchall()
    names: dict[int, set[str]] = defaultdict(set)
    if rows:
        ids = [r["id"] for r in rows]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in conn.execute(f"SELECT item_id, name FROM entities WHERE item_id IN ({','.join('?' * len(chunk))})", chunk):
                names[r["item_id"]].add(norm(r["name"]))
    rec = recurring_titles(conn, segment) if rows else set()
    docs = []
    for r in rows:
        title = clean_title(r["title"])
        extra = r["summary"] or " ".join((r["body"] or "").split()[:BODY_WORDS])
        d = Doc(r["id"], r["gmail_id"], r["source_key"] or r["gmail_id"], r["sent_date"], title,
                int(r["importance"] or 3), names[r["id"]])
        d.recurring = (d.source_key, norm(title)) in rec
        d.tf = Counter(tokens(title) * 2 + tokens(extra))
        for n in d.names:
            for t in tokens(n):
                d.tf[t] += 2
        if sum(d.tf.values()) >= 3:
            docs.append(d)
    return docs


def background_df(conn, segment: str, limit: int = 3000) -> tuple[Counter, int]:
    """Document frequencies over the segment's recent items: a 48 h window alone (~70 items) is too small to
    tell generic words ('ai', 'model', 'episode') from distinctive ones."""
    df: Counter = Counter()
    n = 0
    for r in conn.execute("""SELECT title, body FROM items WHERE segment = ? AND is_sponsor = 0
                             ORDER BY sent_date DESC LIMIT ?""", (segment, limit)):
        df.update(set(tokens(clean_title(r["title"])) + tokens(" ".join((r["body"] or "").split()[:BODY_WORDS]))))
        n += 1
    return df, n


def vectorize(docs: list[Doc], bg: tuple[Counter, int] | None = None) -> None:
    df = Counter(t for d in docs for t in d.tf)
    n = len(docs)
    if bg and bg[1] > n:
        df, n = bg[0] + df, bg[1] + n
    for d in docs:
        v = {t: (1 + math.log(c)) * (math.log((1 + n) / (1 + df[t])) + 1) for t, c in d.tf.items()}
        norm_ = math.sqrt(sum(x * x for x in v.values())) or 1.0
        d.vec = {t: x / norm_ for t, x in v.items()}


def similarity(a: Doc, b: Doc) -> float:
    if len(a.vec) > len(b.vec):
        a, b = b, a
    cos = sum(x * b.vec.get(t, 0.0) for t, x in a.vec.items())
    jac = len(a.names & b.names) / len(a.names | b.names) if (a.names and b.names) else 0.0
    return (1 - NAME_WEIGHT) * cos + NAME_WEIGHT * jac if (a.names and b.names) else cos


def group(docs: list[Doc], threshold: float = THRESHOLD) -> list[list[int]]:
    """Average-linkage agglomeration over doc indexes; a story holds at most one item per newsletter."""
    n = len(docs)
    sims: dict[tuple[int, int], float] = {}
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            if docs[i].source_key == docs[j].source_key or docs[i].recurring or docs[j].recurring:
                continue          # one newsletter never counts twice; multi-news sections stay alone
            s = similarity(docs[i], docs[j])
            if s > 0:
                sims[(i, j)] = s
            if s >= threshold:
                pairs.append((s, i, j))
    pairs.sort(reverse=True)
    cluster_of = list(range(n))
    members: dict[int, list[int]] = {i: [i] for i in range(n)}

    def sim(i: int, j: int) -> float:
        return sims.get((i, j) if i < j else (j, i), 0.0)

    for _, i, j in pairs:
        ci, cj = cluster_of[i], cluster_of[j]
        if ci == cj:
            continue
        a, b = members[ci], members[cj]
        if {docs[x].source_key for x in a} & {docs[y].source_key for y in b}:
            continue
        avg = sum(sim(x, y) for x in a for y in b) / (len(a) * len(b))
        if avg < threshold:
            continue
        for y in b:
            cluster_of[y] = ci
        members[ci] = a + b
        del members[cj]
    return [sorted(m) for m in members.values()]


def headline_for(ds: list[Doc]) -> str:
    """The most important item's title; ties -> a descriptive one (>= 5 words) over a short label."""
    best = sorted(ds, key=lambda d: (-d.importance, len((d.title or "").split()) < 5, len(d.title or "")))[0]
    return best.title or "(untitled)"


def score_for(ds: list[Doc]) -> float:
    sources = len({d.source_key for d in ds})
    return round(2.0 * sources + max(d.importance for d in ds) + 0.1 * len(ds), 2)


def run_cluster(cfg: Config, *, segment: str | None = None, end: datetime | None = None, hours: float = 48,
                threshold: float = THRESHOLD, analyze: bool = True, client=None) -> ClusterResult:
    segment = segment or (cfg.active_segments[0] if cfg.active_segments else "1-TechAI")
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    window_end = end.date().isoformat()
    conn = db.connect(cfg.db_path)
    docs = load_docs(conn, segment, start.isoformat(), end.isoformat())
    vectorize(docs, background_df(conn, segment))
    groups = group(docs, threshold)
    res = ClusterResult(items=len(docs), window_start=start.isoformat(), window_end=window_end)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        old = [r[0] for r in conn.execute("SELECT id FROM stories WHERE segment = ? AND window_end = ?",
                                          (segment, window_end))]
        for sid in old:
            conn.execute("DELETE FROM story_items WHERE story_id = ?", (sid,))
            conn.execute("DELETE FROM stories WHERE id = ?", (sid,))
        for g in groups:
            ds = [docs[k] for k in g]
            sources = len({d.source_key for d in ds})
            cur = conn.execute(
                """INSERT INTO stories (segment, headline, first_seen, last_seen, source_count, created_at, score,
                                        window_end) VALUES (?,?,?,?,?,?,?,?)""",
                (segment, headline_for(ds), min(d.sent_date for d in ds), max(d.sent_date for d in ds), sources,
                 now, score_for(ds), window_end))
            sid = cur.lastrowid
            for d in ds:
                s = max((similarity(d, o) for o in ds if o is not d), default=1.0)
                conn.execute("INSERT INTO story_items (story_id, item_id, similarity) VALUES (?,?,?)",
                             (sid, d.item_id, round(s, 3)))
            res.stories += 1
            res.multi_source += sources >= 2
    if analyze:
        a, f = analyze_stories(cfg, conn, segment, window_end, client=client)
        res.analyzed, res.failed_analysis = a, f
    conn.close()
    return res


# ---------------------------------------------------------------- consensus vs divergence (LLM)
ANALYZE_PROMPT = """You compare how several newsletters covered the SAME news story.
You get each newsletter's text with an item id like [i123]. Using ONLY these texts, list:
- "consensus": facts that at least two newsletters both state (max 3)
- "divergence": points where the newsletters differ — different numbers, different framing or verdicts,
  or an important fact only one of them reports (max 3)
Each point: {"text": "one sentence, max 30 words", "items": [ids of the items that support it]}.
Copy numbers and names exactly. Do not add outside knowledge. If nothing differs, return an empty list.
Reply with one JSON object: {"consensus": [...], "divergence": [...]}"""


def _story_sources(conn, story_id: int) -> list:
    return conn.execute(
        """SELECT i.id, i.title, i.body, m.sender_name, e.summary
           FROM story_items si JOIN items i ON i.id = si.item_id JOIN messages m ON m.gmail_id = i.gmail_id
           LEFT JOIN item_enrichment e ON e.item_id = i.id AND e.error IS NULL
           WHERE si.story_id = ? ORDER BY i.sent_date""", (story_id,)).fetchall()


def validate_points(points, allowed: dict[int, str]) -> list[dict]:
    """Keep points that cite only this story's items and whose numbers appear in a cited item."""
    out = []
    for p in points or []:
        if not isinstance(p, dict):
            continue
        text = re.sub(r"\s+", " ", str(p.get("text") or "")).strip()
        ids = []
        for x in p.get("items") or []:
            try:
                ids.append(int(str(x).lstrip("[i").rstrip("]")))
            except ValueError:
                pass
        ids = [i for i in ids if i in allowed]
        if not text or not ids:
            continue
        cited = norm(" ".join(allowed[i] for i in ids))
        if re.search(r"\d", text) and not number_in_text(text, cited):
            continue
        out.append({"text": text[:300], "items": ids})
    return out[:3]


def analyze_stories(cfg: Config, conn, segment: str, window_end: str, client=None) -> tuple[int, int]:
    from .llm import ChatClient, LLMError, LLMSettings
    stories = conn.execute("SELECT id FROM stories WHERE segment = ? AND window_end = ? AND source_count >= 2",
                           (segment, window_end)).fetchall()
    if not stories:
        return 0, 0
    st = LLMSettings.from_config(cfg.llm)
    if client is None:
        if not st.api_key and "localhost" not in st.base_url:
            log.warning("no LLM key: skipping consensus/divergence for %d stories", len(stories))
            return 0, 0
        client = ChatClient(st, conn)
    done = failed = 0
    for (sid,) in stories:
        src = _story_sources(conn, sid)
        allowed = {r["id"]: f"{r['title'] or ''} {r['body'] or ''}" for r in src}
        parts = []
        for r in src:
            body = " ".join((r["body"] or "").split()[:350])
            parts.append(f"[i{r['id']}] {r['sender_name']}: {r['title'] or ''}\n{body}")
        msgs = [{"role": "system", "content": ANALYZE_PROMPT}, {"role": "user", "content": "\n\n".join(parts)}]
        try:
            data = client.chat_json(st.write_model, msgs, temperature=0.1, max_tokens=700)
            cons, div = validate_points(data.get("consensus"), allowed), validate_points(data.get("divergence"), allowed)
            with conn:
                conn.execute("UPDATE stories SET consensus = ?, divergence = ? WHERE id = ?",
                             (json.dumps(cons, ensure_ascii=False), json.dumps(div, ensure_ascii=False), sid))
            done += 1
        except LLMError as e:
            log.warning("story %s analysis failed: %s", sid, e)
            failed += 1
    return done, failed
