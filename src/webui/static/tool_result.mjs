// One conversion for live cards, restored cards and timeline summaries.
export function formatToolResult(value) {
  if (value === undefined) return {kind:"empty", text:"（这一步没有可展示的结果）"};
  if (value === null) return {kind:"text", text:"null"};
  if (typeof value === "string") {
    if (/^\s*\[object Object\]\s*$/i.test(value)) {
      return {kind:"text",text:"（记录仅保留对象占位文本，无法从该文本恢复原始结构）"};
    }
    if (/^\s*[\[{]/.test(value)) {
      try {
        const parsed=JSON.parse(value);
        if(parsed && typeof parsed === "object") return {kind:"json",text:JSON.stringify(parsed,null,2)};
      } catch { /* Human text and old Python repr remain plain text. Never eval. */ }
    }
    return {kind:"text",text:value};
  }
  if (typeof value === "object") {
    try { return {kind:"json",text:JSON.stringify(value,null,2)}; }
    catch { return {kind:"text",text:"（结果无法序列化，请查看原始记录）"}; }
  }
  return {kind:"text",text:String(value)};
}

export function resultSummary(value,limit=120) {
  if(value === undefined) return "";
  return formatToolResult(value).text.replace(/\s+/g," ").slice(0,limit);
}

export function renderToolResult(box,value,emphasize) {
  const result=formatToolResult(value);
  box.replaceChildren();
  if(result.kind === "json") {
    const pre=box.ownerDocument.createElement("pre");
    pre.className="tool-result-json mono";pre.textContent=result.text;box.appendChild(pre);
  } else if(result.kind === "empty") {
    const span=box.ownerDocument.createElement("span");
    span.className="muted";span.textContent=result.text;box.appendChild(span);
  } else box.innerHTML=emphasize(result.text);
}
