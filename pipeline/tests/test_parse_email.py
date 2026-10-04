from nlagg.config import load_tag_map, REPO_ROOT
from nlagg.parse_email import build_row, is_issue, detect_platform, parse_raw


def row(cfg, raw, gid="abc123abc123"):
    return build_row(raw, gid, inbox_address=cfg.inbox_address, tag_to_segment=cfg.tag_to_segment,
                     sender_overrides=cfg.sender_overrides, sender_fallbacks=cfg.sender_fallbacks)


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
    assert r["source_key"] == "dan@tldrnewsletter.com|tldr"          # no List-Id -> display name
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
    assert is_issue("Welcome to Import AI!", 80) == 0
    assert is_issue("Please confirm your subscription", 30) == 0
    assert is_issue("Your verification code", 12) == 0
    assert is_issue("", 40) == 0
    assert is_issue("Nvidia beats; AMD slips", 30) == 1
    # Real issues whose subject happens to match must survive (long body)
    assert is_issue("Welcome to the AI bubble", 1200) == 1
    assert is_issue("How to verify AI output", 800) == 1
    assert is_issue("", 900) == 1
    assert is_issue("Welcome!", 249) == 0 and is_issue("Welcome!", 250) == 1


def test_platform_substack():
    from tests.conftest import make_eml
    m = parse_raw(make_eml(frm="a@b.c", to="x@y.z", subject="s", headers={"List-Id": "<foo.substack.com>"}))
    assert detect_platform(m) == "substack"


def test_dotenv_with_bom_and_quotes(tmp_path, monkeypatch):
    """Windows Notepad can save .env with a UTF-8 BOM; the first key must still load."""
    from nlagg.config import _load_dotenv
    monkeypatch.delenv("NLAGG_TEST_USER", raising=False)
    monkeypatch.delenv("NLAGG_TEST_PW", raising=False)
    env = tmp_path / ".env"
    env.write_bytes("\ufeffNLAGG_TEST_USER=me@example.com\r\n# comment\r\nNLAGG_TEST_PW=\"abcd efgh\"\r\n".encode("utf-8"))
    _load_dotenv(env)
    import os
    assert os.environ["NLAGG_TEST_USER"] == "me@example.com"
    assert os.environ["NLAGG_TEST_PW"] == "abcd efgh"


def test_source_key_separates_newsletters_from_one_sender():
    from tests.conftest import make_eml
    from nlagg.parse_email import source_key
    def key(name, list_id=None):
        h = {"List-Id": list_id} if list_id else {}
        m = parse_raw(make_eml(frm=f"{name} <dan@tldrnewsletter.com>", to="x@y.z", subject="s", headers=h))
        return source_key(m, "dan@tldrnewsletter.com", name)
    assert len({key("TLDR"), key("TLDR AI"), key("TLDR Web Dev")}) == 3
    assert key("TLDR AI") == key("TLDR AI")                           # stable
    assert key("Anything", "HR Dive <hr.divenewsletter.com>") == "dan@tldrnewsletter.com|hr.divenewsletter.com"


def test_routing_overrides_from_first_real_capture(cfg):
    from nlagg.parse_email import route_segment
    r = lambda sender, tags: route_segment(sender, tags, cfg.tag_to_segment, cfg.sender_overrides)
    assert r("pragmaticengineer@substack.com", ["legal"]) == "1-TechAI"
    assert r("pragmaticengineer+deepdives@substack.com", ["legal"]) == "1-TechAI"
    assert r("importai@substack.com", ["legal"]) == "1-TechAI"
    assert r("lawfare+today-on-lawfare@substack.com", ["techai"]) == "3-Legal"


def test_fallbacks_route_plain_address_picks_only(cfg):
    from nlagg.parse_email import route_segment
    def r(name, sender, tags=()):
        return route_segment(sender, list(tags), cfg.tag_to_segment, cfg.sender_overrides,
                             cfg.sender_fallbacks, name)
    # picks that also arrive at the plain address (no +tag)
    assert r("TLDR", "dan@tldrnewsletter.com") == "1-TechAI"
    assert r("Dru Riley @ Trends.vc", "d@trends.vc") == "6-IndieHacker"
    assert r("The National Law Review - Jack Baudoin", "klappe@natlawreview.com") == "3-Legal"
    assert r("The National Law Review", "publicnotices@natlawreview.com") == "3-Legal"
    # one sending service, split by display name
    assert r("HR Brew", "morningbrew@mail.sailthru.com") == "4-HRPeopleOps"
    assert r("Morning Brew", "morningbrew@mail.sailthru.com") == "2-BizFinance"
    assert r("Brew Markets", "morningbrew@mail.sailthru.com") == "2-BizFinance"
    # a +tag always beats a fallback (TLDR Web Dev on +github stays in GitHub)
    assert r("TLDR Web Dev", "dan@tldrnewsletter.com", ["github"]) == "5-GitHubRepos"
    # newsletters we did not pick stay unmatched (owner decision 2026-10-04)
    assert r("Axios Markets", "markets@axios.com") == "unmatched"
    assert r("Bloomberg Businessweek", "noreply@news.bloomberg.com") == "unmatched"
    assert r("Finimize", "finimize@mail.sailthru.com") == "unmatched"


def test_sender_with_unquoted_at_in_display_name(cfg):
    """Raw header exactly as some senders write it; parseaddr alone returns ('', '')."""
    from nlagg.parse_email import parse_sender
    assert parse_sender("Dru Riley @ Trends.vc <d@trends.vc>") == ("Dru Riley @ Trends.vc", "d@trends.vc")
    assert parse_sender('"Dru Riley @ Trends.vc" <d@trends.vc>') == ("Dru Riley @ Trends.vc", "d@trends.vc")
    assert parse_sender("TLDR <dan@tldrnewsletter.com>") == ("TLDR", "dan@tldrnewsletter.com")
    assert parse_sender("plain@example.com") == ("", "plain@example.com")
    raw = (b"From: Dru Riley @ Trends.vc <d@trends.vc>\r\nTo: notifyy1008@gmail.com\r\n"
           b"Subject: You're in a Status Game\r\nDate: Mon, 05 Oct 2026 07:01:00 -0400\r\n"
           b"Content-Type: text/plain; charset=utf-8\r\n\r\nHello\r\n")
    r = row(cfg, raw)
    assert (r["sender_name"], r["sender_email"]) == ("Dru Riley @ Trends.vc", "d@trends.vc")
    assert r["segment"] == "6-IndieHacker"                 # plain address -> fallback rule
    assert r["source_key"].startswith("d@trends.vc|")
