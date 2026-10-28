"""**接线点**：路由记账 + 窄触发统筹。

★ 为什么单独一个 mixin：`orchestrator.py` 只留控制流骨架。
  这里放三件事，都很小：

  ① `_apply_route`：一次路由结果的**记账**（意图/槽位/预算/事件）——
     统筹会**重跑**路由，两处若各写一遍必然漂移（"重跑那次没计预算"就是这么来的）；
  ② `_maybe_coordinate`：三条窄触发判据 + 一次有界重跑；
  ③ `_memory_block` / `_context_block`：两次**提示词注入** ——
     一次给"会话记忆清单"（哪些数据已经有了、还新不新），
     一次给"上下文分层"（近层原文 / 中层摘要 / 远层索引）。
     两次都遵守同一条纪律：空串 = 不注入，注入必留痕。
"""

from __future__ import annotations

import inspect
import logging
import time
from typing import Any, Optional

from ..common.enums import Intent
from ..common.steps import emit_step
from ..intent.augment import build_tasks, rank_candidates
from ..memory.queries import scope_of
from .context_prompt import (
    context_from_turns,
    describe_context,
    render_context_block,
    summarize_with_gateway,
)
from .coordinator import coordinator_from_router, takes_memory_block
from .memory_prompt import describe_injection, render_memory_block

logger = logging.getLogger("agent.orchestration.coordinate")

#: 统筹重跑之后要在 `degraded` 里说的话（用户得看得出"这次是接哪件事"）
_REROUTE_NOTE = "统筹：这句话是在接上一件事，已按该任务的上下文重跑识别"

#: 一次注入最多读几轮历史（有界：远层走索引，不把全量历史拉进上下文）
CONTEXT_HISTORY_LIMIT = 20


