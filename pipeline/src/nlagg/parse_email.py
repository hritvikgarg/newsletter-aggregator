"""Turn one raw RFC-822 message into a `messages` row. Deterministic, no AI.

Routing and the is_issue subject regex mirror tools/google-skill/export_inbox.ts (the export that
was used to live-verify subscriptions); is_issue additionally requires a short body (see is_issue).
"""
from __future__ import annotations

import email
import hashlib
import re
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from bs4 import BeautifulSoup

# Welcome / confirm / verify mails are not issues (same regex as export_inbox.ts, plus a few).
CONFIRM_RE = re.compile(
    r"confirm|verify|verification|welcome|complete your sign|one-time|added successfully|"
    r"now signed up|now subscribed|thanks for signing|thank you for subscribing|a few things|"
    r"verification code|on the list|activate your|you're in|you’re in",
    re.I,
)

# Header fingerprints of common sending platforms (first match wins).
PLATFORM_RULES: list[tuple[str, str, str]] = [
    # (header, substring, platform)
    ("List-Id", "substack", "substack"),
    ("X-Mailgun-Tag", "substack", "substack"),
    ("List-Unsubscribe", "substack.com", "substack"),
    ("List-Unsubscribe", "beehiiv", "beehiiv"),
    ("X-Beehiiv-Ids", "", "beehiiv"),
    ("X-MC-User", "", "mailchimp"),
    ("List-Id", "mcsv.net", "mailchimp"),
    ("List-Unsubscribe", "list-manage.com", "mailchimp"),
    ("List-Unsubscribe", "convertkit", "convertkit"),
    ("List-Unsubscribe", "kit.com", "convertkit"),
    ("List-Unsubscribe", "ghost", "ghost"),
    ("List-Unsubscribe", "buttondown", "buttondown"),
    ("X-SG-EID", "", "sendgrid"),
    ("X-SES-Outgoing", "", "amazon-ses"),
    ("List-Unsubscribe", "sailthru", "sailthru"),
    ("List-Unsubscribe", "hubspot", "hubspot"),
    ("X-Mailer", "", "x-mailer"),   # fallback marker; value appended below
]

WORDS_PER_MINUTE = 230
# A welcome/confirm mail is short. A subject match alone is not enough: real issues can have
# subjects like "Welcome to the AI bubble" or "How to verify AI output" (decided 2026-10-04).
NON_ISSUE_MAX_WORDS = 250


def parse_raw(raw: bytes) -> EmailMessage:
    return email.message_from_bytes(raw, policy=policy.default)  # type: ignore[return-value]


def _header(msg: EmailMessage, name: str) -> str:
    v = msg.get(name)
    return str(v).strip() if v is not None else ""


def _decode_words(s: str) -> str:
    """Decode RFC 2047 encoded words (=?charset?b?...?=) left in a raw header value."""
    if "=?" not in (s or ""):
        return s
    try:
        from email.header import decode_header, make_header
        return str(make_header(decode_header(s)))
    except Exception:
        return s


def _raw_from(msg: EmailMessage) -> str:
    """The From header as sent (the policy-parsed value can mangle unusual display names)."""
    for k, v in msg.raw_items():
        if k.lower() == "from":
            return re.sub(r"\s+", " ", str(v)).strip()
    return _header(msg, "From")


def _all_headers(msg: EmailMessage, name: str) -> list[str]:
    return [str(v) for v in (msg.get_all(name) or [])]


def find_plus_tags(msg: EmailMessage, inbox_address: str) -> list[str]:
    """Collect '+tag' values addressed to our inbox from Delivered-To, X-Original-To, To, Cc."""
    local, _, domain = inbox_address.partition("@")
    pat = re.compile(rf"{re.escape(local)}\+([a-z0-9_-]+)@{re.escape(domain)}", re.I)
    tags: list[str] = []
    for h in ("Delivered-To", "X-Original-To", "To", "Cc"):
        for v in _all_headers(msg, h):
            for m in pat.finditer(v):
                t = m.group(1).lower()
                if t not in tags:
                    tags.append(t)
    return tags


def route_segment(sender_email: str, tags: list[str], tag_to_segment: dict[str, str],
                  sender_overrides: list[tuple[str, str]],
                  sender_fallbacks: list[tuple[str, str]] = (), sender_name: str = "") -> str:
    """1) overrides (sender address, always win)  2) '+tag'  3) fallbacks (only when no tag
    matched; matched on "name <address>", so one sending service can be split by display name)."""
    s = sender_email.lower()
    for needle, segment in sender_overrides:
        if needle in s:
            return segment
    for t in tags:
        if t in tag_to_segment:
            return tag_to_segment[t]
    who = f"{sender_name} <{sender_email}>".lower()
    for needle, segment in sender_fallbacks:
        if needle in who:
            return segment
    return "unmatched"


def detect_platform(msg: EmailMessage) -> str:
    for header, needle, platform in PLATFORM_RULES:
        v = _header(msg, header)
        if not v:
            continue
        if needle == "" or needle in v.lower():
            if platform == "x-mailer":
                return "other:" + v.split()[0].lower()[:40]
            return platform
    return "unknown"


