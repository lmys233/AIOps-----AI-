"""④ forensics 节点：取证 + 按角色合并。

★ 本模块的方法原样搬自 `orchestrator.py` 的 `Orchestrator`，
  作为 mixin 参与 `Orchestrator` 的组装（**没有第二份实现**）。
★ 两种链路：`IncidentRCA` 走**5 路完整取证**，`METRIC_QUERY` 走**单 Agent 轻链路**
  —— 后者只叫 `metrics_agent`，且构造为 **snapshot 模式**（查现状）。
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from ..agents.base import AgentOutcome
from ..agents.metrics_agent import MetricsAgent
from ..agents.runner import ForensicsRunner, default_agents
from ..attribution.evidence_facts import (GROUNDING_KEY, grounding_of,  # noqa: F401
                                          trace_trees_of, verdict_questions)
from ..attribution.rca import anomaly_onset
from ..board.store import InMemoryBoardStore
from ..common.contracts.evidence import Evidence
from .constants import LIGHT_AGENT_ROLES
from .state import GraphState, board_store, evidence_gain


def _agents_of(orch: Any, state: GraphState, runner: Any = None) -> list[Any]:
    """这一轮真正跑过的取证角色（**优先用刚跑完的那个 runner**）。

    ★★ 为什么不能只看 `orch.runner`：编排层的 `runner` 字段只有"注入进来的"那一个；
      生产装配是**按 executor 现建**一个（`ForensicsRunner(self.executor, board, …)`），
      所以 `orch.runner is None` —— 那时退回 `default_agents()` 会造出**一批新对象**，
      它们身上没有本轮下钻回来的 span 树。症状是"trace 断点证据有 3 条，
      但候选与 trace JSON 一条都没有"（真机实测踩到）。
    ★ 只用来**取回**它们下钻回来的 span 树（`stored_traces()`）—— 不看它们的结论。
    """
    for candidate in (runner, getattr(orch, "runner", None)):
        agents = list(getattr(candidate, "agents", None) or ())
        if agents:
            return agents
    return list(default_agents())


def _memory_digest(state: GraphState) -> list[str]:
    """三层记忆（近 / 中 / 久）的**摘要**（原文靠既有工具按需回取）。

    ★ 只取已经在本轮状态里的事实（会话备忘 / 工具留痕），**不新开一条取数路径**：
      这一层要的是"以前有没有类似事故"的一句话，不是把历史原文搬进提示词。
    """
    out: list[str] = []
    for note in list(getattr(state, "notes", None) or ())[-4:]:
        text = str(note or "").strip()
        if text:
            out.append(text)
    for line in list(getattr(state, "degraded_detail", None) or ())[-2:]:
        text = str(line or "").strip()
        if text:
            out.append(text)
    return out[:4]


def light_agents(intent: Optional[Any]) -> list[Any]:
    """轻链路该叫哪几个取证角色（不适用 → 空列表）。

    ★ 角色的构件从 `default_agents()` 里取（**不新建角色**）：约定的 5 个角色
      与 `AGENT_TO_TOOLS` 的隔离矩阵是契约，轻链路只是"这次少叫几个"。
    ★ `metrics_agent` 在轻链路下构造为 **snapshot 模式**（查现状、不判异常）。
    """
    roles = LIGHT_AGENT_ROLES.get(intent, ()) if intent is not None else ()
    if not roles:
        return []
    out: list[Any] = []
    for agent in default_agents():
        if agent.role not in roles:
            continue
        out.append(MetricsAgent(snapshot=True)
                   if isinstance(agent, MetricsAgent) else agent)
    return out


def _diagnostic_directed(state,agents):
    from ..diagnostics.wiring import enabled
    if not agents or not enabled() or str(state.intent)!='REMEDIATION':return agents
    from ..agents.diagnostic_agents import diagnostic_agents
    roles={a.role for a in agents}
    return list(agents)+[a for a in diagnostic_agents() if a.role not in roles]


class ForensicsMixin:
    """④ forensics 节点：5 路并行取证 + 按角色合并。"""
    # ------------------------------------------------------------------
    #  ④ forensics：5 路并行取证（工具调用/轮次在这里记账）
    # ------------------------------------------------------------------
    async def _node_forensics(self, state: GraphState) -> str:
        """跑一轮取证 → 按角色合并结果 → 记账/发事件。

        ★ 本方法曾有 67 行。现在它只写"选 runner → 跑 → 合并 → 记账"，
          合并与发事件的细节在下面两个方法里。
        """
        runner = self._forensics_runner(state)
        if runner is None:
            # ★ 没接执行器就**不假装查过**：标 `forensics_skipped`，让后面的节点如实收口。
            state.flags["forensics_skipped"] = True
            state.degraded_detail.append(
                "取证执行器未接入 → 本次只给排查方向，**没有查任何数据**")
            state.add_note("取证执行器未接入（T18 的 runner 需要 executor）")
            return "hypotheses"
        run = await self._forensics_run(runner, state)
        gain = _merge_forensics_run(state, run)
        self._ground_root_cause(state, runner)
        state.add_note(f"第 {state.round} 轮取证：{len(state.evidence)} 条证据、"
                       f"{len(state.degraded_signals)} 路缺失、新增增益 {gain:.2f}")
        _emit_forensics_events(self, state, run)
        return "hypotheses"

    def _ground_root_cause(self, state: GraphState, runner: Any = None) -> None:
        """取证一落地就把**候选 / trace JSON / 地板的结论**算出来（纯代码、便宜）。

        ★★ 为什么必须在**取证阶段**做，而且**由代码**做：

          · 候选是"哪些位置可能出错"的**枚举**，它只依赖已经取到的证据 ——
            让模型去枚举，等于每次跑都可能给出不同的一组位置；
          · 地板（最深的 `intrinsic` 节点）是"模型越界/不可用时"的结论底线，
            它必须在**模型之前**就算好，否则"退回地板"无处可退；
          · 记忆摘要（近 / 中 / 久三层）在这里进上下文：长文原文靠既有工具按需回取
            （`memory_read` / `kb_search` / `raw_read`），这一层只看**摘要**。

        ★ 工具隔离**不动**：本方法不调任何工具，只读本轮已经落下来的证据与
          各角色下钻回来的 span 树。
        """
        trees = trace_trees_of(_agents_of(self, state, runner))
        memories = _memory_digest(state)
        grounding = grounding_of(
            evidence=state.evidence, slots=state.slots, trees=trees,
            memories=memories, gaps=list(state.degraded_signals))
        state.flags[GROUNDING_KEY] = grounding
        floor = grounding.get("floor") or {}
        state.add_note(
            f"候选 {len(grounding.get('candidates') or [])} 条（trace 树 "
            f"{len(grounding.get('traces') or [])} 条）；确定性地板："
            f"{floor.get('kind') or '（无候选）'}")
        if floor.get("degraded"):
            state.degraded_detail.append(str(floor["degraded"]))
        questions = verdict_questions(grounding)
        if questions:
            # ★ 反问也是候选之一：无候选 / 候选解释不了时，问题里**带候选与缺口**，
            #   交给既有的澄清预算与去重（`asked_gaps`）裁决要不要真的问。
            state.open_questions = list(dict.fromkeys([*state.open_questions, *questions]))

    async def _forensics_run(self, runner: Any, state: GraphState) -> Any:
        from ..diagnostics.wiring import enabled
        from ..common.tool_budget import tool_budget_scope
        from ..execution.runtime import CURRENT
        state.flags['forensics_metered']=(enabled() or CURRENT.get() is not None) and isinstance(runner,ForensicsRunner)
        if state.flags['forensics_metered']:
            with tool_budget_scope(state.budget):return await self._run_runner(runner,state)
        return await self._run_runner(runner,state)

    async def _run_runner(self, runner: Any, state: GraphState) -> Any:
        """跑取证，并把**工具调用事件**的出口交给 runner（契约修订 20）。

        ★ 事件是**旁路**：注入的 runner（单测里的假 runner / 老实现）如果不认 `on_event`，
          就退回旧调用并**如实留痕**（"这次过程不可见"），而不是让整轮排查崩掉 ——
          但**只吞这一个** TypeError（`on_event` 之外的 TypeError 照旧抛）。
        """
        asked = tuple(state.flags.get("asked_gaps") or ())
        try:
            return await runner.run(
                state.incident_id, state.slots, asked_gaps=asked,
                on_event=lambda event, data: self._emit(state, event, data))
        except TypeError as exc:
            if "on_event" not in str(exc):
                raise
            state.add_note("注入的 runner 不支持 on_event → 本次没有工具调用事件"
                           "（过程不可见：前端只会显示状态行）")
            return await runner.run(state.incident_id, state.slots, asked_gaps=asked)

    def _forensics_runner(self, state: GraphState) -> Any:
        """这一轮该用哪个 runner（定向轮优先按角色过滤；都没接就返回 `None`）。

        ★ `directed_agents` 是**一次性输入**：这里 `pop` 一次，后面的分支都用这个局部值
          （拆函数时踩到过：先 pop 再 `state.flags.get` → 那条"注入的 runner 不支持
          按角色过滤"的降级提示**永远不会响**，而单测当时没断言它）。
        ★ 轻链路意图（`METRIC_QUERY`）在**没有定向角色**时只叫它声明的角色
          （单 Agent 轻链路）；定向轮既然给了角色，就按角色走。
        """
        agents = _diagnostic_directed(state,state.flags.pop("directed_agents", None))
        board = (board_store(state, self.board, need="publish")
                 or InMemoryBoardStore())
        if agents and self.executor is not None:
            # ★ 定向轮：只叫相关角色（靠 executor 现建一个 runner）
            return ForensicsRunner(self.executor, board, agents=agents)
        if self.runner is not None:
            if agents:
                state.add_note("注入的 runner 不支持按角色过滤 → 定向轮跑全部角色"
                               "（行为仍然安全：只是多查几路）")
            return self.runner
        if self.executor is not None:
            light = _diagnostic_directed(state,light_agents(state.intent))
            if light:
                state.add_note(f"单轮定向取证（{state.intent.value if state.intent else '?'}）："
                               f"只叫 {[agent.role for agent in light]} —— 一次取证、按角色收敛")
            return ForensicsRunner(self.executor, board, agents=light or None)
        return None

    @staticmethod
    def _anomaly_onset(state: GraphState) -> Any:
        """异常起点 = 最早的非变更证据 onset（拿它做时域因果的基准）。

        ★ **不在这里再写一份** —— 调 `attribution.anomaly_onset`
          （归因层算先验用的是同一个基准；两份实现漂移一次，时域判据就会打架）。
        """
        return anomaly_onset(state.evidence)

def _merge_forensics_run(state: GraphState, run: Any) -> float:
    if getattr(run,'coverage',None):state.flags['query_coverage']=run.coverage
    if getattr(run,'metric_localization',{}).get('profiles'):state.flags['metric_localization']=run.metric_localization
    """把这一轮的结果并进 state，返回本轮证据增益。

    ★★ **按角色合并，不能整体替换**：
      定向轮只叫**角色子集**，`state.outcomes` 一替换，第 1 轮里其它角色的证据
      就从 `state.evidence`（派生属性）里整体消失了 —— 而它是打分、终止判据、
      `_no_evidence_at_all`、`evidence_count` 的统一输入。
      → 后果是"第 1 轮查得好好的，第 2 轮证据反而变少"，甚至被判成"没有任何证据"。
      本项目红线的反向形态：不是"没查成被当成没命中"，而是"**查到了**被静默丢掉"。
    """
    before = list(state.evidence)
    by_role: dict[str, AgentOutcome] = {item.role: item for item in state.outcomes}
    for outcome in run.outcomes:
        existing = by_role.get(outcome.role)
        by_role[outcome.role] = (existing.merge(outcome) if existing is not None
                                 else outcome)
    state.outcomes = list(by_role.values())
    state.plan = run.plan
    # ★ 缺失/空信号取**并集**：它们是"这一路的状态"，会随轮次累积（跨轮不互相抵消）
    state.degraded_signals = list(dict.fromkeys(
        list(state.degraded_signals) + list(run.degraded_signals)))
    state.degraded_detail.extend(run.degraded_detail)
    state.empty_signals = list(dict.fromkeys(
        list(state.empty_signals) + list(run.empty_signals)))
    state.budget.spend_round()
    state.round = max(1, state.budget.rounds)
    if not state.flags.get('forensics_metered'):
        state.budget.spend_tool(sum(len(outcome.calls) for outcome in run.outcomes))
    # ★★ `evidence_gain(previous, new)` 的第二个参数是**本轮新增**，
    #   而 `state.evidence` 是**累计**属性（第 3 轮 #1 把 outcomes 改成按角色合并之后
    #   它更是只增不减）。以前直接传累计量 → `fresh` 数的是整个列表：
    #     · 第 1 轮查到 5 条 → 5/5 = 1.0
    #     · 第 2 轮**一条新的都没有** → 5/(5+5) = **0.5**（本该是 0）
    #   于是"连续两轮 < 0.10"这条终止条件**永远不成立**（`no_progress` 成了死代码），
    #   本该校停的排查会一直烧到 `max_rounds`，响应里的 `evidence_gains` 也始终在
    #   0.5~1.0 —— 是**误导性**的。
    #   → 传差集：按 `evidence_id` 去掉本轮之前就有的那些。
    gain = evidence_gain(before, _new_evidence(before, state.evidence))
    state.evidence_gains.append(gain)
    return gain


def _new_evidence(previous: Sequence[Evidence],
                  current: Sequence[Evidence]) -> list[Evidence]:
    """本轮**新增**的证据（按 `evidence_id` 去重后的差集）。

    ★ 为什么按 `evidence_id` 而不是按对象/信号名：同一条证据可能在多轮里被重复上报
      （黑板去重是按 id 做的），按 id 才不会把"同一件事被查了两遍"当成新增。
    """
    seen = {item.evidence_id for item in previous}
    return [item for item in current if item.evidence_id not in seen]


def _emit_forensics_events(orch: Any, state: GraphState, run: Any) -> None:
    """事件：每个角色一条 start/done，每条证据一条 evidence。"""
    for outcome in run.outcomes:
        elapsed = round(sum(float(call.get("elapsed_ms") or 0.0)
                            for call in outcome.calls), 1)
        orch._emit(state, "agent_start", {"role": outcome.role, "round": state.round})
        for item in outcome.evidence:
            orch._emit(state, "evidence", {
                "evidence_id": item.evidence_id, "source": item.source.value,
                "signal": item.signal, "summary": item.summary,
                "direction": item.direction.value})
        orch._emit(state, "agent_done", {
            "role": outcome.role, "evidence_count": len(outcome.evidence),
            "empty_count": len(outcome.empty), "degraded": list(outcome.degraded_sources),
            "elapsed_ms": elapsed})
