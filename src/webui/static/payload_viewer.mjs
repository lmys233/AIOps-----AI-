// Independent archive reads. Opening a payload never restarts diagnosis.
export const PART_LABELS = {evidence:"原始证据",problems:"问题与候选",observations:"命名观测",history:"验证历史"};

export function bindPayloadViewer(card, body, {api, esc}) {
  const manifest=body.payload_view;
  if (manifest?.mode!=="view") return;
  const panel=document.createElement("details");panel.className="payload-details";
  const sources=body.explanation?.answer?.knowledge?.sources || [];
  const knowledge=body.intent?.name==="KNOWLEDGE_QA" || Boolean(body.explanation?.answer?.knowledge);
  const scopeNote=knowledge ? `本次为知识问答：${sources.length} 条知识引用见回答明细中的「来源 K…」，点击可查看引用版本原文。这里的 ${manifest.counts.evidence} 条现场证据只统计故障排查采集的日志、指标等，不包含知识引用。` : "";
  panel.innerHTML=`<summary>查看留存原始记录 · ${esc(manifest.counts.evidence)} 条现场证据${knowledge ? ` · ${esc(sources.length)} 条知识引用` : ""}</summary>
    <p class="muted">${esc(scopeNote || manifest.note)}</p>
    <p class="muted">原始证据是本轮诊断留存的现场采集记录；其余分区分别保存问题与候选、结构化观测和候选验证过程。工具调用参数与结果见上方工具流程。</p><div class="payload-tabs"></div>
    <p class="payload-status" aria-live="polite"></p><div class="payload-records"></div>
    <button class="ghost payload-more" type="button">加载</button>`;
  card.appendChild(panel);
  const tabs=panel.querySelector(".payload-tabs"),status=panel.querySelector(".payload-status");
  const records=panel.querySelector(".payload-records"),more=panel.querySelector(".payload-more");
  let part="evidence",cursor=null,generation=0,loading=false,loaded=0,done=false,started=false;
  function label() {
    status.textContent=`${PART_LABELS[part]}：已加载 ${loaded} / ${manifest.counts[part]} 项${loading?" · 正在读取…":""}`;
    more.disabled=loading||done;
    more.textContent=loading?"正在加载…":done?"已读取全部留存项":started?"加载后续 20 项":"读取前 20 项";
  }
  async function load() {
    if (loading||done||!panel.open) return;
    const own=++generation,ownPart=part,ownCursor=cursor;let failed=false;loading=true;label();
    try {
      const params=new URLSearchParams({limit:"20",version:manifest.view_id});
      if(ownCursor)params.set("cursor",ownCursor);
      const result=await api(`${manifest.parts[ownPart]}?${params}`);
      if(own!==generation||!panel.open||!card.isConnected)return;
      if(result.stale||result.status!==200)throw new Error(result.data?.message||`HTTP ${result.status}`);
      const data=result.data;
      if(data.view_id!==manifest.view_id||data.part!==ownPart)throw new Error("留存版本或分区不匹配，请刷新后重试");
      for (const item of data.items) {
        const entry=document.createElement("details");
        entry.innerHTML=`<summary>${esc(item.summary||item.problem_id||item.signal||`记录 ${loaded+1}`)}</summary><pre>${esc(JSON.stringify(item,null,2))}</pre>`;
        records.appendChild(entry);loaded++;
      }
      cursor=data.next_cursor;done=!data.has_more;started=true;
    } catch(error) {
      failed=true;
      if(own===generation&&panel.open){status.textContent=`读取失败：${error.message}；可点击重试。`;more.disabled=false;more.textContent="重试读取";}
    } finally {
      if(own===generation){loading=false;if(!failed)label();}
    }
  }
  for(const name of Object.keys(PART_LABELS)) {
    const button=document.createElement("button");button.type="button";button.className="ghost chip";
    button.textContent=`${PART_LABELS[name]} (${manifest.counts[name]})`;
    button.onclick=()=>{if(part===name)return;generation++;part=name;cursor=null;loaded=0;loading=false;done=false;started=false;records.replaceChildren();label();load();};
    tabs.appendChild(button);
  }
  panel.addEventListener("toggle",()=>{if(panel.open){if(!started)load();}else{generation++;loading=false;label();}});
  more.onclick=load;label();
}
