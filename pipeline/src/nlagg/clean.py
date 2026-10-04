"""M2 — Clean: newsletter HTML -> ordered text blocks with their links. Deterministic, no AI.

Newsletters are table-based HTML. We walk the DOM and emit one Block per block-level run of text
(p, div, td, li, h1-h6, ...), remembering for each block whether it is a heading, fully bold, or
fully one link — those signals drive the story splitter (split.py).

Also here: link canonicalisation (unwrap redirect wrappers that embed the target URL, strip
tracking params) and removal of hidden preheaders, tracking pixels and boilerplate/footer text.
Opaque click-trackers (beehiiv, substack, convertkit, ...) are kept as-is and marked `tracked`:
resolving them needs a network request (see PROJECT_CONTEXT §12, open question).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

# ------------------------------------------------------------------ links
TRACKING_PARAM_PREFIXES = ("utm_", "mc_", "_hs", "oly_", "vero_", "pk_", "mtm_")
# Conservative on purpose: only params that are tracking-only everywhere. Generic short keys
# (e, l, j, id, ref, ...) are kept because some sites need them to resolve the page.
TRACKING_PARAMS = {
    "mkt_tok", "ck_subscriber_id", "fbclid", "gclid", "dclid", "msclkid", "igshid",
    "_bhlid", "last_resource_guid", "elqtrackid", "elqtrack", "trkcampaign",
}
# Query keys that often carry the real destination inside a redirect wrapper
REDIRECT_KEYS = ("url", "u", "q", "redirect", "redirect_url", "target", "dest", "destination", "link", "r")
# Known opaque click-tracker hosts (destination not recoverable without a network request)
TRACKER_HOST_RE = re.compile(
    r"(^|\.)(link\.mail\.beehiiv\.com|mail\.beehiiv\.com|email\.mg\.[a-z0-9.-]+|"
    r"click\.convertkit-mail\d*\.com|convertkit-mail\d*\.com|list-manage\.com|sendgrid\.net|"
    r"ct\.sendgrid\.net|links\.[a-z0-9-]+\.com|click\.[a-z0-9-]+\.com|track\.[a-z0-9-]+\.com|"
    r"tracking\.[a-z0-9-]+\.com|r\.[a-z0-9-]+\.com|email\.[a-z0-9-]+\.com|lnks\.gd|"
    r"cl\.exct\.net|hubspotlinks\.com|mailchi\.mp|mandrillapp\.com|rs6\.net|t\.co|lnkd\.in)$",
    re.I,
)
SOCIAL_HOST_RE = re.compile(
    r"(^|\.)(twitter\.com|x\.com|facebook\.com|instagram\.com|linkedin\.com|threads\.net|"
    r"tiktok\.com|bsky\.app)$", re.I)


def _strip_tracking(url: str) -> str:
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if not (k.lower() in TRACKING_PARAMS or k.lower().startswith(TRACKING_PARAM_PREFIXES))]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q, doseq=True), ""))


def _embedded_target(url: str) -> str | None:
    """Destination URL embedded in a redirect wrapper, if recoverable without a network call."""
    parts = urlsplit(url)
    # 1) ?url=https%3A%2F%2F... style
    for k, v in parse_qsl(parts.query):
        if k.lower() in REDIRECT_KEYS and re.match(r"https?(:|%3A)", v, re.I):
            return unquote(v) if "%3A" in v.upper()[:8] else v
    # 2) path-embedded, percent-encoded, e.g.
    #    tracking.tldrnewsletter.com/CL0/https:%2F%2Fexample.com%2Fx%3Fa=1/1/0100...
    #    (the encoded URL contains no literal '/', so it ends at the next '/')
    m = re.search(r"/(https?(?::|%3A)%2F%2F[^/]+)", parts.path, re.I)
    if m:
        return unquote(m.group(1))
    # 3) path-embedded, literal, e.g. host/redirect/https://example.com/x?y=1 -> rest of the URL
    m = re.search(r"/(https?:/{1,2}.+)$", parts.path, re.I)
    if m:
        inner = m.group(1).replace(":/", "://", 1) if not m.group(1).split(":", 1)[1].startswith("//") else m.group(1)
        return inner + (("?" + parts.query) if parts.query else "")
    return None


def canonical_url(url: str, max_unwrap: int = 3) -> str:
    url = (url or "").strip()
    if not re.match(r"https?://", url, re.I):
        return url
    for _ in range(max_unwrap):
        inner = _embedded_target(url)
        if not inner or inner == url:
            break
        url = inner
    return _strip_tracking(url)


def link_type(url: str) -> str:
    if url.lower().startswith("mailto:"):
        return "mailto"
    parts = urlsplit(url)
    host = parts.netloc.lower().split(":")[0]
    if SOCIAL_HOST_RE.search(host):
        return "social"
    if (TRACKER_HOST_RE.search(host) or host == "substack.com"
            or "/redirect/" in parts.path or parts.path.startswith(("/ss/c/", "/CL0/"))):
        return "tracked"
    return "source"


def domain_of(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


# ------------------------------------------------------------------ blocks
@dataclass
class Link:
    text: str
    url: str          # canonical
    kind: str         # source | tracked | social | mailto


@dataclass
class Block:
    kind: str                     # heading | text | li | hr
    text: str
    level: int = 0                # 1-6 for <h*>
    bold_full: bool = False       # every visible character is bold
    link_full: bool = False       # the whole block is one link
    links: list[Link] = field(default_factory=list)
    lead_link_frac: float = 0.0   # share of visible chars inside the link the block starts with
    lead_bold_frac: float = 0.0   # share of visible chars in the bold run the block starts with

    @property
    def words(self) -> int:
        return len(self.text.split())


BLOCK_TAGS = {
    "p", "div", "td", "th", "tr", "table", "tbody", "thead", "li", "ul", "ol", "h1", "h2", "h3",
    "h4", "h5", "h6", "section", "article", "header", "footer", "blockquote", "center", "hr",
    "br", "dl", "dt", "dd", "pre", "figure", "figcaption", "main", "aside", "nav",
}
DROP_TAGS = {"script", "style", "head", "title", "meta", "link", "noscript", "svg", "button", "form"}
BOLD_TAGS = {"b", "strong"}
HIDDEN_STYLE_RE = re.compile(
    r"display\s*:\s*none|mso-hide\s*:\s*all|visibility\s*:\s*hidden|max-height\s*:\s*0(px)?\s*(;|$)|"
    r"font-size\s*:\s*0(px)?\s*(;|$)|opacity\s*:\s*0(\.0+)?\s*(;|$)", re.I)
BOLD_STYLE_RE = re.compile(r"font-weight\s*:\s*(bold|[6-9]00)", re.I)
ZW_RE = re.compile(r"[​-‏  ‪-‮⁠-⁤﻿͏­]")

# Boilerplate: dropped wherever it appears (single block)
BOILERPLATE_RE = re.compile(
    r"^(view|read|open) (this )?(email |post |issue )?(in|on) (your |a )?(browser|web|online)|"
    r"^(read|view) (it )?online\b|^web version\b|"
    r"^(sign up|subscribe|advertise|view online|read online)(\s*\|\s*(sign up|subscribe|advertise|"
    r"view online|read online|view in browser))+\s*$|^(sign up|subscribe)( here| now)?[.!]?$|^share (this|on)|"
    r"^forwarded this (email|newsletter)|^was this (email )?forwarded to you|"
    r"^(follow us|find us) on|^(download|get) (the|our) app$|^advertise( with us)?$|^\|$",
    re.I)
# Footer: from the first match in the last part of the email, everything is dropped
FOOTER_RE = re.compile(
    r"unsubscribe|manage (your )?(email )?(preferences|subscriptions)|update your (email )?preferences|"
    r"you (are|'re|’re) receiving this|you received this|all rights reserved|"
    r"no longer (want|wish) to receive|our (mailing )?address is|"
    # feedback polls and copyright lines (seen in Superhuman, The Neuron, Substack)
    r"what did you think of (today|this)|your opinion matters|how did we do|tell us how we did|"
    r"^\W*(loved it|it was ok|terrible|good, not great|it sucked)\b|^\s*(©|copyright\s*©?)\s*\d{4}",
    re.I)
FOOTER_ZONE = 0.6        # only look for the footer start in the last 40% of blocks
FOOTER_MAX_WORDS = 60    # footer lines are short; a long paragraph mentioning "unsubscribe" is content


def _is_hidden(tag: Tag) -> bool:
    if tag.get("hidden") is not None or tag.get("aria-hidden") == "true":
        return True
    style = tag.get("style") or ""
    if HIDDEN_STYLE_RE.search(style):
        return True
    cls = " ".join(tag.get("class") or []).lower()
    return "preheader" in cls or "preview-text" in cls


class _Builder:
    def __init__(self) -> None:
        self.blocks: list[Block] = []
        self._parts: list[tuple[str, bool, str | None]] = []   # (text, bold, href)
        self._links: list[tuple[str, str]] = []
        self._kind, self._level = "text", 0

    def text(self, s: str, bold: bool, href: str | None) -> None:
        s = ZW_RE.sub("", s)
        if s:
            self._parts.append((s, bold, href))

    def link(self, text: str, href: str) -> None:
        self._links.append((text, href))

    def start(self, kind: str, level: int = 0) -> None:
        self.flush()
        self._kind, self._level = kind, level

    def flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(p[0] for p in self._parts)).strip()
        if text:
            vis = [(re.sub(r"\s+", "", t), b, h) for t, b, h in self._parts]
            vis = [v for v in vis if v[0]]
            hrefs = {h for _, _, h in vis}
            links, seen = [], set()
            for t, h in self._links:
                cu = canonical_url(h)
                if cu and cu not in seen and not cu.lower().startswith(("javascript:", "#")):
                    seen.add(cu)
                    links.append(Link(re.sub(r"\s+", " ", t).strip(), cu, link_type(cu)))
            total = sum(len(t) for t, _, _ in vis) or 1

            def lead_frac(pred) -> float:
                n = 0
                for t, b_, h in vis:
                    if not pred(b_, h):
                        break
                    n += len(t)
                return n / total

            first_href = vis[0][2] if vis else None
            self.blocks.append(Block(
                kind=self._kind, text=text, level=self._level,
                bold_full=bool(vis) and all(b for _, b, _ in vis),
                link_full=len(hrefs) == 1 and None not in hrefs,
                links=links,
                lead_link_frac=lead_frac(lambda b_, h: h is not None and h == first_href) if first_href else 0.0,
                lead_bold_frac=lead_frac(lambda b_, h: b_),
            ))
        self._parts, self._links = [], []
        self._kind, self._level = "text", 0


def _walk(node, b: _Builder, bold: bool, href: str | None) -> None:
    for child in node.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            b.text(str(child), bold, href)
            continue
        if not isinstance(child, Tag):
            continue
        name = child.name.lower()
        if name in DROP_TAGS or _is_hidden(child):
            continue
        if name == "img":
            continue                       # images are remote; pixels/spacers carry no text
        if name == "hr":
            b.flush()
            b.blocks.append(Block("hr", "---"))
            continue
        is_block = name in BLOCK_TAGS
        if is_block:
            if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                b.start("heading", int(name[1]))
            elif name == "li":
                b.start("li")
            else:
                b.flush()
        child_bold = bold or name in BOLD_TAGS or bool(BOLD_STYLE_RE.search(child.get("style") or ""))
        child_href = href
        if name == "a" and child.get("href"):
            child_href = child["href"]
            b.link(child.get_text(" ", strip=True), child_href)
        _walk(child, b, child_bold, child_href)
        if is_block:
            b.flush()


def html_to_blocks(html: str) -> list[Block]:
    soup = BeautifulSoup(html, "html.parser")
    b = _Builder()
    _walk(soup.body or soup, b, False, None)
    b.flush()
    return b.blocks


URL_RE = re.compile(r"https?://[^\s<>()\[\]]+")


def text_to_blocks(text: str) -> list[Block]:
    """Fallback for text/plain-only mails: paragraphs split on blank lines."""
    blocks = []
    for para in re.split(r"\n\s*\n", text or ""):
        t = re.sub(r"\s+", " ", ZW_RE.sub("", para)).strip()
        if not t:
            continue
        links = [Link("", canonical_url(u), link_type(canonical_url(u))) for u in URL_RE.findall(t)]
        t_clean = URL_RE.sub("", t).strip(" -–|()[]<>") or t
        heading = len(t_clean.split()) <= 8 and t_clean.upper() == t_clean and any(c.isalpha() for c in t_clean)
        blocks.append(Block("heading" if heading else "text", t_clean, level=3 if heading else 0,
                            links=links, link_full=len(links) == 1 and len(t_clean.split()) <= 20))
    return blocks


def strip_boilerplate(blocks: list[Block]) -> list[Block]:
    """Drop header boilerplate anywhere and cut the footer (first footer marker in the last 40%)."""
    out = [bl for bl in blocks if not BOILERPLATE_RE.search(bl.text)]
    start = int(len(out) * FOOTER_ZONE)
    for i in range(start, len(out)):
        if out[i].words <= FOOTER_MAX_WORDS and FOOTER_RE.search(out[i].text):
            return out[:i]
    return out


def clean(html: str | None, text: str | None) -> list[Block]:
    blocks = html_to_blocks(html) if html else text_to_blocks(text or "")
    return strip_boilerplate(blocks)


def to_markdown(blocks: list[Block]) -> str:
    lines = []
    for bl in blocks:
        if bl.kind == "hr":
            lines.append("---")
            continue
        t = bl.text
        if bl.link_full and bl.links:
            t = f"[{t}]({bl.links[0].url})"
        if bl.kind == "heading":
            lines.append("#" * max(1, min(bl.level, 6)) + " " + t)
        elif bl.kind == "li":
            lines.append("- " + t)
        elif bl.bold_full:
            lines.append(f"**{t}**")
        else:
            lines.append(t)
        if not bl.link_full:
            extra = [ln for ln in bl.links if ln.kind == "source"]
            for ln in extra[:5]:
                lines.append(f"  <{ln.url}>")
    return "\n\n".join(lines) + "\n"
