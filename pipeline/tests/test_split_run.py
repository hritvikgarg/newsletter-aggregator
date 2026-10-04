"""End to end: capture fixture emails into a temp archive, then run M2 on them."""
import sqlite3
from pathlib import Path

from nlagg import db
from nlagg.capture import run_capture
from nlagg.split_run import run_split, write_review
from tests.conftest import make_eml

FIX = Path(__file__).parent / "fixtures"


def techai_box():
    def mk(name, subject, frm):
        return make_eml(frm=frm, to="notifyy1008+techai@gmail.com", delivered_to="notifyy1008+techai@gmail.com",
                        subject=subject, text=None, html=(FIX / f"{name}.html").read_text(encoding="utf-8"))
    return {
        "19a0000000000001": mk("roundup_tldr", "OpenAI ships agents", "TLDR AI <dan@tldrnewsletter.com>"),
        "19a0000000000002": mk("essay_diff", "The Economics of Inference", "The Diff <byrne@thediff.co>"),
        "19a0000000000003": mk("teaser_substack", "Three micro-caps to watch", "PMC <pmc@substack.com>"),
        "19a0000000000004": make_eml(frm="TLDR <dan@tldrnewsletter.com>", to="notifyy1008+techai@gmail.com",
                                     delivered_to="notifyy1008+techai@gmail.com",
                                     subject="50% off TLDR Pro — last chance", html="<p>Upgrade today.</p>"),
        "19a0000000000005": make_eml(frm="Morning Brew <crew@morningbrew.com>", to="notifyy1008@gmail.com",
                                     subject="☕ Fed holds", text=None,
                                     html=(FIX / "sectioned_brew.html").read_text(encoding="utf-8")),   # 2-BizFinance
    }


def q(cfg, sql, *args):
    c = sqlite3.connect(cfg.db_path)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, args)]
    c.close()
    return rows


def test_split_end_to_end(cfg, fake_backend_cls):
    run_capture(cfg, fake_backend_cls(techai_box()))
    r = run_split(cfg, segments=["1-TechAI"])
    assert r.failed == 0 and r.processed == 4                  # Brew is 2-BizFinance: not in segment
    assert r.shapes == {"roundup": 1, "essay": 1, "teaser": 1, "empty": 1}   # promo mail: 3 words
    msgs = {m["gmail_id"]: m for m in q(cfg, "SELECT * FROM messages")}
    assert msgs["19a0000000000001"]["split_status"] == "done"
    assert msgs["19a0000000000001"]["split_shape"] == "roundup"
    assert msgs["19a0000000000002"]["split_shape"] == "essay"
    assert msgs["19a0000000000003"]["split_shape"] == "teaser"
    assert msgs["19a0000000000004"]["is_promo"] == 1
    assert msgs["19a0000000000005"]["split_status"] == "pending"

    items = q(cfg, "SELECT * FROM items WHERE gmail_id = '19a0000000000001' ORDER BY position")
    assert len(items) == 5 and items[2]["is_sponsor"] == 1
    assert items[0]["segment"] == "1-TechAI" and items[0]["source_key"] == "dan@tldrnewsletter.com|tldr ai"
    assert items[0]["kind"] == "story" and items[0]["word_count"] > 20
    links = q(cfg, "SELECT * FROM links WHERE item_id = ?", items[0]["id"])
    assert links[0]["url"] == "https://openai.com/index/agents-sdk" and links[0]["domain"] == "openai.com"
    sponsor_links = q(cfg, "SELECT link_type FROM links WHERE item_id = ?", items[2]["id"])
    assert {l["link_type"] for l in sponsor_links} == {"sponsor"}

    md = cfg.archive_dir / msgs["19a0000000000001"]["clean_text_path"]
    assert md.suffix == ".md" and "OpenAI launches Agents SDK" in md.read_text(encoding="utf-8")


def test_split_is_idempotent_and_redo_replaces(cfg, fake_backend_cls):
    run_capture(cfg, fake_backend_cls(techai_box()))
    run_split(cfg, segments=["1-TechAI"])
    n_items = q(cfg, "SELECT COUNT(*) n FROM items")[0]["n"]
    assert run_split(cfg, segments=["1-TechAI"]).processed == 0     # nothing pending
    r = run_split(cfg, segments=["1-TechAI"], redo=True)
    assert r.processed == 4
    assert q(cfg, "SELECT COUNT(*) n FROM items")[0]["n"] == n_items   # replaced, not duplicated
    assert q(cfg, "SELECT COUNT(*) n FROM links WHERE item_id NOT IN (SELECT id FROM items)")[0]["n"] == 0


def test_split_failure_is_recorded_and_retried(cfg, fake_backend_cls):
    run_capture(cfg, fake_backend_cls(techai_box()))
    eml = cfg.archive_dir / q(cfg, "SELECT raw_eml_path p FROM messages WHERE gmail_id='19a0000000000001'")[0]["p"]
    saved = eml.read_bytes()
    eml.unlink()
    r = run_split(cfg, segments=["1-TechAI"])
    assert r.failed == 1
    m = q(cfg, "SELECT split_status, split_error FROM messages WHERE gmail_id='19a0000000000001'")[0]
    assert m["split_status"] == "failed" and "FileNotFoundError" in m["split_error"]
    eml.write_bytes(saved)
    r2 = run_split(cfg, segments=["1-TechAI"])
    assert r2.processed == 1 and r2.failed == 0


def test_all_segments_and_by_id(cfg, fake_backend_cls):
    run_capture(cfg, fake_backend_cls(techai_box()))
    assert run_split(cfg, gmail_ids=["19a0000000000005"]).processed == 1
    brew = q(cfg, "SELECT title FROM items WHERE gmail_id='19a0000000000005' AND kind='story'")
    assert len(brew) == 4
    assert run_split(cfg, segments=None).processed == 4                  # the rest, all segments


def test_review_sheet(cfg, fake_backend_cls, tmp_path):
    run_capture(cfg, fake_backend_cls(techai_box()))
    run_split(cfg, segments=["1-TechAI"])
    out = tmp_path / "review.md"
    assert write_review(cfg, out, segment="1-TechAI", n=20) == 4
    text = out.read_text(encoding="utf-8")
    assert "Target: ≥ 90% OK" in text and "[SPONSOR]" in text and "shape **teaser**" in text
    assert "- [ ] OK" in text


def test_migrates_v2_database(cfg):
    """A DB created by M1 (schema v2) gets the new M2 columns without losing rows."""
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(cfg.db_path)
    c.executescript((FIX / "schema_v2.sql").read_text(encoding="utf-8"))      # exact schema shipped in PR #1
    c.executescript("INSERT INTO messages (gmail_id, segment) VALUES ('old1', '1-TechAI');"
                    "PRAGMA user_version = 2;")
    c.close()
    conn = db.connect(cfg.db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    assert {"is_promo", "split_shape", "split_error"} <= cols
    assert {"kind", "word_count"} <= {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    assert conn.execute("SELECT gmail_id FROM messages").fetchall()[0][0] == "old1"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
