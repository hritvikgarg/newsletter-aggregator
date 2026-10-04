import json
from datetime import datetime

from tests.conftest import FakeChat, seed_items
from nlagg import db
from nlagg.cluster import clean_title, run_cluster, tokens, validate_points
from nlagg.links import resolve_for_items, resolve_url

END = datetime.fromisoformat("2026-10-02T12:00:00+00:00")
T1, T2 = "2026-10-01T10:00:00+00:00", "2026-10-02T09:00:00+00:00"

STORIES = [
    # one story, three newsletters
    {"key": "tldr_meta", "sender": "TLDR", "gmail_id": "tldr1", "sent": T1, "position": 1,
     "title": "Meta Launches Lightweight $1,299 VR Headset That Looks Like Glasses (5 minute read)",
     "body": "Meta unveiled lightweight VR glasses at its Connect event priced at $1,299, alongside a Muse charm device."},
    {"key": "sh_meta", "sender": "Superhuman", "gmail_id": "sh1", "sent": T1, "position": 1,
     "title": "Meta unveils a Muse device, VR glasses, and more, at Connect",
     "body": "At Connect, Meta showed VR glasses costing $1,299 and a small Muse device that clips to your shirt."},
    {"key": "neu_meta", "sender": "The Neuron", "gmail_id": "neu1", "sent": T2, "position": 1,
     "title": "😺 Meta is putting Muse on your glasses and in your pocket",
     "body": "Meta Connect brought VR glasses and a pocket Muse device. The glasses cost $1,299 and ship next month."},
    # unrelated stories
    {"key": "tldr_rsa", "sender": "TLDR", "gmail_id": "tldr1", "sent": T1, "position": 2,
     "title": "There's a new way to break RSA that's faster than anything (5 minute read)",
     "body": "Researchers describe a factoring method that breaks RSA keys faster using lattice reduction tricks."},
    {"key": "sh_waymo", "sender": "Superhuman", "gmail_id": "sh1", "sent": T1, "position": 2,
     "title": "Waymo is scaling fast in Phoenix", "body": "Waymo fleet data shows robotaxi rides doubled in Phoenix."},
    # same newsletter, recurring section, two days: must not merge with each other
    {"key": "neu_treats1", "sender": "The Neuron", "gmail_id": "neu0", "sent": T1, "title": "🍪 Treats to Try",
     "body": "Tools: a note app, a voice agent, a meeting recorder and a coding assistant for teams."},
    {"key": "neu_treats2", "sender": "The Neuron", "gmail_id": "neu1", "sent": T2, "position": 2,
     "title": "🍪 Treats to Try", "body": "Tools: a note app, a voice agent, a meeting recorder and a coding assistant."},
    # outside the window
    {"key": "old", "sender": "TLDR", "gmail_id": "tldr0", "sent": "2026-09-20T10:00:00+00:00",
     "title": "Meta Connect preview: VR glasses expected", "body": "Meta is expected to show VR glasses at Connect."},
    # sponsor never clusters
    {"key": "ad", "sender": "TLDR", "gmail_id": "tldr1", "sent": T1, "position": 3, "is_sponsor": 1,
     "title": "Try our Meta Connect VR glasses ad (Sponsor)", "body": "Meta Connect VR glasses $1,299 Muse."},
]


def stories_by_item(cfg):
    conn = db.connect(cfg.db_path)
    rows = conn.execute("""SELECT s.id, s.source_count, s.headline, si.item_id FROM stories s
                           JOIN story_items si ON si.story_id = s.id""").fetchall()
    conn.close()
    return rows


def test_cluster_groups_same_story_across_newsletters(cfg):
    ids = seed_items(cfg, STORIES)
    r = run_cluster(cfg, end=END, analyze=False)
    rows = stories_by_item(cfg)
    story_of = {row["item_id"]: row["id"] for row in rows}
    assert story_of[ids["tldr_meta"]] == story_of[ids["sh_meta"]] == story_of[ids["neu_meta"]]
    meta = next(row for row in rows if row["item_id"] == ids["tldr_meta"])
    assert meta["source_count"] == 3
    for k in ("tldr_rsa", "sh_waymo", "neu_treats1", "neu_treats2"):
        assert story_of[ids[k]] != story_of[ids["tldr_meta"]]
    assert story_of[ids["neu_treats1"]] != story_of[ids["neu_treats2"]]       # one newsletter never counts twice
    assert ids["old"] not in story_of and ids["ad"] not in story_of
    assert r.multi_source == 1 and r.items == 7


def test_cluster_is_rerunnable(cfg):
    seed_items(cfg, STORIES)
    a = run_cluster(cfg, end=END, analyze=False)
    b = run_cluster(cfg, end=END, analyze=False)
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == b.stories == a.stories
    conn.close()


def test_analysis_points_are_validated(cfg):
    ids = seed_items(cfg, STORIES)
    t, s, n = ids["tldr_meta"], ids["sh_meta"], ids["neu_meta"]

    def reply(model, messages):
        assert model == "llama-3.3-70b-versatile"
        return {"consensus": [{"text": "Meta's VR glasses cost $1,299.", "items": [f"i{t}", s]},
                              {"text": "The glasses cost $999.", "items": [t]},           # number not in source
                              {"text": "Apple will respond.", "items": [99999]}],          # unknown item
                "divergence": [{"text": "Only The Neuron says the glasses ship next month.", "items": [n]}]}

    r = run_cluster(cfg, end=END, client=FakeChat(reply))
    assert r.analyzed == 1
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT consensus, divergence FROM stories WHERE source_count = 3").fetchone()
    assert json.loads(row["consensus"]) == [{"text": "Meta's VR glasses cost $1,299.", "items": [t, s]}]
    assert json.loads(row["divergence"])[0]["items"] == [n]
    conn.close()


def test_no_llm_key_skips_analysis(cfg, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    seed_items(cfg, STORIES)
    assert run_cluster(cfg, end=END).analyzed == 0


def test_tokens_and_titles():
    assert clean_title("😺 Meta ships it (5 minute read)") == "Meta ships it"
    assert "model" in tokens("New models from the lab")


def test_validate_points_parses_ids():
    allowed = {5: "Meta glasses cost $1,299"}
    assert validate_points([{"text": "Costs $1,299", "items": ["[i5]"]}], allowed) == [{"text": "Costs $1,299", "items": [5]}]
    assert validate_points([{"text": "x", "items": []}], allowed) == []


def test_resolve_tracked_links_once(cfg):
    ids = seed_items(cfg, [
        {"key": "a", "sender": "The Neuron", "sent": T1, "title": "x", "body": "y",
         "url": "https://link.mail.beehiiv.com/ss/c/abc"},
        {"key": "b", "sender": "TLDR", "sent": T1, "title": "x", "body": "y", "url": "https://techcrunch.com/story"}])
    calls = []

    def fetch(url, method):
        calls.append((url, method))
        if "beehiiv" in url:
            return 302, "https://www.theverge.com/2026/10/1/meta?utm_source=neuron"
        return 200, None

    conn = db.connect(cfg.db_path)
    out = resolve_for_items(conn, [ids["a"], ids["b"]], fetch=fetch)
    assert out == {ids["a"]: "https://www.theverge.com/2026/10/1/meta", ids["b"]: "https://techcrunch.com/story"}
    resolve_for_items(conn, [ids["a"]], fetch=fetch)
    assert len(calls) == 1                                      # cached: the tracker is clicked once
    conn.close()


def test_resolve_url_failure_keeps_none():
    assert resolve_url("https://link.mail.beehiiv.com/x", fetch=lambda u, m: (404, None)) is None
