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
    r"^\s*(we're|we are) hiring|^\s*jobs? board|^\s*send us (a )?(tip|feedback)",
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


def split_blocks(blocks: list[Block], subject: str = "") -> SplitResult:
    blocks = [b for b in blocks if b.kind != "hr"]
    if not blocks:
        return SplitResult([], "empty")

    items: list[Item] = []
    section: str | None = None
    cur: Item | None = None

    def close() -> None:
        nonlocal cur
        if cur is not None and (cur.title or cur.body.strip()):
            items.append(cur)
        cur = None

    for i, b in enumerate(blocks):
        nxt = blocks[i + 1] if i + 1 < len(blocks) else None
        if b.words <= TITLE_MAX_WORDS and DROP_ITEM_RE.search(b.text):
            # "Love TLDR? Tell your friends…", "Want to advertise…": its own item, dropped below,
            # so the boilerplate never gets glued onto the previous story.
            close()
            cur = Item(position=0, title=b.text, body="", url=None, section=section, kind="drop")
            continue
        if is_section_label(b, nxt):
            close()
            section = READ_TIME_RE.sub("", b.text).strip(" :–-")
            continue
        if is_title_like(b) and (_has_link(b) or (nxt is not None and not is_title_like(nxt))):
            close()
            cur = Item(position=0, title=b.text, body="", url=_first_source_link(b.links),
                       section=section, links=list(b.links))
            continue
        if cur is None:
            cur = Item(position=0, title=None, body="", url=None, section=section, kind="intro")
        cur.body = (cur.body + "\n\n" + b.text).strip() if cur.body else b.text
        cur.links += b.links
        if cur.url is None:
            cur.url = _first_source_link(b.links)
    close()

    # Drop boilerplate items (referral, feedback, hiring, advertise). Judge an item by its title
    # (or the start of an untitled body) so a story is never dropped for what follows it.
    items = [it for it in items
             if it.kind != "drop" and not DROP_ITEM_RE.search(it.title or it.body[:120])
             and (it.body.strip() or it.url)]          # a bare title with no text and no link is a label
    stories = [it for it in items if it.kind == "story"]
    total_words = sum(b.words for b in blocks)
    all_links = [ln for b in blocks for ln in b.links]

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


def _finish(items: list[Item]) -> list[Item]:
    for n, it in enumerate(items, 1):
        it.position = n
        labelled = any(SPONSOR_LABEL_RE.search(x) for x in (it.section or "", it.title or ""))
        it.is_sponsor = int(labelled or bool(SPONSOR_BODY_RE.search(it.body[:200])))
        if it.title:
            it.title = it.title.strip()
    return items
