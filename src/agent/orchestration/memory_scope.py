"""**作用域与落盘**接线：把"查询数据记忆"挂到本次会话上。

★★ 为什么需要这一层（而不是让工具层自己找会话）

工具层（`tools/handlers.py`）拿不到"这是哪次会话"——执行器是**跨会话共享**的。
所以：

  · **作用域**（哪次会话）由编排层在 `run()` 里设成 ContextVar
    （与 `api/constants._USAGE` 同一手法：值随 `asyncio` 任务拷贝，并发下不串味）；
  · **索引的落盘**（`MEMORY_KEY`）由编排层做 —— 它有**会话对象**，
    而且"整份读-改-写"这条纪律本来就该只在一处。

★ 数据本体**不进**会话状态：会话状态是整份读写的，塞几万个点会让每轮都变慢。
     数据在本地落盘（查询缓存）里，记忆里只留**索引**（分层口径）。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from ..memory.queries import MEMORY_KEY
from ..tools.notes import SinkScope, collect

logger = logging.getLogger("agent.orchestration.memory_scope")


class MemoryScopeMixin:
    """查询数据记忆的**作用域**、**索引落盘**与**取数留痕回收**。"""

    def _tool_notes_scope(self) -> SinkScope:
        """本次运行的取数留痕槽（命中、缺口、fail-open 都写这里）。"""
        return SinkScope()

    @staticmethod
    def _collect_tool_notes(state: Any, sink: Optional[list] = None) -> None:
        """把取数留痕汇进 `degraded`（**必须可见**：命中/缺口/降级不许静默）。

        ★★ `sink` 由调用方显式传进来（就是 `SinkScope.sink` 那个列表）：
          修前这里读的是 **ContextVar**，而它是在 `with` **退出之后**调的 ——
          槽已经 `reset` 回父上下文，读到的是**空** ⇒ 留痕被吞掉（真机实测：
          "只补缺口 22 秒"算出来了、日志里也有，响应里却看不到）。
        """
        lines = sink if sink is not None else list(collect())
        for line in lines:
            if line and line not in state.degraded_detail:
                state.degraded_detail.append(line)

    def _query_memory(self) -> Optional[Any]:
        """本次装配的查询数据记忆（没接执行器 / 关掉了 → `None`）。"""
        context = getattr(self.executor, "context", None)
        return getattr(context, "query_memory", None)

    async def _hydrate_query_memory(self, session_id: str) -> None:
        """跑之前把**索引**从会话工作记忆读回来（读不到就当没有 —— fail-open）。"""
        memory, session = self._query_memory(), self._session()
        if memory is None or session is None or not session_id:
            return
        try:
            state = await session.get_state(session_id)
        except Exception as exc:            # noqa: BLE001 —— 记忆不可用不该让请求失败
            logger.debug("查询记忆读取失败（%s）→ 本次按无记忆处理", type(exc).__name__)
            return
        count = memory.hydrate(session_id, state if isinstance(state, Mapping) else {})
        if count:
            logger.debug("查询记忆：读回 %d 条索引（session=%s）", count, session_id)

    async def _persist_query_memory(self, session_id: str) -> None:
        """跑完把**索引**写回会话工作记忆（整份读-改-写；失败只记日志）。"""
        memory, session = self._query_memory(), self._session()
        if memory is None or session is None or not session_id:
            return
        try:
            payload = memory.payload(session_id)
            if not payload:
                return
            state = await session.get_state(session_id)
            merged = dict(state) if isinstance(state, Mapping) else {}
            merged[MEMORY_KEY] = payload
            await session.set_state(session_id, merged)
        except Exception as exc:            # noqa: BLE001 —— 同上
            logger.debug("查询记忆写入失败（%s）→ 本次不落盘", type(exc).__name__)
