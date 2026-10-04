"""M5 — compose our issue from the day's stories: hook · top story · "Everyone's talking about" ·
quick hits · "Safe to skip" · close.

The write model (Groq 70B; never Claude) gets the ranked stories with item ids and must cite an id after
every factual sentence. The citation validator then checks each sentence:
  - cites at least one id, and only ids it was given;
  - every number and proper name in it appears in a cited item;
  - it does not copy 11+ words in a row from a source (we summarise; we don't republish).
On failure the model gets one retry with the list of problems; whatever still fails is dropped and listed
in the validator report. Output: out/issues/<segment>/<date>.md + .html + an issues_out row (status draft).
"""
from __future__ import annotations

import html as htmlmod
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from importlib import resources
from pathlib import Path
from string import Template

from . import db
from .cluster import clean_title, recurring_titles
from .config import Config
from .extract import newsletter_name, norm, number_in_text
from .llm import ChatClient, LLMError, LLMSettings

log = logging.getLogger("nlagg.compose")

CITE_RE = re.compile(r"\[i(\d+)\]")
NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
SOURCE_COUNT_RE = re.compile(r"\b(\d+|two|three|four|five|six)\s+(reports?|sources?|of them|outlets?)\b", re.I)
COPY_NGRAM = 11
MAX_TALKING, MAX_QUICK, MAX_SKIP = 3, 6, 2
# Words that may appear capitalised without being a name from the source
ALLOW = {"i", "ai", "ceo", "cto", "cfo", "us", "u.s", "uk", "eu", "api", "apis", "app", "apps", "llm", "llms",
         "gpu", "gpus", "cpu", "cpus", "vr", "ar", "ipo", "faq", "pdf", "q1", "q2", "q3", "q4", "why", "what",
         "the", "a", "an", "and", "but", "it", "its", "this", "that", "these", "those", "they", "we", "our",
         "only", "both", "meanwhile", "also", "plus", "still", "today", "yesterday", "tomorrow", "monday",
         "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february", "march",
         "april", "may", "june", "july", "august", "september", "october", "november", "december", "one", "two",
         "three", "four", "five", "if", "so", "in", "on", "at", "for", "with", "by", "from", "as", "to", "of",
         "not", "no", "yes", "new", "top", "story", "stories", "newsletter", "newsletters", "source", "sources",
         "according", "everyone", "nobody", "here", "there", "when", "while", "after", "before", "until", "more",
         "most", "less", "some", "many", "all", "each", "every", "another", "other", "safe", "skip", "quick",
         "hits", "talking", "about", "matters", "readers", "builders", "you", "your", "he", "she", "his", "her",
         "their", "them", "is", "are", "was", "were", "will", "can", "could", "should", "would", "has", "have",
         "had", "be", "been", "do", "does", "did", "how", "who", "which", "where", "even", "just", "now", "then",
         "than", "first", "last", "next", "big", "major", "small", "early", "late", "free", "open", "source"}


@dataclass
class StoryIn:
    id: int
    role: str                       # TOP | TALKED_ABOUT | QUICK_HIT | SKIP
    headline: str
    source_count: int
    score: float
    importance: int
    items: list[dict] = field(default_factory=list)       # id, newsletter, title, summary, text, stats
    consensus: list = field(default_factory=list)
    divergence: list = field(default_factory=list)


@dataclass
class ComposeResult:
    issue_id: int | None = None
    md_path: Path | None = None
    html_path: Path | None = None
    subject: str = ""
    stories: int = 0
    dropped: list[str] = field(default_factory=list)
    retried: bool = False
    status: str = "draft"
    skipped_reason: str = ""


# ---------------------------------------------------------------- selection
def previously_cited(conn, segment: str, issue_date: str) -> set[int]:
    out: set[int] = set()
    for r in conn.execute("""SELECT validator_report FROM issues_out WHERE segment = ? AND issue_date < ?
                             AND status != 'rejected'""", (segment, issue_date)):
        try:
            out.update(json.loads(r[0] or "{}").get("cited_items", []))
        except json.JSONDecodeError:
            pass
    return out


