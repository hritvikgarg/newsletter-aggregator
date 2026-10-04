import sqlite3
from datetime import date

from nlagg import db
from nlagg.capture import run_capture, resolve_since
from nlagg.fetchers import EmlDirBackend
from tests.conftest import write_box


def rows(cfg):
    c = sqlite3.connect(cfg.db_path)
    c.row_factory = sqlite3.Row
    out = {r["gmail_id"]: dict(r) for r in c.execute("SELECT * FROM messages")}
    c.close()
    return out


def test_capture_writes_files_and_rows(cfg, mailbox, fake_backend_cls):
    r = run_capture(cfg, fake_backend_cls(mailbox))
    assert r.scanned == 8
    assert r.new_added == 7 and r.ignored_sender == 1 and r.failed == 0
    got = rows(cfg)
    assert len(got) == 8                                   # ignored sender recorded, not re-fetched
    assert got["18f0a1b2c3d4e5fb"]["segment"] == "ignored"
    assert got["18f0a1b2c3d4e5fb"]["raw_eml_path"] is None
    tldr = got["18f0a1b2c3d4e5f6"]
    assert tldr["raw_eml_path"] == "1-TechAI/2026-10-05/tldr__18f0a1b2c3d4e5f6.eml"
    assert (cfg.archive_dir / tldr["raw_eml_path"]).read_bytes() == mailbox["18f0a1b2c3d4e5f6"]
    assert tldr["thread_id"] == "t18f0a1b2c3d4e5f6" and tldr["labels"] == "INBOX"
    assert tldr["enrich_status"] == "pending" and tldr["split_status"] == "pending"
    assert got["18f0a1b2c3d4e5fa"]["is_issue"] == 0       # short welcome mail
    assert got["18f0a1b2c3d4e5fd"]["is_issue"] == 1       # long real issue, "Welcome to ..." subject
    assert r.by_segment == {"1-TechAI": 3, "2-BizFinance": 1, "3-Legal": 1,
                            "4-HRPeopleOps": 1, "unmatched": 1}


def test_capture_is_idempotent(cfg, mailbox, fake_backend_cls):
    run_capture(cfg, fake_backend_cls(mailbox))
    b = fake_backend_cls(mailbox)
    r2 = run_capture(cfg, b)
    assert r2.new_added == 0 and r2.skipped_existing == 8
    assert b.fetched == []                                 # nothing downloaded twice
    assert len(rows(cfg)) == 8


def test_failures_are_retried_next_run(cfg, mailbox, fake_backend_cls):
    bad = "18f0a1b2c3d4e5f7"

    class Flaky(fake_backend_cls):
        """Delivers a broken message for `bad` on the first run, the real one afterwards."""
        def fetch(self, ids):
            from nlagg.fetchers import Fetched
            for i in ids:
                self.fetched.append(i)
                if i == bad and not getattr(self, "ok", False):
                    yield Fetched(i, None)                 # type: ignore[arg-type] -> build_row fails
                else:
                    yield Fetched(i, self.box[i])
    b = Flaky(mailbox)
    r = run_capture(cfg, b)
    assert r.failed == 1 and bad not in rows(cfg)
    b2 = Flaky(mailbox)
    b2.ok = True
    r2 = run_capture(cfg, b2)
    assert r2.new_added == 1 and bad in rows(cfg)
    assert b2.fetched == [bad]
    conn = db.connect(cfg.db_path)
    statuses = [x[0] for x in conn.execute("SELECT status FROM sync_log ORDER BY id")]
    assert statuses == ["partial", "ok"]


def test_dry_run_writes_nothing(cfg, mailbox, fake_backend_cls):
    r = run_capture(cfg, fake_backend_cls(mailbox), dry_run=True)
    assert r.new_added == 8
    assert rows(cfg) == {}
    assert not any(cfg.archive_dir.rglob("*.eml"))


def test_incremental_window(cfg, mailbox, fake_backend_cls):
    conn = db.connect(cfg.db_path)
    assert resolve_since(conn, cfg, "fake", False, None) is None          # first run = full
    run_capture(cfg, fake_backend_cls(mailbox))
    conn = db.connect(cfg.db_path)
    s = resolve_since(conn, cfg, "fake", False, None)
    assert s is not None and (date.today() - s).days in (cfg.overlap_days, cfg.overlap_days + 1)
    assert resolve_since(conn, cfg, "fake", True, None) is None           # --backfill
    assert resolve_since(conn, cfg, "fake", False, date(2026, 8, 1)) == date(2026, 8, 1)


def test_eml_dir_backend(cfg, mailbox, tmp_path):
    src = tmp_path / "export"
    write_box(src, mailbox)
    r = run_capture(cfg, EmlDirBackend(src))
    assert r.new_added == 7
    assert "18f0a1b2c3d4e5f6" in rows(cfg)                 # id taken from filename suffix


def test_schema_has_story_tables(cfg):
    conn = db.connect(cfg.db_path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"messages", "items", "stories", "story_items", "issues_out", "item_enrichment",
            "enrichment", "claims", "entities", "sync_log"} <= names
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_reindex_applies_new_rules_without_moving_files(cfg, fake_backend_cls):
    from tests.conftest import make_eml
    from nlagg.reindex import run_reindex
    box = {"18f0aaaaaaaaaa01": make_eml(frm="The Pragmatic Engineer <pragmaticengineer@substack.com>",
                                        to="notifyy1008+legal@gmail.com", delivered_to="notifyy1008+legal@gmail.com",
                                        subject="The Pulse #150", html="<p>" + "word " * 400 + "</p>")}
    # capture with the OLD rules (no pragmaticengineer override) -> lands in Legal
    old = [o for o in cfg.sender_overrides if o[0] != "pragmaticengineer"]
    cfg.sender_overrides[:] = old
    run_capture(cfg, fake_backend_cls(box))
    assert rows(cfg)["18f0aaaaaaaaaa01"]["segment"] == "3-Legal"
    path_before = rows(cfg)["18f0aaaaaaaaaa01"]["raw_eml_path"]
    # new rule added -> reindex
    cfg.sender_overrides.insert(0, ("pragmaticengineer", "1-TechAI"))
    assert run_reindex(cfg, dry_run=True).moves == {"3-Legal -> 1-TechAI": 1}
    assert rows(cfg)["18f0aaaaaaaaaa01"]["segment"] == "3-Legal"          # dry run wrote nothing
    r = run_reindex(cfg)
    assert r.changed == 1 and r.missing_file == 0
    after = rows(cfg)["18f0aaaaaaaaaa01"]
    assert after["segment"] == "1-TechAI" and after["raw_eml_path"] == path_before
    assert (cfg.archive_dir / path_before).exists()
    assert run_reindex(cfg).changed == 0                                  # idempotent
