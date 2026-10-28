// Warning pages and composer suggestions use server results, never browser name matching.
export function warningScope(workspace,env,cluster) {
  return `${workspace.oid || ""}|${env || "prod"}|${cluster || ""}|${workspace.revision}`;
}

export function commandSegment(text,caret=text.length) {
  let start=0,quote=null,escaped=false;const spans=[];
  for(let i=0;i<text.length;i++) {
    const ch=text[i];if(escaped){escaped=false;continue;}if(ch==="\\"){escaped=true;continue;}
    if(['"',"'","`"].includes(ch)){if(quote===ch)quote=null;else if(!quote)quote=ch;}
    if(ch==="|" && !quote){spans.push([start,i]);start=i+1;}
  }
  spans.push([start,text.length]);const [a,b]=spans.find(([a,b])=>a<=caret && caret<=b) || spans.at(-1);
  return {start:a,end:b,text:text.slice(a,b)};
}

export class WarningList {
  constructor({api,workspace,env,cluster,doc=document,reference,openConversation,isVisible=()=>doc.getElementById('panel-alerts')?.classList?.contains('active')}) {
    Object.assign(this,{api,workspace,env,cluster,doc,reference,openConversation,isVisible});this.epoch=0;this.badgeEpoch=0;this.cursor=null;
    doc.getElementById("warnings-more").addEventListener("click",()=>this.load(false));
  }
  key(){return warningScope(this.workspace(),this.env(),this.cluster());}
  path(){return `/v1/observed-systems/${this.workspace().oid}/warnings`;}
  query(){return new URLSearchParams({env:this.env() || "prod",...(this.cluster()?{cluster:this.cluster()}:{})});}
  badge(total) {
    const el=this.doc.getElementById("alerts-badge");el.textContent=String(total);el.hidden=total===0;
    el.title="当前系统/环境/集群未审阅告警更新";
  }
  reset(){this.epoch++;this.cursor=null;clearInterval(this.timer);this.doc.getElementById("alerts-badge").hidden=true;this.doc.getElementById("warnings-list").replaceChildren();this.doc.getElementById("warnings-more").hidden=true;this.doc.getElementById("warnings-note").textContent="";}
  start(){this.reset();this.count();this.timer=setInterval(()=>this.count(),10000);}
  async count(){
    if(!this.workspace().oid)return;const key=this.key(),epoch=this.epoch,badgeEpoch=this.badgeEpoch;
    try {const r=await this.api(this.path()+"/count?"+this.query());if(r.stale || key!==this.key() || epoch!==this.epoch)return;
      if(r.status!==200)throw new Error(`HTTP ${r.status}`);if(badgeEpoch===this.badgeEpoch)this.badge(r.data.unreviewed);
    } catch {if(key===this.key() && epoch===this.epoch)this.doc.getElementById("alerts-badge").title="未审阅告警数量暂时不可读";}
  }
  async load(reset=true){
    if(!this.workspace().oid)return;if(reset){this.epoch++;this.cursor=null;}
    const epoch=this.epoch,key=this.key(),query=this.query();query.set("limit","20");if(this.cursor)query.set("cursor",this.cursor);
    const more=this.doc.getElementById("warnings-more");more.disabled=true;
    try {
      const r=await this.api(this.path()+"?"+query);if(r.stale || epoch!==this.epoch || key!==this.key())return;
      if(r.status!==200)throw new Error(`HTTP ${r.status}`);
      const list=this.doc.getElementById("warnings-list");if(reset)list.replaceChildren();
      for(const row of r.data.items)this.append(list,row,r.data.snapshot);
      this.cursor=r.data.next_cursor;more.hidden=!this.cursor;this.badge(r.data.unreviewed);
      this.doc.getElementById("warnings-note").textContent=`已加载 ${list.children.length} / ${r.data.total} 条（本次快照）`;
      if(!r.data.total)list.textContent="当前范围暂无告警；这不代表系统没有异常。";
      if(this.isVisible() && Number.isSafeInteger(r.data.snapshot)) {
        this.badgeEpoch++;const badgeEpoch=this.badgeEpoch;
        try {
          const ack=await this.api(this.path()+"/review",{method:"POST",body:{snapshot:r.data.snapshot,env:this.env() || "prod",cluster:this.cluster() || null}});
          if(ack.stale || epoch!==this.epoch || key!==this.key() || badgeEpoch!==this.badgeEpoch)return;
          if(ack.status!==200)throw new Error(`HTTP ${ack.status}`);this.badgeEpoch++;this.badge(ack.data.unreviewed);
        } catch(error) {if(epoch===this.epoch && key===this.key())this.doc.getElementById("warnings-note").textContent+="；审阅状态未保存，请稍后刷新。";}
      }
    } catch(error){if(epoch===this.epoch && key===this.key())this.doc.getElementById("warnings-note").textContent=`告警列表未加载：${error.message}`;}
    finally {if(epoch===this.epoch && key===this.key())more.disabled=false;}
  }
  append(list,row,snapshot) {
    const d=this.doc,card=d.createElement("article");card.className="warning-card";
    const title=d.createElement("h3");title.textContent=row.name;
    const meta=d.createElement("p");meta.className="muted";meta.textContent=`${row.service} · ${row.status==="resolved"?"发送方已恢复":"发送方告警中"} · ${row.occurred_at} · ${row.location || "位置未提供"}`;
    const content=d.createElement("p");content.textContent=row.content;
    const id=d.createElement("code");id.textContent=row.warning_id;
    const cite=d.createElement("button");cite.textContent="引用并排查";cite.addEventListener("click",()=>this.reference(row));
    const details=d.createElement("details"),summary=d.createElement("summary"),pre=d.createElement("pre");
    summary.textContent=`查看同快照原文与状态 · ${row.notification_count} 次通知`;
    pre.textContent="展开后读取原文";details.append(summary,pre);card.append(title,meta,content,id,cite,details);
    let loaded=false;const key=this.key();
    details.addEventListener("toggle",async()=>{
      if(!details.open || loaded)return;loaded=true;pre.textContent="正在读取原文…";
      const query=this.query();if(snapshot!==undefined)query.set("snapshot",String(snapshot));
      try{const result=await this.api(this.path()+"/"+encodeURIComponent(row.warning_id)+"?"+query);
        if(result.stale || key!==this.key())return;if(result.status!==200)throw new Error(`HTTP ${result.status}`);
        pre.textContent=JSON.stringify(result.data,null,2);
      }catch(error){if(key===this.key()){pre.textContent=`原文未加载：${error.message}`;loaded=false;}}
    });
    if(row.incident_id && !["queued","received","running"].includes(row.state)) {
      const open=d.createElement("button");open.textContent="查看自动排查对话";open.addEventListener("click",()=>this.openConversation(row.session_id));card.append(open);
    }
    list.append(card);
  }
}

