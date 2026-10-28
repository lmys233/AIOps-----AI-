const statuses={review:"审核",active:"生效",inactive:"失效",deleted:"删除"};
const indexStates={pending:"排队中",ready:"就绪",failed:"失败",disabled:"停用"};
export function renderKnowledgeText(text,body,esc,render){
  const sources=body?.explanation?.answer?.knowledge?.sources||[];
  const paths=new Set(sources.map(s=>s.url));const ids=new Map(sources.map(s=>[s.citation,s]));
  const pattern=/\[([^\]\n]{1,200})\]\((\/v1\/knowledge\/kb_[a-f0-9]{32}\/versions\/\d+)\)|\[(K\d+)\]/g;
  let output="",start=0;
  for(const match of String(text).matchAll(pattern)){
    output+=render(String(text).slice(start,match.index));const source=ids.get(match[3]);
    const path=match[2]||source?.url,label=match[1]||match[3];
    output+=paths.has(path)?`<a class="knowledge-reference" href="${esc(path)}">${esc(label)}</a>`:render(match[0]);
    start=match.index+match[0].length;
  }
  return output+render(String(text).slice(start));
}
export function formData(values) {
  return {title:values.title.trim(),content:values.content,uploader:values.uploader.trim()||"admin",
    category:values.category.trim()||"运维知识",tags:values.tags.split(/[,，\n]/).map(v=>v.trim()).filter(Boolean),
    observed_id:values.scope.trim()||null,status:values.status};
}
export class KnowledgeManager {
  constructor(api,doc=document){this.api=api;this.doc=doc;this.generation=0;this.offset=0;this.busy=false;this.current=null;}
  el(id){return this.doc.getElementById("kb-"+id);}
  init(){
    this.el("refresh").onclick=()=>this.load(true);this.el("filter").onsubmit=e=>{e.preventDefault();this.load(true);};
    this.el("add").onclick=()=>this.edit(null);this.el("cancel").onclick=()=>{this.el("editor").hidden=true;};
    this.el("save").onclick=e=>{e.preventDefault();this.save();};this.el("more").onclick=()=>this.load(false);
  }
  async load(reset=true){
    const generation=++this.generation;if(reset)this.offset=0;
    this.el("note").textContent="正在读取知识目录…";
    const query=new URLSearchParams({offset:this.offset,limit:20,status:this.el("filter-status").value,
      category:this.el("filter-category").value.trim(),q:this.el("filter-query").value.trim()});
    try{
      const r=await this.api("/v1/knowledge?"+query);if(generation!==this.generation)return;
      if(r.status!==200)throw Error(r.data?.message||r.data?.error||`HTTP ${r.status}`);
      if(reset)this.el("list").replaceChildren();for(const row of r.data.documents)this.render(row);
      this.offset+=r.data.documents.length;this.el("more").hidden=!r.data.has_more;
      this.el("note").textContent=`已显示 ${this.offset} 条知识；只有生效且索引就绪的当前版本参与问答。`;
    }catch(error){if(generation===this.generation)this.el("note").textContent="知识目录读取失败："+error.message;}
  }
  render(row){
    const card=this.doc.createElement("article");card.className="knowledge-card";card.dataset.id=row.id;
    const title=this.doc.createElement("h3");title.textContent=row.title;card.append(title);
    const info=this.doc.createElement("p");info.textContent=`${statuses[row.status]} · 索引 ${indexStates[row.index_state]||row.index_state} · ${row.chunk_count} 块 · ${row.category} · ${row.uploader} · v${row.version}`;card.append(info);
    const meta=this.doc.createElement("p");meta.className="muted";meta.textContent=`${row.id} · ${row.updated_at} · 范围 ${row.observed_id||"平台共享"} · 标签 ${(row.tags||[]).join("、")}`;card.append(meta);
    if(row.index_error){const error=this.doc.createElement("p");error.textContent="索引未完成："+row.index_error;card.append(error);}
    const actions=[["查看原文",()=>this.view(row)]];
    if(row.status!=="deleted")actions.push(["编辑",()=>this.fetchEdit(row)],["重建索引",()=>this.reindex(row)],["删除",()=>this.remove(row)]);
    for(const [name,action] of actions){const b=this.doc.createElement("button");b.textContent=name;b.onclick=action;card.append(b);}
    this.el("list").append(card);
  }
  async view(row){await openKnowledge(this.api,this.doc,`/v1/knowledge/${encodeURIComponent(row.id)}`);}
  async fetchEdit(row){
    try{const r=await this.api(`/v1/knowledge/${encodeURIComponent(row.id)}`);
      if(r.status!==200)throw Error(r.data?.error||`HTTP ${r.status}`);this.edit(r.data);
    }catch(error){this.el("note").textContent="原文读取失败："+error.message;}
  }
  edit(row){
    if(this.busy)return;this.current=row;this.el("editor").hidden=false;
    for(const name of ["title","content","uploader","category","status"])
      this.el(name).value=row?.[name]||({uploader:"admin",category:"运维知识",status:"review"}[name]||"");
    this.el("tags").value=(row?.tags||[]).join(", ");this.el("scope").value=row?.observed_id||"";
    this.el("editor-note").textContent=row?`${row.id} · v${row.version}；编辑后需重新索引` : "新增知识默认为审核；切换为生效后，索引就绪才参与检索";
    this.el("editor").scrollIntoView({block:"start"});
  }
  async save(){
    if(this.busy||!this.el("editor").reportValidity())return;this.busy=true;this.el("save").disabled=true;
    try{
      const values=Object.fromEntries(["title","content","uploader","category","tags","scope","status"].map(k=>[k,this.el(k).value]));
      const document=formData(values),editing=!!this.current;
      const r=await this.api(editing?`/v1/knowledge/${encodeURIComponent(this.current.id)}`:"/v1/knowledge",
        {method:editing?"PUT":"POST",body:editing?{revision:this.current.revision,document}:document});
      if(r.status!==200)throw Error(r.data?.message||r.data?.error||`HTTP ${r.status}`);
      this.el("editor").hidden=true;await this.load(true);
    }catch(error){this.el("editor-note").textContent="保存未完成："+error.message+"；输入已保留";}
    finally{this.busy=false;this.el("save").disabled=false;}
  }
  async mutation(row,kind){
    if(this.busy)return;this.busy=true;
    try{
      const path=`/v1/knowledge/${encodeURIComponent(row.id)}${kind==="reindex"?"/reindex":""}?revision=${encodeURIComponent(row.revision)}`;
      const r=await this.api(path,{method:kind==="reindex"?"POST":"DELETE"});
      if(r.status!==200)throw Error(r.data?.message||r.data?.error||`HTTP ${r.status}`);await this.load(true);
    }catch(error){this.el("note").textContent="操作未完成："+error.message;}
    finally{this.busy=false;}
  }
  reindex(row){return this.mutation(row,"reindex");}
  remove(row){if(window.confirm(`删除知识「${row.title}」？删除后不再参与检索，历史版本保留。`))return this.mutation(row,"delete");}
}
let requestVersion=0;
export async function openKnowledge(api,doc,path,oid=null){
  const generation=++requestVersion;const drawer=doc.getElementById("knowledge-drawer");
  drawer.hidden=false;drawer.querySelector(".knowledge-content").textContent="正在读取原文…";
  try{
    const result=await api(path+(oid?(path.includes("?")?"&":"?")+"observed_id="+encodeURIComponent(oid):""));
    if(generation!==requestVersion||drawer.hidden)return;
    if(result.status!==200)throw Error(result.data?.error||`HTTP ${result.status}`);
    const row=result.data;drawer.querySelector(".knowledge-title").textContent=row.title;
    drawer.querySelector(".knowledge-meta").textContent=`${row.id} · v${row.version} · ${row.uploader} · ${row.category} · ${row.updated_at} · ${statuses[row.current_status||row.status]}${row.historical_version?"（引用历史版本）":""}`;
    drawer.querySelector(".knowledge-content").textContent=row.content;
  }catch(error){if(generation===requestVersion&&!drawer.hidden)drawer.querySelector(".knowledge-content").textContent="原文读取失败："+error.message;}
}
export function bindKnowledgeLinks(api,doc,scope){
  doc.getElementById("knowledge-close").onclick=()=>{requestVersion++;doc.getElementById("knowledge-drawer").hidden=true;};
  doc.addEventListener("click",event=>{
    const anchor=event.target.closest?.("a");if(!anchor)return;const url=new URL(anchor.href,location.href);
    if(url.origin!==location.origin||!/^\/v1\/knowledge\/kb_[a-f0-9]{32}\/versions\/\d+$/.test(url.pathname))return;
    event.preventDefault();openKnowledge(api,doc,url.pathname,scope());
  });
}
