from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import pytest

from nlagg.config import load_config


def make_eml(*, frm: str, to: str, subject: str, html: str | None = "<p>Hello world story</p>",
             text: str | None = "Hello world story", delivered_to: str | None = None,
             date: str = "Mon, 05 Oct 2026 07:01:00 -0400", headers: dict | None = None,
             attachment: bool = False) -> bytes:
    m = EmailMessage()
    if delivered_to:
        m["Delivered-To"] = delivered_to
    m["Received"] = "by 2002:a05:6000:1:b0:1 with SMTP id x; Mon, 5 Oct 2026 04:01:02 -0700 (PDT)"
    m["From"] = frm
    m["To"] = to
    m["Subject"] = subject
    m["Date"] = date
    for k, v in (headers or {}).items():
        m[k] = v
    if text is not None:
        m.set_content(text)
    if html is not None:
        if text is not None:
            m.add_alternative(html, subtype="html")
        else:
            m.set_content(html, subtype="html")
    if attachment:
        m.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="x.pdf")
    return bytes(m)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("NLAGG_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("NLAGG_DB_PATH", str(tmp_path / "archive" / "index.db"))
    return load_config()


def sample_mailbox() -> dict[str, bytes]:
    """A small realistic inbox keyed by (hex) gmail id."""
    return {
        "18f0a1b2c3d4e5f6": make_eml(
            frm="TLDR <dan@tldrnewsletter.com>", to="notifyy1008+techai@gmail.com",
            delivered_to="notifyy1008+techai@gmail.com", subject="OpenAI ships X 🚀, Nvidia earnings",
            html="<html><head><style>p{}</style></head><body><h1>Big Tech</h1><p>"
                 + "word " * 460 + "</p><img src='https://t.co/pixel.gif' width=1></body></html>",
            headers={"List-Unsubscribe": "<https://tldr.beehiiv.com/unsub>"},
        ),
        "18f0a1b2c3d4e5f7": make_eml(
            frm="Morning Brew <crew@morningbrew.com>", to="notifyy1008@gmail.com",
            delivered_to="notifyy1008@gmail.com", subject="☕ Markets rally",
        ),
        "18f0a1b2c3d4e5f8": make_eml(
            frm="HR Brew <hrbrew@morningbrew.com>", to="notifyy1008@gmail.com",
            delivered_to="notifyy1008@gmail.com", subject="Layoffs, again",
        ),
        "18f0a1b2c3d4e5f9": make_eml(
            frm="Lawfare <lawfare@substack.com>", to="notifyy1008+techai@gmail.com",
            delivered_to="notifyy1008+techai@gmail.com", subject="The Week That Was",
            headers={"List-Id": "<lawfare.substack.com>"},
        ),
        "18f0a1b2c3d4e5fa": make_eml(
            frm="Import AI <jack@importai.net>", to="notifyy1008+techai@gmail.com",
            delivered_to="notifyy1008+techai@gmail.com", subject="Welcome to Import AI!",
        ),
        "18f0a1b2c3d4e5fd": make_eml(   # real issue whose subject matches the welcome regex
            frm="The Neuron <theneuron@newsletter.theneurondaily.com>", to="notifyy1008+techai@gmail.com",
            delivered_to="notifyy1008+techai@gmail.com", subject="Welcome to the AI bubble",
            html="<p>" + "story " * 600 + "</p>",
        ),
        "18f0a1b2c3d4e5fb": make_eml(
            frm="Google <no-reply@accounts.google.com>", to="notifyy1008@gmail.com",
            subject="Security alert",
        ),
        "18f0a1b2c3d4e5fc": make_eml(
            frm="Random <hi@random.example>", to="someone-else@example.com",
            subject="Unrelated", html=None, attachment=True,
        ),
    }


@pytest.fixture
def mailbox() -> dict[str, bytes]:
    return sample_mailbox()


class FakeBackend:
    name = "fake"

    def __init__(self, box: dict[str, bytes], fail_ids: set[str] = frozenset()):
        self.box, self.fail_ids, self.fetched = box, set(fail_ids), []

    def list_ids(self, since):
        return list(self.box)

    def fetch(self, ids):
        from nlagg.fetchers import Fetched
        for i in ids:
            self.fetched.append(i)
            raw = b"\x00not an email" if i in self.fail_ids else self.box[i]
            yield Fetched(i, raw, thread_id="t" + i, labels=["INBOX"], snippet=None)


@pytest.fixture
def fake_backend_cls():
    return FakeBackend


def write_box(folder: Path, box: dict[str, bytes]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for gid, raw in box.items():
        (folder / f"export__{gid}.eml").write_bytes(raw)