export class CommandComposer {
  constructor({api,workspace,env,cluster,input,container,timezone=()=>Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai"}) {
    Object.assign(this,{api,workspace,env,cluster,input,container,timezone});this.revision=0;this.closed=false;
    input.addEventListener("input",()=>this.schedule());
    input.addEventListener("keydown",event=>{if(event.key==="Escape"){this.reset();this.closed=true;}});
  }
  key(){return warningScope(this.workspace(),this.env(),this.cluster());}
  reset(){this.revision++;clearTimeout(this.timer);this.abort?.abort();this.container.replaceChildren();this.container.hidden=true;}
  schedule(){this.reset();this.closed=false;if(!this.workspace().oid || !/@\/\w+/.test(this.input.value))return;
    const part=commandSegment(this.input.value,this.input.selectionStart).text;
    if(/@\/select\b/.test(part)){this.show("按时间、位置和内容筛选上步结果");return;}
    if(!/@\/warn\b/.test(part))return;
    this.show("正在查询名称/ID候选…");this.timer=setTimeout(()=>this.load(),160);
  }
  show(text){this.container.hidden=false;this.container.textContent=text;}
  async load(){
    const revision=this.revision,key=this.key(),text=this.input.value;this.abort=new AbortController();
    const segment=commandSegment(text,this.input.selectionStart),argument=segment.text.split(/@\/warn\b/)[1]?.trim() || "";
    try {
      const r=await this.api(`/v1/observed-systems/${this.workspace().oid}/commands/suggest`,{method:"POST",signal:this.abort.signal,
        body:{q:argument,env:this.env() || "prod",cluster:this.cluster(),limit:20}});
      if(r.stale || revision!==this.revision || key!==this.key() || text!==this.input.value)return;
      if(r.status!==200){this.show(r.data?.error || `匹配未完成（HTTP ${r.status}）`);return;}
      if(r.data.question){this.show(r.data.question);return;}
      this.show(r.data.total ? `名称/ID候选 ${r.data.total} 条；发送后模型理解条件` : "暂无名称/ID候选；发送后模型理解条件");
      const d=this.container.ownerDocument;
      for(const row of r.data.items || []) {
        const button=d.createElement("button");button.className="command-suggestion";button.type="button";
        button.textContent=`${row.name} · ${row.service} · ${row.occurred_at}`;
        button.title=`${row.warning_id}\n${row.content}`;button.addEventListener("click",()=>this.choose(row));this.container.append(button);
      }
    } catch(error){if(revision===this.revision && key===this.key())this.show("告警提示暂不可用；可以提交完整命令查看具体错误。");}
  }
  choose(row){
    // Replace only the current command segment, preserving the rest of the pipeline.
    const segment=commandSegment(this.input.value,this.input.selectionStart),offset=segment.text.indexOf("@/warn");if(offset<0)return;
    const start=segment.start+offset,suffix=this.input.value.slice(segment.end);
    this.input.value=this.input.value.slice(0,start)+`@/warn ${row.warning_id} `+suffix;
    this.input.setSelectionRange(start+7+row.warning_id.length,start+7+row.warning_id.length);
    this.reset();this.input.focus();
  }
}
