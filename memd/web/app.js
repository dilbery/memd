(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  let token = "", profile = "PROFILE", offset = 0, generation = 0, readerGeneration = 0;
  let continuation = null, statsBusy = false, statsGeneration = 0;
  let identity = {enabled: false, authenticated: false};
  const hasAccess = () => !!token || (!!identity.session && !!identity.stores?.length);
  const pageSize = 24;
  const number = value => Number(value || 0).toLocaleString();
  const put = (id, value) => { $(id).textContent = value; };
  function notice(id, message = "") { put(id, message); $(id).hidden = !message; }
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  async function api(path, body, credential = token, method = body === undefined ? "GET" : "POST") {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const headers = {};
      if (credential) headers.Authorization = "Bearer " + credential;
      if (identity.csrf) headers["X-CSRF-Token"] = identity.csrf;
      if (body !== undefined) headers["Content-Type"] = "application/json";
      const response = await fetch(path, {method, headers,
        body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal,
        cache: "no-store", credentials: credential ? "omit" : "same-origin"});
      if (response.status === 401) throw new Error("Access token not accepted. Unlock with a current token.");
      if (response.status === 403) throw new Error("This token or profile cannot access this memory store.");
      let data;
      try { data = await response.json(); } catch { throw new Error("The server returned an unexpected response. Try again shortly."); }
      if (!response.ok || data.ok === false) throw new Error(data.detail || data.err || data.error || `Request failed (${response.status}).`);
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("The request timed out. Check the service and try again.");
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function tab() {
    const requested = location.hash.slice(1).split("/")[0];
    const allowed = ["memory", "health", "entities", "onboarding", ...(identity.session ? ["settings"] : []), ...(identity.admin ? ["admin"] : [])];
    const selected = allowed.includes(requested) ? requested : "memory";
    ["memory", "health", "entities", "onboarding", "settings", "admin"].forEach(name => {
      $(name + "-panel").hidden = name !== selected;
      if (name === selected) $("tab-" + name).setAttribute("aria-current", "page");
      else $("tab-" + name).removeAttribute("aria-current");
    });
    document.title = "memd · " + ({memory:"Memory index",health:"Memory health",entities:"Entities",onboarding:"Onboarding",settings:"Settings",admin:"Administration"}[selected]);
    dispatchEvent(new CustomEvent("memd-tab", {detail: selected}));
  }

  function commands() {
    const label = $("machine").value;
    const safeLabel = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(label) ? label : "MACHINE_LABEL";
    const safeProfile = /^[A-Za-z0-9_-]+$/.test(profile) ? profile : "PROFILE";
    const base = location.origin;
    put("issue-command", `./memd-token issue ${safeProfile} ${safeLabel}`);
    put("manage-command", `./memd-token list ${safeProfile}\n./memd-token revoke ${safeProfile} ${safeLabel}`);
    put("onboard-command", `curl -fsS '${base}/clients/onboard.sh' -o onboard.sh\nMEMD_SERVER='${base}' bash onboard.sh`);
    put("endpoint", base + "/mcp/");
  }
  function bars(id, entries, key) {
    const target = $(id); target.replaceChildren();
    const sorted = [...entries].sort((a, b) => key === "importance" ? b[key] - a[key] : b.count - a.count);
    const max = Math.max(1, ...sorted.map(x => x.count));
    for (const entry of sorted) {
      const row = element("div", "bar-row");
      const label = element("span", "", String(entry[key] ?? "Unspecified")); label.title = label.textContent;
      const bar = element("progress"); bar.max = max; bar.value = entry.count;
      bar.setAttribute("aria-label", `${label.textContent}: ${entry.count} notes`);
      row.append(label, bar, element("span", "", number(entry.count))); target.append(row);
    }
    if (!sorted.length) target.append(element("p", "small muted", "No indexed notes yet."));
  }
  async function refreshStatus(force = false) {
    if (identity.enabled && !hasAccess()) {
      put("profile", "Memory store"); put("health-badge", "Sign in to explore");
      put("health-summary", "Use an account or an agent token to access your memories.");
      return;
    }
    if (statsBusy && !force) return;
    const request = ++statsGeneration;
    statsBusy = true; $("refresh").disabled = true;
    const results = await Promise.allSettled([api("/stats" + (profile !== "PROFILE" ? "?profile=" + encodeURIComponent(profile) : "")), api("/health" + (identity.authenticated && profile !== "PROFILE" ? "?profile=" + encodeURIComponent(profile) : ""))]);
    if (request !== statsGeneration) return;
    const errors = [];
    if (results[0].status === "fulfilled") {
      const d = results[0].value; profile = d.profile; put("profile", profile + " / memory store"); commands();
      put("notes", number(d.notes - d.superseded)); put("vectors", number(d.vec));
      put("keywords", number(d.fts)); put("priority", number(d.core));
      put("notes-detail", `${number(d.notes)} total · ${number(d.superseded)} superseded`);
      put("vector-detail", d.pending_vectors ? `${number(d.pending_vectors)} awaiting embeddings` : "All embeddings up to date");
      put("index-size", `${(d.size / 1048576).toFixed(1)} MiB database`);
      put("revision", `Index revision ${(d.lexical_head || d.head || "unknown").slice(0, 12)}${d.refresh?.running ? " · Refresh in progress" : ""}`);
      bars("hosts", d.hosts || [], "host"); bars("importance", d.importance || [], "importance");
    } else {
      errors.push("Index: " + results[0].reason.message);
      ["notes", "vectors", "keywords", "priority"].forEach(id => put(id, "—"));
      ["notes-detail", "vector-detail", "index-size", "revision"].forEach(id => put(id, "Status unavailable"));
      $("hosts").replaceChildren(); $("importance").replaceChildren();
    }
    const badge = $("health-badge"); $("checks").replaceChildren();
    if (results[1].status === "fulfilled") {
      const h = results[1].value;
      const healthy = h.status === "ok";
      badge.className = "badge " + (healthy ? "good" : "warn");
      badge.textContent = healthy ? "Healthy" : "Degraded";
      put("health-summary", healthy ? "Index and services are in sync" : "Recall is available with reduced service health");
      const labels = {index: "Memory index", git: "Repository sync", embed: "Embeddings", rerank: "Reranking"};
      for (const [name, check] of Object.entries(h.checks || {})) {
        const row = element("div", "check-row");
        const ok = check.ok && (name !== "git" || check.in_sync);
        row.append(element("span", "", labels[name] || name), element("span", "", ok ? "Ready" : "Needs attention"));
        row.title = check.detail || ""; $("checks").append(row);
      }
    } else {
      errors.push("Service: " + results[1].reason.message); badge.className = "badge bad";
      badge.textContent = "Unavailable"; put("health-summary", "Could not verify service health");
    }
    notice("status-error", errors.join(" "));
    put("updated", `${errors.length ? "Checked" : "Updated"} ${new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})} · auto-refresh 30s`);
    statsBusy = false; $("refresh").disabled = false;
  }
  function busy(value) {
    $("results").setAttribute("aria-busy", String(value));
    ["search-btn", "browse", "previous", "next"].forEach(id => { $(id).disabled = value || !hasAccess(); });
    put("search-btn", value ? "Loading…" : "Search memories");
  }
  function empty(title, message) {
    const box = element("div", "empty"); box.append(element("h3", "", title), element("p", "", message));
    $("results").replaceChildren(box);
  }
  let activeTag = "";
  function renderNotes(notes) {
    $("results").replaceChildren();
    if (!notes.length) { empty("No memories found", "Try another phrase, or browse all indexed notes."); return; }
    for (const note of notes) {
      const card = element("button", "note-card"); card.type = "button";
      card.append(element("h3", "", note.title || note.slug), element("p", "", (note.body || "").slice(0, 240)));
      const meta = element("div", "note-meta");
      meta.append(element("span", "chip", note.host || "any"), element("span", "", `Importance ${note.importance ?? "—"}`));
      for (const name of (note.tags || []).slice(0, 6)) {
        // The card is itself a button and buttons cannot nest, so the chip is
        // a focusable role=button that answers Enter and Space like one.
        const chip = element("span", "chip chip-tag", name);
        chip.setAttribute("role", "button"); chip.tabIndex = 0;
        chip.setAttribute("aria-label", `Show memories tagged ${name}`);
        const filter = event => { event.preventDefault(); event.stopPropagation(); activeTag = name; browse(0); };
        chip.addEventListener("click", filter);
        chip.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") filter(event); });
        meta.append(chip);
      }
      meta.append(element("span", "note-arrow", "Read memory ↗"));
      card.append(meta); card.addEventListener("click", () => readNote(note.slug)); $("results").append(card);
    }
  }
  async function browse(nextOffset = 0) {
    if (!hasAccess()) return;
    const request = ++generation; busy(true); notice("search-error"); $("pagination").hidden = true;
    try {
      const d = await api(`/ui/notes?offset=${nextOffset}&limit=${pageSize}`
        + (profile !== "PROFILE" ? "&profile=" + encodeURIComponent(profile) : "")
        + (activeTag ? "&tag=" + encodeURIComponent(activeTag) : ""));
      if (request !== generation) return;
      offset = nextOffset; renderNotes(d.notes);
      if (activeTag) {
        const clear = element("button", "chip chip-active", `\u00d7 ${activeTag}`);
        clear.type = "button"; clear.setAttribute("aria-label", `Clear the ${activeTag} tag filter`);
        clear.addEventListener("click", () => { activeTag = ""; browse(0); });
        $("results").prepend(clear);
      }
      put("result-count", activeTag
        ? `${number(d.total)} memories tagged \u201c${activeTag}\u201d`
        : `${number(d.total)} indexed memories`);
      put("page-label", d.total ? `${offset + 1}–${offset + d.notes.length} of ${number(d.total)}` : "No notes yet");
      $("pagination").hidden = d.total <= pageSize;
      busy(false); $("previous").disabled = offset === 0; $("next").disabled = offset + pageSize >= d.total;
    } catch (error) {
      if (request !== generation) return;
      notice("search-error", error.message); empty("Could not load memories", "Check your access token and try Browse all again."); put("result-count", ""); busy(false);
    }
  }
  async function search(event) {
    event.preventDefault(); const query = $("query").value.trim();
    if (!hasAccess()) return;
    if (!query) { browse(); return; }
    const request = ++generation; busy(true); notice("search-error"); $("pagination").hidden = true;
    try {
      const d = await api("/recall", {query, profile: profile === "PROFILE" ? undefined : profile, k: 12, include_core: false});
      if (request !== generation) return;
      renderNotes(d.notes || []); put("result-count", `${(d.notes || []).length} matches for “${query}”`);
    } catch (error) {
      if (request !== generation) return;
      notice("search-error", error.message); empty("Search couldn’t complete", "Try again, or browse your indexed notes."); put("result-count", "");
    } finally { if (request === generation) busy(false); }
  }
  async function readNote(slug, more = false) {
    const request = ++readerGeneration;
    if (!more) { continuation = null; put("reader-title", "Loading memory…"); put("reader-meta", ""); put("reader-body", ""); $("reader").showModal(); }
    notice("reader-error"); $("reader-more").hidden = true;
    try {
      const d = await api("/read", more ? continuation : {slug, profile: profile === "PROFILE" ? undefined : profile, limit: 16000});
      if (request !== readerGeneration) return;
      put("reader-title", d.title || d.slug);
      put("reader-meta", [d.profile, d.verified_at ? "Verified " + d.verified_at : null, "Revision " + (d.revision || "unknown").slice(0, 12)].filter(Boolean).join(" · "));
      put("reader-body", (more ? $("reader-body").textContent : "") + (d.body || ""));
      continuation = d.continuation; $("reader-more").hidden = !continuation;
    } catch (error) {
      if (request !== readerGeneration) return;
      notice("reader-error", error.message); if (!more) put("reader-title", "Memory unavailable");
      $("reader-more").hidden = !continuation;
    }
  }
  function lock() {
    token = ""; generation++; readerGeneration++; continuation = null;
    $("reader").close(); put("reader-title", ""); put("reader-meta", ""); put("reader-body", ""); notice("reader-error");
    $("unlock-form").hidden = false; $("lock").hidden = true; $("query").disabled = true; $("query").value = "";
    $("token").value = ""; $("pagination").hidden = true; put("result-count", ""); notice("search-error"); busy(false);
    empty("Your notes are locked", "Unlock with an access token to browse and search this memory store.");
  }
  $("unlock-form").addEventListener("submit", async event => {
    event.preventDefault(); notice("auth-error"); const candidate = $("token").value.trim();
    $("unlock-btn").disabled = true;
    try {
      await api("/ui/notes?limit=1", undefined, candidate);
      token = candidate; $("token").value = ""; $("unlock-form").hidden = true; $("lock").hidden = false;
      $("query").disabled = false; await loadIdentity(); await browse(); $("query").focus();
    } catch (error) { notice("auth-error", error.message); }
    finally { $("unlock-btn").disabled = false; }
  });
  $("lock").addEventListener("click", () => { lock(); loadIdentity(); });
  $("search-form").addEventListener("submit", search);
  $("browse").addEventListener("click", () => { $("query").value = ""; browse(); });
  $("previous").addEventListener("click", () => browse(Math.max(0, offset - pageSize)));
  $("next").addEventListener("click", () => browse(offset + pageSize));
  $("reader-close").addEventListener("click", () => $("reader").close());
  $("reader").addEventListener("close", () => { readerGeneration++; });
  $("reader-more").addEventListener("click", () => readNote(null, true));
  $("refresh").addEventListener("click", refreshStatus);
  $("machine").addEventListener("input", commands);
  document.querySelectorAll("[data-copy]").forEach(button => button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($(button.dataset.copy).textContent);
      button.textContent = "Copied"; put("copy-status", "Copied to clipboard.");
      setTimeout(() => { button.textContent = "Copy"; }, 1800);
    } catch { put("copy-status", "Clipboard unavailable. Select and copy the command above."); }
  }));
  addEventListener("hashchange", tab);
  addEventListener("pagehide", () => { token = ""; });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshStatus(); });
  async function loadIdentity(preferSession = false) {
    if (preferSession) token = "";
    try { identity = await api("/ui/me"); } catch { identity = {enabled:false,authenticated:false}; }
    $("legacy-issue").hidden = !!identity.enabled; $("legacy-manage").hidden = !!identity.enabled;
    $("issue-admin-link").hidden = !identity.admin;
    $("login-open").hidden = !identity.enabled || identity.session;
    $("logout").hidden = !identity.session;
    $("tab-settings").hidden = !identity.session;
    $("tab-admin").hidden = !identity.admin;
    $("lock").hidden = !token;
    const stores = identity.stores || [];
    $("store-select").replaceChildren();
    stores.forEach(store => { const option = element("option", "", store.name); option.value = store.id; $("store-select").append(option); });
    $("store-select").hidden = stores.length < 2;
    if (stores.length && !stores.some(s => s.id === profile)) profile = identity.default_store || stores[0].id;
    $("store-select").value = profile;
    $("unlock-form").hidden = hasAccess(); $("query").disabled = !hasAccess(); busy(false);
    notice("session-notice", identity.session && !identity.stores?.length ? "Your account has no memory stores assigned yet. Ask an administrator to grant access." : identity.enabled && !identity.authenticated ? "Sign in for your memory stores and settings, or use an access token below." : "");
    dispatchEvent(new CustomEvent("memd-identity", {detail: identity}));
    tab(); commands(); await refreshStatus(true);
    if (hasAccess()) await browse();
    return identity;
  }
  $("store-select").addEventListener("change", () => {
    profile = $("store-select").value; generation++; readerGeneration++; activeTag = "";
    $("reader").close(); put("reader-body", ""); $("query").value = "";
    empty("Loading store…", ""); notice("search-error"); refreshStatus(true); browse(); commands();
  });
  window.memd = {api, notice, element, loadIdentity, identity: () => identity,
    readNote, hasAccess, profile: () => profile,
    signOut: async () => { await api("/ui/logout", {}); identity = {enabled:true,authenticated:false}; lock();
      ["notes", "vectors", "keywords", "priority"].forEach(id => put(id, "—"));
      ["hosts", "importance", "checks"].forEach(id => $(id).replaceChildren());
      profile = "PROFILE"; await loadIdentity(); }};
  tab(); commands(); loadIdentity();
  setInterval(() => { if (!document.hidden) refreshStatus(); }, 30000);
})();
