(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const {api, notice, element: el} = window.memd;
  let overview = null, editStore = null, editUser = null, loading = false, shownReview = true;
  const date = value => value ? new Date(value * 1000).toLocaleString() : "Never";
  const option = (value, label) => { const node = el("option", "", label); node.value = value; return node; };
  function button(label, action, className = "quiet") {
    const node = el("button", className, label); node.type = "button";
    node.addEventListener("click", async () => {
      node.disabled = true;
      try { await action(); } catch (error) { notice(location.hash === "#settings" ? "settings-error" : "admin-error", error.message); }
      finally { node.disabled = false; }
    }); return node;
  }
  async function submit(form, errorId, action) {
    const buttons = [...form.querySelectorAll("button")]; buttons.forEach(b => { b.disabled = true; });
    notice(errorId);
    try { await action(); } catch (error) { notice(errorId, error.message); }
    finally { buttons.forEach(b => { b.disabled = false; }); }
  }
  function reveal(title, description, value) {
    $("secret-title").textContent = title; $("secret-description").textContent = description;
    $("new-secret").textContent = value; $("secret-dialog").showModal();
  }
  document.querySelectorAll("[data-close]").forEach(b => b.addEventListener("click", () => {
    if (b.dataset.close === "secret-dialog") $("new-secret").textContent = "";
    $(b.dataset.close).close();
  }));
  $("secret-dialog").addEventListener("close", () => { $("new-secret").textContent = ""; $("secret-copy").textContent = "Copy"; });
  $("store-dialog").addEventListener("close", () => { $("store-secret").value = ""; });
  $("secret-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("new-secret").textContent); $("secret-copy").textContent = "Copied"; }
    catch { $("secret-copy").textContent = "Select to copy"; }
  });
  $("login-open").addEventListener("click", () => { notice("login-error"); $("login-dialog").showModal(); $("login-user").focus(); });
  $("login-dialog").addEventListener("close", () => { $("login-password").value = ""; });
  $("login-form").addEventListener("submit", event => {
    event.preventDefault(); submit(event.target, "login-error", async () => {
      await api("/ui/login", {username: $("login-user").value, password: $("login-password").value}, "");
      $("login-dialog").close(); await window.memd.loadIdentity(true);
    });
  });
  $("logout").addEventListener("click", async () => {
    try { await window.memd.signOut(); overview = null; clearPrivate(); }
    catch (error) { notice("session-notice", error.message); }
  });
  function clearPrivate() {
    ["tokens-table", "users-table", "audit-list", "jobs-list", "stores-list"].forEach(id => $(id).replaceChildren());
    document.querySelectorAll("dialog[open]").forEach(d => d.close());
    $("password-current").value = ""; $("password-new").value = "";
  }
  $("password-form").addEventListener("submit", event => {
    event.preventDefault(); submit(event.target, "settings-error", async () => {
      await api("/ui/password", {current: $("password-current").value, password: $("password-new").value});
      $("password-current").value = ""; $("password-new").value = "";
      overview = null; clearPrivate(); await window.memd.loadIdentity();
      notice("session-notice", "Password changed. Sign in again with your new password.");
    });
  });
  async function loadOverview() {
    if (loading || !window.memd.identity().admin) return;
    loading = true; notice("admin-error"); notice("settings-error");
    try { overview = await api("/admin/overview"); render(); }
    catch (error) { notice(location.hash === "#settings" ? "settings-error" : "admin-error", error.message); }
    finally { loading = false; }
  }
  function render() {
    $("tokens-table").replaceChildren(); $("users-table").replaceChildren(); $("stores-list").replaceChildren();
    const now = Date.now() / 1000;
    for (const token of overview.tokens) {
      const row = el("tr"); const name = el("td"); name.append(el("strong", "", token.label), el("small", "muted block", `${token.masked} · ${token.username || "Existing agent"}${token.legacy ? " · legacy" : ""}`));
      const state = token.revoked ? "Revoked" : token.expires && token.expires < now ? "Expired" : "Active";
      const status = el("td"); status.append(el("span", "chip", state));
      status.title = token.expires ? "Expires " + date(token.expires) : "No expiry";
      const action = el("td");
      if (!token.revoked) action.append(button("Revoke", async () => {
        if (!confirm(`Revoke “${token.label}”? Any agent using it will immediately lose access.`)) return;
        await api("/admin/tokens/" + token.id, undefined, undefined, "DELETE"); await loadOverview();
      }, "quiet danger"));
      row.append(name, el("td", "", token.store_id + Object.entries(token.extra || {}).map(([store, scope]) => ` + ${store} (${scope})`).join("")), el("td", "", token.scope === "write" ? "Read + write" : "Read only"), el("td", "small", date(token.last_used)), status, action);
      $("tokens-table").append(row);
    }
    if (!overview.tokens.length) { const row = el("tr"); const cell = el("td", "muted", "No tokens have been issued yet."); cell.colSpan = 6; row.append(cell); $("tokens-table").append(row); }
    for (const user of overview.users) {
      const row = el("tr"); const actions = el("td", "table-actions");
      actions.append(button("Store access", () => openGrants(user)), button("Reset password", async () => {
        if (!confirm(`Generate a temporary password for ${user.username} and end their browser sessions?`)) return;
        const data = await api(`/admin/users/${user.id}/reset-password`, {});
        reveal("Temporary password", `Share this privately with ${user.username}.`, data.temporary_password); await loadOverview();
      }));
      if (user.id !== window.memd.identity().user_id) {
        actions.append(button(user.disabled ? "Enable" : "Disable", async () => {
          if (!confirm(`${user.disabled ? "Enable" : "Disable"} ${user.username}? Disabling also blocks their sessions and tokens.`)) return;
          await api("/admin/users/" + user.id, {disabled: !user.disabled}, undefined, "PATCH"); await loadOverview();
        }));
        actions.append(button(user.role === "admin" ? "Make member" : "Make admin", async () => {
          if (!confirm(`Change ${user.username} to ${user.role === "admin" ? "member" : "administrator"}? Administrators can manage every store.`)) return;
          await api("/admin/users/" + user.id, {role: user.role === "admin" ? "member" : "admin"}, undefined, "PATCH"); await loadOverview();
        }));
      }
      const grants = overview.grants.filter(g => g.user_id === user.id).map(g => `${g.store_id} (${g.permission})`).join(", ");
      row.append(el("td", "", user.username), el("td", "", user.role), el("td", "", user.disabled ? "Disabled" : "Active"), el("td", "small", user.role === "admin" ? "All stores" : grants || "None"), actions);
      $("users-table").append(row);
    }
    for (const store of overview.stores) {
      const card = el("article", "panel store-card");
      card.append(el("span", "eyebrow", ({existing:"EXISTING STORE",local:"LOCAL MARKDOWN",git:"GIT REPOSITORY",obsidian:"OBSIDIAN VAULT"}[store.kind])), el("h2", "", store.name), el("p", "small muted", store.config.repo_ssh || store.config.vault_path || "Private Markdown files with Git history."));
      const actions = el("div", "action-row"); actions.append(button("Configure", () => openStore(store), "secondary"));
      for (const [action,label] of [["test","Test connection"],["sync","Sync now"],["reindex","Reindex"]]) actions.append(button(label, async () => {
        await api(`/admin/stores/${store.id}/${action}`, {}); await loadOverview();
      }));
      const download = el("a", "export-link", "Export notes ↓"); download.href = "/ui/export?profile=" + encodeURIComponent(store.id); download.download = store.id + "-memories.zip"; actions.append(download);
      if (store.kind === "obsidian") actions.append(button("Resolve conflicts", () => openConflicts(store)));
      card.append(actions); $("stores-list").append(card);
    }
    $("jobs-list").replaceChildren();
    for (const job of overview.jobs) {
      const row = el("div", "activity-row"); row.append(el("strong", "small", `${job.store_id} · ${job.action} · ${job.state}`), el("span", "small muted", job.detail || "Waiting for completion…"), el("time", "small muted", date(job.created))); $("jobs-list").append(row);
    }
    if (!overview.jobs.length) $("jobs-list").append(el("p", "small muted", "No connection or indexing operations yet."));
    $("audit-list").replaceChildren();
    for (const item of overview.audit) {
      const row = el("div", "activity-row"); row.append(el("span", "small", `${item.actor} · ${item.action} · ${item.target}`), el("time", "small muted", date(item.created))); $("audit-list").append(row);
    }
    for (const table of document.querySelectorAll("#admin-panel table")) {
      const labels = [...table.querySelectorAll("th")].map(th => th.textContent || "Actions");
      table.querySelectorAll("tbody tr").forEach(row => [...row.children].forEach((cell,index) => { cell.dataset.label = labels[index]; }));
    }
  }
  async function openConflicts(store) {
    notice("conflicts-error"); $("conflicts-list").replaceChildren();
    const data = await api(`/admin/stores/${store.id}/conflicts`);
    for (const conflict of data.conflicts) {
      const section = el("section", "panel"); section.append(el("h3", "", conflict.path));
      const copies = el("div", "form-columns");
      for (const [choice,title] of [["vault","Obsidian copy"],["memd","memd copy"]]) {
        const copy = el("div"); copy.append(el("h3", "", title), el("pre", "conflict-preview", conflict[choice + "_preview"]));
        copy.append(button("Keep " + title, async () => {
          if (!confirm(`Keep the ${title} for ${conflict.path}? Both current versions will be archived.`)) return;
          try {
            await api(`/admin/stores/${store.id}/resolve-conflict`, {...conflict, keep: choice});
            $("conflicts-dialog").close(); await openConflicts(store); await loadOverview();
          } catch (error) { notice("conflicts-error", error.message); }
        }, "secondary")); copies.append(copy);
      }
      section.append(copies); $("conflicts-list").append(section);
    }
    if (!data.conflicts.length) $("conflicts-list").append(el("p", "muted", "No pending vault conflicts."));
    $("conflicts-dialog").showModal();
  }
  function fields() {
    const kind = $("store-kind").value;
    $("git-fields").hidden = !["git", "existing", "obsidian"].includes(kind);
    $("vault-fields").hidden = kind !== "obsidian";
    $("crypt-fields").hidden = kind === "obsidian";
  }
  function openStore(store) {
    editStore = store; $("store-form").reset(); notice("store-error");
    $("store-title").textContent = store ? "Configure " + store.name : "Add memory store";
    $("store-id").value = store?.id || ""; $("store-id").disabled = !!store;
    $("store-name").value = store?.name || ""; $("store-kind").value = store?.kind || "local"; $("store-kind").disabled = !!store;
    const cfg = store?.config || {};
    for (const [id,key] of [["remote","repo_ssh"],["branch","branch"],["vault","vault_path"],["git-user","git_username"],["embed-url","embed_url"],["embed-model","embed_model"],["rerank-url","rerank_url"],["rerank-model","rerank_model"]]) $("store-" + id).value = cfg[key] || "";
    $("store-write-folder").value = cfg.write_folder || "Memories";
    $("store-key-file").value = cfg.key_file || "";
    $("store-include").value = (cfg.include || ["**/*.md"]).join("\n"); $("store-exclude").value = (cfg.exclude || []).join("\n");
    $("store-interval").value = cfg.sync_interval ?? 300;
    shownReview = store ? store.publish_review !== false : true; $("store-publish-review").checked = shownReview;
    fields(); $("store-dialog").showModal();
  }
  $("store-kind").addEventListener("change", fields);
  $("store-add").addEventListener("click", () => openStore(null));
  $("store-form").addEventListener("submit", event => {
    event.preventDefault(); submit(event.target, "store-error", async () => {
      const config = {};
      for (const [id,key] of [["remote","repo_ssh"],["branch","branch"],["vault","vault_path"],["git-user","git_username"],["embed-url","embed_url"],["embed-model","embed_model"],["rerank-url","rerank_url"],["rerank-model","rerank_model"]]) config[key] = $("store-" + id).value.trim();
      config.write_folder = $("store-write-folder").value.trim();
      if ($("store-kind").value !== "obsidian") config.key_file = $("store-key-file").value.trim();
      config.include = $("store-include").value.split("\n").map(x => x.trim()).filter(Boolean);
      config.exclude = $("store-exclude").value.split("\n").map(x => x.trim()).filter(Boolean);
      config.sync_interval = Number($("store-interval").value);
      if ($("store-secret").value) { config.secret = $("store-secret").value; config.credential_type = $("store-credential-type").value; }
      // Sent only when changed, so saving other settings never pins a store's
      // environment default (MEMD_<STORE>_PUBLISH_REVIEW) as its own setting.
      if ($("store-publish-review").checked !== shownReview) config.publish_review = $("store-publish-review").checked;
      const payload = {id: $("store-id").value, name: $("store-name").value, kind: $("store-kind").value, config};
      await api(editStore ? "/admin/stores/" + editStore.id : "/admin/stores", payload, undefined, editStore ? "PUT" : "POST");
      $("store-dialog").close(); await window.memd.loadIdentity(); await loadOverview();
    });
  });
  $("user-add").addEventListener("click", () => { $("user-form").reset(); notice("user-error"); $("user-dialog").showModal(); });
  $("user-form").addEventListener("submit", event => {
    event.preventDefault(); submit(event.target, "user-error", async () => {
      const username = $("user-name").value;
      const data = await api("/admin/users", {username, role: $("user-role").value});
      $("user-dialog").close(); reveal("Account created", `Username: ${username}. Share this password privately, then assign store access.`, data.temporary_password); await loadOverview();
    });
  });
  function openGrants(user) {
    editUser = user; $("grants-title").textContent = "Store access · " + user.username; notice("grants-error"); $("grant-fields").replaceChildren();
    if (user.role === "admin") $("grant-fields").append(el("p", "muted", "Administrators already have access to all stores. These grants apply if the account becomes a member."));
    for (const store of overview.stores) {
      const label = el("label", "grant-row", store.name); const select = el("select"); select.dataset.store = store.id;
      select.append(option("", "No access"), option("read", "Read only"), option("write", "Read and write"));
      select.value = overview.grants.find(g => g.user_id === user.id && g.store_id === store.id)?.permission || "";
      label.append(select); $("grant-fields").append(label);
    }
    $("grants-dialog").showModal();
  }
  $("grants-form").addEventListener("submit", event => {
    event.preventDefault(); submit(event.target, "grants-error", async () => {
      const grants = {}; $("grant-fields").querySelectorAll("select").forEach(s => { if (s.value) grants[s.dataset.store] = s.value; });
      await api(`/admin/users/${editUser.id}/grants`, {grants}, undefined, "PUT"); $("grants-dialog").close(); await loadOverview();
    });
  });
  // Additional stores offered for a token: the owner's store access as the
  // server reports it (overview users[].access), minus the token's main store.
  // Write is offered only where the owner has write; the server checks again.
  function extraStores() {
    const chosen = {}; $("token-extra").querySelectorAll("select").forEach(s => { if (s.value) chosen[s.dataset.store] = s.value; });
    $("token-extra").replaceChildren();
    const owner = overview.users.find(u => u.id === $("token-user").value);
    const granted = Object.entries(owner?.access || {}).filter(([id]) => id !== $("token-store").value);
    for (const [id, permission] of granted) {
      const name = overview.stores.find(s => s.id === id)?.name || id;
      const label = el("label", "grant-row"); const text = el("span", "", name);
      if (name !== id) text.append(el("small", "muted block", id));
      const select = el("select"); select.dataset.store = id;
      select.append(option("", "Not included"), option("read", "Read only"));
      if (permission === "write") select.append(option("write", "Read and write"));
      select.value = [...select.options].some(o => o.value === chosen[id]) ? chosen[id] : "";
      label.append(text, select); $("token-extra").append(label);
    }
    if (!granted.length) $("token-extra").append(el("p", "small muted", "The owner has no access to other stores."));
  }
  $("token-add").addEventListener("click", () => {
    $("token-form").reset(); notice("token-error"); $("token-user").replaceChildren(); $("token-store").replaceChildren(); $("token-extra").replaceChildren();
    overview.users.filter(u => !u.disabled).forEach(u => $("token-user").append(option(u.id, u.username)));
    overview.stores.forEach(s => $("token-store").append(option(s.id, s.name)));
    extraStores(); $("token-dialog").showModal();
  });
  for (const id of ["token-user", "token-store"]) $(id).addEventListener("change", extraStores);
  $("token-form").addEventListener("submit", event => {
    event.preventDefault(); submit(event.target, "token-error", async () => {
      const days = Number($("token-expiry").value);
      const extra = {}; $("token-extra").querySelectorAll("select").forEach(s => { if (s.value) extra[s.dataset.store] = s.value; });
      const result = await api("/admin/tokens", {label: $("token-name").value, user_id: $("token-user").value, store_id: $("token-store").value, scope: $("token-scope").value, expires: days ? Date.now() / 1000 + days * 86400 : null, extra_stores: extra});
      $("token-dialog").close(); reveal("Access token created", "Copy this token into your agent or use it with the onboarding installer.", result.token); await loadOverview();
    });
  });
  for (const id of ["admin-refresh", "jobs-refresh"]) $(id).addEventListener("click", loadOverview);
  addEventListener("memd-identity", event => {
    const identity = event.detail;
    $("account-name").textContent = identity.username ? "Signed in as " + identity.username : "";
    $("store-add").hidden = !identity.admin; $("jobs-panel").hidden = !identity.admin;
    if (!identity.session) { overview = null; clearPrivate(); }
    else if (!identity.admin) {
      $("stores-list").replaceChildren();
      for (const store of identity.stores || []) { const card = el("article", "panel"); card.append(el("h2", "", store.name), el("p", "muted", store.permission === "write" ? "Read and write access" : "Read-only access")); $("stores-list").append(card); }
    }
  });
  addEventListener("memd-tab", event => { if (["settings", "admin"].includes(event.detail)) loadOverview(); });
  setInterval(() => { if (!document.hidden && ["#settings", "#admin"].includes(location.hash)) loadOverview(); }, 5000);
})();