def load_stories(conn, segment: str, issue_date: str) -> list[StoryIn]:
    used = previously_cited(conn, segment, issue_date)
    rec = recurring_titles(conn, segment)
    stories: list[StoryIn] = []
    for s in conn.execute("""SELECT * FROM stories WHERE segment = ? AND window_end = ? ORDER BY score DESC""",
                          (segment, issue_date)):
        items = []
        for r in conn.execute(
                """SELECT i.id, i.title, i.body, i.source_key, m.sender_name, e.summary, e.importance, e.category
                   FROM story_items si JOIN items i ON i.id = si.item_id JOIN messages m ON m.gmail_id = i.gmail_id
                   LEFT JOIN item_enrichment e ON e.item_id = i.id AND e.error IS NULL
                   WHERE si.story_id = ? ORDER BY i.sent_date""", (s["id"],)):
            if r["id"] in used or (r["source_key"], norm(clean_title(r["title"]))) in rec:
                continue
            stats = [f"{x['value']} {x['unit'] or ''} ({x['description'] or ''})".strip()
                     for x in conn.execute("SELECT value, unit, description FROM stats WHERE item_id = ?", (r["id"],))]
            items.append({"id": r["id"], "newsletter": newsletter_name(r), "title": clean_title(r["title"]),
                          "summary": r["summary"], "importance": r["importance"] or 3, "category": r["category"],
                          "text": f"{r['title'] or ''}\n{r['body'] or ''}", "stats": stats})
        if not items:
            continue
        stories.append(StoryIn(s["id"], "", s["headline"], len({i["newsletter"] for i in items}), s["score"] or 0,
                               max(i["importance"] for i in items), items,
                               json.loads(s["consensus"] or "[]"), json.loads(s["divergence"] or "[]")))
    stories.sort(key=lambda x: (-(2 * x.source_count + x.importance), -x.score))
    return stories


def assign_roles(stories: list[StoryIn]) -> list[StoryIn]:
    """Top story, then multi-source stories, then quick hits; low-importance leftovers as 'safe to skip'."""
    if not stories:
        return []
    picked: list[StoryIn] = []
    rest = list(stories)
    top = rest.pop(0)
    top.role = "TOP"
    picked.append(top)
    talked = [s for s in rest if s.source_count >= 2][:MAX_TALKING]
    for s in talked:
        s.role = "TALKED_ABOUT"
        rest.remove(s)
    picked += talked
    quick = [s for s in rest if s.importance >= 3 and any(i["summary"] for i in s.items)][:MAX_QUICK]
    for s in quick:
        s.role = "QUICK_HIT"
        rest.remove(s)
    picked += quick
    skip = [s for s in rest if s.importance <= 2 and any(i["summary"] for i in s.items)][:MAX_SKIP]
    for s in skip:
        s.role = "SKIP"
    picked += skip
    return picked


def story_block(s: StoryIn) -> str:
    """Research notes for the writer. Newsletter names are left out on purpose: the issue must not name them."""
    n = len(s.items)
    lines = [f"### STORY {s.id} — {s.role} — " + ("widely covered today" if s.source_count >= 2 else "single report")]
    for k, it in enumerate(s.items, 1):
        body = it["summary"] or " ".join(it["text"].split()[:120])
        lines.append(f"[i{it['id']}] note {k}/{n}: {it['title']}\n    {body}")
        if it["stats"]:
            lines.append("    numbers: " + "; ".join(it["stats"][:4]))
    for p in s.consensus:
        lines.append(f"    notes agree: {p['text']} " + "".join(f"[i{i}]" for i in p["items"]))
    for p in s.divergence:
        lines.append(f"    notes differ: {p['text']} " + "".join(f"[i{i}]" for i in p["items"]))
    return "\n".join(lines)


def build_messages(picked: list[StoryIn], title: str, segment_label: str, version: str) -> list[dict]:
    prompt = resources.files("nlagg").joinpath(f"prompts/compose_{version}.md").read_text(encoding="utf-8")
    prompt = prompt.replace("{newsletter_title}", title).replace("{segment_label}", segment_label)
    return [{"role": "system", "content": prompt},
            {"role": "user", "content": "Today's stories, ranked:\n\n" + "\n\n".join(story_block(s) for s in picked)}]


# ---------------------------------------------------------------- citation validator
def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"“‘'(\[])", (text or "").strip())
    out: list[str] = []
    for p in parts:
        lead = re.match(r"((?:\s*\[i\d+\]\s*)+[.!?]?)", p)
        if out and lead:                                  # "[i12]" after a full stop belongs to that sentence
            out[-1] = out[-1] + " " + lead.group(1).strip()
            p = p[lead.end():]
        if p.strip():
            out.append(p.strip())
    return out


def _ngrams(words: list[str], n: int) -> set[tuple]:
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


