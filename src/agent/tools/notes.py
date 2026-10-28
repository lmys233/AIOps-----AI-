"""取数层的**本次运行留痕槽**（缓存命中与记忆缺口的说明）。

★★ 为什么不是 `ToolContext.notes`

`ToolContext` 是**进程级共享**的（执行器跨会话复用），往它的列表里 append 会造成：

  · **跨会话串味**：A 会话的"用了记忆里的数据"出现在 B 会话的响应里
    （把"本次排查独有的事实"放进共享对象）；
  · **看不到**：那个列表没有任何出口，留痕等于没留（而"命中必须可见"是硬要求）。

所以改用 **ContextVar 留痕槽**：编排层每次 `run()` 开一个槽，工具调用另开自己的子槽，
防止并发任务共享同一个可变列表。单次结果只读自身子槽，结束后再汇总到运行槽与 `degraded`。
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Final, Optional

__all__ = ["MAX_NOTES", "note", "open_sink", "collect", "SinkScope"]

logger = logging.getLogger("agent.tools.notes")

#: 一次运行最多汇几条进 `degraded`（有界：不许把上下文刷爆）
MAX_NOTES: Final[int] = 12

_SINK: ContextVar[Optional[list[str]]] = ContextVar("aiops_tool_notes", default=None)


def open_sink() -> list[str]:
    """开一个**本次运行**的留痕槽（返回它，同时设为当前槽）。"""
    sink: list[str] = []
    _SINK.set(sink)
    return sink


def note(text: str) -> None:
    """往当前槽里写一条（**没有槽就什么都不做** —— 单测/离线路径不该因此报错）。"""
    sink = _SINK.get()
    if sink is None:
        logger.debug("留痕槽未开，丢弃一条取数留痕：%s", text[:80])
        return
    if text and text not in sink:
        sink.append(text)


def collect(*, limit: int = MAX_NOTES) -> tuple[str, ...]:
    """读当前槽（不动它）—— 调用方拿去汇进 `degraded`。"""
    sink = _SINK.get() or []
    return tuple(sink[:limit])


class SinkScope:
    """`with SinkScope() as sink:` —— 退出时把槽还原（`run()` 用它）。"""

    __slots__ = ("sink", "_token")

    def __init__(self) -> None:
        self.sink: list[str] = []
        self._token: object = None

    def __enter__(self) -> list[str]:
        self._token = _SINK.set(self.sink)
        return self.sink

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            _SINK.reset(self._token)  # type: ignore[arg-type]
