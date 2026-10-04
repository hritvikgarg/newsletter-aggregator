"""Split behaviours tuned on the first real capture (synthetic HTML that mirrors each layout).

Real newsletter emails are not committed (copyright); these fixtures reproduce only the structure.
"""
from nlagg.clean import clean
from nlagg.split import split_blocks


def items(html, subject="s"):
    return split_blocks(clean(html, None), subject).items


def titles(html, subject="s"):
    return [(it.section, it.title, it.is_sponsor) for it in items(html, subject)]


P = lambda n, w="word": "<p>" + (w + " ") * n + "</p>"        # noqa: E731
LINK = lambda t, u="https://e.com/x": f"<a href='{u}'><b>{t}</b></a>"   # noqa: E731

# Superhuman-like: heading pairs (section + title), numbered news list, sponsor slots with a CTA button,
# emoji-labelled trending entries, feedback poll at the end.
SUPERHUMAN = (
    "<p>October 03, 2026 | Read online</p>" + P(40, "intro") +
    "<h3>WHAT’S NEXT</h3><h2>The most important news in robotics this week</h2>"
    "<p>Click here to see the clip. Photo: Figure AI</p>"
    "<p>1. <b>Your DoorDash order may soon arrive by drone</b>: " + "drone " * 40 + "</p>"
    "<p>2. <b>Figure retires its F.02 robots</b>: " + "robot " * 40 + "</p>"
    "<p>3. <b>Robots can already perform 74% of physical tasks</b>: " + "tasks " * 40 + "</p>"
    "<h3>SPONSORED BY MULTIVERSE COMPUTING</h3><h2>" + LINK("Frontier AI models, up to 4x faster") + "</h2>"
    + P(30, "ad") + "<p>Speed: Up to 4x faster than others</p>"
    "<p>" + LINK("Start your 30-day trial today.") + "</p>"
    "<h3>ROBOTS IN ACTION</h3><h2>How robots are transforming the world around us</h2>"
    "<p>🤝 Diplomacy GPT: " + "diplomacy " * 30 + "</p>"
    "<p>🌊 Wash Job: " + "laundry " * 30 + "</p>"
    "<h3>ROBOT OF THE WEEK</h3><h2>A robot that caught our eye this week</h2>" + P(40, "steak") +
    "<p><b>What did you think of today's email?</b></p><p>Your feedback helps me create better emails!</p>"
    "<p><a href='https://e.com/1'>Loved it 🧠🧠🧠</a></p><p><a href='https://e.com/2'>Terrible 🧠</a></p>"
    "<p>Until next time, the team.</p>"
)


def test_superhuman_numbered_and_emoji_entries_become_stories():
    t = titles(SUPERHUMAN)
    names = [x[1] for x in t]
    assert "Your DoorDash order may soon arrive by drone" in names
    assert "Figure retires its F.02 robots" in names
    assert "Diplomacy GPT" in names and "Wash Job" in names
    # entries keep the list heading as their section
    sec = dict((x[1], x[0]) for x in t)
    assert sec["Wash Job"] == "How robots are transforming the world around us"


def test_sponsor_label_covers_one_item_and_cta_is_folded():
    its = items(SUPERHUMAN)
    sponsors = [it.title for it in its if it.is_sponsor]
    assert sponsors == ["Frontier AI models, up to 4x faster"]           # not the stories after it
    assert not any((it.title or "").startswith("Start your 30-day") for it in its)
    ad = next(it for it in its if it.is_sponsor)
    assert any("e.com/x" in ln.url for ln in ad.links)                    # CTA link kept on the ad
    robot = next(it for it in its if it.title == "A robot that caught our eye this week")
    assert robot.section == "ROBOT OF THE WEEK" and not robot.is_sponsor


def test_feedback_poll_is_footer():
    names = [it.title for it in items(SUPERHUMAN)]
    assert not any(n and ("Loved it" in n or "Terrible" in n or "What did you think" in n) for n in names)


