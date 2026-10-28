"""工作记忆（Redis / 内存两种实现）。

★ 本文件由原先的单文件 `stores.py`（903 行）拆分而来；
  **公开导入面一字未改**（包的 `__init__.py` 显式再导出）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Mapping, Protocol, runtime_checkable

from ...state.backend import KEYS, decode_json, decode_json_object, encode_json
from .base import MemoryUnavailable, SESSION_KEY_PREFIX, WORKING_TTL_SEC
from .records import ConversationTurn

#: 会话键的三种后缀 → 共享键表（`state.backend.KEYS`）里的键名。
#: ★ 键名**只在那一张表里写**：这里若写成 f-string 拼接，改名时就会两边各写一半。
_WORKING_KEY_KINDS: Final[dict[str, str]] = {
    "turns": "working_turns",
    "state": "working_state",
    "asked": "working_asked",
}


def _tools_from_doc(raw: Any) -> tuple[tuple[str, str, str, str, bool, str], ...]:
    """把存下的工具调用读回 `((name, title, params, result, ok, call_id), …)`。

    ★ 形状不对就**丢掉那一条**；老的 5 元组（还没有 `call_id` 那一版）照样读得进来
      （`call_id` 补空串 —— 那一轮的过程时间线会对不上工具卡，但**不编**）。
    """
    out: list[tuple[str, str, str, str, bool, str]] = []
    for item in (raw or ()):
        if not isinstance(item, (list, tuple)) or len(item) < 4:
            continue
        ok = bool(item[4]) if len(item) > 4 else True
        call_id = str(item[5]) if len(item) > 5 else ""
        out.append((str(item[0]), str(item[1]), str(item[2]), str(item[3]), ok, call_id))
    return tuple(out)


def _steps_from_doc(raw: Any) -> tuple[tuple[str, str], ...]:
    """把存下的**执行过程时间线**读回 `((event, payload_json), …)`；形状不对就丢掉那一条。"""
    out: list[tuple[str, str]] = []
    for item in (raw or ()):
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append((str(item[0]), str(item[1])))
        elif isinstance(item, Mapping):
            out.append((str(item.get("event") or ""), str(item.get("payload") or "")))
    return tuple(item for item in out if item[0])


def _rows_from_doc(raw: Any) -> tuple[tuple[str, str], ...]:
    """把存下来的明细行读回 `((label, value), …)`；形状不对就**丢掉那一行**（不猜）。"""
    out: list[tuple[str, str]] = []
    for item in (raw or ()):
        if isinstance(item, (list, tuple)) and len(item) == 2:
            out.append((str(item[0]), str(item[1])))
        elif isinstance(item, Mapping):
            out.append((str(item.get("label") or ""), str(item.get("value") or "")))
    return tuple(out)


# ======================================================================
#  ① 工作记忆（Redis，会话级 TTL 24h）
# ======================================================================
@runtime_checkable
class WorkingMemory(Protocol):
    """工作记忆：会话历史 + 澄清记账（黑板本身在 `BoardStore`）。"""

    async def append_turn(self, session_id: str, turn: ConversationTurn) -> None: ...
    async def turns(self, session_id: str, *, limit: int = 20) -> list[ConversationTurn]: ...
    async def replace_last_turn(self, session_id: str, turn: ConversationTurn) -> bool: ...
    async def set_state(self, session_id: str, state: Mapping[str, Any]) -> None: ...
    async def get_state(self, session_id: str) -> dict[str, Any]: ...
    async def mark_asked_gap(self, session_id: str, gap: str) -> None: ...
    async def asked_gaps(self, session_id: str) -> set[str]: ...
    async def clear(self, session_id: str) -> None: ...

@dataclass
class InMemoryWorkingMemory:
    """内存工作记忆（离线单测用；**不实现 TTL** —— 那是 Redis 的职责）。"""

    _turns: dict[str, list[ConversationTurn]] = field(default_factory=dict, repr=False)
    _state: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _asked: dict[str, set[str]] = field(default_factory=dict, repr=False)

    async def append_turn(self, session_id: str, turn: ConversationTurn) -> None:
        self._turns.setdefault(session_id, []).append(turn)

    async def restore_turns(self,session_id,turns):
        if not self._turns.get(session_id):self._turns[session_id]=list(turns[-20:])

    async def turns(self, session_id: str, *, limit: int = 20) -> list[ConversationTurn]:
        return list(self._turns.get(session_id, []))[-limit:]

    async def replace_last_turn(self, session_id: str, turn: ConversationTurn) -> bool:
        items = self._turns.get(session_id)
        if not items:
            return False
        items[-1] = turn
        return True

    async def set_state(self, session_id: str, state: Mapping[str, Any]) -> None:
        self._state[session_id] = dict(state)

    async def get_state(self, session_id: str) -> dict[str, Any]:
        return dict(self._state.get(session_id, {}))

    async def mark_asked_gap(self, session_id: str, gap: str) -> None:
        self._asked.setdefault(session_id, set()).add(gap)

    async def asked_gaps(self, session_id: str) -> set[str]:
        return set(self._asked.get(session_id, set()))

    async def clear(self, session_id: str) -> None:
        self._turns.pop(session_id, None)
        self._state.pop(session_id, None)
        self._asked.pop(session_id, None)

@dataclass
class RedisWorkingMemory:
    """Redis 工作记忆（会话级 TTL 24h）。

    ★ 键结构（刻意扁平，便于 `redis-cli` 直接看）::

        aiops:sess:<sid>:turns    LIST  （JSON，每轮一条，LPUSH 后 LTRIM 保最近 N 条）
        aiops:sess:<sid>:state    HASH  （槽位/黑板摘要）
        aiops:sess:<sid>:asked    SET   （已经问过用户的缺口 —— 澄清预算用）

    ★ 键名取自共享键表 `state.backend.KEYS`（`{session_prefix}` = `SESSION_KEY_PREFIX`）：
      这一层不区分租户，所以键名**沿用原写法**（改名会让在线会话的历史读不到）。

    ★ 每次写都刷新 TTL：**活跃会话不会因为"24 小时前开过"而丢上下文**，
      而闲置会话会自动过期（这正是"会话级"该有的语义）。
    """

    client: Any                                   # redis.asyncio.Redis
    ttl_sec: int = WORKING_TTL_SEC
    max_turns: int = 20

    def _key(self, session_id: str, suffix: str) -> str:
        return KEYS[_WORKING_KEY_KINDS[suffix]].format(
            session_prefix=SESSION_KEY_PREFIX, session_id=session_id)

    async def _touch(self, *keys: str) -> None:
        """刷新 TTL。★ **必须 await**：`redis.asyncio` 的 `expire` 是协程，
        漏了 await 不只是"没刷新 TTL"，还会在日志里刷
        `coroutine 'Redis.execute_command' was never awaited`。"""
        for key in keys:
            await self.client.expire(key, self.ttl_sec)

    async def append_turn(self, session_id: str, turn: ConversationTurn) -> None:
        key = self._key(session_id, "turns")
        try:
            await self.client.lpush(key, encode_json(turn.to_dict()))
            await self.client.ltrim(key, 0, self.max_turns - 1)
            await self._touch(key)
        except Exception as exc:  # noqa: BLE001
            raise MemoryUnavailable("working", f"Redis 写入失败：{type(exc).__name__}") from exc

    async def restore_turns(self,session_id,turns):
        script="""if redis.call('EXISTS',KEYS[1])==1 then return 0 end
        for i=2,#ARGV do redis.call('RPUSH',KEYS[1],ARGV[i]) end
        redis.call('EXPIRE',KEYS[1],ARGV[1]); return 1"""
        if turns:
            await self.client.eval(script,1,self._key(session_id,'turns'),self.ttl_sec,
                                   *[encode_json(t.to_dict()) for t in reversed(turns[-self.max_turns:])])

    async def turns(self, session_id: str, *, limit: int = 20) -> list[ConversationTurn]:
        key = self._key(session_id, "turns")
        try:
            raw = await self.client.lrange(key, 0, limit - 1)
        except Exception as exc:  # noqa: BLE001
            raise MemoryUnavailable("working", f"Redis 读取失败：{type(exc).__name__}") from exc
        out: list[ConversationTurn] = []
        for item in reversed(raw):                    # LPUSH → 列表是"新到旧"，翻回来
            doc = decode_json_object(item)            # 坏条目跳过（与"坏 JSON 跳过"同口径）
            if doc is None:
                continue
            out.append(ConversationTurn(
                turn=int(doc.get("turn") or 0),
                user_message=str(doc.get("user_message") or ""),
                response_digest=str(doc.get("response_digest") or ""),
                service_names=tuple(str(s) for s in (doc.get("service_names") or [])),
                at=str(doc.get("at") or ""),
                answer_summary=str(doc.get("answer_summary") or ""),
                answer_rows=_rows_from_doc(doc.get("answer_rows")),
                answer_kind=str(doc.get("answer_kind") or ""),
                answer_advisor_status=str(doc.get("answer_advisor_status") or ""),
                answer_snapshot=dict(doc.get('answer_snapshot') or {}),
                tools=_tools_from_doc(doc.get("tools")),
                steps=_steps_from_doc(doc.get("steps")),
            ))
        return out

    async def set_state(self, session_id: str, state: Mapping[str, Any]) -> None:
        key = self._key(session_id, "state")
        try:
            if state:
                await self.client.hset(key, mapping={
                    k: encode_json(v) for k, v in state.items()})
            await self._touch(key)
        except Exception as exc:  # noqa: BLE001
            raise MemoryUnavailable("working", f"Redis 写入失败：{type(exc).__name__}") from exc

    async def replace_last_turn(self, session_id: str, turn: ConversationTurn) -> bool:
        """把**最近一轮**换成带了答案原文的那一份（LPUSH 列表 → 最新在 index 0）。

        ★ 没有这一轮时返回 `False`（**不新建**：那会凭空造出一条没有用户原话的轮次）。
        """
        key = self._key(session_id, "turns")
        try:
            if not await self.client.llen(key):
                return False
            await self.client.lset(key, 0, encode_json(turn.to_dict()))
            await self._touch(key)
        except Exception as exc:  # noqa: BLE001
            raise MemoryUnavailable("working", f"Redis 写入失败：{type(exc).__name__}") from exc
        return True

    async def get_state(self, session_id: str) -> dict[str, Any]:
        key = self._key(session_id, "state")
        try:
            raw = await self.client.hgetall(key)
        except Exception as exc:  # noqa: BLE001
            raise MemoryUnavailable("working", f"Redis 读取失败：{type(exc).__name__}") from exc
        out: dict[str, Any] = {}
        for field_name, value in (raw or {}).items():
            name = field_name.decode() if isinstance(field_name, bytes) else str(field_name)
            text = value.decode() if isinstance(value, bytes) else value
            # ★ 解析不出来就如实回原文（**不猜**：这份状态不是 JSON，但它是读到了的）
            out[name] = decode_json(text, default=text)
        return out

    async def mark_asked_gap(self, session_id: str, gap: str) -> None:
        key = self._key(session_id, "asked")
        try:
            await self.client.sadd(key, gap)
            await self._touch(key)
        except Exception as exc:  # noqa: BLE001
            raise MemoryUnavailable("working", f"Redis 写入失败：{type(exc).__name__}") from exc

    async def asked_gaps(self, session_id: str) -> set[str]:
        key = self._key(session_id, "asked")
        try:
            raw = await self.client.smembers(key)
        except Exception as exc:  # noqa: BLE001
            raise MemoryUnavailable("working", f"Redis 读取失败：{type(exc).__name__}") from exc
        return {item.decode() if isinstance(item, bytes) else str(item) for item in (raw or set())}

    async def clear(self, session_id: str) -> None:
        try:
            await self.client.delete(*[self._key(session_id, s) for s in ("turns", "state", "asked")])
        except Exception as exc:  # noqa: BLE001
            raise MemoryUnavailable("working", f"Redis 删除失败：{type(exc).__name__}") from exc
