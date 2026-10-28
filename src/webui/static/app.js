// AIOps Web 台（T34 · 零构建：原生 ES module，无任何外部依赖）
// ★ 两条铁律：① 所有数据来自已有契约端点（不新增业务语义）；
//             ② 一律转义后插入 DOM（服务端文本里可能有 HTML/脚本 —— 直接 innerHTML 就是注入）。

const $ = (id) => document.getElementById(id);
import { createHistoryPager } from "./history_paging.js";
import { createEvidenceViewer } from "./evidence_view.js";
import { Workspace } from "./workspace.mjs";
import {setupLegacy} from "./legacy-history.mjs";
import {bindPayloadViewer} from "./payload_viewer.mjs";
import {createToolFlow} from "./tool_flow.mjs";
import {createHistoryRenderer} from "./history_renderer.mjs";
import {createWorkspaceRequests} from "./workspace_requests.mjs";
import {SessionDirectoryCache, SessionLoader} from "./session_loading.mjs";
import {renderAlertReceipts} from "./alerts-panel.mjs";
import {WarningList,CommandComposer} from "./warnings.mjs";
import {bindFeedback} from "./feedback.mjs";
import {bindAnswerCopy} from "./answer_copy.mjs";
import {appendTurnDuration} from "./turn_duration.mjs";
let workspace = null;
const evidenceViewer = createEvidenceViewer({ api, esc, emphasize });
const state = { capabilities: null, lastIncident: null, lastResponse: null, lastAsk: "",
                sessionId: null, sessions: [] };

const token = () => ($("token").value || "").trim();
const envValue = () => ($("env").value || "").trim() || null;
const clusterValue = () => ($("cluster").value || "").trim() || null;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}
function kv(pairs) {
  const rows = pairs.filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("");
  return rows ? `<dl class="kv">${rows}</dl>` : "";
}
// ★ 状态徽标用**中文**（T38）：`abstained` 这种英文值只在折叠区出现 ——
//   用户不需要知道契约里的枚举名。
function advisorStatus(body) {
  return body?.explanation?.advisor_outcome?.status
    || body?.explanation?.answer?.advisor_status || body?.answer?.advisor_status || "";
}
function statusLabel(status, body = null) {
  const view=body?.explanation?.answer || body?.answer;
  if (view?.view_version===1 && view.status_label) return view.status_label;
  const dialogue = body?.explanation?.dialogue;
  if (dialogue?.completed) {
    const labels = { explain_result: "已说明判断依据", explain_gaps: "已说明证据缺口",
      submit_evidence: "已接收待核验资料", compare_candidates: "已比较候选原因" };
    if (labels[dialogue.action]) return labels[dialogue.action];
  }
  if (advisorStatus(body) === "plans_ready") return "已提供修复方案";
  if (advisorStatus(body) === "needs_evidence") return "需要补充证据";
  const diagnosis = body?.explanation?.attribution_v2 || body?.explanation?.answer?.attribution_v2;
  if (diagnosis?.problems?.length && status !== "failed") {
    const confirmed = diagnosis.problems.filter(p => p.cause?.status === "confirmed").length;
    return confirmed === diagnosis.problems.length ? "问题原因已确认" : "已发现问题，原因待核查";
  }
  return { concluded: "已定位根因", clarify: "需要补充信息", abstained: "未得出结论",
           budget_exhausted: "预算用尽", rejected: "已拒绝",
           failed: "系统错误" }[status] || "已结束";
}
function badge(text, kind) { return `<span class="badge ${kind}">${esc(text)}</span>`; }
function setTrace(id) { $("trace").textContent = `trace: ${id || "—"}`; }

const requestClient=createWorkspaceRequests({workspace:()=>workspace,token});
function api(path,options) {return requestClient.request(path,options);}

import { ObservedSystems } from "./observed-systems.mjs";
const observedSystems = new ObservedSystems(api);
observedSystems.init();
workspace=new Workspace(api,enterWorkspace,leaveWorkspace);
const warningList=new WarningList({api,workspace:()=>workspace,env:envValue,cluster:clusterValue,
  reference(row){workspace.panel("chat");$("message").value=`@/warn ${row.warning_id}`;$("message").focus();commandComposer.schedule();},
  async openConversation(sid){workspace.panel("chat");await openSession(sid);}});
const commandComposer=new CommandComposer({api,workspace:()=>workspace,env:envValue,cluster:clusterValue,input:$("message"),container:$("command-preview")});
for(const id of ["env","cluster"])$(id).addEventListener("change",()=>{warningList.start();commandComposer.schedule();
  if($("panel-alerts").classList.contains("active"))loadAlertReceipts();});
import {IntentManager} from "./intent-manager.mjs";
import {KnowledgeManager,bindKnowledgeLinks,renderKnowledgeText} from "./knowledge-manager.mjs";
import {resultSummary} from "./tool_result.mjs";
const knowledgeManager=new KnowledgeManager(api);knowledgeManager.init();bindKnowledgeLinks(api,document,()=>workspace.oid);
$("platform-knowledge").addEventListener("click",()=>{location.hash="knowledge";});
window.addEventListener("hashchange",()=>{if(location.hash==="#knowledge")knowledgeManager.load(true);});
if(location.hash==="#knowledge")knowledgeManager.load(true);
const intentManager=new IntentManager(api);intentManager.init();
$("platform-intents").addEventListener("click",()=>{location.hash="intents";});
$("platform-observed").addEventListener("click",()=>{location.hash="";});
window.addEventListener("hashchange",()=>{if(location.hash==="#intents")intentManager.load();});
if(location.hash==="#intents")intentManager.load();

// 带密钥的请求头（导出走原生 `fetch`：响应是 Markdown 文本，不是 JSON，
// 用 `api()` 会把它当 JSON 解析 —— 那不是"读不到"而是"读成错的形状"）。
function requestHeaders(json = false) {
  const headers = {};
  if (token()) headers["Authorization"] = `Bearer ${token()}`;
  if (json) headers["Content-Type"] = "application/json";
  return headers;
}

// ---------------- 面板切换 ----------------
for (const button of document.querySelectorAll(".nav-item")) {
  button.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b === button));
    document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === `panel-${button.dataset.panel}`));
    if(button.dataset.panel==="alerts")loadAlertReceipts();
  });
}

// ---------------- 状态条（/health） ----------------
async function refreshStatus() {
  const { status, data } = await api("/health");
  const ok = status === 200;
  $("badge-status").className = `badge ${ok ? "ok" : "bad"}`;
  $("badge-status").textContent = ok ? "服务在线" : `服务异常（${status}）`;
  const parts = [];
  if (data) {
    if (data.service) parts.push(`服务=${data.service}`);
    if (data.model_enabled !== undefined) parts.push(`模型=${data.model_enabled ? "已接" : "未接（仅规则层）"}`);
    if (data.auth_enabled !== undefined) parts.push(`鉴权=${data.auth_enabled ? "开" : "关"}`);
    for (const note of (data.degraded || [])) parts.push(`降级：${note}`);
  }
  $("status-detail").textContent = parts.join(" · ") || "无法读取 /health";
}

