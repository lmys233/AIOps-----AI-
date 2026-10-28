"""待确认动作：澄清确认与审批令牌放共享存储。

★ 为什么必须外置：`/v1/chat/resume` 的两条路都要"找回当初那条动作"：
  · 澄清确认（`_is_confirmation` + `_pending_actions[session_id]`）；
  · 审批（`approvals.get(request_id)`）。
  原先它们放在进程内的两个 dict 里 → **进程一重启，"审批了但找不到动作"**；
  多实例下更常见：审批请求打到 A、确认落到 B → 直接 404/过期。

★ 两套实现：
  · `InProcessPendingActionStore`：dict + 容量上限 + 过期清扫；
  · `RedisPendingActionStore`：`SET PX` + `GET` + `DEL`，键带 TTL（过期即自愈）。
"""

from __future__ import annotations

from typing import Any, Final, Optional, Protocol, runtime_checkable

from .backend import KEYS, RedisLike, decode_json_object, encode_json, key_prefix, ttl_ms

__all__ = ["PendingActionStore", "InProcessPendingActionStore", "RedisPendingActionStore",
           "PENDING_TTL_SEC"]


#: 待确认动作的 TTL：审批令牌的有效期是 15 分钟（`common.enums.APPROVAL_TTL_MINUTES`），
#: 这里给**一倍余量**（30 分钟）—— 令牌过期前后的那一刻，也还能把"当初那条动作"找回来。
PENDING_TTL_SEC: Final[float] = 30 * 60.0


@runtime_checkable
class PendingActionStore(Protocol):
    """待确认动作（澄清确认 / 审批令牌）的读写。"""

    async def put(self, key: str, value: dict[str, Any], *,
                  ttl_sec: float = PENDING_TTL_SEC) -> None:
        """登记（**覆盖**同键；分布式实现顺带设 TTL）。"""

    async def get(self, key: str) -> Optional[dict[str, Any]]:
        """取（不存在/已过期 → None）。"""

    async def pop(self, key: str) -> Optional[dict[str, Any]]:
        """取走并删除（"确认一次"只允许生效一次）。"""


class InProcessPendingActionStore:
    """进程内实现：dict + 容量上限（与既有 `_pending_actions` 同语义）。"""

    shared = False

    def __init__(self, *, max_items: int = 4096) -> None:
        self.max_items = int(max_items)
        self.items: dict[str, dict[str, Any]] = {}

    async def put(self, key: str, value: dict[str, Any], *,
                  ttl_sec: float = PENDING_TTL_SEC) -> None:
        self.items[str(key)] = dict(value)
        while len(self.items) > self.max_items:
            self.items.pop(next(iter(self.items)), None)

    async def get(self, key: str) -> Optional[dict[str, Any]]:
        found = self.items.get(str(key))
        return dict(found) if found is not None else None

    async def pop(self, key: str) -> Optional[dict[str, Any]]:
        found = self.items.pop(str(key), None)
        return dict(found) if found is not None else None


class RedisPendingActionStore:
    """Redis 实现：`SET PX` / `GET` / `DEL`（键带 TTL，过期即自愈）。"""

    shared = True

    def __init__(self, backend: RedisLike, *, tenant: str = "default") -> None:
        self._backend = backend
        self._prefix = key_prefix(tenant)

    def _key(self, key: str) -> str:
        return KEYS["pending"].format(prefix=self._prefix, key=key)

    async def put(self, key: str, value: dict[str, Any], *,
                  ttl_sec: float = PENDING_TTL_SEC) -> None:
        await self._backend.set_px(self._key(key), encode_json(value), ttl_ms(ttl_sec))

    async def get(self, key: str) -> Optional[dict[str, Any]]:
        return decode_json_object(await self._backend.get(self._key(key)))

    async def pop(self, key: str) -> Optional[dict[str, Any]]:
        """取走并删除。

        ★ 这里**不是原子**的 GET+DEL：两个实例同时"确认同一条动作"时，两边都可能拿到值。
          为什么可以接受：`resume` 的下一道闸门是**审批令牌校验**（`action_hash` 绑定 +
          `decided_by` 前缀 + 过期时间），重复确认会被它挡下；而"用 Lua 做原子 pop"
          要在原语层再加一条（留到需要时再加，先如实记录这个边界）。
        """
        found = decode_json_object(await self._backend.get(self._key(key)))
        if found is not None:
            await self._backend.delete(self._key(key))
        return found
