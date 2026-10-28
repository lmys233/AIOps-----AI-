import {restoredCalls} from "./history_tools.mjs";
import {appendTurnDuration} from "./turn_duration.mjs";

// Rendering is read-only: session requests/cache/selection belong to SessionLoader.
export function createHistoryRenderer({timeline,esc,messageBubble,answerCardNode,bindExport,
  statusLabel,pushEvent,summarizeEvent,scrollTimelineToBottom,toolCard}) {
function restoredTurnNodes(turn) {
  if (!String(turn.user_message || "").trim()) return [ledgerRow(turn)];
  const input=messageBubble("user", turn.user_message, { at: turn.at });
  const alert=turn.response?.explanation?.alert_ref;
  if(alert)input.querySelector(".msg-who").textContent=`告警系统 · ${alert.source || "外部监控"}`;
  const nodes = [input];
  const calls=document.createElement("div");calls.className="tools history-tools";
  renderTurnTools(turn,calls);
  appendTurnDuration(calls,restoredAnswerBody(turn));
  if(calls.childNodes.length)nodes.push(calls);
  nodes.push(aiHistoryBubble(turn));
  for (const node of nodes) node.dataset.historyTurn = String(turn.turn_id || turn.incident_id || turn.at || "");
  return nodes;
}

function renderRestoredTurns(turns, degraded, headline) {
  const notes = (degraded || []).filter(Boolean);
  if (!turns.length && !notes.length) {
    pushEvent("webui", `${headline}：这条会话还没有对话记录`);
    return;
  }
  const head = document.createElement("div");
  head.className = "hist-head";
  head.innerHTML = `<span class="badge unknown">历史恢复</span>
    <span>${esc(headline)} · 初始载入 ${esc(turns.length)} 轮
    <span class="muted">（本次加载 ${esc(turns.length)} 轮；向上滑动读取更早记录）</span></span>`;
  timeline.appendChild(head);
  for (const note of notes) {
    // ★ 记忆读不到就**如实显示**（不是"没有历史"，而是"读不出来"）
    const row = document.createElement("div");
    row.className = "ev hist-warn";
    row.innerHTML = `<div class="ev-name">记忆降级</div><div>${esc(note)}</div>`;
    timeline.appendChild(row);
  }
  for (const turn of turns) {
    // ★ 台账行（`user_message` 为空 = 这一轮的**正文不在记忆里**）：只渲染一行并标明来源，
    //   不许造一个空气泡假装用户说过话 —— 后端已经把"为什么没有正文"写进上面的降级说明。
    if (!String(turn.user_message || "").trim()) {
      timeline.appendChild(ledgerRow(turn));
      continue;
    }
    // Same order as live: user input, visible tool cards, then the saved answer.
    for (const node of restoredTurnNodes(turn)) timeline.appendChild(node);
  }
  scrollTimelineToBottom();
}

// 恢复历史里的 **AI 气泡**：与实时**同一张答案卡**（用户实测："刷新后卡片就变成了…"，
// 要求"刷新后也要显示成第一个样式"）。
// ★★ 做法：先把"恢复回来的那一轮"**拍平成与 `/v1/chat` 响应同形状**的 body
//    （字段同名 → 卡片的取值代码一套两用），再调**同一个** `answerCardNode`。
//   · `turn.answer`（答案原文）→ `explanation.answer`；
//   · `turn.incident`（服务端给的事故投影）→ 顶层 `intent`/`slots`/`evidence`/`cost`/`degraded`…；
//   · 事故投影缺失（留存过期/从未留存）时对应的格子如实说明，**不编**。
function restoredAnswerBody(turn) {
  if (turn?.response) return Object.assign({}, turn.response, {
    finished_at: turn.at, facts_source: "聊天历史档案（与当轮答案同源）" });
  const body = turn || {};
  const answer = body.answer || {};
  const incident = body.incident || {};
  const projection = Object.assign({}, incident);
  // 投影里那两个下划线字段还原成实时响应的嵌套形状（`explanation.answer.source/note`）
  projection.explanation = {
    attribution_v2: answer.attribution_v2 || null,
    answer: Object.assign({}, answer, {
      source: incident.answer_source || "", note: incident.answer_note || "" }),
    internal_report: incident.internal_report || "",
  };
  delete projection.answer_source;
  delete projection.answer_note;
  delete projection.internal_report;
  const projected = Object.keys(incident).length > 0;
  return Object.assign(projection, {
    status: body.status || "", incident_id: body.incident_id || "",
    rounds: incident.rounds, finished_at: body.at || "",
    // ★★ 事故留存**读不到**（已过期 / 从未留存）时要说清"哪几格为什么是空的"——
    //   否则用户看到的是满屏「（无）」，会以为 Agent 当时什么都没查到
    //   （用户实测那两张卡正是这种形状：事故留存已过期）。
    facts_source: projected
      ? `事故留存（${body.incident_id}）—— 刷新前那张卡里的这些事实来自同一次排查`
      : "★ 这一轮的事故留存**读不到了**（已过期或从未留存）→ 下面这几格在刷新后无法恢复；"
        + "刷新前那张卡里的这些事实当时是有的（实时响应里有，但没有随会话记忆一起留下）",
  });
}

function aiHistoryBubble(turn) {
  const body = turn || {};
  const view = restoredAnswerBody(body);
  // ★★ 气泡本身**不带文本**（与实时那张卡同一个形态）：结论那一句话只出现在**卡片里**
  //   的 `verdict-line`。修前恢复路径把同一句既贴在气泡上（`msg-text`）、又由卡片再渲染一遍
  //   —— 用户实测："这个小的结论卡片直接删掉，只保留这个（`div.card`）"。
  const bubble = messageBubble("ai", "", { at: body.at });
  // Tool calls are visible before the answer; only non-tool reasoning stays folded.
  const process = document.createElement("div");
  process.className = "turn-process";
  renderRestoredSteps(body, process);
  // ★★ 卡片本体：与实时**同一个**构建器（h3 徽标 + 事故号 / verdict / 导出 / 明细 / 推理过程）
  const card = answerCardNode(view, {
    process, steps: process.childNodes.length, restored: true,
    feedback:body.feedback,feedback_target:body.feedback_target,feedback_unavailable:body.feedback_unavailable });
  bubble.appendChild(card);
  bindExport(card, view);
  return bubble;
}

// 台账行：这一轮属于哪次事故、什么状态、回答的首句（**正文不在记忆里**）
function ledgerRow(turn) {
  const body = turn || {};
  const row = messageBubble("ai", turnSummary(body), { at: body.at });
  row.classList.add("msg-ledger");
  row.querySelector(".msg-who").textContent = "台账 · 正文不在记忆里";
  return row;
}

// 恢复历史里的工具卡：`turn.tools` 是**已完成的调用**（没有 start/done 之分），
// 所以直接落一张完成态的卡 —— 与实时那张长得一样（同一套 DOM 与样式）。
// ★ `container` 缺省是时间线；恢复卡片把它传成**推理过程折叠区**里的过程容器，
//   好让"工具/答案步骤"落在与实时**同一个位置**（卡片的「推理过程」里）。
function renderTurnTools(turn, container = null) {
  const {calls,missing,withoutPosition}=restoredCalls(turn);
  if (!calls.length && !missing) return null;
  const box = container || document.createElement("div");
  box.classList.add("tools");
  if(!container)timeline.appendChild(box);
  for (const {key,payload} of calls) {
    const card = toolCard({ ...payload, call_id: `hist-${turn.turn_id || turn.incident_id || turn.at}-${key}` }, box);
    if (card && card.parentElement !== box) box.appendChild(card);
  }
  if(missing || withoutPosition) {
    const note=document.createElement("p");note.className="muted";
    note.textContent=[missing ? `${missing} 个步骤未留存完整调用结果，未补造卡片。` : "",
      withoutPosition ? `${withoutPosition} 次调用缺少首次位置，按已有留存顺序显示。` : ""].filter(Boolean).join(" ");
    box.appendChild(note);
  }
  return box;
}

// 恢复历史里的**执行过程**：按当时记录下来的顺序重放。
// ★★ 用户实测（2026-10-05）："你是不是把思考过程也删了？刷新页面后就不见了，
//    我想要刷新后依旧保留 AIOps 执行的过程。" —— 非工具环节（intent / agent_start /
//    agent_done / report …）以前只走 SSE，刷新后整条时间线就没了。
// ★ 每一行的**文案**仍由 `summarizeEvent` 那一处生成（与实时**同一个**映射），
//    服务端只交回有界载荷 —— 两处各写一份必然漂移。
// ★ 工具那一步按 `call_id` 去 `turn.tools` 里取内容，位置与实时一致（先出现的在前）。
function renderRestoredSteps(turn, container) {
  const steps = (turn && turn.steps) || [];
  if (!steps.length) {
    // Tools are visible before the answer; do not duplicate them in reasoning.
    return 0;
  }
  for (const step of steps) {
    const event = String(step.event || "");
    if (event === "tool") {
      continue;
    }
    pushEvent(event, summarizeEvent(event, step.data || {}), container);
  }
  return container.childNodes.length;
}

function turnSummary(turn) {
  const body = turn || {};
  const status = String(body.status || "").trim();
  // ★ 恢复视图里的 `summary` 就是那一轮的**状态摘要**（回答落地时留下的，有界）；
  //   台账里没有这一轮的状态时**不许猜**（读不到 ≠ 结论正常）。
  const head = String(body.summary || "").trim()
    || (status ? statusLabel(status, body) : "状态未记录（本地台账里没有这一轮）");
  const parts = [head];
  if (body.incident_id) parts.push(`事故 ${body.incident_id}`);
  return parts.join(" · ");
}


return {restoredTurnNodes,renderRestoredTurns,restoredAnswerBody,renderRestoredSteps};
}
