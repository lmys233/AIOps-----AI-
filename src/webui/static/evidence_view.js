// References always point to the saved incident; never execute a raw_ref.
export const SOURCE_ICONS = { logs: "🧾", metrics: "📈", traces: "🔗", changes: "🔎", topology: "🕸", memory: "📚" };
export const oneLine = value => String(value || "").replace(/\s+/g, " ").trim().slice(0, 220);
export const evidenceHref = (incident, id) => `/v1/incidents/${encodeURIComponent(incident)}/evidence/${encodeURIComponent(id)}`;

export function referenceIds(body) {
  const ids = new Set((body.evidence || []).map(e => e.evidence_id));
  const visit = (value, key = "") => {
    if (key === "evidence_ids" && Array.isArray(value)) value.forEach(id => ids.add(id));
    else if (value && typeof value === "object") Object.entries(value).forEach(([k, v]) => visit(v, k));
  };
  visit(body.explanation?.attribution_v2);
  visit(body.explanation?.answer);
  visit(body.remediation_plans);
  return [...ids].filter(id => typeof id === "string" && id.length > 0 && id.length <= 128).slice(0, 500);
}

export function renderReferences(text, ids, link, emphasize, canonical = id => id) {
  if (!ids.length) return emphasize(text);
  const literal = id => id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const part = ids.sort((a, b) => b.length - a.length).map(literal).join("|");
  const regex = new RegExp(`(?:${part})(?:[、,，\\s]+(?:${part}))*`, "g");
  let result = "", start = 0;
  for (const match of String(text).matchAll(regex)) {
    result += emphasize(String(text).slice(start, match.index));
    const found = match[0].match(new RegExp(part, "g")) || [];
    const seen=new Set();const unique=found.filter(id => { const key=canonical(id);if(seen.has(key))return false;seen.add(key);return true; });
    result += `<span class="evidence-reference-list">${unique.map(link).join("")}</span>`;
    start = match.index + match[0].length;
  }
  return result + emphasize(String(text).slice(start));
}

export function createEvidenceViewer({ api, esc, emphasize }) {
  const cache = new Map();
  const dialog = document.createElement("dialog");
  dialog.className = "evidence-drawer";
  dialog.setAttribute("aria-labelledby", "evidence-title");
  dialog.innerHTML = `<header><h2 id="evidence-title">证据详情</h2><button type="button" aria-label="关闭证据详情">关闭 ×</button></header><div class="evidence-drawer-body" aria-live="polite"></div>`;
  document.body.append(dialog);
  let generation = 0, opener;
  dialog.querySelector("button").onclick = () => dialog.close();
  dialog.addEventListener("click", e => { if (e.target === dialog && e.clientX < dialog.getBoundingClientRect().left) dialog.close(); });
  dialog.addEventListener("close", () => { if (dialog.open) return; generation++; opener?.focus(); });

  function index(body) {
    const view=body.explanation?.answer;
    if (view?.view_version===1) return referenceMap(view.references || []);
    return cache.get(body.incident_id) || new Map((body.evidence || []).map(e => [e.evidence_id, e]));
  }
  function referenceMap(rows) {
    const map=new Map();
    rows.forEach(r => [r.evidence_id,...(r.aliases || [])].forEach(id => map.set(id,r)));
    return map;
  }
  function label(row) { return `${row.icon || SOURCE_ICONS[row.source] || "📄"} ${oneLine(row.summary) || "查看引用的证据（摘要待加载）"}`; }
  function link(id, body) {
    const title = label(index(body).get(id) || {});
    return `<a class="evidence-reference" href="${esc(evidenceHref(body.incident_id, id))}" data-evidence-id="${esc(id)}" title="${esc(title)}"><span>${esc(title)}</span><span aria-hidden="true">↗</span></a>`;
  }
  function details(data) {
    const e = data.evidence || {}, range = e.time_range || {};
    const entity = typeof e.entity === "object" ? e.entity?.name || e.entity?.id : e.entity;
    const fields = [ ["证据 ID", data.evidence_id], ["事故", data.incident_id], ["来源", data.source_label],
      ["服务 / 实体", entity], ["信号", e.signal], ["方向", e.direction], ["时间窗", `${range[0] || range.start || "未知"} → ${range[1] || range.end || "未知"}`],
      ["数值", e.magnitude != null ? `${e.magnitude} ${e.magnitude_unit || ""}` : null], ["基线", e.baseline],
      ["首现", e.onset], ["置信度", e.confidence], ["原始引用", e.raw_ref] ];
    const json = value => `<pre>${esc(JSON.stringify(value, null, 2))}</pre>`;
    return `<p class="evidence-summary">${esc(data.summary || e.summary)}</p><p class="muted">${esc(data.note || "")}</p>`
      + `<dl>${fields.filter(([, v]) => v != null).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(typeof v === "object" ? JSON.stringify(v) : v)}</dd>`).join("")}</dl>`
      + (data.observation ? `<h3>采集内容</h3>${json(data.observation)}` : "")
      + (data.verification ? `<h3>核查内容</h3>${json(data.verification)}` : "")
      + `<details><summary>完整证据记录</summary>${json(e)}</details>`;
  }
  async function open(anchor, body) {
    const current = ++generation;
    opener = anchor;
    const content = dialog.querySelector(".evidence-drawer-body");
    content.innerHTML = `<p class="muted">正在读取留存证据…</p>`;
    if (!dialog.open) dialog.showModal();
    try {
      const response = await api(evidenceHref(body.incident_id, anchor.dataset.evidenceId));
      if (current !== generation || !dialog.open) return;
      content.innerHTML = response.status === 200 ? details(response.data)
        : `<p role="alert">${esc(response.data?.message || "无法读取留存证据，请稍后重试。")}</p>`;
    } catch (_) {
      if (current === generation && dialog.open) content.innerHTML = `<p role="alert">读取证据失败，请关闭后重试。</p>`;
    }
  }
  function bind(root, body) {
    const update = () => root.querySelectorAll("a.evidence-reference").forEach(a => {
      const title = label(index(body).get(a.dataset.evidenceId) || {});
      a.firstElementChild.textContent = title;
      a.title = title;
    });
    root.addEventListener("click", e => {
      const anchor = e.target.closest("a.evidence-reference");
      if (anchor && root.contains(anchor)) { e.preventDefault(); open(anchor, body); }
      if (e.target.closest("button[data-toggle]")) update();
    });
    if (!body.incident_id || !root.querySelector("a.evidence-reference")) return;
    if (body.explanation?.answer?.view_version===1) { update();return; }
    if (cache.has(body.incident_id)) { update(); return; }
    api(`/v1/incidents/${encodeURIComponent(body.incident_id)}/evidence`).then(response => {
      if (response.status !== 200) return;
      cache.set(body.incident_id, referenceMap(response.data.references));
      if (cache.size > 100) cache.delete(cache.keys().next().value);
      update();
    }).catch(() => {});
  }
  return { bind, renderText: (text, body) => renderReferences(text, referenceIds(body), id => link(id, body), emphasize,
      id => index(body).get(id)?.evidence_id || id),
    renderList: body => (body.explanation?.answer?.view_version===1 ? body.explanation.answer.references : body.evidence || [])
      .map(e => link(e.evidence_id, body)).join("") || "（无）" };
}
