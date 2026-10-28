"""`ChatService` 的**核心**：重复判定、黑板留存、会话记账、闸门生命周期。

四个 Mixin 各管一层：渲染（`RenderingMixin`）、恢复（`ResumeMixin`）、
告警接入（`AlertIngestMixin`）、流式（`StreamingMixin`）—— 本类只提供它们共用的外壳。
"""

from __future__ import annotations
from ..observed.context import session_key

from typing import Any, Callable, Final, Mapping, Optional
from ..common.enums import CHAT_MESSAGE_MAX
from ..common.config import resolve_deployment_form
from ..intent.router import IntentRouter
from ..intent.augment import weave_for
from ..llm.gateway import LLMCallRecord
from ..sessions import note_turn, open_session_store
from .answer import AnswerWriter
from .answers import AnswerStepMixin
from .tasks import (RECENT_TTL_SEC, load_parked, load_recent, prepare_run_message,
                    service_names_of, sync_parked_task)
from ..attribution.evidence_facts import GROUNDING_KEY  # noqa: F401
from ..llm.gateway import LLMError  # noqa: F401
from .grounding import conclusions_of, error_summary_of_result, select_root_causes
from ..orchestration import Orchestrator
from ..orchestration.budget import Budget
from ..orchestration.context_prompt import (context_from_turns, describe_context,
                                            summarize_with_gateway)
import asyncio
import time
from .alerts import ALERT_MAX_CONCURRENCY
from .approvals import ApprovalRegistry
from .constants import SESSION_CACHE_MAX, _USAGE, logger
from .idempotency import DuplicateGuard, content_fingerprint, ensure_turn_id
from .pending import PendingRegistry, register_pending_from_body
from .pending_flow import PendingFlowMixin
from .ingest import AlertIngestMixin
from .rendering import RenderingMixin
from .requests import ChatRequest
from .resume import ResumeMixin
from .streaming import StreamingMixin
from .util import _action_fingerprint, _now_ms
from .wiring import default_session_memory

#: 落上下文分层时最多回看多少轮（丢给切分的那一份历史；更老的由远层索引负责）
CONTEXT_TURNS: Final[int] = 40
#: 找"可继承的上一轮排查"时最多回看几条轮次台账（最近的几条就够，不必翻整本）
TURN_META_SCAN: Final[int] = 5


def _conclusions_of(result: Any) -> list[dict[str, Any]]:
    """本轮结论条（候选内排序的产物）。"""
    return conclusions_of(result)


def _error_summary_of(result: Any) -> dict[str, Any]:
    """跨服务错误汇总（含候选与指纹），好把裁决阶段的候选一起带上。"""
    return error_summary_of_result(result)


from .history_flow import HistoryMixin


