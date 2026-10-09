// Runs against tests/admin_preview.py: all accounts and notes are disposable.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const base = process.env.MEMD_PREVIEW_URL || 'http://127.0.0.1:8898';
const vault = process.env.MEMD_PREVIEW_VAULT;
const shots = process.env.MEMD_SCREENSHOT_DIR || '/tmp';
(async () => {
 const browser = await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_PATH || undefined});
 try {
  const page = await browser.newPage({viewport:{width:1440,height:1100}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.accept());
  await page.goto(base);
  await page.click('#login-open');await page.fill('#login-user','admin');await page.fill('#login-password','preview-password-only');await page.click('#login-submit');
  await page.waitForSelector('#tab-admin:not([hidden])');await page.click('#tab-admin');
  await page.waitForSelector('#tokens-table tr');
  await page.click('#user-add');await page.fill('#user-name','browser-member');await page.click('#user-form button');
  await page.waitForSelector('#secret-dialog[open]');const password=await page.textContent('#new-secret');assert.ok(password.length>12);
  await page.click('[data-close="secret-dialog"]');assert.equal(await page.textContent('#new-secret'),'');
  await page.click('#tab-settings');await page.click('#store-add');await page.fill('#store-id','browser-store');await page.fill('#store-name','Browser store');await page.click('#store-submit');
  await page.waitForSelector('#store-dialog', {state:'hidden'});
  await page.waitForFunction(()=>document.querySelector('#jobs-list').textContent.includes('complete'));
  // "Published notes need review" round-trips through PUT /admin/stores (publish_review, default on).
  const reviewOf=()=>page.evaluate(async()=>(await (await fetch('/admin/overview',{cache:'no-store'})).json()).stores.find(s=>s.id==='browser-store').publish_review);
  const configure=()=>page.locator('#stores-list article').filter({hasText:'Browser store'}).getByRole('button',{name:'Configure'}).click();
  const review=page.getByLabel('Published notes need review');
  await configure();assert.equal(await review.isChecked(),true);
  await page.fill('#store-name','Browser store <em>team</em>');await review.uncheck();await page.click('#store-submit');
  await page.waitForSelector('#store-dialog', {state:'hidden'});assert.equal(await reviewOf(),false);
  await configure();assert.equal(await review.isChecked(),false);
  await page.screenshot({path:shots+'/memd-store-settings.png'});
  await review.check();await page.click('#store-submit');await page.waitForSelector('#store-dialog', {state:'hidden'});assert.equal(await reviewOf(),true);
  await page.click('#tab-admin');
  const row=page.locator('#users-table tr').filter({hasText:'browser-member'});
  await row.getByRole('button',{name:'Store access',exact:true}).click();
  await page.selectOption('#grant-fields select[data-store="browser-store"]','write');await page.selectOption('#grant-fields select[data-store="amber"]','read');await page.click('#grants-form button');
  await page.waitForSelector('#grants-dialog', {state:'hidden'});
  await page.click('#token-add');await page.fill('#token-name','browser-token');
  const userOption=await page.locator('#token-user option').filter({hasText:'browser-member'}).getAttribute('value');
  // Additional stores list only the owner's other grants, offering write only where the owner has it.
  const extras=()=>page.locator('#token-extra select').evaluateAll(list=>list.map(s=>[s.dataset.store,[...s.options].map(o=>o.value)]));
  await page.selectOption('#token-user',userOption);await page.selectOption('#token-store','amber');
  assert.deepEqual(await extras(),[['browser-store',['','read','write']]]);
  assert.match(await page.textContent('#token-extra'),/Browser store <em>team<\/em>/);assert.equal(await page.locator('#token-extra em').count(),0);
  await page.selectOption('#token-store','browser-store');await page.selectOption('#token-scope','write');
  assert.deepEqual(await extras(),[['amber',['','read']]]);
  await page.locator('#token-extra').getByRole('combobox',{name:'amber',exact:true}).selectOption('read');
  await page.screenshot({path:shots+'/memd-token-issue.png'});
  await page.click('#token-form button');
  await page.waitForSelector('#secret-dialog[open]');const token=await page.textContent('#new-secret');
  assert.match(token,/^mem_browser-store_/);await page.click('[data-close="secret-dialog"]');
  assert.equal((await fetch(base+'/ui/notes',{headers:{Authorization:'Bearer '+token}})).status,200);
  assert.equal((await fetch(base+'/ui/notes?profile=amber',{headers:{Authorization:'Bearer '+token}})).status,200);
  assert.match(await page.locator('#tokens-table tr').filter({hasText:'browser-token'}).textContent(),/browser-store \+ amber \(read\)/);
  // An administrator owner holds every store, so every other store is offered with write.
  await page.click('#token-add');await page.selectOption('#token-store','amber');
  const adminOption=await page.locator('#token-user option').filter({hasText:/^admin$/}).getAttribute('value');await page.selectOption('#token-user',adminOption);
  assert.deepEqual(await extras(),[['browser-store',['','read','write']]]);await page.click('[data-close="token-dialog"]');
  await page.locator('#tokens-table tr').filter({hasText:'browser-token'}).getByRole('button',{name:'Revoke'}).click();
  await page.waitForFunction(()=>[...document.querySelectorAll('#tokens-table tr')].some(r=>r.textContent.includes('browser-token')&&r.textContent.includes('Revoked')));
  assert.equal((await fetch(base+'/ui/notes',{headers:{Authorization:'Bearer '+token}})).status,401);
  await page.screenshot({path:shots+'/memd-administration.png',fullPage:true});
  await page.click('#tab-settings');
  if(vault){
   await page.click('#store-add');await page.fill('#store-id','browser-vault');await page.fill('#store-name','Obsidian research');await page.selectOption('#store-kind','obsidian');await page.fill('#store-vault',vault);await page.click('#store-submit');
   await page.waitForSelector('#store-dialog', {state:'hidden'});
   await page.waitForFunction(()=>document.querySelector('#jobs-list').textContent.includes('browser-vault · sync · complete'));
   await page.selectOption('#store-select','browser-vault');await page.click('#tab-memory');
   await page.waitForSelector('.note-card');assert.match(await page.textContent('#results'),/Research/);
   await page.locator('.note-card').first().click();await page.waitForFunction(()=>document.querySelector('#reader-body').textContent.includes('[[links]]'));await page.click('#reader-close');
   await page.click('#tab-settings');
  }
  await page.screenshot({path:shots+'/memd-settings.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});await page.emulateMedia({colorScheme:'dark'});await page.click('#tab-admin');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.screenshot({path:shots+'/memd-admin-mobile.png',fullPage:true});
  await page.click('#token-add');await page.selectOption('#token-user',userOption);
  assert.equal(await page.evaluate(()=>{const d=document.querySelector('#token-dialog');return d.scrollWidth<=d.clientWidth;}),true);
  await page.screenshot({path:shots+'/memd-token-mobile.png'});await page.click('[data-close="token-dialog"]');
  await page.click('#tab-settings');await configure();
  assert.equal(await page.evaluate(()=>{const d=document.querySelector('#store-dialog');return d.scrollWidth<=d.clientWidth;}),true);
  await page.getByLabel('Published notes need review').scrollIntoViewIfNeeded();
  await page.screenshot({path:shots+'/memd-store-mobile.png'});await page.click('[data-close="store-dialog"]');await page.click('#tab-admin');
  await page.click('#logout');await page.waitForSelector('#login-open:not([hidden])');
  assert.equal(await page.locator('#tokens-table tr').count(),0);
  await page.click('#login-open');await page.fill('#login-user','browser-member');await page.fill('#login-password',password);await page.click('#login-submit');
  await page.waitForSelector('#logout:not([hidden])');assert.equal(await page.locator('#tab-admin').isVisible(),false);
  await page.click('#tab-settings');assert.match(await page.textContent('#stores-list'),/Browser store/);assert.equal(await page.locator('#store-add').isVisible(),false);
  await page.fill('#password-current',password);await page.fill('#password-new','updated-browser-password');await page.click('#password-form button');
  await page.waitForSelector('#login-open:not([hidden])');
  assert.deepEqual(errors,[]);
  console.log('PASS: actual account login, user creation, store provisioning, publish review setting, grants, token issue with additional stores/use/revoke, Obsidian import/read, settings/admin desktop/mobile, logout isolation, member restrictions, password change.');
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