class CoordinateMixin:
    """窄触发统筹的接线（判据见 `coordinator.py` 头三条）。"""

    def _coordinator(self) -> Optional[Any]:
        """本次要用哪个统筹器（显式注入优先；否则从路由器的网关现建一个）。"""
        injected = getattr(self, "coordinator", None)
        if injected is not None:
            return injected
        return coordinator_from_router(self.router)

    def _recall_embedder(self) -> Optional[Any]:
        """R1 的**向量兜底**用的 embedder（默认取既有的词法实现；可注入替换）。"""
        injected = getattr(self, "recall_embedder", None)
        if injected is not None:
            return injected
        try:
            from ..memory.embedding import LexicalHashEmbedder  # noqa: PLC0415

            return LexicalHashEmbedder()
        except Exception:                    # noqa: BLE001 —— 向量路不可用只是少一层兜底
            return None

    def _candidates(self, state: Any) -> list[Any]:
        """★★ **R1 预筛** —— 候选 = 活跃 ∪ 近记忆，先实体、不中才向量，只留 top-k。

        ★ 放进 `state.flags` 复用：判据（要不要统筹）与执行（拿哪些候选去统筹）
          必须看**同一份**候选，否则两处各筛一次就会漂移。
        """
        cached = state.flags.get("coordination_candidates")
        if cached is not None:
            return list(cached)
        config = getattr(self.router, "slot_config", None)
        names = tuple(getattr(config, "service_names", ()) or ())
        active = build_tasks(state.flags.get("active_tasks") or (), config=config)
        recent = build_tasks(state.flags.get("recent_tasks") or (), config=config)
        found = rank_candidates(state.user_message, active, recent=recent,
                                service_names=names, embedder=self._recall_embedder())
        state.flags["coordination_candidates"] = list(found)
        return list(found)

    def _apply_route(self, state: Any, outcome: Any) -> None:
        """把一次路由结果写进状态（**路由只在这里落地** —— 重跑走同一个函数）。"""
        state.route = outcome
        state.intent = outcome.intent
        state.slots = outcome.result.slots
        calls = list(getattr(outcome.trace, "model_calls", ()) or ())
        from ..execution.runtime import CURRENT
        if CURRENT.get() is None:state.budget.spend_llm(calls=len(calls), tokens=self._tokens_so_far(state))
        else:
            from .usage import sync_usage
            sync_usage(state)
        state.budget.notes.append(
            f"路由：{len(calls)} 次模型调用（来源 {outcome.result.source.value}）")
        self._emit(state, "intent", {
            "intent": outcome.result.intent,
            "confidence": outcome.result.confidence,
            "source": outcome.result.source.value,
            "slots": outcome.result.slots.model_dump(mode="json"),
        })
        # ★★ 意图识别也是一次"工具调用"（契约修订 20）——用户要看到
        #   "这一句被理解成了什么、依据是什么"，而不是只在最后看到一个枚举名。
        emit_step(lambda event, data: self._emit(state, event, data), "route", "识别意图",
                  {"message": state.user_message,
                   "env": str(getattr(state.slots, "env", "") or ""),
                   "cluster": str(getattr(state.slots, "cluster", "") or ""),
                   "stages":list(outcome.trace.stages),"contract":(outcome.trace.plugin or {}).get('contract'),
                   "model_proposals":(outcome.trace.plugin or {}).get('model_proposals',[])},
                  result=f"{outcome.result.intent}（信心 {outcome.result.confidence}，"
                         f"来源 {outcome.result.source.value}）；"
                         f"槽位 service={getattr(state.slots.service, 'name', '') or '（未定）'}",
                  elapsed_ms=outcome.result.latency_ms)
    def _should_coordinate(self, state: Any, outcome: Any) -> bool:
        """三条窄触发判据（**全中才统筹**；任一不中就完全不插手）。"""
        coordinator = self._coordinator()
        if coordinator is None or not getattr(coordinator, "enabled", False):
            return False
        if state.flags.get("coordinated"):            # 一次请求只统筹一次（有界）
            return False
        # ★★ 候选 = **活跃 ∪ 近记忆**，经 R1 预筛（先实体、不中才向量、只留 top-k）。
        #   ★ **零候选 → 不统筹** → 越界拒答照旧（拒答正确性靠这条守住）。
        if not self._candidates(state):
            return False
        if not getattr(getattr(outcome, "trace", None), "rule", None):
            return False
        if not outcome.trace.rule.is_fallback:        # 规则层定案了 → 不插手
            return False
        # ★ 只在"本来要拒答/澄清"处插手：正常路由一次都不多花模型调用
        return outcome.intent is Intent.OUT_OF_SCOPE

    async def _maybe_coordinate(self, state: Any, outcome: Any) -> Optional[Any]:
        """跑一次统筹；要接续就**重跑一次**路由并返回新结果（否则 `None`）。"""
        if not self._should_coordinate(state, outcome):
            return None
        state.flags["coordinated"] = True
        config = getattr(self.router, "slot_config", None)
        candidates = self._candidates(state)
        tasks = [item.task for item in candidates]      # 只把 top-k 交给统筹（省 token）
        names = tuple(getattr(config, "service_names", ()) or ())
        # ★ 留痕：选了哪些候选、各自的依据（实体命中 / 向量分 / 活跃还是刚完成）
        state.degraded_detail.append(
            "召回候选（先实体、不中才向量）：" + "；".join(item.describe() for item in candidates))
        memory_block = self._memory_block(state)        # ★★ 会话记忆清单（含抓取时间）
        context_block = await self._context_block(state)  # ★★ 上下文分层（近/中/远）
        coordination = await self._decide(
            state.user_message, tasks, service_names=names, config=config,
            memory_block=memory_block, context_block=context_block)
        if coordination.dropped:
            state.add_note(f"统筹：{coordination.dropped}")
            state.degraded_detail.append(f"统筹未生效：{coordination.dropped}")
        if not coordination.rerouted or coordination.action != "answer_active_task":
            state.add_note(
                f"统筹判定 `{coordination.action}`（{coordination.reply_hint or '未给措辞'}）"
                f"→ 本次不改变识别结果")
            return None
        state.add_note(f"{_REROUTE_NOTE}（{coordination.note}）")
        state.degraded_detail.append(f"{_REROUTE_NOTE} —— {coordination.note}")
        state.flags["coordinated_task"] = coordination.task_id
        return await self.router.route(
            coordination.message, env=state.slots.env, cluster=state.slots.cluster,
            asked_gaps=tuple(state.flags.get("asked_gaps") or ()),
            no_clarify=bool(state.flags.get("no_clarify")))

    async def _decide(self, message: str, tasks: list[Any], *, service_names: Any,
                      config: Any, memory_block: str, context_block: str = "") -> Any:
        """调统筹器的 `decide`（**两段注入各按它认不认那个形参**分别传，替身不认就走原调用）。"""
        coordinator = self._coordinator()
        extra = _accepted(coordinator, memory_block=memory_block,
                          context_block=context_block)
        return await coordinator.decide(message, tasks, service_names=service_names,
                                        config=config, **extra)

    def _memory_block(self, state: Any) -> str:
        """★★ 把**会话记忆清单**渲染给模型（没有记忆 → 空串，**不留噪声**）。

        ★ 为什么注入在这里：这是本层**唯一**一处模型提示词，也是"这句话接下来怎么走
          （包括要不要再查一次）"的判断点。清单带上每段的覆盖窗口与**抓取时间**，
          模型才有依据决定复用还是重取；工具层再以同一口径（60s）机械兜底。
        ★ 注入必须**留痕**：写清交给了几条、最旧一条多少秒 —— 否则"它为什么没重取"
          只能靠猜。
        """
        memory, scope = self._query_memory(), scope_of()
        if memory is None or not scope:
            return ""
        try:
            records = memory.records(scope)
        except Exception as exc:            # noqa: BLE001 —— 记忆不可读只是少一层输入
            state.add_note(f"查询记忆不可读（{type(exc).__name__}）→ 本次不给模型记忆清单")
            return ""
        now = time.time()
        block = render_memory_block(records, now=now)
        trace = describe_injection(records, now=now)
        if block and trace:
            state.add_note(trace)
            state.degraded_detail.append(trace)
        return block

    async def _context_block(self, state: Any) -> str:
        """★★ 把**上下文分层**（近层原文 / 中层摘要 / 远层索引）渲染给模型。

        ★ 与记忆清单同一条纪律：**空串 = 不注入**，注入必留痕。
        ★ 摘要是一次模型请求，所以只在**压力到阈值**时才发（由压缩层裁决）；
          没有可用的摘要回调或调用失败 → 注入**降级块**并留痕，绝不影响主流程。
        """
        session = self._session()
        if session is None or not getattr(state, "session_id", ""):
            state.add_note("未接入会话记忆 → 本次不给模型上下文分层")
            return ""
        turns = await self._read_turns(session, state)
        if not turns:
            return ""
        return await self._render_context(state, turns)

    async def _read_turns(self, session: Any, state: Any) -> list[Any]:
        """读最近若干轮历史（**读不到**不是"没有历史"，要如实说）。"""
        try:
            found = await session.turns(state.session_id, limit=CONTEXT_HISTORY_LIMIT)
            return list(found or ())
        except Exception as exc:            # noqa: BLE001 —— 历史读不到只是少一层输入
            state.add_note(f"会话历史不可读（{type(exc).__name__}）→ 本次不给模型上下文分层")
            return []

    async def _render_context(self, state: Any, turns: list[Any]) -> str:
        """跑一次上下文整理并渲染小节；每一步失败都**降级 + 留痕**（不静默）。"""
        try:
            result, page, plan_note = await context_from_turns(
                turns, budget=state.budget, session_id=state.session_id,
                summarize=_summarizer(self._coordinator()))
        except Exception as exc:            # noqa: BLE001 —— 注入失败不该带走这一轮
            state.add_note(f"上下文分层不可用（{type(exc).__name__}: {exc}）→ 本次不注入")
            return ""
        block = render_context_block(result, page, plan_note=plan_note)
        trace = describe_context(result, page)
        if not block:
            return ""
        state.add_note(trace)
        state.degraded_detail.append(trace)
        if not result.triggered and result.calls == 0:
            state.degraded_detail.append(
                f"上下文未压缩：{result.note}（预算 {state.budget.max_tokens} token）")
        return block


def _accepted(coordinator: Any, **blocks: str) -> dict[str, str]:
    """只把统筹器**认得的**那几段注入放进调用（替身不认的形参一个都不传）。

    ★ 注入是**增强**：送不到不该让统筹失败，也不该改变它的判定口径。
    """
    return {name: text for name, text in blocks.items()
            if str(text or "").strip() and _takes(coordinator, name)}


def _takes(coordinator: Any, name: str) -> bool:
    """这个统筹器的 `decide` 认不认某个注入形参。"""
    if name == "memory_block":
        return takes_memory_block(coordinator)
    try:
        return name in inspect.signature(coordinator.decide).parameters
    except (TypeError, ValueError, AttributeError):
        return False


def _summarizer(coordinator: Any) -> Any:
    """从统筹器取模型网关包成摘要回调；取不到 → `None`（那时不压缩、但要留痕）。"""
    gateway = getattr(coordinator, "_gateway", None)
    if gateway is None:
        return None
    return summarize_with_gateway(gateway, getattr(coordinator, "_model", "") or None)