// ---------------- 能力总览（只读 BFF） ----------------
async function loadCapabilities() {
  if (!workspace?.oid) return;
  const { status,data,stale } = await api("/webui/api/capabilities");
  if(stale)return;
  state.capabilities = data;
  if (status!==200 || !Array.isArray(data?.intents)) { $("caps").textContent = `能力表读取失败：HTTP ${status}`; return; }
  const intentRows = data.intents.map((item) => `<tr><td class="mono">${esc(item.name)}</td>
      <td class="mono">${esc((item.unlocked_tools || []).join(", ") || "（无）")}</td></tr>`).join("");
  const errorRows = (data.error_codes || []).map((item) => `<tr>
      <td class="mono">${esc(item.code)}</td>
      <td>${item.retryable ? '<span class="ok">可重试</span>' : '<span class="muted">不可重试</span>'}</td>
      <td class="mono">${esc(item.disposition)}</td></tr>`).join("");
  const endpointRows = (data.endpoints || []).map((item) => `<tr>
      <td class="mono">${esc(item.method)}</td><td class="mono">${esc(item.path)}</td><td>${esc(item.what)}</td></tr>`).join("");
  $("caps").innerHTML = `
    <div class="card"><h3>本系统已登记 SDK 能力</h3>${(data.registered_services || []).map((s)=>`<details><summary>${esc(s.service)} · ${s.active ? "已登记" : "已停用"}</summary>${(s.instances || []).map((i)=>`<p>${esc(i.env)} · ${esc(i.instance)} · 最近登记 ${esc(i.last_registered_at)}</p><ul>${(i.capabilities || []).map((c)=>`<li>${esc(c.method)} ${esc(c.path)} · v${esc(c.version)} · ${c.enabled ? "已申报" : "未启用"}</li>`).join("")}</ul>`).join("")}</details>`).join("") || "没有登记的服务能力"}</div>
    <div class="card"><h3>意图 → 解锁工具 <span class="muted mono">${data.intents.length} 类</span></h3>
      <table><tr><th>意图</th><th>解锁工具</th></tr>${intentRows}</table></div>
    <div class="card"><h3>错误码与处置 <span class="muted mono">${(data.error_codes || []).length} 个</span></h3>
      <table><tr><th>错误码</th><th>可重试</th><th>处置倾向</th></tr>${errorRows}</table></div>
    <div class="card"><h3>对外端点</h3><table><tr><th>方法</th><th>路径</th><th>作用</th></tr>${endpointRows}</table></div>`;
}

// ---------------- 对话：事件时间线 ----------------
const timeline = $("timeline");
const {beginTools,toolCard,resetTools,finishTools}=createToolFlow({timeline,esc,emphasize});
const {restoredTurnNodes,renderRestoredTurns,restoredAnswerBody,renderRestoredSteps}=
  createHistoryRenderer({timeline,esc,messageBubble,answerCardNode,bindExport,statusLabel,
    pushEvent,summarizeEvent,scrollTimelineToBottom,toolCard});
const historyPager = createHistoryPager({ container: timeline,
  fetchPage: (id, cursor) => api(`/v1/sessions/${encodeURIComponent(id)}?before=${encodeURIComponent(cursor)}`),
  renderTurn: restoredTurnNodes, notify: sessionNote });

// ★★ T37（用户指示）：一轮对话**只显示答案** —— 过程行先收进 `turnProcess`，
//    答案卡生成时整块折进卡片的 `details.reasoning`（**默认收起，可手动展开**）。
//    流式期间另有一行"进行中"提示留在顶部实时更新（否则 20~30s 的排查看起来像卡死）。
let turnProcess = null;

function beginTurn() {
  turnProcess = document.createElement("div");
  turnProcess.className = "turn-process";
}
function takeTurnProcess() {
  const node = turnProcess || document.createElement("div");
  turnProcess = null;
  return node;
}
function flushTurnProcess() {
  // ★ 异常 / HTTP 回退这些**没有答案卡**的路径：过程不能凭空消失，摊回时间线。
  if (turnProcess && turnProcess.childNodes.length) timeline.appendChild(turnProcess);
  turnProcess = null;
}

function pushEvent(name, summary, container = null) {
  const row = document.createElement("div");
  row.className = "ev";
  row.innerHTML = `<div class="ev-name">${esc(name)}</div><div>${esc(summary)}</div>`;
  (container || turnProcess || timeline).appendChild(row);   // ★ T37：过程进容器，不进时间线
  if (!container) timeline.scrollTop = timeline.scrollHeight;
  return row;
}
function clearTimeline() { state.historyOpen = (state.historyOpen || 0) + 1; historyPager.reset(); timeline.innerHTML = ""; turnProcess = null; resetTools(); }

// ---------------- ★★ T54：把每个环节显示成一次 tool call（**流式、边跑边显示**） ----------------
// 用户指示（2026-10-03）："每次输入后系统的操作都要透明化……每个环节当成 tool，
// 调用就是 tool call。比如：'查询：查询什么（展开）'，展开后显示参数，完成后补上结果。
// 注意：**要流式的效果，而不是完成后给出 tool call 列表**。"
// 后端每完成一步就成对发 `tool` 事件（`phase=start` 带参数 / `phase=done` 带结果），
// 这里按**到达顺序**逐张追加卡片；`done` 到达时**就地**把结果补进同一张卡。

const sessionKey = () => workspace.key("session_id");
const sessionLoader = new SessionLoader({ api,
  cache: new SessionDirectoryCache(localStorage, () => workspace.key("session_directory_v1")),
  current: conversationId, select: setSessionId,
  list: items => { state.sessions = items; renderSessions(items); },
  page: data => adoptSession(data.session, data.turns, data.degraded, "已恢复历史会话", data),
  pending: loading => {
    state.historyLoading = loading;
    if (loading) { clearTimeline(); sessionNote("正在加载所选会话的历史…"); }
    syncConversationControls();
  }, error: sessionNote, listError: message => { $("session-list-note").textContent = message; } });

// 当前会话 id（会话号本身由服务端分配）；没有就是 null（**不编造**，交给服务端新建）
function conversationId() { return state.sessionId || null; }

function syncConversationControls() {
  const busy = !!state.chatBusy || !!state.sessionCreating || !workspace.row?.active;
  $("send").disabled = busy || !!state.historyLoading ||
    !!(conversationId() && sessionLoader.loadedId !== conversationId());
  $("session-new").disabled = busy; $("clear").disabled = busy;
}

// 只动指针：会话号本身来自服务端，清指针 = 下一轮由服务端新建
function setSessionId(id) {
  state.sessionId = id ? String(id) : null;
  try {
    if (state.sessionId) localStorage.setItem(sessionKey(), state.sessionId);
    else localStorage.removeItem(sessionKey());
  } catch { /* Browser storage may be unavailable; server session remains usable. */ }
  highlightCurrentSession();
}

// 丢掉当前会话指针（下一个会话号由服务端给）：删除当前会话后回退、或明确要换一条时用
function resetConversation() { setSessionId(null); }

// 会话区的提示行：失败、降级、记忆不可读都如实写出来（不许静默）
function sessionNote(text, kind = "") {
  const box = $("session-note");
  if (!box) return;
  box.className = `sessions-note ${kind}`.trim();
  box.textContent = text || "";
}

// 相对时间：列表里显示「3 分钟前」这类，用户一眼能判断新旧
function timestampMs(value) {
  const text = String(value || "");
  if (/^\d+(\.\d+)?$/.test(text)) return Number(text) * (Number(text) < 1e12 ? 1000 : 1);
  return Date.parse(text);
}

function relativeTime(value) {
  const ms = timestampMs(value);
  if (!value || Number.isNaN(ms)) return "—";
  const diff = Date.now() - ms;
  if (diff < 60000) return "刚刚";
  if (diff < 3600000) return `${Math.floor(diff / 60000)} 分钟前`;
  if (diff < 86400000) return `${Math.floor(diff / 3600000)} 小时前`;
  if (diff < 2592000000) return `${Math.floor(diff / 86400000)} 天前`;
  return new Date(ms).toLocaleString("zh-CN", { hour12: false });
}

// 绝对时间戳：恢复回来的每一轮都带时间，用户能判断新旧
function stamp(value) {
  const ms = timestampMs(value);
  return (!value || Number.isNaN(ms)) ? "时间未知" : new Date(ms).toLocaleString("zh-CN", { hour12: false });
}

// 标题取不到时用最近一条消息的前 20 字顶替（列表接口给的是 last_message）
function sessionTitle(item) {
  const title = String((item && item.title) || "").trim();
  if (title) return title;
  const last = String((item && item.last_message) || "").replace(/\s+/g, " ").trim();
  if (!last) return "（未命名会话）";
  return last.length > 20 ? `${last.slice(0, 20)}…` : last;
}

// 当前会话高亮（只切 class，避免为了高亮把整个列表重排）
function highlightCurrentSession() {
  const id = conversationId();
  for (const li of $("session-list").querySelectorAll(".session-item"))
    li.classList.toggle("active", li.dataset.id === id);
}

// 拉会话列表并渲染（最近活跃在前的顺序由服务端给）
async function loadSessions() {
  return sessionLoader.refresh();
}

