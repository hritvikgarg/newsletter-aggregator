import json
from datetime import datetime

import pytest

from tests.conftest import FakeBackend, FakeChat, make_eml
from tests.test_compose import compose_reply, prepare
from nlagg import db
from nlagg.compose import run_compose
from nlagg.daily import quiet_sources, run_daily
from nlagg.deliver import approve, md_to_text, reject


class FakeSMTP:
    sent = []

    def __init__(self):
        self.logged = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, u, p):
        self.logged = (u, p)

    def send_message(self, m):
        FakeSMTP.sent.append(m)


@pytest.fixture
def draft(cfg, tmp_path, monkeypatch):
    ids = prepare(cfg)
    monkeypatch.setattr(cfg, "repo_root", tmp_path)
    run_compose(cfg, issue_date="2026-10-02", client=FakeChat(lambda m, x: compose_reply(ids, bad=False)),
                resolve_links=False)
    FakeSMTP.sent = []
    monkeypatch.setenv("NLAGG_IMAP_USER", "notifyy1008@gmail.com")
    monkeypatch.setenv("NLAGG_IMAP_PASSWORD", "app-password")
    return cfg


def test_approve_without_recipients_sends_nothing(draft):
    draft.raw["delivery"]["recipients"] = []
    r = approve(draft, smtp_factory=FakeSMTP)
    assert r.status == "approved" and FakeSMTP.sent == []


def test_approve_sends_html_email_bcc(draft):
    draft.raw["delivery"]["recipients"] = ["a@example.com", "b@example.com"]
    assert approve(draft, dry_run=True, smtp_factory=FakeSMTP).message.startswith("would send")
    assert FakeSMTP.sent == []
    r = approve(draft, smtp_factory=FakeSMTP)
    assert r.status == "sent" and r.sent_to == ["a@example.com", "b@example.com"]
    m = FakeSMTP.sent[0]
    assert m["Subject"] == "Meta puts Muse on your face" and m["Bcc"] == "a@example.com, b@example.com"
    assert m.get_body(("html",)).get_content().lstrip().startswith("<!doctype html>")
    assert "TLDR <https://news.example/0>" in m.get_body(("plain",)).get_content()
    assert approve(draft, smtp_factory=FakeSMTP).status == "missing"                        # no draft left
    assert approve(draft, issue_date="2026-10-02", smtp_factory=FakeSMTP).status == "sent"
    assert len(FakeSMTP.sent) == 1                                                         # never sent twice


def test_reject(draft):
    assert reject(draft, reason="too thin").status == "rejected"
    conn = db.connect(draft.db_path)
    rep = json.loads(conn.execute("SELECT validator_report FROM issues_out").fetchone()[0])
    assert rep["rejected_reason"] == "too thin"
    conn.close()


def test_md_to_text():
    assert md_to_text("## Hi\n**x** [TLDR](https://t.co/a)") == "Hi\nx TLDR <https://t.co/a>"


