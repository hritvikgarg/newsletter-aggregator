"""Mail backends. Both yield the same thing: (gmail_id, raw_bytes, meta).

gmail_id is the hex Gmail message id in both backends (API `id` == hex(X-GM-MSGID) over IMAP),
so a mailbox captured with one backend can be continued with the other without duplicates.
"""
from __future__ import annotations

import base64
import imaplib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator, Protocol

from .config import Config


@dataclass
class Fetched:
    gmail_id: str
    raw: bytes
    thread_id: str | None = None
    labels: list[str] | None = None
    snippet: str | None = None
    extra: dict = field(default_factory=dict)


class Backend(Protocol):
    name: str

    def list_ids(self, since: date | None) -> list[str]: ...
    def fetch(self, gmail_ids: Iterable[str]) -> Iterator[Fetched]: ...


# --------------------------------------------------------------------------- Gmail REST API
class GmailApiBackend:
    """Reuses the google-skill OAuth setup (same credentials.json + refresh token)."""

    name = "api"
    RETRIES = 3   # googleapiclient retries 429/5xx with backoff; one blip must not abort a backfill

    def __init__(self, cfg: Config):
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as e:  # pragma: no cover
            raise SystemExit("Gmail API backend needs: pip install -e 'pipeline[gmail]'") from e

        cred_path = Path(os.environ.get(
            "NLAGG_GOOGLE_CREDENTIALS",
            Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "google-skill" / "credentials.json",
        ))
        token_path = Path(os.environ.get(
            "NLAGG_GOOGLE_TOKEN",
            cfg.repo_root / "tools" / "google-skill" / ".claude" / "google-skill.local.json",
        ))
        if not cred_path.exists() or not token_path.exists():
            raise SystemExit(
                f"Gmail API auth not found.\n  credentials: {cred_path} (exists={cred_path.exists()})\n"
                f"  token:       {token_path} (exists={token_path.exists()})\n"
                "Run the google-skill auth once (see PROJECT_CONTEXT.md §6), or use --backend imap."
            )
        c = json.loads(cred_path.read_text(encoding="utf-8"))
        c = c.get("installed") or c.get("web") or c
        tok = json.loads(token_path.read_text(encoding="utf-8"))
        creds = Credentials(
            token=None,
            refresh_token=tok["refresh_token"],
            client_id=c["client_id"],
            client_secret=c["client_secret"],
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        )
        self.svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self.query = cfg.query

    def list_ids(self, since: date | None) -> list[str]:
        q = self.query + (f" after:{since:%Y/%m/%d}" if since else "")
        ids: list[str] = []
        page = None
        while True:
            r = self.svc.users().messages().list(
                userId="me", q=q, maxResults=500, pageToken=page, includeSpamTrash=False
            ).execute(num_retries=self.RETRIES)
            ids += [m["id"] for m in r.get("messages", [])]
            page = r.get("nextPageToken")
            if not page:
                return ids

    def fetch(self, gmail_ids: Iterable[str]) -> Iterator[Fetched]:
        for gid in gmail_ids:
            m = self.svc.users().messages().get(userId="me", id=gid, format="raw").execute(
                num_retries=self.RETRIES)
            raw = base64.urlsafe_b64decode(m["raw"] + "=" * (-len(m["raw"]) % 4))
            yield Fetched(gid, raw, m.get("threadId"), m.get("labelIds"), m.get("snippet"))


