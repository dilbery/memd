"""Browser acceptance against the isolated synthetic HTTPS fixture."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

out=Path(os.environ["MEMD_BROWSER_TEST_ROOT"])
origin="https://localhost:8848"
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    context=browser.new_context(ignore_https_errors=True, viewport={"width":1440,"height":1050})
    page=context.new_page(); errors=[]
    page.on("pageerror",lambda error: errors.append(str(error)))
    page.goto(origin+"/__test_login")
    expect(page.locator(".memory-card")).to_have_count(25)
    page.locator("#next").click(); expect(page.locator(".memory-card")).to_have_count(3)
    page.locator("#search").fill("Project context 00"); expect(page.locator(".memory-card")).to_have_count(1)
    page.locator(".memory-card").click()
    expect(page.locator("#note-body")).to_contain_text("alice@example.invalid")
    assert page.locator("#note-body img").count()==0
    assert "bob@example.invalid" not in page.content()
    page.locator("#search").fill(""); expect(page.locator(".memory-card")).to_have_count(25)
    page.screenshot(path=str(out/"user-desktop.png"),full_page=True)
    page.locator("#edit").click()
    page.locator('[name="title"]').fill("Corrected project context")
    page.locator('[name="body"]').fill("Corrected owner context <script>alert(1)</script>")
    page.locator("#save").click()
    expect(page.locator("#note-title")).to_have_text("Corrected project context")
    expect(page.locator("#note-body")).to_have_text("Corrected owner context <script>alert(1)</script>")
    # A concurrent change refuses the stale form and retains the user's draft.
    page.locator("#edit").click(); page.locator('[name="body"]').fill("Draft to preserve")
    item=page.request.get(origin+"/memories/api/note?slug=memory-00").json()
    csrf=page.locator('meta[name="csrf-token"]').get_attribute("content")
    r=page.request.post(origin+"/memories/api/edit",headers={"Origin":origin,"X-CSRF-Token":csrf},data={"slug":item["slug"],"revision":item["revision"],"title":"Concurrent correction","body":"Other tab"})
    assert r.status==200
    page.locator("#save").click(); expect(page.locator("#edit-error")).to_contain_text("Reload")
    expect(page.locator('[name="body"]')).to_have_value("Draft to preserve")
    page.once("dialog",lambda d:d.accept()); page.get_by_role("button",name="Reload memory").click()
    expect(page.locator("#note-title")).to_have_text("Concurrent correction")
    page.locator("#retract").click(); page.locator('[name="confirmation"]').fill("RETRACT")
    page.locator("#confirm-retract").click(); expect(page.locator("#note-status")).to_have_text("retracted")
    page.locator("#status").select_option("retracted"); expect(page.locator(".memory-card")).to_have_count(1)
    expect(page.locator("#edit")).to_be_hidden()
    page.set_viewport_size({"width":390,"height":844})
    page.screenshot(path=str(out/"user-mobile-reader.png"),full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.locator("#back").click(); page.locator("#status").select_option("active")
    expect(page.locator(".memory-card")).to_have_count(25)
    page.screenshot(path=str(out/"user-mobile-list.png"),full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.locator("#help").click(); expect(page.locator("#help-dialog")).to_be_visible()
    page.get_by_role("button",name="Got it").click()
    page.locator("#logout").click(); expect(page).to_have_url(origin+"/")
    assert page.request.get(origin+"/memories/api/list").status==401
    page.goto(origin+"/__test_login?user=bob@example.invalid")
    assert page.request.get(origin+"/memories/api/note?slug=memory-00").json()["title"]=="Project context 00"
    assert errors==[],errors
    context.close();browser.close()
print("PASS: browser search, pagination, own-store isolation, plain-text rendering, edit, stale conflict/draft retention, retract, mobile, help and logout.")
