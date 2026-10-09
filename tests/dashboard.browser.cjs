// Run with node tests/dashboard.browser.cjs; needs Playwright and Chromium.
// All service responses are synthetic. No production notes or credentials.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const { createServer } = require('node:http');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../memd/web');
const shots = process.env.SCREENSHOT_DIR || '/tmp';
const stats = {ok:true,profile:'test',notes:55,vec:52,fts:55,core:9,superseded:3,pending_vectors:0,size:1048576,head:'abcd1234',hosts:[{host:'node-a',count:40},{host:'any',count:15}],importance:[{importance:3,count:46},{importance:5,count:9}]};
const ref = (slug,title) => ({slug,title,host:'node-a',importance:3});
const evil = '<img src=x onerror=alert(1)>';
const unavailable = reason => ({available:false,reason,total:0,items:[]});
const insights = {ok:true,profile:'test',generated_at:'2026-01-02T03:04:05Z',limit:20,cached:false,age_s:0,
  usage:{available:true,reason:null,recalls:40,reads:12,since:'2025-12-03T00:00:00Z',window_days:30,truncated:false},
  never_recalled:{available:true,reason:null,total:25,eligible:50,basis:'git',items:[ref('note-0',evil),ref('note-1','Memory 1')]},
  recalled_unread:{available:true,reason:null,total:1,min_shown:3,items:[{...ref('note-2','Memory 2'),shown:7}]},
  most_useful:{available:true,reason:null,total:1,items:[{...ref('note-3','Memory 3'),reads:9,shown:12,read_rate:0.75}]},
  stale:{available:true,reason:null,total:1,items:[{...ref('note-4','Memory 4'),volatility:'state',as_of:'2025-09-01',age_days:123,reason:'state, older than 30 days'}]},
  heatmap:{available:true,reason:null,buckets:['0-7d','8-30d','31-90d','91-365d','>1y','undated'],
    tags:{total_rows:14,rows:[{key:evil,notes:9,stale:2,cells:[0,1,2,3,1,2]},{key:'backup',notes:4,stale:0,cells:[4,0,0,0,0,0]}]},
    hosts:{total_rows:1,rows:[{key:'node-a',notes:13,stale:2,cells:[4,1,2,3,1,2]}]}},
  failed_verifications:unavailable('No note declares verify probes (mem-verify checks those).'),
  supersede_candidates:unavailable('No facts extracted yet; run mem-facts to derive them.'),
  contradictions:{available:true,reason:null,total:1,items:[{subject:'mirror',predicate:'points to',notes:2,more_values:0,values:[{object:evil,notes:[ref('note-5','Memory 5')]},{object:'10.10.1.2',notes:[ref('note-6','Memory 6')]}]}]},
  stale_summaries:{...unavailable('No summary notes in this store (mem-summarize proposes them).'),summaries:0},
  inbox:{available:true,reason:null,pending:3,oldest:null},
  forget:{available:true,reason:null,archived:4,pending:2,branch:'memd/forget'},
  coverage:{available:true,reason:null,notes:52,superseded:3,pending_vectors:2,pending_chunks:3,pending:3,vector_share:0.96,chunk_share:0.94}};
const entityList = {ok:true,profile:'test',generated_at:'2026-01-02T03:04:05Z',total:4,limit:150,truncated:false,notes:52,entities:[
  {kind:'host',name:'node-a',display:'node-a',notes:12,current_facts:3,stale:2,failed_verification:1,last_activity:'2026-01-01'},
  {kind:'service',name:'web proxy',display:'Web proxy',notes:4,current_facts:1,stale:0,failed_verification:0,last_activity:null},
  {kind:'tag',name:'backup',display:'backup',notes:3,current_facts:0,stale:0,failed_verification:0,last_activity:'2025-12-01'},
  {kind:'service',name:'evil',display:evil,notes:1,current_facts:0,stale:0,failed_verification:0,last_activity:null}]};
const entityPage = {ok:true,profile:'test',generated_at:'2026-01-02T03:04:05Z',cached:false,
  entity:{kind:'host',name:'node-a',display:'node-a',notes:12,current_facts:3,stale:2,failed_verification:1,last_activity:'2026-01-01',reasons:{scoped:8,fact:3,tag:0,mention:4}},
  current_facts:{total:1,items:[{subject:'web proxy',predicate:'runs on',object:'node-a',role:'object',source:'note-3',source_title:'Memory 3',method:'pattern',since:'2026-01-01'}]},
  timeline:{total:2,items:[{subject:'web proxy',predicate:'runs on',object:'node-a',role:'object',valid_from:'2026-01-01',valid_to:null,current:true,source:'note-3',source_title:'Memory 3',closed_by:null,note_changed:false},
    {subject:'web proxy',predicate:'runs on',object:evil,role:'object',valid_from:'2025-06-01',valid_to:'2026-01-01',current:false,source:'note-4',source_title:evil,closed_by:'note-3',note_changed:false}]},
  notes:{total:60,items:[{slug:'note-4',title:evil,host:'node-a',date:'2025-09-01',stale:true,stale_reason:'state, older than 30 days',verification:{status:'failed',checked_at:'2026-01-01',probe:'tcp node-a:53: refused'},why:['scoped','mention']},
    {slug:'note-3',title:'Memory 3',host:'node-a',date:null,stale:false,stale_reason:null,verification:null,why:['fact']}]},
  related:{total:1,items:[{kind:'service',name:'web proxy',display:'Web proxy',shared_notes:2,fact_link:true}]},
  conflicts:{total:0,items:[]},
  inbox:{available:true,pending:2,can_review:false,items:null}};