@dataclass
class Checker:
    texts: dict[int, str]                  # item id -> title + body
    newsletters: set[str]

    def __post_init__(self):
        self.norm_texts = {k: norm(v) for k, v in self.texts.items()}
        self.grams = {k: _ngrams(re.findall(r"[a-z0-9$%']+", v), COPY_NGRAM) for k, v in self.norm_texts.items()}
        self.nl_names = [n for n in self.newsletters if n and len(n) >= 3]

    def check(self, sentence: str, *, need_cite: bool = True, scope: list[int] | None = None) -> str | None:
        """None if the sentence is fine, else the reason it fails."""
        cites = [int(x) for x in CITE_RE.findall(sentence)]
        bare = CITE_RE.sub("", sentence).strip()
        if not re.search(r"[A-Za-z]", bare):
            return None
        unknown = [c for c in cites if c not in self.texts]
        if unknown:
            return f"cites unknown item(s) {unknown}"
        if need_cite and not cites:
            return "no citation"
        for n in self.nl_names:                     # our issue never names the newsletters it reads
            if re.search(rf"(?<![\w]){re.escape(n)}(?![\w])", bare):
                return f"names the newsletter '{n}'"
        if re.search(r"\bnewsletters?\b", bare, re.I):
            return "mentions newsletters"
        ids = cites or scope or list(self.texts)
        src = " ".join(self.norm_texts[i] for i in ids)
        check_nums = SOURCE_COUNT_RE.sub(" ", bare)
        for n in NUM_RE.findall(check_nums):
            if not number_in_text(n, src):
                return f"number {n} not in the cited source"
        words = re.findall(r"[A-Za-z][\w.&+'’-]*", bare)
        for k, w in enumerate(words):
            if k == 0 or not (w[0].isupper() or w.isupper()):
                continue
            lw = norm(w).strip(".'’-")
            lw = re.sub(r"['’]s$", "", lw)
            if lw in ALLOW or not lw:
                continue
            if lw not in src:
                return f"name '{w}' not in the cited source"
        toks = re.findall(r"[a-z0-9$%']+", norm(bare))
        sent_grams = _ngrams(toks, COPY_NGRAM)
        for i in ids:
            if sent_grams & self.grams.get(i, set()):
                return "copies 11+ words from the source"
        return None


_ODD_SPACES = str.maketrans({"\u2011": "-", "\u2010": "-", "\u202f": " ", "\u00a0": " ", "\u2009": " "})


def tidy(v):
    """Models like gpt-oss use non-breaking hyphens / narrow spaces ("3\u20115 miles"): plain ones read and match better."""
    if isinstance(v, str):
        return v.translate(_ODD_SPACES)
    if isinstance(v, list):
        return [tidy(x) for x in v]
    if isinstance(v, dict):
        return {k: tidy(x) for k, x in v.items()}
    return v


def validate_issue(data: dict, picked: list[StoryIn], checker: Checker) -> tuple[dict, list[str]]:
    """Return (cleaned issue, problems). Failing sentences are removed from the cleaned issue."""
    data = tidy(data)
    problems: list[str] = []
    all_ids = [i["id"] for s in picked for i in s.items]

    def clean_text(part: str, text, need_cite=True, scope=None) -> str:
        keep = []
        for snt in sentences(str(text or "")):
            why = checker.check(snt, need_cite=need_cite, scope=scope)
            if why:
                problems.append(f"{part}: \"{snt[:90]}\" — {why}")
            else:
                keep.append(snt)
        return " ".join(keep)

    def clean_title_(part: str, text, scope) -> str:
        t = re.sub(r"\s+", " ", CITE_RE.sub("", str(text or ""))).strip()
        why = checker.check(t, need_cite=False, scope=scope) if t else None
        if why:
            problems.append(f"{part}: \"{t[:90]}\" — {why}")
            return ""
        return t

    by_role = {r: [s for s in picked if s.role == r] for r in ("TOP", "TALKED_ABOUT", "QUICK_HIT", "SKIP")}
    out: dict = {}
    out["subject"] = clean_title_("subject", data.get("subject"), all_ids)[:90]
    out["hook"] = clean_text("hook", data.get("hook"))
    top_ids = [i["id"] for s in by_role["TOP"] for i in s.items]
    t = data.get("top_story") or {}
    out["top_story"] = {"headline": clean_title_("top headline", t.get("headline"), top_ids),
                        "paragraphs": [p for p in (clean_text("top story", x) for x in (t.get("paragraphs") or [])[:4]) if p],
                        "why_it_matters": clean_text("why it matters", t.get("why_it_matters"))}
    talked_ids = [i["id"] for s in by_role["TALKED_ABOUT"] for i in s.items]
    out["talking_about"] = []
    for x in (data.get("talking_about") or [])[:MAX_TALKING]:
        if not isinstance(x, dict):
            continue
        text = clean_text("talking about", x.get("text"))
        if text:
            out["talking_about"].append({"headline": clean_title_("talking headline", x.get("headline"), talked_ids),
                                         "text": text})
    out["quick_hits"] = [{"text": tx} for tx in (clean_text("quick hit", (x or {}).get("text") if isinstance(x, dict) else x)
                                                 for x in (data.get("quick_hits") or [])[:MAX_QUICK]) if tx]
    out["safe_to_skip"] = [{"text": tx} for tx in (clean_text("safe to skip", (x or {}).get("text") if isinstance(x, dict) else x)
                                                   for x in (data.get("safe_to_skip") or [])[:MAX_SKIP]) if tx]
    close = re.sub(r"\s+", " ", CITE_RE.sub("", str(data.get("close") or ""))).strip()
    out["close"] = close if close and not NUM_RE.search(close) else "That's all for today."
    if not out["subject"]:
        out["subject"] = (out["top_story"]["headline"] or (picked[0].headline if picked else "Today's digest"))[:90]
    if not out["top_story"]["paragraphs"]:
        problems.append("top story: nothing left after validation")
    return out, problems


