"""Duplicate copies of one issue (subscribed on +tag AND plain address) are marked and skipped."""
import sqlite3

from nlagg import db
from nlagg.capture import run_capture
from nlagg.dedupe import mark_duplicates
from nlagg.split_run import run_split
from tests.conftest import make_eml

BODY = "<h2><a href='https://e.com/a'>Story A</a></h2><p>" + "alpha " * 160 + "</p>" \
       "<h2><a href='https://e.com/b'>Story B</a></h2><p>" + "beta " * 160 + "</p>"


def mk(to, subject="You're in a Status Game", date="Mon, 05 Oct 2026 07:01:00 -0400", body=BODY):
    return make_eml(frm='"Dru Riley @ Trends.vc" <d@trends.vc>', to=to, delivered_to=to,
                    subject=subject, date=date, text=None, html=body)


def q(cfg, sql, *a):
    c = sqlite3.connect(cfg.db_path)
    c.row_factory = sqlite3.Row
    out = [dict(r) for r in c.execute(sql, a)]
    c.close()
    return out


def test_same_issue_twice_keeps_one(cfg, fake_backend_cls):
    box = {
        "19b0000000000001": mk("notifyy1008@gmail.com"),                                 # plain copy
        "19b0000000000002": mk("notifyy1008+indie@gmail.com",                             # tagged copy,
                               date="Mon, 05 Oct 2026 07:03:00 -0400"),                   # 2 min later
    }
    r = run_capture(cfg, fake_backend_cls(box))
    assert r.duplicates == 1
    m = {x["gmail_id"]: x for x in q(cfg, "SELECT * FROM messages")}
    # both now route to IndieHacker (fallback + tag); the earlier one is canonical
    assert m["19b0000000000001"]["segment"] == m["19b0000000000002"]["segment"] == "6-IndieHacker"
    assert m["19b0000000000001"]["duplicate_of"] is None
    assert m["19b0000000000002"]["duplicate_of"] == "19b0000000000001"
    assert m["19b0000000000002"]["split_status"] == "skipped"
    s = run_split(cfg, segments=["6-IndieHacker"])
    assert s.processed == 1
    assert {x["gmail_id"] for x in q(cfg, "SELECT DISTINCT gmail_id FROM items")} == {"19b0000000000001"}


def test_canonical_prefers_a_real_segment(cfg, fake_backend_cls):
    unmatched = make_eml(frm="Some Sender <x@unknown.example>", to="notifyy1008@gmail.com",
                         delivered_to="notifyy1008@gmail.com", subject="Daily", text=None, html=BODY)
    tagged = make_eml(frm="Some Sender <x@unknown.example>", to="notifyy1008+techai@gmail.com",
                      delivered_to="notifyy1008+techai@gmail.com", subject="Daily", text=None, html=BODY,
                      date="Mon, 05 Oct 2026 07:30:00 -0400")
    run_capture(cfg, fake_backend_cls({"19b0000000000011": unmatched, "19b0000000000012": tagged}))
    m = {x["gmail_id"]: x for x in q(cfg, "SELECT * FROM messages")}
    assert m["19b0000000000011"]["segment"] == "unmatched"
    assert m["19b0000000000012"]["duplicate_of"] is None              # the routed copy wins
    assert m["19b0000000000011"]["duplicate_of"] == "19b0000000000012"


def test_not_duplicates(cfg, fake_backend_cls):
    box = {
        # same recurring subject, different week -> different issues
        "19b0000000000021": mk("notifyy1008+indie@gmail.com", subject="Weekly digest"),
        "19b0000000000022": mk("notifyy1008+indie@gmail.com", subject="Weekly digest",
                               date="Mon, 12 Oct 2026 07:01:00 -0400"),
        # same subject, same hour, but clearly different content length -> different
        "19b0000000000023": mk("notifyy1008+indie@gmail.com", subject="Edition",
                               body="<p>" + "short " * 300 + "</p>"),
        "19b0000000000024": mk("notifyy1008@gmail.com", subject="Edition",
                               body="<p>" + "long " * 900 + "</p>"),
    }
    r = run_capture(cfg, fake_backend_cls(box))
    assert r.duplicates == 0
    assert q(cfg, "SELECT COUNT(*) n FROM messages WHERE duplicate_of IS NOT NULL")[0]["n"] == 0


def test_mark_duplicates_is_recomputed(cfg, fake_backend_cls):
    box = {"19b0000000000031": mk("notifyy1008@gmail.com"),
           "19b0000000000032": mk("notifyy1008+indie@gmail.com", date="Mon, 05 Oct 2026 07:02:00 -0400")}
    run_capture(cfg, fake_backend_cls(box))
    conn = db.connect(cfg.db_path)
    assert mark_duplicates(conn) == 1 and mark_duplicates(conn) == 1     # stable, no drift
    # if the copies stop matching (e.g. a rule change), the skipped one returns to pending
    conn.execute("UPDATE messages SET subject = 'Changed' WHERE gmail_id = '19b0000000000032'")
    conn.commit()
    assert mark_duplicates(conn) == 0
    st = conn.execute("SELECT split_status FROM messages WHERE gmail_id='19b0000000000032'").fetchone()[0]
    assert st == "pending"
    conn.close()
