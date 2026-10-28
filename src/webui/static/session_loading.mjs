// Cache only directory metadata. History stays on the scoped, paginated server API.
const fields = ["session_id", "observed_id", "title", "last_message", "turn_count",
  "created_at", "last_turn_at", "updated_at", "selected_at", "is_last"];
export function directory(items) {
  const seen = new Set();
  return (Array.isArray(items) ? items : []).filter(item => {
    if (!item || typeof item.session_id !== "string" || !item.session_id || item.session_id.startsWith("alert_") || seen.has(item.session_id)) return false;
    seen.add(item.session_id); return true;
  }).slice(0, 100).map(item => Object.fromEntries(fields.filter(k => k in item &&
    (item[k] === null || ["string", "boolean"].includes(typeof item[k]) ||
      (typeof item[k] === "number" && Number.isFinite(item[k]))))
    .map(k => [k, typeof item[k] === "string" ? item[k].slice(0, k === "last_message" ? 500 : 200) : item[k]])));
}
export class SessionDirectoryCache {
  constructor(storage, key) { this.storage = storage; this.key = key; }
  read() {
    try {
      const value = JSON.parse(this.storage.getItem(this.key()) || "null");
      return value?.version === 1 ? directory(value.items) : [];
    } catch { return []; }
  }
  write(items) {
    try { this.storage.setItem(this.key(), JSON.stringify({ version: 1, items: directory(items) })); }
    catch { /* Restricted storage/quota does not block server-backed conversations. */ }
  }
}
function latest(items) {
  const selected = items.filter(item => item.selected_at).sort((a, b) =>
    Date.parse(b.selected_at) - Date.parse(a.selected_at));
  return (selected[0] || items[0])?.session_id || null;
}
export class SessionLoader {
  constructor({ api, cache, current, select, list, page, pending, error, listError = error }) {
    Object.assign(this, { api, cache, current, select, list, page, pending, error, listError });
    this.reset();
  }
  reset() {
    this.epoch = (this.epoch || 0) + 1; this.listSeq = 0; this.historySeq = 0;
    this.items = []; this.loadedId = null; this.loadingId = null; this.inflight = null;
  }
  publish(items) { this.items = directory(items); this.cache.write(this.items); this.list(this.items); }
  update(item, supersede = true) {
    // A metadata mutation supersedes any directory refresh started before it.
    if (supersede) this.listSeq++;
    this.publish([item, ...this.items.filter(row => row.session_id !== item.session_id)]);
  }
  remove(id) { this.listSeq++; this.publish(this.items.filter(row => row.session_id !== id)); }
  activate(id) {
    this.historySeq++; this.loadingId = null; this.inflight = null;
    this.loadedId = id; this.select(id); this.pending(false);
  }
  async start(stored) {
    this.list(this.items = this.cache.read());
    const id = stored || latest(this.items), epoch = this.epoch, sequence = this.historySeq;
    // Start the selected history and list independently; never prefetch other histories.
    const history = id ? this.open(id) : Promise.resolve(null);
    if (!id) this.pending(true);
    const initialSequence = id ? this.historySeq : sequence;
    const refreshed = this.refresh().then(items => {
      if (this.epoch !== epoch || this.historySeq !== initialSequence || id || !items) return null;
      const chosen = latest(items);
      if (chosen) return this.open(chosen);
      this.pending(false); return null;
    });
    await Promise.all([history, refreshed]);
    if (!id && this.epoch === epoch && this.historySeq === initialSequence) this.pending(false);
    return this.current();
  }
  async refresh() {
    const epoch = this.epoch, sequence = ++this.listSeq;
    try {
      const result = await this.api("/v1/sessions");
      if (result.stale || epoch !== this.epoch || sequence !== this.listSeq) return null;
      if (result.status !== 200 || !Array.isArray(result.data?.sessions)) throw new Error(`HTTP ${result.status}`);
      const selected = this.loadedId === this.current() && this.items.find(item => item.session_id === this.loadedId);
      const items = result.data.sessions;
      // A recent-directory limit does not invalidate an older, successfully opened session.
      this.publish(selected && !items.some(item => item.session_id === selected.session_id) ? [selected, ...items] : items);
      this.listError(""); return this.items;
    } catch (error) {
      if (epoch === this.epoch && sequence === this.listSeq)
        this.listError(`会话列表刷新失败：${error.message}；保留已有列表，可点击刷新重试`, "warn");
      return null;
    }
  }
  open(id) {
    if (id === this.loadingId) return this.inflight;
    if (id === this.loadedId) return Promise.resolve(id);
    const epoch = this.epoch, sequence = ++this.historySeq;
    this.select(id); this.loadedId = null; this.loadingId = id; this.pending(true);
    const valid = () => epoch === this.epoch && sequence === this.historySeq;
    this.inflight = (async () => {
      try {
        const result = await this.api(`/v1/sessions/${encodeURIComponent(id)}`);
        if (result.stale || !valid()) return null;
        if (result.status !== 200 || result.data?.session?.session_id !== id) {
          if (result.status === 404) { this.remove(id); this.select(null); }
          throw new Error(`HTTP ${result.status}`);
        }
        this.loadedId = id;
        this.page(result.data);
        // Updating the row does not refetch the whole directory or any other history.
        this.update(result.data.session, false); return id;
      } catch (error) {
        if (valid()) {
          this.loadedId = null;
          this.error(`会话历史加载失败：${error.message}；点击该会话可重试`, "bad");
        }
        return null;
      } finally {
        if (valid()) { this.loadingId = null; this.inflight = null; this.pending(false); }
      }
    })();
    return this.inflight;
  }
}
