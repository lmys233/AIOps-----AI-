const COPY_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h3"/></svg>';
const DONE_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>';

// Copy the saved answer, independent of table/JSON toggles and collapsed panels.
export function answerCopyText(body, origin = "") {
  const answer = body?.explanation?.answer || body?.answer || {};
  const parts = [String(answer.summary || body?.report || "").trim()];
  const details = Array.isArray(answer.details) ? answer.details : [];
  for (const row of details) {
    if (row && String(row.value ?? "").trim()) parts.push(`${row.label ? `${row.label}\n` : ""}${row.value}`);
  }
  // Older answers may retain sources without the rendered reference rows.
  for (const source of answer.knowledge?.sources || []) {
    if (!details.some(row => row?.label === `来源 ${source.citation}`)) {
      parts.push(`来源 ${source.citation}：${source.title || "知识原文"} · v${source.version ?? "?"}${source.url ? ` (${source.url})` : ""}`);
    }
  }
  const text = parts.filter(Boolean).join("\n\n");
  return origin ? text.replace(/(\]\(|\()\/v1\/knowledge\//g, `$1${origin}/v1/knowledge/`) : text;
}

export async function copyText(text, {clipboard = globalThis.navigator?.clipboard, doc = document} = {}) {
  if (clipboard?.writeText) {
    try { await clipboard.writeText(text); return; } catch { /* Try the user-gesture fallback. */ }
  }
  const active = doc.activeElement, selection = doc.getSelection();
  const ranges = selection ? Array.from({length: selection.rangeCount}, (_, i) => selection.getRangeAt(i).cloneRange()) : [];
  const inputSelection = active && typeof active.selectionStart === "number" ? [active.selectionStart, active.selectionEnd] : null;
  const input = doc.createElement("textarea");
  input.value = text; input.readOnly = true; input.className = "copy-buffer";
  doc.body.appendChild(input);
  try {
    input.select();
    if (!doc.execCommand?.("copy")) throw new Error("复制未完成，请允许剪贴板访问后重试");
  } finally {
    input.remove(); active?.focus?.({preventScroll: true});
    if (inputSelection) active.setSelectionRange(...inputSelection);
    if (!inputSelection && selection && ranges.length) { selection.removeAllRanges(); ranges.forEach(range => selection.addRange(range)); }
  }
}

export function bindAnswerCopy(card, body) {
  const area = document.createElement("div"); area.className = "answer-copy-actions";
  const note = document.createElement("span"); note.className = "answer-copy-status muted";
  note.setAttribute("role", "status"); note.setAttribute("aria-live", "polite");
  const button = document.createElement("button"); button.type = "button"; button.className = "ghost answer-copy";
  button.title = "复制本次回答"; button.setAttribute("aria-label", "复制本次回答"); button.innerHTML = COPY_ICON;
  const text = answerCopyText(body, globalThis.location?.origin || "");
  button.disabled = !text;
  button.addEventListener("click", async () => {
    button.disabled = true; note.textContent = "";
    try {
      await copyText(text); button.innerHTML = DONE_ICON;
      button.title = "已复制"; button.setAttribute("aria-label", "已复制"); note.textContent = "已复制";
    } catch (error) {
      button.innerHTML = COPY_ICON; button.title = "复制失败，点击重试";
      button.setAttribute("aria-label", "复制失败，点击重试"); note.textContent = error.message;
    } finally { button.disabled = false; }
  });
  area.append(note, button); card.appendChild(area);
}
