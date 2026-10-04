"""Backends tested against fake Gmail API / IMAP servers (no network)."""
import base64
import json
from datetime import date

import pytest

from nlagg import fetchers


# ------------------------------------------------------------------ Gmail API
class _Exec:
    calls: list = []

    def __init__(self, v): self.v = v

    def execute(self, num_retries=0):
        _Exec.calls.append(num_retries)
        return self.v


class FakeMessages:
    def __init__(self, pages, raws):
        self.pages, self.raws, self.queries = pages, raws, []

    def list(self, userId, q, maxResults, pageToken, includeSpamTrash):
        self.queries.append(q)
        return _Exec(self.pages[pageToken])

    def get(self, userId, id, format):
        assert format == "raw"
        b64 = base64.urlsafe_b64encode(self.raws[id]).decode().rstrip("=")   # Gmail strips padding
        return _Exec({"id": id, "threadId": "th" + id, "labelIds": ["Label_1"], "snippet": "snip", "raw": b64})


class FakeSvc:
    def __init__(self, m): self.m = m
    def users(self): return self
    def messages(self): return self.m


def test_gmail_api_backend(cfg, tmp_path, monkeypatch, mailbox):
    cred = tmp_path / "credentials.json"
    cred.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "sec"}}), encoding="utf-8")
    tok = tmp_path / "tok.json"
    tok.write_text(json.dumps({"refresh_token": "rt"}), encoding="utf-8")
    monkeypatch.setenv("NLAGG_GOOGLE_CREDENTIALS", str(cred))
    monkeypatch.setenv("NLAGG_GOOGLE_TOKEN", str(tok))
    ids = list(mailbox)
    fm = FakeMessages(
        {None: {"messages": [{"id": i} for i in ids[:4]], "nextPageToken": "p2"},
         "p2": {"messages": [{"id": i} for i in ids[4:]]}},
        mailbox,
    )
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: FakeSvc(fm))
    b = fetchers.GmailApiBackend(cfg)
    assert b.list_ids(date(2026, 10, 1)) == ids                       # pagination followed
    assert fm.queries[0].endswith("after:2026/10/01") and fm.queries[0].startswith("in:anywhere")
    f = next(b.fetch([ids[0]]))
    assert f.raw == mailbox[ids[0]] and f.thread_id == "th" + ids[0] and f.labels == ["Label_1"]
    assert _Exec.calls and all(n == 3 for n in _Exec.calls)          # transient errors retried


def test_gmail_api_missing_auth_is_clear(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("NLAGG_GOOGLE_CREDENTIALS", str(tmp_path / "nope.json"))
    with pytest.raises(SystemExit, match="Gmail API auth not found"):
        fetchers.GmailApiBackend(cfg)


# ------------------------------------------------------------------ IMAP
class FakeImap:
    def __init__(self, host, box):
        self.box = box          # gid(hex) -> raw
        self.uids = {str(100 + n).encode(): gid for n, gid in enumerate(box)}
        self.searches = []

    def login(self, u, p): self.user = u
    def select(self, mb, readonly): return ("OK", [b"7"])
    def logout(self): pass

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            self.searches.append(args[1])
            return "OK", [b" ".join(self.uids)]
        uidset, what = args
        if what == "(X-GM-MSGID)":
            return "OK", [b"%d (X-GM-MSGID %d UID %s)" % (n + 1, int(self.uids[u], 16), u)
                          for n, u in enumerate(uidset.split(b","))]
        gid = self.uids[uidset]
        hdr = b"1 (X-GM-THRID %d X-GM-LABELS (\\Inbox) UID %s BODY[] {%d}" % (
            int(gid, 16) + 1, uidset, len(self.box[gid]))
        return "OK", [(hdr, self.box[gid]), b")"]


def test_imap_backend(cfg, monkeypatch, mailbox):
    monkeypatch.setenv("NLAGG_IMAP_PASSWORD", "app pw")
    monkeypatch.setattr(fetchers.imaplib, "IMAP4_SSL", lambda host: FakeImap(host, mailbox))
    b = fetchers.ImapBackend(cfg)
    ids = b.list_ids(date(2026, 10, 2))
    assert ids == list(mailbox)                                      # hex(X-GM-MSGID) == API id
    assert b.imap.searches == ["SINCE 02-Oct-2026"]
    f = next(b.fetch([ids[1]]))
    assert f.raw == mailbox[ids[1]]
    assert f.thread_id == format(int(ids[1], 16) + 1, "x")


def test_imap_needs_password(cfg, monkeypatch):
    monkeypatch.delenv("NLAGG_IMAP_PASSWORD", raising=False)
    with pytest.raises(SystemExit, match="NLAGG_IMAP_PASSWORD"):
        fetchers.ImapBackend(cfg)
