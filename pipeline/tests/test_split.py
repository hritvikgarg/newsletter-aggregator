from pathlib import Path

from nlagg.clean import Block, Link, clean, text_to_blocks
from nlagg.split import classify_promo, split_blocks

FIX = Path(__file__).parent / "fixtures"


def split_fixture(name, subject="Subject"):
    return split_blocks(clean((FIX / f"{name}.html").read_text(encoding="utf-8"), None), subject)


def test_roundup_tldr():
    r = split_fixture("roundup_tldr", "OpenAI ships agents")
    assert r.shape == "roundup" and r.is_promo == 0
    assert [(it.section, it.title) for it in r.items] == [
        ("Headlines & Launches", "OpenAI launches Agents SDK 2.0 (4 minute read)"),
        ("Headlines & Launches", "Nvidia beats estimates on data center demand (3 minute read)"),
        ("Headlines & Launches", "Build internal AI tools in minutes with Retool (Sponsor)"),
        ("Deep Dives & Analysis", "Why most LLM evals fail in production (8 minute read)"),
        ("Quick Links", "TinyLLM (GitHub Repo)"),   # survives the referral block right after it
    ]
    assert [it.is_sponsor for it in r.items] == [0, 0, 1, 0, 0]
    assert r.items[1].url == "https://www.reuters.com/tech/nvidia-q3/"
    assert r.items[0].body.startswith("OpenAI released a new version")
    assert "referral" not in " ".join(it.body for it in r.items).lower()
    assert [it.position for it in r.items] == [1, 2, 3, 4, 5]


def test_sectioned_brew():
    r = split_fixture("sectioned_brew", "☕ Fed holds")
    kinds = [(it.kind, it.section, it.title) for it in r.items]
    assert kinds == [
        ("intro", None, None),
        ("story", "MARKETS", "The Fed holds steady, stocks don't"),
        ("story", "MARKETS", "Oil slips on supply news"),
        ("story", "TOGETHER WITH VANTA", "Get compliant without the spreadsheets"),
        ("story", "TECH", "Apple's chip team loses another leader"),
    ]                                          # "Share the Brew" referral block dropped
    fed = r.items[1]
    assert "Powell" in fed.body and fed.url == "https://www.cnbc.com/2026/10/01/fed-decision.html"
    assert [it.is_sponsor for it in r.items] == [0, 0, 0, 1, 0]


def test_essay_is_one_item():
    r = split_fixture("essay_diff", "The Economics of Inference")
    assert r.shape == "essay" and len(r.items) == 1
    it = r.items[0]
    assert it.kind == "essay" and it.title == "The Economics of Inference" and it.word_count > 250


def test_teaser_points_to_full_post():
    r = split_fixture("teaser_substack", "Three micro-caps to watch")
    assert r.shape == "teaser" and len(r.items) == 1
    it = r.items[0]
    assert it.kind == "teaser"
    assert it.url == "https://planetmicrocap.substack.com/p/three-names-to-watch"
    assert "Keep reading" not in it.body and "Start trial" not in it.body


def test_sponsor_word_in_news_is_not_an_ad():
    blocks = [
        Block("heading", "Olympics sponsor pulls out of Paris deal", 2, links=[Link("x", "https://e.com/a", "source")], link_full=True),
        Block("text", "The sponsor cited costs. Organizers are looking for a replacement."),
        Block("heading", "Second story", 2, links=[Link("y", "https://e.com/b", "source")], link_full=True),
        Block("text", "Body two."),
    ]
    r = split_blocks(blocks, "news")
    assert [it.is_sponsor for it in r.items] == [0, 0]


def test_promo_mail():
    assert classify_promo("50% off TLDR Pro — last chance", 0) == 1
    assert classify_promo("Join our webinar on AI agents", 1) == 1
    # a real issue whose subject mentions a sale is not a promo mail (many stories)
    assert classify_promo("Black Friday sale numbers disappoint; Nvidia slips", 6) == 0
    assert classify_promo("Nvidia beats estimates", 0) == 0


def test_plain_text_issue():
    text = ("TOP STORIES\n\nNvidia beats estimates https://reuters.com/x?utm_source=tldr\n\n"
            "Revenue rose 62% on data center demand.\n\nOpenAI ships agents https://openai.com/a\n\n"
            "A new SDK with tracing.")
    r = split_blocks(text_to_blocks(text), "Daily")
    titles = [it.title for it in r.items if it.kind == "story"]
    assert titles == ["Nvidia beats estimates", "OpenAI ships agents"]
    assert r.items[-1].url == "https://openai.com/a"


def test_empty():
    assert split_blocks([], "x").shape == "empty"
