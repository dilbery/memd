(() => {
  "use strict";
  // Memory health view (GET /insights). Every note field is set with textContent.
  const $ = id => document.getElementById(id);
  const {api, notice, element: el} = window.memd;
  let report = null, generation = 0, busy = false;
  const number = value => Number(value || 0).toLocaleString();
  const active = () => location.hash === "#health";
  const days = n => n === 1 ? "1 day" : `${number(n)} days`;

  function noteLink(item) {
    const link = el("button", "note-link", item.title || item.slug); link.type = "button";
    link.title = "Read " + item.slug;
    link.addEventListener("click", () => window.memd.readNote(item.slug));
    return link;
  }
  function reset() {
    generation++; report = null;
    $("health-tiles").replaceChildren(); $("heat-body").replaceChildren(); $("health-lists").replaceChildren();
    put("health-meta", ""); put("heat-more", ""); notice("health-error");
    $("health-content").hidden = true;
  }
  function put(id, text) { $(id).textContent = text; }

  const TILES = [
    ["never_recalled", "Never recalled", s => s.available ? `of ${number(s.eligible)} notes older than the usage window` : "No usage data"],
    ["recalled_unread", "Shown, never read", s => s.available ? `recalled ${s.min_shown}+ times without a read` : "No usage data"],
    ["stale", "Stale", s => s.available ? "past their freshness window" : "No volatility declared"],
    ["failed_verifications", "Failed checks", s => s.available ? `mem-verify, last ${s.days} days` : "No verify probes"],
    ["contradictions", "Contradictions", s => s.available ? "facts that disagree across notes" : "No facts extracted"],
    ["supersede_candidates", "Supersede candidates", s => s.available ? "every fact replaced by newer notes" : "No facts extracted"],
    ["stale_summaries", "Stale summaries", s => s.available ? `of ${number(s.summaries)} summaries` : "No summaries"],
  ];
  function tile(label, value, detail) {
    const box = el("article", "metric");
    box.append(el("span", "metric-label", label), el("strong", "", value), el("span", "muted small", detail));
    return box;
  }
  function renderTiles(d) {
    const tiles = [];
    for (const [key, label, detail] of TILES) {
      const s = d[key];
      tiles.push(tile(label, s.available ? number(s.total) : "—", detail(s)));
    }
    const inbox = d.inbox, cov = d.coverage;
    tiles.push(tile("Awaiting review", inbox.available ? number(inbox.pending) : "—", inbox.available ? "candidates in the inbox" : "No proposals yet"));
    const forget = d.forget;
    if (forget) tiles.push(tile("Archived", number(forget.archived), forget.available ? `${number(forget.pending)} more proposed on ${forget.branch}` : forget.reason));
    tiles.push(tile("Index pending", number(cov.pending), cov.pending ? `${number(cov.pending_vectors)} note vectors, ${number(cov.pending_chunks)} chunk sets` : `All ${number(cov.notes)} notes embedded`));
    $("health-tiles").replaceChildren(...tiles);
  }

  function heatLevel(value, max) { return value ? Math.max(1, Math.ceil(5 * value / max)) : 0; }
  function renderHeat() {
    const heat = report.heatmap, target = $("heat-body"); target.replaceChildren(); put("heat-more", "");
    if (!heat.available) { target.append(el("p", "small muted", heat.reason)); return; }
    const group = $("heat-group").value, data = heat[group];
    const table = el("table", "heatmap");
    table.append(el("caption", "sr-only", `Live notes by ${group === "tags" ? "tag" : "host"} and age of their as-of date, with stale counts`));
    const head = el("tr");
    const corner = el("th", "", group === "tags" ? "Tag" : "Host"); corner.scope = "col"; head.append(corner);
    for (const bucket of heat.buckets) { const th = el("th", "num", bucket); th.scope = "col"; head.append(th); }
    for (const label of ["Notes", "Stale"]) { const th = el("th", "num", label); th.scope = "col"; head.append(th); }
    const thead = el("thead"); thead.append(head); table.append(thead);
    const max = Math.max(1, ...data.rows.flatMap(row => row.cells));
    const tbody = el("tbody");
    for (const row of data.rows) {
      const tr = el("tr"); const th = el("th", "heat-key", row.key); th.scope = "row"; th.title = row.key; tr.append(th);
      row.cells.forEach((value, i) => {
        const td = el("td", "num heat-" + heatLevel(value, max), number(value));
        td.setAttribute("aria-label", `${row.key}, ${heat.buckets[i]}: ${value} notes`); tr.append(td);
      });
      tr.append(el("td", "num", number(row.notes)), el("td", "num" + (row.stale ? " heat-stale" : ""), number(row.stale)));
      tbody.append(tr);
    }
    table.append(tbody); target.append(table);
    if (!data.rows.length) target.replaceChildren(el("p", "small muted", "No notes to show."));
    if (data.total_rows > data.rows.length) put("heat-more", `Showing the ${data.rows.length} ${group === "tags" ? "tags" : "hosts"} with the most stale notes of ${number(data.total_rows)}.`);
  }

  const LISTS = [
    ["stale", "Stale notes", "Changeable state past its freshness window, or a failed re-check. Verify before relying on them.",
      i => [i.reason, i.as_of ? `as of ${i.as_of}` : "", i.host]],
    ["contradictions", "Contradictions", "One subject with different current values in different notes. Supersede the wrong one.", null],
    ["failed_verifications", "Failed verifications", "The note’s declared probe failed on its last check.",
      i => [`checked ${i.checked_at}`, i.probe]],
    ["supersede_candidates", "Supersede candidates", "Every fact in these notes was replaced by newer notes.",
      i => [`${i.facts} facts closed by ${i.closed_by.join(", ")}`]],
    ["stale_summaries", "Stale summaries", "A source changed or was retired since the summary was written.",
      i => [...i.reasons, i.more_reasons ? `${i.more_reasons} more` : ""]],
    ["recalled_unread", "Shown, never read", "Recall keeps returning these but no agent reads them. Sharpen or retire.",
      i => [`shown ${number(i.shown)} times`, i.host]],
    ["never_recalled", "Never recalled", "No recall returned these in the usage window. Candidates to merge or retire.",
      i => [`importance ${i.importance ?? "—"}`, i.host]],
    ["most_useful", "Most useful", "Read most often after a recall.",
      i => [`read ${number(i.reads)} times`, i.read_rate == null ? "" : `${Math.round(i.read_rate * 100)}% of ${number(i.shown)} showings`]],
  ];
  function listPanel(key, title, description, meta) {
    const s = report[key];
    const panel = el("section", "panel health-list"); panel.setAttribute("aria-label", title);
    const heading = el("div", "section-heading");
    const text = el("div"); text.append(el("h2", "", title), el("p", "small muted", description));
    heading.append(text, el("span", "badge" + (s.available && s.total ? " warn" : ""), s.available ? number(s.total) : "No data"));
    panel.append(heading);
    if (!s.available) { panel.append(el("p", "small muted", s.reason)); return panel; }
    if (!s.items.length) { panel.append(el("p", "small muted", "Nothing found.")); return panel; }
    const ul = el("ul", "health-items");
    for (const item of s.items) {
      const li = el("li");
      if (key === "contradictions") {
        li.append(el("strong", "", `${item.subject} · ${item.predicate}`));
        for (const value of item.values) {
          const line = el("div", "contradiction"); line.append(el("span", "chip", value.object));
          value.notes.forEach(note => line.append(noteLink(note)));
          li.append(line);
        }
        if (item.more_values) li.append(el("span", "small muted", `${item.more_values} more values`));
      } else {
        li.append(noteLink(item));
        const parts = meta(item).filter(Boolean);
        if (parts.length) li.append(el("span", "small muted", parts.join(" · ")));
      }
      ul.append(li);
    }
    panel.append(ul);
    if (s.total > s.items.length) panel.append(el("p", "small muted", `Showing ${s.items.length} of ${number(s.total)}. mem-health --limit lists more.`));
    return panel;
  }
  function render() {
    const d = report; $("health-content").hidden = false;
    const u = d.usage;
    put("health-meta", [
      `${number(d.coverage.notes)} live notes`,
      u.available ? `usage window ${days(Math.round(u.window_days))} (${number(u.recalls)} recalls, ${number(u.reads)} reads)` : `usage: ${u.reason}`,
      `computed ${new Date(d.generated_at).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}${d.cached ? ` · cached ${Math.round(d.age_s)} s` : ""}`,
    ].join(" · "));
    renderTiles(d); renderHeat();
    $("health-lists").replaceChildren(...LISTS.map(args => listPanel(...args)));
  }

  async function load(fresh = false) {
    if (!window.memd.hasAccess()) { reset(); $("health-locked").hidden = false; return; }
    if (busy && !fresh) return;
    $("health-locked").hidden = true;
    const request = ++generation; busy = true;
    $("health-refresh").disabled = true; $("health-content").setAttribute("aria-busy", "true");
    if (!report) put("health-meta", "Computing memory health…");
    $("health-content").hidden = false;
    const query = new URLSearchParams(), profile = window.memd.profile();
    if (profile !== "PROFILE") query.set("profile", profile);
    if (fresh) query.set("fresh", "true");
    try {
      const search = String(query);
      const d = await api("/insights" + (search ? "?" + search : ""));
      if (request !== generation) return;
      notice("health-error"); report = d; render();
    } catch (error) {
      if (request !== generation) return;
      notice("health-error", error.message); if (!report) $("health-content").hidden = true;
    } finally {
      if (request === generation) { busy = false; $("health-refresh").disabled = false; $("health-content").setAttribute("aria-busy", "false"); }
    }
  }

  $("health-refresh").addEventListener("click", () => load(true));
  $("heat-group").addEventListener("change", () => { if (report) renderHeat(); });
  $("store-select").addEventListener("change", () => { reset(); busy = false; if (active()) load(); });
  addEventListener("memd-tab", event => { if (event.detail === "health") load(); });
  addEventListener("memd-identity", () => {
    if (!window.memd.hasAccess()) { reset(); busy = false; $("health-locked").hidden = false; }
    else if (active()) load();
  });
  addEventListener("pagehide", reset);
})();
