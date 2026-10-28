"""共享状态后端。

★ 为什么要有这一层：闸门与账本需要一组**原子原语**（`SET NX`、CAS 删除、`INCR`），
  而"多实例正确性"完全押在这几个原语的语义上。把原语抽成 8 个方法，是为了：
  ① 测试里能用一个**忠实的假后端**跑（不必起 Redis，本仓单测一贯不依赖 Docker）；
  ② 真后端（`RedisBackend`）只负责"翻译"，不掺业务判断。

★ 四条硬性要求在这里落地：
  ① **凡是共享状态都必须带 TTL** —— 原语签名里 TTL 不是可选参数，写不出来就没法调；
  ② **只有持有者能释放** —— 靠 `compare_and_delete`（CAS），不是裸 `delete`；
  ③ **不可达要能降级** —— 调用方拿 `StateUnavailable` 决定退回进程内实现并**如实说明**；
  ④ 键前缀带 `tenant` —— 由 `key_prefix()` 统一拼。
"""

from __future__ import annotations

import json
import time
from typing import Any, Final, Optional, Protocol, runtime_checkable

__all__ = ["KEYS", "RedisLike", "FakeRedis", "RedisBackend", "StateUnavailable",
           "key_prefix", "ttl_ms", "encode_json", "decode_json", "decode_json_object"]


class StateUnavailable(RuntimeError):
    """共享状态不可用（连不上 / 超时 / 协议错）。

    ★ 为什么单独一个异常类型：调用方要区分"**记不住**（降级到进程内并说明）"与
      "业务失败"。少了这个类型，降级就会被写成 `except Exception: pass` ——
      那正是明令禁止的静默失效。
    """


def key_prefix(tenant: str = "default") -> str:
    """键前缀：`aiops:{tenant}:`（单租户，但键名先留位）。"""
    return f"aiops:{tenant or 'default'}:"


def ttl_ms(seconds: float) -> int:
    """秒 → 毫秒（Redis 的 `PX` 单位）；至少 1s，避免写出"立刻过期"的键。"""
    return max(1000, int(seconds * 1000))


def encode_json(value: Any) -> str:
    """统一的 JSON 编码口径。

    ★ 两个参数是**口径**，不是随手写的：`ensure_ascii=False` 让中文在 `redis-cli` 里可读，
      `default=str` 让一个陌生类型不至于把**整份**状态写失败。手抄这两个参数的地方
      漏一个，就会出现"同一份数据两种编码"。
    """
    return json.dumps(value, ensure_ascii=False, default=str)


def decode_json(raw: Optional[Any], *, default: Any = None) -> Any:
    """统一的 JSON 解码：拿不到 / 解析不出来 → `default`（**不猜、不抛**）。"""
    if raw is None:
        return default
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def decode_json_object(raw: Optional[Any]) -> Optional[dict[str, Any]]:
    """只认 JSON **对象**（其他形态 → `None`）：读写双方对"什么算一份状态"同一口径。"""
    value = decode_json(raw)
    return value if isinstance(value, dict) else None


@runtime_checkable
class RedisLike(Protocol):
    """共享状态需要的**最小原语集**（8 个方法，全部带 TTL 或原子语义）。"""

    async def set_nx_px(self, key: str, value: str, ttl_millis: int) -> bool:
        """`SET key value NX PX ttl` → 拿到锁返回 True。"""

    async def set_px(self, key: str, value: str, ttl_millis: int) -> None:
        """无条件覆盖（幂等映射这类"只写不抢"的键）。"""

    async def get(self, key: str) -> Optional[str]:
        """读；不存在/已过期返回 None。"""

    async def compare_and_delete(self, key: str, expected: str) -> bool:
        """**CAS 删除**：只有当前值等于 `expected` 才删。"""

    async def incr_by(self, key: str, amount: int, ttl_seconds: int) -> int:
        """原子自增，并保证这个键**一定有 TTL**（返回自增后的值）。"""

    async def touch(self, key: str, ttl_millis: int) -> bool:
        """续期（闸门是"续期式"的；键已不存在 → False）。"""

    async def delete(self, key: str) -> None:
        """无条件删除（只用于"清理自己的统计键"这类不涉及所有权的地方）。"""

    async def ping(self) -> bool:
        """可用性探测（降级判据用它，而不是靠"调到一半失败"）。"""


#: 键空间（**这张表是键名的唯一来源**：实现不许随手起名；
#: 加键就在 `KEYS` 里加一条、再由写它的实现负责给 TTL）
#:
#: 两种占位符：
#:   · `{prefix}` = `key_prefix(tenant)` —— 共享状态层的键；
#:   · `{session_prefix}` = 记忆层会话前缀 —— **那层不区分租户**，键名沿用原写法
#:     （改键名等于让在线会话的历史突然"读不到"，与"没查成≠没有"是同一条红线）。
#:
#: ★ 这里没有"永久键"：每条都有 TTL，由写它的实现传进去
#:   （`gate` / `alert_*` / `pending` / `result_*` 传 `ttl_ms(...)` 或
#:   `incr_by(..., ttl_seconds)`；`working_*` 写路径紧跟着 `EXPIRE`）。
#:   `usage` 目前**只有键位、没有写入方**（记账走注入的 `usage_sink`）——
#:   留着是因为它属于约定好的键空间；真去写它的时候必须带 TTL。
KEYS: Final[dict[str, str]] = {
    "gate": "{prefix}gate:session:{session_id}",
    "alert_seen": "{prefix}alert:seen:{fingerprint}",
    "alert_incident": "{prefix}alert:incident:{fingerprint}",
    "alert_quota": "{prefix}alert:quota:{window_start}",
    "usage": "{prefix}usage:{session_id}:{window}",
    "pending": "{prefix}pending:{key}",
    "result_response": "{prefix}result:response:{session_id}:{fingerprint}",
    "result_incident": "{prefix}result:incident:{incident_id}",
    "working_turns": "{session_prefix}{session_id}:turns",
    "working_state": "{session_prefix}{session_id}:state",
    "working_asked": "{session_prefix}{session_id}:asked",
}