function renderSessions(items) {
  const box = $("session-list");
  const current = conversationId();
  // ★ 还没有会话号时，列表顶上摆一条**不可点**的「新会话」：用户能看出"当前是一条还没落库的新会话"，
  //   而不是以为界面坏了（首屏不建空会话，所以列表里本来就没有它）。
  const pending = current ? "" : `<li class="session-item active pending">
      <div class="session-title">新会话</div>
      <div class="session-meta mono">还没有会话号（发第一条消息时由服务端分配）</div>
    </li>`;
  if (!items.length) {
    box.innerHTML = pending || `<li class="session-empty">还没有会话（发一条消息，或点「＋ 新建会话」）</li>`;
    return;
  }
  box.innerHTML = pending + items.map((item) => {
    const id = String(item.session_id || "");
    const when = relativeTime(item.last_turn_at || item.updated_at || item.created_at);
    const turns = Number(item.turn_count || 0);
    return `<li class="session-item${id && id === current ? " active" : ""}" data-id="${esc(id)}">
      <div class="session-title" title="双击改名：${esc(sessionTitle(item))}">${esc(sessionTitle(item))}</div>
      <div class="session-meta mono">${esc(when)} · ${esc(turns)} 轮</div>
      <div class="session-ops">
        <button class="ghost chip" data-rename title="重命名">✎</button>
        <button class="ghost chip" data-delete title="删除这条会话">🗑</button>
      </div>
    </li>`;
  }).join("");
  for (const li of box.querySelectorAll(".session-item")) {
    const id = li.dataset.id;
    if (!id) continue;                       // 「新会话」占位条没有可点的操作
    const item = items.find((it) => String(it.session_id || "") === id) || {};
    const title = li.querySelector(".session-title");
    const rename = li.querySelector("[data-rename]");
    const remove = li.querySelector("[data-delete]");
    if (title) title.addEventListener("dblclick", () => startRename(li, id, item));
    if (rename) rename.addEventListener("click", (event) => {
      event.stopPropagation();
      startRename(li, id, item);
    });
    if (remove) remove.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteSession(id, sessionTitle(item));
    });
    li.addEventListener("click", (event) => {
      if (event.target.closest("input, button")) return;   // 改名/删除的点击不当作"切换"
      if (id !== conversationId() || sessionLoader.loadedId !== id) openSession(id);
    });
  }
}

// 原地改名（铅笔按钮或双击标题）：回车提交、Esc 取消、失焦按提交处理
function startRename(li, id, item) {
  const box = li.querySelector(".session-title");
  if (!box || li.querySelector(".session-rename")) return;
  const input = document.createElement("input");
  input.className = "session-rename";
  input.value = String((item && item.title) || "").trim();
  input.placeholder = sessionTitle(item);
  box.replaceWith(input);
  input.focus();
  input.select();
  let settled = false;                       // 回车会连带触发失焦，只认第一次
  const commit = async (save) => {
    if (settled) return;
    settled = true;
    const title = input.value.trim();
    if (!save || !title || title === String((item && item.title) || "").trim()) { renderSessions(state.sessions); return; }
    const { status, data,stale } = await api(`/v1/sessions/${encodeURIComponent(id)}?include_history=false`, { method: "PATCH", body: { title } });
    if(stale)return;
    sessionNote(status === 200 ? `已重命名为「${title}」` : `重命名失败（HTTP ${status}）`, status === 200 ? "ok" : "bad");
    if (status === 200 && data?.session) sessionLoader.update(data.session);
    else renderSessions(state.sessions);
  };
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); commit(true); }
    if (event.key === "Escape") { event.preventDefault(); commit(false); }
  });
  input.addEventListener("blur", () => commit(true));
}

