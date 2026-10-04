"""M2 — Split: one cleaned issue -> story items. Deterministic heuristics, no AI.

Handles the content shapes found in the 70-issue audit (PROJECT_CONTEXT §7):
- links-roundup (~52%) and brief/sectioned (~35%): a *title-like* block (heading, fully-bold
  line, or a short line that is/starts with a link) starts a new item; the following text
  blocks are its body. Short unlinked headings followed by a title are *section* labels.
- content essay: fewer than 2 titled items but a long body -> one 'essay' item.
- teaser/preview (truncated free Substack): short body + a "read more / keep reading" link
  -> one 'teaser' item pointing at the full post.
Sponsor items are flagged (is_sponsor=1), referral/share/job-board boilerplate is dropped,
and promo mails (sale/upgrade/webinar subjects with ≤2 stories) are flagged at message level.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .clean import Block, Link

TITLE_MAX_WORDS = 25
SECTION_MAX_WORDS = 8
ESSAY_MIN_WORDS = 150
TEASER_MAX_WORDS = 400

# Sponsor slots are *labelled*; match the labels, not the word "sponsor" in a news story.
SPONSOR_LABEL_RE = re.compile(          # checked on section + title
    r"\(sponsor(ed)?\)|^\s*sponsor(ed|s)?\s*$|^\s*sponsored\b|(word|message|note) from our (sponsor|partner)|"
    r"^\s*from our (sponsor|partner)s?\b|^\s*together with\b|^\s*presented by\b|^\s*brought to you by\b|"
    r"^\s*in partnership with\b|^\s*partner(ed)? (content|post)|^\s*advertisement\s*$|"
    r"\[ad\]|\(ad\)|^\s*ad\s*[:|]",
    re.I)
SPONSOR_BODY_RE = re.compile(           # checked on the first words of the body
    r"^\s*(together with|presented by|brought to you by|in partnership with|sponsored by|"
    r"this (issue|newsletter|edition) is (sponsored|brought to you) by)\b",
    re.I)
DROP_ITEM_RE = re.compile(
    r"refer (a|your) friend|share (tldr|the neuron|this newsletter|.{0,20} with (a friend|your friends))|"
    r"invite (your )?(friends|colleagues)|referral (link|program|rewards)|^\s*want to advertise|"
    r"^\s*advertise with|^\s*(love|enjoy(ing)?) (this|tldr|the newsletter)|^\s*how did we do|"
    r"rate (this|today's) (issue|newsletter)|^\s*(was|did) (this|you enjoy)|^\s*feedback\b|"
    r"^\s*(we're|we are) hiring|^\s*jobs? board|^\s*send us (a )?(tip|feedback)|"
    # seen in the first real capture (TLDR, The Neuron, Superhuman, Substack)
    r"^\s*advertise to\b|want to work at|track your referrals|^\s*https?://\S+\s*$|"
    r"^\s*(like|comment|restack|share)\s*$|^\s*share the \w+|^\s*want more\?|we just launched|"
    r"^\s*a cat[’']s commentary|^\s*watch and/or listen|\bat tldr \(\$",   # TLDR's own job ads
    re.I)
TEASER_LINK_RE = re.compile(
    r"(read|keep|continue) (more|reading|the full|the rest)|read (in|on) (the )?app|"
    r"(full|rest of the) (story|post|article)|upgrade to (read|paid)|subscribe to (read|keep reading)",
    re.I)
# Call-to-action links: never a story title even when the whole line is a link
CTA_RE = re.compile(
    r"^\s*(start (a |your )?(free )?trial|subscribe( now| here)?|upgrade( now)?|sign up( now)?|"
    r"get (the )?app|learn more|try (it )?(for )?free|get started|book a demo|get a demo|"
    r"click (here|to share)|read more|keep reading.*|continue reading.*|read the full.*)\s*[.!→>»]*\s*$",
    re.I)
PROMO_SUBJECT_RE = re.compile(
    r"\d+\s?% off|\bsale\b|\bdiscount\b|\bdeal(s)? (ends|end)|last chance|limited[- ]time|"
    r"upgrade to (pro|premium|paid|plus)|go (pro|premium|paid)|black friday|cyber monday|"
    r"free trial|special offer|\bwebinar\b|register (now|today)|\bpromo code\b|exclusive offer",
    re.I)
# Substack byline: "<Author name>" followed by a date line ("Oct 1 READ IN APP")
DATE_LINE_RE = re.compile(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? \d{1,2}\b", re.I)
# Sub-stories inside one item: "1. Title: text" or "🤝 Label: text" entries (Superhuman, The Neuron)
NUMBERED_ENTRY_RE = re.compile(r"^\s*\d{1,2}[.)]\s+(?P<title>[^:]{3,140}?):\s+(?P<body>\S.*)$", re.S)
# Label entries must start with an emoji/symbol ("🤝 Diplomacy GPT: ..."): plain "Why this matters:" style
# labels are facets of ONE story (The Neuron, Import AI), not separate stories.
LABEL_ENTRY_RE = re.compile(r"^(?P<pre>[^\w\s]{1,4})\s*(?P<title>[A-Z0-9][^:.!?]{2,60}?):\s+(?P<body>\S.*)$", re.S)
NOT_A_LABEL = {"photo", "photos", "source", "sources", "prompt", "note", "update", "image", "video",
               "credit", "via", "bonus", "ps", "p.s", "tl;dr", "tldr", "why it matters", "the catch"}
ENTRY_MIN_WORDS = 15
INTRO_MIN_WORDS = 12          # shorter intros are masthead leftovers ("TLDR", a date)
BARE_SPONSOR_RE = re.compile(r"\W*(together with|presented by|brought to you by|in partnership with|"
                             r"sponsored by|from our (sponsor|partner)s?)\W*", re.I)
# Button wording: a short one-link line with one of these words is a call-to-action, not a story
CTA_HINT_RE = re.compile(r"\b(here|today|now|register|request|read|try|get|start|join|download|claim|"
                         r"apply|invest|book|watch|listen|sign|seat|demo|report|blueprint|see|discover|"
                         r"explore|learn|check)\b", re.I)
# Section names that are reference material, not stories (podcast notes)
NOT_A_STORY_TITLE_RE = re.compile(r"^\W*(timestamps|references|show notes|transcript|chapters)\W*$|"
                                  r"^\s*\d{1,2}:\d{2}(:\d{2})?\s*$", re.I)      # podcast durations
ENTRIES_MIN_SHARE = 0.6       # split into entries only when entries are most of the item (a list, not an essay)
# Paid-post cut-off: nothing after it is content (Substack previews)
PAYWALL_RE = re.compile(r"to unlock the rest|this post is for paid subscribers|"
                        r"keep reading with a \d+-day free trial", re.I)
# Substack web-post header ("Oct 1 READ IN APP"): the email is ONE post; its sub-headings are
# sections of that post, not separate stories (owner decision 2026-10-05).
READ_IN_APP_RE = re.compile(r"\bread in app\b", re.I)
SPONSOR_INTRO_MIN_WORDS = 20  # an untitled body this long under a sponsor label IS the sponsor slot
CAPTION_MAX_WORDS = 40        # a list heading whose only text is a media caption


def _is_cta_block(b: Block) -> bool:
    """A one-link button line ("Register your interest here", "Read the report") — body, never a title."""
    return (b.link_full and b.kind != "heading" and b.words <= 8 and not READ_TIME_RE.search(b.text)
            and bool(CTA_HINT_RE.search(b.text)))


def _is_cta(it: "Item") -> bool:
    words = len((it.title or "").split())
    return (it.kind == "story" and it.title_is_link and not it.title_is_heading
            and len(it.body.split()) < 4 and words <= 14 and (words <= 6 or bool(CTA_HINT_RE.search(it.title or ""))))
READ_TIME_RE = re.compile(r"\(\s*\d+\s*(minute|min)\s*read\s*\)|\(\s*(github repo|website|sponsor)\s*\)", re.I)


@dataclass
class Item:
    position: int
    title: str | None
    body: str
    url: str | None
    section: str | None = None
    kind: str = "story"            # story | intro | essay | teaser
    is_sponsor: int = 0
    links: list[Link] = field(default_factory=list)
    title_is_link: bool = False                                   # title block was one link (CTA-like)
    title_is_heading: bool = False
    blocks: list[Block] = field(default_factory=list, repr=False)  # body blocks (not persisted)

    @property
    def word_count(self) -> int:
        return len(((self.title or "") + " " + self.body).split())


@dataclass
class SplitResult:
    items: list[Item]
    shape: str                     # roundup | essay | teaser | empty
    is_promo: int = 0


def _first_source_link(links: list[Link]) -> str | None:
    for ln in links:
        if ln.kind == "source":
            return ln.url
    for ln in links:
        if ln.kind == "tracked":
            return ln.url
    return None


def is_title_like(b: Block) -> bool:
    if b.kind == "hr" or b.words == 0 or b.words > TITLE_MAX_WORDS:
        return False
    if CTA_RE.search(b.text) or TEASER_LINK_RE.search(b.text):
        return False
    if b.text.rstrip().endswith((".", "?", "!", ":")) and not (b.kind == "heading" or b.link_full):
        # a sentence, not a headline (headings/linked titles may end with '?' or '!')
        if not READ_TIME_RE.search(b.text):
            return False
    if (b.link_full and not b.bold_full and b.kind != "heading" and b.text.rstrip().endswith(".")
            and not READ_TIME_RE.search(b.text)):
        return False        # a plain linked sentence (embedded post / tweet card), not a headline
    if b.kind == "heading" or b.bold_full or b.link_full:
        return True
    # "Title (5 minute read)" where only the title part is linked/bold
    return (b.lead_link_frac >= 0.6 or b.lead_bold_frac >= 0.85) and b.words <= TITLE_MAX_WORDS


def _has_link(b: Block) -> bool:
    return any(ln.kind in ("source", "tracked") for ln in b.links)


def is_section_label(b: Block, nxt: Block | None) -> bool:
    """Short unlinked heading whose next block is itself a title (e.g. 'BIG TECH & STARTUPS')."""
    if not is_title_like(b) or _has_link(b) or b.words > SECTION_MAX_WORDS:
        return False
    return nxt is not None and is_title_like(nxt)


def classify_promo(subject: str, n_stories: int) -> int:
    return int(bool(PROMO_SUBJECT_RE.search(subject or "")) and n_stories <= 2)


def _entry(b: Block) -> tuple[str, str] | None:
    """(title, body) if the block is a self-contained news entry, else None."""
    if b.words < ENTRY_MIN_WORDS:
        return None
    m = NUMBERED_ENTRY_RE.match(b.text)
    if not m:
        m = LABEL_ENTRY_RE.match(b.text)
        if (not m or m.group("title").strip().lower() in NOT_A_LABEL
                or not any(ord(ch) > 0x2000 for ch in m.group("pre"))):      # emoji / pictograph prefix
            return None
    title = m.group("title").strip()
    if len(title.split()) > 20:
        return None
    return title, m.group("body").strip()


def _split_entries(items: list[Item]) -> list[Item]:
    """Break an item whose body is a list of >= 2 news entries into one item per entry."""
    out: list[Item] = []
    for it in items:
        entries = [(b, _entry(b)) for b in it.blocks]
        entry_words = sum(b.words for b, e in entries if e is not None)
        if (it.kind != "story" or it.is_sponsor or sum(e is not None for _, e in entries) < 2
                or entry_words < ENTRIES_MIN_SHARE * max(1, sum(b.words for b in it.blocks))):
            out.append(it)
            continue
        rest = [b for b, e in entries if e is None]
        if sum(b.words for b in rest) >= 15:          # keep the parent if it has its own text
            parent = Item(0, it.title, "\n\n".join(b.text for b in rest),
                          _first_source_link([ln for b in rest for ln in b.links]) or it.url,
                          section=it.section, links=[ln for b in rest for ln in b.links], blocks=rest)
            out.append(parent)
        for b, e in entries:
            if e is None:
                continue
            t, body = e
            out.append(Item(0, t, body, _first_source_link(b.links), section=it.title or it.section,
                            links=list(b.links), blocks=[b]))
    return out


def split_blocks(blocks: list[Block], subject: str = "") -> SplitResult:
    # separators ("*******", "———") carry no text
    blocks = [b for b in blocks if b.kind != "hr" and re.search(r"[0-9A-Za-z]", b.text)]
    if not blocks:
        return SplitResult([], "empty")

    items: list[Item] = []
    section: str | None = None
    section_before_sponsor: str | None = None   # sponsor labels cover ONE item, then this comes back
    sponsor_section_used = False
    cur: Item | None = None

    def close() -> None:
        nonlocal cur
        if cur is not None and (cur.title or cur.body.strip()):
            items.append(cur)
        cur = None

    for i, b in enumerate(blocks):
        nxt = blocks[i + 1] if i + 1 < len(blocks) else None
        if b.words <= TITLE_MAX_WORDS and PAYWALL_RE.search(b.text):
            break                                # paid preview: the rest is the upsell
        if b.words <= TITLE_MAX_WORDS and DROP_ITEM_RE.search(b.text):
            # "Love TLDR? Tell your friends…", "Want to advertise…": its own item, dropped below,
            # so the boilerplate never gets glued onto the previous story.
            close()
            cur = Item(position=0, title=b.text, body="", url=None, section=section, kind="drop")
            continue
        # Substack byline: author name (often a link) followed by "Oct 1 READ IN APP ..."
        is_byline = nxt is not None and bool(DATE_LINE_RE.match(nxt.text)) and b.words <= 5
        nxt2 = blocks[i + 2] if i + 2 < len(blocks) else None
        before_byline = (nxt is not None and nxt.words <= 5 and nxt2 is not None
                         and bool(DATE_LINE_RE.match(nxt2.text)))     # a post's subtitle, not a section
        # A bare sponsor label ("Together With", "Presented by") always labels the next item
        bare_sponsor = bool(BARE_SPONSOR_RE.fullmatch(b.text))     # may carry the sponsor's logo link
        if (bare_sponsor or (is_section_label(b, nxt) and not before_byline)) and not is_byline \
                and len(re.sub(r"[^0-9A-Za-z]", "", b.text)) >= 3:      # not a trivia answer like "B"
            close()
            label = READ_TIME_RE.sub("", b.text).strip(" :–-")
            if SPONSOR_LABEL_RE.search(label):
                if not (section and SPONSOR_LABEL_RE.search(section)):
                    section_before_sponsor = section
                sponsor_section_used = False
            section = label
            continue
        # social handles ("@poteto") and button lines are never titles
        not_title = is_byline or before_byline or _is_cta_block(b) or b.text.lstrip().startswith("@")
        in_sponsor = bool(section and SPONSOR_LABEL_RE.search(section))
        if (in_sponsor and cur is not None and cur.title is None
                and len(cur.body.split()) >= SPONSOR_INTRO_MIN_WORDS):
            sponsor_section_used = True          # an untitled ad ("Brought to you by" + list) was the slot
        if (in_sponsor and sponsor_section_used and b.kind != "heading"
                and cur is not None and cur.section == section):
            not_title = True                     # bold feature lines inside the ad ("Fast: …") stay in it
        if is_title_like(b) and not not_title and (_has_link(b) or (nxt is not None and not is_title_like(nxt))):
            close()
            if in_sponsor:
                if sponsor_section_used:                 # the sponsor slot is over
                    section = section_before_sponsor
                else:
                    sponsor_section_used = True
            cur = Item(position=0, title=b.text, body="", url=_first_source_link(b.links),
                       section=section, links=list(b.links), title_is_link=b.link_full,
                       title_is_heading=b.kind == "heading")
            continue
        if cur is None:
            cur = Item(position=0, title=None, body="", url=None, section=section, kind="intro")
        cur.body = (cur.body + "\n\n" + b.text).strip() if cur.body else b.text
        cur.blocks.append(b)
        cur.links += b.links
        if cur.url is None:
            cur.url = _first_source_link(b.links)
    close()

    # Drop boilerplate items (referral, feedback, hiring, advertise). Judge an item by its title
    # (or the start of an untitled body) so a story is never dropped for what follows it.
    items = [it for it in items
             if it.kind != "drop" and not DROP_ITEM_RE.search(it.title or it.body[:120])
             and (it.body.strip() or it.url)          # a bare title with no text and no link is a label
             and not (it.kind == "intro" and len(it.body.split()) < INTRO_MIN_WORDS)
             and not NOT_A_STORY_TITLE_RE.match(it.title or "")
             # masthead / name line with nothing under it ("The Pragmatic Engineer")
             and not (it.kind == "story" and not it.body.strip() and len((it.title or "").split()) <= 3
                      and not READ_TIME_RE.search(it.title or ""))
             # truncated card with nothing under it ("Building Codex with Tibo Sott…")
             and not (it.kind == "story" and not it.body.strip() and (it.title or "").rstrip().endswith(("…", "...")))]

    # Call-to-action buttons ("Register here.", "Start your 30-day trial today.", "Read the report")
    # are one-link lines with no text of their own: fold them into the item they belong to.
    merged: list[Item] = []
    for it in items:
        if merged and _is_cta(it) and merged[-1].kind in ("story", "intro"):
            merged[-1].links += it.links
            continue
        merged.append(it)
    items = _drop_list_headings(_split_entries(_mark_sponsors(merged)))
    total_words = sum(b.words for b in blocks)
    all_links = [ln for b in blocks for ln in b.links]

    # Substack web post: one post = one essay item; sub-headings stay inside it as "## " sections.
    # Sponsor slots stay separate (flagged), so ad text never lands in the post.
    if any(READ_IN_APP_RE.search(b.text) for b in blocks[:15]):
        post = [it for it in items if not it.is_sponsor]
        ads = [it for it in items if it.is_sponsor]
        if post:
            first = next((it.title for it in post if it.title), None)
            title = subject or first                     # essays: title = subject,
            if first and subject and subject.rstrip().endswith(("…", "...")):
                title = first                            # unless the sender cut the subject short
            parts = []
            for it in post:
                head = f"## {it.title}\n\n" if it.title and it.title != title else ""
                parts.append((head + it.body).strip())
            essay = Item(0, title, "\n\n".join(p for p in parts if p),
                         next((it.url for it in post if it.url), None), kind="essay",
                         links=[ln for it in post for ln in it.links])
            return SplitResult(_finish([essay] + ads), "essay", classify_promo(subject, 1))

    stories = [it for it in items if it.kind == "story"]

    # Teaser: short, with a read-more link -> one item pointing at the full post
    teaser_link = next((ln for ln in all_links if TEASER_LINK_RE.search(ln.text)), None)
    if teaser_link and total_words <= TEASER_MAX_WORDS and len(stories) <= 2:
        body = "\n\n".join(b.text for b in blocks
                             if not (TEASER_LINK_RE.search(b.text) or CTA_RE.search(b.text)))
        it = Item(0, subject or None, body, teaser_link.url, kind="teaser", links=all_links)
        return SplitResult(_finish([it]), "teaser", classify_promo(subject, 1))

    # Essay: not a roundup -> the whole issue is one item
    if len(stories) < 2 and total_words >= ESSAY_MIN_WORDS:
        body = "\n\n".join(b.text for b in blocks)
        it = Item(0, subject or (stories[0].title if stories else None), body,
                  _first_source_link(all_links), kind="essay", links=all_links)
        return SplitResult(_finish([it]), "essay", classify_promo(subject, 1))

    return SplitResult(_finish(items), "roundup" if stories else "empty",
                       classify_promo(subject, len(stories)))


def _drop_list_headings(items: list[Item]) -> list[Item]:
    """A list heading whose own text is only a photo caption ("The most important news in robotics
    this week" + "Click here to see the clip. Photo: X") labels the items after it; it is not a story."""
    out: list[Item] = []
    for i, it in enumerate(items):
        nxt = items[i + 1] if i + 1 < len(items) else None
        if (it.kind == "story" and it.title and not it.is_sponsor and nxt is not None
                and nxt.section == it.title and len(it.body.split()) <= CAPTION_MAX_WORDS):
            continue
        out.append(it)
    return out


def _mark_sponsors(items: list[Item]) -> list[Item]:
    for it in items:
        labelled = any(SPONSOR_LABEL_RE.search(x) for x in (it.section or "", it.title or ""))
        it.is_sponsor = int(labelled or bool(SPONSOR_BODY_RE.search(it.body[:200])))
    return items


def _finish(items: list[Item]) -> list[Item]:
    _mark_sponsors(items)
    for n, it in enumerate(items, 1):
        it.position = n
        if it.title:
            it.title = it.title.strip()
    return items