def test_daily_run_end_to_end(cfg, tmp_path, monkeypatch):
    """capture (fake mailbox) -> split -> extract -> cluster -> compose, draft only, owner notified."""
    monkeypatch.setattr(cfg, "repo_root", tmp_path)
    monkeypatch.setenv("NLAGG_IMAP_USER", "notifyy1008@gmail.com")
    monkeypatch.setenv("NLAGG_IMAP_PASSWORD", "app-password")
    cfg.raw["delivery"]["owner"] = "owner@example.com"
    now = datetime.fromisoformat("2026-10-05T09:00:00+00:00")
    html1 = ("<h1>Big Tech</h1><p><a href='https://e.com/m'><b>Meta launches $1,299 VR glasses at Connect (3 minute read)</b></a></p>"
             "<p>" + "Meta showed VR glasses at Connect for $1,299 with a Muse assistant built in. " * 4 + "</p>"
             "<p><a href='https://e.com/r'><b>New RSA attack is faster (5 minute read)</b></a></p>"
             "<p>" + "Researchers broke RSA keys faster with lattice tricks in a new paper. " * 4 + "</p>")
    html2 = ("<h2>Meta unveils VR glasses and a Muse device at Connect</h2>"
             "<p>" + "At Connect Meta unveiled VR glasses for $1,299 and a Muse device for your pocket. " * 4 + "</p>"
             "<h2>Waymo doubles rides in Phoenix</h2><p>" + "Waymo robotaxi rides doubled in Phoenix this month. " * 4 + "</p>")
    box = {"aa01": make_eml(frm="TLDR <dan@tldrnewsletter.com>", to="notifyy1008+techai@gmail.com",
                            delivered_to="notifyy1008+techai@gmail.com", subject="Meta glasses, RSA",
                            html=html1, date="Mon, 05 Oct 2026 07:00:00 +0000"),
           "aa02": make_eml(frm="Superhuman <superhuman@mail.joinsuperhuman.ai>", to="notifyy1008+techai@gmail.com",
                            delivered_to="notifyy1008+techai@gmail.com", subject="Meta's big day",
                            html=html2, date="Mon, 05 Oct 2026 08:00:00 +0000")}

    def llm(model, messages):
        sysmsg = messages[0]["content"]
        if "extract facts" in sysmsg:
            t = messages[1]["content"].split("Title: ", 1)[1].split("\n", 1)[0]
            return {"summary": t, "category": "launch", "importance": 5 if "Meta" in t else 3,
                    "entities": [{"name": "Meta", "type": "company"}] if "Meta" in t else []}
        if "compare how several newsletters" in sysmsg:
            return {"consensus": [], "divergence": []}
        user = messages[1]["content"]
        import re
        ids = re.findall(r"\[i(\d+)\]", user)
        return {"subject": "Meta's glasses day", "hook": f"Two newsletters led with Meta's VR glasses [i{ids[0]}][i{ids[1]}].",
                "top_story": {"headline": "Meta shows VR glasses", "paragraphs": [f"Meta showed VR glasses for $1,299 [i{ids[0]}]."],
                              "why_it_matters": f"Muse moves onto your face [i{ids[1]}]."},
                "talking_about": [], "quick_hits": [], "safe_to_skip": [], "close": "Bye for now."}

    r = run_daily(cfg, backend=FakeBackend(box), client=FakeChat(llm), now=now, smtp_factory=FakeSMTP)
    assert r.ok, r.steps
    assert r.steps["capture"].startswith("ok: 2 new") and "2 issues" in r.steps["split"]
    assert "1 multi-source" in r.steps["cluster"] and r.steps["compose"].startswith('ok: draft "Meta')
    assert r.steps["notify"] == "ok: owner emailed" and "Draft ready" in FakeSMTP.sent[-1]["Subject"]
    assert r.log_path.exists() and "step compose" in r.log_path.read_text(encoding="utf-8")
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT status FROM issues_out").fetchone()[0] == "draft"      # never sent automatically
    conn.close()
    assert not (tmp_path / "pipeline" / "out" / "logs" / "daily.lock").exists()


def test_daily_without_llm_key_reports_failure_but_finishes(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "repo_root", tmp_path)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    from tests.conftest import seed_items
    seed_items(cfg, [{"key": "a", "sender": "TLDR", "title": "Meta glasses", "body": "Meta glasses cost $1,299 " * 5,
                      "sent": "2026-10-05T07:00:00+00:00"}])
    r = run_daily(cfg, now=datetime.fromisoformat("2026-10-05T09:00:00+00:00"), skip_capture=True)
    assert r.steps["extract"].startswith("failed") and "GROQ_API_KEY" in r.steps["extract"]
    assert "cluster" in r.steps and not r.ok


def test_quiet_sources(cfg):
    from tests.conftest import seed_items
    seed_items(cfg, [{"key": f"k{i}", "sender": "Import AI", "gmail_id": f"m{i}", "title": "t", "body": "b",
                      "sent": f"2026-09-{10 + i:02d}T07:00:00+00:00"} for i in range(4)])
    q = quiet_sources(cfg, quiet_days=4, now=datetime.fromisoformat("2026-10-05T00:00:00+00:00"))
    assert len(q) == 1 and "importai@example.com" in q[0]
