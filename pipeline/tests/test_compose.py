import json
from datetime import datetime

from tests.conftest import FakeChat, seed_items
from tests.test_cluster import END, STORIES
from nlagg import db
from nlagg.cluster import run_cluster
from nlagg.compose import Checker, run_compose, sentences
from nlagg.extract import run_extract


def ids_of(cfg):
    conn = db.connect(cfg.db_path)
    out = {r["title"]: r["id"] for r in conn.execute("SELECT id, title FROM items")}
    conn.close()
    return out


def extract_reply(model, messages):
    text = messages[1]["content"]
    imp = 5 if "Meta" in text else (1 if "Waymo" in text else 3)
    first = text.split("Title: ", 1)[1].split("\n", 1)[0]
    return {"summary": first, "category": "launch", "importance": imp, "entities": [], "claims": [],
            "stats": [], "quotes": [], "topics": []}


def prepare(cfg):
    seed_items(cfg, STORIES)
    run_extract(cfg, days=30, client=FakeChat(extract_reply), now=END)
    run_cluster(cfg, end=END, analyze=False)
    return ids_of(cfg)


def compose_reply(ids, bad=True):
    t = ids["Meta Launches Lightweight $1,299 VR Headset That Looks Like Glasses (5 minute read)"]
    s = ids["Meta unveils a Muse device, VR glasses, and more, at Connect"]
    n = ids["😺 Meta is putting Muse on your glasses and in your pocket"]
    r = ids["There's a new way to break RSA that's faster than anything (5 minute read)"]
    w = ids["Waymo is scaling fast in Phoenix"]
    body = {
        "subject": "Meta puts Muse on your face",
        "hook": f"Three newsletters led with Meta's Connect event [i{t}][i{s}][i{n}].",
        "top_story": {"headline": "Meta shows VR glasses and a Muse device",
                      "paragraphs": [f"Meta unveiled lightweight VR glasses priced at $1,299 [i{t}].",
                                     f"Only The Neuron says they ship next month [i{n}]."
                                     + (f" Apple is expected to answer with $999 glasses [i{t}]." if bad else "")],
                      "why_it_matters": f"Glasses may become the main way people use Muse [i{n}]."
                                        + (" Analysts expect 10 million sales." if bad else "")},
        "talking_about": [],
        "quick_hits": [{"text": f"Researchers describe a faster way to break RSA keys [i{r}]."}]
                      + ([{"text": "Google released Gemini 5 [i99999]."}] if bad else []),
        "safe_to_skip": [{"text": f"Waymo's Phoenix numbers are interesting but nothing new for builders [i{w}]."}],
        "close": "See you tomorrow."}
    return body


def test_compose_validates_retries_and_writes(cfg, tmp_path, monkeypatch):
    ids = prepare(cfg)
    monkeypatch.setattr(cfg, "repo_root", tmp_path)
    replies = [compose_reply(ids, bad=True), compose_reply(ids, bad=True)]
    fake = FakeChat(lambda m, msgs: replies.pop(0))
    r = run_compose(cfg, issue_date="2026-10-02", client=fake, resolve_links=False)
    assert r.retried and len(fake.calls) == 2
    assert "The checker rejected" in fake.calls[1][1][-1]["content"]
    reasons = " | ".join(r.dropped)
    assert "Apple" in reasons and "no citation" in reasons and "unknown item" in reasons
    md = r.md_path.read_text(encoding="utf-8")
    assert "Apple" not in md and "Gemini 5" not in md and "$1,299" in md
    assert "(TLDR)" in md.replace("[TLDR](https://news.example/0)", "(TLDR)") or "[TLDR](" in md
    html = r.html_path.read_text(encoding="utf-8")
    assert "<a href=" in html and "Meta puts Muse on your face" in html and "$" not in html.split("<title>")[0][:0]
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT * FROM issues_out").fetchone()
    rep = json.loads(row["validator_report"])
    assert row["status"] == "draft" and rep["cited_items"] and rep["retried"]
    conn.close()


def test_compose_clean_reply_has_no_problems_and_no_retry(cfg, tmp_path, monkeypatch):
    ids = prepare(cfg)
    monkeypatch.setattr(cfg, "repo_root", tmp_path)
    fake = FakeChat(lambda m, msgs: compose_reply(ids, bad=False))
    r = run_compose(cfg, issue_date="2026-10-02", client=fake, resolve_links=False)
    assert r.dropped == [] and not r.retried and len(fake.calls) == 1
    roles = fake.calls[0][1][1]["content"]
    assert "TOP" in roles and "covered by 3 newsletter(s)" in roles


def test_items_cited_yesterday_are_not_reused(cfg, tmp_path, monkeypatch):
    ids = prepare(cfg)
    monkeypatch.setattr(cfg, "repo_root", tmp_path)
    run_compose(cfg, issue_date="2026-10-02", client=FakeChat(lambda m, x: compose_reply(ids, bad=False)),
                resolve_links=False)
    conn = db.connect(cfg.db_path)
    conn.execute("UPDATE stories SET window_end = '2026-10-03'")
    conn.commit()
    conn.close()
    fake = FakeChat(lambda m, x: compose_reply(ids, bad=False))
    run_compose(cfg, issue_date="2026-10-03", client=fake, resolve_links=False)
    prompt = fake.calls[0][1][1]["content"]
    assert "Meta" not in prompt                       # yesterday's cited items are left out


def test_approved_issue_is_not_overwritten(cfg, tmp_path, monkeypatch):
    ids = prepare(cfg)
    monkeypatch.setattr(cfg, "repo_root", tmp_path)
    run_compose(cfg, issue_date="2026-10-02", client=FakeChat(lambda m, x: compose_reply(ids, bad=False)),
                resolve_links=False)
    conn = db.connect(cfg.db_path)
    conn.execute("UPDATE issues_out SET status = 'approved'")
    conn.commit()
    conn.close()
    r = run_compose(cfg, issue_date="2026-10-02", client=FakeChat(lambda m, x: 1 / 0), resolve_links=False)
    assert "already approved" in r.skipped_reason


def test_checker_rules():
    c = Checker({1: "OpenAI raised $40 billion from SoftBank, Sam Altman said on Monday."}, {"TLDR"})
    assert c.check("OpenAI raised $40 billion [i1].") is None
    assert c.check("TLDR says OpenAI raised $40 billion [i1].") is None              # newsletter names are fine
    assert "number" in c.check("OpenAI raised $50 billion [i1].")
    assert "name" in c.check("OpenAI and Microsoft raised money [i1].")
    assert c.check("OpenAI raised money.") == "no citation"
    assert "copies" in c.check("OpenAI raised $40 billion from SoftBank, Sam Altman said on Monday [i1].")
    assert c.check("Two newsletters covered it and OpenAI raised money [i1].") is None   # source counts are ok


def test_sentence_split_keeps_dangling_ids():
    assert sentences("A thing happened. [i1] Then another [i2].") == ["A thing happened. [i1]", "Then another [i2]."]