def cited_items(issue: dict) -> list[int]:
    blob = json.dumps(issue, ensure_ascii=False)
    return sorted({int(x) for x in CITE_RE.findall(blob)})


# ---------------------------------------------------------------- rendering
# Our issue reads as our own publication: the [iN] ids never appear and no newsletter is named. In "original"
# link mode a paragraph links to the ORIGINAL article it is about (the company post, the paper, the news site) —
# never to a newsletter's own pages or click-trackers.
NEWSLETTER_DOMAINS = ("superhuman.ai", "joinsuperhuman.ai", "tldr.tech", "tldrnewsletter.com", "theneurondaily.com",
                      "theneuron.ai", "substack.com", "beehiiv.com", "pragmaticengineer.com", "importai.net",
                      "jack-clark.net", "morningbrew.com", "sailthru.com", "list-manage.com", "kit.com",
                      "convertkit.com", "mailchimp.com")


def is_newsletter_url(url: str, extra: tuple[str, ...] = ()) -> bool:
    from .clean import domain_of, link_type
    if not url or link_type(url) != "source":
        return True                                    # trackers, mailto, social chrome
    d = domain_of(url)
    return any(d == x or d.endswith("." + x) for x in NEWSLETTER_DOMAINS + tuple(extra))


def plain(text: str) -> str:
    t = CITE_RE.sub("", text or "")
    t = re.sub(r"\s+([.!?,;:])", r"\1", t)
    return re.sub(r"\s{2,}", " ", t).strip()


@dataclass
class Linker:
    urls: dict[int, str]               # item id -> best URL (original article) or ""
    mode: str = "original"             # original | none
    used: set = field(default_factory=set)

    def for_text(self, text: str) -> str:
        """URL of the first cited item with an original-article link not used yet ('' if none / mode none)."""
        if self.mode != "original":
            return ""
        for i in CITE_RE.findall(text or ""):
            u = self.urls.get(int(i), "")
            if u and u not in self.used:
                self.used.add(u)
                return u
        return ""


FONT = "font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;"


def _p(text: str, link: str, style: str) -> str:
    more = (f' <a href="{htmlmod.escape(link)}" style="color:#2747E8;text-decoration:none;font-weight:600;'
            f'white-space:nowrap;">Read more</a>') if link else ""
    return f'<p style="{style}">{htmlmod.escape(plain(text))}{more}</p>'


