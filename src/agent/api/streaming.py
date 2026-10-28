"""流式路径（SSE）：事件生成器、闸门收尾与降级兜底体。

与同步路径**共用**同一个外壳（`_reject_duplicate` / `_wait_for_leader` / `_claim_turn` /
`_run_recorded` / `_acquire_gate` / `_settle_inflight` / `_pop_inflight` 都由 `ChatService`
提供），这里只放"怎么把事件推给客户端"这一件事。
"""

from __future__ import annotations

from typing import Any, Mapping
from ..common.enums import ErrorCode
from .constants import SSE_HEARTBEAT_SEC, logger
from .contract_body import empty_contract_body
from .errors import DuplicateRequest
from .idempotency import ensure_turn_id
from .requests import ChatRequest
import asyncio
import json
import time


class StreamingMixin:
    """流式路径（SSE）：事件生成器、闸门收尾与降级兜底体。"""

    async def stream_events(self, request, *, trace_id):
        owner=asyncio.current_task();self._request_tasks.add(owner)
        try:
            async for chunk in self._stream_events(request,trace_id=trace_id):yield chunk
        finally:self._request_tasks.discard(owner)

    async def _stream_events(self, request: ChatRequest, *, trace_id: str) -> Any:
        """`POST /v1/chat/stream` 的事件生成器。

        三条硬要求都在这里落地：
          ① 每个事件带 `trace_id`（编排层的 `_emit` 统一补）；
          ② `error` 事件之后**仍然发 `report`**（降级结果），绝不静默断流；
          ③ 心跳（15s）防代理超时。

        ★ 与 `/v1/chat` **共用**同一个外壳（重复判定 / `_wait_for_leader` /
          `_run_recorded` / `_acquire_gate` / `_settle_inflight`）—— 口径不可能分叉。
          另写一套的代价：不占闸门（同会话并发各跑一次、各计一次费）、不进 `_USAGE` 桶
          （`cost` 报 **0 tokens**）、不登记待确认动作（流式提出的处置澄清无法被后续确认）。
        """
        turn_id = ensure_turn_id(request)
        # ---- ① 重复请求直接拒绝（SSE 上是 error + 降级 report，不回放旧答案）----
        # ---- ② 同一会话已在跑 → 等它落定（与 /v1/chat 同一口径），再判一次重复 ----
        try:
            await self._reject_duplicate(request, turn_id)
            await self._wait_for_leader(request, turn_id=turn_id)
        except DuplicateRequest as exc:
            for chunk in self._duplicate_events(request, trace_id=trace_id, exc=exc):
                yield chunk
            return
        # ---- ③ 自己领跑：跑一次 + 记账，边跑边把事件推出来 ----
        queue: asyncio.Queue[Any] = asyncio.Queue()
        # ★★ 闸门在**主协程**里建好再交给任务：建在 producer 任务内部的话，两个请求
        #   同时穿过 `_wait_for_leader` 之后可能各自成为领跑者并互相覆盖 gate（窄竞态）。
        # ★ 这里走 `_acquire_gate`（分布式实现下靠 `SET NX PX` 竞争）；
        #   抢输（None）说明别人刚成为领跑者 → 回去等它，**不许**并行跑。
        gate = await self._acquire_gate(request.session_id, trace_id)
        if gate is None:
            for chunk in await self._lost_race_events(request, trace_id=trace_id,
                                                      turn_id=turn_id):
                yield chunk
            return
        # ★ 登记在跑之前（与同步路径同序）：后来者据此判重复
        await self._claim_turn(request, turn_id)
        task = self.spawn(self._stream_producer(request, trace_id=trace_id,
                                                         queue=queue, gate=gate))
        try:
            async for chunk in self._drain_stream(queue):
                yield chunk
        finally:
            self._close_stream(request, gate=gate, task=task)

    async def _lost_race_events(self, request: ChatRequest, *, trace_id: str,
                                turn_id: str) -> list[str]:
        """抢输闸门后的两条出路：判出重复 → 重复事件；否则如实报「闸门忙」。"""
        try:
            await self._wait_for_leader(request, turn_id=turn_id)
        except DuplicateRequest as exc:
            return self._duplicate_events(request, trace_id=trace_id, exc=exc)
        return [self._sse("error", {
            "code": ErrorCode.INTERNAL_ERROR.value,
            "message": "同会话已有排查在跑，本次未能取得闸门，请重试",
            "error_type": "GATE_BUSY"})]

    def _duplicate_events(self, request: ChatRequest, *, trace_id: str,
                          exc: DuplicateRequest) -> list[str]:
        """重复请求的 SSE 事件：`error`（带 `CONFLICT`）+ **仍然发一份降级 `report`**。"""
        logger.info("重复请求被拒（session=%s turn=%s detail=%s）",
                    request.session_id, exc.turn_id, exc.detail)
        body = self._degraded_body(request, trace_id=trace_id, code=exc.code,
                                   note=exc.message,
                                   degraded_note="这是重复请求，本次**没有**重新排查")
        return [self._sse("error", {**exc.to_dict(), "error_type": "DUPLICATE_REQUEST"}),
                self._sse("report", body)]

    def _close_stream(self, request: ChatRequest, *,
                      gate: asyncio.Future[dict[str, Any]],
                      task: asyncio.Task[None]) -> None:
        """流式路径收尾：取消还没跑完的 producer、**落定闸门**、清掉 `_inflight`。

        ★ 闸门既然在**主协程**（`stream_events`）里建立，就必须在**主协程**里收尾：
          producer 若在"跑到第一步之前"就被取消，它的函数体**根本不会执行**
          （未启动的协程被 `throw()` 只会当场抛，进不了 `try`）→ 闸门既不落定也不
          清除 → 同 `session_id` 的后续请求在**没有超时**的等待上永久挂起。

        ★ 与 `/v1/chat` 的 `_run_as_leader` **同序**：先落定、再清表。
          `_settle_inflight` 对已落定的闸门是 no-op（幂等，重复调用无副作用）；
          但 `pop` **不是**幂等的 —— 它必须只清**自己建立的那个**闸门，
          否则会删掉同会话里另一个正在跑的请求的闸门。
        """
        if not task.done() and not task.cancelling():
            task.cancel()
        self._settle_inflight(gate, None, RuntimeError(
            "流式领跑未落定（客户端断开或任务在开始前被取消）—— 重试即可"))
        self._pop_inflight(request.session_id, gate)

    async def _stream_producer(self, request: ChatRequest, *, trace_id: str,
                               queue: asyncio.Queue[Any],
                               gate: asyncio.Future[dict[str, Any]]) -> None:
        """把一次排查的事件推进队列；**最后一定推一条 `report`**，然后关闭队列。

        ★ 与 `/v1/chat` 走同一个 `_run_recorded`：计费、`turn_id` 回显、台账与待确认
          登记几件事在这里**不可能**再漏。

        ★★ 兜底必须是 **`BaseException`**，且**取消也要落定闸门**。
          `CancelledError` 继承自 `BaseException` —— 客户端断开时 ASGI 取消的正是它：
          若让它漏出去，那个 `gate` **永不落定**；而等待方是 `await asyncio.shield(...)`
          **没有超时** → 已经拿到 gate 的同会话等待者**永久挂起**（每个挂起的请求都占着
          协程与连接）。`session_id` 由客户端给 → 未认证客户端可借此钉住连接。
          口径与 `_run_as_leader` 完全一致：**取消被翻译成普通失败**再交给等待者。
        """

        def sink(event: str, data: dict[str, Any]) -> None:
            queue.put_nowait((event, data))

        try:
            body = await self._run_recorded(request, trace_id=trace_id, on_event=sink)
            self._settle_inflight(gate, body)
            from ..presentation.transport import project
            sink("report", project(body,request.options.response_mode))
        except BaseException as exc:  # noqa: BLE001 —— 必须变成 error + report，不能断流
            cancelled = isinstance(exc, asyncio.CancelledError)
            if not cancelled:
                # 取消不是"失败"：它是"这次不做了"，不该按内部错误记堆栈
                logger.exception("流式排查失败（trace_id=%s）", trace_id)
            self._settle_inflight(gate, None, exc)
            if not cancelled:
                sink("error", {"code": ErrorCode.INTERNAL_ERROR.value,
                               "message": "内部错误，请把 trace_id 提供给维护者",
                               "error_type": type(exc).__name__})
                sink("report", self._degraded_body(
                    request, trace_id=trace_id, code=ErrorCode.INTERNAL_ERROR.value))
            if cancelled:
                raise                      # ★ 取消必须继续向上抛（不能被"处理掉"）
        finally:
            self._pop_inflight(request.session_id, gate)
            queue.put_nowait(None)

    @staticmethod
    async def _drain_stream(queue: asyncio.Queue[Any]) -> Any:
        """SSE 排空循环：15s 心跳（注释行不污染事件流）+ 事件转发，收到 `None` 收尾。"""
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_SEC)
            except asyncio.TimeoutError:
                yield f": heartbeat {int(time.time())}\n\n"
                continue
            if item is None:
                return
            event, data = item
            yield StreamingMixin._sse(event, data)

    @staticmethod
    def _sse(event: str, data: Mapping[str, Any]) -> str:
        """按 SSE 线格式拼一个事件（`data` 一行 JSON，非 ASCII 不转义）。"""
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"

    def _degraded_body(self, request: ChatRequest, *, trace_id: str, code: str,
                       incident_id: str = "", note: str = "",
                       degraded_note: str = "") -> dict[str, Any]:
        """兜底的 `report`：内部异常/重复请求时也必须给一个**符合契约**的降级结果。

        ★ 骨架来自 `empty_contract_body` —— 与正常路径**同一份字段集**，缺的键一律给
          **诚实的空值**（不是编一个看起来正常的值）。错误路径上按同一份契约取值会
          `KeyError`，是"错误路径的响应形状和正常路径不一样"这类最难查的问题的成因。
        ★ `note` / `degraded_note` 给"不是内部错误"的降级用（重复请求的措辞与内部错误
          不同 —— 同一句话糊两种原因，用户会一直重试）。
        """
        why = str(note or "内部错误，请把 trace_id 提供给维护者")
        if degraded_note:
            report = f"这次排查**没有完成**（{code}）：{why}。本次没有任何变更被执行。"
            steps = "换一轮新的 turn_id 或稍后再问；本次没有产生任何变更。"
        else:
            report = (f"这次排查**没有完成**（{code}）：{why}；"
                      "已记录 trace_id，请把它提供给维护者。")
            steps = "把 trace_id 提供给维护者后重试；本次没有产生任何变更。"
        body = empty_contract_body(
            request.session_id, trace_id=trace_id, incident_id=incident_id,
            turn_id=ensure_turn_id(request),
            write_allowed=bool(getattr(request.options, "allow_write", False)),
            degraded=[str(degraded_note or f"排查失败（{code}）—— 这是降级结果")],
            termination_reason=f"排查没有完成（{code}）")
        body.update({
            "report": report,
            "next_steps": steps,
            "error": {"code": code, "message": why},
        })
        from ..presentation.projection import finish
        return finish(body)
