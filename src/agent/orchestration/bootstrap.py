"""从零建状态 / 从检查点续跑（编排层的**入口**部分）。

★ 本模块的方法原样搬自 `orchestrator.py` 的 `Orchestrator`，
  作为 mixin 参与 `Orchestrator` 的组装（**没有第二份实现**）。
"""

from __future__ import annotations

from ..common.contracts.intent import Slots
from .state import GraphState
from typing import Any, Mapping, Optional, Sequence

from ..board.store import InMemoryBoardStore
from .budget import Budget, budget_from_config
import dataclasses
from .constants import EMPTY_SLOTS, new_incident_id


class BootstrapMixin:
    """从零建状态 / 从检查点续跑（编排层的**入口**部分）。"""
    # ------------------------------------------------------------------
    #  状态与依赖
    # ------------------------------------------------------------------
    async def _build_state(self, *, session_id: str, trace_id: str, message: str,
                           env: Optional[str], cluster: Optional[str],
                           incident_id: Optional[str], budget: Optional[Budget],
                           options: Any, usage: Sequence[Any],
                           resume_from: Optional[str],
                           active_tasks: Sequence[Mapping[str, Any]] = (),
                           recent_tasks: Sequence[Mapping[str, Any]] = (),
                           inherited: Optional[Mapping[str, Any]] = None,
                           deployment_form: str = "") -> GraphState:
        """建这次的 `GraphState`：**先定上限**（请求侧与平台侧取较严者）→ 建/续状态。

        ★ 本方法曾有 56 行。现在它只写"上限从哪来 + 走新建还是续跑"，
          两件事各自一个方法 —— 上限口径与恢复口径
          因此也各自只在一处。
        ★ `active_tasks` 是**本次 run 才有的**事实（会话台账原样条目），
          与新建/续跑两条路都有关，故一并交给 `_stamp_run_flags` 落进 flags。
        ★ `recent_tasks`（**刚完成的任务**）同理：它也是"本次 run 才有的输入"，
          与新建/续跑无关。它必须与 `active_tasks` 一起落到 flags —— 否则
          `coordinate._candidates` 读到的近记忆恒为空（那条 `run(recent_tasks=…)`
          的形参就成了一个只收不用的空壳）。
        """
        limits = _limits_with_request_options(budget, options, self.router)
        dry_run_only = bool(getattr(options, "dry_run_only", False))
        board = self.board or InMemoryBoardStore()
        incident = incident_id or new_incident_id()
        from ..observed.context import current
        scope=current()
        if scope:scope.incident_ids.add(incident)
        slots = _slots_from_request(env, cluster)
        if resume_from:
            payload = await self.checkpointer.load(resume_from)
            if payload is not None:
                return await self._restore_state(
                    payload, limits=limits, board=board, incident=incident,
                    session_id=session_id, trace_id=trace_id, message=message,
                    usage=usage, dry_run_only=dry_run_only, active_tasks=active_tasks,
                    recent_tasks=recent_tasks, inherited=inherited,
                    deployment_form=deployment_form)
        return await self._fresh_state(
            incident=incident, session_id=session_id, trace_id=trace_id,
            message=message, slots=slots, limits=limits, board=board,
            usage=usage, dry_run_only=dry_run_only, active_tasks=active_tasks,
            recent_tasks=recent_tasks, inherited=inherited,
            deployment_form=deployment_form)

    async def _restore_state(self, payload: Mapping[str, Any], *, limits: Budget,
                             board: Any, incident: str, session_id: str,
                             trace_id: str, message: str, usage: Sequence[Any],
                             dry_run_only: bool,
                             active_tasks: Sequence[Mapping[str, Any]] = (),
                             recent_tasks: Sequence[Mapping[str, Any]] = (),
                             inherited: Optional[Mapping[str, Any]] = None,
                             deployment_form: str = "") -> GraphState:
        """从检查点恢复（★ 覆盖的是**本次请求的上下文**，进度沿用检查点）。"""
        state = GraphState.from_checkpoint(payload, budget=limits)
        state.incident_id = incident or state.incident_id
        state.session_id = session_id or state.session_id
        state.trace_id = trace_id or state.trace_id
        state.user_message = message or state.user_message
        state.flags['original_user_message']=message or state.user_message
        _stamp_run_flags(state, board=board, usage=usage, dry_run_only=dry_run_only,
                         active_tasks=active_tasks, recent_tasks=recent_tasks,
                         inherited=inherited)
        self._resume_at(state, payload)
        state.board = await board.create(state.incident_id, state.slots)
        return state

    async def _fresh_state(self, *, incident: str, session_id: str, trace_id: str,
                           message: str, slots: Any, limits: Budget, board: Any,
                           usage: Sequence[Any], dry_run_only: bool,
                           active_tasks: Sequence[Mapping[str, Any]] = (),
                           recent_tasks: Sequence[Mapping[str, Any]] = (),
                           inherited: Optional[Mapping[str, Any]] = None,
                           deployment_form: str = "") -> GraphState:
        """新建状态（★ 黑板在 incident 建立时就有，哪怕是空板：`AgentState.board` 必填）。"""
        state = GraphState(incident_id=incident, session_id=session_id,
                           trace_id=trace_id, user_message=message, slots=slots,
                           budget=limits)
        state.flags['original_user_message']=message
        _stamp_run_flags(state, board=board, usage=usage, dry_run_only=dry_run_only,
                         active_tasks=active_tasks, recent_tasks=recent_tasks,
                         inherited=inherited, deployment_form=deployment_form)
        state.board = await board.create(incident, slots)
        return state

    def _resume_at(self, state: GraphState, payload: Mapping[str, Any]) -> None:
        """把检查点里的 `resume_node` 消费成"本次从哪个节点开始"。

        ★ 三种情况都要**如实留痕**，不许静默退回入口（那正是"重跑 + 重复计账"的入口）：
          · 有合法节点名 → 从它开始，并说清"跳过了已完成的节点、不再重复计账"；
          · 没有（旧检查点） → 退回入口，但**明说**"这次会重跑已完成的节点"；
          · 名字不合法 → 同样退回入口并说明（不假装续跑成功）。
        """
        node = payload.get("resume_node")
        if node is None:
            state.add_note("检查点里没有 `resume_node`（旧格式）→ 本次**从入口重跑**，"
                           "已完成的节点会再执行一次、预算会重复计入")
            return
        if node not in self.graph:
            state.add_note(f"检查点里的 `resume_node={node!r}` 不是本图的节点 → "
                           f"从入口重跑（不假装续跑成功）")
            return
        state.flags["resume_node"] = str(node)
        state.add_note(f"从检查点续跑：已完成到 `{node}` —— 直接从该节点继续，"
                       f"前面的节点不再重跑、预算不重复计入")

    def _session(self) -> Any:
        return self.session


