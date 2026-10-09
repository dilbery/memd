(() => {
  "use strict";
  // Entities view (GET /entities, GET /entities/{kind}/{name}). Every value is set with textContent.
  const $ = id => document.getElementById(id);
  const {api, notice, element: el} = window.memd;
  const KINDS = {host: "Host", service: "Service", tag: "Tag"};
  const WHY = {scoped: "scoped to it", fact: "states a fact", tag: "tagged", mention: "mentions it"};
  let listing = null, listGeneration = 0, pageGeneration = 0, shown = "", listBusy = false, pageBusy = "";
  const number = value => Number(value || 0).toLocaleString();
  const put = (id, text) => { $(id).textContent = text; };
  const plural = (n, one, many) => `${number(n)} ${n === 1 ? one : many}`;

  function route() {
    const parts = location.hash.slice(1).split("/");
    if (parts[0] !== "entities" || parts.length < 3) return null;
    try {
      return {kind: decodeURIComponent(parts[1]), name: decodeURIComponent(parts.slice(2).join("/"))};
    } catch { return null; }
  }
  function entityHash(kind, name) { return "#entities/" + encodeURIComponent(kind) + "/" + encodeURIComponent(name); }
  function storeQuery(fresh) {
    const query = new URLSearchParams(), profile = window.memd.profile();
    if (profile !== "PROFILE") query.set("profile", profile);
    if (fresh) query.set("fresh", "true");
    const search = String(query);
    return search ? "?" + search : "";
  }
  function noteLink(slug, title) {
    const link = el("button", "note-link", title || slug); link.type = "button";
    link.title = "Read " + slug;
    link.addEventListener("click", () => window.memd.readNote(slug));
    return link;
  }
  function entityLink(item) {
    const link = el("a", "entity-link", item.display || item.name);
    link.href = entityHash(item.kind, item.name);
    return link;
  }
  function reset() {
    listGeneration++; pageGeneration++; listing = null; shown = ""; listBusy = false; pageBusy = "";
    $("entities-list").replaceChildren(); $("entity-sections").replaceChildren(); $("entity-tiles").replaceChildren();
    put("entities-meta", ""); put("entity-meta", ""); notice("entities-error");
    $("entities-list-view").hidden = true; $("entity-view").hidden = true;
  }

  // ------------------------------------------------------------------ list
  function summaryLine(e) {
    return [plural(e.notes, "note", "notes"), plural(e.current_facts, "current fact", "current facts"),
      e.stale ? `${number(e.stale)} stale` : "", e.failed_verification ? plural(e.failed_verification, "failed check", "failed checks") : "",
      e.last_activity ? `last ${e.last_activity}` : ""].filter(Boolean).join(" · ");
  }
  function renderList() {
    const target = $("entities-list"); target.replaceChildren();
    if (!listing) return;
    const query = $("entities-query").value.trim().toLocaleLowerCase(), kind = $("entities-kind").value;
    const items = listing.entities.filter(e => (!kind || e.kind === kind) &&
      (!query || e.name.toLocaleLowerCase().includes(query) || (e.display || "").toLocaleLowerCase().includes(query)));
    for (const e of items) {
      const li = el("li", "entity-row");
      const head = el("div", "entity-row-head");
      head.append(entityLink(e), el("span", "chip", KINDS[e.kind] || e.kind));
      if (e.failed_verification) head.append(el("span", "badge bad", "Failed check"));
      else if (e.stale) head.append(el("span", "badge warn", "Stale notes"));
      li.append(head, el("span", "small muted", summaryLine(e)));
      target.append(li);
    }
    if (!items.length) {
      const empty = el("li", "empty");
      empty.append(el("h3", "", listing.entities.length ? "No entity matches" : "No entities yet"),
        el("p", "", listing.entities.length ? "Try another name or kind." : "Entities come from note hosts, extracted facts (mem-facts) and tags used on several notes."));
      target.append(empty);
    }
    const parts = [`${number(items.length)} of ${number(listing.entities.length)} shown`];
    if (listing.truncated) parts.push(`the ${number(listing.limit)} strongest of ${number(listing.total)} entities`);
    parts.push(`${number(listing.notes)} live notes`);
    if (listing.cached_at) parts.push(`computed ${listing.cached_at}`);
    put("entities-meta", parts.join(" · "));
  }
  async function loadList(fresh = false) {
    if (listBusy && !fresh) return;
    const request = ++listGeneration; listBusy = true;
    $("entities-list").setAttribute("aria-busy", "true"); $("entities-refresh").disabled = true;
    if (!listing) put("entities-meta", "Finding entities…");
    try {
      const d = await api("/entities" + storeQuery(fresh));
      if (request !== listGeneration) return;
      notice("entities-error");
      listing = {...d, cached_at: new Date(d.generated_at).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})};
      renderList();
    } catch (error) {
      if (request !== listGeneration) return;
      notice("entities-error", error.message); if (!listing) put("entities-meta", "");
    } finally {
      if (request === listGeneration) { listBusy = false; $("entities-list").setAttribute("aria-busy", "false"); $("entities-refresh").disabled = false; }
    }
  }

  // ------------------------------------------------------------------ detail
  function tile(label, value, detail) {
    const box = el("article", "metric");
    box.append(el("span", "metric-label", label), el("strong", "", value), el("span", "muted small", detail));
    return box;
  }
  function section(title, description, count, fill, warn = false, emptyText = "Nothing recorded.") {
    const panel = el("section", "panel health-list"); panel.setAttribute("aria-label", title);
    const heading = el("div", "section-heading");
    const text = el("div"); text.append(el("h2", "", title), el("p", "small muted", description));
    heading.append(text, el("span", "badge" + (warn && count ? " warn" : ""), number(count)));
    panel.append(heading);
    const list = el("ul", "health-items");
    fill(list);
    panel.append(list.children.length ? list : el("p", "small muted", emptyText));
    return panel;
  }
  function more(panel, part) {
    if (part.total > part.items.length) panel.append(el("p", "small muted", `Showing ${part.items.length} of ${number(part.total)}.`));
    return panel;
  }
  function verificationText(v) {
    if (!v) return "";
    if (v.status === "failed") return `verification failed ${v.checked_at}: ${v.probe}`;
    if (v.status === "verified") return `verified ${v.verified_at}`;
    return "probe declared, not yet checked";
  }
  function renderPage(d) {
    const e = d.entity;
    put("entity-title", e.display || e.name); put("entity-kind", KINDS[e.kind] || e.kind);
    put("entity-meta", [e.display !== e.name ? `key ${e.name}` : "", e.last_activity ? `last activity ${e.last_activity}` : "no dated activity",
      `computed ${new Date(d.generated_at).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}`].filter(Boolean).join(" · "));
    const inbox = d.inbox;
    $("entity-tiles").replaceChildren(
      tile("Notes", number(e.notes), Object.entries(e.reasons).filter(([, n]) => n).map(([k, n]) => `${number(n)} ${WHY[k]}`).join(", ") || "none"),
      tile("Current facts", number(e.current_facts), `${plural(d.timeline.total, "fact", "facts")} in its history`),
      tile("Stale", number(e.stale), "notes past their freshness window"),
      tile("Failed checks", number(e.failed_verification), "mem-verify, last 30 days"),
      tile("Awaiting review", inbox.available ? number(inbox.pending) : "—", inbox.available ? "inbox candidates mentioning it" : "No proposals yet"));

    const panels = [];
    panels.push(more(section("Current facts", "What memory says is true now, and which note says so.", d.current_facts.total, ul => {
      for (const f of d.current_facts.items) {
        const li = el("li"); li.append(el("strong", "", `${f.subject} · ${f.predicate} · ${f.object}`));
        const meta = el("span", "small muted fact-source", `since ${f.since || "undated"} · from `); meta.append(noteLink(f.source, f.source_title));
        li.append(meta); ul.append(li);
      }
    }), d.current_facts));
    panels.push(more(section("Notes", "Notes scoped to it, tagged with it, stating facts about it or mentioning it. Stale and failing notes first.", d.notes.total, ul => {
      for (const n of d.notes.items) {
        const li = el("li"); li.append(noteLink(n.slug, n.title));
        const flags = el("div", "entity-flags");
        if (n.stale) flags.append(el("span", "badge warn", n.stale_reason || "stale"));
        if (n.verification?.status === "failed") flags.append(el("span", "badge bad", "check failed"));
        if (flags.children.length) li.append(flags);
        li.append(el("span", "small muted", [n.date ? `as of ${n.date}` : "undated", verificationText(n.verification),
          n.why.map(w => WHY[w] || w).join(", ")].filter(Boolean).join(" · ")));
        ul.append(li);
      }
    }, d.notes.items.some(n => n.stale)), d.notes));
    panels.push(more(section("Timeline", "Every fact about it, newest first; a closed fact was replaced by a later one.", d.timeline.total, ul => {
      for (const f of d.timeline.items) {
        const li = el("li"); const line = el("div", "contradiction");
        line.append(el("span", "chip", `${f.valid_from || "undated"} → ${f.current ? "now" : (f.valid_to || "open")}`),
          el("span", "", `${f.subject} · ${f.predicate} · ${f.object}`));
        li.append(line);
        const meta = el("span", "small muted fact-source", (f.closed_by ? `closed by ${f.closed_by} · ` : "") + (f.note_changed ? "note edited since · " : "") + "from ");
        meta.append(noteLink(f.source, f.source_title)); li.append(meta); ul.append(li);
      }
    }), d.timeline));
    panels.push(section("Conflicts", "One subject and predicate with different current values in different notes. Supersede the wrong one.", d.conflicts.total, ul => {
      for (const c of d.conflicts.items) {
        const li = el("li"); li.append(el("strong", "", `${c.subject} · ${c.predicate}`));
        for (const value of c.values) {
          const line = el("div", "contradiction"); line.append(el("span", "chip", value.object));
          value.notes.forEach(note => line.append(noteLink(note.slug, note.title))); li.append(line);
        }
        if (c.more_values) li.append(el("span", "small muted", `${c.more_values} more values`));
        ul.append(li);
      }
    }, true));
    panels.push(more(section("Related", "Entities that share notes with it or appear in its facts.", d.related.total, ul => {
      for (const r of d.related.items) {
        const li = el("li"); const line = el("div", "contradiction");
        line.append(entityLink(r), el("span", "chip", KINDS[r.kind] || r.kind));
        li.append(line, el("span", "small muted", [r.shared_notes ? plural(r.shared_notes, "shared note", "shared notes") : "", r.fact_link ? "linked by a fact" : ""].filter(Boolean).join(" · ")));
        ul.append(li);
      }
    }), d.related));
    panels.push(section("Review inbox", "Pending candidates that mention it.", inbox.pending, ul => {
      for (const c of inbox.items || []) {
        const li = el("li"); li.append(el("strong", "", c.title));
        li.append(el("span", "small muted", [c.source, c.created ? `proposed ${c.created.slice(0, 10)}` : ""].filter(Boolean).join(" · "))); ul.append(li);
      }
    }, false, !inbox.can_review && inbox.pending
      ? `${plural(inbox.pending, "candidate waits", "candidates wait")} for review. Sign in with write access to this store to see them.`
      : "Nothing waiting."));
    $("entity-sections").replaceChildren(...panels);
  }
  async function loadPage(target, fresh = false) {
    const key = target.kind + "/" + target.name;
    if (pageBusy === key && !fresh) return;
    const request = ++pageGeneration, first = shown !== key; shown = key; pageBusy = key;
    $("entity-view").setAttribute("aria-busy", "true"); $("entities-refresh").disabled = true;
    if (first) {
      put("entity-title", "Loading…"); put("entity-kind", KINDS[target.kind] || ""); put("entity-meta", "");
      $("entity-tiles").replaceChildren(); $("entity-sections").replaceChildren();
    }
    try {
      const d = await api("/entities/" + encodeURIComponent(target.kind) + "/" + encodeURIComponent(target.name) + storeQuery(fresh));
      if (request !== pageGeneration) return;
      notice("entities-error"); renderPage(d);
      if (first) $("entity-title").focus();
    } catch (error) {
      if (request !== pageGeneration) return;
      shown = ""; put("entity-title", "Entity unavailable"); put("entity-meta", "");
      notice("entities-error", /Request failed \(404\)|No such entity/.test(error.message) ? "No such entity in this store. It may have been renamed or retired." : error.message);
    } finally {
      if (request === pageGeneration) { pageBusy = ""; $("entity-view").setAttribute("aria-busy", "false"); $("entities-refresh").disabled = false; }
    }
  }

  function show(fresh = false) {
    if (!location.hash.startsWith("#entities")) return;
    if (!window.memd.hasAccess()) { reset(); $("entities-locked").hidden = false; return; }
    $("entities-locked").hidden = true; notice("entities-error");
    const target = route();
    $("entities-list-view").hidden = !!target; $("entity-view").hidden = !target;
    if (target) { loadPage(target, fresh); return; }
    shown = "";
    if (!listing || fresh) loadList(fresh); else renderList();
  }

  $("entities-filter").addEventListener("submit", event => event.preventDefault());
  $("entities-query").addEventListener("input", renderList);
  $("entities-kind").addEventListener("change", renderList);
  $("entities-refresh").addEventListener("click", () => { if (route()) show(true); else { listing = null; show(true); } });
  $("store-select").addEventListener("change", () => { reset(); show(); });
  addEventListener("memd-tab", event => { if (event.detail === "entities") show(); });
  addEventListener("memd-identity", () => { if (!window.memd.hasAccess()) { reset(); $("entities-locked").hidden = false; } else show(); });
  addEventListener("pagehide", reset);
})();
