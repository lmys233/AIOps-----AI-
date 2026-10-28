"""编排器 —— 按 `graph.py` 的描述驱动一次排查。

--------------------------------------------------------------------------------
★ 一次请求的骨架（与图一一对应）
--------------------------------------------------------------------------------

```
resolve → route → clarify ─┬─(要问)────────────────────────► report
                           └─(不问)─► forensics → hypotheses → decide
                                                              ├─(定向)─► directed → forensics
                                                              └────────► report
```

`run()` **只做三件事**：建状态、按图走节点、落检查点。所有判定都在节点函数里，
所有上限都在 `Budget` 里，所有"要不要问"都在 `ClarifyBudget` 里 ——
没有第四处地方偷偷改状态（比"图能编译"更重要的性质：**图是唯一控制流**）。

--------------------------------------------------------------------------------
★★ 四个必须写清楚的口径
--------------------------------------------------------------------------------

1. **预算来自请求与配置的较严者**：`options.max_rounds / token_budget` 是调用方上限，
   `AgentConfig` 是平台上限 → 取 min。以前只把 `options` **回声**出来
   （`enforced=false`），现在真的接上：`enforced` 逐字段给真值、`used` 给真实消耗。
2. **取证只对 `INCIDENT_RCA` 开**：其余意图当前没有可落地的查询路径，
   直接进 `report` 并写"接下来会查什么"。★ **不假装查过**：没查就不写证据、
   也不写"没异常"（不把"没查成"说成"没有异常"）。
3. **`status=clarify` 只有一个出口**：只有 `clarify` 节点能把它置成 `clarify`。
   工具层的任何错误码都进不了这条路。
4. **打分是临时的**（`ProvisionalScorer`）：分数只用来决定"要不要再查一轮"，
   不足以支撑根因结论；响应里 `explanation.scoring_kind = provisional(...)` 明说这一点，
   配置了 RCA 打分器时会被真公式替换。
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any, Final, Mapping, Optional, Sequence

from ..agents.runner import ForensicsRunner, default_agents
from ..attribution import CriticAgent, RcaAgent
from ..common.enums import EvidenceSource, Intent
from ..common.steps import record_process_step as _record_process_step
from ..common.steps import record_tool_call as _record_tool_call
from ..memory.queries import use_scope
from ..memory.session import ReferenceResolution
from .actions import ActionPlanMixin, proposal_blocked_for_target
from .evidence_scope import SOURCE_CURRENT
from .bootstrap import BootstrapMixin
from .budget import Budget
from .checkpoint import Checkpointer, InMemoryCheckpointer
from .clarify import (ClarifyBudget, ClarifyDecision, DEFAULT_MAX_QUESTIONS,
                      question_text, replace_unconfirmable_confirm)
from .constants import (CAN_PRODUCE_ROOT_CAUSE, EVIDENCE_INTENTS, LIGHT_INTENTS,
                        EventSink as EventSink_, INCIDENT_ID_PREFIX,
                        REMEDIATION_INTENTS, SINGLE_ROUND_INTENTS, logger, new_incident_id)
from .coordinate import CoordinateMixin
from .memory_scope import MemoryScopeMixin
from .decide_node import DecideMixin
from .facts import FactsMixin
from .forensics_node import ForensicsMixin
from .graph import Graph, MAX_NODE_STEPS, build_graph
from .hypotheses import (CATEGORY_AGENT_ROLE, ProvisionalScorer, HypothesisScorer)
from .hypotheses_node import AttributionMixin
from .state import GraphState, TOP2_CLOSE, TerminationDecision, board_store, should_terminate

#: 事件回调：`(event, data)` —— **同步**即可（服务壳把它塞进 `asyncio.Queue` 供 SSE 读）。
#: ★ 为什么不是 async：编排的关键路径上多一个 `await` 就多一个让出点，
#:   而"发事件"对判定没有任何影响；服务壳那边的队列写入本身是同步的。
#: ★ 类型别名与共享常量搬到了 `constants.py`（避免 mixin ↔ orchestrator
#:   循环导入）；这里**再导出**，所以 `orchestrator.EventSink` 之类的老路径照旧可用。
EventSink = EventSink_

__all__ = ["Orchestrator", "OrchestrationResult", "INCIDENT_ID_PREFIX", "new_incident_id",
           "EventSink", "CAN_PRODUCE_ROOT_CAUSE"]

#: 信号源 → 取证角色（定向二次取证时按"缺哪一路"叫谁）
_SOURCE_ROLE: Final[dict[str, str]] = {
    EvidenceSource.METRICS.value: "metrics_agent",
    EvidenceSource.LOGS.value: "log_agent",
    EvidenceSource.TRACES.value: "trace_agent",
    EvidenceSource.CHANGES.value: "change_agent",
    EvidenceSource.TOPOLOGY.value: "topology_agent",
}


# ======================================================================
@dataclass
class OrchestrationResult:
    """一次编排的产物：状态 + **给人看的事实**（服务壳据此渲染文案）。"""

    state: GraphState
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return self.state.status

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "state": self.state.to_dict(), "facts": self.facts}


# ======================================================================
@dataclass
class Orchestrator(BootstrapMixin, ActionPlanMixin, ForensicsMixin,
                   AttributionMixin, DecideMixin, FactsMixin, CoordinateMixin,
                   MemoryScopeMixin):
    """把"意图 → 澄清 → 取证 → 假设 → 终止 → 报告"串成一条可控的流程。

    ★★ 本文件曾经 1115 行。现在它只留**控制流骨架** ——
      字段 + `run()` + 节点分派 + 不依赖外部子系统的几个节点；每个"有事可讲"的
      节点搬进自己的 mixin 模块（`bootstrap` / `actions` / `forensics_node` /
      `hypotheses_node` / `decide_node` / `facts`）。
      **判据没变、实现没有第二份**：搬走的都是原方法体，一行没改。
    """

    router: Any
    session: Any = None                       # SessionMemory（None → 不做指代消解）
    #: ★★ 长期记忆入口（`MemoryService`）：处置方案要用的**程序性 / 情景**两层靠它召回
    #:   （这类问题以前怎么处置的）。`None` = 没接入 → 方案只依据本轮证据并**如实留痕**。
    memory: Any = None
    executor: Any = None                      # ToolExecutor（None → 不取证，只给方向）
    board: Any = None                         # BoardStore（None → 每次 run 一个内存黑板）
    runner: Any = None                        # ForensicsRunner（None → 按 executor+board 建）
    #: 打分器：默认是 **RCA Agent**（D1 先验 + D2 对数空间 softmax）；
    #: `ProvisionalScorer` 仍可注入，用于消融对照。
    scorer: HypothesisScorer = field(default_factory=RcaAgent)
    #: ★★ 窄触发统筹器（`None` = 按路由器上的网关现建；没有网关则**不统筹**）。
    #:   显式注入优先 —— 单测/消融要能钉住"统筹到底有没有被调用"。
    coordinator: Any = None
    #: ★★ R1 的**向量兜底** embedder（`None` = 用既有的词法实现；可注入替换）
    recall_embedder: Any = None
    #: Critic：反事实检验（只排除、不新增）。`lookup` 在每次 run 里绑定（接黑板/工具）。
    critic: CriticAgent = field(default_factory=CriticAgent)
    checkpointer: Checkpointer = field(default_factory=InMemoryCheckpointer)
    clarify_max_questions: int = DEFAULT_MAX_QUESTIONS
    graph: Graph = field(default_factory=build_graph)
    attribution_v2_enabled: bool = False

    # ------------------------------------------------------------------
    async def run(
        self,
        *,
        session_id: str,
        trace_id: str,
        message: str,
        env: Optional[str] = None,
        cluster: Optional[str] = None,
        incident_id: Optional[str] = None,
        usage: Sequence[Any] = (),
        budget: Optional[Budget] = None,
        options: Any = None,
        resume_from: Optional[str] = None,
        active_tasks: Sequence[Mapping[str, Any]] = (),
        recent_tasks: Sequence[Mapping[str, Any]] = (),
        inherited: Optional[Mapping[str, Any]] = None,
        deployment_form: str = "",
        on_event: Optional[EventSink] = None,
        task_control: Optional[Mapping[str, Any]] = None,
    ) -> OrchestrationResult:
        """跑一次编排。**永远返回结果**（判不出来/查不到/超预算都有对应状态）。

        Args:
            active_tasks: ★ 本会话**搁置中的任务**（只服务窄触发统筹"这句话在说哪件事"）。
            recent_tasks / inherited: ★ 前者是**刚完成的任务**（传进来就必须真的进 flags）；
                后者是**上一轮那次带证据的排查**（调用方按"同会话、最近、没过期"挑好）。
            deployment_form: ★ 部署形态事实（`k8s`/`host_process`/`unknown`）决定机制名词（没配说"实例"）。
            on_event: 可选的事件回调（服务壳的 SSE 用）。事件**每个都带 `trace_id`**
                （流式接口要求），事件名取自约定的那 9 种类型。
        """
        state = await self._build_state(
            session_id=session_id, trace_id=trace_id, message=message, env=env,
            cluster=cluster, incident_id=incident_id, budget=budget, options=options,
            usage=usage, resume_from=resume_from, active_tasks=active_tasks,
            recent_tasks=recent_tasks, inherited=inherited, deployment_form=deployment_form)
        if on_event is not None:
            state.flags["on_event"] = on_event
        if task_control:
            from .state import TaskBinding
            state.task_binding=TaskBinding.from_mapping(task_control)
            if task_control.get('webprobe_context'):state.flags['webprobe_context']=task_control['webprobe_context']
        # ★★ 把"查询数据记忆"的作用域设成**本次会话**，跑完再把索引写回会话工作记忆
        #   （取数层据此判断"这段窗口是不是已经有了" → 不发请求 / 只补缺口）。
        notes_scope = await self._execute_scoped_graph(state,session_id)
        # ★ 取数留痕（记忆命中 / 只补缺口 / 缓存命中 / fail-open）汇进 `degraded` —— 必须可见
        #   ★★ 必须传**槽对象本身**：`with` 退出时 ContextVar 已 reset，再读就是空的
        self._collect_tool_notes(state, notes_scope.sink)
        await self._finish(state)
        return OrchestrationResult(state=state, facts=self._facts(state))

    async def _execute_scoped_graph(self,state,session_id):
        await self._hydrate_query_memory(session_id)
        notes=self._tool_notes_scope()
        try:
            with notes,use_scope(session_id):
                await self._walk_graph(state,started=time.monotonic())
        finally:await self._persist_query_memory(session_id)
        return notes

    async def _walk_graph(self, state: GraphState, *, started: float) -> None:
        """**按图驱动**直到终止节点（`run()` 的第二半，从 52 行里抽出来）。

        ★★ 续跑从**检查点记下的那个节点**开始，而不是永远从入口重来。
          `pop` 而不是 `get`：它是"本次运行的一次性输入"，不该留在 flags 里被当成状态。
        ★ 环走飞了（条件边写错/节点不前进）→ 如实收口，不转圈烧钱。
        """
        node = state.flags.pop("resume_node", None) or self.graph.entry
        from ..execution.runtime import CURRENT
        execution=CURRENT.get()
        if execution:node=await execution.enter_graph(state,node)
        elapsed_base=state.budget.elapsed_sec
        for _ in range(MAX_NODE_STEPS):
            if execution:await execution.checkpoint(state,node)
            nxt = await self._handler(node)(state)
            from .usage import sync_usage
            sync_usage(state)
            state.budget.observe_elapsed(elapsed_base+time.monotonic() - started)
            if nxt is None:
                nxt = self.graph.next_of(node)
            if execution:await execution.checkpoint(state,nxt)
            if node != self.graph.terminal and nxt == self.graph.terminal:
                await self._checkpoint(state, resume_node=nxt)
            if node == self.graph.terminal:
                return
            node = nxt
        decision = should_terminate(
            state, budget_reason=f"节点步数超过 {MAX_NODE_STEPS}（图可能写错了）")
        state.termination = decision
        self._apply_status(state, decision)
        state.add_note(f"编排在 {MAX_NODE_STEPS} 步后强制收口（自环次数异常）")

    # ------------------------------------------------------------------
    #  节点分派（图里有名 = 这里有实现）
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    #  事件（SSE 类型；每个事件都带 trace_id）
    # ------------------------------------------------------------------
    @staticmethod
    def _emit(state: GraphState, event: str, data: Mapping[str, Any]) -> None:
        # ★★ 工具调用要进**会话记忆**（刷新后恢复工具卡 + 下一轮给模型看），
        #   所以先**无条件**记进 state：没有 SSE 订阅者（同步 `/v1/chat`）时同样要有。
        if event == "tool":
            _record_tool_call(state, data)
        # ★★ 执行过程也要进会话记忆：刷新后「推理过程」里的 `intent` / `agent_start` /
        #   `agent_done` / `report` 这些行以前只走 SSE → 刷新就没了（用户实测）。
        #   记的是**顺序 + 有界载荷**，渲染仍由前端 `summarizeEvent` 一处做。
        _record_process_step(state, event, data)
        sink: Optional[EventSink] = state.flags.get("on_event")
        if sink is None:
            return
        payload = {"trace_id": state.trace_id, "incident_id": state.incident_id,
                   "round": state.round, **dict(data)}
        try:
            sink(event, payload)
        except Exception:  # noqa: BLE001 —— 发事件失败绝不能影响排查
            logger.warning("事件回调失败（event=%s）", event, exc_info=False)

    def _handler(self, node: str) -> Any:
        handlers = {
            "resolve": self._node_resolve,
            "route": self._node_route,
            "clarify": self._node_clarify,
            "forensics": self._node_forensics,
            "hypotheses": self._node_hypotheses,
            "decide": self._node_decide,
            "directed": self._node_directed,
            "report": self._node_report,
        }
        if node not in handlers:
            raise KeyError(f"图里有节点 {node!r} 但没有实现 —— 图的描述与实现不一致")
        return handlers[node]




    # ------------------------------------------------------------------
    #  ① resolve：代词消解（必须在意图层之前）
    # ------------------------------------------------------------------
    async def _node_resolve(self, state: GraphState) -> str:
        if state.task_binding:return 'route'
        session = self._session()
        if session is None or not state.session_id:
            state.add_note("未接入会话记忆 → 不做指代消解（跨会话本来也不消解）")
            return "route"
        try:
            resolution: ReferenceResolution = await session.resolve_reference(
                state.session_id, state.user_message)
        except Exception as exc:  # noqa: BLE001 —— 记忆不可用不该让整次请求失败
            state.add_note(f"指代消解失败（{type(exc).__name__}: {exc}）→ 按原句判定")
            return "route"
        state.reference = resolution
        if resolution.resolved and resolution.pronoun:
            # ★ 只替换代词本身，不动其余文字（改整句会改掉用户的原始表述）
            state.user_message = state.user_message.replace(
                resolution.pronoun, str(resolution.service), 1)
            state.add_note(f"指代消解：{resolution.note}")
        elif resolution.resolved and resolution.service:
            # ★★ **省略式追问**（"怎么解决？"）没有代词可替换 —— 把服务**补进消息**，
            #   让槽位抽取接手（与时间窗同一手法：不新造契约字段）。
            #   修前这一步缺失 → 服务丢了 → 追问目标（用户实测："AI 却要求我补充目标服务"）。
            state.user_message = f"{state.user_message}（{resolution.service}）"
            state.add_note(f"省略式追问的指代消解：{resolution.note}")
        elif resolution.pronoun or resolution.service:
            state.add_note(f"指代未消解：{resolution.note}")
        return "route"

    # ------------------------------------------------------------------
    #  ② route：意图 + 槽位（预算里的模型调用在这里发生）
    # ------------------------------------------------------------------
    async def _node_route(self, state: GraphState) -> str:
        # ★ 把"本会话已经问过的缺口"与"用户要求别问"传进路由 ——
        #   反问的**去重**与**禁问**都在仲裁器里统一判。
        asked_gaps = tuple(state.flags.get("asked_gaps") or ())
        no_clarify = bool(state.flags.get("no_clarify"))
        outcome = (state.task_binding.outcome if state.task_binding else None) or await self.router.route(state.user_message, env=state.slots.env,
                                          cluster=state.slots.cluster,
                                          asked_gaps=asked_gaps,
                                          no_clarify=no_clarify)
        self._apply_route(state, outcome)
        # ★★ 窄触发统筹 —— 只有「规则层未定案 + 有活跃任务 + 本来要拒答」
        #   三条同时成立才插手；要接续就用任务上下文**重跑一次**（最多 1 次，见 mixin）。
        rerouted = None if state.task_binding else await self._maybe_coordinate(state, outcome)
        if rerouted is not None:
            self._apply_route(state, rerouted)
        return "clarify"

    def _tokens_so_far(self, state: GraphState) -> int:
        """本次请求累计 token（来自 LLM 网关的用量回调；拿不到就是 0）。

        ★ 记的是**增量**：`usage` 是整次请求的账本，预算已经记过的部分不重复计。
        """
        usage = state.flags.get("usage") or ()
        total = 0
        for record in usage:
            total += int(getattr(record, "input_tokens", 0) or 0)
            total += int(getattr(record, "output_tokens", 0) or 0)
        return max(0, total - state.budget.tokens)

    # ------------------------------------------------------------------
    #  ③ clarify：澄清预算的**唯一出口**
    # ------------------------------------------------------------------
    async def _node_clarify(self, state: GraphState) -> str:
        if state.intent is Intent.KNOWLEDGE_QA:
            from ..agents.qa_agent import QaAgent
            return await QaAgent().run(self,state)
        # ---- ★ 写意图先出**处置建议 + 预案 + 风险分级**（它同时决定"有没有可确认的东西"）----
        #   ★★ 这一轮没有冲着点名服务取到证据时，这里**只出占位的一条**
        #   （"先补齐证据"），真实方案要等取证之后（`report` 节点）才算得出来。
        self._prepare_action(state)
        selected = state.flags.get("selected_plan")
        if self.attribution_v2_enabled:
            from ..attribution.v2.followup import reuse
            if reuse(state):return 'report'
        self._mark_confirmable(state, selected)
        decision = await self._decide_clarify(state)
        routed = await self._consume_clarify(state, decision)
        if routed is not None:
            return routed
        #   ★★ 没有取证执行器 / 意图不在取证链路里时，上面那次 `_prepare_action`
        #   已经是最终结论（真实方案要等取证，见 `actions.py` 的口径）。
        if state.intent not in EVIDENCE_INTENTS:
            state.flags["forensics_not_applicable"] = True
            return self._close_without_forensics(state, selected)
        return "forensics"

    async def _consume_clarify(self, state: GraphState,
                               decision: Optional[ClarifyDecision]) -> Optional[str]:
        """裁决澄清：要问就**问出去**；处置轮要问时**先挂起**去取证。

        ★★ 为什么处置轮要挂起：否则"缺服务 / 要点确认"会把这一轮直接截断，
          用户拿到的是一个**零取证**的答案（真机实测：问 order 线程池，回的是
          "没发现任何异常" + 一条 inventory 的 SQL 方案）。挂起只是把那一问推迟到
          `report`，**同一份裁决、同一句话**，判定口径一个字没改。
        Returns:
            下一个节点名；`None` = 继续往下走（不打断）。
        """
        if decision is None:
            return None
        state.flags["clarify"] = decision
        if state.intent in REMEDIATION_INTENTS:
            # ★★ 处置轮：这一问**推迟到取证之后重算**（不是"原样推迟"）。
            #   取证之前手里没有证据，算出来的问题与取证之后的方案对不上 ——
            #   零证据时只能问"我先查什么"，而查到证据、有了可落地方案之后
            #   该问的是**安全确认门**（`report` 节点按当时的事实重算，见 `_ask_deferred_clarify`）。
            state.flags["clarify_deferred"] = True
            return "forensics"
        if decision.ask:
            return await self._ask_clarify(state, decision)
        # ★ 预算压掉澄清 → 降级继续，并把不确定性写进结论（不静默）
        state.degraded_detail.append(f"澄清被预算压掉：{decision.reason}")
        state.add_note(decision.reason)
        return None

    async def _ask_deferred_clarify(self, state: GraphState) -> bool:
        """把推迟到取证之后的那一问**按当前事实重算**并问出去（问了返回 `True`）。

        ★ 为什么必须重算：同一份裁决在"取证前 / 取证后"会指出**不同**的一句话 ——
          取证前只能问"我先查什么"（问"要不要执行"用户接不住：真机实测会死循环）；
          取证后拿到了证据、也有了可落地的预案，该问的是**安全确认门**。
          不重算则这一轮永远问"先取证"，而方案表里已经躺着一条 L3 方案（自相矛盾）。
        ★ 安全门一个字没动：重算只换"问哪一句"，`confirm` 问题依旧不占信息预算；
          信息问题被压掉时照样把"压掉了什么"写进降级说明（不静默）。
        """
        decision: Optional[ClarifyDecision] = (
            await self._decide_clarify(state) or state.flags.get("clarify"))
        if decision is None:
            return False
        state.flags["clarify"] = decision
        if decision.ask:
            await self._ask_clarify(state, decision)
            return True
        state.degraded_detail.append(f"澄清被预算压掉：{decision.reason}")
        state.add_note(decision.reason)
        return False

    def _close_without_forensics(self, state: GraphState, selected: Any) -> str:
        """不取证的意图在 clarify 处收口（状态在这里定死，不留给 report 默认）。"""
        label = state.intent.value if state.intent else "（未定意图）"
        why = f"{label} 在本阶段没有落地查询路径（不取证）"
        if selected is not None:
            # ★ 用户已经选了方案 → 这不是"没有落地路径"，
            #   而是**如实收口**：展示已收集的证据与所选方案的流程。
            why = "写意图本阶段没有写通道（不执行变更），只给候选方案与流程"
        decision = self._terminal_without_target(state, why)
        self._apply_status(state, decision)
        state.add_note(f"{label} 没有落地的查询路径 → {state.status}：**不假装查过**")
        return "report"

    async def _ask_clarify(self, state: GraphState, decision: ClarifyDecision) -> str:
        """把澄清裁决**问出去**（把状态与正文定成"要用户补一句"）。"""
        state.status = "clarify"
        state.flags["clarify_allowed"] = True
        state.report = self._clarify_text(decision)
        await self._remember_asked(state, decision)
        return "report"

    @staticmethod
    def _mark_confirmable(state: GraphState, selected: Any) -> None:
        """写意图**有没有可确认的东西**（只有 `_prepare_action` 刚跑完时知道）。

        ★ 判据从"有没有单一动作"放宽成"**有没有候选方案**" —— 候选方案同样是
          可以继续的东西（用户挑一条，再走确认门）；没有候选才去问缺什么。
        ★ 有候选但用户**还没选**时先问"选哪个方案"：确认门等选完再问，
          否则用户看到的是"确认哪一个？"，而一次只能确认一个动作。
        """
        if state.intent is not Intent.REMEDIATION:
            return
        plans = tuple(plan for plan in (state.flags.get("remediation_plans") or ())
                      if not getattr(plan, "is_gap", False))
        has_action = state.flags.get("action") is not None
        state.flags["confirmable_action"] = bool(has_action or plans)
        if has_action:
            needs = ""                        # 明确提出单一动作时，保留原有安全确认门
        elif plans:
            # ★ 有候选方案但用户还没选 → 先问"选哪个方案"（这是**接得住**的一问）
            needs = "" if selected is not None else "remediation"
        elif state.flags.get("evidence_gap_pending"):
            # ★★ 认出了动作、但这一轮**没有证据** → 不许问"你确认要执行处置动作吗"
            #   （用户既没看到方案、也没看到动作，确认了也接不住：真机实测会变成
            #   "确认 → 再问一遍"的死循环）。能说的实话只有一句：我先查什么。
            needs = "evidence"
        elif state.flags.get("action_blocked"):
            # ★ 认出了动作但**没认出对谁做** → 照旧问服务（安全门那条路一个字不动）
            needs = "service" if proposal_blocked_for_target(state) else ""
        else:
            needs = ""
        state.flags["confirm_needs"] = needs
        if not state.flags["confirmable_action"]:
            state.add_note(
                "写意图但**没有可确认的预案、也没有候选方案** → 不问『要不要执行』"
                f"（问了也接不住）→ 改问『{needs or 'service'}』")


    async def _decide_clarify(self, state: GraphState) -> Optional[ClarifyDecision]:
        """有候选问题才裁决；一个问题都没有 → `None`（不是"决定不问"）。

        ★ `IntentResult.clarification` 是**契约 dict**（`{"questions": [...]}`），
          不是 `ClarificationQuestion` 对象 —— 两种形状都要认（落地时踩过：
          只认对象会让"澄清永远不发生"）。
        """
        result = state.route.result if state.route is not None else None
        payload = getattr(result, "clarification", None) if result is not None else None
        if isinstance(payload, Mapping):
            questions = tuple(payload.get("questions") or ())
            confirm = bool(payload.get("requires_confirmation"))
        else:
            questions = tuple(getattr(payload, "questions", ()) or ())
            confirm = bool(getattr(payload, "requires_confirmation", False))
        # ★★ 没有东西可确认时 → 把确认问句换成"缺什么问什么"（见 `clarify.py`）
        confirmable = state.flags.get("confirmable_action")
        questions = replace_unconfirmable_confirm(
            questions, confirmable=confirmable,
            needs=str(state.flags.get("confirm_needs") or ""))
        if confirmable is not True or state.flags.get("confirm_needs"):
            # ★ 确认门已经被换掉了（没东西可确认 / 先让用户选方案）→ 别再挂"这是确认门"
            confirm = False
        if not questions:
            return None
        already: set[str] = set()
        session = self._session()
        if session is not None and state.session_id:
            try:
                already = set(await session.asked_gaps(state.session_id))
            except Exception as exc:  # noqa: BLE001
                state.add_note(f"读取『已问缺口』失败（{type(exc).__name__}）→ 按没问过处理")
        return ClarifyBudget(max_questions=self.clarify_max_questions).decide(
            questions, already_asked=sorted(already), requires_confirmation=confirm)

    async def _remember_asked(self, state: GraphState, decision: ClarifyDecision) -> None:
        session = self._session()
        if session is None or not state.session_id:
            return
        for gap in decision.asked_gaps:
            try:
                await session.mark_asked(state.session_id, gap)
            except Exception as exc:  # noqa: BLE001
                state.add_note(f"记录『已问缺口』失败（{type(exc).__name__}）")

    @staticmethod
    def _clarify_text(decision: ClarifyDecision) -> str:
        return "；".join(question_text(q) for q in decision.questions)











    # ------------------------------------------------------------------
    #  ⑦ directed：把"要查什么"写成 open_questions（可解释的二次取证）
    # ------------------------------------------------------------------
    async def _node_directed(self, state: GraphState) -> str:
        # ★ **单轮取证**的意图只跑一轮（查现状 / 处置轮）。
        #   查现状的意图再"定向二次取证"没有意义：它本来就不判异常，
        #   多跑几轮只会把同一批数字再查一遍（并且把墙钟拉长）。
        if state.intent in SINGLE_ROUND_INTENTS:
            return await self._single_round_close(state)

        can, why = self._directed_target(state)
        if not can:                                  # 复检：预算可能在别处变了
            decision = self._terminal_without_target(state, why)
            state.termination = decision
            self._apply_status(state, decision)
            state.add_note(decision.reason)
            return "report"

        questions: list[str] = []
        # ★ 仲裁给的 open_questions 优先（它逐条说明"缺什么才能区分"）
        if state.arbitration is not None and state.arbitration.open_questions:
            questions.extend(str(item) for item in state.arbitration.open_questions)
        top = state.hypotheses[:2]
        if len(top) == 2 and abs(top[0].score - top[1].score) < TOP2_CLOSE:
            questions.append(
                f"{top[0].hypothesis_id} 与 {top[1].hypothesis_id} 分数接近"
                f"（{top[0].score:.2f} / {top[1].score:.2f}）—— 需要能区分它们的证据"
                f"（{top[0].category} vs {top[1].category}）")
        for signal in state.degraded_signals:
            questions.append(f"{signal} 这一路没查成 —— 需要重试该源"
                             f"（否则这条信号是空的，不是「没有异常」）")
        state.open_questions = list(dict.fromkeys(state.open_questions + questions))
        board = board_store(state, self.board, need="raise_questions")
        if board is not None:
            try:
                await board.raise_questions(state.incident_id, questions)
            except Exception as exc:  # noqa: BLE001
                state.add_note(f"缺口落黑板失败（{type(exc).__name__}）")

        roles = self._directed_roles(state)
        selected = [agent for agent in default_agents() if agent.role in roles]
        state.flags["directed_agents"] = selected or None
        state.directed_rounds += 1
        state.add_note(f"定向二次取证（第 {state.directed_rounds} 次）：{why}"
                       f" → 只叫 {sorted(roles) or '（无法定位角色，改跑全部）'}")
        return "forensics"

    async def _single_round_close(self, state: GraphState) -> str:
        """单轮取证意图的收口（★ 处置轮的**真实方案在这里才算**：取证已经跑完）。"""
        if state.intent not in LIGHT_INTENTS:
            # ★★ 先让模型**带着记忆**写方案（草稿），再走既有的候选/锚定/确认门那条路。
            await self._generate_ai_plans(state)
            self._prepare_action(state)
            self._mark_confirmable(state, state.flags.get("selected_plan"))
        decision = self._terminal_without_target(
            state, f"{state.intent.value} 是单轮取证（查一轮就收敛）→ 一轮收口")
        state.termination = decision
        self._apply_status(state, decision)
        state.add_note(decision.reason)
        return "report"

    @staticmethod
    def _directed_roles(state: GraphState) -> set[str]:
        """按 top-2 假设的类别 + 缺失信号源，选出**该叫哪几个角色**。"""
        roles = {CATEGORY_AGENT_ROLE.get(item.category, "")
                 for item in state.hypotheses[:2]}
        roles.update(_SOURCE_ROLE.get(signal, "") for signal in state.degraded_signals)
        return {role for role in roles if role}

    # ------------------------------------------------------------------
    #  ⑧ report：结论 + 不确定性（报告必须是一个图节点，不能隐式收尾）
    # ------------------------------------------------------------------
    async def _node_report(self, state: GraphState) -> str:
        # ★★ 处置轮：**方案必须在取证之后定稿**。收敛（`decide` 直接终止）时图不会经过
        #   `directed`，而 clarify 那次 `_prepare_action` 是在取证**之前**跑的 ——
        #   不在这里重算，用户拿到的就是"零证据"的占位方案（真机实测：问 order 线程池、
        #   给 inventory 的 SQL 方案）。
        if (state.intent in REMEDIATION_INTENTS and state.evidence
                and state.flags.get("remediation_evidence_source") != SOURCE_CURRENT):
            # ★★ 先让模型**带着记忆**写方案（草稿），再走既有的候选/锚定/确认门那条路：
            #   记忆（SOP + 既往案例）与这一轮的取证事实必须进**同一次**调用，
            #   方案才可能对上用户点名的那个机制。
            await self._generate_ai_plans(state)
            self._prepare_action(state)
            self._mark_confirmable(state, state.flags.get("selected_plan"))
        # ★★ 处置轮**挂起的那一问**在这里问回去 —— 但**按取证之后的事实重算**
        #   （"先把取证跑完，再问那一句"；重算的理由见 `_ask_deferred_clarify`）。
        if state.flags.pop("clarify_deferred", None):
            if await self._ask_deferred_clarify(state):
                return "report"
        if state.flags.get("clarify_allowed") and not state.report:
            decision: Optional[ClarifyDecision] = state.flags.get("clarify")
            state.report = self._clarify_text(decision) if decision else "需要补充信息。"
        if state.status == "running":
            # ★★ 兜底收口**也必须过同一条判据**：走到这里只说明"图走完了、
            #   没有任何节点定过状态"，**不是**"这次真有根因"。写死 `concluded`
            #   等于绕开 `_guard_concluded`（`concluded` ⟺ 手里有根因）——
            #   没有证据的收口会被报成"已定位根因"。
            decision = TerminationDecision(
                terminate=True, kind="no_progress", status="concluded",
                reason="图已走到 report 节点（没有任何节点给出终止决定）→ 按当前事实收口")
            self._apply_status(state, decision)
            # ★ `_apply_status` 可能把它降级成 `abstained` → 终止决定要跟着如实改，
            #   否则 `status` 与 `termination` 会各说一套。
            if not self._has_root_cause(state):
                decision = dataclasses.replace(decision, status=state.status)
            state.termination = decision
        # ★ `report` 事件由**服务壳**在渲染完整响应之后发出（report 的 data
        #   就是 `/v1/chat` 的最终响应体）—— 编排层这里拿不到渲染后的响应，
        #   所以只负责把状态定下来，不在这里发半成品。
        return "report"

    # ------------------------------------------------------------------
    async def _finish(self, state: GraphState) -> None:
        """收口：写黑板、记会话轮次、校验契约。**每一条都不能省。**"""
        board = board_store(state, self.board, need="raise_questions")
        if board is not None and state.open_questions:
            try:
                await board.raise_questions(state.incident_id, state.open_questions)
            except Exception as exc:  # noqa: BLE001
                state.add_note(f"缺口落黑板失败（{type(exc).__name__}）")
        session = self._session()
        if session is not None and state.session_id:
            try:
                service = state.slots.service
                names = ([service.name] if service is not None
                         else list(state.slots.service_names))
                await session.remember_turn(
                    state.session_id, user_message=state.user_message,
                    response_digest=state.report or state.status, service_names=names)
            except Exception as exc:  # noqa: BLE001
                state.add_note(f"会话轮次记账失败（{type(exc).__name__}）")
        try:
            state.flags["agent_state"] = state.to_agent_state()
        except Exception as exc:  # noqa: BLE001
            state.add_note(f"AgentState 校验失败：{type(exc).__name__}: {exc}")

    async def _checkpoint(self, state: GraphState, *, resume_node: str) -> None:
        """落检查点，并把**续跑应从哪个节点开始**一起存下来。

        ★★ 以前这里写 `payload["stage"] = "pre-report"`，而全仓没有任何读取点 ——
          `stage` 是**死数据**：续跑永远从 `resolve` 重来，于是 `route` 又花一次模型调用、
          `forensics` 又 `spend_round()` / `spend_tool()` 一次：**重复执行 + 重复计账**，
          `max_rounds=1` 时甚至什么都没做就撞上 `budget_exhausted`。
          现在存的是**可消费的节点名**，`run()` 会从它开始（`_build_state` 读进 flags）。
        """
        try:
            payload = state.to_checkpoint()
            payload["resume_node"] = resume_node
            await self.checkpointer.save(state.incident_id, payload)
        except Exception as exc:  # noqa: BLE001 —— 落检查点失败不该带走整次排查
            state.add_note(f"检查点写入失败（{type(exc).__name__}: {exc}）")









