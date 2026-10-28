"""会话闸门：同一 `session_id` 同时只允许**一个真跑**。

★ 为什么把它抽出来：这条不变量原先由进程内的一个 dict 承担 ——
  于是"重启后同会话幂等"与"双实例互斥"都不成立。
  抽成接口后：进程内实现与 Redis 实现（多实例）可替换。

★★ 血泪约束（三处都要守）：
  ① 释放闸门必须**校验持有者**（`pop` 判 `is`，Redis 侧判 CAS）：
     一次收尾删掉了同会话另一个还在跑的请求的闸门 → 第三个请求看到"没人跑" → 并行计费；
  ② 取消必须被**翻译成普通失败**再交给等待者 —— 否则一条断线会打断同会话所有请求；
  ③ 等待方**不许被连带取消**（`shield`），且闸门是**续期式**的（排查可能比 TTL 长）。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Final, Mapping, Optional, Protocol, runtime_checkable

from .backend import KEYS, RedisLike, StateUnavailable, key_prefix, ttl_ms

__all__ = ["GateHandle", "InflightGate", "InProcessInflightGate", "RedisInflightGate",
           "DEFAULT_GATE_TTL_SEC"]

#: 闸门 TTL（续期式：正常情况下由持有者主动续期；这个值只是"持有者暴毙"时的自愈上界）
DEFAULT_GATE_TTL_SEC: Final[float] = 900.0


@runtime_checkable
class GateHandle(Protocol):
    """一次领跑的"占位凭证"。"""

    session_id: str
    token: str

    def done(self) -> bool:
        """这次领跑是否已经落定（成功或失败）。"""

    def settle(self, body: Optional[Mapping[str, Any]],
               exc: Optional[BaseException] = None) -> None:
        """把结局交给等待者（**幂等**：重复调用只生效一次）。"""

    async def wait(self) -> None:
        """等这次领跑结束（**不等它的结果**：起来后由调用方自己再判一次重复）。"""


@runtime_checkable
class InflightGate(Protocol):
    """闸门接口：拿 → 看 → 清（只清自己的）。"""

    async def acquire(self, session_id: str, trace_id: str) -> Optional[GateHandle]:
        """占住闸门；**别人已占**返回 None（调用方应回去等它落定）。"""

    async def peek(self, session_id: str) -> Optional[GateHandle]:
        """看有没有人在跑（**只读**：不要拿它的 handle 去 settle）。"""

    async def pop(self, session_id: str, gate: GateHandle) -> None:
        """清除闸门 —— **只清自己建立的那一个**。"""

    async def renew(self, gate: GateHandle) -> bool:
        """续期（进程内实现直接 True；Redis 侧 `PEXPIRE`）。"""


class _InProcessHandle:
    """进程内 handle：押一个 `asyncio.Future`。"""

    __slots__ = ("session_id", "token", "_future", "_settled")

    def __init__(self, session_id: str, token: str,
                 future: "asyncio.Future[dict[str, Any]]") -> None:
        self.session_id = session_id
        self.token = token
        self._future = future
        self._settled = False

    def done(self) -> bool:
        return self._future.done()

    # ★ 保留 `result()` / `exception()`：迁移期间既有代码（与用例）是按 `asyncio.Future`
    #   的鸭子类型写的，少这两个方法会让它们静默变成 AttributeError。
    def result(self) -> dict[str, Any]:
        return self._future.result()

    def exception(self) -> Optional[BaseException]:
        return self._future.exception()

    def settle(self, body: Optional[Mapping[str, Any]],
               exc: Optional[BaseException] = None) -> None:
        """落定结局。

        ★ **领跑者被取消**（客户端断线）时不能把 `CancelledError` 原样塞进
          闸门 —— 等待方在 `await asyncio.shield(...)` 处会收到它，于是等待方**自己**
          的请求也被取消（一条断线打断同会话所有请求）。取消必须翻译成普通失败。
        """
        if self._settled or self._future.done():
            return
        self._settled = True
        if body is not None:
            self._future.set_result(dict(body))
            return
        failure: BaseException = exc if exc is not None else RuntimeError("领跑请求失败")
        if isinstance(failure, asyncio.CancelledError):
            failure = RuntimeError("领跑的那次请求被取消（客户端断开）—— 重试即可")
        self._future.set_exception(failure)
        self._future.exception()          # 标记已取：避免 "never retrieved" 噪音

    async def wait(self) -> None:
        await asyncio.shield(self._future)


class InProcessInflightGate:
    """进程内闸门（**默认装配**；只有单进程语义）。

    ★ `table` 暴露内部字典，是为了让既有用例能继续观测"闸门在不在表里"；
      生产代码不要读它 —— 换成 Redis 实现时那张表根本不存在。

    ★ 为什么还留三个 `*_nowait` 同步方法：`ChatService._close_stream` 是**同步收尾**
      （它跑在 `stream_events` 的 `finally` 里，取消路径上不能 await ——
      三次翻车都在"取消时清理没跑完"）。默认装配是进程内实现，这条路径必须保持
      **同步且一定执行**；换成分布式实现时，收尾改走异步接口（见 `shared`）。
    """

    #: `True` = 状态在本进程内（同步接口可用）；分布式实现为 `False`
    shared = False

    def __init__(self, *, ttl_sec: float = DEFAULT_GATE_TTL_SEC) -> None:
        self.ttl_sec = ttl_sec
        self.table: dict[str, _InProcessHandle] = {}

    def open_nowait(self, session_id: str, trace_id: str = "") -> GateHandle:
        """同步建闸门（**覆盖**同键 = 后到的接手）。"""
        loop = asyncio.get_running_loop()
        handle = _InProcessHandle(session_id, trace_id, loop.create_future())
        self.table[session_id] = handle
        return handle

    def peek_nowait(self, session_id: str) -> Optional[GateHandle]:
        return self.table.get(session_id)

    def pop_nowait(self, session_id: str, gate: GateHandle) -> None:
        """清除闸门 —— **只清自己建立的那一个**（判据是对象身份，不是"键在不在"）。"""
        if self.table.get(session_id) is gate:
            self.table.pop(session_id, None)

    async def acquire(self, session_id: str, trace_id: str) -> Optional[GateHandle]:
        return self.open_nowait(session_id, trace_id)

    async def peek(self, session_id: str) -> Optional[GateHandle]:
        return self.peek_nowait(session_id)

    async def pop(self, session_id: str, gate: GateHandle) -> None:
        self.pop_nowait(session_id, gate)

    async def renew(self, gate: GateHandle) -> bool:
        return not gate.done()


class _RedisHandle:
    """Redis handle：**不押 Future**，等待 = 轮询"锁还在不在"。

    ★ 为什么这样也成立：领跑者落定时会释放锁；等待方醒来后**自己再判一次重复**
      （`(session_id, turn_id)` 或抖动窗口内的同内容 → 直接拒绝），
      而不是去取领跑者的结果 —— 重复请求本来就不该拿到答案。
    """

    __slots__ = ("session_id", "token", "_backend", "_key", "_ttl_sec", "owned",
                 "_finished")

    def __init__(self, session_id: str, token: str, *, backend: RedisLike, key: str,
                 ttl_sec: float, owned: bool) -> None:
        self.session_id = session_id
        self.token = token
        self._backend = backend
        self._key = key
        self._ttl_sec = ttl_sec
        #: `True` = 这把锁是**我抢到的**（只有这种 handle 才允许释放）
        self.owned = owned
        self._finished = False

    def done(self) -> bool:
        return self._finished

    def settle(self, body: Optional[Mapping[str, Any]],
               exc: Optional[BaseException] = None) -> None:
        """落定（**锁的释放在 `pop` 里**；`body` 只是留给调用方自己用，不进任何回放缓存）。

        ★ 两件事必须分开：`settle` 是"这次跑完了"，`pop` 才是"把锁还回去"。
          合并成一件会让 `_close_stream`（先 settle 再 pop）在 Redis 实现下
          **把锁留着不还**，直到 TTL 才自愈。
        ★ 非持有者（`peek` 拿到的只读 handle）调用它是 **no-op**。
        """
        if self.owned:
            self._finished = True

    async def wait(self) -> None:
        """等到锁消失（或超时）：超时按**失败**上报，绝不静默继续。"""
        deadline = asyncio.get_running_loop().time() + self._ttl_sec
        while True:
            current = await self._backend.get(self._key)
            if current is None:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise StateUnavailable(
                    f"等待同会话领跑者超过 {self._ttl_sec:g}s（闸门仍在 {self._key}）")
            await asyncio.sleep(RELEASE_POLL_SEC)


#: 等待方轮询间隔（Redis 实现专用；进程内实现不轮询）
RELEASE_POLL_SEC: Final[float] = 0.05


class RedisInflightGate:
    """Redis 闸门：`SET NX PX` 抢锁 + **CAS 释放** + 续期。

    ★ `shared = True`：分布式实现**只有异步接口**（同步的 `*_nowait` 一律抛错），
      以免有人在取消路径上"顺手"调用它却没等它完成（那等于把清理丢掉）。
    """

    shared = True

    def __init__(self, backend: RedisLike, *, tenant: str = "default",
                 ttl_sec: float = DEFAULT_GATE_TTL_SEC) -> None:
        self._backend = backend
        self._prefix = key_prefix(tenant)
        self.ttl_sec = ttl_sec

    def _key(self, session_id: str) -> str:
        return KEYS["gate"].format(prefix=self._prefix, session_id=session_id)

    # ---- 同步接口：分布式实现一律拒绝（见类注释）----
    def open_nowait(self, session_id: str, trace_id: str = "") -> GateHandle:
        raise StateUnavailable(
            "分布式闸门没有同步 open：请用 `await acquire(session_id, trace_id)`")

    def peek_nowait(self, session_id: str) -> Optional[GateHandle]:
        raise StateUnavailable("分布式闸门没有同步 peek：请用 `await peek(session_id)`")

    def pop_nowait(self, session_id: str, gate: GateHandle) -> None:
        raise StateUnavailable(
            "分布式闸门没有同步 pop：释放要走 `await pop(...)`（CAS），"
            "取消路径上用 create_task 触发，不能把释放丢掉")

    async def acquire(self, session_id: str, trace_id: str) -> Optional[GateHandle]:
        token = f"{trace_id}:{uuid.uuid4().hex[:8]}"
        ok = await self._backend.set_nx_px(self._key(session_id), token,
                                           ttl_ms(self.ttl_sec))
        if not ok:
            return None
        return _RedisHandle(session_id, token, backend=self._backend,
                            key=self._key(session_id), ttl_sec=self.ttl_sec, owned=True)

    async def peek(self, session_id: str) -> Optional[GateHandle]:
        current = await self._backend.get(self._key(session_id))
        if current is None:
            return None
        return _RedisHandle(session_id, str(current), backend=self._backend,
                            key=self._key(session_id), ttl_sec=self.ttl_sec, owned=False)

    async def pop(self, session_id: str, gate: GateHandle) -> None:
        """CAS 释放：只有"值还是我的 token"才删。

        ★★ 还有一道**所有权**闸门：`peek()` 返回的只读 handle 虽然带着当前持有者的 token，
          但它**不是抢锁的那个** —— 允许它 `pop` 就等于"看一眼就能放掉别人的锁"。
          这里直接拒绝（fail-closed），而不是静默 no-op：静默会让"释放失败"看不见。
        """
        if not getattr(gate, "owned", False):
            raise StateUnavailable(
                "拒绝释放一个不是自己抢到的闸门（`peek()` 的 handle 只用于等待）")
        await self._backend.compare_and_delete(self._key(session_id), gate.token)
        if isinstance(gate, _RedisHandle):
            gate.owned = False
            gate._finished = True

    async def renew(self, gate: GateHandle) -> bool:
        """续期**自己抢到的**锁（别人的锁不归我续：那等于替它保命）。"""
        if not getattr(gate, "owned", False):
            return False
        return await self._backend.touch(self._key(gate.session_id), ttl_ms(self.ttl_sec))