class FakeRedis:
    """**忠实的假后端**（给单测用）：只实现 `RedisLike` 的 8 个原语。

    ★ 忠实在哪：① 过期按"单调时钟 + 毫秒"判定（不依赖真实时间流逝）；
      ② `set_nx_px` 与 `incr_by`、`compare_and_delete` 都是**原子**的
      （单线程语义，等价于 Redis 的单命令原子性）；
      ③ 键不存在时 `compare_and_delete` 返回 False（与 `GET`+`DEL` 的 Lua 版一致）。
    """

    def __init__(self, *, clock: Any = time.monotonic) -> None:
        self._data: dict[str, str] = {}
        self._expire_at: dict[str, float] = {}
        self._clock = clock
        #: 记录"有没有写过不带 TTL 的键" —— 判据用
        self.wrote_without_ttl: list[str] = []

    # ---- 内部 ----
    def _alive(self, key: str) -> bool:
        deadline = self._expire_at.get(key)
        if deadline is None:
            if key in self._data:
                self.wrote_without_ttl.append(key)
            return key in self._data
        if self._clock() >= deadline:
            self._data.pop(key, None)
            self._expire_at.pop(key, None)
            return False
        return True

    def _get(self, key: str) -> Optional[str]:
        return self._data.get(key) if self._alive(key) else None

    # ---- RedisLike ----
    async def set_nx_px(self, key: str, value: str, ttl_millis: int) -> bool:
        if self._alive(key):
            return False
        self._data[key] = value
        self._expire_at[key] = self._clock() + ttl_millis / 1000.0
        return True

    async def set_px(self, key: str, value: str, ttl_millis: int) -> None:
        self._data[key] = value
        self._expire_at[key] = self._clock() + ttl_millis / 1000.0

    async def get(self, key: str) -> Optional[str]:
        return self._get(key)

    async def compare_and_delete(self, key: str, expected: str) -> bool:
        if self._get(key) != expected:
            return False
        self._data.pop(key, None)
        self._expire_at.pop(key, None)
        return True

    async def incr_by(self, key: str, amount: int, ttl_seconds: int) -> int:
        current = int(self._get(key) or 0)
        value = current + amount
        self._data[key] = str(value)
        # ★ 只有"新建的键"才设 TTL（与 Redis 的 `INCR` + `EXPIRE NX` 口径一致：
        #   同一个窗口键被反复自增时，不能在每次自增后把窗口往后推）
        if key not in self._expire_at:
            self._expire_at[key] = self._clock() + max(1, ttl_seconds)
        return value

    async def touch(self, key: str, ttl_millis: int) -> bool:
        if not self._alive(key):
            return False
        self._expire_at[key] = self._clock() + ttl_millis / 1000.0
        return True

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._expire_at.pop(key, None)

    async def ping(self) -> bool:
        return True


class RedisBackend:
    """真后端：把 8 个原语翻译成 Redis 命令（CAS 用 Lua，保证原子）。

    ★ 连接是**懒建**的：`ping()` 之前不连 —— 这样"没有 Redis 的环境"里，
      只要调用方在 `ping()` 失败后降级，就不会在 import 期炸掉。
    """

    #: 只有持有者能释放锁（CAS 删除）—— 必须是**一段 Lua**，不能读-改-写两次往返
    _CAS_DELETE_LUA: Final[str] = (
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('del', KEYS[1]) else return 0 end"
    )
    _INCR_TTL_LUA: Final[str] = (
        "local v = redis.call('incrby', KEYS[1], ARGV[1]) "
        "if redis.call('ttl', KEYS[1]) < 0 then redis.call('expire', KEYS[1], ARGV[2]) end "
        "return v"
    )

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def from_url(cls, url: str, *, timeout_sec: float = 2.0) -> "RedisBackend":
        import redis.asyncio as aioredis  # noqa: PLC0415

        client = aioredis.from_url(url, socket_timeout=timeout_sec,
                                   socket_connect_timeout=timeout_sec,
                                   decode_responses=True)
        return cls(client)

    async def set_nx_px(self, key: str, value: str, ttl_millis: int) -> bool:
        return bool(await self._client.set(key, value, nx=True, px=ttl_millis))

    async def set_px(self, key: str, value: str, ttl_millis: int) -> None:
        await self._client.set(key, value, px=ttl_millis)

    async def get(self, key: str) -> Optional[str]:
        value = await self._client.get(key)
        return None if value is None else str(value)

    async def compare_and_delete(self, key: str, expected: str) -> bool:
        return bool(await self._client.eval(self._CAS_DELETE_LUA, 1, key, expected))

    async def incr_by(self, key: str, amount: int, ttl_seconds: int) -> int:
        return int(await self._client.eval(self._INCR_TTL_LUA, 1, key, amount,
                                           max(1, ttl_seconds)))

    async def touch(self, key: str, ttl_millis: int) -> bool:
        return bool(await self._client.pexpire(key, ttl_millis))

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception as exc:  # noqa: BLE001 —— 探测失败 = 不可用（调用方据此降级）
            raise StateUnavailable(f"Redis 不可达：{type(exc).__name__}: {exc}") from exc
