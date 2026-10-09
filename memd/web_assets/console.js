"use strict";
const $ = (s) => document.querySelector(s);
const csrf = $('meta[name="csrf-token"]').content;
const ownerList=document.createElement("datalist");
ownerList.id="directory-owners";document.body.append(ownerList);
const ownerInput=$('[name="owner"]');ownerInput.setAttribute("list",ownerList.id);
ownerInput.placeholder="Search a person by name or email, or enter a service owner";
const ownerHint=document.createElement("p");ownerHint.className="muted";ownerInput.parentElement.after(ownerHint);
let tokens = [], events = [], page = 0, editing = null, action = "create", operationId = null;
const size = 15;
const date = (value) => value ? new Date(value * 1000).toLocaleString(undefined, {dateStyle:"medium", timeStyle:"short"}) : "—";
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function message(text, error = false) {
  const box = $("#message");
  box.className = `notice ${error ? "error" : "success"}`;
  box.textContent = text;
  box.hidden = false;
}
async function api(path, body) {
  const options = {credentials:"same-origin", cache:"no-store", headers:{}};
  if (body !== undefined) {
    options.method = "POST";
    options.headers = {"Content-Type":"application/json", "X-CSRF-Token":csrf};
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401) {
      message("Your session needs a fresh sign-in. Changes have not been retried.", true);
      const link = element("a", "Sign in again", "button");
      link.href = "/auth/login";
      $("#message").append(" ", link);
    }
    throw new Error(typeof data.detail === "string" ? data.detail : "The request could not be completed.");
  }
  return data;
}
function button(label, handler, className = "quiet") {
  const node = element("button", label, className);
  node.type = "button";
  node.addEventListener("click", handler);
  return node;
}
function renderTokens() {
  const now = Date.now()/1000;
  // Recompute expiry even when the list was fetched before the boundary.
  tokens.forEach(t => {t.status = t.revoked !== null ? "revoked" : t.expires && t.expires <= now ? "expired" : "active";});
  $("#count-active").textContent = tokens.filter(t => t.status === "active").length;
  $("#count-expiring").textContent = tokens.filter(t => t.status === "active" && t.expires && t.expires < now+14*86400).length;
  $("#count-legacy").textContent = tokens.filter(t => t.legacy && t.status === "active").length;
  const search = $("#search").value.toLowerCase(), status = $("#status").value;
  const selected = tokens.filter(t => [t.label,t.owner,t.id,t.purpose,...t.stores].join(" ").toLowerCase().includes(search)
    && (status === "all" || (status === "legacy" ? t.legacy : t.status === status)));
  selected.sort((a,b) => $("#sort").value === "label" ? a.label.localeCompare(b.label) : $("#sort").value === "expiry" ? (a.expires || Infinity)-(b.expires || Infinity) : b.created-a.created);
  const pages = Math.max(1,Math.ceil(selected.length/size));
  page = Math.min(page,pages-1);
  const rows = $("#token-rows"); rows.replaceChildren();
  selected.slice(page*size,(page+1)*size).forEach(t => {
    const row = element("tr"), name = element("td");
    name.append(button(t.label, () => details(t), "token-link"), element("small",t.owner));
    const access = element("td");
    access.append(element("span", t.legacy ? "Existing unrestricted access" : t.operations.join(" · ")),
      element("small", t.legacy ? "Migration required" : t.stores.join(", ")));
    const state = element("td"); state.append(element("span",t.status,`badge ${t.status}`));
    if(t.legacy) state.append(element("span","Legacy","badge legacy"));
    const controls = element("td"), group = element("div",undefined,"row-actions");
    group.append(button("Edit",()=>openEditor("edit",t)));
    if(t.status === "active") {
      if(!t.legacy) group.append(button("Rotate",()=>openEditor("rotate",t)));
      group.append(button("Revoke",()=>openEditor("revoke",t),"quiet danger"));
    }
    controls.append(group);
    row.append(name,access,state,element("td",t.expires ? date(t.expires) : "No expiry · legacy"),element("td",date(t.last_seen)),controls);
    rows.append(row);
  });
  $("#empty").hidden = selected.length > 0;
  $("#result-count").textContent = `${selected.length} token${selected.length === 1 ? "" : "s"}`;
  $("#page-count").textContent = `${page+1} / ${pages}`;
  $("#prev").disabled = page === 0; $("#next").disabled = page+1 >= pages;
}
async function refreshTokens() { tokens = (await api("/admin/api/tokens")).tokens; renderTokens(); }
function renderActivity() {
  const search = $("#activity-search").value.toLowerCase(), rows = $("#activity-rows");
  rows.replaceChildren();
  events.filter(e => [e.actor,e.action,e.token_id].join(" ").toLowerCase().includes(search)).forEach(e => {
    const row = element("tr");
    [date(e.at),e.action,e.actor,e.token_id || "—"].forEach(v => row.append(element("td",v)));
    rows.append(row);
  });
}
async function refreshActivity() { events = (await api("/admin/api/activity")).events; renderActivity(); }
function details(t) {
  const box = $("#details"); box.replaceChildren();
  const fields = {Label:t.label, "Token ID":t.id, Owner:t.owner, Purpose:t.purpose, Status:t.status,
    "Store access":t.legacy ? "Legacy unrestricted access" : t.stores.join(", "), Operations:t.operations.join(", "),
    Created:date(t.created), Expires:date(t.expires), Revoked:date(t.revoked), "Last observed":date(t.last_seen),
    Revision:t.revision, "Rotated from":t.rotated_from || "—"};
  Object.entries(fields).forEach(([k,v])=>box.append(element("dt",k),element("dd",v)));
  $("#detail-dialog").showModal();
}
function openEditor(mode,t = null) {
  action = mode; editing = t; operationId = crypto.randomUUID();
  const form = $("#token-form"); form.reset();
  $("#form-error").hidden = true;
  const label = mode[0].toUpperCase()+mode.slice(1)+" token";
  $("#dialog-title").textContent = label; $("#submit").textContent = label;
  $("#dialog-intro").textContent = mode === "create" ? "The credential is shown once. Store it securely before closing." :
    mode === "rotate" ? `Replace “${t.label}” with a new credential using the same permissions.` :
    mode === "edit" ? "Update ownership and description. Permissions and expiry remain fixed; create a replacement to change access." : `Revoke “${t.label}”. This cannot be undone.`;
  $("#metadata-fields").hidden = !["create","edit"].includes(mode);
  for (const field of ["label","owner","purpose"]) {
    form.elements[field].required = ["create","edit"].includes(mode);
    form.elements[field].value = t ? t[field] : "";
  }
  $("#scope-fields").hidden = mode !== "create";
  form.elements.stores.required = mode === "create";
  $("#expiry-fields").hidden = !["create","rotate"].includes(mode);
  $("#overlap-fields").hidden = mode !== "rotate";
  $("#revoke-fields").hidden = mode !== "revoke";
  form.elements.confirmation.required = mode === "revoke";
  $("#editor").showModal();
}
$("#token-form").addEventListener("submit",async(event)=>{
  event.preventDefault();
  const form = event.target, data = new FormData(form), body = {operation_id:operationId};
  if(editing) body.revision = editing.revision;
  if(["create","edit"].includes(action)) ["label","owner","purpose"].forEach(k=>body[k]=data.get(k).trim());
  if(action === "create") { body.stores = data.get("stores").split(",").map(s=>s.trim()).filter(Boolean); body.operations = data.getAll("operations"); }
  if(["create","rotate"].includes(action)) body.days = Number(data.get("days"));
  if(action === "rotate") body.overlap_hours = Number(data.get("overlap_hours"));
  if(action === "revoke" && data.get("confirmation") !== editing.label) {
    $("#form-error").textContent = "Enter the exact token label to confirm revocation."; $("#form-error").hidden = false; return;
  }
  $("#submit").disabled = true;
  try {
    const result = await api(action === "create" ? "/admin/api/tokens" : `/admin/api/tokens/${encodeURIComponent(editing.id)}/${action}`,body);
    $("#editor").close();
    if(result.secret) {
      $("#secret").value = result.secret; delete result.secret;
      $("#copy-status").textContent = ""; $("#secret-dialog").showModal();
    } else message(action === "revoke" ? "Token revoked. It will be refused on the next operation." : "Token details updated.");
    await refreshTokens();
  } catch(error) {
    $("#form-error").textContent = `${error.message} If the response was lost, refresh the token list before creating another credential.`;
    $("#form-error").hidden = false;
  } finally { $("#submit").disabled = false; }
});
$("#copy-secret").addEventListener("click",async()=>{
  try { await navigator.clipboard.writeText($("#secret").value); $("#copy-status").textContent = "Copied. Save it in your service's secret store."; }
  catch { $("#secret").select(); $("#copy-status").textContent = "Clipboard unavailable. Copy the selected credential manually."; }
});
$("#secret-done").addEventListener("click",()=>{ $("#secret").value = ""; $("#secret-dialog").close(); });
$("#secret-dialog").addEventListener("cancel",event=>event.preventDefault());
$("#secret-dialog").addEventListener("close",()=>{ $("#secret").value = ""; });
window.addEventListener("pagehide",()=>{ $("#secret").value = ""; });
window.addEventListener("pageshow",event=>{if(event.persisted) location.reload();});
document.querySelectorAll(".close").forEach(node=>node.addEventListener("click",()=>node.closest("dialog").close()));
$("#create").addEventListener("click",()=>openEditor("create"));
$("#refresh").addEventListener("click",()=>refreshTokens().catch(e=>message(e.message,true)));
$("#activity-refresh").addEventListener("click",()=>refreshActivity().catch(e=>message(e.message,true)));
for(const id of ["search","status","sort"]) $("#"+id).addEventListener("input",()=>{page=0;renderTokens();});
$("#activity-search").addEventListener("input",renderActivity);
$("#prev").addEventListener("click",()=>{page--;renderTokens();});
$("#next").addEventListener("click",()=>{page++;renderTokens();});
$("#logout").addEventListener("click",async()=>{
  try {await api("/auth/logout",{});location.assign("/");} catch(e) {message(e.message,true);}
});
function navigate() {
  const view = ["tokens","activity","help"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "tokens";
  document.querySelectorAll(".view").forEach(node=>{node.hidden = node.id !== view;});
  document.querySelectorAll("[data-view]").forEach(node=>{node.classList.toggle("selected",node.dataset.view === view);node.setAttribute("aria-current",node.dataset.view === view ? "page" : "false");});
  document.title = `${view === "tokens" ? "Access tokens" : view === "activity" ? "Activity" : "Connection help"} · memd`;
  if(view === "activity") refreshActivity().catch(e=>message(e.message,true));
}
window.addEventListener("hashchange",navigate);navigate();
refreshTokens().catch(e=>message(e.message,true));
api("/admin/api/owners").then(result=>{
  for(const owner of result.owners){const option=document.createElement("option");option.value=owner.email;option.label=owner.name;ownerList.append(option);}
  ownerHint.textContent=result.owners.length ? `People suggested from ${result.source}. Service or team owners can also be entered.` : "Directory suggestions are unavailable. Enter a person, team or service owner.";
}).catch(()=>{ownerHint.textContent="Directory suggestions are unavailable. Enter a person, team or service owner.";});