class ChatService(HistoryMixin, PendingFlowMixin, RenderingMixin, ResumeMixin,
                   AlertIngestMixin, StreamingMixin, AnswerStepMixin):
    """`ChatService` 的**核心**：幂等、黑板留存、会话记账、闸门生命周期。"""

    def __init__(self, *, router: IntentRouter, cache_max: int = SESSION_CACHE_MAX,
                 orchestrator: Optional[Any] = None,
                 auth_token: Optional[str] = None,
                 gate: Optional[Any] = None, state: Optional[Any] = None,
                 sessions: Optional[Any] = None,
                 deployment_form: Optional[str] = None, history: Optional[Any] = None) -> None:
        self._router = router
        from ..adapters.task_ports import ServiceTaskPorts
        self.task_ports = ServiceTaskPorts(self)
        #: ★ 本地会话表（**不依赖 Redis/PG**）：会话列表/上次会话靠它，缺省时按环境装配。
        #:   只算路径不建库 —— 没用到会话表的路径不会因此碰磁盘。
        self.sessions: Any = sessions if sessions is not None else open_session_store()
        #: ★★ 共享密钥（`AIOPS_AGENT_TOKEN`）：`None`/空 → **不鉴权**（本机开发）；
        #:   非回环绑定又没配它时 `main()` **拒绝启动**（fail-closed）——
        #:   `/v1/alerts` 是设计上要从网络调用的端点，"没人知道这个端口"不是安全措施。
        self._auth_token = (str(auth_token).strip() if auth_token else "") or None
        #: ★ 结果与**重复判定登记**共用一个有界存储（同一个 `ResultStore`）：
        #:   判定登记按 `(session_id, 轮次 id / 内容指纹)` 索引，容量由它兜（共享实现靠 TTL）。
        #:   ★ 这里**不再**有"答案回放"：同一句话再问一次是重复请求（直接拒绝，旧结论会误导人）
        self._cache_max = cache_max
        #: ★ 编排层：判意图之后的**全部**流程都在它手里。
        #:   不注入时按"最小可用"装配：进程内记忆 + 无取证执行器 ——
        #:   于是行为仍然可解释（"没接取证 → 不给根因结论"），而不是静默给个空结论。
        self._orchestrator = orchestrator or Orchestrator(
            router=router, session=default_session_memory())
        self._bind_history(history)
        #: ★ **部署形态事实**（`host_process` / `k8s` / `unknown`）：用户可见文案里的机制名词
        #:   （Pod / 副本…）由它决定，**不由禁词表**；没配就是 `unknown` → 只说"服务实例"
        #:   并**明说**"部署形态未配置"（见 `plans.py`）。★ 公开属性：**导出**那条路也按同一份
        #:   事实出文案，否则"页面说实例、导出件说 Pod"就是同一个事实两份说法。
        self.deployment_form = (str(deployment_form).strip() if deployment_form is not None
                                else resolve_deployment_form())
        #: ★★ **答案总结**（用户："答案应该是通俗易懂的，至少需要让大模型总结一次"）。
        #:   网关从 router 取（没配模型 → `available=False`，直接走**同样是白话**的模板）。
        #:   这次调用走同一个网关 → 按请求的 usage sink 自动计入 `cost`。
        self._answers = AnswerWriter(gateway=getattr(router, "gateway", None),
                                     model=getattr(router, "main_model", ""))
        self.pendings = PendingRegistry()   # ★ T59a：待确认状态（对话行为 → 目标 的结构化依据）
        #: ★★ 四件共享状态（闸门/账本/结果/待确认动作）统一装配：默认是进程内实现
        #:   （单进程语义）；装配 Redis 后是**多实例 + 重启不丢**。降级时 `state.note`
        #:   必须进响应/日志（"记不住"这件事不许静默）。
        self._bind_state(gate=gate, state=state, cache_max=cache_max)
        #: 待批动作：审批令牌（`apr_*`）仍是进程内 + 数据库约束那一套；
        #: ★ 残留（如实记录）：审批令牌的**跨重启**恢复未做（`approval_requests` 表在，
        #:   但读取路径还没接）。
        self.approvals = ApprovalRegistry()
        #: 后台排查任务（告警触发的），仅用于优雅收尾时取消
        self._background: set["asyncio.Task[Any]"] = set()
        self._owned_tasks: set[asyncio.Task] = set()
        self._request_tasks: set[asyncio.Task] = set()
        #: ★★ 同时在跑的**告警触发**排查上限（有界信号量）。
        #:   `ALERT_MAX_CONCURRENCY` 与 `ALERT_MAX_BATCH` 是两个不同的闸门：
        #:   前者限"同时烧钱的速度"，后者限"单次请求的规模"。
        self._alert_semaphore: "asyncio.Semaphore" = asyncio.Semaphore(ALERT_MAX_CONCURRENCY)

    def spawn(self, coroutine):
        task=asyncio.create_task(coroutine)
        self._owned_tasks.add(task)
        task.add_done_callback(self._owned_tasks.discard)
        return task

    async def close_workers(self):
        from ..common.lifecycle import stop_tasks
        # Cancellation finalizers can enqueue gate release; drain them before clients.
        await stop_tasks(self._background | self._owned_tasks | self._request_tasks)
        if self._owned_tasks:await asyncio.gather(*self._owned_tasks,return_exceptions=True)

    @property
    def router(self) -> IntentRouter:
        return self._router

    @property
    def orchestrator(self) -> Any:
        """编排层（排障/探针用；也让"编排是唯一控制流"可被断言）。"""
        return self._orchestrator

    @property
    def auth_token(self) -> Optional[str]:
        """本实例要求的共享密钥（`None` = 未配置）。"""
        return self._auth_token

    def use_auth_token(self, token: Optional[str]) -> None:
        """显式设定共享密钥（★ `create_app(service=…, auth_token=…)` 必须生效）。

        以前这个参数在"注入 service"的分支里被**静默忽略**（测试与运维以为配了密钥）。
        """
        self._auth_token = (str(token).strip() if token else "") or None

    # ------------------------------------------------------------------
    async def handle(self, request: ChatRequest, *, trace_id: str,
                     incident_id: Optional[str] = None) -> dict[str, Any]:
        """处理一次对话请求：**重复直接拒绝**，其余跑一次。

        ★ 判据两条（都在 `DuplicateGuard` 里）：`turn_id` 相同**一律**判重复；
          仅内容一致时**只在抖动窗口内**判重复 —— 窗口外的一致输入正常进入
          （"用户就是要再问一遍"是用户自己的事）。
        ★ 同一会话正有请求在跑 → 先等它（并发去重：同一时刻只允许一个真跑），
          等到的若是**同一个 key** → 走上面的拒绝路径（**不回放**它的结果）。
        """
        turn_id = ensure_turn_id(request)
        await self._reject_duplicate(request, turn_id)
        await self._wait_for_leader(request, turn_id=turn_id)
        return await self._run_as_leader(request, trace_id=trace_id,
                                         incident_id=incident_id, turn_id=turn_id)

    async def reject_duplicate(self, request: ChatRequest) -> str:
        """对话入口的**重复判定**（路由在**起流之前**先用它，返回这一轮的 `turn_id`）。

        ★ 为什么要有这条公开入口：`/v1/chat/stream` 的响应头在生成器开跑之前就发出去了 ——
          只在生成器里判，重复请求只能拿到 200 + 流里的 error；先判一次才能给出**真正的 409**。
        """
        turn_id = ensure_turn_id(request)
        await self._reject_duplicate(request, turn_id)
        return turn_id

    async def _reject_duplicate(self, request: ChatRequest, turn_id: str) -> None:
        """重复就抛 `DuplicateRequest`（409）：**直接拒绝**，不返还上一次的结果。"""
        await self._duplicates.reject(request.session_id, turn_id,
                                      content_fingerprint(request))

    async def _claim_turn(self, request: ChatRequest, turn_id: str) -> None:
        """占住这一轮（跑之前落共享存储）—— 判据要**多实例一致、重启不丢**。"""
        await self._duplicates.claim(request.session_id, turn_id,
                                     content_fingerprint(request))

    async def _wait_for_leader(self, request: ChatRequest, *, turn_id: str) -> None:
        """同一会话正有请求在跑 → 等它落定，然后再判一次重复（同 key → 409）。

        ★ 为什么必须等：既避免"同一请求并发执行两次、各计一次费"，
          也保证"第 2 轮能看见第 1 轮的工作记忆"（多轮语义）。
        ★★ `CancelledError` 在 3.8+ 是 `BaseException` 的子类 —— 取消必须继续向上抛
          （`except BaseException: pass` 会把"客户端断开 → 我这次本该被取消"
          变成"我继续等 / 继续跑完并计费"）。
        ★ 等待对象是 `GateHandle`（两套实现同一接口）：分布式实现是"轮询锁是否已释放"，
          结果仍由调用方随后按判定登记与共享存储拿。
        """
        if getattr(self._gate, "shared", False):
            running = await self._gate.peek(request.session_id)
        else:
            running = self._inflight.get(request.session_id)
        if running is None:
            return
        try:
            await running.wait()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 领跑失败不影响我这一次
            pass
        await self._reject_duplicate(request, turn_id)

    def _bind_state(self, *, gate: Optional[Any], state: Optional[Any],
                    cache_max: int) -> None:
        """装配四件共享状态。

        ★ 默认 = 全进程内实现（行为与旧版逐字一致，单进程语义）；
          传入 `state`（`state.build_state_bundle(...)` 的产物）即切到共享实现。
        ★ `self._inflight` 是**只为内存实现保留的镜像**（既有用例用它观测
          "闸门在不在表里"）：分布式实现下它是空表，误读它会得出错误结论。
        """
        bundle = state
        if bundle is None:
            from ..state import build_state_bundle  # noqa: PLC0415

            bundle = build_state_bundle(cache_max=cache_max)
        self._state = bundle
        self._shared_state = bool(getattr(bundle, "shared", False))
        self._state_note = str(getattr(bundle, "note", "") or "")
        self._gate: Any = gate if gate is not None else bundle.gate
        self._inflight: dict[str, Any] = getattr(self._gate, "table", {})
        self._ledger: Any = bundle.ledger
        self._results: Any = bundle.results
        self._pending: Any = bundle.pending
        #: ★★ 重复判定：状态落 `ResultStore`（共享实现下多实例一致、重启不丢），
        #:   键里带 `session_id`。进程内实现下再叠一层**轮次 id 镜像**（见 `idempotency`），
        #:   否则同一进程里的多个服务壳会各记一份。
        self._duplicates = DuplicateGuard(self._results, shared=self._shared_state)
        #: ★ 兼容旧读法：`alerts` 就是账本（早期它是 `AlertQueue`，现在由账本持有）。
        self.alerts = self._ledger

    @property
    def state_note(self) -> str:
        """共享状态不可用时的**必读说明**（空串 = 一切正常）。

        ★ 为什么要暴露它：降级必须"响" —— 装配层探测失败会退回
          进程内实现，响应里必须看得到"本次是单进程语义"，而不是让运维以为多实例去重还在。
        """
        return self._state_note

    def _open_inflight(self, session_id: str) -> Any:
        """占住会话闸门（同步接口，**仅内存实现可用**）：实现搬到
        `state.InProcessInflightGate.open_nowait`，这里只做委托。
        """
        return self._gate.open_nowait(session_id)

    async def _acquire_gate(self, session_id: str, trace_id: str) -> Any:
        """抢先占闸门；**别人已占**返回 `None`（调用方应回去等它落定）。

        进程内实现永远拿到（单进程里没有竞争）；分布式实现靠 `SET NX PX` 竞争
        —— 返回 None 就说明这一瞬间别人成了领跑者，**绝不能**当成"没人跑"继续。
        """
        if not getattr(self._gate, "shared", False):
            return self._open_inflight(session_id)
        return await self._gate.acquire(session_id, trace_id)

    async def _keep_gate_alive(self, gate: Any) -> Optional["asyncio.Task[None]"]:
        """闸门**续期**（分布式实现才需要）：排查可能比 TTL 长，过期会让第二个实例插进来。

        ★ 只在共享实现下起后台任务（内存实现是无副作用的 no-op）——
          这样默认装配的行为与旧版逐字一致，而换成 Redis 后不会"跑一半锁没了"。
        """
        if not getattr(self._gate, "shared", False):
            return None
        interval = max(1.0, float(getattr(self._gate, "ttl_sec", 900.0)) / 3.0)

        async def _renew_loop() -> None:
            while not gate.done():
                await asyncio.sleep(interval)
                if gate.done():
                    return
                try:
                    await self._gate.renew(gate)
                except Exception as exc:  # noqa: BLE001 —— 续期失败要**说出来**，不能静默
                    logger.warning("闸门续期失败（%s）：%s", type(exc).__name__, exc)
                    return

        return self.spawn(_renew_loop())

    def _release_gate_nowait(self, session_id: str, gate: Any) -> None:
        """同步释放（给"取消路径上的同步收尾"用）。

        ★ 分布式实现下只能**派一个任务**去删：流式的收尾跑在生成器的 `finally` 里，
          取消路径上不能 await（否则清理没跑完就把请求结束了）。
          任务没跑成也不至于死锁：闸门自带 TTL 会自愈。
        """
        if not getattr(self._gate, "shared", False):
            self._gate.pop_nowait(session_id, gate)
            return
        try:
            self.spawn(self._gate.pop(session_id, gate))
        except RuntimeError:              # 没有运行中的 loop（进程收尾）
            logger.warning("释放分布式闸门时没有事件循环 → 交给 TTL 自愈（session=%s）",
                           session_id)

    def _pop_inflight(self, session_id: str,
                      gate: Any) -> None:
        """释放闸门：**只释放自己建立的那一个**。

        判据是对象身份（`is`）而不是键是否存在 —— 同会话的下一轮可能已经把新闸门
        写进同一个键，按键删除会误删它，于是第三轮与**还在跑的那一轮**并行真跑
        （两次模型调用、两次记账）。`session_id` 由客户端给，"拿到 `report` 就发下
        一轮"又是最自然的客户端行为 → 这条误删是**客户端可触发**的重复计费。
        分布式实现下由 `CAS` 删除承担同一判据（见 `state.gate`）。
        """
        self._release_gate_nowait(session_id, gate)

    @staticmethod
    def _settle_inflight(gate: Any,
                         body: Optional[Mapping[str, Any]],
                         exc: Optional[BaseException] = None) -> None:
        """把结局交给等待者（成功给结果、失败给异常），并保证异常已被取走。

        ★★ **领跑者被取消**（客户端断线）时不能把 `CancelledError` 原样塞进
          gate —— 等待方在 `await asyncio.shield(running)` 处会收到它，于是
          `except CancelledError: raise` 会把**等待方自己的请求**也取消掉。而等待方与
          领跑者只是恰好用了同一个 `session_id`（多轮对话的正常用法），甚至可能是
          另一个客户端 —— 一条断线会连带打断同会话上所有在跑的请求。
          → 取消**翻译成普通失败**再交出去；领跑者自己照常被取消。
        ★ 逻辑搬进 `GateHandle.settle`（两套实现共用同一份判据），这里只做委托。
        """
        gate.settle(body, exc)

    async def _run_as_leader(self, request, *, trace_id, incident_id, turn_id=""):
        owner=asyncio.current_task();self._request_tasks.add(owner)
        try:
            return await self._run_leader(request,trace_id=trace_id,incident_id=incident_id,turn_id=turn_id)
        finally:self._request_tasks.discard(owner)

    async def _run_leader(self, request: ChatRequest, *, trace_id: str,
                            incident_id: Optional[str],
                            turn_id: str = "") -> dict[str, Any]:
        """自己成为领跑者：占住闸门、**先登记这一轮**、跑一次、落定结果。"""
        gate = await self._acquire_gate(request.session_id, trace_id)
        if gate is None:
            # ★ 分布式实现下的"抢输了"：别人刚成为领跑者 → 回去等它落定（不能并行跑）
            await self._wait_for_leader(request, turn_id=turn_id)
            gate = await self._acquire_gate(request.session_id, trace_id)
            if gate is None:                      # 连续两次抢输：如实失败，不硬跑
                raise RuntimeError("同会话已有排查在跑，本次未能取得闸门（请重试）")
        renew_task = await self._keep_gate_alive(gate)
        try:
            # ★ 登记**在跑之前**：后来者（含并发的、重试的）据此判重复，
            #   于是"同一轮绝不跑两次"不依赖"上一次成功过"。
            await self._claim_turn(request, turn_id)
            body = await self._run_recorded(request, trace_id=trace_id,
                                            incident_id=incident_id)
            self._settle_inflight(gate, body)
            return body
        except BaseException as exc:
            self._settle_inflight(gate, None, exc)
            raise
        finally:
            if renew_task is not None:
                renew_task.cancel()
            self._pop_inflight(request.session_id, gate)

    # ------------------------------------------------------------------
    async def _handle_new(self, request: ChatRequest, *, trace_id: str,
                          usage: list[LLMCallRecord],
                          incident_id: Optional[str] = None,
                          on_event: Callable[[str, dict[str, Any]], None] | None = None
                          ) -> dict[str, Any]:
        """跑一次编排，然后**渲染**成契约约定的响应体。

        ★ 渲染单独抽成 `_render`：SSE（`/v1/chat/stream`）也要发同一份 `report`
          事件 —— 两处各写一遍必然漂移（"chat 与 incidents 结果一致"也会跟着破）。
        """
        degraded: list[str] = []
        from ..adapters.task_runtime import enabled,controller,scope_for
        if enabled(self):return await controller(self).handle(self,request,scope_for(request),trace_id,usage,on_event,incident_id)
        # ★★ 共享状态降级（探测失败退回进程内）必须**在响应里可见** ——
        #   否则运维会以为"同会话去重/告警抑制"还在生效，而其实只活在这个进程里。
        if self._state_note:
            degraded.append(self._state_note)
        from ..dialogue.flow import handle as handle_dialogue
        dialogue = await handle_dialogue(self,request,trace_id,usage,on_event,incident_id)
        if dialogue is not None:
            return dialogue
        # ★★ 入口准备 = 输入护栏 + 会话台账判定 + 上下文编织（新事不许被旧任务挡住）
        (session, message, resumed_from, active_tasks,
         recent_tasks) = await self._prepare_input(request, degraded)
        # ★★ 可继承的上一轮排查（处置轮不取证 —— 靠它才给得出有依据的方案）
        inherited = await self._inherited_investigation(request, degraded)
        # ★★ **确认类信号**：先按**结构化待确认状态**分派，没有结构化依据再退回既有确认链路
        confirmed = await self.maybe_pending_or_confirmation(
            request, session, trace_id=trace_id, on_event=on_event)
        if confirmed is not None:
            confirmed["turn_id"] = ensure_turn_id(request)
            return confirmed
        # ★ 事件号可以由调用方**预分配**（告警接入就是这么做的：告警契约要求同一
        #   fingerprint 一个 incident，不能等跑完才编号，否则 /v1/alerts 的 id 查不到）
        result = await self._orchestrator.run(
            session_id=request.session_id, trace_id=trace_id, message=message,
            env=request.context.env, cluster=request.context.cluster,
            usage=usage, options=request.options, incident_id=incident_id,
            active_tasks=active_tasks, recent_tasks=recent_tasks,
            inherited=inherited, on_event=on_event,
            deployment_form=self.deployment_form)
        return await self._finish_new_result(result,request,trace_id,usage,degraded,
            session,message,resumed_from,on_event)

    async def _finish_new_result(self,result,request,trace_id,usage,degraded,
                                 session,message,resumed_from,on_event):
        """Render and save the original pipeline result through one finish path."""
        # ★★ 渲染之前先让模型**总结一次答案**（用户明确要求）：结果塞进 `facts["answer"]`，
        #   渲染层据此写 `report`；★ 它只换措辞、不换结论（不改任何判据）。
        # ★★ **候选内排序**：这一次模型调用只在代码枚举的候选里挑 1~3 条并给措辞；
        #   越界（提到候选/链路里没有的服务或节点）→ 弃用它的选择、退回代码的
        #   确定性地板，并把"越界"写进 `degraded`。模型是增强，地板是底。
        await self._select_root_causes(result, degraded)
        facts = dict(result.facts)
        facts["error_summary"] = _error_summary_of(result)
        facts["root_cause_conclusions"] = _conclusions_of(result)
        facts["answer"] = await self._answer_step(result, message, trace_id, on_event)
        body = self._render(result.state, facts, request, trace_id=trace_id,
                            usage=usage, degraded=degraded)
        if body.get('explanation',{}).get('attribution_v2'):
            from ..dialogue.view import compatible
            body=compatible(body)
        await self._attach_turn_answer(request, body, result)   # ★ 答案与工具调用补进近层记忆
        if not result.state.flags.get('task_control'):
            await self._settle_turn(request, body, result, degraded, session, message, resumed_from)
        return body

    async def _settle_turn(self, request: ChatRequest, body: dict[str, Any], result: Any,
                           degraded: list[str], session: Any, message: str,
                           resumed_from: str) -> None:
        """落定这一轮：**越界接回待确认** → 台账 → 待确认登记 → 故障处置建议 → 事件留存。

        ★ 顺序有意：越界覆盖要**先**做，它把这一轮的响应改成"接着确认"，
          台账与事件留存的才是**用户真正看到的那一份**。
        """
        if result.state.flags.get('task_control'):return
        self._drain_new_topic_note(body)
        kept = self.apply_pending_override(request, body, result,
                                       resumed_from=resumed_from)
        await sync_parked_task(session, session_id=request.session_id, message=message,
                               body=body, resumed_from=resumed_from,
                               tools=self._tool_calls_of(result))
        await self._note_pending_action(request, result.facts, body)
        if not kept:
            # ★ 覆盖之后**不再登记新的待确认**：否则会把正等着的那条挤成"挂起"
            register_pending_from_body(self.pendings, session_id=request.session_id,
                                       body=body, facts=result.facts)
        self._attach_failure_advice(body, degraded)
        await self._store_incident(body)

    async def _prepare_input(self, request: ChatRequest, degraded: list[str]
                             ) -> tuple[Any, str, str, list[dict[str, Any]],
                                        list[dict[str, Any]]]:
        """入口准备：输入护栏 + **会话台账判定** + **上下文编织**。

        ★ 超长输入先截断（本阶段以截断代替摘要）——**必须说出来**：
          "输入太长"也要如实在 `degraded` 里可见，而不是悄悄改小用户的输入。

        ★★ 返回值多了一份**活跃任务清单**。它同时是
          ① 编织的输入（"这句是不是在说那件事"）② 编排层**窄触发统筹**的输入。
          两处必须看**同一份**任务 —— 否则编织认一件事、统筹认另一件（自相矛盾）。
        """
        message = request.message
        if len(message) > CHAT_MESSAGE_MAX:
            message = message[:CHAT_MESSAGE_MAX]
            degraded.append(
                f"输入超过 {CHAT_MESSAGE_MAX} 字符，已截断后再判定"
                f"（本阶段以截断代替摘要）")
        session = getattr(self._orchestrator, "session", None)
        names = service_names_of(self._router)
        parked = await load_parked(session, request.session_id)
        message, resumed_from = await prepare_run_message(
            session, session_id=request.session_id, message=message,
            service_names=names, degraded=degraded)
        recent_tasks = await load_recent(session, request.session_id, degraded=degraded)
        if not resumed_from:
            # ★ 已经按"补信息"重写过消息时不再编织（同一件事只做一次）
            # ★★ 把**近记忆**也交给编织 —— 强证据（实体/接续词）走完整织法，
            #   弱证据（刚完成 + 像追问的短句）**只补目标服务**（不注入意图词）。
            message, weave_notes = weave_for(
                message, parked, names=names,
                config=getattr(self._router, "slot_config", None),
                recent=recent_tasks)
            degraded.extend(weave_notes)
        return session, message, resumed_from, parked, recent_tasks

    # ------------------------------------------------------------------
    #  ★★ 候选内排序：模型增强，代码地板（判定在 `attribution/decision.py`，
    #     接线在同目录的 `grounding.py` —— 本类只留一个入口）
    # ------------------------------------------------------------------
    async def _select_root_causes(self, result: Any, degraded: list[str]) -> None:
        """在代码枚举的候选里让模型挑 1~3 条；越界/失败一律退回地板。"""
        await select_root_causes(self, result, degraded)

    def _attach_failure_advice(self, body: dict[str, Any],
                               degraded: list[str]) -> None:
        """下游不可用时的**自愈**：拒答也要给出"事实 + 手册 + 确认命令"。

        ★ 只在"本次没有别的澄清"且状态是 `abstained`/`failed`（本来就给不出结论）时附加 ——
          有结论时不该用运维手册去打断（那是两件事）。
        ★ 手册原文**不经模型改写**（`clarify.manual` 直出文件内容）。
        """
        if body.get("clarification") or body.get("status") not in ("abstained", "failed"):
            return
        hub = getattr(self._router, "clarify", None)
        if hub is None:
            return
        #   ★ 两处都要看：`degraded`（本次运行的降级留痕）与 `body["degraded"]`
        #     （已经被渲染进响应的那份）—— 工具失败可能只在其中一处，只看一处会漏。
        question = hub.advice_from_degraded(
            list(degraded) + [str(item) for item in (body.get("degraded") or [])])
        if question is None:
            return
        body["clarification"] = {
            "questions": [question.to_dict()],
            "reason": "下游不可用（没查成）→ 给出可执行确认步骤与运维手册原文，"
                      "而不是只回一句『数据源不可用』",
            "plugin_id": question.plugin_id,
        }
        body.setdefault("degraded", []).append(
            f"已附运维手册建议（plugin_id={question.plugin_id}）："
            f"这属于「没查成」，不是「查了没异常」")

    async def _note_pending_action(self, request: ChatRequest, facts: Mapping[str, Any],
                                   body: Mapping[str, Any]) -> None:
        """把"确认门 + 预案"记进会话 —— 用户确认时（`/v1/chat/resume`）要接着这一次的
        预案走审批/拒绝，而不是重新猜他想做什么。

        ★★ 这一步以前只写在 `/v1/chat` 的路径里，**流式路径漏了** ——
          于是"流式提出的处置澄清无法被后续确认"。现在两条路径都过这里。
        ★ 登记动作改走 `PendingActionStore`（共享实现下**重启/换实例**仍能确认）。
        """
        action, risk = facts.get("proposed_action"), facts.get("risk")
        if not (action and risk and body.get("status") == "clarify"):
            return
        await self._pending.put(session_key(request.session_id), {
            "action": action, "risk": risk, "incident_id": body["incident_id"],
            # 动作指纹＝审批绑定的那个 hash（与执行端**同一个实现**）
            "action_hash": _action_fingerprint(action),
            "allow_write": bool(getattr(request.options, "allow_write", False))})
        # ★ 有界性由存储实现负责（进程内实现按 `cache_max` 淘汰；Redis 实现靠 TTL）

    async def _run_recorded(self, request: ChatRequest, *, trace_id: str,
                            incident_id: Optional[str] = None,
                            on_event: Callable[[str, dict[str, Any]], None] | None = None
                            ) -> dict[str, Any]:
        from ..observed.chat import run
        return await run(self,request,trace_id,incident_id,on_event)

    async def _run_recorded_unscoped(self, request: ChatRequest, *, trace_id: str,
                            incident_id: Optional[str] = None,
                            on_event: Callable[[str, dict[str, Any]], None] | None = None
                            ) -> dict[str, Any]:
        """**两条路径唯一的"跑一次 + 记账"实现**。

        同步 `/v1/chat` 与流式 `/v1/chat/stream` 都走它，于是：
          · `_USAGE` 记账桶一定装上（否则 `cost` 报 0 tokens —— 那是**假的**）；
          · `wall_ms` 一定填上，`turn_id` 由渲染层统一回显；
          · incident 留存（`_store_incident`）与轮次台账（`_note_turn`）都在这里。
        ★ 流式以前另写了一套 `produce()`，上面几件事**全漏**（现在只有这一条路）。
        """
        bucket: list[LLMCallRecord] = []
        token = _USAGE.set(bucket)
        from ..llm.accounting import REQUEST_RECORDS
        request_usage_token=REQUEST_RECORDS.set(bucket)
        started = _now_ms()
        from ..history.capture import capture
        details={}
        def history_event(event,data):
            capture(details,event,data)
            if on_event:on_event(event,data)
        try:
            body = await self._handle_new(request, trace_id=trace_id, usage=bucket,
                                          incident_id=incident_id, on_event=history_event if self.history else on_event)
            from ..observed.context import stamp
            stamp(body)
            from ..alerts.context import stamp as stamp_alert
            stamp_alert(body)
            body["cost"]["wall_ms"] = round(_now_ms() - started, 1)
            if not (body.get('explanation',{}).get('dialogue',{}).get('action')):
                await self._note_context(request, body)
            from ..dialogue.limits import stamp
            from ..dialogue.lineage import annotate
            if not body.get('explanation',{}).get('task_control'):await annotate(self,request,body)
            body['cost']['input_tokens']=sum(r.input_tokens for r in bucket)
            body['cost']['output_tokens']=sum(r.output_tokens for r in bucket)
            stamp(body,bucket)
            from ..presentation.projection import finish
            finish(body)
            if not await self._save_history(request,body,details):return body
            if not body.get('explanation',{}).get('task_control'):
                await self._attach_turn_answer(request,body)
                await self._store_incident(body)
            await self._note_turn(request, body)
            return body
        finally:
            REQUEST_RECORDS.reset(request_usage_token)
            _USAGE.reset(token)

    # ------------------------------------------------------------------
    #  结果查询（流式见 `streaming.py`）
    # ------------------------------------------------------------------
    async def get_incident(self, incident_id: str) -> Optional[dict[str, Any]]:
        """`GET /v1/incidents/{id}` 的数据源（**与 `/v1/chat` 同一份 body**）。

        ★ 同一个 body 直接返回：chat 与 incidents 结果一致就是这么保证的 ——
          另做一份"查询用的投影"迟早会与 chat 的响应漂移。
        ★ 改走 `ResultStore`（共享实现下**跨重启/跨实例**都查得到）。
        """
        body=await self._results.get_incident(str(incident_id))
        control=((body or {}).get('explanation') or {}).get('task_control') or {}
        if control and control.get('commit_state')!='committed' and self.history:
            saved=await self.history.store.incident(str(incident_id))
            if saved is not None:return saved
        if body is None and self.history:body=await self.history.store.incident(str(incident_id))
        return body

    async def _store_incident(self, body: Mapping[str, Any]) -> None:
        from ..observed.context import stamp
        if isinstance(body,dict):stamp(body)
        incident_id = str(body.get("incident_id") or "")
        if not incident_id:
            return
        await self._results.put_incident(incident_id, dict(body))

    async def list_incidents(self) -> list[dict[str, Any]]:
        """已留存的 incident 摘要（倒序：最近的在前）。

        ★ 只给摘要（id/status/意图/轮次/证据条数），不给整份 body ——
          列表接口不该把"每次排查的全部证据"都吐出来（那是 `GET /v1/incidents/{id}` 的事）。
        ★ 顺序由存储实现给（进程内实现有插入序；Redis 实现不维护索引 →
          返回空列表并留 note，端点侧照"还没查过"处理，**不假装有**）。
        """
        out: list[dict[str, Any]] = []
        for incident_id in await self._results.list_incidents(limit=self._cache_max):
            body = await self._results.get_incident(incident_id) or {}
            from ..observed.context import current,body_scope
            scope=current()
            if scope and body_scope(body)!=scope.observed_id:continue
            out.append({"incident_id": incident_id,
                        "status": body.get("status"),
                        "session_id": body.get("session_id"),
                        "intent": (body.get("intent") or {}).get("name"),
                        "rounds": body.get("rounds"),
                        "evidence_count": len(body.get("evidence") or [])})
        return out

    # ------------------------------------------------------------------
    async def _inherited_investigation(self, request: ChatRequest,
                                       degraded: list[str]) -> dict[str, Any]:
        """挑出**可继承给这一轮的上一轮排查**（没有就返回空 dict）。

        挑选口径（四条**都要**满足，宁可不继承也不猜）：
          ① 同一会话、本地轮次台账里**最近的一条**带 `incident_id` 的记录；
          ② 那条记录有**抓取时刻**，且距今 ≤ `RECENT_TTL_SEC`（与"刚完成的任务"同一档 TTL）；
          ③ 那次事故的留存里**真的有证据**（没证据继承过来也没用）；
          ④ 不继承**本会话以外的**东西（不跨会话）。

        ★ 实体是否对得上，交给编排层判（那时槽位已经抽出来了）——
          这里只负责"取候选 + 说明为什么取了它"。
        """
        if not request.session_id:
            return {}
        store = getattr(self, "sessions", None)
        reader = getattr(store, "turn_meta", None)
        if reader is None:
            return {}
        try:
            metas = list(reader(request.session_id, limit=TURN_META_SCAN))
        except Exception as exc:  # noqa: BLE001 —— 台账读不到只是"不继承"，不打扰这一轮
            degraded.append(f"上一轮排查不可读（{type(exc).__name__}）→ 处置候选将没有可继承的依据")
            return {}
        now = time.time()
        for meta in reversed(metas):
            incident_id = str((meta or {}).get("incident_id") or "")
            try:
                stamp = float((meta or {}).get("at") or 0.0)
            except (TypeError, ValueError):
                stamp = 0.0
            if not incident_id or stamp <= 0:
                continue
            age = now - stamp
            if age > RECENT_TTL_SEC:
                continue
            body = await self._results.get_incident(incident_id) or {}
            evidence = [item for item in (body.get("evidence") or []) if isinstance(item, Mapping)]
            if not evidence:
                continue
            services = sorted({str((item.get("entity") or {}).get("name") or "")
                               for item in evidence if isinstance(item.get("entity"), Mapping)}
                              - {""})
            from ..attribution.v2.followup import investigation_snapshot
            return investigation_snapshot(body,incident_id,stamp,age,evidence,services)
        return {}

    async def _note_turn(self, request: ChatRequest, body: dict[str, Any]) -> None:
        """记一轮会话台账（**只记这一轮属于哪次事故、什么状态**，不存答案）。

        ★ 会话台账只在**这一处**收口（同步与流式都过这里）——
          于是被拒的重复请求（根本没跑）不会多记一轮。
        ★ 结果留存只服务 `GET /v1/incidents/{id}`（`_store_incident`）：
          "同一句话再问一次"不再返还旧答案，旧答案也不再需要按请求指纹存一份。
        """
        from ..history.codec import stored_message
        note_turn(self.sessions, request.session_id, message=stored_message(request,body),
                  status=str(body.get("status") or ""),
                  incident_id=str(body.get("incident_id") or ""),
                  summary=_turn_summary(body))

    @staticmethod
    def _tool_calls_of(result: Any) -> list[dict[str, Any]]:
        """本轮**实际调过的工具**（编排层无条件记下；同步路径同样有）。"""
        flags = getattr(getattr(result, "state", None), "flags", None) or {}
        return [dict(item) for item in (flags.get("tool_calls") or ())
                if isinstance(item, dict)]

    @staticmethod
    def _process_steps_of(result: Any) -> list[dict[str, Any]]:
        """本轮**执行过程的时间线**（工具卡 + 非工具环节，按发生顺序）。

        ★ 用户实测（2026-10-05）："你是不是把思考过程也删了？刷新页面后就不见了，
          我想要刷新后依旧保留 AIOps 执行的过程。" —— 这些行以前只走 SSE。
        """
        flags = getattr(getattr(result, "state", None), "flags", None) or {}
        return [dict(item) for item in (flags.get("process_steps") or ())
                if isinstance(item, dict)]

    async def _attach_turn_answer(self, request: ChatRequest, body: dict[str, Any],
                                  result: Any = None) -> None:
        """把这一轮的**答案原文**补进会话记忆（近层）—— 补不上要**留痕**。

        ★ 用户实测（2026-10-03）："刷新后对话显示就变了……**回答原文为什么不给看**？"
          根因是近层只存了 200 字符的摘要（`response_digest`），答案原文根本没落库。
        ★ 这条是**旁路**：写不进去不影响这一轮的答案，但必须在 `degraded` 里说清 ——
          否则用户下次刷新又只看到一句摘要，还是不知道为什么。
        """
        memory = getattr(self._orchestrator, "session", None)
        attach = getattr(memory, "attach_answer", None)
        answer = ((body.get("explanation") or {}).get("answer") or {})
        from ..memory.answer_snapshot import with_diagnosis
        answer=with_diagnosis(answer,(body.get('explanation') or {}).get('attribution_v2'))
        if attach is None or not request.session_id or not isinstance(answer, dict):
            return
        # ★★ 这一轮**调过哪些工具**也要落近层（用户 2026-10-03 指示：刷新后工具卡不该消失）。
        #   它由编排层**无条件**记进 `state.flags["tool_calls"]`（同步 `/v1/chat`
        #   没有 SSE 订阅者时同样有），这里只是把它交给记忆层。
        tools = self._tool_calls_of(result) or None
        # ★★ 执行过程（`intent` / `agent_start` / `agent_done` / `report` … + 工具卡的位置）
        #   同样落近层：刷新后「推理过程」要能把"AI 是怎么跑的"恢复出来。
        steps = self._process_steps_of(result) or None
        try:
            stored = await attach(request.session_id, answer, tools, steps)
        except Exception as exc:              # noqa: BLE001 —— 旁路失败不许带走这一轮
            stored = False
            _append_note(body, f"答案原文没能写进会话记忆（{type(exc).__name__}: {exc}）"
                               f"→ 刷新后这一轮只能看到摘要")
            return
        if not stored:
            _append_note(body, "★ 答案原文没能写进会话记忆（这一轮没落上或记忆不可用）"
                               "→ 刷新后这一轮只能看到摘要")

    async def _note_context(self, request, body):
        from ..observed.context_notes import note
        await note(self,request,body)


