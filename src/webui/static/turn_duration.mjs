// Wall time belongs to the turn: overlapping tools and retries must not be summed.
export function durationText(value) {
  const ms = typeof value === "number" ? value :
    typeof value === "string" && value.trim() ? Number(value) : NaN;
  if (!Number.isFinite(ms) || ms < 0) return null;
  return ms < 1000 ? `${Math.round(ms)} 毫秒` : `${(ms / 1000).toFixed(2)} 秒`;
}

export function appendTurnDuration(container, body, {browserMs = null, interrupted = false} = {}) {
  const row = document.createElement("div");
  row.className = "tool-total muted";
  const saved = durationText(body?.cost?.wall_ms), fallback = durationText(browserMs);
  row.textContent = `总耗时：${saved ?? (fallback ? `${fallback}（浏览器计时${interrupted ? "；未收到完整结果" : ""}）` : "未记录")}`;
  row.title = saved ? "服务端记录的本轮处理墙钟耗时，包含重试与等待；并行调用不重复相加。" :
    fallback ? "浏览器从本轮请求开始计时，含网络等待，不代表服务端已执行完成。" : "旧记录没有留存整轮耗时，不能根据工具耗时相加推算。";
  container.appendChild(row);
  return row;
}
