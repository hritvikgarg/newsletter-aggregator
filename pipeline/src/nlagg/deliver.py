"""M6 — human approval, then delivery by email (Gmail SMTP with the App Password capture already uses).

Nothing is ever sent automatically: `nlagg approve` is the only path to recipients. Without recipients in
config.yaml `delivery.recipients`, approving just marks the issue approved (the HTML file is the product).
"""
from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from . import db
from .config import Config


@dataclass
class DeliverResult:
    status: str
    sent_to: list[str]
    message: str


def _smtp_settings(cfg: Config) -> tuple[str, int, str, str]:
    d = cfg.raw.get("delivery", {})
    user = os.environ.get("NLAGG_IMAP_USER") or cfg.inbox_address
    pw = os.environ.get("NLAGG_IMAP_PASSWORD", "")
    if not pw:
        raise SystemExit("NLAGG_IMAP_PASSWORD is not set (pipeline/.env): needed to send email")
    return d.get("smtp_host", "smtp.gmail.com"), int(d.get("smtp_port", 465)), user, pw


def build_email(subject: str, html: str, text: str, sender: str, to: list[str]) -> EmailMessage:
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = sender
    m["To"] = sender                      # recipients go in Bcc: nobody sees the list
    m["Bcc"] = ", ".join(to)
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    m.set_content(text)
    m.add_alternative(html, subtype="html")
    return m


def send_email(cfg: Config, msg: EmailMessage, smtp_factory=None) -> None:
    host, port, user, pw = _smtp_settings(cfg)
    factory = smtp_factory or (lambda: smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=60))
    with factory() as s:
        s.login(user, pw)
        s.send_message(msg)


def md_to_text(md: str) -> str:
    """Plain-text part: markdown links become 'name (url)'."""
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 <\2>", md)
    return re.sub(r"^#+\s*", "", t, flags=re.M).replace("**", "")


def _row(conn, segment: str, issue_date: str | None):
    if issue_date:
        return conn.execute("SELECT * FROM issues_out WHERE segment = ? AND issue_date = ?",
                            (segment, issue_date)).fetchone()
    return conn.execute("SELECT * FROM issues_out WHERE segment = ? AND status = 'draft' ORDER BY issue_date DESC LIMIT 1",
                        (segment,)).fetchone()


def approve(cfg: Config, *, segment: str | None = None, issue_date: str | None = None, smtp_factory=None,
            dry_run: bool = False) -> DeliverResult:
    segment = segment or (cfg.active_segments[0] if cfg.active_segments else "1-TechAI")
    conn = db.connect(cfg.db_path)
    row = _row(conn, segment, issue_date)
    if row is None:
        conn.close()
        return DeliverResult("missing", [], f"no draft issue for {segment} {issue_date or '(latest)'}")
    if row["status"] in ("sent",):
        conn.close()
        return DeliverResult("sent", [], f"issue {row['issue_date']} was already sent")
    report = json.loads(row["validator_report"] or "{}")
    to = [r.strip() for r in cfg.raw.get("delivery", {}).get("recipients", []) if r and r.strip()]
    if dry_run:
        conn.close()
        return DeliverResult(row["status"], to, f"would send \"{report.get('subject')}\" to {len(to)} recipient(s)")
    status, sent_to, note = "approved", [], "approved; no recipients configured (delivery.recipients) — nothing sent"
    if to:
        html = Path(row["html_path"]).read_text(encoding="utf-8")
        text = md_to_text(Path(row["md_path"]).read_text(encoding="utf-8"))
        sender = os.environ.get("NLAGG_IMAP_USER") or cfg.inbox_address
        send_email(cfg, build_email(report.get("subject") or "Today's digest", html, text, sender, to), smtp_factory)
        status, sent_to, note = "sent", to, f"sent to {len(to)} recipient(s)"
    report.update({"approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sent_to": sent_to})
    with conn:
        conn.execute("UPDATE issues_out SET status = ?, validator_report = ? WHERE id = ?",
                     (status, json.dumps(report, ensure_ascii=False), row["id"]))
    conn.close()
    return DeliverResult(status, sent_to, note)


def reject(cfg: Config, *, segment: str | None = None, issue_date: str | None = None, reason: str = "") -> DeliverResult:
    segment = segment or (cfg.active_segments[0] if cfg.active_segments else "1-TechAI")
    conn = db.connect(cfg.db_path)
    row = _row(conn, segment, issue_date)
    if row is None or row["status"] == "sent":
        conn.close()
        return DeliverResult("missing" if row is None else "sent", [], "nothing to reject")
    report = json.loads(row["validator_report"] or "{}")
    report["rejected_reason"] = reason
    with conn:
        conn.execute("UPDATE issues_out SET status = 'rejected', validator_report = ? WHERE id = ?",
                     (json.dumps(report, ensure_ascii=False), row["id"]))
    conn.close()
    return DeliverResult("rejected", [], f"issue {row['issue_date']} rejected; its stories can be used again")


def notify_owner(cfg: Config, subject: str, body: str, smtp_factory=None) -> bool:
    """Short plain email to delivery.owner (draft ready / health alerts). False if no owner configured."""
    owner = (cfg.raw.get("delivery", {}).get("owner") or "").strip()
    if not owner:
        return False
    sender = os.environ.get("NLAGG_IMAP_USER") or cfg.inbox_address
    m = EmailMessage()
    m["Subject"], m["From"], m["To"], m["Date"] = subject, sender, owner, formatdate(localtime=True)
    m.set_content(body)
    send_email(cfg, m, smtp_factory)
    return True