# The Neuron-like: plain "Label:" paragraphs are facets of ONE story, sticky "FROM OUR PARTNERS".
NEURON = (
    P(30, "welcome") +
    "<h2>" + LINK("😺 Trump and the AI CEOs signed a voluntary safety pact", "https://e.com/a") + "</h2>"
    "<p><b>First, the rebrand</b>: " + "rebrand " * 25 + "</p>"
    "<p><b>Why this matters</b>: " + "matters " * 25 + "</p>"
    "<h3>FROM OUR PARTNERS</h3><h2>" + LINK("The Enterprise Guide to Scalable AI", "https://e.com/ad") + "</h2>" + P(40, "ad") +
    "<h2>🎓 AI Skill of the Day: strip the who, keep the what</h2>" + P(60, "skill") +
    "<h2>📰 Around the Horn</h2>" + P(60, "horn") +
    "<p><a href='https://e.com/adv'>Advertise to 700K readers of The Neuron here!</a></p>"
)


def test_neuron_facets_not_split_and_partner_label_not_sticky():
    t = titles(NEURON)
    names = [x[1] for x in t]
    assert "First, the rebrand" not in names and "Why this matters" not in names
    flagged = [x[1] for x in t if x[2]]
    assert flagged == ["The Enterprise Guide to Scalable AI"]
    assert "🎓 AI Skill of the Day: strip the who, keep the what" in names
    assert not any("Advertise to 700K" in (n or "") for n in names)


# Substack-like post: title, subtitle, linked byline + date line, sections, Like/Comment/Restack footer.
SUBSTACK = (
    "<h1>" + LINK("Import AI 474: Platonic mindspace; TPUs in space", "https://importai.substack.com/p/474") + "</h1>"
    "<h3>Where do you exceed the capabilities of an LLM?</h3>"
    "<p>" + LINK("Jack Clark", "https://substack.com/@jackclark") + "</p>"
    "<p>Sep 28 READ IN APP Welcome to Import AI. " + "research " * 200 + "</p>"
    "<p>*******</p>"
    "<h2>In the blind mountain</h2>" + P(120, "fiction") +
    "<p><a href='https://e.com/like'>Like</a></p><p><a href='https://e.com/c'>Comment</a></p>"
    "<p><a href='https://e.com/r'>Restack</a></p><p>© 2026 Jack Clark 548 Market Street PMB 72296</p>"
)


def test_substack_post_is_one_essay_with_sections():
    its = items(SUBSTACK, subject="Import AI 474: Platonic mindspace; TPUs in space")
    # owner decision 2026-10-05: one Substack post = one item; sub-headings stay inside as sections
    assert [(it.kind, it.title) for it in its] == [("essay", "Import AI 474: Platonic mindspace; TPUs in space")]
    assert "## In the blind mountain" in its[0].body
    assert its[0].word_count > 300                   # byline + subtitle + intro + sections
    assert "Like" not in its[0].body.split() and "© 2026" not in its[0].body


# Pragmatic Engineer paid preview / podcast: sponsor list with no title, paywall cut-off.
PRAGMATIC = (
    "<h1>" + LINK("Why has Shopify dropped React Native?", "https://e.com/post") + "</h1>"
    "<p><a href='https://e.com/author'>Gergely Orosz</a></p><p>Sep 29</p><p><a href='https://e.com/app'>READ IN APP</a></p>"
    + P(60, "intro") +
    "<h2>Brought to You by</h2><p>• turbopuffer – " + "search " * 40 + "</p><p>• Linear – " + "agents " * 40 + "</p>"
    "<h2>1. Why Shopify chose React Native</h2>" + P(150, "history") +
    "<h2>Why performance wins</h2>" + P(150, "perf") +
    "<h2>4. Haven’t we seen this before?...</h2>"
    "<p>" + LINK("Subscribe to The Pragmatic Engineer to unlock the rest.", "https://e.com/sub") + "</p>"
    "<h2>A subscription gets you:</h2><p>Full articles every Tuesday and Thursday</p>"
)


def test_pragmatic_post_sponsor_list_and_paywall():
    its = items(PRAGMATIC, subject="Why has Shopify dropped React Native?")
    essay, ads = its[0], its[1:]
    assert essay.kind == "essay" and essay.title == "Why has Shopify dropped React Native?"
    assert "## 1. Why Shopify chose React Native" in essay.body and "## Why performance wins" in essay.body
    assert "turbopuffer" not in essay.body                              # the ad stays out of the post
    assert len(ads) == 1 and ads[0].is_sponsor and "turbopuffer" in ads[0].body
    assert "subscription gets you" not in essay.body and "unlock the rest" not in essay.body