def render_md(issue: dict, linker: Linker, title: str, date_label: str) -> str:
    def c(t: str) -> str:
        u = linker.for_text(t)
        return plain(t) + (f" [Read more]({u})" if u else "")
    out = [f"**{title}** — {date_label}", "", f"# {issue['subject']}", "", plain(issue["hook"]), "",
           "## Today's big story", f"### {issue['top_story']['headline']}"]
    out += [c(p) + "\n" for p in issue["top_story"]["paragraphs"]]
    if issue["top_story"]["why_it_matters"]:
        out.append(f"**Why it matters:** {c(issue['top_story']['why_it_matters'])}\n")
    if issue["talking_about"]:
        out.append("## Everyone's talking about")
        out += [(f"**{x['headline']}** " if x["headline"] else "") + c(x["text"]) + "\n" for x in issue["talking_about"]]
    if issue["quick_hits"]:
        out.append("## Quick hits")
        out += [f"- {c(x['text'])}" for x in issue["quick_hits"]] + [""]
    if issue["safe_to_skip"]:
        out.append("## Safe to skip")
        out += [f"- {c(x['text'])}" for x in issue["safe_to_skip"]] + [""]
    out.append(issue["close"])
    return "\n".join(out).strip() + "\n"


def render_html(issue: dict, linker: Linker, title: str, date_label: str, footer: str) -> str:
    body = f"{FONT}margin:0 0 14px;font-size:16px;line-height:1.6;color:#2B3445;"

    def section(heading: str, inner: str, bg: str = "#FFFFFF") -> str:
        return (f'<tr><td class="px" style="padding:26px 36px 6px;background:{bg};{FONT}">'
                f'<h3 style="margin:0 0 14px;padding-top:22px;border-top:1px solid #DDE3EA;font-size:18px;'
                f'line-height:1.3;font-weight:800;letter-spacing:-0.01em;color:#0F1B2D;">{heading}</h3>{inner}</td></tr>')

    top = issue["top_story"]
    why = top["why_it_matters"]
    why_block = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
                 f'style="margin:6px 0 10px;"><tr><td style="border-left:3px solid #2747E8;padding:4px 0 4px 16px;'
                 f'{FONT}font-size:16px;line-height:1.6;color:#0F1B2D;"><strong>Why it matters.</strong> '
                 f'{htmlmod.escape(plain(why))}</td></tr></table>') if why else ""
    talking = "".join(
        f'<p style="{FONT}margin:0 0 4px;font-size:17px;font-weight:700;color:#0F1B2D;">{htmlmod.escape(x["headline"])}</p>'
        + _p(x["text"], linker.for_text(x["text"]), body) for x in issue["talking_about"])

    def bullets(xs, color="#2B3445", size="16px") -> str:
        rows = "".join(
            f'<tr><td valign="top" style="width:18px;padding:9px 0 0;"><span style="display:block;width:7px;height:7px;'
            f'background:#2747E8;"></span></td><td style="{FONT}padding:0 0 12px;font-size:{size};line-height:1.6;'
            f'color:{color};">{htmlmod.escape(plain(x["text"]))}'
            + ((lambda u: f' <a href="{htmlmod.escape(u)}" style="color:#2747E8;text-decoration:none;font-weight:600;'
                          f'white-space:nowrap;">Read more</a>' if u else "")(linker.for_text(x["text"])))
            + '</td></tr>' for x in xs)
        return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table>'

    tpl = Template(resources.files("nlagg").joinpath("templates/issue.html").read_text(encoding="utf-8"))
    return tpl.substitute(
        subject=htmlmod.escape(issue["subject"]), preheader=htmlmod.escape(plain(issue["hook"]))[:180],
        wordmark=htmlmod.escape(title), date_label=htmlmod.escape(date_label),
        hook=htmlmod.escape(plain(issue["hook"])), top_headline=htmlmod.escape(top["headline"]),
        top_paragraphs="".join(_p(p, linker.for_text(p), body) for p in top["paragraphs"]),
        why_block=why_block,
        talking_block=section("Everyone's talking about", talking) if talking else "",
        quick_block=section("Quick hits", bullets(issue["quick_hits"])) if issue["quick_hits"] else "",
        skip_block=section("Safe to skip", bullets(issue["safe_to_skip"], "#667085", "15px"))
        if issue["safe_to_skip"] else "",
        close=htmlmod.escape(issue["close"]), footer=htmlmod.escape(footer))


