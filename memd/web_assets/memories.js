"use strict";
const $=s=>document.querySelector(s),csrf=$('meta[name="csrf-token"]').content;
let current=null,page=1,pages=1,editing=false,loading=0,opening=0,timer;
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
 if(error.status===409){const b=node("button","Reload memory","button");b.type="button";b.addEventListener("click",()=>{if(confirm("Reloading discards the changes in this form. Continue?")){editing=false;openNote(current.slug);}});n.append(" ",b);}
}
async function load(){
 const serial=++loading,params=new URLSearchParams({q:$("#search").value,status:$("#status").value,page:String(page)});
 try{const result=await api("/memories/api/list?"+params);if(serial!==loading)return;
  page=result.page;pages=result.pages;$("#active-count").textContent=result.counts.active;$("#retracted-count").textContent=result.counts.retracted;
  const list=$("#memory-list");list.replaceChildren();
  for(const item of result.items){const b=node("button",undefined,"memory-card"+(current?.slug===item.slug?" selected":""));b.type="button";b.dataset.slug=item.slug;b.append(node("h3",item.title),node("p",item.description||item.excerpt),node("span",item.status,`badge ${item.status}`));b.addEventListener("click",()=>openNote(item.slug));list.append(b);}
  const empty=$("#empty");empty.hidden=!!result.total;
  empty.querySelector("h2").textContent=$("#search").value?"No matching memories":$("#status").value==="active"?"No active memories yet":"No memories in this view";
  empty.querySelector("p").textContent=$("#search").value?"Try a different word or change the status filter.":"Memories saved by your tools appear here. Only your own store is shown.";
  $("#result-count").textContent=`${result.total} memor${result.total===1?"y":"ies"}`;$("#page-count").textContent=`${page} / ${pages}`;$("#prev").disabled=page<=1;$("#next").disabled=page>=pages;
 }catch(e){showError(e);}
}
function discard(){return !editing||confirm("Discard the changes in this form?");}
async function openNote(slug){
 if(!discard())return;editing=false;const serial=++opening;
 try{const item=await api("/memories/api/note?"+new URLSearchParams({slug}));if(serial!==opening)return;current=item;renderNote();document.querySelectorAll(".memory-card").forEach(n=>n.classList.toggle("selected",n.dataset.slug===slug));}
 catch(e){showError(e);}
}
function renderNote(){
 $(".memory-layout").classList.add("reading");$("#choose").hidden=true;$("#detail").hidden=false;$("#edit-form").hidden=true;
 $("#note-title").textContent=current.title;$("#note-description").textContent=current.description;$("#note-body").textContent=current.body;
 $("#note-status").textContent=current.status;$("#note-status").className=`badge ${current.status}`;
 $("#note-tags").replaceChildren(...current.tags.map(t=>node("span",t,"badge")));
 $("#retired-notice").hidden=current.status==="active";
 $("#retired-notice").textContent=current.status==="retracted"?"You retracted this memory. It is excluded from recall and agent reads. Earlier versions remain in history.":"This memory has been replaced and is no longer included in recall.";
 $("#edit").hidden=!current.editable;$("#retract").hidden=current.status!=="active";
 const fields={"Source":current.source||"Not recorded","Saved by":current.saved_by||"Not recorded","Observed":current.observed_at||"Not recorded","Last correction":current.changed_at?new Date(current.changed_at).toLocaleString():"None","Memory ID":current.slug};
 const dl=$("#provenance");dl.replaceChildren();Object.entries(fields).forEach(([k,v])=>dl.append(node("dt",k),node("dd",v)));
}
$("#edit").addEventListener("click",()=>{editing=true;$("#detail").hidden=true;$("#edit-form").hidden=false;$("#edit-error").hidden=true;const f=$("#edit-form");for(const k of ["title","body","description"])f.elements[k].value=current[k]||"";f.elements.tags.value=current.tags.join(", ");f.elements.title.focus();});
$("#cancel").addEventListener("click",()=>{if(discard()){editing=false;renderNote();}});
$("#edit-form").addEventListener("submit",async e=>{e.preventDefault();$("#save").disabled=true;const f=e.target;
 try{const result=await api("/memories/api/edit",{slug:current.slug,revision:current.revision,title:f.elements.title.value,description:f.elements.description.value,body:f.elements.body.value,tags:f.elements.tags.value.split(",").map(t=>t.trim()).filter(Boolean)});
  editing=false;notice(result.message+(!result.search_updated?" Search is still updating.":""));await openNote(current.slug);await load();
 }catch(error){showError(error,"#edit-error");}finally{$("#save").disabled=false;}
});
$("#retract").addEventListener("click",()=>{$("#retract-title").textContent=current.title;$("#retract-form").reset();$("#retract-error").hidden=true;$("#retract-dialog").showModal();});
$("#retract-form").addEventListener("submit",async e=>{e.preventDefault();if(e.target.elements.confirmation.value!=="RETRACT"){showError(new Error("Type RETRACT to confirm."),"#retract-error");return;}$("#confirm-retract").disabled=true;
 try{const result=await api("/memories/api/retract",{slug:current.slug,revision:current.revision});$("#retract-dialog").close();notice("Memory retracted. "+result.message);await openNote(current.slug);await load();}
 catch(error){showError(error,"#retract-error");}finally{$("#confirm-retract").disabled=false;}
});
$("#back").addEventListener("click",()=>{$(".memory-layout").classList.remove("reading");});
$("#help").addEventListener("click",()=>$("#help-dialog").showModal());
document.querySelectorAll(".close").forEach(n=>n.addEventListener("click",()=>n.closest("dialog").close()));
$("#search").addEventListener("input",()=>{clearTimeout(timer);timer=setTimeout(()=>{page=1;load();},250);});
$("#status").addEventListener("change",()=>{page=1;load();});
$("#refresh").addEventListener("click",()=>load());$("#prev").addEventListener("click",()=>{page--;load();});$("#next").addEventListener("click",()=>{page++;load();});
$("#logout").addEventListener("click",async()=>{if(!discard())return;try{await api("/user-auth/logout",{});editing=false;location.assign("/");}catch(e){showError(e);}});
window.addEventListener("beforeunload",e=>{if(editing){e.preventDefault();e.returnValue="";}});
window.addEventListener("pageshow",e=>{if(e.persisted)location.reload();});
load();