# Untitled sponsor block: the label is used up by it, so the NEXT section is not a sponsor.
NEURON_UNTITLED_AD = (
    "<h2>🎓 AI Skill of the Day: branch a thread</h2>" + P(60, "skill") +
    "<p><b>FROM OUR PARTNERS</b></p>" + P(40, "durable") + "<p><b>Try chat.agent now.</b></p>"
    "<h2>🍪 Treats to Try</h2>" + P(80, "tool") +
    "<h2>📰 Around the Horn</h2>" + P(80, "news")
)


def test_untitled_sponsor_block_does_not_flag_next_section():
    t = titles(NEURON_UNTITLED_AD)
    flags = {x[1]: x[2] for x in t}
    assert flags["🍪 Treats to Try"] == 0 and flags["📰 Around the Horn"] == 0
    assert [x for x in t if x[2]] == [("FROM OUR PARTNERS", None, 1)]


# Superhuman ad with bold feature lines and a bold linked tagline: all one sponsor item.
STACKAI = (
    "<h3>TODAY IN AI</h3><h2>" + LINK("Microsoft combines Copilot’s tools", "https://e.com/ms") + "</h2>" + P(60, "copilot") +
    "<h3>PRESENTED BY STACKAI</h3><h2>" + LINK("Anyone can build an agent. But getting it past IT? Good luck", "https://e.com/ad") + "</h2>"
    "<p>Most no-code agents aren’t safe for enterprise scale. But StackAI satisfies builders and IT:</p>"
    "<p><b>Fast: Chat assistants, forms, Slack apps, and API endpoints in minutes</b></p>"
    "<p>Safe: Governed deployments w/ review and rollback, plus RBAC, SSO, audit logs</p>"
    "<p>" + LINK("Scale AI across your entire enterprise in just weeks.", "https://e.com/ad2") + "</p>"
    "<h3>FROM THE FRONTIER</h3><h2>AI’s progress keeps speeding up</h2>" + P(120, "frontier")
)


def test_sponsor_feature_lines_stay_in_the_ad():
    t = titles(STACKAI)
    assert [x[1] for x in t] == ["Microsoft combines Copilot’s tools",
                                 "Anyone can build an agent. But getting it past IT? Good luck",
                                 "AI’s progress keeps speeding up"]
    assert [x[2] for x in t] == [0, 1, 0]


# A list heading whose only text is a photo caption is not a story; a linked sentence
# (embedded post card) inside a story is not a new story.
CAPTION_AND_EMBED = (
    P(30, "intro") +
    "<h3>WHAT’S NEXT</h3><h2>The most important news in robotics this week</h2>"
    "<p>Click here to see the clip. Photo: Figure AI</p>"
    "<p>1. <b>Your DoorDash order may soon arrive by drone</b>: " + "drone " * 40 + "</p>"
    "<p>2. <b>Figure retires its F.02 robots</b>: " + "robot " * 40 + "</p>"
    "<h2>😺 OpenAI launched Dots</h2>" + P(60, "dots") + "<p>What’s a dot? One of these little guys:</p>"
    "<p><a href='https://x.com/openai/1'>Introducing dots, always-on agents built to handle everything.</a></p>"
    + P(60, "more") +
    "<h2>📰 Around the Horn</h2>" + P(60, "horn")
)


def test_caption_heading_dropped_and_embed_stays_in_story():
    names = [it.title for it in items(CAPTION_AND_EMBED)]
    assert "The most important news in robotics this week" not in names
    assert "Your DoorDash order may soon arrive by drone" in names
    assert not any(n and n.startswith("Introducing dots") for n in names)
    dots = next(it for it in items(CAPTION_AND_EMBED) if it.title == "😺 OpenAI launched Dots")
    assert "Introducing dots" in dots.body and dots.word_count > 120


