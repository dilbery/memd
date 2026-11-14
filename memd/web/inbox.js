(() => {
  "use strict";
  // Review inbox for a signed-in account (GET /inbox, POST /inbox/{id}/approve and /reject).
  // A candidate is whatever an agent wrote: every value is set with textContent or a field's value.
  const $ = id => document.getElementById(id);
  const {api, notice, element: el, reviewPolicy} = window.memd;
  const PAGE = 20;
  const STATUS = {pending: "Waiting for review", approved: "Approved", rejected: "Rejected"};
  const FIELDS = {title: "inbox-field-title", description: "inbox-field-description", body: "inbox-field-body",
    tags: "inbox-field-tags", importance: "inbox-field-importance"};
  const TEXT = ["title", "description", "body"];
  const META = {host: "Host", source: "Source", observed_at: "Observed", verified_at: "Verified", volatility: "Volatility",
    pinned: "Pinned", verify: "Verification probes", supersedes: "Replaces", slug: "Requested slug",
    expected_revision: "Expected revision", conflict: "Conflict"};
  let stores = [], store = "", offset = 0, current = null, proposed = {}, deciding = false;
  let listGeneration = 0, pageGeneration = 0;
  const number = value => Number(value || 0).toLocaleString();
  const put = (id, text) => { $(id).textContent = text; };
  const plural = (n, one, many) => `${number(n)} ${n === 1 ? one : many}`;
  const when = seconds => seconds ? new Date(seconds * 1000).toLocaleString() : "an unknown date";
  const active = () => location.hash.slice(1).split("/")[0] === "inbox";
  const storeName = () => stores.find(s => s.id === store)?.name || store;
  // Under this policy the server refuses the proposer's own account and any change to the text.
  const fourEyes = () => reviewPolicy(stores.find(s => s.id === store)) === "four-eyes";
  const plain = value => typeof value === "string" ? value
    : Array.isArray(value) && value.every(item => typeof item === "string") ? value.join(", ") : JSON.stringify(value);

  function route() {
    const parts = location.hash.slice(1).split("/");
    if (parts[0] !== "inbox" || parts.length < 3) return null;
    try { return {store: decodeURIComponent(parts[1]), id: decodeURIComponent(parts[2])}; } catch { return null; }
  }
  const candidateHash = id => "#inbox/" + encodeURIComponent(store) + "/" + encodeURIComponent(id);
  function ago(seconds) {
    const minutes = Math.max(0, Math.round((Date.now() / 1000 - seconds) / 60));
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes} min ago`;
    if (minutes < 1440) return `${plural(Math.round(minutes / 60), "hour", "hours")} ago`;
    return `${plural(Math.round(minutes / 1440), "day", "days")} ago`;
  }
  function noteLink(slug, title) {
    const from = store, link = el("button", "note-link", title || slug); link.type = "button";
    link.title = "Read " + slug;
    link.addEventListener("click", () => window.memd.readNote(slug, false, from));
    return link;
  }
  function setStore(id) { store = id; $("inbox-store").value = id; offset = 0; $("inbox-policy").hidden = !fourEyes(); }
  function reset() {
    listGeneration++; pageGeneration++; current = null; proposed = {}; offset = 0; deciding = false;
    for (const id of ["inbox-list", "inbox-lint-items", "inbox-details"]) $(id).replaceChildren();
    for (const id of Object.values(FIELDS)) $(id).value = "";
    for (const id of ["inbox-meta", "inbox-candidate-title", "inbox-candidate-status", "inbox-candidate-meta",
      "inbox-decided", "inbox-lint-action", "inbox-reject-name"]) put(id, "");
    for (const id of ["inbox-error", "inbox-message", "inbox-review-error", "inbox-reject-error"]) notice(id);
    for (const option of $("inbox-status").options) option.textContent = STATUS[option.value];
    $("inbox-status").value = "pending"; $("inbox-reject-reason").value = "";
    if ($("inbox-reject-dialog").open) $("inbox-reject-dialog").close();
    $("inbox-candidate").hidden = true; $("inbox-decided").hidden = true; $("inbox-pagination").hidden = true;
  }

  // ------------------------------------------------------------------ list
  function flags(lint) {
    const out = [];
    if (lint.error) out.push(["bad", "Needs changes"]);
    if (lint.conflicts?.length) out.push(["bad", "Conflict"]);
    if (lint.near_duplicates?.length) out.push(["warn", "Near duplicate"]);
    if (lint.pending_duplicates?.length) out.push(["warn", "Pending duplicate"]);
    return out;
  }
  function row(c) {
    const li = el("li", "entity-row"), head = el("div", "entity-row-head");
    const link = el("a", "entity-link", c.title || "Untitled candidate"); link.href = candidateHash(c.id);
    head.append(link);
    for (const [kind, label] of flags(c.lint || {})) head.append(el("span", "badge " + kind, label));
    const decision = c.status === "pending" ? `proposed ${ago(c.created)}`
      : `${c.status} ${ago(c.decided_at || c.created)}${c.reviewer ? " by " + c.reviewer : ""}`;
    li.append(head, el("span", "small muted", [c.proposer || "Unnamed proposer", c.source, decision].filter(Boolean).join(" · ")));
    if (c.excerpt) li.append(el("span", "small", c.excerpt));
    return li;
  }
  function renderList(d) {
    const status = $("inbox-status").value, counts = d.counts || {};
    for (const option of $("inbox-status").options) option.textContent = `${STATUS[option.value]} (${number(counts[option.value])})`;
    const list = $("inbox-list"); list.replaceChildren(...d.items.map(row));
    if (!d.items.length) {
      const empty = el("li", "empty");
      empty.append(el("h3", "", status === "pending" ? "Nothing to review" : `No ${status} candidates`),
        el("p", "", status === "pending" ? "What agents propose for this store waits here until a reviewer decides."
          : "Decided candidates are listed here for a while after the decision."));
      list.append(empty);
    }
    put("inbox-meta", d.total ? `${number(offset + 1)}–${number(offset + d.items.length)} of ${plural(d.total, "candidate", "candidates")} · ${storeName()}` : storeName());
    $("inbox-pagination").hidden = d.total <= PAGE;
    put("inbox-page-label", `Page ${number(offset / PAGE + 1)} of ${number(Math.ceil(d.total / PAGE))}`);
    $("inbox-previous").disabled = offset === 0; $("inbox-next").disabled = offset + PAGE >= d.total;
  }
  async function loadList(next = offset) {
    const request = ++listGeneration;
    $("inbox-list").setAttribute("aria-busy", "true"); $("inbox-refresh").disabled = true;
    try {
      const query = new URLSearchParams({profile: store, status: $("inbox-status").value, offset: String(next), limit: String(PAGE)});
      const d = await api("/inbox?" + query, undefined, "");
      if (request !== listGeneration) return;
      // A decision can empty the page being shown: step back to the last one that exists.
      if (!d.items.length && d.total && next) { loadList(Math.floor((d.total - 1) / PAGE) * PAGE); return; }
      // The page moves only once it has loaded: after a failed load, Next must not skip one.
      offset = next; notice("inbox-error"); renderList(d);
    } catch (error) {
      if (request !== listGeneration) return;
      notice("inbox-error", error.detail || error.message);
    } finally {
      if (request === listGeneration) { $("inbox-list").setAttribute("aria-busy", "false"); $("inbox-refresh").disabled = false; }
    }
  }

  // ------------------------------------------------------------------ candidate
  function lintItem(label, ...content) {
    const li = el("li"), line = el("div", "contradiction");
    line.append(el("span", "chip", label), ...content); li.append(line);
    return li;
  }
  function renderLint(c, pending) {
    const lint = c.lint || {}, items = [];
    const slug = lint.slug || "a new unique slug";
    put("inbox-lint-title", pending ? "If approved" : "Checks when it was proposed");
    put("inbox-lint-action", (lint.action === "supersede" ? `Replaces ${lint.supersedes || "an existing memory"} with ${slug}.`
      : lint.action === "update" ? `Updates the existing memory ${slug}.`
      : lint.action === "create" ? `Creates a new memory as ${slug}.` : "No preview was recorded.")
      + (pending && lint.action ? " Checked when it was proposed." : ""));
    if (lint.error) items.push(lintItem("Needs changes", el("span", "", lint.error)));
    for (const conflict of lint.conflicts || []) {
      const item = lintItem(conflict.kind === "updates" ? "Updates" : "Contradicts", noteLink(conflict.slug, conflict.title));
      if (conflict.evidence) item.append(el("span", "small muted", conflict.evidence));
      items.push(item);
    }
    for (const near of lint.near_duplicates || []) {
      items.push(lintItem("Near duplicate", noteLink(near.slug),
        el("span", "muted", near.cosine ? `similarity ${Number(near.cosine).toFixed(2)}` : "keyword match")));
    }
    for (const entry of lint.pending_duplicates || []) {
      const other = typeof entry === "string" ? {id: entry} : entry || {};
      const link = el(other.id ? "a" : "span", other.id ? "entity-link" : "", other.title || other.id || "another candidate");
      if (other.id) link.href = candidateHash(other.id);
      items.push(lintItem("Also pending", link));
    }
    const listed = new Set([...(lint.conflicts || []), ...(lint.near_duplicates || [])].map(entry => entry.slug));
    const related = (lint.related || []).filter(slug => !listed.has(slug));
    if (related.length) items.push(lintItem("Related", ...related.map(slug => noteLink(slug))));
    for (const warning of lint.warnings || []) items.push(lintItem("Note", el("span", "", warning)));
    if (!items.length) items.push(el("li", "muted", "No duplicates, conflicts or warnings."));
    $("inbox-lint-items").replaceChildren(...items);
  }
  function renderDetails(c) {
    const rows = [["Proposed by", c.proposer || "Not recorded"], ["Channel", c.source || "Not recorded"],
      ["Proposed", when(c.created)], ["Store", storeName()]];
    for (const [key, value] of Object.entries(c.meta || {})) {
      if (!["tags", "importance", "description"].includes(key)) rows.push([META[key] || key, plain(value)]);
    }
    if (c.published_from) rows.push(["Published from", plain(c.published_from)]);
    if (c.status === "approved" && c.slug) rows.push(["Saved as", noteLink(c.slug)]);
    if (c.last_error) rows.push(["Last error", c.last_error]);
    rows.push(["Candidate", c.id]);
    const list = $("inbox-details"); list.replaceChildren();
    for (const [label, value] of rows) { const dd = el("dd"); dd.append(value); list.append(el("dt", "", label), dd); }
  }
  function renderCandidate() {
    const c = current, meta = c.meta || {}, pending = c.status === "pending";
    // Changing a candidate's text needs write on the store; tags and importance need only review.
    // Where the proposer may not decide, nobody rewrites what they proposed either.
    const kept = pending && fourEyes();
    const write = pending && !kept && stores.find(s => s.id === store)?.permission === "write";
    const help = !pending || write ? "" : kept ? "inbox-text-policy" : "inbox-text-help";
    put("inbox-candidate-title", c.title || "Untitled candidate"); put("inbox-candidate-status", STATUS[c.status] || c.status);
    put("inbox-candidate-meta", [`Proposed by ${c.proposer || "an unnamed proposer"}`, c.source ? `${c.source} channel` : "", ago(c.created), storeName()].filter(Boolean).join(" · "));
    const who = c.reviewer || "a reviewer";
    put("inbox-decided", c.status === "approved"
      ? `Approved by ${who} on ${when(c.decided_at)}${c.slug ? `, saved as ${c.slug}` : ""}.${c.original ? " It was edited before approval." : ""}`
      : c.status === "rejected" ? `Rejected by ${who} on ${when(c.decided_at)}. ${c.reason ? "Reason: " + c.reason : "No reason was given."}` : "");
    $("inbox-decided").hidden = pending;
    $(FIELDS.title).value = c.title || ""; $(FIELDS.description).value = meta.description || ""; $(FIELDS.body).value = c.body || "";
    $(FIELDS.tags).value = (meta.tags || []).join(", "); $(FIELDS.importance).value = String(meta.importance || 3);
    // What the fields show is the baseline for "changed": a browser may normalise line endings.
    proposed = Object.fromEntries(Object.entries(FIELDS).map(([key, id]) => [key, $(id).value]));
    for (const key of TEXT) {
      $(FIELDS[key]).readOnly = !write;
      // Described by the help only while it applies: a hidden element still describes.
      if (help) $(FIELDS[key]).setAttribute("aria-describedby", help);
      else $(FIELDS[key]).removeAttribute("aria-describedby");
    }
    $(FIELDS.tags).readOnly = !pending; $(FIELDS.importance).disabled = !pending;
    for (const id of ["inbox-text-help", "inbox-text-policy"]) $(id).hidden = id !== help;
    $("inbox-actions").hidden = !pending;
    notice("inbox-review-error");
    renderLint(c, pending); renderDetails(c);
    $("inbox-candidate").hidden = false;
  }
  async function loadCandidate(id) {
    const request = ++pageGeneration, first = current?.id !== id;
    $("inbox-view").setAttribute("aria-busy", "true"); $("inbox-refresh").disabled = true;
    if (first) {
      current = null; $("inbox-candidate").hidden = true; $("inbox-decided").hidden = true;
      put("inbox-candidate-title", "Loading…"); put("inbox-candidate-status", ""); put("inbox-candidate-meta", "");
    }
    try {
      const c = await api("/inbox/" + encodeURIComponent(id) + "?profile=" + encodeURIComponent(store), undefined, "");
      if (request !== pageGeneration) return;
      notice("inbox-error"); current = c; renderCandidate();
      if (first) $("inbox-candidate-title").focus();
    } catch (error) {
      if (request !== pageGeneration) return;
      current = null; $("inbox-candidate").hidden = true; put("inbox-candidate-title", "Candidate unavailable");
      notice("inbox-error", error.detail || error.message);
    } finally {
      if (request === pageGeneration) { $("inbox-view").setAttribute("aria-busy", "false"); $("inbox-refresh").disabled = false; }
    }
  }

  // ------------------------------------------------------------------ decisions
  function edits() {
    const out = {};
    for (const [key, id] of Object.entries(FIELDS)) {
      const value = $(id).value;
      if (value === proposed[key] || $(id).readOnly) continue;
      out[key] = key === "tags" ? value.split(",").map(tag => tag.trim()).filter(Boolean) : key === "importance" ? Number(value) : value;
    }
    return out;
  }
  async function decide(errorId, action) {
    // Enter in a field submits the form even when the candidate shown is already decided.
    if (!current || current.status !== "pending" || deciding) return;
    const c = current, buttons = ["inbox-approve", "inbox-reject", "inbox-reject-submit"].map($);
    deciding = true; notice(errorId); buttons.forEach(b => { b.disabled = true; });
    try {
      const message = await action(c, store);
      if ($("inbox-reject-dialog").open) $("inbox-reject-dialog").close();
      if (current === c) current = null;
      notice("inbox-message", message);
      // Back to the list, which reloads: the decided candidate has left this view.
      if (route()?.id === c.id) { location.hash = "#inbox"; $("inbox-title").focus(); }
      else if (active() && !route()) loadList();
    } catch (error) {
      // A refusal (403) or a candidate that is no longer pending (409): the server's own words.
      notice(errorId, error.detail || error.message);
    } finally { deciding = false; buttons.forEach(b => { b.disabled = false; }); }
  }
  $("inbox-review-form").addEventListener("submit", event => {
    event.preventDefault();
    decide("inbox-review-error", async (c, where) => {
      const changed = edits();
      const result = await api("/inbox/" + encodeURIComponent(c.id) + "/approve",
        Object.keys(changed).length ? {profile: where, edits: changed} : {profile: where}, "");
      // The receipt's warnings say what the save itself reported (a pending sync, a near-duplicate).
      const receipt = result.receipt || {};
      return [`Approved “${c.title}”${receipt.slug ? ` and saved it as ${receipt.slug}` : ""}.`,
        result.edited ? "Your changes were applied." : "", ...(receipt.warnings || [])].filter(Boolean).join(" ");
    });
  });
  $("inbox-reject").addEventListener("click", () => {
    if (!current) return;
    put("inbox-reject-name", current.title); $("inbox-reject-reason").value = ""; notice("inbox-reject-error");
    $("inbox-reject-dialog").showModal(); $("inbox-reject-reason").focus();
  });
  $("inbox-reject-form").addEventListener("submit", event => {
    event.preventDefault();
    decide("inbox-reject-error", async (c, where) => {
      const reason = $("inbox-reject-reason").value.trim();
      await api("/inbox/" + encodeURIComponent(c.id) + "/reject", reason ? {profile: where, reason} : {profile: where}, "");
      return `Rejected “${c.title}”. Nothing was saved.`;
    });
  });

  function show() {
    if (!active() || !stores.length) return;
    let target = route();
    if (target && target.store !== store) {
      if (stores.some(s => s.id === target.store)) setStore(target.store);
      else {
        history.replaceState(null, "", "#inbox"); target = null;
        notice("inbox-message", "That candidate is in a store you do not review.");
      }
    }
    $("inbox-list-view").hidden = !!target; $("inbox-view").hidden = !target;
    if (target) { notice("inbox-message"); loadCandidate(target.id); }
    // A candidate still on its way is not this view's any more: its answer is dropped.
    else { pageGeneration++; current = null; loadList(); }
  }
  $("inbox-refresh").addEventListener("click", show);
  $("inbox-store").addEventListener("change", () => {
    setStore($("inbox-store").value); notice("inbox-message"); $("inbox-list").replaceChildren(); loadList();
  });
  $("inbox-status").addEventListener("change", () => { offset = 0; loadList(); });
  $("inbox-previous").addEventListener("click", () => loadList(Math.max(0, offset - PAGE)));
  $("inbox-next").addEventListener("click", () => loadList(offset + PAGE));
  // The header's store is the one being worked in: follow it where this session reviews it.
  $("store-select").addEventListener("change", () => {
    const id = $("store-select").value;
    if (id === store || !stores.some(s => s.id === id)) return;
    setStore(id);
    if (!active()) return;
    if (route()) location.hash = "#inbox"; else loadList();
  });
  // The stores this session reviews; the picker starts on the header's store when that is one of them.
  function sync() {
    stores = window.memd.reviewStores();
    const picker = $("inbox-store"); picker.replaceChildren();
    for (const entry of stores) { const option = el("option", "", entry.name); option.value = entry.id; picker.append(option); }
    $("inbox-store-field").hidden = stores.length < 2;
    if (!stores.some(s => s.id === store)) store = (stores.find(s => s.id === window.memd.profile()) || stores[0])?.id || "";
    picker.value = store; $("inbox-policy").hidden = !fourEyes();
  }
  addEventListener("memd-identity", () => { reset(); sync(); });
  addEventListener("memd-tab", event => {
    // The reject dialog names one candidate, and a navigation (the browser's Back
    // works under a modal dialog) can show another: it would be the one rejected.
    if ($("inbox-reject-dialog").open) $("inbox-reject-dialog").close();
    if (event.detail === "inbox") show();
  });
  addEventListener("pagehide", reset);
  // The identity can arrive before this script runs; then its events were missed.
  sync(); show();
})();
