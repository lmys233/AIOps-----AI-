import {workspacePath} from "./workspace.mjs";

export function createWorkspaceRequests({workspace,token:getToken}) {
  const pending=new Set();
async function request(path, { method = "GET", body = null, headers = {},signal=null } = {}) {
  const revision=workspace()?.revision;const original=path;
  path=workspacePath(path,workspace()?.oid);
  if(body!==null && (path==="/v1/chat"||path==="/v1/chat/resume"))
    body={...body,options:{...(body.options||{}),response_mode:"view"}};
  if(workspace()?.oid && /^\/v1\/observed-systems\/[^/]+\/(?:sessions(?:\/[^?]*)?|incidents\/[^/?]+)(?:\?|$)/.test(path))
    path+=(path.includes("?")?"&":"?")+"response_mode=view";
  const h = { ...headers };
  if (getToken()) h["Authorization"] = `Bearer ${getToken()}`;
  if (body !== null) h["Content-Type"] = "application/json";
  const controller=new AbortController();
  const scoped=path!==original || path.startsWith("/v1/observed-systems/");
  const abort=()=>controller.abort();signal?.addEventListener("abort",abort,{once:true});if(signal?.aborted)controller.abort();
  if(scoped && (method==="GET" || /\/commands\/(?:preview|suggest)$/.test(path)))pending.add(controller);
  let resp;
  try { resp=await fetch(path, {method,headers:h,body:body===null?undefined:JSON.stringify(body),signal:controller.signal}); }
  catch(error) { if(controller.signal.aborted)return {status:409,data:null,stale:true}; throw error; }
  finally {pending.delete(controller);signal?.removeEventListener("abort",abort);}
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if ((path!==original||path.startsWith("/v1/observed-systems/")) && revision!==workspace()?.revision) return {status:409,data:null,stale:true};
  return { status: resp.status, data };
}

return {request,reset(){for(const item of pending)item.abort();pending.clear();}};
}