// 删除会话：**要确认**（不可撤销），并把"记忆删了、事故留着"说清楚
async function deleteSession(id, title) {
  if (!id) return;
  if (state.chatBusy && id === conversationId()) { sessionNote("请等当前回答完成后再删除当前会话", "warn"); return; }
  const ok = window.confirm(`删除会话「${title}」？\n\n该会话的历史正文和近期记忆会一并删除；\n事故记录（审计留痕）会保留，不受影响。`);
  if (!ok) return;
  const { status, data,stale } = await api(`/v1/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
  if(stale)return;
  if (status !== 200 && status !== 204) {
    sessionNote(`删除失败（HTTP ${status}）${status === 404 ? "：这条会话已经不在了" : ""}`, "bad");
    await loadSessions();
    return;
  }
  // ★ 本地表删了、记忆没删掉（或没删接口）→ 服务端会带 `degraded`，照说出来
  const notes = ((data && data.degraded) || []).filter(Boolean);
  sessionLoader.remove(id);
  sessionNote(notes.length ? `已删除会话「${title}」：${notes.join("；")}`
                           : `已删除会话「${title}」（事故记录保留）`, notes.length ? "warn" : "ok");
  if (id === conversationId()) {
    sessionLoader.activate(null);
    resetConversation();                     // 删掉的是当前会话 → 退回服务端记的「上次会话」（只读，不新建）
    clearTimeline();
    await restoreSession();
    return;
  }
  await loadSessions();
}

// 采纳一条会话：设为当前 + 渲染近期记忆 + 滚到最新（切换 / 恢复 / 删除后回退共用）
function adoptSession(session, turns, degraded, headline, page = {}) {
  const id = String((session && session.session_id) || "");
  if (!id) return null;
  setSessionId(id);
  clearTimeline();
  renderRestoredTurns(turns || [], degraded || [], headline || "已恢复历史会话");
  historyPager.init(id, { ...page, turns });
  const notes = (degraded || []).filter(Boolean);
  sessionNote(notes.length ? `历史恢复说明：${notes.join("；")}` : "", notes.length ? "warn" : "");
  scrollTimelineToBottom();
  return id;
}

// 启动/刷新：先渲染本地目录；目录刷新与所选历史独立，没有选择时再用服务端目录。
// ★★ 这里**只读不写**：首屏**不**新建会话。空会话（没有任何一轮）不该占用户列表里的一行。
async function restoreSession() {
  let stored = null;
  try { stored = localStorage.getItem(sessionKey()); } catch {}
  return sessionLoader.start(stored);
}

// 要发消息了才要会话号：没有就**这时**请服务端铸一个（先拿到 id，再发 /v1/chat）
async function ensureSession() {
  if (state.historyLoading) return null;
  if (state.sessionId) return sessionLoader.loadedId === state.sessionId ? state.sessionId : null;
  return createSession({ announce: false });
}

// 新建会话：会话号由服务端分配（201）→ 切过去 → **清空对话区**（不沿用旧会话的消息）
async function createSession({ announce = true } = {}) {
  if (!workspace.row?.active || state.sessionCreating || (state.chatBusy && announce)) return null;
  // Invalidate earlier history/list auto-selection before creating another conversation.
  sessionLoader.activate(null); resetConversation(); clearTimeline();
  const selection = sessionLoader.historySeq;
  const revision=workspace.revision;
  state.sessionCreating = true; syncConversationControls();
  try {
  const { status, data,stale } = await api("/v1/sessions?include_history=false", { method: "POST", body: {} });
  if(stale || selection !== sessionLoader.historySeq)return null;
  const session = (data && data.session) || null;
  if ((status !== 201 && status !== 200) || !session || !session.session_id) {
    sessionNote(`新建会话失败（HTTP ${status}）：会话号必须由服务端分配，前端不自己拼`, "bad");
    return null;
  }
  const id = String(session.session_id);
  sessionLoader.activate(id); sessionLoader.update(session);
  clearTimeline();
  if (announce) pushEvent("webui", `已新建会话：${id}（会话号由服务端分配）`);
  // ★ 服务端说"有界淘汰/记忆降级"时必须照说，不许把它当成成功就完事
  const notes = (data.degraded || []).filter(Boolean);
  if (notes.length) sessionNote(notes.join("；"), "warn");
  await markSelected(id);
  if(revision!==workspace.revision)return null;
  scrollTimelineToBottom();
  return id;
  } catch (error) {
    if (revision === workspace.revision) sessionNote(`新建会话失败：${error.message}`, "bad");
    return null;
  } finally {
    if (revision === workspace.revision) { state.sessionCreating = false; syncConversationControls(); }
  }
}

// 点击列表切换会话：读该会话的近期记忆 → 整体替换对话区 → 记为「上次会话」
async function openSession(id) {
  if (state.chatBusy) { sessionNote("请等当前回答完成后再切换会话", "warn"); return; }
  const revision=workspace.revision;
  const loaded = await sessionLoader.open(id);
  if(revision!==workspace.revision || conversationId() !== id || !loaded)return;
  await markSelected(id);
}

// 把当前会话记成「上次会话」（刷新后才能回到这条）
async function markSelected(id) {
  if (!id) return;
  const { status,stale } = await api(`/v1/sessions/${encodeURIComponent(id)}/select?include_history=false`, { method: "POST", body: {} });
  if(stale || conversationId() !== id)return;
  if (status !== 200 && status !== 204) sessionNote(`设为上次会话失败（HTTP ${status}）—— 刷新后可能回不到这条`, "warn");
}

// 一轮问答之后：列表刷新（标题/时间/轮次跟着变）+ 记为「上次会话」
async function afterTurn() {
  const revision=workspace.revision;
  try {
    await markSelected(conversationId());
    if(revision!==workspace.revision)return;
    await loadSessions();
  } catch (exc) {
    sessionNote(`会话列表刷新失败：${String(exc)}`, "warn");
  }
}

// 恢复的历史对话：用户话在左、AI 摘要在右，两边都标「历史」+ 时间
// ★★ 渲染完必须**停在最新一条**，而且**只动对话区自己那个滚动条**。
//    以前这里还调了 `last.scrollIntoView({block:"end"})` —— 它会连同**所有可滚动祖先**
//    一起滚（页面、侧栏），于是"定位到最新对话"变成了"把侧栏的会话列表顶出视野"。
//    现在只赋 `scrollTop`，容器之外一概不动；等一帧再来一次，避免换行/图片把位置顶走。
function scrollTimelineToBottom() {
  const toBottom = () => { timeline.scrollTop = timeline.scrollHeight; };
  toBottom();
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(toBottom);
}

// ★★ 一条对话消息（**收发同形**）：用户在左、AI 在右。
//    发送与"刷新后恢复历史"必须**长得一样** —— 修前发送走的是上下列表（事件行 + 卡片）、
//    恢复走的是左右气泡，同一段对话刷新前后两种样子（用户实测反馈）。
function messageBubble(kind, text, { at = "", extra = "", expandable = false } = {}) {
  const box = document.createElement("div");
  box.className = `msg msg-${kind}`;
  const who = kind === "user" ? "我" : "AIOps";
  box.innerHTML = `<div class="msg-who">${esc(who)}</div>
    <div class="msg-text">${emphasize(text || "")}</div>
    <div class="msg-at mono">${esc(stamp(at))}</div>`;
  if (extra) box.insertAdjacentHTML("beforeend", extra);
  // ★ AI 的那条**可展开**（结论常显、过程与明细折进 details）——与恢复历史同形
  if (expandable && !box.querySelector("details")) {
    box.insertAdjacentHTML("beforeend",
      `<details class="msg-more"><summary>展开明细</summary>
         <div class="msg-more-body"></div></details>`);
  }
  return box;
}

// 发送时先把用户那句话贴到左边（与恢复历史同一个形态）
function appendUserBubble(text) {
  timeline.appendChild(messageBubble("user", text, { at: new Date().toISOString() }));
  scrollTimelineToBottom();
}

function outcomeCard(body) {
  if (!finishTools(body)) {
    const flow=document.createElement("div");flow.className="tools";
    appendTurnDuration(flow,body);timeline.appendChild(flow);
  }
  const status = body.status || "?";
  const kind = advisorStatus(body) === "plans_ready" || status === "concluded" || status === "answered"
    ? "ok" : (status === "clarify" ? "warn" : "bad");
  const intent = body.intent || {};
  const cost = body.cost || {};
  const degraded = (body.degraded || []).map((d) => `<li>${esc(d)}</li>`).join("");
  // ★★ AI 的那条也在**右侧气泡**里（答案常显、过程折进 `details.reasoning`）——
  //    与"我"的左侧气泡成对；展开入口就是卡片里那个「推理过程」。
  const bubble = messageBubble("ai", "", {
    at: body.finished_at || new Date().toISOString() });
  // ★ T37：把本轮过程折进答案卡（默认收起）。答案在前，过程在后 ——
  //   用户先看到"这次得出了什么"，需要时再展开看"怎么得出的"。
  const process = takeTurnProcess();
  const steps = process.childNodes.length;
  const card = answerCardNode(body, { process, steps });
  bubble.appendChild(card);
  timeline.appendChild(bubble);
  scrollTimelineToBottom();
  bindExport(card, body);          // ★ 答案卡上的「保存为 md 文件」入口
  setTrace(body.trace_id);
  if (body.incident_id) state.lastIncident = body.incident_id;
  return card;
}

// ★★ **一张答案卡，两条路共用**（实时 = `outcomeCard`；刷新恢复 = `aiHistoryBubble`）。
//   修前恢复那条路自己拼了个朴素气泡（只有答案文本 + 一个「展开明细」三行），
//   于是**同一个回答，刷新前后长得不一样**（用户实测："刷新后卡片就变成了…"）。
//   ★ 共用的是**结构**（h3 徽标 + 事故号 / verdict / 导出入口 / 明细 / 推理过程），
//     取值靠"实时响应体"与"恢复拍平体"字段同名（见 `restoredAnswerBody`）。
//   ★ 恢复时实时的逐条事件时间线**没有落库**：能给的是当时记录下来的步骤（工具/答案卡），
//     数量如实显示，并在折叠区里说明这一格与实时的差别（不假装一模一样）。
function answerCardNode(body, { process = null, steps = 0, restored = false, feedback = null, feedback_target = null, feedback_unavailable = false } = {}) {
  const systemScope=body.explanation?.observed_system;
  const status = String(body.status || "?");
  const kind = advisorStatus(body) === "plans_ready" || status === "concluded"
    ? "ok" : (status === "clarify" ? "warn" : "bad");
  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML = `<h3>${badge(statusLabel(status, body), kind)}<span class="mono muted">${esc(body.incident_id || "")}</span></h3>
    <div class="verdict">${verdictBlock(body)}</div>
    <details class="reasoning">
      <summary>${steps ? `推理过程（${steps} 条过程记录）` : "推理过程"}${restored ? ` <span class="muted">· 刷新恢复</span>` : ""}
        <span class="muted">· 包含事件与证据，不代表工具请求次数 · 点击展开：事件时间线 · 意图 · 槽位 · 候选 · 证据 · 成本 · 降级</span></summary>
      <div class="process-slot"></div>
      ${reasoningKv(body, restored)}
    </details>`;
  bindPayloadViewer(card,body,{api,esc});
  if (systemScope?.observed_id) {
    const label=document.createElement("p"); label.className="muted system-scope";
    label.textContent=`被观测系统：${systemScope.observed_name || systemScope.observed_id} · ${systemScope.observed_id}`;
    card.prepend(label);
  }
  // ★ T37：把过程行搬进折叠区（`replaceWith` 会让它从原位置移走）
  const slot = card.querySelector(".process-slot");
  if (process && process.childNodes.length) slot.replaceWith(process); else slot.remove();
  // ★ T42：明细块的**视图切换**（表格 ⇄ JSON）—— 同一份数据，不重新问模型
  bindDetailsToggle(card);
  evidenceViewer.bind(card, body);
  bindFeedback(card,{body,target:feedback_target,feedback,unavailable:feedback_unavailable,api});
  bindAnswerCopy(card,body);
  return card;
}

// 推理过程里的**事实格**（实时与恢复共用同一份取值代码）。
function reasoningKv(body, restored = false) {
  const status = String(body.status || "?");
  const intent = body.intent || {};
  const cost = body.cost || {};
  const explanation = body.explanation || {};
  const answerMeta = explanation.answer || {};
  const degraded = (body.degraded || []).map((d) => `<li>${esc(d)}</li>`).join("");
  const more = (total, shown) => (Number(total) > shown
    ? ` <span class="muted">（共 ${esc(total)} 条，这里列前 ${esc(shown)} 条）</span>` : "");
  const rows = [
    ["状态（原始值）", `<span class="mono">${esc(status)}</span>
      <span class="muted">· 答案来源 <span class="mono">${esc(answerMeta.source || "-")}</span>
      ${answerMeta.note ? esc(answerMeta.note) : ""}</span>`],
    ["意图", `${esc(intent.name || "-")} <span class="muted">信心 ${esc(intent.confidence ?? "-")}（原始 ${esc(intent.confidence_raw ?? "-")}，来源 ${esc(intent.source || "-")}）</span>`],
    ["槽位", `<span class="mono">${esc(JSON.stringify(body.slots || {}))}</span>`],
    ["槽位未指定", `用户没有明说，当前用配置默认值：<span class="mono">${esc((body.unresolved_entities || []).join(", ") || "（无）")}</span>
      <span class="muted">· 字段 <span class="mono">unresolved_entities</span></span>`],
    ["候选假设", (body.hypotheses || []).map((h) => `${h.rank}. ${esc(h.statement || "")}（${esc(h.score)}，${esc(h.status || "-")}）`).join("<br>")
      + more(body.hypotheses_total, (body.hypotheses || []).length) || "（无）"],
    // ★★ BUG-191：证据行必须带**摘要**（人读的那句话），不能只印 id+signal+direction ——
    //   实测用户看到"七条 `metric_snapshot flat`"却看不到**值**，结论区之外
    //   一处人话都没有（值明明查到了：它就在 `evidence[].summary` 里）。
    ["证据", (evidenceViewer.renderList(body)
      + (body.payload_view?.mode==="view" ? `<p class="muted">原始 ${esc(body.evidence_total)} 条证据按需读取，以上是留存引用简述。</p>`
        : more(body.evidence_total, (body.evidence || []).length))) || "（无）"],
    ["建议动作", body.proposed_action ? `<span class="mono">${esc(JSON.stringify(body.proposed_action))}</span>` : "（无）"],
    ["风险", body.risk ? `${esc(body.risk.level || "")} ${esc(body.risk.action_class || "")}` : "（无）"],
    // ★★ BUG-193：`cost` 的字段是 `input_tokens`/`output_tokens`/`tool_calls`/`wall_ms`
    //   （spec §6.1），**没有** `tokens`/`rounds` —— 修前读 `cost.tokens` 与
    //   `cost.rounds` 让成本行永远显示「tokens=0 轮次=-」（轮次其实在**顶层** `rounds`）。
    ["成本", `tokens=${esc((cost.input_tokens ?? 0) + (cost.output_tokens ?? 0))}`
      + `（入 ${esc(cost.input_tokens ?? 0)} / 出 ${esc(cost.output_tokens ?? 0)}）`
      + ` 工具 ${esc(cost.tool_calls ?? 0)} 次 · 轮次 ${esc(body.rounds ?? "-")}`
      + ` · 墙钟 ${esc(cost.wall_ms ?? "-")}ms`],
    ["终止判据", esc(body.termination_reason || (body.termination && body.termination.reason) || "（无）")],
    // ★ T38：内部说明（含章节号/工具名/判据的那份）**只在这里**出现 ——
    //   它就是修前被摆在答案区的那段话。
    ["技术说明", explanation.internal_report
      ? `<div class="ev-sum">${emphasize(explanation.internal_report)}</div>` : "（无）"],
    ["降级", (degraded ? `<ul>${degraded}</ul>` : "（无）")
      + more(body.degraded_total, (body.degraded || []).length)],
  ];
  if (restored) {
    // ★ 如实说清"这一格与实时的差别"：事件时间线（agent_start/agent_done/report…）
    //   是**实时过程**，没有落会话记忆；刷新后给的是当时记录下来的步骤。
    rows.push(["事实来源（刷新后）", `<div class="ev-sum">${esc(body.facts_source || "")}</div>`]);
    rows.push(["过程时间线", `<div class="ev-sum">刷新恢复的是**当时记录下来的执行过程**
      （识别意图 → 各 Agent 取证 → 证据入黑板 → 裁决 → 方案 → 答案，以及每一次工具调用的
      参数与结果），顺序与实时一致；每轮最多留 ${60} 步、每步载荷有界 ——
      超出的那几步在实时看得到、刷新后看不到（不补写、不假装）。</div>`]);
  }
  return kv(rows.filter(([, value]) => value !== undefined && value !== null && value !== ""));
}

// ★★ T42/T53：明细块的**视图切换**（表格 ⇄ JSON）—— 同一份数据，不重新问模型。
//   实时答案卡与"刷新后恢复的历史气泡"**共用这一个绑定**（两处各写一遍必然漂移）。
function bindDetailsToggle(root) {
  root.querySelectorAll(".details button[data-toggle]").forEach((button) =>
    button.addEventListener("click", () => {
      const box = button.closest(".details");
      const next = box.dataset.kind === "table" ? "json" : "table";
      box.dataset.kind = next;
      box.querySelector(".details-body").innerHTML =
        box.querySelector(`template.as-${next}`).innerHTML;
      button.textContent = `换成 ${next === "table" ? "JSON" : "表格"}`;
    }));
}

// 把后端的 `**…**` 渲染成 <strong>（★ 先 esc 再替换 —— 顺序反了就是注入）
function emphasize(text) {
  return esc(text).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
}

// ★★ T42：明细块由**前端**渲染（模型只给"行"，不写标记语法）——
//   表格 / JSON 是**同一份数据**的两种视图，按 AI 选的 `details_kind` 默认展示，
//   右上角可一键切换（切视图不再问模型）。
function detailsBlock(meta, body) {
  const rows = (meta.details || []).filter((row) => row && String(row.value || "").trim());
  if (!rows.length) return "";
  const kind = (meta.details_kind || "table").toLowerCase() === "json" ? "json" : "table";
  const asTable = `<table class="detail"><tr><th>项</th><th>内容</th></tr>`
    + rows.map((row) => `<tr><td class="k">${esc(row.label || "")}</td>`
      + `<td>${renderKnowledgeText(row.value||"",body,esc,text=>evidenceViewer.renderText(text,body))}</td></tr>`).join("") + `</table>`;
  const asJson = `<pre class="detail-json mono">${
    esc(JSON.stringify(Object.fromEntries(rows.map((r) => [r.label || "", r.value || ""])), null, 2))}</pre>`;
  return `<div class="details" data-kind="${kind}">
    <div class="details-head">
      <span class="muted">明细（${rows.length} 项）</span>
      <button class="ghost chip" data-toggle>换成 ${kind === "table" ? "JSON" : "表格"}</button>
    </div>
    <div class="details-body">${kind === "table" ? asTable : asJson}</div>
    <template class="as-table">${asTable}</template>
    <template class="as-json">${asJson}</template>
  </div>`;
}

// ★★ 一个判据，**两处共用**（实时答案卡 + 刷新后恢复的历史气泡）：
//   "这一轮有没有可导出的东西"。修前这条判据只长在 `verdictBlock` 里，而恢复那条路
//   根本没渲染导出入口 → **刷新后按钮就没了**（用户实测）。
//   ★ 两种形状都要认：实时是 `body.explanation.answer`，恢复回来的 `turn` 是 `turn.answer`。
function exportableBody(body) {
  const meta = ((body || {}).explanation || {}).answer || (body || {}).answer || {};
  const summary = String(meta.summary || (body || {}).report || "").trim();
  return !!((body || {}).incident_id
    && (summary || (meta.details || []).length || (body || {}).root_cause
        || (body || {}).status === "clarify"));
}

// ★★ 结论区（T38/T42）：**一段总结**（黑粗）+ 明细块。
//   数据全部来自后端：`explanation.answer.{summary,details,details_kind}`
//   （没有 summary 时退回 `report`，例如澄清/越界的固定话术）。
function verdictBlock(body) {
  const meta = ((body.explanation || {}).answer) || {};
  const summary = String(meta.summary || body.report || "").trim();
  const exportable = exportableBody(body);
  const head = summary
    ? `<div class="verdict-line">${emphasize(summary).replace(/\n/g, "<br>")}</div>`
    : `<div class="verdict-line">${esc(statusLabel(body.status, body))}</div>`;
  return head + (exportable ? exportButton() : "") + detailsBlock(meta, body);
}

// ---------------- 导出 Markdown（内容服务端生成，位置用户自选） ----------------
const EXPORT_ENDPOINT = "/v1/remediation/export";
const EXPORT_LABEL = "保存为 md 文件";

// ★★ 入口只在**答案卡**上（有结论或方案时出现）；**不自动弹保存框** ——
//   浏览器要求 `showSaveFilePicker` 必须在用户手势里调用，
//   跑完一轮排查（20~30s）再弹一定会被拒。
function exportButton() {
  return `<div class="verdict-actions">
    <button class="ghost chip" data-export title="把这次排查的结论、证据与方案导出成 Markdown（位置由你选）">${EXPORT_LABEL}</button>
    <span class="hint" data-export-note></span>
  </div>`;
}

// 在按钮旁边**就地**说明结果（成功/失败都说，不许静默）
function flashNote(button, text, kind = "") {
  const note = button.parentElement.querySelector("[data-export-note]");
  if (note) { note.className = `hint ${kind}`; note.textContent = text; }
  return note;
}

function bindExport(card, body) {
  const button = card.querySelector("button[data-export]");
  if (button) button.addEventListener("click", () => exportMarkdown(button, body));
}

// ★★ 用户在对话里说「导出 / 下载 / 保存为 md」时，**回复里要指出入口在哪**
//   （不许静默什么都不说）。入口在答案卡上，由上面的按钮承担。
function exportHint() {
  if (!state.lastAsk || !/导出|下载|保存为\s*md|存成\s*md|导出成/i.test(state.lastAsk)) return;
  const row = document.createElement("div");
  row.className = "ev live";
  row.innerHTML = `<div class="ev-name">导出</div>
    <div>点答案卡上的「${EXPORT_LABEL}」即可：内容由服务端按本次排查的事实生成，
    位置由你在系统保存框里自选（浏览器不支持时会改为下载到默认目录）。</div>`;
  timeline.appendChild(row);
  timeline.scrollTop = timeline.scrollHeight;
}

async function exportMarkdown(button, body) {
  const incidentId = String(body.incident_id || "");
  button.disabled = true;
  flashNote(button, "正在生成 Markdown …", "muted");
  try {
    const response = await fetch(EXPORT_ENDPOINT, {
      method: "POST", headers: requestHeaders(true),
      body: JSON.stringify({ session_id: body.session_id || conversationId(), incident_id: incidentId,
        observed_id: body.explanation?.observed_system?.observed_id || workspace.oid }) });
    if (!response.ok) {
      const detail = await response.text();
      flashNote(button, `导出失败：HTTP ${response.status} ${String(detail).slice(0, 200)}`, "bad");
      return;
    }
    const text = await response.text();
    await saveMarkdown(text, incidentId, button);
  } catch (exc) {
    flashNote(button, `导出失败：${String(exc)}`, "bad");
  } finally {
    button.disabled = false;
  }
}

function markdownName(incidentId) {
  const stamp = new Date().toISOString().slice(0, 19).replace(/[-:]/g, "").replace("T", "-");
  return `aiops-${incidentId || "incident"}-${stamp}.md`;
}

// ★★ 系统保存框**只在点击处理器里**调用（`showSaveFilePicker` 必须跟着用户手势，
//   否则浏览器直接抛 SecurityError）。不支持时回落成普通下载，并**如实说明**。
async function saveMarkdown(text, incidentId, button) {
  const suggestedName = markdownName(incidentId);
  const picker = window.showSaveFilePicker;
  if (typeof picker !== "function") {
    flashNote(button, "浏览器不支持系统保存框 → 已改为下载到默认目录", "warn");
    downloadMarkdown(text, suggestedName);
    return;
  }
  let handle = null;
  try {
    handle = await picker.call(window, { suggestedName,
      types: [{ description: "Markdown 文件", accept: { "text/markdown": [".md"] } }] });
  } catch (exc) {
    if (exc && exc.name === "AbortError") { flashNote(button, "已取消保存"); return; }
    flashNote(button, `系统保存框不可用（${String(exc && exc.name || exc)}）→ 已改为下载到默认目录`, "warn");
    downloadMarkdown(text, suggestedName);
    return;
  }
  await writeMarkdown(handle, text, suggestedName, button);
}

async function writeMarkdown(handle, text, suggestedName, button) {
  try {
    const stream = await handle.createWritable();
    await stream.write(text);
    await stream.close();
    flashNote(button, `已保存${handle.name ? `：${handle.name}` : "到你选的位置"}`, "ok");
  } catch (exc) {
    flashNote(button, `写入失败（${String(exc)}）→ 已改为下载到默认目录`, "warn");
    downloadMarkdown(text, suggestedName);
  }
}

function downloadMarkdown(text, filename) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/markdown" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

// ---------------- 反问：**用普通对话回答**（T40，用户指示） ----------------
// ★★ 改前的做法（用户明确否掉）：把"回答"做成**卡片内的输入框 + 提交按钮**，
//    提交走 `/v1/chat/resume` —— 那是**问答器**，用户被拉进一个专用控件里。
// ★ 现在：反问就是**助手的一句话**（问句 + 建议以文本呈现），候选项做成
//    **点一下就填进输入框**的快捷词；用户按正常方式发消息，AIOps 从新消息里取缺失信息。
//    后端（T40 的会话台账）负责把"这句是在补上一次的信息"认出来。
function clarificationCard(body) {
  const payload = body.clarification || {};
  const questions = payload.questions || [];
  if (!questions.length) return;
  const card = document.createElement("div");
  card.className = "card ask";
  const blocks = questions.map((q, index) => `
    <div class="q" data-index="${index}">
      <div class="ask-text">${emphasize(q.question || "")}</div>
      ${(q.options || []).length ? `<div class="composer-actions">
        <span class="hint">直接点一个填进输入框（也可以自己打字）：</span>
        ${(q.options || []).map((opt) =>
          `<button class="ghost chip" data-fill="${esc(opt)}">${esc(opt)}</button>`).join("")}
      </div>` : ""}
      <div class="muted mono">slot=${esc(q.slot || "")} plugin_id=${esc(q.plugin_id || "（旧路径：无插件）")}</div>
    </div>`).join("");
  card.innerHTML = `<h3>${badge("需要你补充信息", "warn")}</h3>${blocks}
    <div class="hint">↑ 在**下面的输入框**里回我就行（跟平时提问一样）；先问别的事也可以，那件事我会记着。</div>`;
  timeline.appendChild(card);
  timeline.scrollTop = timeline.scrollHeight;
  // ★ 快捷词只**填充输入框**（不提交、不走特殊接口）—— 回答的路径与普通提问**完全一致**
  card.querySelectorAll("button[data-fill]").forEach((button) =>
    button.addEventListener("click", () => {
      const box = $("message");
      box.value = button.dataset.fill;
      box.focus();
    }));
}

// ---------------- 审批卡（L3 红线：审批人必须 human:） ----------------
function approvalCard(body) {
  const action = body.proposed_action;
  if (!action) return;
  const pending = body.approval || body.pending_approval || {};
  const requestId = pending.request_id || action.request_id || "";
  const actionHash = pending.action_hash || action.action_hash || "";
  if (!requestId || !actionHash) return;   // 没有待批令牌就不摆审批按钮（不假装能批）
  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML = `<h3>${badge("待人工审批", "warn")}<span class="mono muted">${esc(requestId)}</span></h3>
    ${kv([["动作", `<span class="mono">${esc(JSON.stringify(action))}</span>`],
          ["风险", esc((body.risk || {}).level || "")],
          ["action_hash", `<span class="mono">${esc(actionHash)}</span>`]])}
    <form class="inline">
      <input class="approver" placeholder="审批人，例如 human:yangcheng" size="30">
      <input class="reason" placeholder="理由" size="24">
      <button class="primary" data-approve>批准</button>
      <button data-reject>拒绝</button>
    </form>
    <div class="hint">审批人必须以 <span class="mono">human:</span> 开头；<span class="mono">agent:</span> 会被后端拒绝（H4 红线）。</div>`;
  timeline.appendChild(card);
  timeline.scrollTop = timeline.scrollHeight;
  const send = async (approved) => {
    const revision=workspace.revision;
    const approver = card.querySelector(".approver").value.trim();
    if (!approver.startsWith("human:")) {
      card.querySelector(".approver").focus();
      pushEvent("approval", "审批人必须以 human: 开头 —— 已在前端拦下（后端同样会拒）");
      return;
    }
    card.remove();
    const { status, data } = await api("/v1/chat/resume", { method: "POST", body: {
      session_id: body.session_id, resume_type: "approval",observed_id:workspace.oid,
      context:{env:envValue(),cluster:clusterValue()},
      approval: { request_id: requestId, approved, action_hash: actionHash,
                  approver, reason: card.querySelector(".reason").value || "" } } });
    if(revision!==workspace.revision)return;
    pushEvent("approval", `HTTP ${status}`);
    if (data) outcomeCard(data);
    await afterTurn();               // 审批也是一轮：列表的轮次/时间跟着走
  };
  card.querySelector("button[data-approve]").addEventListener("click", () => send(true));
  card.querySelector("button[data-reject]").addEventListener("click", () => send(false));
}

// ---------------- 发送：同步 / 流式 ----------------
function options() {
  return { max_rounds: 3, dry_run_only: false, allow_write: false,
           no_clarify: $("noclari").checked };
}
function requestBody(message) {
  // ★★ BUG-199：左侧那个「时间窗」输入框以前是个**装饰品** —— `app.js` 从来没读过它，
  //    用户把它改成 10h，请求里却仍是默认 30m（实测：故障日志因此超窗，Agent 看不到）。
  //   ★ 契约里 `context` **没有**时间窗字段（spec §6.1 的参数表只有 env/cluster/recent_turns，
  //     且 `extra="forbid"`），而槽位抽取认 `\d+(s|m|h|d)` ——
  //     所以把时间窗**并进消息**交给抽取层（不新造契约字段）。
  const win = ($("window").value || "").trim();
  const text = win ? `${message}\n（时间窗 ${win}）` : message;
  // ★★ BUG-201：**一条会话 = 一个 session_id** —— 后端靠它做多轮（指代消解 / 任务台账 /
  //   澄清答问）。这里只交出**当前会话指针**（会话号由服务端分配，前端不自己拼）。
  const body = { session_id: conversationId(), message: text, user: { id: "webui:user" },
                 context: { env: envValue(), cluster: clusterValue(),timezone:Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai", ...workspace.selected() }, options: {...options(),response_mode:"view"} };
  return body;
}

async function sendSync(body) {
  const revision=workspace.revision;
  const { status, data } = await api("/v1/chat", { method: "POST", body });
  if (revision!==workspace.revision)return null;
  pushEvent("POST /v1/chat", `HTTP ${status}`);
  // ★★ T54：工具调用是**边跑边推**的 SSE 事件 —— 关掉流式就看不到过程。如实说明（不许静默）
  pushEvent("提示", "本次没勾「流式显示过程」→ 看不到逐条的工具调用；勾上后每次查询的参数与结果都会实时显示。");
  if (data) { outcomeCard(data); exportHint(); clarificationCard(data); approvalCard(data); }
  return data;
}

async function sendStream(body) {
  const revision=workspace.revision;
  const headers = { "Content-Type": "application/json", "Accept": "text/event-stream" };
  if (token()) headers["Authorization"] = `Bearer ${token()}`;
  const resp = await fetch("/v1/chat/stream", { method: "POST", headers, body: JSON.stringify(body),signal:state.chatAbort?.signal });
  if(revision!==workspace.revision)return null;
  if (!resp.ok || !resp.body) { pushEvent("stream", `HTTP ${resp.status}（回退到同步）`); return sendSync(body); }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let lastReport = null;
  // ★ T37：这一行**留在时间线上**（不进 `turnProcess`），实时显示最新事件 ——
  //   否则过程全折叠后，一次 20~30s 的排查看起来像卡死。答案卡出现时它被移除。
  const liveRow = document.createElement("div");
  liveRow.className = "ev live";
  liveRow.innerHTML = `<div class="ev-name">进行中</div><div>已连接，等待事件 …</div>`;
  timeline.appendChild(liveRow);
  timeline.scrollTop = timeline.scrollHeight;
  const setLive = (text) => { liveRow.querySelector("div:last-child").textContent = text; };
  while (true) {
    const { value, done } = await reader.read();
    if(revision!==workspace.revision){await reader.cancel();return null;}
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";
    for (const chunk of parts) {
      const lines = chunk.split("\n");
      const event = (lines.find((l) => l.startsWith("event:")) || "").slice(6).trim();
      const dataLine = lines.find((l) => l.startsWith("data:")) || "";
      if (!event) { setLive("仍在排查（心跳）…"); continue; }
      let payload = {};
      try { payload = JSON.parse(dataLine.slice(5).trim() || "{}"); } catch { payload = { raw: dataLine }; }
      setLive(summarizeEvent(event, payload));
      // ★★ T54：工具调用**边到边显示**（成对的 start/done 就地合成一张卡）
      if (event === "tool") toolCard(payload);
      else pushEvent(event, summarizeEvent(event, payload));
      if (event === "report") lastReport = payload;
      if (event === "error") pushEvent("error", payload.message || JSON.stringify(payload));
    }
  }
  liveRow.remove();
  if (lastReport) {
    outcomeCard(lastReport);
    exportHint();
    clarificationCard(lastReport);
    approvalCard(lastReport);
  }
  return lastReport;
}

function summarizeEvent(event, payload) {
  if (event === "intent") return `意图 ${payload.intent}（信心 ${payload.confidence}，来源 ${payload.source}）`;
  if (event === "tool") {
    const title = payload.title || payload.name || "步骤";
    const phase = payload.phase === "done" ? "完成" : "开始";
    const result=resultSummary(payload.result);
    return `${phase}：${title}${result ? ` —— ${result}` : ""}`;
  }
  if (event === "agent_start") return payload.role==="qa_agent" ? "知识问答开始检索" : `第 ${payload.round} 轮 · ${payload.role} 开始取证`;
  if (event === "agent_done") return payload.role==="qa_agent" ? `知识问答结束：${payload.sources || 0} 条知识来源` : `${payload.role}：证据 ${payload.evidence_count ?? "?"} 条`;
  if (event === "evidence") return `${payload.evidence_id || ""} ${payload.signal || ""} ${payload.summary || ""}`;
  if (event === "hypothesis") return `候选 ${payload.hypothesis_id || ""} 分数 ${payload.score ?? ""}`;
  if (event === "critic") return `Critic 复核 ${payload.hypothesis_id || ""} ${payload.verdict || ""}`;
  if (event === "report") return `排查结束：${payload.status}（${payload.incident_id || ""}）`;
  return JSON.stringify(payload).slice(0, 160);
}

async function sendMessage() {
  if (!workspace.oid || !workspace.row?.active || state.chatBusy || state.historyLoading || state.sessionCreating) return;
  const revision=workspace.revision;state.chatAbort=new AbortController();
  const message = $("message").value.trim();
  if (!message) return;
  state.chatBusy = true;
  $("send").disabled = true;
  // ★ 发之前先确保有一个**服务端分配**的会话号（本地指针失效时由服务端补一个新的）
  let sessionId = null;
  try {
    sessionId = await ensureSession();
  } catch (exc) {
    pushEvent("error", `会话初始化失败：${String(exc)}`);
  }
  if(revision!==workspace.revision)return;
  if (!sessionId) {
    pushEvent("error", "没有可用的会话号（服务端未分配）—— 见左侧会话区提示，本次不发送。");
    state.chatBusy = false; syncConversationControls();
    return;
  }
  state.lastAsk = message;           // ★ 「导出/下载」这类话要能指出入口
  // ★★ 发出去就**清空输入框**（原话已经贴成左边的气泡了，输入框留给下一个问题）——
  //   修前只有「清空」按钮（那是**开新会话**），输入框里的旧问题会一直留着，
  //   下一次提问得先把旧字删干净。
  //   ★ 清空点放在**会话号已经拿到之后**：拿不到会话号时上面已经 return，
  //     那时一个字都不动 —— 发送失败不该把用户刚打的问题吃掉。
  $("message").value = "";
  appendUserBubble(message);         // ★ 提问贴到**左边气泡**（与恢复历史同一个形态）
  beginTools();                      // ★★ T54：本轮的工具调用卡**从零开始**逐张追加
  beginTurn();                       // ★ T37：之后的过程行都进本轮容器
  const body = requestBody(message);
  try {
    if ($("stream").checked) await sendStream(body); else await sendSync(body);
  } catch (exc) {
      if(revision===workspace.revision)pushEvent("error", String(exc));
  } finally {
      if(revision!==workspace.revision)return;
    finishTools(null,{interrupted:true});
    flushTurnProcess();              // ★ T37：没有答案卡时（异常/回退）过程摊回时间线
    await afterTurn();               // ★ 本轮之后：刷新会话列表 + 记为「上次会话」
    state.chatBusy = false; syncConversationControls();
  }
}

// ---------------- 告警面板 ----------------
const SAMPLE_ALERT = {
  source: "alertmanager",
  alerts: [{ fingerprint: "webui-demo-1", status: "firing",
    labels: { alertname: "HighP99Latency", service: "order", severity: "critical" },
    annotations: { summary: "order P99 > 2s" }, startsAt: "2026-10-01T00:00:00Z" }],
};
async function submitAlerts() {
  let payload;
  try { payload = JSON.parse($("alerts-payload").value); }
  catch (exc) { $("alerts-result").textContent = `JSON 解析失败：${exc}`; return; }
  if(!workspace.oid)return;
  for(const alert of payload.alerts || []) alert.labels={...alert.labels,observed_id:workspace.oid,env:envValue()};
  for(const alert of payload.alerts || []) {
    if(clusterValue())alert.labels.cluster=clusterValue();
    else delete alert.labels.cluster;
    alert.annotations={...alert.annotations,time_window:$("window").value || "30m"};
  }
  const revision=workspace.revision;
  const { status, data } = await api("/v1/alerts", { method: "POST", body: payload });
  if(revision!==workspace.revision)return;
  $("alerts-result").textContent = `HTTP ${status}\n` + JSON.stringify(data, null, 2);
  if (status === 202 && data) {
    refreshStatus();
    await loadAlertReceipts();
  }
}

async function loadAlertReceipts() {
  if(!workspace.oid)return;
  await warningList.load();
  if(!workspace.oid)return;
  const revision=workspace.revision;
  try {
    const {status,data,stale}=await api(`/v1/observed-systems/${workspace.oid}/alerts`);
    if(stale || revision!==workspace.revision)return;
    if(status!==200)throw new Error(`HTTP ${status}`);
    renderAlertReceipts($("alerts-records"),data.alerts || [],{
      async openConversation(sid){workspace.panel("chat");await openSession(sid);},
      async openIncident(iid){
        workspace.panel("incidents");await loadIncidents();
        const result=await api(`/v1/incidents/${encodeURIComponent(iid)}`);
        if(!result.stale)$("incident-detail").textContent=JSON.stringify(result.data,null,2);
      },
    });
  } catch(error) {
    if(revision===workspace.revision)$("alerts-records").textContent=`接收记录读取失败：${error.message}`;
  }
}

// ---------------- 事故面板 ----------------
async function loadIncidents() {
  const { data,stale } = await api("/v1/incidents");
  if(stale)return;
  const items = (data && data.incidents) || [];
  $("incident-list").innerHTML = items.length
    ? items.map((it) => `<li data-id="${esc(it.incident_id)}">${esc(it.incident_id)}<br>
        <span class="muted mono">${esc(it.status)} · ${esc(it.intent || "")} · 证据 ${esc(it.evidence_count ?? 0)}</span></li>`).join("")
    : `<li class="muted">还没有事故记录（先发一次问诊）</li>`;
  for (const li of $("incident-list").querySelectorAll("li[data-id]")) {
    li.addEventListener("click", async () => {
      const { status, data: detail,stale } = await api(`/v1/incidents/${li.dataset.id}`);
      if(stale)return;
      $("incident-detail").textContent = `HTTP ${status}\n` + JSON.stringify(detail, null, 2);
    });
  }
}

// ---------------- 启动 ----------------
$("send").addEventListener("click", sendMessage);
$("send").addEventListener("click",()=>commandComposer.reset());
$("message").addEventListener("keydown", (event) => {
  // ★★ 回车即发送（Shift+Enter 换行）—— 判据三条都要：
  //   ① `key === "Enter"`；② **没按 Shift**（Shift+Enter 留给换行，长问题要能分行）；
  //   ③ `isComposing` 为假 —— 中文输入法**选词时也会发 Enter**，那一下是在确认候选字，
  //      不能当发送（不判这一条，拼音打字选词就把半句话发出去了）。
  //   Ctrl/Cmd+Enter 照旧可用（它就是不带 Shift 的 Enter）；空内容由 `sendMessage` 自己挡。
  if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  commandComposer.reset();
  sendMessage();
});
$("clear").addEventListener("click", () => {
  // ★ BUG-201：清空 = **开一条新会话**（否则新问题会继承旧上下文）——
  //   会话号由服务端分配，前端只把指针切过去，并把对话区清掉。
  createSession({ announce: true });
});
$("session-new").addEventListener("click", () => createSession({ announce: true }));
$("session-refresh").addEventListener("click", loadSessions);
$("alerts-send").addEventListener("click", submitAlerts);
$("alerts-refresh").addEventListener("click", loadAlertReceipts);
$("alerts-sample").addEventListener("click", () => {
  $("alerts-payload").value = JSON.stringify(systemAlertSample(), null, 2);
});
document.querySelector('[data-panel="incidents"]').addEventListener("click", loadIncidents);

$("alerts-payload").value = JSON.stringify(SAMPLE_ALERT, null, 2);
refreshStatus();
setInterval(refreshStatus, 15000);
workspace.init();
setupLegacy(api,exportMarkdown);

async function leaveWorkspace() {
  warningList.reset();commandComposer.reset();
  requestClient.reset();
  $("knowledge-drawer").hidden=true;
  sessionLoader.reset(); state.historyLoading = false; state.chatBusy = false; state.sessionCreating = false;
  state.chatAbort?.abort();clearTimeline();state.sessionId=null;state.sessions=[];
  state.lastIncident=null;state.lastResponse=null;state.lastAsk="";state.capabilities=null;
  $("session-list").innerHTML="";$("session-note").textContent="";$("session-list-note").textContent="";$("message").value="";
  $("incident-list").innerHTML="";$("incident-detail").textContent="";$("caps").textContent="";
  $("alerts-result").textContent="";$("alerts-records").replaceChildren();$("send").disabled=false;$("trace").textContent="trace: —";
  state.historyOpen=(state.historyOpen || 0)+1;$("chat-hint").textContent="";
  for(const dialog of document.querySelectorAll("dialog[open]"))dialog.close();
}

async function enterWorkspace(row,revision) {
  warningList.start();
  syncConversationControls();
  $("alerts-payload").value=JSON.stringify(systemAlertSample(),null,2);
  await Promise.allSettled([workspace.requestedSession ? openSession(workspace.requestedSession) : restoreSession(), loadCapabilities()]);
  if(revision!==workspace.revision)return;
  if(!timeline.childNodes.length)pushEvent("webui",`当前工作台：${row.name}；发第一条消息时创建该系统的会话。`);
}

function systemAlertSample() {
  const row=workspace.row;const service=(row?.services || []).find((s)=>s.active)?.service;
  return {source:"alertmanager",alerts:[{fingerprint:(row?.observed_id || "")+"-example",status:"firing",
    labels:{observed_id:row?.observed_id || "",env:envValue(),service:service || "",alertname:"ServiceError",severity:"warning"},
    annotations:{summary:"请按实际告警修改"},startsAt:new Date().toISOString()}]};
}
