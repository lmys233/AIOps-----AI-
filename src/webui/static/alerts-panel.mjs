// Structured receipts remain separate from the diagnosis; textContent protects sender text.
export function renderAlertReceipts(container,rows,{openConversation,openIncident}) {
  container.replaceChildren();
  const doc=container.ownerDocument;
  const states={recorded:"已保存，引用后排查",received:"已接收",queued:"待执行",running:"排查中",completed:"排查已结束",failed:"排查失败",
    interrupted:"排查中断",suppressed:"重复抑制",rejected:"未入队",resolved_notice:"发送方恢复通知"};
  if(!rows.length){container.textContent="暂无接收记录";return;}
  for(const row of rows) {
    const record=row.record || {};const labels=record.labels || {};
    const article=doc.createElement("article");article.className="message assistant";
    const text=doc.createElement("p");text.textContent=`${states[row.state] || row.state} · ${labels.service || ""} · ${record.annotations?.summary || labels.alertname || "告警"}`;
    const meta=doc.createElement("p");meta.className="muted";meta.textContent=`${row.received_at || ""} · ${row.receipt_id}${row.error ? " · "+row.error : ""}`;
    article.append(text,meta);
    if(row.incident_id && row.session_id && !["queued","received","running"].includes(row.state)) {
      const button=doc.createElement("button");button.textContent="查看排查对话";
      button.addEventListener("click",()=>openConversation(row.session_id));article.append(button);
      const incident=doc.createElement("button");incident.textContent="查看事故记录";
      incident.addEventListener("click",()=>openIncident(row.incident_id));article.append(incident);
    }
    const details=doc.createElement("details");const summary=doc.createElement("summary");summary.textContent="告警接收字段（脱敏）";
    const pre=doc.createElement("pre");pre.textContent=JSON.stringify(record,null,2);details.append(summary,pre);article.append(details);
    container.append(article);
  }
}
