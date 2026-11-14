// Review controls and the Inbox tab of the account web app, without a browser:
// the page's own scripts run against a small DOM built from memd/web/index.html
// and a scripted fetch. Run by tests/test_web_review.py, one scenario per process:
//   node tests/web_review.node.cjs --list | <scenario>
'use strict';
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const {inspect} = require('node:util');
const vm = require('node:vm');
const web = path.join(__dirname, '..', 'memd', 'web');

// ---------------------------------------------------------------- a small DOM
const VOID = new Set(['meta', 'link', 'img', 'input', 'br', 'hr']);
// A node's links to the rest of the page are not enumerable. When a check on a node fails, node:assert
// renders every enumerable property of both values, to any depth, and compares the two renderings line
// by line: through `parent` and `page` that is the whole document once per element, and a process that
// runs out of memory where a short message was due.
const link = (node, links) => { for (const [name, value] of Object.entries(links)) Object.defineProperty(node, name, {value, writable: true}); };
class Text {
  constructor(data) { this.data = data; link(this, {parent: null}); }
  get textContent() { return this.data; }
}
class Element {
  constructor(tag, page) { this.tagName = tag; this.attrs = new Map(); link(this, {page, parent: null, childNodes: [], listeners: {}}); }
  get children() { return this.childNodes.filter(node => node instanceof Element); }
  get textContent() { return this.childNodes.map(node => node.textContent).join(''); }
  set textContent(value) { this.replaceChildren(...(value === null || value === undefined || value === '' ? [] : [String(value)])); }
  append(...items) {
    for (const item of items) {
      const node = item instanceof Element || item instanceof Text ? item : new Text(String(item));
      if (node.parent) node.parent.childNodes.splice(node.parent.childNodes.indexOf(node), 1);
      node.parent = this; this.childNodes.push(node);
    }
  }
  prepend(node) { this.append(node); this.childNodes.unshift(this.childNodes.pop()); }
  replaceChildren(...items) { for (const node of this.childNodes) node.parent = null; this.childNodes = []; this.append(...items); }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  removeAttribute(name) { this.attrs.delete(name); }
  get dataset() {
    const attrs = this.attrs;
    return new Proxy({}, {get: (_, key) => attrs.get('data-' + key), set: (_, key, value) => { attrs.set('data-' + key, String(value)); return true; }});
  }
  get type() { return this.attrs.get('type') || (this.tagName === 'button' ? 'submit' : this.tagName === 'input' ? 'text' : ''); }
  set type(value) { this.attrs.set('type', value); }
  get options() { return this.querySelectorAll('option'); }
  get value() {
    if (this.tagName === 'option') return this.attrs.has('value') ? this.attrs.get('value') : this.textContent;
    if (this.tagName === 'select') {
      const options = this.options, chosen = options.find(option => option.selected) || (this.unset ? null : options[0]);
      return chosen ? chosen.value : '';
    }
    if (this.entered !== undefined) return this.entered;
    return this.tagName === 'textarea' ? this.textContent : this.attrs.get('value') || '';
  }
  set value(value) {
    const text = value === null || value === undefined ? '' : String(value);
    if (this.tagName === 'option') this.attrs.set('value', text);
    else if (this.tagName === 'select') {
      const hit = this.options.find(option => option.value === text);
      for (const option of this.options) option.selected = option === hit;
      this.unset = !hit;
    } else this.entered = text;
  }
  get checked() { return this.ticked === undefined ? this.attrs.has('checked') : this.ticked; }
  set checked(value) { this.ticked = !!value; }
  get open() { return this.attrs.has('open'); }
  showModal() { this.attrs.set('open', ''); }
  close() { if (this.open) { this.attrs.delete('open'); this.dispatchEvent(event('close')); } }
  reset() {
    for (const field of this.querySelectorAll('input').concat(this.querySelectorAll('textarea'))) { field.entered = undefined; field.ticked = undefined; }
    for (const select of this.querySelectorAll('select')) { select.unset = false; for (const option of select.options) option.selected = false; }
  }
  focus() { if (visible(this) && !this.disabled) this.page.document.activeElement = this; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  dispatchEvent(fired) {
    fired.target ||= this;
    for (let node = this; node && !fired.stopped; node = node.parent) for (const listener of node.listeners[fired.type] || []) listener.call(node, fired);
    return !fired.defaultPrevented;
  }
  querySelectorAll(selector) {
    const chain = selector.trim().split(/\s+/).map(compound), found = [];
    const walk = node => { for (const child of node.children) { if (matches(child, chain)) found.push(child); walk(child); } };
    walk(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
for (const [property, attribute] of [['hidden', 'hidden'], ['disabled', 'disabled'], ['readOnly', 'readonly']]) {
  Object.defineProperty(Element.prototype, property, {
    get() { return this.attrs.has(attribute); },
    set(on) { if (on) this.attrs.set(attribute, ''); else this.attrs.delete(attribute); }});
}
for (const [property, attribute] of [['id', 'id'], ['className', 'class'], ['href', 'href']]) {
  Object.defineProperty(Element.prototype, property, {
    get() { return this.attrs.get(attribute) || ''; }, set(value) { this.attrs.set(attribute, String(value)); }});
}
function compound(text) {
  const parts = {tag: (text.match(/^[a-z0-9]+/i) || [''])[0].toLowerCase(), tests: []};
  for (const [, id, name, attribute, value] of text.matchAll(/#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g)) {
    if (id) parts.tests.push(node => node.id === id);
    else if (name) parts.tests.push(node => node.className.split(/\s+/).includes(name));
    else parts.tests.push(node => node.attrs.has(attribute) && (value === undefined || node.attrs.get(attribute) === value));
  }
  return parts;
}
const fits = (node, parts) => (!parts.tag || node.tagName === parts.tag) && parts.tests.every(test => test(node));
function matches(node, chain) {
  if (!fits(node, chain[chain.length - 1])) return false;
  let above = node.parent;
  for (let i = chain.length - 2; i >= 0; i--) {
    while (above && !fits(above, chain[i])) above = above.parent;
    if (!above) return false;
    above = above.parent;
  }
  return true;
}
const event = (type, extra = {}) => ({type, defaultPrevented: false, stopped: false,
  preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; }, ...extra});
function visible(node) { for (let at = node; at; at = at.parent) if (at.hidden || (at.tagName === 'dialog' && !at.open)) return false; return true; }
const decode = text => text.replace(/&(amp|lt|gt|quot);/g, (_, name) => ({amp: '&', lt: '<', gt: '>', quot: '"'}[name]));
function parse(html, page) {
  const root = new Element('#document', page);
  let at = root;
  const token = /<!--[\s\S]*?-->|<!doctype[^>]*>|<\/([a-z0-9]+)>|<([a-z0-9]+)((?:\s+[^\s=>\/]+(?:="[^"]*")?)*)\s*\/?>|([^<]+)/gi;
  for (const [, closing, opening, attributes, text] of html.matchAll(token)) {
    if (closing) {
      let open = at;
      while (open !== root && open.tagName !== closing.toLowerCase()) open = open.parent;
      if (open !== root) at = open.parent;
    } else if (opening) {
      const node = new Element(opening.toLowerCase(), page);
      for (const [, name, value] of attributes.matchAll(/([^\s=]+)(?:="([^"]*)")?/g)) node.attrs.set(name.toLowerCase(), decode(value || ''));
      at.append(node);
      if (!VOID.has(node.tagName)) at = node;
    } else if (text) at.append(decode(text));
  }
  return root;
}

// ---------------------------------------------------------------- the page
const settle = async (turns = 8) => { for (let i = 0; i < turns; i++) await new Promise(resolve => setImmediate(resolve)); };
function deferred() { let release; const promise = new Promise(resolve => { release = resolve; }); return {promise, release}; }

async function open(serve) {
  const page = {requests: []}, listeners = {};
  let hash = '';
  const root = parse(readFileSync(path.join(web, 'index.html'), 'utf8'), page);
  const find = (node, id) => { for (const child of node.children) { if (child.id === id) return child; const inner = find(child, id); if (inner) return inner; } return null; };
  page.document = {hidden: false, title: '', activeElement: null, getElementById: id => find(root, id),
    createElement: tag => new Element(tag.toLowerCase(), page), querySelectorAll: selector => root.querySelectorAll(selector),
    addEventListener() {}};
  const window = {document: page.document, console, URLSearchParams, AbortController, setTimeout, clearTimeout, setInterval: () => 0,
    CustomEvent: class { constructor(type, init = {}) { this.type = type; this.detail = init.detail; } },
    addEventListener(type, listener) { (listeners[type] ||= []).push(listener); },
    dispatchEvent(fired) { for (const listener of listeners[fired.type] || []) listener(fired); return true; },
    confirm: () => true, navigator: {clipboard: {writeText: async () => {}}}, innerWidth: 1280,
    history: {replaceState(_state, _title, url) { hash = url; }},
    location: {origin: 'http://memd.example.com', get hash() { return hash; },
      set hash(value) {
        const next = value.startsWith('#') ? value : '#' + value;
        if (next === hash) return;
        hash = next; setImmediate(() => window.dispatchEvent({type: 'hashchange'}));
      }},
    fetch(url, init = {}) {
      const [route, search = ''] = url.split('?');
      const request = {method: init.method || 'GET', path: route, query: Object.fromEntries(new URLSearchParams(search)),
        headers: {...init.headers}, body: init.body === undefined ? undefined : JSON.parse(init.body), credentials: init.credentials};
      page.requests.push(request);
      return Promise.resolve().then(() => serve(request)).then(answer => {
        const [status, data] = Array.isArray(answer) ? answer : [200, answer];
        return {status, ok: status >= 200 && status < 300, json: async () => JSON.parse(JSON.stringify(data))};
      });
    }};
  window.window = window;
  vm.createContext(window);
  // The scripts the page itself loads, in its order.
  const scripts = root.querySelectorAll('script').map(script => script.getAttribute('src'));
  for (const src of scripts) {
    assert.match(src, /^\/ui\/assets\/[a-z]+\.js$/);
    vm.runInContext(readFileSync(path.join(web, path.basename(src)), 'utf8'), window, {filename: path.basename(src)});
  }
  page.window = window; page.scripts = scripts.map(src => path.basename(src));
  page.$ = id => { const node = page.document.getElementById(id); assert.ok(node, 'no element #' + id); return node; };
  page.go = async value => { window.location.hash = value; await settle(); };
  page.hash = () => hash;
  page.fire = async (node, type, extra) => { node.dispatchEvent(event(type, extra)); await settle(); };
  page.click = async node => {
    assert.ok(visible(node), 'clicked something that is not shown: ' + (node.id || node.tagName));
    if (node.disabled) return;
    const fired = event('click');
    node.dispatchEvent(fired);
    if (node.tagName === 'a' && node.href.startsWith('#')) window.location.hash = node.href;
    if (node.tagName === 'button' && node.type === 'submit' && !fired.defaultPrevented) {
      let form = node.parent;
      while (form && form.tagName !== 'form') form = form.parent;
      if (form) form.dispatchEvent(event('submit'));
    }
    await settle();
  };
  page.choose = async (select, value) => { select.value = value; assert.equal(select.value, value); await page.fire(select, 'change'); };
  page.sent = (method, pattern) => page.requests.filter(request => request.method === method && pattern.test(request.path));
  await settle();
  return page;
}

// ---------------------------------------------------------------- a scripted server
const store = (id, permission, review, policy) => ({id, name: id === 'team' ? 'Team notes' : id, permission,
  ...(review === undefined ? {} : {review}), ...(policy === undefined ? {} : {review_policy: policy})});
const SIGNED_OUT = {ok: true, enabled: true, authenticated: false, username: null, user_id: null, admin: false, session: false, csrf: '', default_store: '', stores: []};
const session = (stores, fields = {}) => ({ok: true, enabled: true, authenticated: true, username: 'cobalt', user_id: 'u-cobalt', admin: false,
  session: true, csrf: 'csrf-of-this-session', default_store: stores[0]?.id || '', stores, ...fields});
const LINT = {slug: 'restore-drill', action: 'create', related: [], near_duplicates: [], conflicts: [], warnings: [], error: null};
let serial = 0;
const candidate = (fields = {}) => ({id: (++serial).toString(16).padStart(16, '0'), status: 'pending', created: Date.now() / 1000 - 600,
  source: 'agent', proposer: 'laptop-agent', title: 'Restore drill', body: 'Restore the newest verified backup.', meta: {tags: ['backup']},
  lint: LINT, reviewer: null, decided_at: null, reason: null, slug: null, revision: null, last_error: null, published_from: null,
  receipt: null, original: null, ...fields});

// What a "four-eyes" store answers with 403: a decision from the account that proposed
// the candidate (its `account`, which the server never sends), and a change to its text.
const REFUSED = {own: 'This store needs a second reviewer: a candidate cannot be decided from the account that proposed it.',
  text: 'In this store a reviewer may change only importance and tags.'};

// The endpoints the page calls. `inbox` maps a store to its candidates; `intercept`
// answers a request first when it returns something.
function backend(identity, inbox = {}) {
  const state = {identity, inbox, intercept: null, overview: null};
  state.serve = request => {
    const early = state.intercept ? state.intercept(request) : undefined;
    if (early !== undefined) return early;
    const {path: route, query, body} = request;
    if (route === '/ui/me') return request.headers.Authorization ? state.token : state.identity;
    if (route === '/ui/logout') { state.identity = SIGNED_OUT; return {ok: true}; }
    if (route === '/stats') return {profile: query.profile || 'team', notes: 0, superseded: 0, vec: 0, fts: 0, core: 0, size: 0, hosts: [], importance: []};
    if (route === '/health') return {status: 'ok', checks: {}};
    if (route === '/ui/notes') return {notes: [], total: 0};
    if (route === '/admin/overview') return state.overview;
    // A store's settings: review_policy is one of three values or a 400, and the overview then reports it.
    const [, edited] = (request.method === 'PUT' && route.match(/^\/admin\/stores\/([a-z][a-z0-9-]*)$/)) || [];
    if (edited) {
      const entry = state.overview.stores.find(s => s.id === edited), policy = body.config.review_policy;
      if (policy !== undefined && !['open', 'agents', 'four-eyes'].includes(policy)) return [400, {detail: 'review_policy must be open, agents or four-eyes.'}];
      entry.name = body.name;
      // GET /ui/me names the policy of the same store, for a session that can reach it.
      const mine = state.identity.stores.find(s => s.id === edited);
      if (policy !== undefined) { entry.review_policy = policy; if (mine) mine.review_policy = policy; }
      return {ok: true, store: entry};
    }
    if (route.startsWith('/admin/')) return {ok: true, token: 'mem_team_issued'};
    const [, id, action] = route.match(/^\/inbox(?:\/([0-9a-f]+))?(?:\/(approve|reject))?$/) || [];
    if (!route.startsWith('/inbox')) return [404, {detail: 'Not found'}];
    const profile = (body || query).profile, held = state.inbox[profile];
    if (!held) return [403, {detail: 'Reviewing the inbox needs review access to this store.'}];
    if (!id) {
      const status = query.status || 'pending', offset = Number(query.offset), shown = held.filter(c => c.status === status);
      const counts = {pending: 0, approved: 0, rejected: 0};
      for (const c of held) counts[c.status]++;
      return {ok: true, total: shown.length, counts, offset, profile: query.profile,
        items: shown.slice(offset, offset + Number(query.limit)).map(({body: text, receipt, original, account, ...rest}) => ({...rest, excerpt: text.slice(0, 240)}))};
    }
    const found = held.find(c => c.id === id);
    if (!found) return [404, {detail: 'Candidate not found'}];
    if (!action) { const {account, ...shown} = found; return shown; }
    if (found.status !== 'pending') return [409, {detail: `This candidate was already ${found.status}`}];
    if (state.identity.stores.find(s => s.id === profile)?.review_policy === 'four-eyes') {
      if (found.account && found.account === state.identity.user_id) return [403, {detail: REFUSED.own}];
      if (Object.keys(body.edits || {}).some(key => !['tags', 'importance'].includes(key))) return [403, {detail: REFUSED.text}];
    }
    Object.assign(found, {status: action === 'approve' ? 'approved' : 'rejected', reviewer: state.identity.username, decided_at: Date.now() / 1000});
    if (action === 'reject') { found.reason = body.reason || ''; return {ok: true, id, status: 'rejected'}; }
    found.slug = 'restore-drill';
    return {ok: true, id, status: 'approved', edited: !!body.edits, receipt: {slug: found.slug, synced: false, warnings: []}};
  };
  return state;
}
// Holds the requests that match until release(), then answers them as usual.
function hold(server, test) {
  const gate = deferred(), before = server.intercept;
  server.intercept = request => {
    if (!test(request)) return before ? before(request) : undefined;
    return gate.promise.then(() => { const mine = server.intercept; server.intercept = null; try { return server.serve(request); } finally { server.intercept = mine; } });
  };
  return gate;
}
const REVIEWER = () => session([store('amber', 'read', false), store('team', 'read', true)], {default_store: 'amber'});
const WRITER = () => session([store('team', 'write', true)]);
const rows = page => page.$('inbox-list').querySelectorAll('.entity-row');
const openCandidate = async (page, title) => {
  const link = page.$('inbox-list').querySelectorAll('.entity-link').find(node => node.textContent === title);
  assert.ok(link, 'no candidate titled ' + title);
  await page.click(link);
};
const type = (page, id, value) => { page.$(id).value = value; };
// Settings as an administrator sees it: the stores of GET /admin/overview, each on its card.
const ADMIN = () => session([store('amber', 'write', true), store('team', 'write', true)], {username: 'amber', user_id: 'u-amber', admin: true});
const overview = stores => ({ok: true, stores, users: [], grants: [], tokens: [], jobs: [], audit: []});
const listed = (id, kind, fields = {}, config = {}) => ({id, name: id === 'team' ? 'Team notes' : id, kind, config, publish_review: true, ...fields});
const card = (page, name) => page.$('stores-list').children.find(node => node.querySelector('h2').textContent === name);
const chips = node => node.querySelectorAll('.chip').map(chip => chip.textContent);
const configure = async (page, name) => {
  await page.click(card(page, name).querySelectorAll('button').find(button => button.textContent === 'Configure'));
  assert.equal(page.$('store-dialog').open, true);
};
const radios = page => page.$('store-review').querySelectorAll('input');
const chosen = page => radios(page).filter(radio => radio.checked).map(radio => radio.value);
// Choosing a radio button: the browser clears the rest of its group, then fires change on it.
const pick = async (page, value) => {
  for (const radio of radios(page)) radio.checked = radio.value === value;
  await page.fire(radios(page).find(radio => radio.value === value), 'change');
};
const saveStore = async page => {
  await page.click(page.$('store-submit'));
  assert.equal(page.$('store-error').textContent, '');
  assert.equal(page.$('store-dialog').open, false);
};
const closeStore = page => page.click(page.$('store-dialog').querySelector('[data-close]'));

// ---------------------------------------------------------------- scenarios
const SCENARIOS = {
  async the_page_loads_the_inbox_script_last() {
    const page = await open(backend(SIGNED_OUT).serve);
    assert.deepEqual(page.scripts, ['app.js', 'admin.js', 'health.js', 'entities.js', 'inbox.js']);
  },

  async no_inbox_tab_when_signed_out_or_unlocked_with_a_token() {
    const server = backend(SIGNED_OUT, {team: [candidate()]});
    // A token that may even review: the tab is for signed-in sessions only.
    server.token = {...session([store('team', 'write', true)]), session: false, csrf: ''};
    const page = await open(server.serve);
    assert.equal(page.$('tab-inbox').hidden, true);
    type(page, 'token', 'mem_team_secret');
    await page.fire(page.$('unlock-form'), 'submit');
    assert.equal(page.$('lock').hidden, false);
    assert.equal(page.$('tab-inbox').hidden, true);
    await page.go('#inbox');
    assert.equal(page.$('inbox-panel').hidden, true);
    assert.equal(page.$('memory-panel').hidden, false);
    assert.deepEqual(page.sent('GET', /^\/inbox/), []);
  },

  async inbox_tab_needs_review_on_a_store() {
    const server = backend(REVIEWER(), {team: [candidate()]});
    const page = await open(server.serve);
    assert.equal(page.$('tab-inbox').hidden, false);
    await page.go('#inbox');
    assert.equal(page.$('inbox-panel').hidden, false);
    assert.equal(page.window.document.title, 'memd · Inbox');
    assert.equal(page.$('tab-inbox').getAttribute('aria-current'), 'page');
    // One store to review: no picker, and it is the store asked for although the header shows another.
    assert.equal(page.$('inbox-store-field').hidden, true);
    assert.equal(page.$('store-select').value, 'amber');
    const [listing] = page.sent('GET', /^\/inbox$/);
    assert.deepEqual(listing.query, {profile: 'team', status: 'pending', offset: '0', limit: '20'});
    // The session cookie, never a bearer token.
    assert.equal(listing.headers.Authorization, undefined);
    assert.equal(listing.credentials, 'same-origin');
    assert.equal(rows(page).length, 1);
    // Write without review is not enough.
    server.identity = session([store('amber', 'write', false), store('team', 'read', false)]);
    await page.window.memd.loadIdentity();
    await settle();
    assert.equal(page.$('tab-inbox').hidden, true);
    assert.equal(page.$('inbox-panel').hidden, true);
    assert.equal(rows(page).length, 0);
  },

  async a_server_that_reports_no_review_means_write() {
    const server = backend(session([store('amber', 'read'), store('team', 'write')]), {team: [candidate()], amber: [candidate()]});
    const page = await open(server.serve);
    assert.equal(page.$('tab-inbox').hidden, false);
    await page.go('#inbox');
    assert.equal(page.$('inbox-store-field').hidden, true);
    assert.deepEqual(page.$('inbox-store').options.map(option => option.value), ['team']);
    assert.equal(page.sent('GET', /^\/inbox$/)[0].query.profile, 'team');
    // And no tab at all when every store is read-only.
    server.identity = session([store('amber', 'read'), store('team', 'read')]);
    await page.window.memd.loadIdentity();
    assert.equal(page.$('tab-inbox').hidden, true);
  },

  async the_store_picker_lists_only_stores_with_review() {
    const server = backend(session([store('amber', 'write', true), store('cobalt', 'write', false), store('team', 'read', true)]),
      {amber: [candidate({title: 'In amber'})], team: [candidate({title: 'In team'}), candidate({title: 'Also in team'})]});
    const page = await open(server.serve);
    await page.go('#inbox');
    assert.equal(page.$('inbox-store-field').hidden, false);
    assert.deepEqual(page.$('inbox-store').options.map(option => [option.value, option.textContent]), [['amber', 'amber'], ['team', 'Team notes']]);
    assert.equal(rows(page)[0].textContent.includes('In amber'), true);
    await page.choose(page.$('inbox-store'), 'team');
    assert.equal(page.requests.at(-1).query.profile, 'team');
    assert.deepEqual(rows(page).map(row => row.querySelector('.entity-link').textContent), ['In team', 'Also in team']);
    assert.match(page.$('inbox-meta').textContent, /^1–2 of 2 candidates · Team notes$/);
    // A link to a candidate in a store this session does not review ends at the list, with a reason.
    await page.go('#inbox/cobalt/00000000000000aa');
    assert.equal(page.hash(), '#inbox');
    assert.equal(page.$('inbox-view').hidden, true);
    assert.equal(page.$('inbox-message').textContent, 'That candidate is in a store you do not review.');
    assert.deepEqual(page.requests.filter(request => request.query.profile === 'cobalt' && request.path.startsWith('/inbox')), []);
  },

  async the_list_shows_text_flags_counts_and_pages() {
    const lint = {...LINT, error: 'importance must be 1 to 5', conflicts: [{slug: 'old-drill', kind: 'contradicts'}],
      near_duplicates: [{slug: 'restore-notes', cosine: 0.93}], pending_duplicates: [{id: '00000000000000ff', title: 'Same again'}]};
    const pending = [candidate({title: 'Restore drill <b>steps</b>', body: '<img src=x onerror=alert(1)> first', lint}),
      ...Array.from({length: 44}, (_, i) => candidate({title: 'Candidate ' + (i + 2)}))];
    const decided = [candidate({status: 'approved', reviewer: 'amber', decided_at: Date.now() / 1000 - 7200, slug: 'kept'}),
      candidate({status: 'rejected', reviewer: 'amber', decided_at: Date.now() / 1000 - 60, reason: 'Not durable'})];
    const page = await open(backend(REVIEWER(), {team: [...pending, ...decided]}).serve);
    await page.go('#inbox');
    assert.equal(rows(page).length, 20);
    const first = rows(page)[0];
    assert.equal(first.querySelector('.entity-link').textContent, 'Restore drill <b>steps</b>');
    assert.equal(first.querySelector('.entity-link').href, '#inbox/team/' + pending[0].id);
    assert.deepEqual(first.querySelectorAll('.badge').map(badge => badge.textContent), ['Needs changes', 'Conflict', 'Near duplicate', 'Pending duplicate']);
    assert.match(first.textContent, /laptop-agent · agent · proposed 10 min ago/);
    assert.match(first.textContent, /<img src=x onerror=alert\(1\)> first/);
    assert.deepEqual(rows(page)[1].querySelectorAll('.badge'), []);
    // What an agent wrote is text: it made no elements.
    for (const tag of ['b', 'img', 'script']) assert.deepEqual(page.$('inbox-panel').querySelectorAll(tag), [], tag);
    assert.deepEqual(page.$('inbox-status').options.map(option => option.textContent), ['Waiting for review (45)', 'Approved (1)', 'Rejected (1)']);
    assert.equal(page.$('inbox-meta').textContent, '1–20 of 45 candidates · Team notes');
    assert.equal(page.$('inbox-pagination').hidden, false);
    assert.equal(page.$('inbox-page-label').textContent, 'Page 1 of 3');
    assert.equal(page.$('inbox-previous').disabled, true);
    await page.click(page.$('inbox-next'));
    await page.click(page.$('inbox-next'));
    assert.equal(page.requests.at(-1).query.offset, '40');
    assert.equal(page.$('inbox-meta').textContent, '41–45 of 45 candidates · Team notes');
    assert.equal(page.$('inbox-page-label').textContent, 'Page 3 of 3');
    assert.equal(page.$('inbox-next').disabled, true);
    assert.equal(rows(page).length, 5);
    await page.click(page.$('inbox-previous'));
    assert.equal(page.$('inbox-meta').textContent, '21–40 of 45 candidates · Team notes');
    // Another status starts at its first page and says who decided.
    await page.choose(page.$('inbox-status'), 'rejected');
    assert.deepEqual(page.requests.at(-1).query, {profile: 'team', status: 'rejected', offset: '0', limit: '20'});
    assert.equal(rows(page).length, 1);
    assert.match(rows(page)[0].textContent, /rejected 1 min ago by amber/);
    assert.equal(page.$('inbox-pagination').hidden, true);
    await page.choose(page.$('inbox-status'), 'approved');
    assert.match(rows(page)[0].textContent, /approved 2 hours ago by amber/);
  },

  async a_page_that_failed_to_load_is_not_skipped() {
    const server = backend(REVIEWER(), {team: Array.from({length: 45}, (_, i) => candidate({title: 'Candidate ' + (i + 1)}))});
    const page = await open(server.serve);
    await page.go('#inbox');
    server.intercept = request => request.path === '/inbox' ? [503, {detail: 'The store is busy. Try again shortly.'}] : undefined;
    await page.click(page.$('inbox-next'));
    assert.equal(page.$('inbox-error').hidden, false);
    assert.equal(page.$('inbox-error').textContent, 'The store is busy. Try again shortly.');
    // Still on the first page, as the list and its label say.
    assert.equal(page.$('inbox-meta').textContent, '1–20 of 45 candidates · Team notes');
    assert.equal(rows(page)[0].textContent.includes('Candidate 1'), true);
    server.intercept = null;
    await page.click(page.$('inbox-next'));
    assert.equal(page.requests.at(-1).query.offset, '20');
    assert.equal(page.$('inbox-error').hidden, true);
    assert.equal(page.$('inbox-meta').textContent, '21–40 of 45 candidates · Team notes');
  },

  async an_older_list_answer_does_not_replace_a_newer_one() {
    const held = [candidate({title: 'Waiting'}), candidate({title: 'Kept', status: 'approved', reviewer: 'amber', decided_at: 1}),
      candidate({title: 'Dropped', status: 'rejected', reviewer: 'amber', decided_at: 1})];
    const server = backend(REVIEWER(), {team: held});
    const page = await open(server.serve);
    await page.go('#inbox');
    const approved = hold(server, request => request.path === '/inbox' && request.query.status === 'approved');
    const rejected = hold(server, request => request.path === '/inbox' && request.query.status === 'rejected');
    await page.choose(page.$('inbox-status'), 'approved');
    await page.choose(page.$('inbox-status'), 'rejected');
    rejected.release(); await settle();
    approved.release(); await settle();
    assert.deepEqual(rows(page).map(row => row.querySelector('.entity-link').textContent), ['Dropped']);
    assert.equal(page.$('inbox-list').getAttribute('aria-busy'), 'false');
    assert.equal(page.$('inbox-refresh').disabled, false);
  },

  async a_reviewer_without_write_sees_the_candidate_and_changes_only_tags_and_importance() {
    const lint = {...LINT, action: 'supersede', supersedes: 'old-drill', related: ['backup-procedures', 'restore-notes'],
      conflicts: [{slug: 'old-drill', title: 'Old drill', kind: 'updates', evidence: 'Says weekly, not daily.'}],
      near_duplicates: [{slug: 'restore-notes', cosine: 0.931}], warnings: ['The title is long.'], error: 'verify needs a probe'};
    const held = [candidate({title: 'Restore drill <b>steps</b>', lint, meta: {tags: ['backup'], importance: 4, host: 'vmhost', description: 'How to restore.'}}),
      candidate({title: 'Second'})];
    const server = backend(REVIEWER(), {team: held});
    const page = await open(server.serve);
    await page.go('#inbox');
    await openCandidate(page, 'Restore drill <b>steps</b>');
    assert.equal(page.hash(), '#inbox/team/' + held[0].id);
    assert.deepEqual(page.requests.at(-1).query, {profile: 'team'});
    assert.equal(page.requests.at(-1).path, '/inbox/' + held[0].id);
    assert.equal(page.$('inbox-list-view').hidden, true);
    assert.equal(page.$('inbox-view').hidden, false);
    assert.equal(page.$('inbox-candidate').hidden, false);
    assert.equal(page.window.document.activeElement, page.$('inbox-candidate-title'));
    assert.equal(page.$('inbox-candidate-title').textContent, 'Restore drill <b>steps</b>');
    assert.equal(page.$('inbox-candidate-status').textContent, 'Waiting for review');
    assert.equal(page.$('inbox-field-body').value, 'Restore the newest verified backup.');
    assert.equal(page.$('inbox-field-description').value, 'How to restore.');
    assert.equal(page.$('inbox-field-tags').value, 'backup');
    assert.equal(page.$('inbox-field-importance').value, '4');
    for (const id of ['inbox-field-title', 'inbox-field-description', 'inbox-field-body']) {
      assert.equal(page.$(id).readOnly, true, id);
      assert.equal(page.$(id).getAttribute('aria-describedby'), 'inbox-text-help', id);
    }
    assert.equal(page.$('inbox-field-tags').readOnly, false);
    assert.equal(page.$('inbox-field-importance').disabled, false);
    assert.equal(page.$('inbox-text-help').hidden, false);
    assert.equal(page.$('inbox-actions').hidden, false);
    assert.equal(page.$('inbox-decided').hidden, true);
    // The preview of what approving would do, and what was proposed.
    assert.equal(page.$('inbox-lint-action').textContent, 'Replaces old-drill with restore-drill. Checked when it was proposed.');
    assert.deepEqual(page.$('inbox-lint-items').children.map(item => item.textContent), ['Needs changesverify needs a probe',
      'UpdatesOld drillSays weekly, not daily.', 'Near duplicaterestore-notessimilarity 0.93', 'Relatedbackup-procedures', 'NoteThe title is long.']);
    assert.match(page.$('inbox-details').textContent, /^Proposed bylaptop-agentChannelagentProposed.+StoreTeam notesHostvmhostCandidate[0-9a-f]{16}$/);
    for (const tag of ['b', 'img']) assert.deepEqual(page.$('inbox-view').querySelectorAll(tag), [], tag);
    // Tags and importance changed; a text field changed behind the page's back stays unsent.
    type(page, 'inbox-field-tags', 'backup, drill ,');
    type(page, 'inbox-field-importance', '5');
    type(page, 'inbox-field-title', 'Rewritten by a reviewer');
    await page.click(page.$('inbox-approve'));
    const [approval] = page.sent('POST', /\/approve$/);
    assert.equal(approval.path, `/inbox/${held[0].id}/approve`);
    assert.deepEqual(approval.body, {profile: 'team', edits: {tags: ['backup', 'drill'], importance: 5}});
    assert.equal(approval.headers['X-CSRF-Token'], 'csrf-of-this-session');
    assert.equal(approval.headers.Authorization, undefined);
    assert.equal(approval.credentials, 'same-origin');
    // Back at the list, which was loaded again, with the outcome and the focus on the heading.
    assert.equal(page.hash(), '#inbox');
    assert.equal(page.$('inbox-view').hidden, true);
    assert.deepEqual(rows(page).map(row => row.querySelector('.entity-link').textContent), ['Second']);
    assert.equal(page.$('inbox-message').textContent, 'Approved “Restore drill <b>steps</b>” and saved it as restore-drill. Your changes were applied.');
    assert.equal(page.window.document.activeElement, page.$('inbox-title'));
    // Nothing changed: no edits at all.
    await openCandidate(page, 'Second');
    assert.equal(page.$('inbox-message').hidden, true);
    await page.click(page.$('inbox-approve'));
    assert.deepEqual(page.sent('POST', /\/approve$/)[1].body, {profile: 'team'});
    assert.equal(page.$('inbox-message').textContent, 'Approved “Second” and saved it as restore-drill.');
  },

  async a_reviewer_with_write_sends_only_the_fields_that_changed() {
    const held = [candidate({meta: {tags: ['backup'], description: 'How to restore.'}}), candidate({title: 'Second'})];
    const page = await open(backend(WRITER(), {team: held}).serve);
    await page.go('#inbox');
    await openCandidate(page, 'Restore drill');
    for (const id of ['inbox-field-title', 'inbox-field-description', 'inbox-field-body', 'inbox-field-tags']) {
      assert.equal(page.$(id).readOnly, false, id);
      assert.equal(page.$(id).getAttribute('aria-describedby'), null, id);
    }
    assert.equal(page.$('inbox-text-help').hidden, true);
    // Importance was not proposed: the field shows the default, which is not a change.
    assert.equal(page.$('inbox-field-importance').value, '3');
    type(page, 'inbox-field-title', 'Restore drill, monthly');
    await page.click(page.$('inbox-approve'));
    assert.deepEqual(page.sent('POST', /\/approve$/)[0].body, {profile: 'team', edits: {title: 'Restore drill, monthly'}});
    await openCandidate(page, 'Second');
    type(page, 'inbox-field-body', 'The text, corrected.');
    type(page, 'inbox-field-description', 'A summary.');
    type(page, 'inbox-field-tags', '');
    await page.click(page.$('inbox-approve'));
    assert.deepEqual(page.sent('POST', /\/approve$/)[1].body,
      {profile: 'team', edits: {description: 'A summary.', body: 'The text, corrected.', tags: []}});
  },

  async rejecting_takes_an_optional_reason() {
    const held = [candidate({title: 'Scratch observation'}), candidate({title: 'Second'})];
    const page = await open(backend(REVIEWER(), {team: held}).serve);
    await page.go('#inbox');
    await openCandidate(page, 'Scratch observation');
    await page.click(page.$('inbox-reject'));
    const dialog = page.$('inbox-reject-dialog');
    assert.equal(dialog.open, true);
    assert.equal(page.window.document.activeElement, page.$('inbox-reject-reason'));
    assert.equal(page.$('inbox-reject-name').textContent, 'Scratch observation');
    assert.deepEqual(page.sent('POST', /\/reject$/), []);
    type(page, 'inbox-reject-reason', '  Not durable enough to keep ');
    await page.click(page.$('inbox-reject-submit'));
    const [rejection] = page.sent('POST', /\/reject$/);
    assert.equal(rejection.path, `/inbox/${held[0].id}/reject`);
    assert.deepEqual(rejection.body, {profile: 'team', reason: 'Not durable enough to keep'});
    assert.equal(rejection.headers['X-CSRF-Token'], 'csrf-of-this-session');
    assert.equal(dialog.open, false);
    assert.equal(page.hash(), '#inbox');
    assert.deepEqual(rows(page).map(row => row.querySelector('.entity-link').textContent), ['Second']);
    assert.equal(page.$('inbox-message').textContent, 'Rejected “Scratch observation”. Nothing was saved.');
    assert.match(page.$('inbox-status').textContent, /Waiting for review \(1\)Approved \(0\)Rejected \(1\)/);
    // Without a reason, and the last reason is not carried over.
    await openCandidate(page, 'Second');
    await page.click(page.$('inbox-reject'));
    assert.equal(page.$('inbox-reject-reason').value, '');
    await page.click(page.$('inbox-reject-submit'));
    assert.deepEqual(page.sent('POST', /\/reject$/)[1].body, {profile: 'team'});
    assert.equal(page.$('inbox-list').querySelectorAll('.empty').length, 1);
    // The decided candidate says who, when and why, and offers no decision.
    await page.choose(page.$('inbox-status'), 'rejected');
    await openCandidate(page, 'Scratch observation');
    assert.equal(page.$('inbox-decided').hidden, false);
    assert.match(page.$('inbox-decided').textContent, /^Rejected by cobalt on .+\. Reason: Not durable enough to keep$/);
    assert.equal(page.$('inbox-actions').hidden, true);
    assert.equal(page.$('inbox-field-tags').readOnly, true);
    assert.equal(page.$('inbox-field-importance').disabled, true);
    await page.fire(page.$('inbox-review-form'), 'submit');
    assert.equal(page.sent('POST', /\/approve$/).length, 0);
  },

  async an_approved_candidate_shows_its_note_and_who_decided() {
    const held = [candidate({status: 'approved', reviewer: 'amber', decided_at: Date.now() / 1000 - 30, slug: 'restore-drill',
      original: {title: 'Restore', body: 'Restore.', meta: {}}})];
    const page = await open(backend(REVIEWER(), {team: held}).serve);
    await page.go('#inbox');
    await page.choose(page.$('inbox-status'), 'approved');
    await openCandidate(page, 'Restore drill');
    assert.match(page.$('inbox-decided').textContent, /^Approved by amber on .+, saved as restore-drill\. It was edited before approval\.$/);
    assert.equal(page.$('inbox-lint-title').textContent, 'Checks when it was proposed');
    // The saved note opens from the inbox's store, not the one the header shows.
    const link = page.$('inbox-details').querySelector('.note-link');
    assert.equal(link.textContent, 'restore-drill');
    await page.click(link);
    assert.deepEqual(page.sent('POST', /^\/read$/)[0].body, {slug: 'restore-drill', profile: 'team', limit: 16000});
  },

  async a_refusal_is_shown_in_the_servers_words() {
    const held = [candidate({title: 'Decided elsewhere'})];
    const server = backend(WRITER(), {team: held});
    const page = await open(server.serve);
    await page.go('#inbox');
    await openCandidate(page, 'Decided elsewhere');
    const refusal = 'This store needs a second reviewer: you proposed this candidate.';
    server.intercept = request => request.path.endsWith('/approve') ? [403, {detail: refusal}] : undefined;
    type(page, 'inbox-field-title', 'Decided elsewhere, edited');
    await page.click(page.$('inbox-approve'));
    assert.equal(page.$('inbox-review-error').hidden, false);
    assert.equal(page.$('inbox-review-error').textContent, refusal);
    // The candidate stays open with what was typed, and can be decided again.
    assert.equal(page.hash(), '#inbox/team/' + held[0].id);
    assert.equal(page.$('inbox-field-title').value, 'Decided elsewhere, edited');
    assert.equal(page.$('inbox-approve').disabled, false);
    assert.equal(page.$('inbox-message').hidden, true);
    // Decided in another tab meanwhile: a 409 with its own sentence.
    server.intercept = null;
    held[0].status = 'rejected';
    await page.click(page.$('inbox-approve'));
    assert.equal(page.$('inbox-review-error').textContent, 'This candidate was already rejected');
    assert.equal(page.hash(), '#inbox/team/' + held[0].id);
    // The reject dialog shows its refusal inside the dialog and stays open.
    held[0].status = 'pending';
    server.intercept = request => request.path.endsWith('/reject') ? [403, {detail: refusal}] : undefined;
    await page.click(page.$('inbox-reject'));
    await page.click(page.$('inbox-reject-submit'));
    assert.equal(page.$('inbox-reject-dialog').open, true);
    assert.equal(page.$('inbox-reject-error').textContent, refusal);
    // So does the list, when review was taken away.
    server.intercept = request => request.path === '/inbox' ? [403, {detail: 'Reviewing the inbox needs review access to this store.'}] : undefined;
    await page.click(page.$('inbox-reject-dialog').querySelector('[data-close]'));
    await page.click(page.$('inbox-view').querySelector('.back-link'));
    assert.equal(page.$('inbox-error').hidden, false);
    assert.equal(page.$('inbox-error').textContent, 'Reviewing the inbox needs review access to this store.');
  },

  async a_second_submit_while_deciding_sends_nothing() {
    const held = [candidate()];
    const server = backend(REVIEWER(), {team: held});
    const page = await open(server.serve);
    await page.go('#inbox');
    await openCandidate(page, 'Restore drill');
    const slow = hold(server, request => request.path.endsWith('/approve'));
    await page.fire(page.$('inbox-review-form'), 'submit');
    for (const id of ['inbox-approve', 'inbox-reject', 'inbox-reject-submit']) assert.equal(page.$(id).disabled, true, id);
    await page.fire(page.$('inbox-review-form'), 'submit');
    await page.fire(page.$('inbox-reject-form'), 'submit');
    assert.equal(page.sent('POST', /^\/inbox\//).length, 1);
    slow.release(); await settle();
    assert.equal(page.sent('POST', /^\/inbox\//).length, 1);
    assert.equal(held[0].status, 'approved');
    for (const id of ['inbox-approve', 'inbox-reject', 'inbox-reject-submit']) assert.equal(page.$(id).disabled, false, id);
    assert.equal(page.hash(), '#inbox');
  },

  async a_candidate_that_arrives_after_leaving_it_is_not_shown() {
    const held = [candidate({title: 'Slow to load'}), candidate({title: 'Second'})];
    const server = backend(WRITER(), {team: held});
    const page = await open(server.serve);
    await page.go('#inbox');
    const slow = hold(server, request => request.path === '/inbox/' + held[0].id);
    await openCandidate(page, 'Slow to load');
    assert.equal(page.$('inbox-candidate-title').textContent, 'Loading…');
    await page.click(page.$('inbox-view').querySelector('.back-link'));
    assert.equal(page.$('inbox-list-view').hidden, false);
    slow.release(); await settle();
    // The list is what is shown: the late answer filled nothing in behind it.
    assert.equal(page.$('inbox-candidate').hidden, true);
    assert.equal(page.$('inbox-field-title').value, '');
    assert.equal(page.$('inbox-refresh').disabled, false);
    // Opening it again is a first opening: the heading takes the focus.
    await openCandidate(page, 'Slow to load');
    assert.equal(page.$('inbox-field-title').value, 'Slow to load');
    assert.equal(page.window.document.activeElement, page.$('inbox-candidate-title'));
  },

  async the_reject_dialog_never_decides_another_candidate() {
    const held = [candidate({title: 'Named in the dialog'}), candidate({title: 'Opened afterwards'})];
    const page = await open(backend(REVIEWER(), {team: held}).serve);
    await page.go('#inbox');
    await openCandidate(page, 'Named in the dialog');
    await page.click(page.$('inbox-reject'));
    assert.equal(page.$('inbox-reject-name').textContent, 'Named in the dialog');
    // The browser's history moves to another candidate while the dialog is open.
    await page.go('#inbox/team/' + held[1].id);
    assert.equal(page.$('inbox-candidate-title').textContent, 'Opened afterwards');
    assert.equal(page.$('inbox-reject-dialog').open, false);
    assert.deepEqual(page.sent('POST', /\/reject$/), []);
    // Asked again, the dialog names the candidate now shown, and rejects that one.
    await page.click(page.$('inbox-reject'));
    assert.equal(page.$('inbox-reject-name').textContent, 'Opened afterwards');
    await page.click(page.$('inbox-reject-submit'));
    assert.deepEqual(held.map(c => c.status), ['pending', 'rejected']);
  },

  async signing_out_empties_the_inbox() {
    const held = [candidate({title: 'Private candidate', body: 'Private text.'})];
    const server = backend(WRITER(), {team: held});
    const page = await open(server.serve);
    await page.go('#inbox');
    await openCandidate(page, 'Private candidate');
    assert.equal(page.$('inbox-field-body').value, 'Private text.');
    await page.click(page.$('logout'));
    assert.equal(page.$('tab-inbox').hidden, true);
    assert.equal(page.$('inbox-panel').hidden, true);
    assert.doesNotMatch(page.$('inbox-panel').textContent, /Private/);
    for (const id of ['inbox-field-title', 'inbox-field-body', 'inbox-field-tags']) assert.equal(page.$(id).value, '', id);
    assert.equal(page.$('inbox-reject-name').textContent, '');
  },

  async a_four_eyes_store_says_so_and_keeps_the_text_as_proposed() {
    // Write and review on both stores; only team is four-eyes.
    const held = {amber: [candidate({title: 'In amber'})],
      team: [candidate({title: 'In team', meta: {tags: ['backup'], description: 'How to restore.'}}), candidate({title: 'Also in team'})]};
    const server = backend(session([store('amber', 'write', true, 'open'), store('team', 'write', true, 'four-eyes')]), held);
    const page = await open(server.serve);
    await page.go('#inbox');
    const policy = page.$('inbox-policy'), text = ['inbox-field-title', 'inbox-field-description', 'inbox-field-body'];
    assert.equal(page.$('inbox-store').value, 'amber');
    assert.equal(policy.hidden, true);
    await page.choose(page.$('inbox-store'), 'team');
    assert.equal(policy.hidden, false);
    assert.equal(policy.textContent, 'In this store a candidate is decided by someone other than the account that proposed it, and only its tags and importance can be changed.');
    // The notice stays above the candidate, whose text is read-only although this reviewer holds write.
    await openCandidate(page, 'In team');
    assert.equal(policy.hidden, false);
    for (const id of text) {
      assert.equal(page.$(id).readOnly, true, id);
      assert.equal(page.$(id).getAttribute('aria-describedby'), 'inbox-text-policy', id);
    }
    assert.equal(page.$('inbox-text-policy').hidden, false);
    assert.match(page.$('inbox-text-policy').textContent, /as they were proposed\. You can still set the tags and importance; .+reject the candidate with a reason\.$/);
    // Not the sentence about write access, which would send this reviewer looking for a grant.
    assert.equal(page.$('inbox-text-help').hidden, true);
    assert.equal(page.$('inbox-field-tags').readOnly, false);
    assert.equal(page.$('inbox-field-importance').disabled, false);
    assert.equal(page.$('inbox-actions').hidden, false);
    // Tags and importance are sent; text changed behind the page's back is not.
    type(page, 'inbox-field-tags', 'backup, drill');
    type(page, 'inbox-field-importance', '4');
    type(page, 'inbox-field-body', 'Rewritten by a reviewer.');
    await page.click(page.$('inbox-approve'));
    assert.deepEqual(page.sent('POST', /\/approve$/)[0].body, {profile: 'team', edits: {tags: ['backup', 'drill'], importance: 4}});
    assert.equal(held.team[0].status, 'approved');
    assert.equal(page.hash(), '#inbox');
    assert.equal(policy.hidden, false);
    // Past the page the server refuses a change to the text, in its own words, and the candidate stays open.
    await openCandidate(page, 'Also in team');
    page.$('inbox-field-title').readOnly = false;
    type(page, 'inbox-field-title', 'Rewritten past the page');
    await page.click(page.$('inbox-approve'));
    assert.deepEqual(page.sent('POST', /\/approve$/)[1].body, {profile: 'team', edits: {title: 'Rewritten past the page'}});
    assert.equal(page.$('inbox-review-error').textContent, REFUSED.text);
    assert.equal(page.hash(), '#inbox/team/' + held.team[1].id);
    assert.equal(held.team[1].status, 'pending');
    // The other store is not four-eyes: no notice, and its text is this reviewer's to change.
    await page.click(page.$('inbox-view').querySelector('.back-link'));
    await page.choose(page.$('inbox-store'), 'amber');
    assert.equal(policy.hidden, true);
    await openCandidate(page, 'In amber');
    assert.equal(policy.hidden, true);
    for (const id of text) {
      assert.equal(page.$(id).readOnly, false, id);
      assert.equal(page.$(id).getAttribute('aria-describedby'), null, id);
    }
    assert.deepEqual([page.$('inbox-text-policy').hidden, page.$('inbox-text-help').hidden], [true, true]);
    // A link to a candidate in the four-eyes store brings the notice with it. Decided, it needs neither help.
    await page.go('#inbox/team/' + held.team[0].id);
    assert.equal(page.$('inbox-candidate-status').textContent, 'Approved');
    assert.equal(policy.hidden, false);
    assert.deepEqual([page.$('inbox-text-policy').hidden, page.$('inbox-text-help').hidden], [true, true]);
    for (const id of text) assert.equal(page.$(id).getAttribute('aria-describedby'), null, id);
    // The policy is the server's to change: the next identity moves the notice, and the fields with it.
    server.identity = session([store('amber', 'write', true, 'four-eyes'), store('team', 'write', true, 'agents')]);
    await page.window.memd.loadIdentity();
    await settle();
    assert.equal(policy.hidden, true);
    await page.go('#inbox/amber/' + held.amber[0].id);
    assert.equal(policy.hidden, false);
    assert.equal(page.$('inbox-field-body').readOnly, true);
    // A server that reports no policy holds nothing back here.
    server.identity = WRITER();
    await page.window.memd.loadIdentity();
    await settle();
    assert.equal(page.$('inbox-store').value, 'team');
    assert.equal(policy.hidden, true);
    // Review without write in a four-eyes store: the reason given is the store's rule, which write access would not lift.
    server.identity = session([store('team', 'read', true, 'four-eyes')]);
    await page.window.memd.loadIdentity();
    await settle();
    await page.go('#inbox/team/' + held.team[1].id);
    assert.equal(page.$('inbox-candidate-status').textContent, 'Waiting for review');
    for (const id of text) {
      assert.equal(page.$(id).readOnly, true, id);
      assert.equal(page.$(id).getAttribute('aria-describedby'), 'inbox-text-policy', id);
    }
    assert.deepEqual([page.$('inbox-text-policy').hidden, page.$('inbox-text-help').hidden], [false, true]);
  },

  async a_refused_self_decision_leaves_the_candidate_open() {
    // The first was proposed from this session's own account, by one of its agent tokens.
    const held = [candidate({title: 'Proposed by my agent', account: 'u-cobalt'}),
      candidate({title: 'Proposed by a colleague', account: 'u-amber', proposer: 'desk-agent'})];
    const server = backend(session([store('team', 'write', true, 'four-eyes')]), {team: held});
    const page = await open(server.serve);
    await page.go('#inbox');
    await openCandidate(page, 'Proposed by my agent');
    const shown = page.hash();
    type(page, 'inbox-field-tags', 'backup, drill');
    type(page, 'inbox-field-importance', '5');
    await page.click(page.$('inbox-approve'));
    const changes = {profile: 'team', edits: {tags: ['backup', 'drill'], importance: 5}};
    assert.deepEqual(page.sent('POST', /\/approve$/).map(request => request.body), [changes]);
    // The server's sentence, whole and alone, where the decision was asked for.
    assert.equal(page.$('inbox-review-error').hidden, false);
    assert.equal(page.$('inbox-review-error').textContent, REFUSED.own);
    assert.equal(page.$('inbox-review-error').getAttribute('role'), 'alert');
    assert.equal(page.$('inbox-error').hidden, true);
    assert.equal(page.$('inbox-message').hidden, true);
    // Nothing moved: the same candidate, still waiting, with what was typed and both decisions on offer.
    assert.equal(page.hash(), shown);
    assert.equal(page.$('inbox-view').hidden, false);
    assert.equal(page.$('inbox-candidate').hidden, false);
    assert.equal(page.$('inbox-candidate-status').textContent, 'Waiting for review');
    assert.equal(page.$('inbox-field-tags').value, 'backup, drill');
    assert.equal(page.$('inbox-field-importance').value, '5');
    assert.equal(page.$('inbox-actions').hidden, false);
    for (const id of ['inbox-approve', 'inbox-reject']) assert.equal(page.$(id).disabled, false, id);
    assert.equal(page.$('inbox-policy').hidden, false);
    assert.equal(held[0].status, 'pending');
    // Asked again, the same changes go with it: the refusal did not make them the candidate's own values.
    await page.click(page.$('inbox-approve'));
    assert.deepEqual(page.sent('POST', /\/approve$/).map(request => request.body), [changes, changes]);
    assert.equal(page.$('inbox-review-error').textContent, REFUSED.own);
    assert.equal(page.hash(), shown);
    // Rejecting it is refused the same way, inside the dialog that asked, which keeps the reason typed.
    await page.click(page.$('inbox-reject'));
    type(page, 'inbox-reject-reason', 'Not needed after all');
    await page.click(page.$('inbox-reject-submit'));
    assert.equal(page.sent('POST', /\/reject$/).length, 1);
    assert.equal(page.$('inbox-reject-dialog').open, true);
    assert.equal(page.$('inbox-reject-error').textContent, REFUSED.own);
    assert.equal(page.$('inbox-reject-reason').value, 'Not needed after all');
    assert.equal(held[0].status, 'pending');
    await page.click(page.$('inbox-reject-dialog').querySelector('[data-close]'));
    assert.equal(page.hash(), shown);
    assert.equal(page.$('inbox-candidate').hidden, false);
    // Back at the list it still waits, for someone else.
    await page.click(page.$('inbox-view').querySelector('.back-link'));
    assert.deepEqual(rows(page).map(row => row.querySelector('.entity-link').textContent), ['Proposed by my agent', 'Proposed by a colleague']);
    assert.match(page.$('inbox-status').textContent, /^Waiting for review \(2\)Approved \(0\)Rejected \(0\)$/);
    // A colleague's candidate is this reviewer's to decide, and the last refusal is not carried over to it.
    await openCandidate(page, 'Proposed by a colleague');
    assert.equal(page.$('inbox-review-error').hidden, true);
    await page.click(page.$('inbox-approve'));
    assert.equal(held[1].status, 'approved');
    assert.equal(page.hash(), '#inbox');
    assert.equal(page.$('inbox-message').textContent, 'Approved “Proposed by a colleague” and saved it as restore-drill.');
    assert.deepEqual(rows(page).map(row => row.querySelector('.entity-link').textContent), ['Proposed by my agent']);
  },

  async administration_sets_review_per_store_and_per_token() {
    const admin = session([store('amber', 'write', true), store('team', 'write', true)], {username: 'amber', user_id: 'u-amber', admin: true});
    const server = backend(admin);
    const stores = [{id: 'amber', name: 'amber', kind: 'local', config: {}}, {id: 'team', name: 'Team notes', kind: 'local', config: {}}];
    const token = fields => ({id: 't1', label: 'laptop-agent', masked: '••••abcd', username: 'cobalt', user_id: 'u-cobalt', store_id: 'team',
      scope: 'write', extra: {}, legacy: 0, last_used: null, expires: null, revoked: null, ...fields});
    server.overview = {ok: true, stores, jobs: [], audit: [],
      users: [{id: 'u-amber', username: 'amber', role: 'admin', disabled: 0, access: {amber: 'write', team: 'write'}, reviews: ['amber', 'team']},
        {id: 'u-cobalt', username: 'cobalt', role: 'member', disabled: 0, access: {amber: 'read', team: 'write'}, reviews: ['team']}],
      grants: [{user_id: 'u-cobalt', store_id: 'amber', permission: 'read', review: false}, {user_id: 'u-cobalt', store_id: 'team', permission: 'write', review: true}],
      tokens: [token({review: true}), token({id: 't2', label: 'reader', scope: 'read', review: false})]};
    const page = await open(server.serve);
    await page.go('#admin');
    const storeAccess = () => page.$('users-table').children[1].querySelectorAll('button').find(button => button.textContent === 'Store access');
    assert.match(page.$('users-table').children[1].textContent, /amber \(read\), team \(write, review\)/);
    assert.deepEqual(page.$('tokens-table').children.map(row => row.children[2].textContent), ['Read + write, review', 'Read only']);
    // Store access: the access choice plus "Can review", which needs access.
    await page.click(storeAccess());
    assert.equal(page.$('grants-dialog').open, true);
    const access = id => page.$('grant-fields').querySelector(`select[data-store="${id}"]`);
    const review = id => page.$('grant-fields').querySelector(`input[data-review="${id}"]`);
    assert.deepEqual([access('amber').value, review('amber').checked, review('amber').disabled], ['read', false, false]);
    assert.deepEqual([access('team').value, review('team').checked, review('team').disabled], ['write', true, false]);
    assert.equal(review('team').type, 'checkbox');
    assert.equal(review('team').getAttribute('aria-label'), 'Can review Team notes');
    review('amber').checked = true;                      // read and review
    review('team').checked = false;                     // write without review
    await page.click(page.$('grants-form').querySelector('button'));
    const [saved] = page.sent('PUT', /^\/admin\/users\/u-cobalt\/grants$/);
    assert.deepEqual(saved.body, {grants: {amber: {permission: 'read', review: true}, team: {permission: 'write', review: false}}});
    assert.equal(saved.headers['X-CSRF-Token'], 'csrf-of-this-session');
    assert.equal(page.$('grants-dialog').open, false);
    // No access: the box is off and disabled, and the store is left out.
    await page.click(storeAccess());
    await page.choose(access('amber'), '');
    assert.deepEqual([review('amber').checked, review('amber').disabled], [false, true]);
    await page.choose(access('team'), 'read');
    assert.deepEqual([review('team').checked, review('team').disabled], [false, false]);
    await page.click(page.$('grants-form').querySelector('button'));
    assert.deepEqual(page.sent('PUT', /grants$/)[1].body, {grants: {team: {permission: 'read', review: false}}});
    // Issue a token: "May review" is sent as review, and offered only where the owner reviews.
    await page.click(page.$('token-add'));
    type(page, 'token-name', 'review-agent');
    await page.choose(page.$('token-user'), 'u-cobalt');
    await page.choose(page.$('token-store'), 'amber');
    assert.equal(page.$('token-review').disabled, true);
    await page.choose(page.$('token-store'), 'team');
    assert.deepEqual([page.$('token-review').disabled, page.$('token-review').checked], [false, false]);
    page.$('token-review').checked = true;
    await page.click(page.$('token-form').querySelector('button'));
    const [issued] = page.sent('POST', /^\/admin\/tokens$/);
    assert.deepEqual([issued.body.label, issued.body.user_id, issued.body.store_id, issued.body.review], ['review-agent', 'u-cobalt', 'team', true]);
    await page.click(page.$('secret-dialog').querySelector('[data-close]'));
    await page.click(page.$('token-add'));
    type(page, 'token-name', 'plain-agent');
    await page.click(page.$('token-form').querySelector('button'));
    assert.equal(page.sent('POST', /^\/admin\/tokens$/)[1].body.review, false);
  },

  async administration_reads_an_overview_without_review_as_write() {
    const admin = session([store('amber', 'write'), store('team', 'write')], {username: 'amber', user_id: 'u-amber', admin: true});
    const server = backend(admin);
    server.overview = {ok: true, jobs: [], audit: [], tokens: [],
      stores: [{id: 'amber', name: 'amber', kind: 'local', config: {}}, {id: 'team', name: 'Team notes', kind: 'local', config: {}}],
      users: [{id: 'u-cobalt', username: 'cobalt', role: 'member', disabled: 0, access: {amber: 'read', team: 'write'}}],
      grants: [{user_id: 'u-cobalt', store_id: 'amber', permission: 'read'}, {user_id: 'u-cobalt', store_id: 'team', permission: 'write'}]};
    const page = await open(server.serve);
    await page.go('#admin');
    assert.match(page.$('users-table').children[0].textContent, /amber \(read\), team \(write, review\)/);
    await page.click(page.$('users-table').querySelectorAll('button').find(button => button.textContent === 'Store access'));
    const boxes = page.$('grant-fields').querySelectorAll('input');
    assert.deepEqual(boxes.map(box => [box.dataset.review, box.checked]), [['amber', false], ['team', true]]);
    await page.click(page.$('grants-dialog').querySelector('[data-close]'));
    // Without the owner's review stores the box stays available: the server decides.
    await page.click(page.$('token-add'));
    assert.equal(page.$('token-review').disabled, false);
  },

  async the_review_choice_round_trips_as_review_policy() {
    const server = backend(ADMIN());
    // The served store's entry is an older server's: it names no policy.
    server.overview = overview([listed('amber', 'existing'), listed('team', 'local', {review_policy: 'agents', reviewers: 3})]);
    const page = await open(server.serve);
    await page.go('#settings');
    // One choice of three, each option with its own line of help.
    assert.equal(page.$('store-review').tagName, 'fieldset');
    assert.equal(page.$('store-review').querySelector('legend').textContent, 'Review');
    assert.deepEqual(radios(page).map(radio => [radio.type, radio.getAttribute('name'), radio.value, radio.parent.textContent]),
      [['radio', 'review-policy', 'open', 'No review'], ['radio', 'review-policy', 'agents', 'Agent saves need review'],
        ['radio', 'review-policy', 'four-eyes', 'All saves need review by someone else']]);
    const help = radios(page).map(radio => page.$(radio.getAttribute('aria-describedby')).textContent);
    assert.match(help[0], /^Saves are written directly, unless the instance queues agent saves\.$/);
    assert.match(help[1], /^Saves from agent tokens wait in this store’s inbox\.$/);
    assert.match(help[2], /^Every save and publish waits\. .+ cannot approve or reject it, and reviewers cannot change its text\.$/);
    // The dialog opens on the store's effective policy.
    await configure(page, 'Team notes');
    assert.deepEqual(chosen(page), ['agents']);
    // Left as shown it is not sent: saving other settings never pins an environment default on the store.
    type(page, 'store-name', 'Team');
    await saveStore(page);
    const puts = () => page.sent('PUT', /^\/admin\/stores\/team$/);
    assert.equal(puts()[0].body.name, 'Team');
    assert.equal('review_policy' in puts()[0].body.config, false);
    // The box below it follows the same rule.
    assert.equal('publish_review' in puts()[0].body.config, false);
    await configure(page, 'Team');
    assert.deepEqual(chosen(page), ['agents']);
    await pick(page, 'four-eyes');
    await saveStore(page);
    assert.equal(puts()[1].body.config.review_policy, 'four-eyes');
    assert.equal(puts()[1].headers['X-CSRF-Token'], 'csrf-of-this-session');
    // What was saved is what the overview reports, and what the dialog shows next time.
    await configure(page, 'Team');
    assert.deepEqual(chosen(page), ['four-eyes']);
    await pick(page, 'open');
    await saveStore(page);
    assert.equal(puts()[2].body.config.review_policy, 'open');
    // Chosen and put back before saving: nothing to send.
    await configure(page, 'Team');
    assert.deepEqual(chosen(page), ['open']);
    await pick(page, 'agents');
    await pick(page, 'open');
    await saveStore(page);
    assert.equal('review_policy' in puts()[3].body.config, false);
    // A store whose entry names no policy is open.
    await configure(page, 'amber');
    assert.deepEqual(chosen(page), ['open']);
    // A choice abandoned in one dialog is not the next one's: a new store starts open,
    // and is created with what was chosen for it.
    await pick(page, 'four-eyes');
    await closeStore(page);
    await page.click(page.$('store-add'));
    assert.deepEqual(chosen(page), ['open']);
    type(page, 'store-id', 'shared');
    type(page, 'store-name', 'Shared');
    await pick(page, 'agents');
    await saveStore(page);
    const created = page.sent('POST', /^\/admin\/stores$/);
    assert.equal(created.length, 1);
    assert.deepEqual([created[0].body.id, created[0].body.kind, created[0].body.config.review_policy], ['shared', 'local', 'agents']);
    assert.equal(created[0].headers['X-CSRF-Token'], 'csrf-of-this-session');
    assert.equal(page.sent('PUT', /^\/admin\/stores\/amber$/).length, 0);
  },

  async four_eyes_warns_of_too_few_reviewers_and_of_what_is_not_reviewed() {
    const server = backend(ADMIN());
    const remote = {repo_ssh: 'https://git.example.com/team/notes.git'};
    server.overview = overview([listed('solo', 'local', {reviewers: 1}), listed('pair', 'local', {reviewers: 2}),
      listed('nobody', 'local', {reviewers: 0}), listed('older', 'local'), listed('repo', 'git', {reviewers: 3}, remote),
      listed('served', 'existing', {reviewers: 2}, remote),
      listed('vault', 'obsidian', {reviewers: 1, review_policy: 'four-eyes'}, {vault_path: '/vaults/team'})]);
    const page = await open(server.serve);
    await page.go('#settings');
    const few = page.$('store-review-few'), unreviewed = page.$('store-review-unreviewed');
    // [the warning about reviewers, whether the note about the repository or vault is shown]
    const notes = () => [few.hidden ? '' : few.textContent, !unreviewed.hidden];
    // Both sit in one live region, so they are announced when the choice brings them up.
    assert.equal(few.parent, unreviewed.parent);
    assert.equal(few.parent.getAttribute('role'), 'status');
    assert.equal(few.parent.hidden, false);
    assert.match(unreviewed.textContent, /^Notes that arrive through this store’s Git repository or Obsidian vault are not reviewed\./);
    // One reviewer: said only under four-eyes, and it does not stop the save.
    await configure(page, 'solo');
    assert.deepEqual(notes(), ['', false]);
    await pick(page, 'agents');
    assert.deepEqual(notes(), ['', false]);
    await pick(page, 'four-eyes');
    assert.match(notes()[0], /^Only one account can review this store, so nothing that account saves, .+ could be approved\. Give a second account review access under Administration\.$/);
    assert.equal(notes()[1], false);
    await pick(page, 'open');
    assert.deepEqual(notes(), ['', false]);
    await pick(page, 'four-eyes');
    await saveStore(page);
    assert.equal(page.sent('PUT', /^\/admin\/stores\/solo$/)[0].body.config.review_policy, 'four-eyes');
    // Two reviewers are enough, none at all is said as such, and a server that reports no count gets no warning.
    await configure(page, 'pair');
    await pick(page, 'four-eyes');
    assert.deepEqual(notes(), ['', false]);
    await closeStore(page);
    await configure(page, 'nobody');
    await pick(page, 'four-eyes');
    assert.match(notes()[0], /^No account can review this store, so nothing saved into it could be approved\./);
    await closeStore(page);
    await configure(page, 'older');
    await pick(page, 'four-eyes');
    assert.deepEqual(notes(), ['', false]);
    await closeStore(page);
    // A store kept in a Git repository: what arrives through the repository is not reviewed.
    await configure(page, 'repo');
    assert.deepEqual(notes(), ['', false]);
    await pick(page, 'four-eyes');
    assert.deepEqual(notes(), ['', true]);
    await pick(page, 'agents');
    assert.deepEqual(notes(), ['', false]);
    await closeStore(page);
    // The served store has a remote too, for as long as its address is filled in.
    await configure(page, 'served');
    await pick(page, 'four-eyes');
    assert.deepEqual(notes(), ['', true]);
    type(page, 'store-remote', '  ');
    await page.fire(page.$('store-remote'), 'input');
    assert.deepEqual(notes(), ['', false]);
    await closeStore(page);
    // A vault store that is already four-eyes, with one reviewer: both, as the dialog opens.
    await configure(page, 'vault');
    assert.deepEqual(chosen(page), ['four-eyes']);
    assert.match(notes()[0], /^Only one account can review this store/);
    assert.equal(notes()[1], true);
    // Typing an address for it leaves the warning alone: a live region whose sentence
    // is written again says it again, here at every key.
    const said = few.childNodes[0];
    type(page, 'store-remote', 'https://git.example.com/team/vault.git');
    await page.fire(page.$('store-remote'), 'input');
    assert.equal(few.childNodes[0] === said, true, 'the warning was written again');
    assert.equal(few.textContent, said.data);
    assert.equal(notes()[1], true);
    await closeStore(page);
    // A new store has no count yet, and the note follows the storage chosen for it.
    await page.click(page.$('store-add'));
    assert.deepEqual(notes(), ['', false]);
    await pick(page, 'four-eyes');
    assert.deepEqual(notes(), ['', false]);
    await page.choose(page.$('store-kind'), 'git');
    assert.deepEqual(notes(), ['', true]);
    await page.choose(page.$('store-kind'), 'local');
    assert.deepEqual(notes(), ['', false]);
    await page.choose(page.$('store-kind'), 'obsidian');
    assert.deepEqual(notes(), ['', true]);
  },

  async store_cards_name_a_policy_that_holds_saves_back() {
    const server = backend(ADMIN());
    server.overview = overview([listed('amber', 'existing', {review_policy: 'open'}), listed('older', 'local'),
      listed('team', 'local', {review_policy: 'four-eyes', reviewers: 2}), listed('drafts', 'local', {review_policy: 'agents'}),
      listed('later', 'local', {review_policy: 'a-policy-this-page-does-not-know'})]);
    const page = await open(server.serve);
    await page.go('#settings');
    for (const name of ['amber', 'older', 'later']) assert.deepEqual(chips(card(page, name)), [], name);
    // In the words of the dialog's own options, before the card's buttons.
    const label = value => radios(page).find(radio => radio.value === value).parent.textContent;
    assert.deepEqual(chips(card(page, 'Team notes')), ['All saves need review by someone else']);
    assert.deepEqual(chips(card(page, 'drafts')), ['Agent saves need review']);
    assert.deepEqual([label('four-eyes'), label('agents')], [...chips(card(page, 'Team notes')), ...chips(card(page, 'drafts'))]);
    const parts = card(page, 'Team notes').children;
    assert.equal(parts.indexOf(parts.find(node => chips(node).length)) < parts.indexOf(parts.find(node => node.className === 'action-row')), true);
    // The dialog of a store with a policy this page does not know shows it as open, and sends nothing for it.
    await configure(page, 'later');
    assert.deepEqual(chosen(page), ['open']);
    await saveStore(page);
    assert.equal('review_policy' in page.sent('PUT', /^\/admin\/stores\/later$/)[0].body.config, false);
    // A member's cards come from their own stores (GET /ui/me), which name the policy too.
    server.identity = session([store('amber', 'write', true), store('team', 'read', true, 'four-eyes'), store('drafts', 'write', false, 'agents')]);
    await page.window.memd.loadIdentity();
    await settle();
    assert.deepEqual(page.$('stores-list').children.map(node => [node.querySelector('h2').textContent, node.querySelector('p').textContent, chips(node)]),
      [['amber', 'Read and write access, can review the inbox', []],
        ['Team notes', 'Read-only access, can review the inbox', ['All saves need review by someone else']],
        ['drafts', 'Read and write access', ['Agent saves need review']]]);
  },

  async a_policy_saved_in_settings_reaches_the_inbox_without_a_reload() {
    // An administrator reviews the store they configure: it is in GET /admin/overview and in GET /ui/me.
    const held = [candidate({title: 'Waiting'})];
    const server = backend(ADMIN(), {amber: [], team: held});
    server.overview = overview([listed('amber', 'existing'), listed('team', 'local', {review_policy: 'open', reviewers: 2})]);
    const page = await open(server.serve);
    const policy = page.$('inbox-policy'), waiting = '#inbox/team/' + held[0].id;
    const shown = () => [policy.hidden, page.$('inbox-field-body').readOnly, page.$('inbox-text-policy').hidden];
    await page.go(waiting);
    assert.equal(page.$('inbox-candidate-title').textContent, 'Waiting');
    assert.deepEqual(shown(), [true, false, true]);
    const save = async value => {
      await page.go('#settings');
      await configure(page, 'Team notes');
      await pick(page, value);
      const before = page.requests.length;
      await saveStore(page);
      // The session's own stores are read again after the save, not only the overview.
      assert.deepEqual(page.requests.slice(before).filter(request => ['/admin/stores/team', '/ui/me'].includes(request.path))
        .map(request => request.method + ' ' + request.path), ['PUT /admin/stores/team', 'GET /ui/me']);
    };
    await save('four-eyes');
    await page.go(waiting);
    assert.equal(page.$('inbox-candidate-title').textContent, 'Waiting');
    assert.deepEqual(shown(), [false, true, false]);
    // The list of that store says so too.
    await page.click(page.$('inbox-view').querySelector('.back-link'));
    assert.equal(page.$('inbox-store').value, 'team');
    assert.equal(policy.hidden, false);
    // Back on "No review" the notice goes, and the text is this reviewer's to change again.
    await save('open');
    await page.go(waiting);
    assert.deepEqual(shown(), [true, false, true]);
  },

  async a_failed_check_on_a_node_is_reported_in_a_few_lines() {
    const page = await open(backend(REVIEWER(), {team: [candidate()]}).serve);
    await page.go('#inbox');
    await openCandidate(page, 'Restore drill');
    // What node:assert shows of a value when a check on it fails: its enumerable properties, here to a few levels.
    const shown = value => inspect(value, {customInspect: false, showHidden: false, getters: true, sorted: true, depth: 4, breakLength: Infinity});
    const title = page.$('inbox-candidate-title');
    assert.equal(page.window.document.activeElement === title, true);
    for (const node of [title, title.childNodes[0], page.$('inbox-panel'), page.$('inbox-field-importance')]) {
      const text = shown(node);
      // The node itself and nothing of the page around it: not its parent, its children or the requests made.
      assert.equal(text.length < 400 && !/parent|childNodes|page|listeners/.test(text), true, text.slice(0, 400));
    }
    assert.match(shown(title), /^Element \{ attrs: Map\(\d\) \{ 'id' => 'inbox-candidate-title'/);
    assert.equal(shown(title.childNodes[0]), "Text { data: 'Restore drill' }");
    // The links are still there for the page's scripts and for these scenarios.
    assert.equal(title.childNodes[0].parent === title && title.parent.parent.id === 'inbox-view' && title.page === page, true);
  },
};

const wanted = process.argv[2];
if (wanted === '--list') { console.log(Object.keys(SCENARIOS).join('\n')); process.exit(0); }
if (!SCENARIOS[wanted]) { console.error('Unknown scenario: ' + wanted); process.exit(2); }
SCENARIOS[wanted]().then(() => { console.log('PASS ' + wanted); process.exit(0); },
  error => { console.error(error); process.exit(1); });