def test_tldr_job_ad_is_dropped():
    html = ("<h1>Miscellaneous</h1><p>" + LINK("Xbox's CEO Isn't Playing Around (13 minute read)") + "</p>" + P(40, "x") +
            "<p>" + LINK("Product Manager, Applied AI at TLDR ($225k base + $60k bonus, Fully Remote)") + "</p>" + P(40, "job"))
    assert [x[1] for x in titles(html)] == ["Xbox's CEO Isn't Playing Around (13 minute read)"]


# TLDR-like masthead: "Sign Up | Advertise | View Online", a lone "TLDR", and "Together With" + logo link.
TLDR = (
    "<p><a href='https://tldr.tech/signup'>Sign Up</a> |<a href='https://advertise.tldr.tech'>Advertise</a>|"
    "<a href='https://a.tldr/web'>View Online</a></p><p>TLDR</p>"
    "<p><b>Together <a href='https://www.modulate.ai/'>With</a></b></p>"
    "<h1>TLDR 2026-10-01</h1>"
    "<p>" + LINK("Modulate raises $25M for voice AI (Sponsor)", "https://www.modulate.ai/pr") + "</p>" + P(40, "ad") +
    "<p>" + LINK("Read the report", "https://www.modulate.ai/report") + "</p>"
    "<h1>Big Tech &amp; Startups</h1>"
    "<p>" + LINK("Google announces Gemini 4 (3 minute read)", "https://e.com/g") + "</p>" + P(40, "gemini") +
    "<p>" + LINK("Track your referrals here.", "https://refer.tldr.tech/x") + "</p>"
    "<p><b>Want to work at TLDR? 💼</b></p>" + P(30, "jobs")
)


def test_tldr_masthead_together_with_and_cta():
    t = titles(TLDR)
    assert [x[1] for x in t] == ["Modulate raises $25M for voice AI (Sponsor)", "Google announces Gemini 4 (3 minute read)"]
    assert [x[2] for x in t] == [1, 0]


# Pragmatic Engineer podcast notes: masthead, durations, Timestamps / References, @handles.
PODCAST = (
    "<h1>" + LINK("Distributed databases with Peter Mattis", "https://e.com/pod") + "</h1>" + P(60, "summary") +
    "<p>" + LINK("The Pragmatic Engineer", "https://e.com/home") + "</p>"
    "<p>" + LINK("1:25:32", "https://e.com/play") + "</p>"
    "<h2>Takeaways from the conversation with Peter</h2>" + P(200, "takeaway") +
    "<h2>Timestamps</h2>" + P(80, "00:01") + "<h2>References</h2>" + P(40, "ref") +
    "<p><b>@thsottiaux</b></p>" + P(20, "bio")
)


def test_podcast_notes_boilerplate():
    names = [it.title for it in items(PODCAST)]
    assert names == ["Distributed databases with Peter Mattis", "Takeaways from the conversation with Peter"]


def test_numbered_quotes_inside_an_essay_are_not_split():
    essay = ("<h2>" + LINK("Import AI 472: cheating math agents", "https://e.com/472") + "</h2>"
             + P(400, "essay") +
             "<p>1. Prover-beta”: " + "quote " * 20 + "</p><p>2. Prover-rho”: " + "quote " * 20 + "</p>" + P(200, "more"))
    its = items(essay, subject="Import AI 472")
    assert len(its) == 1 and its[0].kind == "essay"          # one post, not 3 stories
    assert "Prover-beta" in its[0].body


def test_mime_encoded_display_name_is_decoded(cfg):
    from nlagg.parse_email import build_row
    raw = (b"From: Superhuman =?windows-1251?b?lg==?= Zain Kahn <superhuman@mail.joinsuperhuman.ai>\r\n"
           b"To: notifyy1008+techai@gmail.com\r\nDelivered-To: notifyy1008+techai@gmail.com\r\n"
           b"Subject: x\r\nDate: Mon, 05 Oct 2026 07:01:00 -0400\r\nContent-Type: text/plain\r\n\r\nhi\r\n")
    r = build_row(raw, "x", inbox_address=cfg.inbox_address, tag_to_segment=cfg.tag_to_segment,
                  sender_overrides=cfg.sender_overrides, sender_fallbacks=cfg.sender_fallbacks)
    assert r["sender_name"] == "Superhuman \u2013 Zain Kahn"
    assert r["source_key"] == "superhuman@mail.joinsuperhuman.ai|superhuman zain kahn"
