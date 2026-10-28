const escape = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

export function registered(rows) {
  return rows.filter((s) => s.active && s.registered_at && s.services?.some((v) => v.active));
}

export function systemContext(id, rows) {
  return registered(rows).some((s) => s.observed_id === id) ? {observed_id:id} : {};
}

function instanceCard(i) {
  const caps = (i.capabilities || []).map((c) => `<li><code>${escape(c.method)} ${escape(c.path)}</code> · v${escape(c.version)} · ${c.enabled ? "已申报" : "未启用"}</li>`).join("");
  const routes=(i.endpoints || []).map((e)=>`<li><code>${escape(e.method)} ${escape(e.path)}</code> · ${escape(e.description)} · ${escape(e.policy?.effect || "unknown")} · ${e.policy?.probe ? "允许核验" : "未开放核验"} · ${e.policy?.load ? "允许受控负载" : "未开放负载"}<details><summary>参数与限额</summary><pre>${escape(JSON.stringify({parameters:e.parameters,body_schema:e.body_schema,policy:e.policy},null,2))}</pre></details></li>`).join("");
  return `<details><summary>实例 ${escape(i.instance)} · ${escape(i.env)} · 启动 ${escape(i.boot_id)}</summary><p>SDK ${escape(i.sdk_version)} · ${escape(i.base_url)}</p><ul>${caps}</ul><details><summary>业务接口 · ${(i.endpoints || []).length} 项 · ${escape(i.endpoint_status || "legacy_unavailable")}</summary><p>登记版本 ${escape(i.endpoint_revision || "—")}；登记接口不等于授权调用。</p><ul>${routes || "尚无业务接口清单，请升级SDK或配置适配器。"}</ul></details></details>`;
}

export function renderSystem(s) {
  const services = (s.services || []).map((v) => `<details><summary>${escape(v.service)} · ${v.active ? "已登记" : "已停用"} · ${(v.instances || []).length} 个启动记录</summary>${(v.instances || []).map(instanceCard).join("")}</details>`).join("");
  const access = s.last_access ? `${escape(s.last_access.finished_at)} · ${s.last_access.ok ? "访问成功" : escape(s.last_access.error)}` : "已注册，当前可达性未核查";
  const enter=s.active && s.registered_at && s.services?.some((v)=>v.active);
  return `<article class="observed-card"><div class="observed-heading"><h3>${escape(s.name)} <span class="badge ${s.active ? "unknown" : "bad"}">${s.active ? "已登记" : "已停用"}</span></h3><button class="primary" data-enter="${escape(s.observed_id)}" ${enter ? "" : "disabled"}>进入工作台 →</button></div><p class="mono">${escape(s.observed_id)}</p><p>首次注册：${escape(s.registered_at || "等待 SDK 注册")}<br>最近注册：${escape(s.last_registered_at || "—")}</p><p>最近实际访问：${access}</p><button data-access="${escape(s.observed_id)}">查看调用档案</button><details><summary>服务及 SDK 接口 · ${(s.services || []).length} 个服务</summary>${services || '<p class="muted">暂无注册服务</p>'}</details></article>`;
}