def _to_iso_utc(date_header: str) -> str | None:
    if not date_header:
        return None
    try:
        dt = parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _received_iso(msg: EmailMessage) -> str | None:
    # Top-most Received header carries the time Google accepted the mail.
    rec = _all_headers(msg, "Received")
    if rec and ";" in rec[0]:
        return _to_iso_utc(rec[0].rsplit(";", 1)[1].strip())
    return None


def bodies(msg: EmailMessage) -> tuple[str | None, str | None, bool]:
    """Return (html, text, has_attachments)."""
    html = text = None
    has_attach = False
    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = part.get_content_disposition()
        ctype = part.get_content_type()
        if disp == "attachment":
            has_attach = True
            continue
        try:
            content = part.get_content()
        except (LookupError, ValueError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", errors="replace")
        if ctype == "text/html" and html is None:
            html = content
        elif ctype == "text/plain" and text is None:
            text = content
    return html, text, has_attach


def visible_text(html: str | None, text: str | None) -> str:
    """Rough readable text for word counts/snippets. Proper cleaning happens in M2."""
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style", "head", "title", "meta"]):
            t.decompose()
        out = soup.get_text(" ", strip=True)
    else:
        out = text or ""
    return re.sub(r"\s+", " ", out).strip()


def source_key(msg: EmailMessage, sender_email: str, sender_name: str) -> str | None:
    """Stable key per *newsletter* (not per sender address).

    One address can send several newsletters (dan@tldrnewsletter.com sends TLDR, TLDR AI and
    TLDR Web Dev; newsletter@divenewsletter.com sends several Industry Dive titles), so the address
    alone would undercount "how many sources covered this story". Use the List-Id when present,
    otherwise the normalised display name, next to the address.
    """
    if not sender_email:
        return None
    lid = _header(msg, "List-Id")
    m = re.search(r"<([^>]+)>", lid)
    part = (m.group(1) if m else lid).strip().lower()
    if not part:
        part = re.sub(r"[^a-z0-9]+", " ", (sender_name or "").lower()).strip()
    return f"{sender_email}|{part}" if part else sender_email


_ANGLE_RE = re.compile(r"<\s*([^<>\s]+@[^<>\s]+)\s*>")


def parse_sender(from_header: str) -> tuple[str, str]:
    """(display name, address). parseaddr returns ('', '') for display names with an unquoted '@'
    (e.g. `Dru Riley @ Trends.vc <d@trends.vc>`), so fall back to the <address> in the raw header."""
    name, addr = parseaddr(from_header)
    if addr and "@" in addr:
        return name, addr
    m = _ANGLE_RE.search(from_header or "")
    if m:
        return from_header[:m.start()].strip().strip('"').strip(), m.group(1)
    return name, addr


def is_issue(subject: str, word_count: int) -> int:
    """0 only when the subject looks like welcome/confirm (or is empty) AND the body is short."""
    looks_like_admin = not subject.strip() or bool(CONFIRM_RE.search(subject))
    return 0 if (looks_like_admin and word_count < NON_ISSUE_MAX_WORDS) else 1


def slugify(s: str, n: int = 28) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", (s or "x").lower()).strip("-")[:n]) or "x"


def build_row(raw: bytes, gmail_id: str, *, inbox_address: str, tag_to_segment: dict[str, str],
              sender_overrides: list[tuple[str, str]],
              sender_fallbacks: list[tuple[str, str]] = (), thread_id: str | None = None,
              labels: list[str] | None = None, snippet: str | None = None,
              backend: str = "file") -> dict:
    msg = parse_raw(raw)
    sender_name, sender_email = parse_sender(_header(msg, "From"))     # decoded (MIME words)
    if not sender_email or "@" not in sender_email:
        sender_name, sender_email = parse_sender(_raw_from(msg))          # e.g. unquoted '@' in name
        sender_name = _decode_words(sender_name)
    sender_email = sender_email.lower()
    subject = _header(msg, "Subject")
    tags = find_plus_tags(msg, inbox_address)
    delivered = next(iter(_all_headers(msg, "Delivered-To")), "") or _header(msg, "To")
    html, text, has_attach = bodies(msg)
    vis = visible_text(html, text)
    words = len(vis.split())

    return {
        "gmail_id": gmail_id,
        "thread_id": thread_id,
        "segment": route_segment(sender_email, tags, tag_to_segment, sender_overrides,
                                 sender_fallbacks, sender_name),
        "source_key": source_key(msg, sender_email, sender_name),
        "newsletter": None,          # mapped to sources.csv names later (sender -> newsletter table)
        "publisher": None,
        "sender_name": sender_name or sender_email,
        "sender_email": sender_email,
        "to_address": ", ".join(a for _, a in getaddresses([delivered])) or delivered,
        "subject": subject,
        "sent_date": _to_iso_utc(_header(msg, "Date")),
        "received_date": _received_iso(msg),
        "is_issue": is_issue(subject, words),
        "labels": None if labels is None else ",".join(labels),
        "snippet": snippet if snippet is not None else vis[:200],
        "list_id": _header(msg, "List-Id") or None,
        "list_unsubscribe": _header(msg, "List-Unsubscribe") or None,
        "sending_platform": detect_platform(msg),
        "has_html": int(html is not None),
        "has_text": int(text is not None),
        "word_count": words,
        "reading_minutes": round(words / WORDS_PER_MINUTE, 1),
        "has_attachments": int(has_attach),
        "content_hash": hashlib.sha256(raw).hexdigest(),
        "capture_backend": backend,
        "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