# --------------------------------------------------------------------------- IMAP (App Password)
class ImapBackend:
    """Gmail IMAP with an App Password. Recommended for scheduled runs (no 7-day token expiry)."""

    name = "imap"
    HOST = "imap.gmail.com"
    MAILBOX = '"[Gmail]/All Mail"'
    _MSGID_RE = re.compile(rb"X-GM-MSGID (\d+)")
    _THRID_RE = re.compile(rb"X-GM-THRID (\d+)")
    _UID_RE = re.compile(rb"UID (\d+)")

    # Gmail drops long sessions ("socket error: EOF"). Reconnect and carry on; give up only if the
    # connection keeps dropping without any successful download in between.
    MAX_CONSECUTIVE_DROPS = 3

    def __init__(self, cfg: Config):
        self._user = os.environ.get("NLAGG_IMAP_USER", cfg.inbox_address)
        self._pw = os.environ.get("NLAGG_IMAP_PASSWORD")
        if not self._pw:
            raise SystemExit("Set NLAGG_IMAP_PASSWORD (Gmail App Password) for --backend imap.")
        self.reconnects = 0
        self._consecutive_drops = 0
        self._connect()
        self._uid_by_id: dict[str, bytes] = {}

    def _connect(self) -> None:
        self.imap = imaplib.IMAP4_SSL(self.HOST)
        self.imap.login(self._user, self._pw)
        typ, _ = self.imap.select(self.MAILBOX, readonly=True)
        if typ != "OK":
            raise SystemExit(f"Could not open {self.MAILBOX}; is IMAP enabled in Gmail settings?")

    def _reconnect(self) -> None:
        self._consecutive_drops += 1
        if self._consecutive_drops > self.MAX_CONSECUTIVE_DROPS:
            raise RuntimeError(f"IMAP connection dropped {self.MAX_CONSECUTIVE_DROPS} times in a row; "
                               "re-run capture to resume (already-saved mail is skipped)")
        self.reconnects += 1
        try:
            self.imap.logout()
        except Exception:
            pass
        self._connect()      # UIDs stay valid: same mailbox, same UIDVALIDITY

    def list_ids(self, since: date | None) -> list[str]:
        crit = f"SINCE {since:%d-%b-%Y}" if since else "ALL"
        typ, data = self.imap.uid("SEARCH", None, crit)
        uids = data[0].split() if data and data[0] else []
        ids: list[str] = []
        for i in range(0, len(uids), 500):
            chunk = b",".join(uids[i:i + 500])
            typ, resp = self.imap.uid("FETCH", chunk, "(X-GM-MSGID)")
            for line in resp:
                blob = line[0] if isinstance(line, tuple) else line
                if not isinstance(blob, bytes):
                    continue
                m, u = self._MSGID_RE.search(blob), self._UID_RE.search(blob)
                if m and u:
                    gid = format(int(m.group(1)), "x")
                    self._uid_by_id[gid] = u.group(1)
                    ids.append(gid)
        # Newest first, so recent issues are available even if a long backfill is interrupted
        return sorted(ids, key=lambda g: int(self._uid_by_id[g]), reverse=True)

    def _fetch_one(self, uid: bytes):
        while True:
            try:
                out = self.imap.uid("FETCH", uid, "(X-GM-THRID X-GM-LABELS BODY.PEEK[])")
                self._consecutive_drops = 0
                return out
            except (imaplib.IMAP4.abort, OSError):
                self._reconnect()

    def fetch(self, gmail_ids: Iterable[str]) -> Iterator[Fetched]:
        for gid in gmail_ids:
            uid = self._uid_by_id[gid]
            typ, resp = self._fetch_one(uid)
            for part in resp:
                if isinstance(part, tuple):
                    t = self._THRID_RE.search(part[0])
                    yield Fetched(gid, part[1], format(int(t.group(1)), "x") if t else None)
                    break

    def close(self) -> None:
        try:
            self.imap.logout()
        except Exception:
            pass


# --------------------------------------------------------------------------- local .eml files
class EmlDirBackend:
    """Import .eml files from a folder (e.g. an earlier export_inbox.ts run, or test fixtures).

    The gmail_id is taken from a '__<id>.eml' filename suffix when present, else a hash of the file.
    """

    name = "file"
    _ID_RE = re.compile(r"__([0-9a-f]{12,20})\.eml$", re.I)

    def __init__(self, folder: Path):
        if not Path(folder).is_dir():
            raise SystemExit(f"--from folder not found: {folder}")
        self.files = {self._id_for(p): p for p in sorted(Path(folder).rglob("*.eml"))}

    def _id_for(self, p: Path) -> str:
        m = self._ID_RE.search(p.name)
        if m:
            return m.group(1).lower()
        import hashlib
        return "file-" + hashlib.sha256(p.read_bytes()).hexdigest()[:16]

    def list_ids(self, since: date | None) -> list[str]:
        return list(self.files)

    def fetch(self, gmail_ids: Iterable[str]) -> Iterator[Fetched]:
        for gid in gmail_ids:
            yield Fetched(gid, self.files[gid].read_bytes())
