"""结果留存：**重复判定登记** 与 **事故查询** 放共享存储。

★ 为什么这两件事属于"共享状态"：契约承诺"同一轮（`turn_id` 相同，或抖动窗口内同内容）
  **判为重复并直接拒绝**"、`GET /v1/incidents/{id}` 能查回结果 —— 而原先它们放在**进程内的两个 dict** 里：
  进程一重启，"重复判定"就失效（同一轮再跑一次、再计一次费），"查事故"直接 404。
  在多实例下更糟：客户端重试打到另一台，等于把同一件事做两遍。

★ 两套实现的分工：
  · `InProcessResultStore`：**逐字复刻**既有 dict + 插入序 + 容量上限（LRU 式淘汰）；
  · `RedisResultStore`：JSON 序列化 + **键级 TTL**（不做容量淘汰 —— TTL 就是它的上界，
    这一点必须写在快照里，避免读者以为"有 LRU"）。
"""

from __future__ import annotations

from typing import Any, Final, Optional, Protocol, runtime_checkable

from .backend import (KEYS, RedisLike, decode_json_object, encode_json, key_prefix,
                      ttl_ms)

__all__ = ["ResultStore", "InProcessResultStore", "RedisResultStore",
           "RESULT_TTL_SEC"]

#: 结果留存的 TTL（比会话 TTL 长：客户端可能隔一会儿才重试）
RESULT_TTL_SEC: Final[float] = 24 * 3600.0


@runtime_checkable
class ResultStore(Protocol):
    """重复判定登记 + 事故查询（异步：分布式实现要走网络）。"""

    async def get_response(self, session_id: str, fingerprint: str) -> Optional[dict[str, Any]]:
        """取一条判定登记（`{"claimed_at_ms", "turn_id"}`）；没登记过 → `None`。"""

    async def put_response(self, session_id: str, fingerprint: str,
                           body: dict[str, Any]) -> None:
        """写一条判定登记（**覆盖**同键：同一轮重复登记按最新一次算）。"""

    async def get_incident(self, incident_id: str) -> Optional[dict[str, Any]]:
        """按 `incident_id` 取回结果（供事故查询端点用）。"""

    async def put_incident(self, incident_id: str, body: dict[str, Any]) -> None:
        """留存结果供 `GET /v1/incidents/{id}` 查询。"""

    async def list_incidents(self, *, limit: int) -> list[str]:
        """最近的事故号（新 → 旧）。"""


class InProcessResultStore:
    """进程内实现：与既有 `_responses` / `_incidents` / `_order` **同语义**。"""

    shared = False

    def __init__(self, *, cache_max: int) -> None:
        self.cache_max = int(cache_max)
        self.responses: dict[tuple[str, str], dict[str, Any]] = {}
        self.order: list[tuple[str, str]] = []
        self.incidents: dict[str, dict[str, Any]] = {}
        self.incident_order: list[str] = []

    async def get_response(self, session_id: str,
                           fingerprint: str) -> Optional[dict[str, Any]]:
        return self.responses.get((session_id, fingerprint))

    async def put_response(self, session_id: str, fingerprint: str,
                           body: dict[str, Any]) -> None:
        key = (session_id, fingerprint)
        if key not in self.responses:
            self.order.append(key)
        self.responses[key] = dict(body)
        while len(self.responses) > self.cache_max:
            oldest = self.order.pop(0)
            self.responses.pop(oldest, None)

    async def get_incident(self, incident_id: str) -> Optional[dict[str, Any]]:
        return self.incidents.get(incident_id)

    async def put_incident(self, incident_id: str, body: dict[str, Any]) -> None:
        if incident_id not in self.incidents:
            self.incident_order.append(incident_id)
        self.incidents[incident_id] = dict(body)

    async def list_incidents(self, *, limit: int) -> list[str]:
        return list(self.incident_order[-limit:])[::-1]


class RedisResultStore:
    """Redis 实现：JSON + TTL（**没有 LRU**：TTL 就是上界，如实写在快照里）。"""

    shared = True

    def __init__(self, backend: RedisLike, *, tenant: str = "default",
                 ttl_sec: float = RESULT_TTL_SEC) -> None:
        self._backend = backend
        self._prefix = key_prefix(tenant)
        self.ttl_sec = float(ttl_sec)
        self.notes: list[str] = []

    def _response_key(self, session_id: str, fingerprint: str) -> str:
        return KEYS["result_response"].format(prefix=self._prefix, session_id=session_id,
                                             fingerprint=fingerprint)

    def _incident_key(self, incident_id: str) -> str:
        return KEYS["result_incident"].format(prefix=self._prefix,
                                              incident_id=incident_id)

    async def get_response(self, session_id: str,
                           fingerprint: str) -> Optional[dict[str, Any]]:
        raw = await self._backend.get(self._response_key(session_id, fingerprint))
        return decode_json_object(raw)

    async def put_response(self, session_id: str, fingerprint: str,
                           body: dict[str, Any]) -> None:
        await self._backend.set_px(self._response_key(session_id, fingerprint),
                                   encode_json(body), ttl_ms(self.ttl_sec))

    async def get_incident(self, incident_id: str) -> Optional[dict[str, Any]]:
        raw = await self._backend.get(self._incident_key(incident_id))
        return decode_json_object(raw)

    async def put_incident(self, incident_id: str, body: dict[str, Any]) -> None:
        await self._backend.set_px(self._incident_key(incident_id),
                                   encode_json(body), ttl_ms(self.ttl_sec))

    async def list_incidents(self, *, limit: int) -> list[str]:
        """★ Redis 实现**不维护"最近事故号"索引**：那需要一个有序集合（多一个键空间）。

        调用方（`list_incidents` 端点）要的是"最近若干条"，而**进程内实现**才有插入序。
        取舍：多实例下按 `incident_id` 前缀 + `SCAN` 代价高，且与"单实例演示"的语义不同 →
        这里如实返回空列表并留一条 note（**不假装有**），端点侧对空列表的处理与"还没查过"一致。
        """
        self.notes.append("RedisResultStore 不维护最近事故号索引 → list_incidents 返回空；"
                          "请按 incident_id 直接查询（GET /v1/incidents/{id}）")
        del self.notes[:-5]
        return []
