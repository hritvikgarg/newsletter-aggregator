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
        "hook": f"Meta's Connect event was the story of the day [i{t}][i{s}][i{n}].",
        "top_story": {"headline": "Meta shows VR glasses and a Muse device",
                      "paragraphs": [f"Meta unveiled lightweight VR glasses priced at $1,299 [i{t}].",
                                     f"One report says they ship next month [i{n}]."
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
    assert "[Read story](https://news.example/0)" in md and "[i" not in md
    html = r.html_path.read_text(encoding="utf-8")
    assert "Read story" in html and "Meta puts Muse on your face" in html
    for name in ("TLDR", "Superhuman", "The Neuron", "newsletter", "[i"):        # reads as our own publication
        assert name not in html.split("<body")[1] and name not in md
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
    assert "TOP" in roles and "widely covered today" in roles
    assert "TLDR" not in roles and "Superhuman" not in roles           # the writer never sees newsletter names


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
    assert "names the newsletter" in c.check("TLDR says OpenAI raised $40 billion [i1].")
    assert c.check("The newsletter says OpenAI raised money [i1].") == "mentions newsletters"
    assert "number" in c.check("OpenAI raised $50 billion [i1].")
    assert "name" in c.check("OpenAI and Microsoft raised money [i1].")
    assert c.check("OpenAI raised money.") == "no citation"
    assert "copies" in c.check("OpenAI raised $40 billion from SoftBank, Sam Altman said on Monday [i1].")
    assert c.check("Two reports put it differently and OpenAI raised money [i1].") is None


def test_sentence_split_keeps_dangling_ids():
    assert sentences("A thing happened. [i1] Then another [i2].") == ["A thing happened. [i1]", "Then another [i2]."]


def test_links_go_to_original_articles_only():
    from nlagg.compose import Linker, is_newsletter_url
    assert is_newsletter_url("https://archive.superhuman.ai/123") and is_newsletter_url("https://link.mail.beehiiv.com/x")
    assert is_newsletter_url("https://pragmaticengineer.substack.com/p/x")
    assert not is_newsletter_url("https://apnews.com/article/spacex") and not is_newsletter_url("https://www.anthropic.com/research/x")
    lk = Linker({1: "https://apnews.com/a", 2: "https://apnews.com/a", 3: ""})
    assert lk.for_text("x [i1]") == "https://apnews.com/a" and lk.for_text("y [i2]") == ""   # same link once
    assert Linker({1: "https://apnews.com/a"}, "none").for_text("x [i1]") == ""


def test_page_image_found_and_cached(cfg):
    from nlagg.links import find_page_image, page_images
    html = '<html><head><meta property="og:image" content="/img/hero.jpg?w=1200&amp;h=630"></head></html>'
    assert find_page_image(html, "https://apnews.com/article/x") == "https://apnews.com/img/hero.jpg?w=1200&h=630"
    assert find_page_image('<meta name="twitter:image" content="https://cdn.x.com/a.png">', "https://x.com") == "https://cdn.x.com/a.png"
    assert find_page_image("<p>no meta</p>", "https://x.com") is None
    conn = db.connect(cfg.db_path)
    calls = []
    fetch = lambda u: (calls.append(u), html)[1]                                    # noqa: E731
    assert page_images(conn, ["https://apnews.com/article/x"], fetch) == {
        "https://apnews.com/article/x": "https://apnews.com/img/hero.jpg?w=1200&h=630"}
    page_images(conn, ["https://apnews.com/article/x"], fetch)
    assert len(calls) == 1
    conn.close()


def test_cards_render_images_headlines_and_fallback(cfg, tmp_path, monkeypatch):
    ids = prepare(cfg)
    monkeypatch.setattr(cfg, "repo_root", tmp_path)
    cfg.raw["compose"]["images"] = "article"
    reply = compose_reply(ids, bad=False)
    reply["quick_hits"][0]["headline"] = "RSA keys break faster"
    r = run_compose(cfg, issue_date="2026-10-02", client=FakeChat(lambda m, x: reply), resolve_links=True,
                    fetch=lambda u, m: (200, None),
                    fetch_html=lambda u: '<meta property="og:image" content="https://img.example/meta.jpg">'
                    if u.endswith("/0") else "")
    html = r.html_path.read_text(encoding="utf-8")
    assert 'src="https://img.example/meta.jpg"' in html                     # featured card image
    assert "linear-gradient" in html                                         # card without an image
    assert "RSA keys break faster" in html and "Read story" in html and "Launch" in html
    assert "October 1, 2026" in html


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40


def test_ai_images_generated_once_and_used_in_cards(cfg, tmp_path, monkeypatch):
    ids = prepare(cfg)
    monkeypatch.setattr(cfg, "repo_root", tmp_path)
    cfg.raw["compose"]["images"] = "ai"
    cfg.raw["compose"]["ai_images"] = {"provider": "pollinations", "base_url": "https://gen.pollinations.ai", "model": "flux"}
    reply = compose_reply(ids, bad=False)
    reply["top_story"]["image"] = "VR glasses floating above a stage [i1] with the Meta logo"
    reply["quick_hits"][0]["image"] = "a padlock cracking open"
    urls = []

    def get(url, headers, timeout):
        urls.append(url)
        return PNG

    r = run_compose(cfg, issue_date="2026-10-02", client=FakeChat(lambda m, x: reply), resolve_links=False,
                    image_kw={"get": get})
    assert r.images == 2 and len(urls) == 2
    assert urls[0].startswith("https://gen.pollinations.ai/image/") and "nologo=true" in urls[0]
    assert "Meta%20logo" not in urls[0] and "%5Bi1%5D" not in urls[0]                # brief cleaned
    html = r.html_path.read_text(encoding="utf-8")
    assert html.count('src="img/') == 2 and (r.html_path.parent / "img").is_dir()
    run_compose(cfg, issue_date="2026-10-02", client=FakeChat(lambda m, x: reply), resolve_links=False,
                image_kw={"get": get}, force=True)
    assert len(urls) == 2                                   # same briefs -> files reused, no new generation


def test_failed_image_falls_back_to_tile(cfg, tmp_path, monkeypatch):
    ids = prepare(cfg)
    monkeypatch.setattr(cfg, "repo_root", tmp_path)
    reply = compose_reply(ids, bad=False)
    reply["top_story"]["image"] = "a rocket"

    def get(url, headers, timeout):
        raise OSError("service down")

    r = run_compose(cfg, issue_date="2026-10-02", client=FakeChat(lambda m, x: reply), resolve_links=False,
                    image_kw={"get": get})
    assert r.images == 0 and "linear-gradient" in r.html_path.read_text(encoding="utf-8")


def test_openai_compatible_provider():
    import base64
    from nlagg.images import ImageSettings, generate
    s = ImageSettings(provider="openai_compatible", base_url="https://api.together.xyz/v1", model="flux")
    seen = {}

    def post(url, body, headers, timeout):
        seen.update(url=url, body=body)
        return {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}

    assert generate("a rocket", s, 1, post=post) == PNG
    assert seen["url"].endswith("/images/generations") and seen["body"]["size"] == "1200x630"


def test_cloudflare_provider():
    import base64
    from nlagg.images import ImageSettings, generate
    s = ImageSettings(provider="cloudflare", base_url="https://api.cloudflare.com/client/v4",
                      model="@cf/black-forest-labs/flux-1-schnell", api_key="tok", account_id="acc")
    seen = {}

    def post(url, body, headers, timeout):
        seen.update(url=url, headers=headers)
        return {"result": {"image": base64.b64encode(PNG).decode()}, "success": True}

    assert generate("a rocket", s, 7, post=post) == PNG
    assert seen["url"] == "https://api.cloudflare.com/client/v4/accounts/acc/ai/run/@cf/black-forest-labs/flux-1-schnell"
    assert seen["headers"] == {"Authorization": "Bearer tok"}