def _turn_summary(body: Mapping[str, Any]) -> str:
    """这一轮的**状态摘要**：回答的第一句（恢复会话时每轮显示的就是它）。

    ★ 为什么取回答而不是状态码：状态码已经在旁边单独一格里了，而"这一轮聊到哪了"
      只有回答说得清；模型没写回答时就退回状态码（**不编**）。
    """
    for key in ("report", "answer", "summary"):
        text = str(body.get(key) or "").strip()
        if text:
            return " ".join(text.splitlines()[0].split())
    return str(body.get("status") or "").strip()


def _budget_of(body: Mapping[str, Any]) -> Budget:
    """按这一次的**真实用量**与契约里的上限造一个预算（压力判定读它，不按字符猜）。

    ★ 上限取响应里的 `budget.limits.max_tokens`（编排层已把请求选项与配置取较严者折进去），
      用量取 `cost.tokens`（模型网关的**实测**记账）—— 两者都是契约字段，不另立口径。
    """
    limits = (body.get("budget") or {}).get("limits") or {}
    tokens = (body.get("cost") or {}).get("tokens") or 0
    try:
        maximum = int(limits.get("max_tokens") or 0)
    except (TypeError, ValueError):
        maximum = 0
    try:
        used = int(tokens)
    except (TypeError, ValueError):
        used = 0
    return Budget(max_tokens=maximum, tokens=used)


def _append_note(body: dict[str, Any], text: str) -> None:
    """往响应的 `degraded` 里追加一条留痕（**空的不写、重复的不写**）。"""
    clean = str(text or "").strip()
    if not clean:
        return
    notes = body.setdefault("degraded", [])
    if isinstance(notes, list) and clean not in notes:
        notes.append(clean)

