"""Find where KTN puts the email and feed URL after creation."""
import time
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("https://kill-the-newsletter.com/", wait_until="networkidle")
    page.locator("input[name='title']").fill("RIG Debug Test2")
    page.locator("button[type='submit']").click()
    time.sleep(4)

    print("=== ALL INPUT VALUES ===")
    for inp in page.locator("input").all():
        try:
            print(f"  name={inp.get_attribute('name')!r:20s} type={inp.get_attribute('type')!r:10s} value={inp.input_value()!r}")
        except Exception as e:
            print(f"  [err] {e}")

    print("\n=== CODE / PRE / SPAN ELEMENTS ===")
    for tag in ["code", "pre", "span", "p"]:
        els = page.locator(tag).all()
        for el in els:
            try:
                txt = el.inner_text().strip()
                if txt and len(txt) < 200:
                    print(f"  <{tag}>: {txt!r}")
            except Exception:
                pass

    browser.close()
