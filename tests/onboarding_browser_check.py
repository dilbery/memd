"""Synthetic HTTPS user onboarding lifecycle; no real credentials or accounts."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect

origin='https://localhost:8848'
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    ctx=browser.new_context(ignore_https_errors=True,viewport={'width':1440,'height':1050})
    page=ctx.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(origin+'/__test_login');page.get_by_role('link',name='Onboarding').click()
    expect(page.locator('#no-tokens')).to_be_visible()
    page.locator('[name="label"]').fill('Laptop <test>')
    page.locator('#create').click();expect(page.locator('#secret-dialog')).to_be_visible()
    secret=page.locator('#secret').input_value();assert secret.startswith('memd_')
    page.locator('#test-token').click();expect(page.locator('#test-result')).to_have_text('Connected to your personal memory store.')
    page.locator('#secret-done').click();assert page.locator('#secret').input_value()==''
    expect(page.locator('#token-list')).to_contain_text('Laptop <test>')
    assert secret not in page.locator('#instructions').inner_text()
    for shell in ('bash','powershell'):
        page.locator('#shell').select_option(shell)
        for client,text in [('claude','claude mcp add-json'),('codex','--bearer-token-env-var MEMD_TOKEN'),('omp','~/.omp/agent/mcp.json'),('pi','pi -e ./memd-personal.ts'),('other','Authorization: Bearer <your token>')]:
            page.locator('#client').select_option(client)
            expect(page.locator('#instructions')).to_contain_text(text)
            assert secret not in page.locator('#instructions').inner_text()
    page.locator('#client').select_option('codex');page.locator('#shell').select_option('bash')
    page.screenshot(path=str(Path(os.environ['MEMD_BROWSER_TEST_ROOT'])/'onboarding-desktop.png'),full_page=True)
    # The other user cannot list this token, even with the same browser.
    page.goto(origin+'/__test_login?user=bob@example.invalid');page.goto(origin+'/memories/onboarding')
    expect(page.locator('#no-tokens')).to_be_visible()
    page.goto(origin+'/__test_login');page.goto(origin+'/memories/onboarding')
    expect(page.locator('#token-list')).to_contain_text('Laptop <test>')
    page.once('dialog',lambda d:d.accept());page.get_by_role('button',name='Revoke',exact=True).click()
    expect(page.locator('#token-list')).to_contain_text('revoked')
    response=page.request.post(origin+'/recall',headers={'Authorization':'Bearer '+secret},data={})
    assert response.status==401
    page.set_viewport_size({'width':390,'height':844})
    page.screenshot(path=str(Path(os.environ['MEMD_BROWSER_TEST_ROOT'])/'onboarding-mobile.png'),full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert errors==[],errors
    ctx.close();browser.close()
print('PASS: onboarding navigation, issue/show-once/connection test, all setup guides, cross-user list isolation, revoke, mobile and no JS errors.')
