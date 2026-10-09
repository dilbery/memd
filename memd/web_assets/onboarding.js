"use strict";
const $=s=>document.querySelector(s),csrf=$('meta[name="csrf-token"]').content,origin=$('meta[name="memd-origin"]').content;
let tokens=[],issued=null;
function node(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function notice(text,error=false){const n=$("#message");n.className=`notice ${error?"error":"success"}`;n.textContent=text;n.hidden=false;}
async function api(path,data){const options={cache:"no-store",credentials:"same-origin",headers:{}};if(data!==undefined){options.method="POST";options.headers={"Content-Type":"application/json","X-CSRF-Token":csrf};options.body=JSON.stringify(data);}const r=await fetch(path,options),body=await r.json();if(!r.ok){const e=new Error(typeof body.detail==="string"?body.detail:"The request could not be completed.");e.status=r.status;throw e;}return body;}
function error(e){notice(e.message,true);if(e.status===401||e.status===403){const a=node("a","Sign in again");a.href="/user-auth/login";$("#message").append(" ",a);}}
async function copy(text,button){try{await navigator.clipboard.writeText(text);const old=button.textContent;button.textContent="Copied";setTimeout(()=>button.textContent=old,1800);}catch{notice("Clipboard access was denied. Select the text and copy it manually.",true);}}
function snippet(parent,text){const box=node("div",undefined,"snippet"),pre=node("pre",text),button=node("button","Copy");button.type="button";button.addEventListener("click",()=>copy(text,button));box.append(pre,button);parent.append(box);}
function instructions(){
 const target=$("#instructions"),client=$("#client").value,ps=$("#shell").value==="powershell";target.replaceChildren();
 target.append(node("h3","1. Load your token in this terminal"),node("p","Run this command, then paste your token at the hidden prompt. This lasts for the current terminal only; repeat it when opening a new terminal.","muted"));
 snippet(target,ps?'$env:MEMD_TOKEN = Read-Host "Paste your memd token" -MaskInput':'read -rsp "Paste your memd token: " MEMD_TOKEN; export MEMD_TOKEN; echo');
 const url=origin+"/mcp/",entry={type:"http",url,headers:{Authorization:"Bearer ${MEMD_TOKEN}"}};
 target.append(node("h3","2. Configure your client"));
 if(client==="claude"){
  snippet(target,"claude mcp add-json --scope user memd-personal '"+JSON.stringify(entry)+"'");
  target.append(node("p","Start Claude Code from this terminal. Run /mcp to check memd-personal. The user-scoped entry is available across your projects. If it already exists, inspect and update that entry instead of adding it again.","muted"));
 }else if(client==="codex"){
  snippet(target,`codex mcp add memd-personal --url ${url} --bearer-token-env-var MEMD_TOKEN`);
  target.append(node("p","Start Codex from this terminal, then run /mcp. For the desktop app or an IDE, the app process must also receive MEMD_TOKEN; setting it in an unrelated terminal does not update an already-running app.","muted"));
 }else if(client==="omp"){
  target.append(node("p","Merge this entry into mcpServers in ~/.omp/agent/mcp.json. Keep your other entries. Named OMP profiles use ~/.omp/profiles/<name>/agent/mcp.json. Current OMP expands the environment variable in this header.","muted"));
  snippet(target,JSON.stringify({mcpServers:{"memd-personal":entry}},null,2));
  target.append(node("p","Start omp from this terminal and check /mcp. If an older build sends the placeholder literally and returns 401, use the Python bridge below. It also works in other stdio-only MCP clients.","muted"));
  bridge(target,ps);
 }else if(client==="pi"){
  target.append(node("p","Pi uses the memd extension for automatic recall and the remember tool. Download it first; inspect it before loading. If you already have a memd extension, update that copy instead of loading two.","muted"));
  snippet(target,ps?`Invoke-WebRequest '${origin}/clients/pi-memd.ts' -OutFile memd-personal.ts\n$env:MEMD_URL = '${origin}'\nRemove-Item Env:MEMD_PROFILE -ErrorAction SilentlyContinue\npi -e ./memd-personal.ts`:`curl --fail --show-error '${origin}/clients/pi-memd.ts' -o memd-personal.ts\nexport MEMD_URL='${origin}'\nunset MEMD_PROFILE\npi -e ./memd-personal.ts`);
  target.append(node("p","After testing, place the extension in ~/.pi/agent/extensions/ for automatic discovery. Keep MEMD_URL and MEMD_TOKEN available when starting Pi. Your token selects the store; no profile override is needed.","muted"));
 }else{
  target.append(node("p","Use Streamable HTTP MCP with the server URL and Authorization header below. Put the actual token in your client's secret field. ${MEMD_TOKEN} is a placeholder unless your client explicitly supports environment expansion.","muted"));
  snippet(target,`MCP URL: ${url}\nAuthorization: Bearer <your token>\nTools: recall, read, save\nREST base URL: ${origin}\nREST: POST /recall, POST /read, POST /save\nContent-Type: application/json`);
  bridge(target,ps);
 }
 target.append(node("h3","3. Check the connection"),node("p",client==="pi"?'Ask Pi to remember a short, non-sensitive preference. Open My memories here and confirm it appeared.':'Ask your agent: “Use memd-personal to recall my saved preferences.” To test saving, ask it to remember a short, non-sensitive preference, then check My memories here.',"muted"));
 target.append(node("p","Your device must be able to reach this server and trust its TLS certificate. Keep the client’s normal tool approval prompts. If access fails after token expiry, create a replacement and update the environment variable.","muted"));
 const links={claude:["Claude Code MCP documentation","https://code.claude.com/docs/en/mcp"],codex:["Official Codex MCP documentation","https://developers.openai.com/codex/mcp/"],omp:["OMP MCP documentation","https://github.com/can1357/oh-my-pi/blob/main/docs/mcp-config.md"],pi:["Pi extension documentation","https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md"]};
 if(links[client]){const a=node("a",links[client][0]);a.href=links[client][1];a.target="_blank";a.rel="noopener noreferrer";target.append(a);}
}
function bridge(target,ps){
 target.append(node("h3","Python bridge for stdio clients"),node("p","Requires Python 3. Download and inspect the bridge, then configure your client to launch it. Use an absolute path in the configuration. It reads MEMD_TOKEN from the client process environment.","muted"));
 snippet(target,ps?`Invoke-WebRequest '${origin}/clients/memd-mcp-bridge' -OutFile memd-mcp-bridge.py`:`curl --fail --show-error '${origin}/clients/memd-mcp-bridge' -o memd-mcp-bridge.py`);
 snippet(target,JSON.stringify({mcpServers:{"memd-personal":{type:"stdio",command:ps?"python":"python3",args:[ps?"C:/path/to/memd-mcp-bridge.py":"/absolute/path/to/memd-mcp-bridge.py",origin]}}},null,2));
}
async function load(){try{tokens=(await api("/memories/onboarding/api/tokens")).tokens;const list=$("#token-list");list.replaceChildren();$("#no-tokens").hidden=!!tokens.length;for(const t of tokens){const row=node("div",undefined,"token-item"),info=node("div"),title=node("h3",t.label);title.append(node("span",t.status,`badge ${t.status}`));info.append(title,node("p",`Created ${new Date(t.created*1000).toLocaleDateString()} · Expires ${new Date(t.expires*1000).toLocaleDateString()} · Last used ${t.last_seen?new Date(t.last_seen*1000).toLocaleString():"never"}`));row.append(info);if(t.status==="active"){const b=node("button","Revoke");b.addEventListener("click",async()=>{if(!confirm(`Revoke “${t.label}”? Tools using it will lose access.`))return;b.disabled=true;try{await api(`/memories/onboarding/api/tokens/${t.id}/revoke`,{revision:t.revision,operation_id:crypto.randomUUID()});notice("Token revoked.");await load();}catch(e){error(e);b.disabled=false;}});row.append(b);}list.append(row);}}catch(e){error(e);}}
$("#create-form").addEventListener("submit",async e=>{e.preventDefault();$("#create").disabled=true;try{issued=await api("/memories/onboarding/api/tokens",{label:e.target.elements.label.value,days:Number(e.target.elements.days.value),operation_id:crypto.randomUUID()});$("#secret").value=issued.secret;$("#test-result").textContent="";$("#secret-dialog").showModal();e.target.reset();await load();}catch(e){error(e);await load();}finally{$("#create").disabled=false;}});
function clearSecret(){$("#secret").value="";issued=null;}
$("#secret-dialog").addEventListener("close",clearSecret);
for(const id of ["#secret-close","#secret-done"])$(id).addEventListener("click",()=>$("#secret-dialog").close());
$("#copy-secret").addEventListener("click",e=>copy($("#secret").value,e.target));
$("#test-token").addEventListener("click",async()=>{if(!issued)return;$("#test-token").disabled=true;try{const r=await fetch(origin+"/recall",{method:"POST",cache:"no-store",credentials:"omit",headers:{"Authorization":"Bearer "+issued.secret,"Content-Type":"application/json"},body:JSON.stringify({query:"",include_core:false})});const body=await r.json();if(!r.ok||body.profile!==issued.token.personal_store)throw new Error("Connection could not be verified. Check your token and network access.");$("#test-result").textContent="Connected to your personal memory store.";}catch(e){$("#test-result").textContent=e.message;}finally{$("#test-token").disabled=false;}});
$("#client").addEventListener("change",instructions);$("#shell").addEventListener("change",instructions);$("#refresh").addEventListener("click",load);
$("#logout").addEventListener("click",async()=>{try{await api("/user-auth/logout",{});clearSecret();location.assign("/");}catch(e){error(e);}});
window.addEventListener("pagehide",clearSecret);window.addEventListener("pageshow",e=>{if(e.persisted)location.reload();});
instructions();load();
