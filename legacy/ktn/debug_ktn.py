"""Debug script to inspect KTN page structure after feed creation."""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()
    page.goto("https://kill-the-newsletter.com/", wait_until="networkidle")

    print("=== INITIAL PAGE TITLE ===")
    print(page.title())

    print("\n=== FORM HTML ===")
    form = page.locator("form")
    if form.count():
        print(form.first.inner_html())

    # Fill and submit
    inputs = page.locator("input").all()
    print(f"\n=== ALL INPUTS ({len(inputs)}) ===")
    for inp in inputs:
        try:
            print(f"  type={inp.get_attribute('type')} name={inp.get_attribute('name')} placeholder={inp.get_attribute('placeholder')}")
        except Exception:
            pass

    # Find text input and fill it
    text_input = page.locator("input[type='text'], input:not([type])").first
    text_input.fill("RIG Debug Test")

    buttons = page.locator("button, input[type='submit']").all()
    print(f"\n=== BUTTONS ({len(buttons)}) ===")
    for b in buttons:
        try:
            print(f"  tag={b.evaluate('el => el.tagName')} type={b.get_attribute('type')} text={b.inner_text()}")
        except Exception:
            pass

    # Submit
    page.locator("button[type='submit'], input[type='submit']").first.click()
    page.wait_for_load_state("networkidle", timeout=15000)

    print("\n=== POST-SUBMIT PAGE TITLE ===")
    print(page.title())

    print("\n=== POST-SUBMIT URL ===")
    print(page.url)

    print("\n=== POST-SUBMIT FULL BODY TEXT ===")
    print(page.locator("body").inner_text()[:3000])

    print("\n=== ALL LINKS ON PAGE ===")
    for a in page.locator("a").all():
        try:
            href = a.get_attribute("href")
            txt = a.inner_text().strip()
            if href:
                print(f"  {txt!r:40s} -> {href}")
        except Exception:
            pass

    browser.close()
