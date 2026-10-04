"""
Phase 2b — Subscribe all 45 newsletters using the KTN emails created in Phase 2a.
Updates data/sources.csv with status, status_detail, and date_subscribed.

Run: python scripts/phase2b_subscribe.py
     python scripts/phase2b_subscribe.py --segment 1-TechAI   # single segment
     python scripts/phase2b_subscribe.py --dry-run             # print plan only

Statuses written:
  subscribed_pending_confirmation — form submitted successfully
  needs_manual_signup             — CAPTCHA / paywall / unhandled block
"""

import argparse
import csv
import time
from datetime import date
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE_DIR = Path(__file__).parent.parent
SOURCES_CSV = BASE_DIR / "data" / "sources.csv"
FEEDS_CSV   = BASE_DIR / "data" / "category_feeds.csv"

TODAY = date.today().isoformat()  # 2026-07-24

SOURCES_FIELDS = [
    "segment", "newsletter_name", "publisher", "signup_url",
    "subscriber_estimate", "send_frequency", "double_optin", "description",
    "pick_type", "kt_email", "kt_feed_url", "status", "status_detail",
    "date_subscribed", "expected_next_check",
]


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def load_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_sources(rows):
    with open(SOURCES_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SOURCES_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def feeds_by_segment(feeds):
    return {r["segment"]: r for r in feeds}


# ---------------------------------------------------------------------------
# Generic signup logic
# ---------------------------------------------------------------------------

EMAIL_SELECTORS = [
    "input[type='email']",
    "input[name='email']",
    "input[placeholder*='email' i]",
    "input[id*='email' i]",
]

SUBMIT_SELECTORS = [
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('Subscribe')",
    "button:has-text('Sign up')",
    "button:has-text('Sign Up')",
    "button:has-text('Join')",
    "button:has-text('Get')",
    "button:has-text('Submit')",
]

CAPTCHA_SIGNALS = [
    "captcha", "recaptcha", "hcaptcha", "cf-turnstile",
    "cloudflare", "bot check", "verify you",
]

BLOCK_SIGNALS = [
    "paywall", "subscribe to read", "become a member",
    "paid subscriber", "upgrade to",
]


def page_has_signal(page, signals: list[str]) -> bool:
    content = page.content().lower()
    return any(s in content for s in signals)


def find_email_input(page):
    for sel in EMAIL_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.count() and el.is_visible(timeout=2000):
                return el
        except Exception:
            continue
    return None


def find_submit(page):
    for sel in SUBMIT_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.count() and el.is_visible(timeout=2000):
                return el
        except Exception:
            continue
    return None


def try_subscribe(page, signup_url: str, email: str) -> tuple[str, str]:
    """
    Returns (status, status_detail).
    status: 'subscribed_pending_confirmation' | 'needs_manual_signup'
    """
    try:
        page.goto(signup_url, wait_until="domcontentloaded", timeout=30000)
    except PWTimeout:
        return "needs_manual_signup", "page load timeout"
    except Exception as e:
        return "needs_manual_signup", f"navigation error: {e}"

    # Check for hard blockers before interacting
    if page_has_signal(page, BLOCK_SIGNALS):
        return "needs_manual_signup", "paywall or member-only gate detected"

    if page_has_signal(page, CAPTCHA_SIGNALS):
        return "needs_manual_signup", "CAPTCHA detected before form fill"

    email_input = find_email_input(page)
    if not email_input:
        return "needs_manual_signup", "no email input found on page"

    try:
        email_input.scroll_into_view_if_needed()
        email_input.fill(email)
        time.sleep(0.5)
    except Exception as e:
        return "needs_manual_signup", f"could not fill email: {e}"

    submit = find_submit(page)
    if not submit:
        return "needs_manual_signup", "no submit button found"

    try:
        submit.click()
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except PWTimeout:
        return "needs_manual_signup", "timeout after submit click"
    except Exception as e:
        return "needs_manual_signup", f"submit click error: {e}"

    # Post-submit CAPTCHA check
    if page_has_signal(page, CAPTCHA_SIGNALS):
        return "needs_manual_signup", "CAPTCHA appeared after submit"

    return "subscribed_pending_confirmation", ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment", help="Run only this segment (e.g. 1-TechAI)")
    parser.add_argument("--dry-run", action="store_true", help="Print plan, no browser")
    parser.add_argument("--headless", action="store_true", help="Run browser headless")
    args = parser.parse_args()

    sources = load_csv(SOURCES_CSV)
    feeds   = feeds_by_segment(load_csv(FEEDS_CSV))

    # Validate all segments have feeds
    missing = [s for s in set(r["segment"] for r in sources) if s not in feeds or not feeds[s].get("kt_email")]
    if missing:
        print(f"[ERROR] These segments have no KTN email yet — run phase2a first: {missing}")
        return

    targets = [
        r for r in sources
        if not r.get("status")  # skip already-done rows
        and (not args.segment or r["segment"] == args.segment)
    ]

    print(f"Subscribing {len(targets)} newsletters{' (dry-run)' if args.dry_run else ''}")

    if args.dry_run:
        for r in targets:
            seg = r["segment"]
            kt_email = feeds[seg]["kt_email"]
            print(f"  {r['newsletter_name']:35s}  →  {kt_email}  ({r['signup_url']})")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        for row in targets:
            seg     = row["segment"]
            name    = row["newsletter_name"]
            url     = row["signup_url"]
            kt_email = feeds[seg]["kt_email"]
            kt_feed  = feeds[seg]["kt_feed_url"]

            print(f"  [{seg}] {name} ...")

            status, detail = try_subscribe(page, url, kt_email)

            row["kt_email"]      = kt_email
            row["kt_feed_url"]   = kt_feed
            row["status"]        = status
            row["status_detail"] = detail
            row["date_subscribed"] = TODAY

            icon = "✓" if status == "subscribed_pending_confirmation" else "✗"
            print(f"    {icon} {status}" + (f" — {detail}" if detail else ""))

            save_sources(sources)   # save after each row so progress is never lost
            time.sleep(3)           # polite pause

        browser.close()

    print("\nDone — sources.csv updated.")
    print("Check status column; rows marked needs_manual_signup require manual action.")
    print("Next: run phase3_next_check.py to fill expected_next_check dates.")


if __name__ == "__main__":
    main()
