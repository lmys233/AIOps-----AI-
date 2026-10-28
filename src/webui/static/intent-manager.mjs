// Shared platform configuration. User text never becomes HTML or executable code.
export const emptyIntent = () => ({id:"",name:"",description:"",handler:"INCIDENT_RCA",enabled:true,
  priority:100,gate:.5,patterns:[],scope:null,examples:[]});

export function editorPayload(form) {
  const read=(id)=>form.querySelector(`#intent-${id}`).value;
  return {id:read("id").trim(),name:read("name").trim(),description:read("description"),
    handler:read("handler"),enabled:form.querySelector("#intent-enabled").checked,
    priority:Number(read("priority")),gate:Number(read("gate")),patterns:JSON.parse(read("patterns")),
    scope:JSON.parse(read("scope")),examples:read("examples").split("\n").map(x=>x.trim()).filter(Boolean)};
}

export class IntentManager {
  constructor(api,doc=document) {this.api=api;this.doc=doc;this.data=null;this.generation=0;this.busy=false;}
  el(id) {return this.doc.getElementById(id);}
  init() {
    this.el("intent-refresh").onclick=()=>this.load();
    this.el("intent-add").onclick=()=>this.edit(emptyIntent(),true);
    this.el("intent-cancel").onclick=()=>this.el("intent-editor").hidden=true;
    this.el("intent-editor").onsubmit=(e)=>{e.preventDefault();this.save();};
  }
  async load() {
    const generation=++this.generation;
    this.el("intent-note").textContent="正在加载意图目录…";
    try {
      const r=await this.api("/v1/intents");if(generation!==this.generation)return;
      if(r.status!==200) {this.el("intent-note").textContent=`目录读取失败（${r.status}）：${r.data?.message || r.data?.error || "请重试"}`;return;}
      this.data=r.data;this.render();
    } catch(e) {if(generation===this.generation)this.el("intent-note").textContent="目录读取失败："+e.message;}
  }
  render() {
    const {routing,protected:boundary}=this.data;
    this.el("intent-note").textContent=`规则 → 示例向量 → 大模型 · ${routing.kind==="lexical" ? "本地词法向量（非语义模型）" : routing.provider.startsWith("bge-") ? "BGE 语义向量（CPU）" : "语义向量"} · 示例就绪 ${routing.ready}/${routing.total}${routing.error ? " · 预计算未完成："+routing.error : ""}。${boundary}`;
    const list=this.el("intent-list");list.replaceChildren();
    for(const row of [...this.data.routes].sort((a,b)=>a.priority-b.priority||a.id.localeCompare(b.id))) {
      const card=this.doc.createElement("article");card.className="intent-card";
      const title=this.doc.createElement("h3");title.textContent=`${row.name} · ${row.id} ${row.enabled ? "" : "（已停用）"}`;card.append(title);
      const desc=this.doc.createElement("p");desc.textContent=row.description;card.append(desc);
      const info=this.doc.createElement("p");info.className="muted";info.textContent=`处理能力 ${row.handler} · 优先级 ${row.priority} · 门槛 ${row.gate} · ${row.patterns.length} 条规则 · ${row.examples.length} 条示例`;card.append(info);
      const details=this.doc.createElement("details"),summary=this.doc.createElement("summary");summary.textContent="查看规则、作用域与示例";details.append(summary);
      const pre=this.doc.createElement("pre");pre.textContent=JSON.stringify({patterns:row.patterns,scope:row.scope,examples:row.examples},null,2);details.append(pre);card.append(details);
      for(const [name,action] of [["编辑",()=>this.edit(row,false)],["删除",()=>this.remove(row)]]) {
        const button=this.doc.createElement("button");button.textContent=name;button.onclick=action;card.append(button);
      }
      list.append(card);
    }
  }
  edit(row,creating) {
    if(!this.data || this.busy)return;
    this.current=row;this.creating=creating;this.editRevision=this.data.revision;
    const form=this.el("intent-editor");form.hidden=false;
    for(const id of ["id","name","description","handler","priority","gate"])this.el("intent-"+id).value=row[id];
    this.el("intent-id").readOnly=!creating;this.el("intent-enabled").checked=row.enabled;
    this.el("intent-patterns").value=JSON.stringify(row.patterns,null,2);
    this.el("intent-scope").value=JSON.stringify(row.scope,null,2);
    this.el("intent-examples").value=row.examples.join("\n");this.el("intent-editor-note").textContent="";
    form.scrollIntoView({block:"start",behavior:"smooth"});
  }
  async save() {
    if(this.busy)return;
    let route;
    try {route=editorPayload(this.el("intent-editor"));} catch(e) {this.el("intent-editor-note").textContent="规则或作用域 JSON 格式错误："+e.message;return;}
    this.busy=true;this.el("intent-save").disabled=true;
    try {
      const r=await this.api(this.creating ? "/v1/intents" : "/v1/intents/"+encodeURIComponent(this.current.id),
        {method:this.creating ? "POST" : "PUT",body:{revision:this.editRevision,route}});
      if(r.status!==200) {this.el("intent-editor-note").textContent=`保存失败（${r.status}）：${r.data?.message || r.data?.error}`;return;}
      this.el("intent-editor").hidden=true;await this.load();
    } catch(e) {this.el("intent-editor-note").textContent="未确认保存成功，请刷新核实："+e.message;}
    finally {this.busy=false;this.el("intent-save").disabled=false;}
  }
  async remove(row) {
    if(this.busy || !globalThis.confirm(`删除路由「${row.name}」？历史诊断保留。`))return;
    this.busy=true;
    try {
      const r=await this.api(`/v1/intents/${encodeURIComponent(row.id)}?revision=${encodeURIComponent(this.data.revision)}`,{method:"DELETE"});
      if(r.status!==200) {this.el("intent-note").textContent=`删除失败（${r.status}）：${r.data?.message || r.data?.error}`;return;}
      await this.load();
    } catch(e) {this.el("intent-note").textContent="未确认删除成功，请刷新核实："+e.message;}
    finally {this.busy=false;}
  }
}
