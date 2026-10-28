// 分页控制不重绘已有卡片：保存视口、防重请求、切换后丢弃旧响应。
export function createHistoryPager({ container, fetchPage, renderTurn, notify }) {
  let session = "", cursor = null, loading = false, epoch = 0, marker = null;
  const seen = new Set();
  const key = turn => String(turn.turn_id || turn.incident_id || `${turn.at}:${turn.user_message}`);
  function reset() { epoch++; session = ""; cursor = null; loading = false; seen.clear(); marker = null; }
  function label() {
    if (marker) { marker.disabled = loading || !cursor; marker.textContent = loading
      ? "正在加载更早记录…" : `已加载 ${seen.size} 轮 · ${cursor ? "向上滑动或点击加载更早 20 轮" : "已到最早可读取记录"}`; }
  }
  function init(id, data) {
    reset(); session = id; cursor = data.has_more ? data.next_cursor : null;
    for (const turn of data.turns || []) seen.add(key(turn));
    marker = document.createElement("button"); marker.className = "ghost history-more";
    marker.addEventListener("click", load); container.prepend(marker); label();
  }
  async function load() {
    if (!session || !cursor || loading) return;
    const ownEpoch = epoch, ownSession = session, ownCursor = cursor;
    loading = true; label();
    try {
      const result = await fetchPage(ownSession, ownCursor);
      if (epoch !== ownEpoch || session !== ownSession) return;
      if (result.status !== 200) throw new Error(`HTTP ${result.status}`);
      const data = result.data, fragment = document.createDocumentFragment();
      const height = container.scrollHeight, top = container.scrollTop;
      for (const turn of data.turns || []) {
        if (seen.has(key(turn))) continue;
        seen.add(key(turn)); for (const node of renderTurn(turn)) fragment.appendChild(node);
      }
      marker.after(fragment);
      container.scrollTop = top + container.scrollHeight - height;
      cursor = data.has_more ? data.next_cursor : null;
      if ((data.degraded || []).length) notify(data.degraded.join("；"), "warn");
    } catch (error) {
      if (epoch === ownEpoch) notify(`更早记录加载失败：${error.message}，可再次上滑或点击重试`, "warn");
    } finally { if (epoch === ownEpoch) { loading = false; label(); } }
  }
  container.addEventListener("scroll", () => { if (container.scrollTop < 72) load(); }, { passive: true });
  return { init, reset, load };
}
