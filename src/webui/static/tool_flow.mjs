import {renderToolResult} from "./tool_result.mjs";
import {attemptSummary} from "./model_attempts.mjs";
import {appendTurnDuration} from "./turn_duration.mjs";

// A flow owns call_id -> DOM pairing; live and restored calls use the same renderer.
export function createToolFlow({timeline,esc,emphasize}) {
let toolBox = null;                    // 本轮的工具卡容器（发送时建、答案卡出现后留在时间线上）
const toolCards = new Map();           // call_id → 卡片元素（并发调用靠它配对，不靠同名 FIFO）
let started = null, finished = false;

function beginTools() {
  toolBox = document.createElement("div");
  toolBox.className = "tools";
  timeline.appendChild(toolBox);
  toolCards.clear();
  started = performance.now(); finished = false;
  return toolBox;
}

function finishTools(body = null, {interrupted = false} = {}) {
  if (!toolBox || finished) return null;
  finished = true;
  return appendTurnDuration(toolBox, body, {browserMs: performance.now() - started, interrupted});
}

function toolBoxFor() {
  if (!toolBox) beginTools();
  return toolBox;
}

// 一张工具卡：折叠行「查询指标 · promql_read · 进行中」；展开是参数 + 结果
function toolCard(payload, container = null) {
  const callId = String(payload.call_id || payload.name || "");
  let card = toolCards.get(callId);
  if (!card) {
    card = document.createElement("details");
    card.className = "tool run";
    card.dataset.call = callId;
    card.innerHTML = `<summary>
        <span class="tool-badge">调用</span>
        <span class="tool-title">${esc(payload.title || payload.name || "步骤")}</span>
        <span class="tool-name mono">${esc(payload.name || "")}</span>
        <span class="tool-name mono">${esc(payload.signal ? `↳ ${payload.signal}` : "")}</span>
        <span class="tool-status">进行中…</span>
        <span class="tool-meta mono"></span>
      </summary>
      <div class="tool-body">
        <div class="tool-sec"><div class="tool-k">参数</div><pre class="tool-params mono"></pre></div>
        <div class="tool-sec tool-result-sec" hidden><div class="tool-k">结果</div>
          <div class="tool-result"></div></div>
      </div>`;
    card.querySelector(".tool-params").textContent =
      JSON.stringify(payload.params || {}, null, 2);
    if (payload.why) {
      card.querySelector(".tool-body").insertAdjacentHTML("afterbegin",
        `<div class="tool-sec"><div class="tool-k">为什么查它</div>
           <div class="tool-why">${emphasize(payload.why)}</div></div>`);
    }
    (container || toolBoxFor()).appendChild(card);
    toolCards.set(callId, card);
    if (!container) timeline.scrollTop = timeline.scrollHeight;
  } else {
    // ★ 同一编号再来一条 start（重跑/重试）→ 合并参数，不新建卡
    const params = card.querySelector(".tool-params");
    if (params && !params.textContent.trim()) {
      params.textContent = JSON.stringify(payload.params || {}, null, 2);
    }
  }
  if (String(payload.phase || "") === "done") finishToolCard(card, payload, !container);
  return card;
}

// ★ `done` 到达：**就地**补结果 + 状态 + 耗时（不是整轮结束后再补一张清单）
function finishToolCard(card, payload, scroll = true) {
  const ok = payload.ok !== false && !payload.degraded;
  card.classList.remove("run");
  card.classList.add(ok ? "ok" : "bad");
  card.querySelector(".tool-status").textContent = ok ? "完成" : "没成功";
  const ms = typeof payload.elapsed_ms === "number" ? payload.elapsed_ms :
    typeof payload.elapsed_ms === "string" && payload.elapsed_ms.trim() ? Number(payload.elapsed_ms) : NaN;
  const elapsed = Number.isFinite(ms) ? `${Math.round(ms)}ms` : "";
  const purpose = payload.purpose === "aux" ? "辅助取数" : "";
  const attempts=attemptSummary(payload);
  card.querySelector(".tool-meta").textContent =
    [elapsed, attempts, payload.failure_kind || "", payload.error_code || "", purpose].filter(Boolean).join(" · ");
  const sec = card.querySelector(".tool-result-sec");
  const box = card.querySelector(".tool-result");
  renderToolResult(box,payload.result,emphasize);
  if (payload.raw_ref) {
    const ref=document.createElement("span");ref.className="muted mono";ref.textContent=String(payload.raw_ref);
    box.appendChild(document.createElement("br"));box.appendChild(ref);
  }
  sec.hidden = false;
  if (scroll) timeline.scrollTop = timeline.scrollHeight;
}

function resetTools() { toolBox = null; toolCards.clear(); started = null; finished = false; }

// ---------------- 本地会话管理：列表 / 新建 / 切换 / 重命名 / 删除 / 刷新自动恢复 ----------------
// ★★ 会话号**只由服务端铸**：前端不再用时间戳自己拼 id，只保存一个指针 ——
//    当前会话指针和目录元数据按系统保存在本地；正文只请求当前选中的会话。
return {beginTools,toolCard,resetTools,finishTools};
}