# ---------------------------------------------------------------- orchestration
def run_compose(cfg: Config, *, segment: str | None = None, issue_date: str | None = None, client=None,
                resolve_links: bool = True, fetch=None, force: bool = False) -> ComposeResult:
    segment = segment or (cfg.active_segments[0] if cfg.active_segments else "1-TechAI")
    issue_date = issue_date or datetime.now(timezone.utc).date().isoformat()
    comp = cfg.raw.get("compose", {})
    title = comp.get("title", {}).get(segment, f"The {segment.split('-', 1)[-1]} Digest")
    label = comp.get("labels", {}).get(segment, segment.split("-", 1)[-1])
    res = ComposeResult()
    conn = db.connect(cfg.db_path)
    existing = conn.execute("SELECT id, status FROM issues_out WHERE segment = ? AND issue_date = ?",
                            (segment, issue_date)).fetchone()
    if existing and existing["status"] in ("approved", "sent") and not force:
        res.skipped_reason = f"issue {issue_date} is already {existing['status']}"
        conn.close()
        return res
    picked = assign_roles(load_stories(conn, segment, issue_date))
    if not picked:
        res.skipped_reason = "no stories for this date (run `nlagg cluster` first)"
        conn.close()
        return res
    st = LLMSettings.from_config(cfg.llm)
    client = client or ChatClient(st, conn)
    checker = Checker({i["id"]: i["text"] for s in picked for i in s.items},
                      {i["newsletter"] for s in picked for i in s.items})
    msgs = build_messages(picked, title, label, st.prompt_version)
    data = client.chat_json(st.write_model, msgs, temperature=0.4, max_tokens=2200)
    issue, problems = validate_issue(data, picked, checker)
    if problems:
        res.retried = True
        fix = msgs + [{"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
                      {"role": "user", "content": "The checker rejected these sentences:\n- " + "\n- ".join(problems[:25])
                       + "\nRewrite the whole JSON so every sentence passes. Same keys."}]
        try:
            data2 = client.chat_json(st.write_model, fix, temperature=0.2, max_tokens=2200)
            issue2, problems2 = validate_issue(data2, picked, checker)
            if len(problems2) <= len(problems):
                issue, problems = issue2, problems2
        except LLMError as e:
            log.warning("compose retry failed: %s", e)
    res.dropped = problems
    cited = cited_items(issue)
    links_mode = str(comp.get("links", "original")).lower()
    urls: dict[int, str] = {}
    if links_mode == "original" and cited:
        if resolve_links:
            from .links import resolve_for_items
            urls = resolve_for_items(conn, cited, **({"fetch": fetch} if fetch else {}))
        else:
            urls = {r["id"]: r["url"] for r in conn.execute(
                f"SELECT id, url FROM items WHERE id IN ({','.join('?' * len(cited))})", cited)}
        urls = {i: u for i, u in urls.items() if u and not is_newsletter_url(u)}
    linker_md, linker_html = Linker(urls, links_mode), Linker(urls, links_mode)
    footer = comp.get("footer", {}).get(segment) if isinstance(comp.get("footer"), dict) else comp.get("footer")
    footer = footer or f"{title} is a daily briefing on {label}. Reply to this email to send feedback or to unsubscribe."
    date_label = date.fromisoformat(issue_date).strftime("%A, %d %B %Y").replace(" 0", " ")
    out_dir = cfg.repo_root / "pipeline" / "out" / "issues" / segment
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path, html_path = out_dir / f"{issue_date}.md", out_dir / f"{issue_date}.html"
    md_path.write_text(render_md(issue, linker_md, title, date_label), encoding="utf-8")
    html_path.write_text(render_html(issue, linker_html, title, date_label, footer), encoding="utf-8")
    report = {"problems_dropped": problems, "retried": res.retried, "cited_items": cited,
              "roles": {s.id: s.role for s in picked}, "subject": issue["subject"]}
    with conn:
        conn.execute(
            """INSERT INTO issues_out (segment, issue_date, status, html_path, md_path, story_ids, validator_report,
                                       llm_model, prompt_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(segment, issue_date) DO UPDATE SET status='draft', html_path=excluded.html_path,
                 md_path=excluded.md_path, story_ids=excluded.story_ids, validator_report=excluded.validator_report,
                 llm_model=excluded.llm_model, prompt_version=excluded.prompt_version, created_at=excluded.created_at""",
            (segment, issue_date, "draft", str(html_path), str(md_path), json.dumps([s.id for s in picked]),
             json.dumps(report, ensure_ascii=False), st.write_model, st.prompt_version,
             datetime.now(timezone.utc).isoformat(timespec="seconds")))
    res.issue_id = conn.execute("SELECT id FROM issues_out WHERE segment = ? AND issue_date = ?",
                                (segment, issue_date)).fetchone()[0]
    conn.close()
    res.md_path, res.html_path, res.subject, res.stories = md_path, html_path, issue["subject"], len(picked)
    return res