export class ObservedSystems {
  constructor(api, doc = document, storage = localStorage) {
    this.api=api; this.doc=doc; this.storage=storage; this.rows=[]; this.cursor=null; this.busy=false;
  }
  selected() { return systemContext(this.doc.getElementById("observed-select")?.value, this.rows); }
  init() {
    this.doc.getElementById("observed-list").addEventListener("click", (event) => {
      const enter=event.target.closest("[data-enter]");
      if (enter && !enter.disabled) location.hash="system="+enter.dataset.enter;
      const button=event.target.closest("[data-access]");
      if (button) this.access(button.dataset.access);
    });
    this.doc.getElementById("observed-more").addEventListener("click", () => this.load(false));
    this.doc.getElementById("observed-refresh").addEventListener("click", () => this.load(true));
    this.doc.getElementById("observed-select").addEventListener("change", (event) => {
      this.storage.setItem("aiops_observed_id", event.target.value);
      const row=this.rows.find((s)=>s.observed_id===event.target.value);
      const envs=[...new Set((row?.services || []).flatMap((s)=>(s.instances || []).map((i)=>i.env)))];
      if (envs.length===1) this.doc.getElementById("env").value=envs[0];
      this.doc.getElementById("observed-selection-note").textContent = "后续提问使用所选系统；既有任务和历史保留原系统归属。";
    });
    this.doc.querySelector('[data-panel="observed"]').addEventListener("click", () => this.load(true));
    return this.load(true);
  }
  async load(reset=false) {
    if (this.busy) return;
    this.busy=true; const note=this.doc.getElementById("observed-note"); note.textContent="正在读取注册目录…";
    this.doc.getElementById("observed-more").disabled=true;
    this.doc.getElementById("observed-refresh").disabled=true;
    try {
      const path="/v1/observed-systems?limit=20" + (!reset && this.cursor ? `&before=${encodeURIComponent(this.cursor)}` : "");
      const {status,data}=await this.api(path);
      if (status!==200 || !data?.systems) throw Error(`注册目录请求失败（${status}）`);
      this.rows=reset ? data.systems : [...this.rows,...data.systems]; this.cursor=data.next_cursor;
      const saved=this.doc.getElementById("observed-select").value || this.storage.getItem("aiops_observed_id") || "";
      if (saved && !this.rows.some((s) => s.observed_id===saved)) {
        const result=await this.api(`/v1/observed-systems/${encodeURIComponent(saved)}`);
        if (result.status===200) this.rows.push(result.data);
      }
      this.draw(saved); this.doc.getElementById("observed-more").hidden=!data.has_more;
      note.textContent=this.rows.length ? `已加载 ${this.rows.length} 个系统；登记状态不表示实时在线。` : "暂无被观测系统，请先完成 SDK 接入注册。";
    } catch (error) { note.textContent=error.message; this.doc.getElementById("observed-selection-note").textContent="注册目录不可读，请核查接入；当前系统仍由服务端校验。"; }
    finally { this.busy=false; this.doc.getElementById("observed-more").disabled=false; this.doc.getElementById("observed-refresh").disabled=false; }
  }
  draw(saved) {
    this.rows=[...new Map(this.rows.map((s)=>[s.observed_id,s])).values()];
    this.doc.getElementById("observed-list").innerHTML=this.rows.map(renderSystem).join("");
    const select=this.doc.getElementById("observed-select");
    select.innerHTML='<option value="">请选择系统</option>'+registered(this.rows).map((s)=>`<option value="${escape(s.observed_id)}">${escape(s.name)} · ${escape(s.observed_id.slice(-8))}</option>`).join("");
    select.value=registered(this.rows).some((s)=>s.observed_id===saved) ? saved : "";
    if (!select.value) this.storage.removeItem("aiops_observed_id");
  }
  async access(oid) {
    const dialog=this.doc.createElement("dialog"); dialog.className="access-dialog";
    const close=this.doc.createElement("button"); close.textContent="关闭"; close.onclick=()=>dialog.remove();
    const body=this.doc.createElement("pre"); body.textContent="正在读取调用档案…";
    dialog.append(close,body);this.doc.body.append(dialog);dialog.showModal();
    const response=await this.api(`/v1/observed-systems/${encodeURIComponent(oid)}/access?limit=20`);
    body.textContent=response.status===200 ? "" : `调用档案读取失败（${response.status}）`;
    for (const row of response.data?.attempts || []) {
      const link=this.doc.createElement("button");link.textContent=`${row.finished_at} · ${row.tool} · ${row.error} · 尝试 ${row.attempt}`;
      link.onclick=async()=>{const r=await this.api(`/v1/observed-systems/${encodeURIComponent(oid)}/access/${encodeURIComponent(row.attempt_id)}`);body.textContent=JSON.stringify(r.data,null,2);};
      dialog.insertBefore(link,body);
    }
    if (response.status===200 && !(response.data?.attempts || []).length) body.textContent="暂无调用档案。登记不表示实时可达。";
  }
}
