// System workspace navigation and per-system browser preferences.

export function workspacePath(path, oid) {
  if (!oid) return path;
  const root=`/v1/observed-systems/${encodeURIComponent(oid)}`;
  if (path.startsWith("/v1/sessions")) return root+path.slice(3);
  if (path.startsWith("/v1/incidents")) return root+path.slice(3);
  if (path==="/webui/api/capabilities") return root+"/capabilities";
  return path;
}

export class Workspace {
  constructor(api, enter, leave, doc=document, storage=localStorage) {
    this.api=api;this.enter=enter;this.leave=leave;this.doc=doc;this.storage=storage;this.oid=null;this.row=null;this.revision=0;
  }
  key(name) { return `aiops:system:${this.oid || "home"}:${name}`; }
  selected() { return this.oid ? {observed_id:this.oid} : {}; }
  settings(save=false) {
    const ids=["env","cluster","window","noclari","stream"];
    if (save && this.oid) {
      this.storage.setItem(this.key("settings"),JSON.stringify(Object.fromEntries(ids.map((id)=>[id,this.doc.getElementById(id).type==="checkbox" ? this.doc.getElementById(id).checked : this.doc.getElementById(id).value]))));return;
    }
    const envs=[...new Set((this.row?.services || []).flatMap((s)=>(s.instances || []).map((i)=>i.env)))];
    let data={env:envs.length===1 ? envs[0] : "",cluster:"",window:"30m",noclari:false,stream:true};
    try {data={...data,...JSON.parse(this.storage.getItem(this.key("settings")) || "{}")};} catch {}
    for (const id of ids) {const el=this.doc.getElementById(id);if(el.type==="checkbox") el.checked=!!data[id];else el.value=data[id];}
  }
  init() {
    window.addEventListener("hashchange",()=>this.route());
    this.doc.getElementById("workspace-back").addEventListener("click",()=>{location.hash="";});
    for (const id of ["env","cluster","window","noclari","stream"]) this.doc.getElementById(id).addEventListener("change",()=>this.settings(true));
    return this.route();
  }
  async route() {
    this.settings(true);this.revision++;const revision=this.revision;
    await this.leave();this.oid=null;this.row=null;this.doc.body.classList.add("system-home");
    this.panel("observed");this.doc.getElementById("workspace-title").textContent="被观测系统";
    if(location.hash==="#intents") {
      this.panel("intents");this.doc.getElementById("workspace-title").textContent="意图列表";return;
    }
    if(location.hash==="#knowledge") {
      this.panel("knowledge");this.doc.getElementById("workspace-title").textContent="知识库";return;
    }
    const match=location.hash.match(/^#system=(obs_[a-f0-9]{32})(?:&session=([A-Za-z0-9_:-]{1,200}))?$/);
    if (!match) return;
    const oid=match[1];const result=await this.api(`/v1/observed-systems/${oid}`);
    if (revision!==this.revision)return;
    if (result.status!==200 || !result.data.registered_at) {this.doc.getElementById("observed-note").textContent="该系统未登记或目录不可读，请核查接入。";return;}
    this.oid=oid;this.row=result.data;this.requestedSession=match[2] || null;this.settings();this.doc.body.classList.remove("system-home");
    this.doc.getElementById("workspace-title").textContent=`${this.row.name} · ${oid.slice(-8)}`;
    this.doc.getElementById("workspace-system").textContent=`${this.row.name} · ${oid}`;
    this.panel("chat");await this.enter(this.row,revision);
  }
  panel(name) {
    this.doc.querySelectorAll(".panel").forEach((p)=>p.classList.toggle("active",p.id===`panel-${name}`));
    this.doc.querySelectorAll(".nav-item").forEach((b)=>b.classList.toggle("active",b.dataset.panel===name));
  }
}
