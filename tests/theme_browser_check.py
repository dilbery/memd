"""Theme acceptance against the isolated user/admin HTTPS browser fixtures."""
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


out = Path(os.environ["MEMD_BROWSER_TEST_ROOT"])
with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(ignore_https_errors=True, color_scheme="dark", reduced_motion="reduce", viewport={"width": 1440, "height": 1050})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("https://localhost:8848/__test_login")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.emulate_media(color_scheme="light")
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    toggle = page.get_by_role("button", name="Dark mode", exact=True)
    toggle.focus()
    page.keyboard.press("Space")
    expect(toggle).to_have_attribute("aria-pressed", "true")
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    for path, name in [("/memories", "memories"), ("/memories/onboarding", "onboarding"), ("/", "welcome")]:
        page.goto("https://localhost:8848" + path)
        for theme in ["dark", "light"]:
            expect(page.locator("html")).to_have_attribute("data-theme", theme)
            if name == "memories":
                expect(page.locator(".memory-card")).to_have_count(25)
                page.locator(".memory-card").first.click()
            page.screenshot(path=str(out / f"theme-{name}-{theme}.png"), full_page=True)
            page.set_viewport_size({"width": 375, "height": 812})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            expect(toggle).to_be_in_viewport()
            page.screenshot(path=str(out / f"theme-{name}-{theme}-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1440, "height": 1050})
            toggle.click()
    other = context.new_page()
    other.goto("https://localhost:8848/")
    toggle.click()
    expect(other.locator("html")).to_have_attribute("data-theme", "light")
    other.close()
    page.goto("https://localhost:8847/__test_login")
    expect(page.locator("#count-active")).not_to_have_text("â€”")
    toggle.click()  # This origin starts with the page's emulated light preference.
    page.locator("#create").click()
    expect(page.locator("#editor")).to_be_visible()
    assert page.locator("#editor").evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(27, 38, 54)"
    page.screenshot(path=str(out / "theme-admin-dialog-dark.png"), full_page=True)
    page.keyboard.press("Escape")
    page.screenshot(path=str(out / "theme-admin-dark.png"), full_page=True)
    page.set_viewport_size({"width": 375, "height": 812})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    expect(toggle).to_be_in_viewport()
    # Restricted browser storage must not break the toggle or page scripts.
    restricted = browser.new_context(ignore_https_errors=True, color_scheme="light")
    restricted.add_init_script("Object.defineProperty(window, 'localStorage', {get() {throw new Error('disabled')}})")
    restricted_page = restricted.new_page()
    restricted_page.on("pageerror", lambda error: errors.append(str(error)))
    restricted_page.goto("https://localhost:8848/")
    restricted_page.get_by_role("button", name="Dark mode", exact=True).click()
    expect(restricted_page.locator("html")).to_have_attribute("data-theme", "dark")
    assert not errors, errors
    browser.close()
print("Theme browser acceptance passed: system preference, keyboard, persistence, tabs, all pages, mobile, dialogs and restricted storage.")
