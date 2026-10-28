"""告警账本：指纹**幂等** / **抑制窗口** / **每窗口配额** 放共享存储。

★ 这三件事原先放在**单进程**的两个 dict + 一个 list 里，于是：
  · 进程一重启，同一条告警会**再起一次排查**（"记过了"这句话只活了进程的一生）；
  · 两个实例各记一份 → 配额形同虚设（N 个实例 = N 倍账单）。

★★ 分布式实现的**原子性靠"抢键"而不是靠"读-改-写"**：
  ① 幂等（同一指纹只有一个事件号）= `SET NX` 抢 `alert:incident:{fp}` —— 抢到的人铸号，
     抢不到的人**读回赢家的号**，于是"一个指纹一个事件号"在并发下也成立；
  ② 抑制窗口 = `alert:seen:{fp}` 带 TTL（窗口一到自动消失，"窗口过了就该重新触发"不用写代码）；
  ③ 配额 = `INCR` + TTL（原子自增，多实例求和一致）。
  ★ 已知边界（如实写在这里，不假装完美）：配额"先自增后判断"会让**被拒的那次也占一个额度**
    —— 方向是**更严**（fail-closed），不会放过超额请求。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import (Any, Callable, Final, Mapping, Optional, Protocol, Sequence,
                    runtime_checkable)

from .backend import KEYS, RedisLike, StateUnavailable, key_prefix, ttl_ms

__all__ = ["AlertDecision", "AlertLedger", "AlertQueueLike", "InProcessAlertLedger",
           "RedisAlertLedger", "new_incident_id"]

#: 幂等映射的 TTL（"同一 fingerprint 一个事件号"要活得比抑制窗口长得多）
IDENTITY_TTL_SEC: Final[float] = 24 * 3600.0


def new_incident_id() -> str:
    """铸一个事件号（与 `orchestration.new_incident_id` 同格式，避免两处漂移）。"""
    from ..orchestration import new_incident_id as _new  # noqa: PLC0415

    return _new()


def _as_float(raw: Optional[str]) -> Optional[float]:
    """把"上次触发时刻"解析成数字；解析不出来就当作**没有记录**（fail-open 到"重新触发"）。

    ★ 方向说明：解析失败时选择"重新触发"而不是"永远抑制" —— 抑制过头会让**真告警被吞**，
      而那比"多跑一次排查"严重得多（这里的第一要求是幂等，不是"少跑"）。
    """
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class AlertDecision:
    """一条告警的处置结论（与 API 层的 `alerts.AlertDecision` 同字段，避免两套口径）。

    ★ 为什么不直接 import 那个 dataclass：那是 `api` 层的对象，而状态层要被 api 依赖
      （方向不能反过来）。两边字段逐字一致，并有用例对账。
    """

    __slots__ = ("fingerprint", "incident_id", "accepted", "suppressed", "reason")

    def __init__(self, *, fingerprint: str, incident_id: str, accepted: bool,
                 suppressed: bool = False, reason: str = "") -> None:
        self.fingerprint = fingerprint
        self.incident_id = incident_id
        self.accepted = accepted
        self.suppressed = suppressed
        self.reason = reason

    def to_dict(self) -> dict[str, Any]:
        return {"fingerprint": self.fingerprint, "incident_id": self.incident_id,
                "accepted": self.accepted, "suppressed": self.suppressed,
                "reason": self.reason}


@runtime_checkable
class AlertLedger(Protocol):
    """告警账本接口（**异步**：分布式实现要走网络）。"""

    async def submit(self, alerts: Sequence[Mapping[str, Any]], *,
                     now: Optional[float] = None) -> list[Any]:
        """逐条给出处置结论（顺序与入参一致）。"""

    async def snapshot(self) -> dict[str, Any]:
        """排障用的自描述快照（有界性、配额用度都要看得见）。"""


class AlertQueueLike(Protocol):
    """进程内告警队列**需要的能力**（窄接口：只要这三个）。

    ★ 为什么单独写出来：账本只负责"按指纹判幂等/抑制/配额"这一件事，
      它不该知道那份实现住在哪一层、叫什么名字。`state` 只认这个形状，
      具体实现由装配点注入 —— 于是换一份队列实现，本模块一个字符都不用改。
    ★ 带数据成员（`notes`）所以**不加** `runtime_checkable`：那种协议做 `isinstance`
      会在运行期抛 `TypeError`，标上去反而像"可以当判据用"。
    """

    #: 最近若干条说明（"为什么被拒/被挤掉"，`snapshot` 之外的读法）
    notes: list[str]

    def submit(self, alerts: Sequence[Mapping[str, Any]], *,
               now: Optional[float] = None) -> Sequence[Any]:
        """逐条定幂等/抑制/配额（顺序与入参一致，**同步**）。"""

    def snapshot(self) -> dict[str, Any]:
        """自描述快照（有界性与配额用度都要看得见）。"""


class InProcessAlertLedger:
    """进程内账本：把一份进程内告警队列包成账本接口（`submit` 变异步）。

    ★ 队列**构造期注入**（见 `AlertQueueLike`）：本层不 import 服务层，
      所以"状态层能独立装配/测试"是成立的。
    ★ 这里**没有默认队列**：拿不到就如实报 `StateUnavailable`，
      而不是悄悄造一个空账本（那会让"告警一条都没进来"看起来像"没有告警"）。
      装配点 `build_state_bundle()` 负责把默认实现接上。
    """

    shared = False

    def __init__(self, queue: Optional[AlertQueueLike] = None, *,
                 queue_factory: Optional[Callable[..., AlertQueueLike]] = None,
                 **kwargs: Any) -> None:
        if queue is None and queue_factory is not None:
            queue = queue_factory(**kwargs)
        if queue is None:
            raise StateUnavailable(
                "进程内账本需要注入告警队列（`AlertQueueLike`：submit / snapshot / notes）"
                "—— 走 `build_state_bundle()` 装配即可拿到默认实现")
        self.queue = queue

    async def submit(self, alerts: Sequence[Mapping[str, Any]], *,
                     now: Optional[float] = None) -> list[Any]:
        return list(self.queue.submit(alerts, now=now))

    async def snapshot(self) -> dict[str, Any]:
        return dict(self.queue.snapshot())

    #: 既有用例直接读账本内部时用（与注入的队列同名同义）
    @property
    def notes(self) -> list[str]:
        return self.queue.notes


class RedisAlertLedger:
    """Redis 账本（多实例）：抢键铸号 + 窗口键 TTL + `INCR` 配额。"""

    shared = True

    def __init__(self, backend: RedisLike, *, tenant: str = "default",
                 window_sec: float, max_per_window: int,
                 identity_ttl_sec: float = IDENTITY_TTL_SEC) -> None:
        self._backend = backend
        self._prefix = key_prefix(tenant)
        self.window_sec = float(window_sec)
        self.max_per_window = int(max_per_window)
        self.identity_ttl_sec = float(identity_ttl_sec)
        self.notes: list[str] = []          # 只保留最近的说明（有界，见 `_note`）

    # ------------------------------------------------------------------
    def _key(self, kind: str, **kwargs: Any) -> str:
        return KEYS[kind].format(prefix=self._prefix, **kwargs)

    def _note(self, text: str, *, keep: int = 50) -> None:
        self.notes.append(text)
        del self.notes[:-keep]

    @staticmethod
    def _fingerprint(raw: Mapping[str, Any]) -> tuple[str, bool]:
        fingerprint = str(raw.get("fingerprint") or "").strip()
        if fingerprint:
            return fingerprint, False
        digest = hashlib.sha256(
            json.dumps(raw, sort_keys=True, ensure_ascii=False,
                       default=str).encode("utf-8")).hexdigest()[:12]
        return f"fp_auto_{digest}", True

    async def _quota_used(self, now: float, *, amount: int) -> int:
        """配额自增（原子）；窗口键按"窗口起点"分桶，避免滑动列表。"""
        window_start = int(now // max(1.0, self.window_sec))
        key = self._key("alert_quota", window_start=window_start)
        return await self._backend.incr_by(key, amount, int(self.window_sec * 2))

    async def submit(self, alerts: Sequence[Mapping[str, Any]], *,
                     now: Optional[float] = None) -> list[AlertDecision]:
        moment = time.monotonic() if now is None else float(now)
        out: list[AlertDecision] = []
        for raw in alerts:
            out.append(await self._decide_one(raw, moment))
        return out

    async def _decide_one(self, raw: Mapping[str, Any], moment: float) -> AlertDecision:
        """一条告警：**已有幂等键**走 `_with_known_identity`，否则走 `_claim_identity`。"""
        fingerprint, auto = self._fingerprint(raw)
        if auto:
            self._note(f"告警没有 fingerprint → 按内容哈希兜底 {fingerprint}")
        keys = self._keys(fingerprint)
        known = await self._backend.get(keys["identity"])
        if known is not None:
            return await self._with_known_identity(fingerprint, str(known), keys, moment)
        return await self._claim_identity(fingerprint, keys, moment)

    def _keys(self, fingerprint: str) -> dict[str, str]:
        return {"seen": self._key("alert_seen", fingerprint=fingerprint),
                "identity": self._key("alert_incident", fingerprint=fingerprint)}

    async def _with_known_identity(self, fingerprint: str, known: str,
                                   keys: Mapping[str, str], moment: float) -> AlertDecision:
        """已有幂等键：**在窗口内 → 抑制**；**窗口已过 → 重新触发但沿用同一事件号**。

        ★ 判据必须与进程内实现**逐字一致**：用"上次触发时刻"比较，而不是"看键还在不在"。
          为什么（实测踩到）：键的 TTL 走的是 **Redis 的真实时钟**，而 `now` 是调用方
          给的逻辑时刻（生产里同源，测试/回放不同源）→ 用"键在不在"当判据，同一段输入在
          两套实现上会给出**不同结论**，那就等于两套口径。TTL 在这里只承担**清理**。
        """
        last_at = _as_float(await self._backend.get(keys["seen"]))
        if last_at is not None and moment - last_at < self.window_sec:
            return AlertDecision(
                fingerprint=fingerprint, incident_id=known, accepted=False,
                suppressed=True,
                reason=(f"同一 fingerprint 在抑制窗口内（{moment - last_at:.0f}s < "
                        f"{self.window_sec:.0f}s）→ 不重复触发"))
        used = await self._quota_used(moment, amount=1)
        if used > self.max_per_window:
            return self._quota_denied(fingerprint, known)
        await self._mark_seen(keys, moment)
        return AlertDecision(
            fingerprint=fingerprint, incident_id=known, accepted=True,
            reason=("抑制窗口已过 → 重新触发（**沿用同一事件号**：同一 fingerprint "
                    "只有一个 incident）"))

    async def _claim_identity(self, fingerprint: str, keys: Mapping[str, str],
                              moment: float) -> AlertDecision:
        """首次接入：用 `SET NX` 决定"谁铸号"，抢不到就**沿用赢家的号**（并发下仍只有一个）。"""
        mine = new_incident_id()
        claimed = await self._backend.set_nx_px(keys["identity"], mine,
                                                ttl_ms(self.identity_ttl_sec))
        if not claimed:
            winner = await self._backend.get(keys["identity"])
            if winner is None:                      # 赢家刚过期（极窄窗口）→ 当作被抑制
                return AlertDecision(fingerprint=fingerprint, incident_id=mine,
                                     accepted=False, suppressed=True,
                                     reason="并发抢键时赢家的键恰好过期 → 本次不触发（重试即可）")
            return AlertDecision(
                fingerprint=fingerprint, incident_id=str(winner), accepted=False,
                suppressed=True,
                reason=("另一实例先抢到同一 fingerprint 的幂等键 → 本次不重复触发"
                        "（并发下「一个指纹一个事件号」仍成立）"))
        used = await self._quota_used(moment, amount=1)
        if used > self.max_per_window:
            await self._mark_seen(keys, moment)
            return self._quota_denied(fingerprint, mine)
        await self._mark_seen(keys, moment)
        return AlertDecision(fingerprint=fingerprint, incident_id=mine, accepted=True,
                             reason="首次接入 → 入队")

    async def _mark_seen(self, keys: Mapping[str, str], moment: float) -> None:
        """记下"这次真跑的时刻"（TTL = 抑制窗口：过期即自动清理，判据另算）。"""
        await self._backend.set_px(keys["seen"], str(moment), ttl_ms(self.window_sec))

    def _quota_denied(self, fingerprint: str, incident_id: str) -> AlertDecision:
        self._note(f"全局配额用尽（{self.max_per_window}/{self.window_sec:.0f}s）"
                   f"→ 拒绝入队 {fingerprint}")
        return AlertDecision(
            fingerprint=fingerprint, incident_id=incident_id, accepted=False,
            suppressed=False,
            reason=(f"全局吞吐配额已用尽（{self.max_per_window} 次 / "
                    f"{self.window_sec:.0f}s）→ 本次不入队，请稍后重试或提高配额"
                    f"（AIOPS_ALERT_MAX_PER_WINDOW）"))

    async def snapshot(self) -> dict[str, Any]:
        return {"window_sec": self.window_sec, "max_per_window": self.max_per_window,
                "shared": True, "notes": list(self.notes[-5:]),
                "storage": "redis（幂等/抑制/配额三件事都带 TTL）"}
