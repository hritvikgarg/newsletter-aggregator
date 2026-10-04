from pathlib import Path

import pytest

from nlagg.clean import canonical_url, clean, html_to_blocks, link_type, strip_boilerplate, text_to_blocks, to_markdown

FIX = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("raw,expected,kind", [
    # TLDR wraps the destination percent-encoded in the path; utm params are dropped
    ("https://tracking.tldrnewsletter.com/CL0/https:%2F%2Fwww.theverge.com%2F2026%2Fai%3Futm_source=tldrai/1/0100019a/abc=",
     "https://www.theverge.com/2026/ai", "source"),
    ("https://www.google.com/url?q=https://example.com/a%3Fb%3D1&sa=D", "https://example.com/a?b=1", "source"),
    # only tracking-only params are stripped; generic ones (id, l) survive
    ("https://example.com/x?utm_source=tldr&id=5&l=en&mc_cid=1&fbclid=z", "https://example.com/x?id=5&l=en", "source"),
    ("https://foo.substack.com/p/bar?utm_medium=email", "https://foo.substack.com/p/bar", "source"),
    # opaque trackers can't be unwrapped offline -> kept, marked tracked
    ("https://substack.com/redirect/2/eyJlIjoiaHR0cHM6Ly9leGFtcGxlLmNvbSJ9",
     "https://substack.com/redirect/2/eyJlIjoiaHR0cHM6Ly9leGFtcGxlLmNvbSJ9", "tracked"),
    ("https://link.mail.beehiiv.com/ss/c/u001.abc/def", "https://link.mail.beehiiv.com/ss/c/u001.abc/def", "tracked"),
    ("https://twitter.com/tldr", "https://twitter.com/tldr", "social"),
    ("mailto:hi@x.com", "mailto:hi@x.com", "mailto"),
])
def test_canonical_url(raw, expected, kind):
    assert canonical_url(raw) == expected
    assert link_type(canonical_url(raw)) == kind


def test_hidden_preheader_pixels_and_styles_removed():
    blocks = html_to_blocks((FIX / "roundup_tldr.html").read_text(encoding="utf-8"))
    text = " ".join(b.text for b in blocks)
    assert "OpenAI ships agents, Nvidia beats" not in text      # display:none preheader
    assert "font-family" not in text                            # <style>
    assert "‌" not in text                                 # zero-width chars


def test_block_signals():
    blocks = html_to_blocks((FIX / "roundup_tldr.html").read_text(encoding="utf-8"))
    title = next(b for b in blocks if b.text.startswith("OpenAI launches"))
    assert title.link_full and title.bold_full and title.lead_link_frac == 1.0
    assert title.links[0].url == "https://openai.com/index/agents-sdk"
    heading = next(b for b in blocks if b.text == "Quick Links")
    assert heading.kind == "heading" and heading.level == 1


def test_boilerplate_and_footer_cut():
    blocks = clean((FIX / "roundup_tldr.html").read_text(encoding="utf-8"), None)
    texts = [b.text for b in blocks]
    assert not any(t.startswith("View Online") for t in texts)
    assert not any("unsubscribe" in t.lower() for t in texts)
    assert not any("Manage your preferences" in t for t in texts)
    assert texts[-1].startswith("If your company is interested")   # footer starts right after


def test_footer_marker_early_in_email_does_not_cut_content():
    html = "<p>Opt out of the noise: our five best reads.</p>" + "".join(
        f"<h2><a href='https://e.com/{i}'>Story {i}</a></h2><p>Body {i} text here.</p>" for i in range(6))
    blocks = clean(html, None)
    assert sum(b.text.startswith("Story") for b in blocks) == 6


def test_text_only_fallback():
    blocks = text_to_blocks("TOP STORIES\n\nNvidia beats estimates https://reuters.com/x?utm_source=a\n\nMore text here.")
    assert blocks[0].kind == "heading"
    assert blocks[1].links[0].url == "https://reuters.com/x"
    assert "https://" not in blocks[1].text


def test_markdown_render():
    md = to_markdown(clean((FIX / "sectioned_brew.html").read_text(encoding="utf-8"), None))
    assert "## The Fed holds steady, stocks don't" in md
    assert "<https://www.cnbc.com/2026/10/01/fed-decision.html>" in md
    assert "Unsubscribe" not in md


def test_story_mentioning_unsubscribe_is_not_cut_as_footer():
    long_story = ("Regulators said users must be able to unsubscribe from data sharing in one click, "
                  + "and companies have until March to comply with the new rule across all products. " * 4)
    html = "".join(f"<h2><a href='https://e.com/{i}'>Story {i}</a></h2><p>Body {i}.</p>" for i in range(5))
    html += f"<h2><a href='https://e.com/x'>Privacy rule</a></h2><p>{long_story}</p>"
    html += "<p>Users can now opt out of AI training.</p><p>Unsubscribe | Preferences</p>"
    texts = [b.text for b in clean(html, None)]
    assert any(t.startswith("Regulators said") for t in texts)
    assert "Users can now opt out of AI training." in texts
    assert not any(t.startswith("Unsubscribe") for t in texts)
