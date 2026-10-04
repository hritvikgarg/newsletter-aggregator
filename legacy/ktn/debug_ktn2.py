"""Capture DOM snapshot after clicking Create feed."""
import time
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()

    # Listen to all network requests/responses
    responses = []
    page.on("response", lambda r: responses.append((r.status, r.url)))

    page.goto("https://kill-the-newsletter.com/", wait_until="networkidle")

    page.locator("input[name='title']").fill("RIG Debug Test")

    print("Clicking submit...")
    page.locator("button[type='submit']").click()

    # Wait a few seconds for any AJAX to complete
    time.sleep(5)

    print("\n=== NETWORK RESPONSES AFTER CLICK ===")
    for status, url in responses[-10:]:
        print(f"  {status}  {url}")

    print("\n=== CURRENT URL ===")
    print(page.url)

    print("\n=== BODY TEXT (first 2000 chars) ===")
    print(page.locator("body").inner_text()[:2000])

    print("\n=== ALL LINKS ===")
    for a in page.locator("a").all():
        try:
            href = a.get_attribute("href") or ""
            txt = a.inner_text().strip()
            print(f"  {txt!r:40s}  {href}")
        except Exception:
            pass

    print("\n=== INPUT VALUE AFTER SUBMIT ===")
    print(repr(page.locator("input[name='title']").input_value()))

    input("Press Enter to close browser...")
    browser.close()
