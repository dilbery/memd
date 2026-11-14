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
  await page.waitForSelector('#login-open:not([hidden])');assert.equal(await page.locator('#tab-inbox').isVisible(),false);
  await page.click('#login-open');await page.fill('#login-user','admin');await page.fill('#login-password','preview-password-only');await page.click('#login-submit');
  // An administrator reviews every store, so the Inbox tab is there from the start.
  await page.waitForSelector('#tab-admin:not([hidden])');assert.equal(await page.locator('#tab-inbox').isVisible(),true);await page.click('#tab-admin');
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
  // The Review choice round-trips through PUT /admin/stores as config.review_policy. The served store has one reviewer
  // so far (this administrator): "All saves need review by someone else" warns about that and saves all the same.
  const policyOf=store=>page.evaluate(async id=>{const s=(await (await fetch('/admin/overview',{cache:'no-store'})).json()).stores.find(x=>x.id===id);return [s.review_policy,s.reviewers];},store);
  const served=page.locator('#stores-list article').filter({has:page.locator('h2',{hasText:/^amber$/})});
  const policy=name=>page.getByRole('radio',{name,exact:true});
  const fourEyes=policy('All saves need review by someone else'),few=page.locator('#store-review-few'),unreviewed=page.locator('#store-review-unreviewed');
  assert.deepEqual(await policyOf('amber'),['open',1]);assert.equal(await served.locator('.chip').count(),0);
  await served.getByRole('button',{name:'Configure'}).click();
  assert.equal(await policy('No review').isChecked(),true);assert.equal(await few.isVisible(),false);
  await fourEyes.check();
  assert.equal(await few.isVisible(),true);assert.match(await few.textContent(),/^Only one account can review this store/);assert.equal(await unreviewed.isVisible(),false);
  await policy('Agent saves need review').check();assert.equal(await few.isVisible(),false);
  await fourEyes.check();await page.screenshot({path:shots+'/memd-store-review.png'});
  await page.click('#store-submit');await page.waitForSelector('#store-dialog', {state:'hidden'});
  assert.deepEqual(await policyOf('amber'),['four-eyes',1]);
  // The store's card names the policy, and the dialog opens on it with the warning already there.
  await served.locator('.chip').waitFor();assert.equal(await served.locator('.chip').textContent(),'All saves need review by someone else');
  await served.getByRole('button',{name:'Configure'}).click();
  assert.equal(await fourEyes.isChecked(),true);assert.equal(await few.isVisible(),true);
  await page.click('[data-close="store-dialog"]');
  // For a store kept in a Git repository or an Obsidian vault it also says that what arrives there is not reviewed.
  await page.click('#store-add');assert.equal(await policy('No review').isChecked(),true);await fourEyes.check();
  assert.equal(await few.isVisible(),false);assert.equal(await unreviewed.isVisible(),false);
  await page.selectOption('#store-kind','git');assert.equal(await unreviewed.isVisible(),true);
  await page.selectOption('#store-kind','obsidian');assert.equal(await unreviewed.isVisible(),true);
  await page.selectOption('#store-kind','local');assert.equal(await unreviewed.isVisible(),false);
  await page.click('[data-close="store-dialog"]');
  // Under that policy a signed-in session's save waits too: 202 with the queued receipt.
  const queued=await page.evaluate(async()=>{const me=await (await fetch('/ui/me',{cache:'no-store'})).json();
   const r=await fetch('/save',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':me.csrf},body:JSON.stringify({profile:'amber',title:'Rotation schedule',body:'Rotate the backup media on the first Monday.',tags:['backup']})});
   return {status:r.status,receipt:await r.json()};});
  assert.equal(queued.status,202);assert.equal(queued.receipt.queued,true);const own=queued.receipt.inbox_id;assert.match(own,/^[0-9a-f]{16}$/);
  // The Inbox says what the policy means, and keeps the text as proposed even for a reviewer who holds write.
  await page.click('#tab-inbox');await page.waitForSelector('#inbox-store-field:not([hidden])');await page.selectOption('#inbox-store','amber');
  await page.waitForSelector('#inbox-policy:not([hidden])');
  assert.match(await page.textContent('#inbox-policy'),/decided by someone other than the account that proposed it, and only its tags and importance can be changed/);
  await page.locator('#inbox-list .entity-link').filter({hasText:'Rotation schedule'}).click();await page.waitForSelector('#inbox-candidate:not([hidden])');
  assert.equal(await page.locator('#inbox-policy').isVisible(),true);
  for(const id of ['inbox-field-title','inbox-field-description','inbox-field-body'])assert.equal(await page.locator('#'+id).isEditable(),false,id);
  assert.equal(await page.getAttribute('#inbox-field-body','aria-describedby'),'inbox-text-policy');
  assert.equal(await page.locator('#inbox-text-policy').isVisible(),true);assert.equal(await page.locator('#inbox-text-help').isVisible(),false);
  assert.equal(await page.locator('#inbox-field-tags').isEditable(),true);
  await page.screenshot({path:shots+'/memd-inbox-four-eyes.png',fullPage:true});
  // This session's own account proposed it: approving is refused in the server's words, and the candidate stays open.
  await page.fill('#inbox-field-tags','backup, media');
  const selfApproval=page.waitForResponse(r=>r.url().endsWith('/inbox/'+own+'/approve'));
  await page.click('#inbox-approve');
  const refusedApproval=await selfApproval;assert.equal(refusedApproval.status(),403);
  await page.waitForSelector('#inbox-review-error:not([hidden])');
  assert.equal(await page.textContent('#inbox-review-error'),(await refusedApproval.json()).detail);
  assert.equal(new URL(page.url()).hash,'#inbox/amber/'+own);assert.equal(await page.locator('#inbox-candidate').isVisible(),true);
  assert.equal(await page.inputValue('#inbox-field-tags'),'backup, media');assert.equal(await page.locator('#inbox-approve').isEnabled(),true);
  assert.equal(await page.locator('#inbox-message').isVisible(),false);
  // So is rejecting it, inside the dialog that asked, which stays open.
  await page.click('#inbox-reject');await page.waitForSelector('#inbox-reject-dialog[open]');
  const selfRejection=page.waitForResponse(r=>r.url().endsWith('/inbox/'+own+'/reject'));
  await page.click('#inbox-reject-submit');
  const refusedRejection=await selfRejection;assert.equal(refusedRejection.status(),403);
  await page.waitForSelector('#inbox-reject-error:not([hidden])');
  assert.equal(await page.textContent('#inbox-reject-error'),(await refusedRejection.json()).detail);
  assert.equal(await page.locator('#inbox-reject-dialog[open]').count(),1);
  await page.click('[data-close="inbox-reject-dialog"]');
  assert.equal(new URL(page.url()).hash,'#inbox/amber/'+own);assert.equal(await page.locator('#inbox-candidate').isVisible(),true);
  // Back on "No review" the notice goes, the text is this reviewer's to change, and the same session may decide it.
  await page.click('#tab-settings');await served.getByRole('button',{name:'Configure'}).click();
  await policy('No review').check();await page.click('#store-submit');await page.waitForSelector('#store-dialog', {state:'hidden'});
  assert.deepEqual(await policyOf('amber'),['open',1]);await served.locator('.chip').waitFor({state:'detached'});
  await page.click('#tab-inbox');await page.waitForSelector('#inbox-list .entity-row');assert.equal(await page.locator('#inbox-policy').isVisible(),false);
  await page.locator('#inbox-list .entity-link').filter({hasText:'Rotation schedule'}).click();await page.waitForSelector('#inbox-candidate:not([hidden])');
  assert.equal(await page.locator('#inbox-field-body').isEditable(),true);assert.equal(await page.locator('#inbox-text-policy').isVisible(),false);
  await page.click('#inbox-reject');await page.waitForSelector('#inbox-reject-dialog[open]');await page.fill('#inbox-reject-reason','Only there for the review check');
  await page.click('#inbox-reject-submit');await page.waitForSelector('#inbox-reject-dialog', {state:'hidden'});
  await page.waitForSelector('#inbox-list .empty');
  await page.click('#tab-admin');
  const row=page.locator('#users-table tr').filter({hasText:'browser-member'});
  await row.getByRole('button',{name:'Store access',exact:true}).click();
  // "Can review" needs access, and a new access choice starts it from what that access used to mean.
  const canReview=store=>page.locator(`#grant-fields input[data-review="${store}"]`);
  const grantsOf=()=>page.evaluate(async()=>{const o=await (await fetch('/admin/overview',{cache:'no-store'})).json();const u=o.users.find(x=>x.username==='browser-member');
   return {reviews:u.reviews,grants:o.grants.filter(g=>g.user_id===u.id).map(g=>[g.store_id,g.permission,g.review]).sort()};});
  assert.equal(await canReview('browser-store').isDisabled(),true);assert.equal(await canReview('amber').isDisabled(),true);
  await page.selectOption('#grant-fields select[data-store="browser-store"]','write');await page.selectOption('#grant-fields select[data-store="amber"]','read');
  assert.equal(await canReview('browser-store').isChecked(),true);assert.equal(await canReview('amber').isEnabled(),true);assert.equal(await canReview('amber').isChecked(),false);
  await page.click('#grants-form button');
  await page.waitForSelector('#grants-dialog', {state:'hidden'});
  await page.waitForFunction(()=>document.querySelector('#users-table').textContent.includes('browser-store (write, review)'));
  assert.match(await row.textContent(),/amber \(read\)/);
  assert.deepEqual(await grantsOf(),{reviews:['browser-store'],grants:[['amber','read',false],['browser-store','write',true]]});
  await page.click('#token-add');await page.fill('#token-name','browser-token');
  const userOption=await page.locator('#token-user option').filter({hasText:'browser-member'}).getAttribute('value');
  // Additional stores list only the owner's other grants, offering write only where the owner has it.
  const extras=()=>page.locator('#token-extra select').evaluateAll(list=>list.map(s=>[s.dataset.store,[...s.options].map(o=>o.value)]));
  await page.selectOption('#token-user',userOption);await page.selectOption('#token-store','amber');
  assert.deepEqual(await extras(),[['browser-store',['','read','write']]]);
  assert.match(await page.textContent('#token-extra'),/Browser store <em>team<\/em>/);assert.equal(await page.locator('#token-extra em').count(),0);
  // A token may review only where its owner does: not on amber alone, but once browser-store is one of its stores.
  const mayReview=page.getByLabel('May review inbox candidates');
  assert.equal(await mayReview.isDisabled(),true);
  await page.locator('#token-extra').getByRole('combobox',{name:/Browser store/}).selectOption('read');assert.equal(await mayReview.isEnabled(),true);
  await page.selectOption('#token-store','browser-store');await page.selectOption('#token-scope','write');
  assert.deepEqual(await extras(),[['amber',['','read']]]);
  await page.locator('#token-extra').getByRole('combobox',{name:'amber',exact:true}).selectOption('read');
  assert.equal(await mayReview.isChecked(),false);await mayReview.check();
  await page.screenshot({path:shots+'/memd-token-issue.png'});
  await page.click('#token-form button');
  await page.waitForSelector('#secret-dialog[open]');const token=await page.textContent('#new-secret');
  assert.match(token,/^mem_browser-store_/);await page.click('[data-close="secret-dialog"]');
  assert.equal((await fetch(base+'/ui/notes',{headers:{Authorization:'Bearer '+token}})).status,200);
  assert.equal((await fetch(base+'/ui/notes?profile=amber',{headers:{Authorization:'Bearer '+token}})).status,200);
  assert.match(await page.locator('#tokens-table tr').filter({hasText:'browser-token'}).textContent(),/browser-store \+ amber \(read\)/);
  assert.match(await page.locator('#tokens-table tr').filter({hasText:'browser-token'}).textContent(),/Read \+ write, review/);
  assert.equal(await page.evaluate(async()=>(await (await fetch('/admin/overview',{cache:'no-store'})).json()).tokens.find(t=>t.label==='browser-token').review),true);
  // Three candidates for the store's inbox, proposed with the agent token.
  const propose=async fact=>{const r=await fetch(base+'/propose',{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify(fact)});
   assert.equal(r.status,200);return (await r.json()).id;};
  const keep=await propose({title:'Restore drill <b>steps</b>',body:'Restore the newest verified backup, then compare checksums.',tags:['backup']});
  const drop=await propose({title:'Scratch observation',body:'A passing remark that should not be kept.'});
  const stale=await propose({title:'Decided elsewhere',body:'Another reviewer decides this one first.'});
  // The administrator holds write on every store: a store picker, and the candidate's text is editable.
  await page.click('#tab-inbox');await page.waitForSelector('#inbox-store-field:not([hidden])');
  assert.equal(await page.title(),'memd · Inbox');
  assert.deepEqual((await page.locator('#inbox-store option').evaluateAll(list=>list.map(o=>o.value))).sort(),['amber','browser-store']);
  await page.selectOption('#inbox-store','browser-store');
  await page.waitForFunction(()=>document.querySelectorAll('#inbox-list .entity-row').length===3);
  await page.locator('#inbox-list .entity-link').filter({hasText:'Restore drill'}).click();
  await page.waitForSelector('#inbox-candidate:not([hidden])');
  assert.equal(await page.locator('#inbox-field-body').isEditable(),true);assert.equal(await page.locator('#inbox-text-help').isVisible(),false);
  await page.screenshot({path:shots+'/memd-inbox-candidate.png',fullPage:true});
  await page.click('#inbox-view .back-link');await page.waitForSelector('#inbox-list-view:not([hidden])');
  await page.click('#tab-admin');
  // An administrator owner holds every store, so every other store is offered with write.
  await page.click('#token-add');await page.selectOption('#token-store','amber');
  const adminOption=await page.locator('#token-user option').filter({hasText:/^admin$/}).getAttribute('value');await page.selectOption('#token-user',adminOption);
  assert.deepEqual(await extras(),[['browser-store',['','read','write']]]);await page.click('[data-close="token-dialog"]');
  await page.locator('#tokens-table tr').filter({hasText:'browser-token'}).getByRole('button',{name:'Revoke'}).click();
  await page.waitForFunction(()=>[...document.querySelectorAll('#tokens-table tr')].some(r=>r.textContent.includes('browser-token')&&r.textContent.includes('Revoked')));
  assert.equal((await fetch(base+'/ui/notes',{headers:{Authorization:'Bearer '+token}})).status,401);
  // Review without write: read access with "Can review" set by hand.
  await row.getByRole('button',{name:'Store access',exact:true}).click();
  assert.equal(await canReview('browser-store').isChecked(),true);assert.equal(await canReview('amber').isChecked(),false);
  await page.selectOption('#grant-fields select[data-store="browser-store"]','read');
  assert.equal(await canReview('browser-store').isChecked(),false);
  await page.getByRole('checkbox',{name:/^Can review Browser store/}).check();
  await page.screenshot({path:shots+'/memd-grants-review.png'});
  await page.click('#grants-form button');await page.waitForSelector('#grants-dialog', {state:'hidden'});
  await page.waitForFunction(()=>document.querySelector('#users-table').textContent.includes('browser-store (read, review)'));
  assert.deepEqual(await grantsOf(),{reviews:['browser-store'],grants:[['amber','read',false],['browser-store','read',true]]});
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
   // What arrives through the vault is not reviewed, and the Review choice says so for this store.
   await page.locator('#stores-list article').filter({hasText:'Obsidian research'}).getByRole('button',{name:'Configure'}).click();
   await fourEyes.check();assert.equal(await unreviewed.isVisible(),true);await page.click('[data-close="store-dialog"]');
  }
  await page.screenshot({path:shots+'/memd-settings.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});await page.emulateMedia({colorScheme:'dark'});await page.click('#tab-admin');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.screenshot({path:shots+'/memd-admin-mobile.png',fullPage:true});
  await page.click('#token-add');await page.selectOption('#token-user',userOption);
  assert.equal(await page.evaluate(()=>{const d=document.querySelector('#token-dialog');return d.scrollWidth<=d.clientWidth;}),true);
  await page.screenshot({path:shots+'/memd-token-mobile.png'});await page.click('[data-close="token-dialog"]');
  await row.getByRole('button',{name:'Store access',exact:true}).click();
  assert.equal(await page.evaluate(()=>{const d=document.querySelector('#grants-dialog');return d.scrollWidth<=d.clientWidth;}),true);
  await page.screenshot({path:shots+'/memd-grants-mobile.png'});await page.click('[data-close="grants-dialog"]');
  await page.click('#tab-settings');await configure();
  assert.equal(await page.evaluate(()=>{const d=document.querySelector('#store-dialog');return d.scrollWidth<=d.clientWidth;}),true);
  await page.getByLabel('Published notes need review').scrollIntoViewIfNeeded();
  await page.screenshot({path:shots+'/memd-store-mobile.png'});await page.click('[data-close="store-dialog"]');await page.click('#tab-admin');
  await page.click('#logout');await page.waitForSelector('#login-open:not([hidden])');
  assert.equal(await page.locator('#tokens-table tr').count(),0);assert.equal(await page.locator('#tab-inbox').isVisible(),false);
  await page.click('#login-open');await page.fill('#login-user','browser-member');await page.fill('#login-password',password);await page.click('#login-submit');
  await page.waitForSelector('#logout:not([hidden])');assert.equal(await page.locator('#tab-admin').isVisible(),false);
  await page.click('#tab-settings');assert.match(await page.textContent('#stores-list'),/Browser store/);assert.equal(await page.locator('#store-add').isVisible(),false);
  assert.match(await page.textContent('#stores-list'),/Read-only access, can review the inbox/);
  // The member reviews browser-store without write access to it: one review store, so no picker.
  await page.setViewportSize({width:1440,height:1100});await page.emulateMedia({colorScheme:'light'});
  await page.click('#tab-inbox');await page.waitForSelector('#inbox-list .entity-row');
  assert.equal(await page.title(),'memd · Inbox');assert.equal(await page.locator('#inbox-store-field').isVisible(),false);
  assert.equal(await page.locator('#inbox-list .entity-row').count(),3);
  assert.match(await page.textContent('#inbox-status'),/Waiting for review \(3\).*Approved \(0\).*Rejected \(0\)/s);
  assert.match(await page.textContent('#inbox-list'),/Restore drill <b>steps<\/b>/);assert.equal(await page.locator('#inbox-panel b').count(),0);
  assert.match(await page.textContent('#inbox-list'),/browser-token · agent · proposed /);
  await page.screenshot({path:shots+'/memd-inbox.png',fullPage:true});
  // Approve one with a changed tag. The candidate opens from the keyboard; its text is not editable.
  await page.locator('#inbox-list .entity-link').filter({hasText:'Restore drill'}).focus();await page.keyboard.press('Enter');
  await page.waitForSelector('#inbox-candidate:not([hidden])');
  assert.equal(new URL(page.url()).hash,'#inbox/browser-store/'+keep);
  assert.equal(await page.evaluate(()=>document.activeElement.id),'inbox-candidate-title');
  assert.equal(await page.textContent('#inbox-candidate-title'),'Restore drill <b>steps</b>');
  for(const id of ['inbox-field-title','inbox-field-description','inbox-field-body'])assert.equal(await page.locator('#'+id).isEditable(),false,id);
  assert.equal(await page.locator('#inbox-field-tags').isEditable(),true);assert.equal(await page.locator('#inbox-field-importance').isEnabled(),true);
  assert.equal(await page.locator('#inbox-text-help').isVisible(),true);
  assert.equal(await page.inputValue('#inbox-field-body'),'Restore the newest verified backup, then compare checksums.');
  assert.equal(await page.inputValue('#inbox-field-tags'),'backup');
  assert.match(await page.textContent('#inbox-details'),/Proposed bybrowser-tokenChannelagent/);
  await page.fill('#inbox-field-tags','backup, drill');
  const approval=page.waitForRequest(r=>r.url().endsWith('/inbox/'+keep+'/approve'));
  await page.click('#inbox-approve');
  assert.deepEqual((await approval).postDataJSON(),{profile:'browser-store',edits:{tags:['backup','drill']}});
  await page.waitForFunction(()=>document.querySelectorAll('#inbox-list .entity-row').length===2);
  assert.equal(new URL(page.url()).hash,'#inbox');
  assert.match(await page.textContent('#inbox-message'),/^Approved “Restore drill <b>steps<\/b>” and saved it as \S+ Your changes were applied\./);
  // Reject another with a reason.
  await page.locator('#inbox-list .entity-link').filter({hasText:'Scratch observation'}).click();
  await page.waitForSelector('#inbox-candidate:not([hidden])');
  await page.click('#inbox-reject');await page.waitForSelector('#inbox-reject-dialog[open]');
  assert.equal(await page.evaluate(()=>document.activeElement.id),'inbox-reject-reason');
  assert.equal(await page.textContent('#inbox-reject-name'),'Scratch observation');
  await page.fill('#inbox-reject-reason','Not durable enough to keep');
  const rejection=page.waitForRequest(r=>r.url().endsWith('/inbox/'+drop+'/reject'));
  await page.click('#inbox-reject-submit');
  assert.deepEqual((await rejection).postDataJSON(),{profile:'browser-store',reason:'Not durable enough to keep'});
  await page.waitForSelector('#inbox-reject-dialog', {state:'hidden'});
  await page.waitForFunction(()=>document.querySelectorAll('#inbox-list .entity-row').length===1);
  assert.equal(await page.textContent('#inbox-message'),'Rejected “Scratch observation”. Nothing was saved.');
  // A refusal is shown in the server's own words: review without write cannot change the text, even past the page.
  await page.click('#inbox-list .entity-link');await page.waitForSelector('#inbox-candidate:not([hidden])');
  await page.evaluate(()=>{const title=document.querySelector('#inbox-field-title');title.readOnly=false;title.value='Rewritten by a reviewer';});
  const refusal=page.waitForResponse(r=>r.url().endsWith('/inbox/'+stale+'/approve'));
  await page.click('#inbox-approve');
  const refused=await refusal;assert.equal(refused.status(),403);
  await page.waitForSelector('#inbox-review-error:not([hidden])');
  assert.equal(await page.textContent('#inbox-review-error'),(await refused.json()).detail);
  // So is a candidate that was decided elsewhere meanwhile (409).
  await page.click('#inbox-refresh');
  await page.waitForFunction(()=>document.querySelector('#inbox-review-error').hidden&&document.querySelector('#inbox-field-title').value==='Decided elsewhere'&&document.querySelector('#inbox-field-title').readOnly);
  await page.evaluate(async id=>{const me=await (await fetch('/ui/me',{cache:'no-store'})).json();
   const r=await fetch('/inbox/'+id+'/reject',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':me.csrf},body:JSON.stringify({profile:'browser-store',reason:'Decided in another tab'})});
   if(!r.ok)throw new Error('reject elsewhere failed: '+r.status);},stale);
  const conflict=page.waitForResponse(r=>r.url().endsWith('/inbox/'+stale+'/approve'));
  await page.click('#inbox-approve');
  const conflicted=await conflict;assert.equal(conflicted.status(),409);
  await page.waitForSelector('#inbox-review-error:not([hidden])');
  assert.equal(await page.textContent('#inbox-review-error'),(await conflicted.json()).detail);
  await page.click('#inbox-view .back-link');await page.waitForSelector('#inbox-list .empty');
  assert.match(await page.textContent('#inbox-status'),/Waiting for review \(0\).*Approved \(1\).*Rejected \(2\)/s);
  // Decided candidates show who decided, when and why; the approved note opens from the inbox's own store.
  await page.selectOption('#inbox-status','approved');
  await page.waitForFunction(()=>document.querySelector('#inbox-list .entity-row')?.textContent.includes('by browser-member'));
  await page.click('#inbox-list .entity-link');await page.waitForSelector('#inbox-decided:not([hidden])');
  assert.match(await page.textContent('#inbox-decided'),/^Approved by browser-member on .+, saved as \S+ It was edited before approval\.$/);
  assert.equal(await page.locator('#inbox-actions').isVisible(),false);assert.equal(await page.locator('#inbox-field-tags').isEditable(),false);
  assert.equal(await page.inputValue('#inbox-field-tags'),'backup, drill');
  await page.click('#inbox-details .note-link');
  await page.waitForFunction(()=>document.querySelector('#reader-body').textContent.includes('compare checksums'));await page.click('#reader-close');
  await page.click('#inbox-view .back-link');await page.selectOption('#inbox-status','rejected');
  await page.waitForFunction(()=>document.querySelectorAll('#inbox-list .entity-row').length===2);
  await page.locator('#inbox-list .entity-link').filter({hasText:'Scratch observation'}).click();
  await page.waitForSelector('#inbox-decided:not([hidden])');
  assert.match(await page.textContent('#inbox-decided'),/^Rejected by browser-member on .+\. Reason: Not durable enough to keep$/);
  await page.setViewportSize({width:390,height:844});await page.emulateMedia({colorScheme:'dark'});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.screenshot({path:shots+'/memd-inbox-candidate-mobile.png',fullPage:true});
  await page.click('#inbox-view .back-link');await page.waitForSelector('#inbox-list .entity-row');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.screenshot({path:shots+'/memd-inbox-mobile.png',fullPage:true});
  await page.click('#tab-settings');
  await page.fill('#password-current',password);await page.fill('#password-new','updated-browser-password');await page.click('#password-form button');
  await page.waitForSelector('#login-open:not([hidden])');
  assert.deepEqual(errors,[]);
  console.log('PASS: actual account login, user creation, store provisioning, publish review setting, review policy choice with its warnings, a four-eyes store in the inbox (notice, text kept as proposed, own proposal refused and left open), grants with review, token issue with additional stores and review/use/revoke, Obsidian import/read, settings/admin desktop/mobile, logout isolation, member restrictions, inbox review without write (approve with a changed tag, reject with a reason, refusals as the server words them), password change.');
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
