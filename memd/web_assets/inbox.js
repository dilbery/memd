"use strict";
const $=s=>document.querySelector(s),csrf=$('meta[name="csrf-token"]').content;
let current=null,page=1,pages=1,dirty=false,loading=0,opening=0;
function node(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function notice(text,error=false){const n=$("#message");n.className=`notice ${error?"error":"success"}`;n.textContent=text;n.hidden=false;}
async function api(path,data){
 const opts={cache:"no-store",credentials:"same-origin",headers:{}};
 if(data!==undefined){opts.method="POST";opts.headers={"Content-Type":"application/json","X-CSRF-Token":csrf};opts.body=JSON.stringify(data);}
 const r=await fetch(path,opts),body=await r.json();
 if(!r.ok){const e=new Error(typeof body.detail==="string"?body.detail:"The request could not be completed.");e.status=r.status;throw e;}
 return body;
}
function showError(error,target="#message"){
 const n=$(target);n.textContent=error.message;n.className="notice error";n.hidden=false;
 if(error.status===401||error.status===403){const a=node("a","Sign in again","button");a.href="/user-auth/login";n.append(" ",a);}
}
function when(seconds){return seconds?new Date(seconds*1000).toLocaleString():"";}
function flags(item){const l=item.lint||{},out=[];if((l.near_duplicates||[]).length)out.push("near-duplicate");if((l.conflicts||[]).length)out.push("conflict");if(l.error)out.push("needs changes");return out;}
async function load(){
 const serial=++loading,params=new URLSearchParams({status:$("#status").value,page:String(page)});
 try{const result=await api("/memories/api/inbox?"+params);if(serial!==loading)return;
  page=result.page;pages=result.pages;
  for(const s of ["pending","approved","rejected"])$(`#${s}-count`).textContent=result.counts[s];
  const list=$("#candidate-list");list.replaceChildren();
  for(const item of result.items){const b=node("button",undefined,"memory-card"+(current?.id===item.id?" selected":""));b.type="button";b.dataset.id=item.id;
   b.append(node("h3",item.title),node("p",item.excerpt),node("span",item.status,`badge ${item.status}`));
   for(const f of flags(item))b.append(" ",node("span",f,"badge flag"));
   b.addEventListener("click",()=>openCandidate(item.id));list.append(b);}
  const empty=$("#empty");empty.hidden=!!result.total;
  empty.querySelector("h2").textContent=$("#status").value==="pending"?"Nothing to review":"No candidates in this view";
  $("#result-count").textContent=`${result.total} candidate${result.total===1?"":"s"}`;$("#page-count").textContent=`${page} / ${pages}`;$("#prev").disabled=page<=1;$("#next").disabled=page>=pages;
 }catch(e){showError(e);}
}
function discard(){return !dirty||confirm("Discard your changes to this proposal?");}
async function openCandidate(id){
 if(!discard())return;dirty=false;const serial=++opening;
 try{const item=await api("/memories/api/inbox/"+encodeURIComponent(id));if(serial!==opening)return;current=item;render();document.querySelectorAll(".memory-card").forEach(n=>n.classList.toggle("selected",n.dataset.id===id));}
 catch(e){showError(e);}
}
function lintLine(label,text){const li=node("li");li.append(node("b",label+" "),document.createTextNode(text));return li;}
function render(){
 $(".memory-layout").classList.add("reading");$("#choose").hidden=true;const f=$("#review-form");f.hidden=false;$("#review-error").hidden=true;
 const meta=current.meta||{},lint=current.lint||{},pending=current.status==="pending";
 f.elements.title.value=current.title;f.elements.body.value=current.body;f.elements.description.value=meta.description||"";
 f.elements.tags.value=(meta.tags||[]).join(", ");f.elements.importance.value=String(meta.importance||3);
 for(const el of f.elements)if(el.name)el.disabled=!pending;
 $("#approve").hidden=!pending;$("#reject").hidden=!pending;
 $("#candidate-status").textContent=current.status;$("#candidate-status").className=`badge ${current.status}`;
 const decided=$("#decided");decided.hidden=pending;
 decided.textContent=current.status==="approved"?`Approved by ${current.reviewer||"a reviewer"} on ${when(current.decided_at)} as ${current.slug||"a memory"}.`:current.status==="rejected"?`Rejected by ${current.reviewer||"a reviewer"} on ${when(current.decided_at)}${current.reason?": "+current.reason:"."}`:"";
 const verbs={create:"A new memory is created",update:"The existing memory is updated",supersede:"A memory is replaced"};
 $("#lint-action").textContent=`${verbs[lint.action]||"A memory is saved"} as ${lint.slug||"a new identifier"}${lint.supersedes?`, retiring ${lint.supersedes}`:""}. Checked when it was proposed.`;
 const items=$("#lint-items");items.replaceChildren();
 if(lint.error)items.append(lintLine("Needs changes:",lint.error));
 for(const d of lint.near_duplicates||[])items.append(lintLine("Near-duplicate:",`${d.slug}${d.cosine?` (similarity ${d.cosine})`:""}`));
 for(const c of lint.conflicts||[])items.append(lintLine(c.kind==="updates"?"Updates:":"Contradicts:",`${c.slug} — ${c.evidence}`));
 const shown=new Set([...(lint.near_duplicates||[]).map(d=>d.slug),...(lint.conflicts||[]).map(c=>c.slug)]);
 const related=(lint.related||[]).filter(s=>!shown.has(s));if(related.length)items.append(lintLine("Related:",related.join(", ")));
 for(const w of lint.warnings||[])items.append(lintLine("Note:",w));
 if(!items.children.length)items.append(node("li","No duplicates or conflicts found.","muted"));
 const fields={"Proposed by":current.proposer||"Not recorded","Channel":current.source||"Not recorded","Proposed":when(current.created),"Source":meta.source||"Not recorded","Host":meta.host||"Not set","Last error":current.last_error||"None","Candidate ID":current.id};
 const dl=$("#provenance");dl.replaceChildren();Object.entries(fields).forEach(([k,v])=>dl.append(node("dt",k),node("dd",v)));
}
function edits(){
 const f=$("#review-form"),meta=current.meta||{},out={};
 const tags=f.elements.tags.value.split(",").map(t=>t.trim()).filter(Boolean),importance=Number(f.elements.importance.value);
 if(f.elements.title.value!==current.title)out.title=f.elements.title.value;
 if(f.elements.body.value!==current.body)out.body=f.elements.body.value;
 if(f.elements.description.value!==(meta.description||""))out.description=f.elements.description.value;
 if(tags.join("\n")!==(meta.tags||[]).join("\n"))out.tags=tags;
 if(importance!==(meta.importance||3))out.importance=importance;
 return out;
}
$("#review-form").addEventListener("input",()=>{dirty=true;});
$("#review-form").addEventListener("submit",async e=>{e.preventDefault();$("#approve").disabled=true;
 try{const result=await api(`/memories/api/inbox/${encodeURIComponent(current.id)}/approve`,{edits:edits()});
  dirty=false;const r=result.receipt||{};notice(`Saved as ${r.slug}.`+(r.synced?"":" Backup sync is pending.")+(result.edited?" Your corrections were applied.":""));
  await openCandidate(current.id);await load();
 }catch(error){showError(error,"#review-error");}finally{$("#approve").disabled=false;}
});
$("#reject").addEventListener("click",()=>{$("#reject-title").textContent=current.title;$("#reject-form").reset();$("#reject-error").hidden=true;$("#reject-dialog").showModal();});
$("#reject-form").addEventListener("submit",async e=>{e.preventDefault();$("#confirm-reject").disabled=true;
 try{await api(`/memories/api/inbox/${encodeURIComponent(current.id)}/reject`,{reason:e.target.elements.reason.value});$("#reject-dialog").close();dirty=false;notice("Proposal rejected. Nothing was saved.");await openCandidate(current.id);await load();}
 catch(error){showError(error,"#reject-error");}finally{$("#confirm-reject").disabled=false;}
});
$("#back").addEventListener("click",()=>{if(discard()){dirty=false;$(".memory-layout").classList.remove("reading");}});
document.querySelectorAll(".close").forEach(n=>n.addEventListener("click",()=>n.closest("dialog").close()));
$("#status").addEventListener("change",()=>{page=1;load();});
$("#refresh").addEventListener("click",()=>load());$("#prev").addEventListener("click",()=>{page--;load();});$("#next").addEventListener("click",()=>{page++;load();});
$("#logout").addEventListener("click",async()=>{if(!discard())return;try{await api("/user-auth/logout",{});dirty=false;location.assign("/");}catch(e){showError(e);}});
window.addEventListener("beforeunload",e=>{if(dirty){e.preventDefault();e.returnValue="";}});
window.addEventListener("pageshow",e=>{if(e.persisted)location.reload();});
load();
