"""
Phase 2a — Create one Kill-the-Newsletter feed per segment.
Updates data/category_feeds.csv with kt_email and kt_feed_url.

Run: python scripts/phase2a_create_feeds.py
"""

import csv
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE_DIR = Path(__file__).parent.parent
FEEDS_CSV = BASE_DIR / "data" / "category_feeds.csv"

SEGMENTS = [
    "1-TechAI",
    "2-BizFinance",
    "3-Legal",
    "4-HRPeopleOps",
    "5-GitHubRepos",
    "6-IndieHacker",
    "7-Absurdist",
    "8-MicroSmallCap",
    "9-MainstreamNews",
]

# Human-readable feed names submitted to KTN
FEED_NAMES = {
    "1-TechAI":         "RIG Tech AI",
    "2-BizFinance":     "RIG Biz Finance",
    "3-Legal":          "RIG Legal",
    "4-HRPeopleOps":    "RIG HR People Ops",
    "5-GitHubRepos":    "RIG GitHub Repos",
    "6-IndieHacker":    "RIG Indie Hacker",
    "7-Absurdist":      "RIG Absurdist",
    "8-MicroSmallCap":  "RIG Micro Small Cap",
    "9-MainstreamNews": "RIG Mainstream News",
}


def load_feeds():
    with open(FEEDS_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_feeds(rows):
    fieldnames = ["segment", "segment_label", "kt_email", "kt_feed_url"]
    with open(FEEDS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def create_feed(page, feed_name: str) -> dict:
    """Navigate to KTN, submit a feed name, return {email, feed_url}."""
    page.goto("https://kill-the-newsletter.com/", wait_until="networkidle", timeout=30000)

    # Fill in the feed name (field is named 'title')
    page.locator("input[name='title']").fill(feed_name)

    # Click Create — KTN uses JS so result appears in-page without navigation
    page.locator("button[type='submit']").click()

    # KTN renders email + feed URL into unnamed input[type=text] fields.
    # Wait until the email input value is populated.
    page.wait_for_function(
        "() => [...document.querySelectorAll('input[type=text]')]"
        ".some(i => i.value.includes('@kill-the-newsletter.com'))",
        timeout=20000,
    )

    # Grab all unnamed text inputs in DOM order: [email, feed_url, title, icon, ...]
    inputs = page.locator("input[type='text']:not([name])").all()
    email    = inputs[0].input_value().strip() if len(inputs) > 0 else None
    feed_url = inputs[1].input_value().strip() if len(inputs) > 1 else None

    return {"email": email, "feed_url": feed_url}


def main():
    rows = load_feeds()

    # Build a lookup so we can update by segment key
    row_map = {r["segment"]: r for r in rows}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # visible so you can watch/intervene
        context = browser.new_context()
        page = context.new_page()

        for seg in SEGMENTS:
            row = row_map.get(seg)
            if not row:
                print(f"  [SKIP] {seg} not found in CSV")
                continue

            if row.get("kt_email") and row.get("kt_feed_url"):
                print(f"  [SKIP] {seg} already has email+feed")
                continue

            feed_name = FEED_NAMES[seg]
            print(f"  [CREATE] {seg} -> '{feed_name}' ...")

            try:
                result = create_feed(page, feed_name)
                if result["email"] and result["feed_url"]:
                    row["kt_email"] = result["email"]
                    row["kt_feed_url"] = result["feed_url"]
                    print(f"    email:    {result['email']}")
                    print(f"    feed_url: {result['feed_url']}")
                else:
                    print(f"    [WARN] Could not extract email/feed_url — check browser window")
                    print(f"    page title: {page.title()}")
            except PWTimeout as e:
                print(f"    [ERROR] Timeout for {seg}: {e}")
            except Exception as e:
                print(f"    [ERROR] {seg}: {e}")

            time.sleep(2)  # polite pause between requests

        browser.close()

    save_feeds(list(row_map.values()))
    print("\nDone — category_feeds.csv updated.")
    print("Run phase2b_subscribe.py next.")


if __name__ == "__main__":
    main()
