"""Playwright acceptance against the isolated HTTPS synthetic fixture."""
import os
import uuid
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

out=Path(os.environ["MEMD_BROWSER_TEST_ROOT"])
job_label="Browser <test> " + uuid.uuid4().hex[:8]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    context=browser.new_context(ignore_https_errors=True, viewport={"width":1440,"height":1050})
    page=context.new_page(); errors=[]
    page.on("pageerror",lambda error: errors.append(str(error)))
    page.goto("https://localhost:8847/__test_login")
    expect(page.locator("#count-active")).not_to_have_text("—")
    total=len(page.request.get("https://localhost:8847/admin/api/tokens").json()["tokens"])
    expect(page.locator("#token-rows tr")).to_have_count(15)
    page.locator("#next").click(); expect(page.locator("#token-rows tr")).to_have_count(min(15,total-15))
    page.locator("#search").fill("Synthetic index job 01"); expect(page.locator("#token-rows tr")).to_have_count(1)
    page.locator("#search").fill("")
    page.locator("#create").click()
    expect(page.locator('#directory-owners option')).to_have_count(1)
    assert page.locator('#directory-owners option').get_attribute('value')=='alice@example.invalid'
    page.locator('[name="label"]').fill(job_label)
    page.locator('[name="owner"]').fill("Platform")
    page.locator('[name="purpose"]').fill("Synthetic browser lifecycle check")
    page.locator('[name="stores"]').fill("automation")
    page.locator("#submit").click()
    expect(page.locator("#secret-dialog")).to_be_visible()
    assert page.locator("#secret").input_value().startswith("memd_")
    page.locator("#secret-done").click()
    assert page.locator("#secret").input_value()==""
    page.locator("#search").fill(job_label)
    expect(page.locator("#token-rows tr")).to_have_count(1)
    page.get_by_role("button",name="Edit",exact=True).click()
    page.locator('[name="owner"]').fill("Platform operations")
    page.locator("#submit").click()
    expect(page.locator("#token-rows")).to_contain_text("Platform operations")
    page.get_by_role("button",name="Rotate",exact=True).click()
    page.locator('[name="overlap_hours"]').fill("0")
    page.locator("#submit").click()
    expect(page.locator("#secret-dialog")).to_be_visible(); page.locator("#secret-done").click()
    expect(page.locator("#token-rows tr")).to_have_count(2)
    page.get_by_role("button",name="Revoke",exact=True).click()
    page.locator('[name="confirmation"]').fill(job_label)
    page.locator("#submit").click()
    expect(page.locator("#token-rows")).to_contain_text("revoked")
    page.locator("#search").fill("")
    page.screenshot(path=str(out/"desktop.png"),full_page=True)
    page.get_by_role("link",name="Activity",exact=False).click()
    expect(page.locator("#activity-rows")).to_contain_text("revoke")
    page.get_by_role("link",name="Connection help",exact=False).click()
    expect(page.locator("#help")).to_be_visible()
    page.set_viewport_size({"width":390,"height":844})
    page.get_by_role("link",name="Access tokens",exact=False).click()
    page.screenshot(path=str(out/"mobile.png"),full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.locator("#logout").click(); expect(page).to_have_url("https://localhost:8847/")
    assert errors==[],errors
    context.close();browser.close()
print("Browser acceptance passed: pagination, search, create, edit, rotate, revoke, secret clearing, navigation, mobile overflow, logout; no JS errors.")
