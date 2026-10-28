"""共享状态：把"只活在单进程里"的状态抽成**可替换实现**。

硬性要求：**状态不得只活在单进程里** —— 接口与两套实现、Redis 键空间
（TTL / 原子性 / CAS 释放 / 降级）都在本包内落地。

| 状态 | 接口 | 进程内实现（默认） | 分布式实现 |
|---|---|---|---|
| 会话闸门 | `InflightGate` | `InProcessInflightGate` | `RedisInflightGate` |
| 告警账本（幂等/抑制/配额） | `AlertLedger` | `InProcessAlertLedger` | `RedisAlertLedger` |
| 用量记账 | `UsageMeter` | `InProcessUsageMeter` | `RedisUsageMeter` |
| 待确认动作 | `PendingActionStore` | `InProcessPendingActionStore` | `RedisPendingActionStore` |

★ 本模块只提供"状态放哪"的能力；**红线一条不动**（谁能写、写要不要审批、结论要不要证据
仍由意图层 / 工具层 / 审批闸门决定）。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .backend import (KEYS, FakeRedis, RedisBackend, RedisLike, StateUnavailable,
                      decode_json, decode_json_object, encode_json, key_prefix, ttl_ms)
from .gate import (DEFAULT_GATE_TTL_SEC, GateHandle, InflightGate,
                   InProcessInflightGate, RedisInflightGate)
from .ledger import (AlertDecision, AlertLedger, AlertQueueLike, InProcessAlertLedger,
                     RedisAlertLedger)
from .pending import (PENDING_TTL_SEC, InProcessPendingActionStore,
                      PendingActionStore, RedisPendingActionStore)
from .results import (RESULT_TTL_SEC, InProcessResultStore, RedisResultStore,
                      ResultStore)

__all__ = [
    "KEYS", "FakeRedis", "RedisBackend", "RedisLike", "StateUnavailable",
    "key_prefix", "ttl_ms", "encode_json", "decode_json", "decode_json_object",
    "DEFAULT_GATE_TTL_SEC", "GateHandle", "InflightGate",
    "InProcessInflightGate", "RedisInflightGate",
    "AlertDecision", "AlertLedger", "AlertQueueLike", "InProcessAlertLedger",
    "RedisAlertLedger",
    "PENDING_TTL_SEC", "PendingActionStore", "InProcessPendingActionStore",
    "RedisPendingActionStore",
    "RESULT_TTL_SEC", "ResultStore", "InProcessResultStore", "RedisResultStore",
    "StateBundle", "build_state_bundle", "build_state_from_url_sync",
]


class StateBundle:
    """四件共享状态的装配结果（**一份**真相：谁在管闸门/账本/结果/待确认动作）。"""

    __slots__ = ("gate", "ledger", "results", "pending", "shared", "note")

    def __init__(self, *, gate: object, ledger: object, results: object,
                 pending: object, shared: bool, note: str = "") -> None:
        self.gate = gate
        self.ledger = ledger
        self.results = results
        self.pending = pending
        self.shared = shared
        self.note = note

    def describe(self) -> dict[str, object]:
        return {"shared": self.shared, "note": self.note,
                "gate": type(self.gate).__name__, "ledger": type(self.ledger).__name__,
                "results": type(self.results).__name__,
                "pending": type(self.pending).__name__}


def _local_alert_queue(factory: Optional[Callable[[], Any]] = None) -> Any:
    """进程内告警队列的**默认实现**（装配点用；注入的工厂优先）。

    ★ 这里是 `state` 与 api 层之间**唯一**的接触点，且只在"没注入队列"时用到：
      账本本身只认 `AlertQueueLike`（见 `ledger.py`），拿到的队列换一份实现，
      状态层一行都不用改。要彻底去掉这条默认依赖，得让服务层反过来复用
      状态层自己的队列实现（那要动 api/，按改动边界只登记、不改）。
    """
    if factory is not None:
        return factory()
    from ..api.alerts import AlertQueue  # noqa: PLC0415 —— 复用既有实现，不重写第二份

    return AlertQueue()


def build_state_bundle(*, backend: Optional[RedisLike] = None, tenant: str = "default",
                       cache_max: int = 2000, window_sec: float = 300.0,
                       max_per_window: int = 60,
                       alert_queue_factory: Optional[Callable[[], Any]] = None
                       ) -> StateBundle:
    """装配闸门 / 账本 / 结果留存 / 待确认动作四件共享状态。

    · `backend=None` → 四件都用进程内实现，`shared=False`，`note` 写明"单进程语义"；
    · `backend` 可用  → 四件都用 Redis 实现，`shared=True`，`note=""`；
    · `alert_queue_factory` → 进程内账本用的告警队列（不注入就用默认实现）。

    本函数**不做可用性探测、不抛异常**：探测在 `build_state_from_url_sync` 里做，
    失败的结果是一条 `note` —— 调用方**必须**把它原样带进日志/响应（降级要响）。
    """
    from .ledger import InProcessAlertLedger as _LocalLedger
    from .pending import InProcessPendingActionStore as _LocalPending
    from .results import InProcessResultStore as _LocalResults

    if backend is None:
        return StateBundle(
            gate=InProcessInflightGate(),
            ledger=_LocalLedger(_local_alert_queue(alert_queue_factory)),
            results=_LocalResults(cache_max=cache_max), pending=_LocalPending(),
            shared=False,
            note=("共享状态未配置（AIOPS_STATE_REDIS_URL 未设）→ **单进程语义**："
                  "重启会丢幂等/抑制/待确认动作，多实例各记一份"))
    return StateBundle(
        gate=RedisInflightGate(backend, tenant=tenant),
        ledger=RedisAlertLedger(backend, tenant=tenant, window_sec=window_sec,
                                max_per_window=max_per_window),
        results=RedisResultStore(backend, tenant=tenant),
        pending=RedisPendingActionStore(backend, tenant=tenant),
        shared=True, note="")


def build_state_from_url_sync(url: str, *, timeout_sec: float = 1.0,
                              **kwargs: object) -> StateBundle:
    """同步版本（给 `create_app()` 这种**同步**装配点用）。

    ★ 为什么允许一次同步探测：它只在**启动时**发生一次（不是每请求），
      用同步客户端探一下比"起来之后每请求才失败"好得多 —— 后者会让
      "共享状态不可用"变成请求期的 500，而要求的是**降级 + 说明**。
    ★ 探测失败 → 进程内实现 + note（调用方把它写进日志与响应）。
    """
    if not url:
        return build_state_bundle(**kwargs)          # type: ignore[arg-type]
    client=None
    try:
        import redis as _sync_redis  # noqa: PLC0415

        client = _sync_redis.Redis.from_url(url, socket_timeout=timeout_sec,
                                            socket_connect_timeout=timeout_sec)
        client.ping()
    except Exception as exc:  # noqa: BLE001
        bundle = build_state_bundle(**kwargs)        # type: ignore[arg-type]
        bundle.note = (f"共享状态不可用（{type(exc).__name__}: {exc}）→ **已退回进程内实现**："
                       f"单进程语义（重启会丢幂等/抑制/待确认动作）")
        return bundle
    finally:
        if client is not None:client.close()
    return build_state_bundle(backend=RedisBackend.from_url(url), **kwargs)  # type: ignore[arg-type]