def _limits_with_request_options(budget: Optional[Budget], options: Any,
                                 router: Any) -> Budget:
    """本次预算上限 = **平台上限**与**请求侧上限**取较严者。

    ★★ **不再就地改写调用方传进来的 `budget`** ——
      `limits.max_rounds = min(...)` 会静默改掉调用方的对象（同一对象被两次 run 共用时，
      第二次的上限被第一次悄悄改小，且调用方无从察觉）。这里 `replace` 一份再收紧。
    ★ 请求侧上限**真的接上**（以前只回声，现在生效）。
    """
    limits = dataclasses.replace(
        budget or budget_from_config(getattr(router, "config", None)))
    if options is None:
        return limits
    want_rounds = int(getattr(options, "max_rounds", 0) or 0)
    want_tokens = int(getattr(options, "token_budget", 0) or 0)
    if want_rounds > 0:
        limits.max_rounds = min(limits.max_rounds, want_rounds)
    if want_tokens > 0:
        limits.max_tokens = min(limits.max_tokens, want_tokens)
    return limits


def _slots_from_request(env: Optional[str], cluster: Optional[str]) -> Any:
    """空槽位 + 请求里给了的 env/cluster 覆盖（★ 兜底值与契约默认值一致）。"""
    slots = Slots(**EMPTY_SLOTS)
    overrides = {key: value for key, value in (("env", env), ("cluster", cluster))
                 if value}
    return slots.model_copy(update=overrides) if overrides else slots


def _stamp_run_flags(state: GraphState, *, board: Any, usage: Sequence[Any],
                     dry_run_only: bool,
                     active_tasks: Sequence[Mapping[str, Any]] = (),
                     recent_tasks: Sequence[Mapping[str, Any]] = (),
                     inherited: Optional[Mapping[str, Any]] = None,
                     deployment_form: str = "") -> None:
    """把**本次 run 才有的**事实写进 flags（新建与续跑两条路共用，避免两处漂移）。

    ★ `options.dry_run_only` 在这里**读一次**，`_prepare_action` 真拦，
      `_enforcement` 如实回声 —— 一条链上有读点、有拦点、有出口。
    ★ `active_tasks` 也在这里落一次 —— 统筹只读这份（**不另建第二份任务台账**）。
    ★ `recent_tasks` 同样只在这里落一次 —— `coordinate._candidates` 的候选 =
      **活跃 ∪ 近记忆**，两边读的都是本函数写下的这一份。
    ★ `inherited`（**上一轮那次带证据的排查**）也在这里落一次：处置轮自己不做取证，
      没有它就只能给"重启/回滚"这类通用方案（真机上"表不存在"就是这么被漏掉的）。
    """
    state.flags["board_store"] = board
    # 请求内网关持续追加的账本不能复制，否则预算一直看不到新模型用量。
    state.flags["usage"] = usage if isinstance(usage, list) else list(usage)
    state.flags["clarify_allowed"] = False
    state.flags["dry_run_only"] = dry_run_only
    state.flags["active_tasks"] = [dict(item) for item in active_tasks
                                   if isinstance(item, Mapping)]
    #: ★★ **近记忆**（刚完成的任务）—— 与活跃任务分开存，候选排序时排在后面
    state.flags["recent_tasks"] = [dict(item) for item in recent_tasks
                                   if isinstance(item, Mapping)]
    #: ★★ **可继承的上一轮排查**（事故号 + 证据 + 假设 + 抓取时刻）—— 空映射 = 没有可继承的
    state.flags["inherited_investigation"] = (dict(inherited)
                                              if isinstance(inherited, Mapping) else {})
    #: ★★ **部署形态事实**：用户可见文案里的机制名词（Pod/副本…）由它决定，不由"禁词表"
    state.flags["deployment_form"] = str(deployment_form or "")
