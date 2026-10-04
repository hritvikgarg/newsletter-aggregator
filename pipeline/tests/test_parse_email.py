from nlagg.config import load_tag_map, REPO_ROOT
from nlagg.parse_email import build_row, is_issue, detect_platform, parse_raw


def row(cfg, raw, gid="abc123abc123"):
    return build_row(raw, gid, inbox_address=cfg.inbox_address, tag_to_segment=cfg.tag_to_segment,
                     sender_overrides=cfg.sender_overrides)


def test_tag_map_matches_category_feeds():
    m = load_tag_map(REPO_ROOT / "data" / "category_feeds.csv")
    assert m["techai"] == "1-TechAI"
    assert m["hr"] == "4-HRPeopleOps"
    assert m["news"] == "9-MainstreamNews"
    assert len(m) == 9


def test_routing(cfg, mailbox):
    seg = {gid: row(cfg, raw, gid)["segment"] for gid, raw in mailbox.items()}
    assert seg["18f0a1b2c3d4e5f6"] == "1-TechAI"        # +techai tag
    assert seg["18f0a1b2c3d4e5f7"] == "2-BizFinance"    # Morning Brew sender rule
    assert seg["18f0a1b2c3d4e5f8"] == "4-HRPeopleOps"   # HR Brew beats morningbrew.com rule
    assert seg["18f0a1b2c3d4e5f9"] == "3-Legal"         # Lawfare override beats +techai tag
    assert seg["18f0a1b2c3d4e5fc"] == "unmatched"


def test_fields(cfg, mailbox):
    r = row(cfg, mailbox["18f0a1b2c3d4e5f6"], "18f0a1b2c3d4e5f6")
    assert r["sender_email"] == "dan@tldrnewsletter.com"
    assert r["sender_name"] == "TLDR"
    assert r["source_key"] == "dan@tldrnewsletter.com"
    assert r["sent_date"] == "2026-10-05T11:01:00+00:00"
    assert r["received_date"] == "2026-10-05T11:01:02+00:00"
    assert r["is_issue"] == 1
    assert r["sending_platform"] == "beehiiv"
    assert r["has_html"] == 1 and r["has_text"] == 1
    assert 460 <= r["word_count"] <= 465         # style/head stripped
    assert r["reading_minutes"] == 2.0
    assert len(r["content_hash"]) == 64
    assert "notifyy1008+techai@gmail.com" in r["to_address"]


def test_attachment_and_html_less(cfg, mailbox):
    r = row(cfg, mailbox["18f0a1b2c3d4e5fc"])
    assert r["has_attachments"] == 1 and r["has_html"] == 0 and r["has_text"] == 1


def test_is_issue_rules():
    assert is_issue("Welcome to Import AI!") == 0
    assert is_issue("Please confirm your subscription") == 0
    assert is_issue("Your verification code") == 0
    assert is_issue("") == 0
    assert is_issue("Nvidia beats; AMD slips") == 1


def test_platform_substack():
    from tests.conftest import make_eml
    m = parse_raw(make_eml(frm="a@b.c", to="x@y.z", subject="s", headers={"List-Id": "<foo.substack.com>"}))
    assert detect_platform(m) == "substack"