const note = i => ({slug:'note-'+i,title:i === 0 ? '<img src=x onerror=alert(1)>' : 'Memory '+i,host:'node-a',importance:3,tags:i === 1 ? ['backup'] : [],body:'A durable note about backup configuration and restore procedures.'});
const server = createServer((req,res) => {
  const name = req.url === '/' ? 'index.html' : req.url.replace('/ui/assets/', '');
  if (!['index.html','app.js','admin.js','health.js','entities.js','style.css','icon.svg'].includes(name)) {res.writeHead(404).end();return;}
  res.setHeader('Content-Type', {'html':'text/html','css':'text/css','js':'text/javascript','svg':'image/svg+xml'}[name.split('.').pop()]);
  res.end(readFileSync(path.join(root,name)));
});
(async () => {
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  const base = 'http://127.0.0.1:'+server.address().port;
  const browser = await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_PATH || undefined});
  try {
    const context = await browser.newContext({viewport:{width:1440,height:1080}});
    const page = await context.newPage(); const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let offline = false, searches = 0, insightCalls = [], entityCalls = [];
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), p = url.pathname;
      if (p === '/' || p.startsWith('/ui/assets/')) return route.continue();
      const send = (body,status=200) => route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
      if (p === '/ui/me') return send({ok:true,enabled:false,authenticated:request.headers().authorization==='Bearer browser-test',stores:[],session:false});
      if (p === '/stats') return offline ? send({detail:'Offline'},503) : send(stats);
      if (p === '/health') return offline ? send({ok:false,status:'down'},200) : send({ok:true,status:'ok',checks:{index:{ok:true},git:{ok:true,in_sync:true},embed:{ok:true},rerank:{ok:true}}});
      if (request.headers().authorization !== 'Bearer browser-test') return send({detail:'invalid token'},401);
      if (p === '/ui/notes') {
        const offset = Number(url.searchParams.get('offset') || 0), limit = Number(url.searchParams.get('limit') || 24);
        if (url.searchParams.get('tag') === 'backup') return send({ok:true,profile:'test',total:1,notes:[note(1)]});
        return send({ok:true,profile:'test',total:52,notes:Array.from({length:Math.min(limit,52-offset)},(_,i)=>note(offset+i))});
      }
      if (p === '/insights') { insightCalls.push(url.search); return send(insights); }
      if (p === '/entities') { entityCalls.push(p + url.search); return send(entityList); }
      if (p.startsWith('/entities/')) {
        entityCalls.push(p + url.search);
        if (p === '/entities/host/node-a') return send(entityPage);
        if (p === '/entities/service/web%20proxy') return send({...entityPage,entity:{...entityPage.entity,kind:'service',name:'web proxy',display:'Web proxy'},related:{total:0,items:[]}});
        return send({detail:'No such entity in this store.'},404);
      }
      if (p === '/recall') {
        searches++; assert.equal(request.postDataJSON().include_core,false);
        return send({ok:true,notes:request.postDataJSON().query === 'nothing' ? [] : [note(0)]});
      }
      if (p === '/read') {
        const more = !!request.postDataJSON().offset;
        return send({ok:true,slug:'note-0',title:'Example memory',profile:'test',body:more?'\nSecond page.':'First page <script>alert(1)</script>',revision:'abc',continuation:more?null:{slug:'note-0',offset:10,revision:'abc'}});
      }
      return send({detail:'unknown route'},404);
    });
    await page.goto(base);
    await page.waitForFunction(() => document.querySelector('#notes').textContent === '52');
    await page.screenshot({path:shots+'/memd-dashboard-desktop.png',fullPage:true});
    await page.fill('#token','bad-token'); await page.click('#unlock-btn');
    await page.waitForSelector('#auth-error:not([hidden])');
    assert.match(await page.textContent('#auth-error'),/not accepted/);
    await page.fill('#token','browser-test'); await page.click('#unlock-btn');
    await page.waitForSelector('.note-card'); assert.equal(await page.locator('.note-card').count(),24);
    assert.equal(await page.locator('.note-card img').count(),0);
    await page.click('#next'); await page.waitForFunction(() => document.querySelector('#page-label').textContent.startsWith('25'));
    await page.click('#previous'); await page.waitForFunction(() => document.querySelector('#page-label').textContent.startsWith('1–'));
    // Tag chips filter from the keyboard (the card is a button, so the chip is role=button).
    await page.focus('.chip-tag'); await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.querySelectorAll('.note-card').length === 1);
    assert.match(await page.textContent('#result-count'),/tagged/);
    assert.equal(await page.locator('#reader[open]').count(),0);
    await page.click('.chip-active'); await page.waitForFunction(() => document.querySelectorAll('.note-card').length === 24);
    await page.fill('#query','backup'); await page.click('#search-btn');
    await page.waitForFunction(() => document.querySelectorAll('.note-card').length === 1);
    await page.click('.note-card'); await page.waitForSelector('#reader-more:not([hidden])');
    assert.equal(await page.locator('#reader script').count(),0);
    await page.click('#reader-more'); await page.waitForFunction(() => document.querySelector('#reader-body').textContent.includes('Second page.'));
    await page.click('#reader-close');
    await page.fill('#query','nothing'); await page.click('#search-btn'); await page.waitForSelector('.empty');
    assert.match(await page.textContent('#results'),/No memories found/); assert.equal(searches,2);
    await page.click('#tab-health'); await page.waitForSelector('#health-tiles .metric');
    assert.equal(await page.title(),'memd · Memory health');
    assert.equal(await page.locator('#memory-panel').isVisible(),false);
    assert.equal(await page.locator('#health-tiles .metric').count(),10);
    assert.match(await page.textContent('#health-tiles'),/Archived42 more proposed on memd\/forget/);
    assert.match(await page.textContent('#health-tiles'),/Never recalled25of 50 notes/);
    assert.match(await page.textContent('#health-tiles'),/Failed checks—No verify probes/);
    assert.match(await page.textContent('#health-meta'),/usage window 30 days/);
    assert.equal(await page.locator('#health-panel img').count(),0);
    assert.equal(await page.locator('.heatmap tbody tr').count(),2);
    assert.equal(await page.locator('.heatmap td.heat-5').count(),1);
    assert.equal(await page.getAttribute('.heatmap td.heat-5','aria-label'),'backup, 0-7d: 4 notes');
    assert.match(await page.textContent('#heat-more'),/2 tags with the most stale notes of 14/);
    assert.match(await page.textContent('.heatmap tbody th'),/<img src=x/);
    await page.selectOption('#heat-group','hosts'); await page.waitForFunction(() => document.querySelector('.heatmap tbody th').textContent === 'node-a');
    assert.match(await page.textContent('#health-lists'),/run mem-facts to derive them/);
    assert.match(await page.textContent('#health-lists'),/Showing 2 of 25/);
    assert.match(await page.textContent('#health-lists'),/mirror · points to/);
    await page.click('#health-lists .note-link >> text=Memory 3'); await page.waitForSelector('#reader[open]');
    await page.waitForFunction(() => document.querySelector('#reader-title').textContent === 'Example memory');
    await page.click('#reader-close');
    await page.click('#health-refresh');
    await page.waitForFunction(() => document.querySelector('#health-refresh').disabled === false);
    assert.equal(insightCalls.at(-1),'?profile=test&fresh=true');
    await page.screenshot({path:shots+'/memd-health-desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true);
    await page.emulateMedia({colorScheme:'dark'});
    await page.screenshot({path:shots+'/memd-health-mobile-dark.png',fullPage:true});
    await page.locator('.heat-panel').screenshot({path:shots+'/memd-health-heatmap-mobile-dark.png'});
    await page.emulateMedia({colorScheme:'light'}); await page.setViewportSize({width:1440,height:1080});
    await page.click('#tab-entities'); await page.waitForSelector('#entities-list .entity-row');
    assert.equal(await page.title(),'memd · Entities');
    assert.equal(await page.locator('#health-panel').isVisible(),false);
    assert.equal(await page.locator('#entities-list .entity-row').count(),4);
    assert.equal(await page.locator('#entities-panel img').count(),0);
    assert.match(await page.textContent('#entities-list'),/<img src=x/);
    assert.match(await page.textContent('#entities-meta'),/4 of 4 shown · 52 live notes/);
    await page.fill('#entities-query','PROXY'); await page.waitForFunction(() => document.querySelectorAll('#entities-list .entity-row').length === 1);
    await page.fill('#entities-query',''); await page.selectOption('#entities-kind','tag');
    await page.waitForFunction(() => document.querySelectorAll('#entities-list .entity-row').length === 1);
    await page.fill('#entities-query','nothing-like-this'); await page.waitForSelector('#entities-list .empty');
    await page.fill('#entities-query',''); await page.selectOption('#entities-kind','');
    await page.click('#entities-list .entity-link >> text=node-a'); await page.waitForSelector('#entity-sections .health-list');
    assert.equal(new URL(page.url()).hash,'#entities/host/node-a');
    assert.equal(await page.evaluate(() => document.activeElement.id),'entity-title');
    assert.equal(await page.textContent('#entity-title'),'node-a');
    assert.equal(await page.locator('#entity-tiles .metric').count(),5);
    assert.match(await page.textContent('#entity-tiles'),/8 scoped to it, 3 states a fact, 4 mentions it/);
    assert.equal(await page.locator('#entity-view img').count(),0);
    const sections = await page.textContent('#entity-sections');
    assert.match(sections,/web proxy · runs on · node-a/);
    assert.match(sections,/Showing 2 of 60/);
    assert.match(sections,/verification failed 2026-01-01: tcp node-a:53: refused/);
    assert.match(sections,/2 candidates wait for review. Sign in with write access/);
    assert.match(sections,/2025-06-01 → 2026-01-01/);
    await page.click('#entity-sections .note-link >> text=Memory 3'); await page.waitForSelector('#reader[open]');
    await page.waitForFunction(() => document.querySelector('#reader-title').textContent === 'Example memory');
    await page.click('#reader-close');
    await page.screenshot({path:shots+'/memd-entity-desktop.png',fullPage:true});
    await page.click('#entity-sections .entity-link >> text=Web proxy');
    await page.waitForFunction(() => document.querySelector('#entity-title').textContent === 'Web proxy');
    assert.ok(entityCalls.includes('/entities/service/web%20proxy?profile=test'));
    await page.goBack(); await page.waitForFunction(() => document.querySelector('#entity-title').textContent === 'node-a');
    await page.setViewportSize({width:390,height:844}); await page.emulateMedia({colorScheme:'dark'});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true);
    await page.screenshot({path:shots+'/memd-entity-mobile-dark.png',fullPage:true});
    await page.emulateMedia({colorScheme:'light'}); await page.setViewportSize({width:1440,height:1080});
    await page.evaluate(() => { location.hash = '#entities/host/elsewhere'; });
    await page.waitForSelector('#entities-error:not([hidden])');
    assert.match(await page.textContent('#entities-error'),/No such entity/);
    await page.click('#entity-view .back-link'); await page.waitForSelector('#entities-list-view:not([hidden])');
    assert.equal(await page.locator('#entities-error').isVisible(),false);
    await page.screenshot({path:shots+'/memd-entities-desktop.png',fullPage:true});
    await page.click('#tab-memory');
    await page.click('#tab-onboarding'); await page.fill('#machine','my-workstation');
    assert.match(await page.textContent('#issue-command'),/issue test my-workstation/);
    assert.match(await page.textContent('#onboard-command'),new RegExp(base));
    assert.equal(await page.locator('#memory-panel').isVisible(),false);
    await page.screenshot({path:shots+'/memd-onboarding-desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true);
    await page.screenshot({path:shots+'/memd-onboarding-mobile.png',fullPage:true});
    await page.click('#tab-memory'); await page.click('#lock');
    assert.equal(await page.locator('#query').isDisabled(),true);
    assert.equal(await page.textContent('#reader-body'),'');
    await page.click('#tab-health'); await page.waitForSelector('#health-locked:not([hidden])');
    assert.equal(await page.locator('#health-content').isVisible(),false);
    assert.equal(await page.locator('#health-tiles .metric').count(),0);
    await page.click('#tab-entities'); await page.waitForSelector('#entities-locked:not([hidden])');
    assert.equal(await page.locator('#entities-list .entity-row').count(),0);
    await page.click('#tab-memory');
    offline = true; await page.click('#refresh'); await page.waitForSelector('#status-error:not([hidden])');
    assert.equal(await page.textContent('#notes'),'—'); assert.equal(await page.textContent('#health-badge'),'Unavailable');
    offline = false; await page.click('#refresh'); await page.waitForFunction(() => document.querySelector('#notes').textContent === '52');
    await page.emulateMedia({colorScheme:'dark'});
    await page.screenshot({path:shots+'/memd-dashboard-mobile-dark.png',fullPage:true});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true);
    assert.deepEqual(errors,[]);
    console.log('PASS: unlock/rejection, browse/pagination, tag filter by keyboard, search/empty, full-note paging, text injection safety, memory health view, entities list/detail, onboarding, lock, outage/recovery, mobile and dark mode.');
  } finally {await browser.close();server.close();}
})().catch(error => {console.error(error);server.close();process.exitCode=1;});
