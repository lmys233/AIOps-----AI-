"""登记待批的高危动作：`request_id` 必须存在且未过期，登记表有界。"""

from __future__ import annotations

from ..common.enums import APPROVAL_TTL_MINUTES
from typing import Any, Final, Optional
from dataclasses import dataclass, field
import time


#: 审批请求的有效期（分钟）—— 由分钟常量换算，不在两处各写一个数
APPROVAL_TTL_SEC: Final[float] = APPROVAL_TTL_MINUTES * 60.0

#: 待批登记表上限（只增不减的字典 = 无界增长）。
#: ★ 取 256：远大于"同时在等人批的动作数"，又给内存一个硬上界。
APPROVAL_MAX_PENDING: Final[int] = 256

# ======================================================================
#  审批登记（request_id 必须存在且未过期；action_hash 绑定）
# ======================================================================
@dataclass
class PendingApproval:
    """一条**待批**的高危动作（只在内存里活着，TTL 15 分钟）。"""

    request_id: str
    incident_id: str
    session_id: str
    action_hash: str
    risk_level: str
    action_class: str
    target: str
    diff_preview: str = ""
    created_at: float = field(default_factory=time.monotonic)

    def expires_in(self) -> float:
        return max(0.0, APPROVAL_TTL_SEC - (time.monotonic() - self.created_at))

    def expired(self) -> bool:
        return self.expires_in() <= 0

    def to_dict(self) -> dict[str, Any]:
        return {"request_id": self.request_id, "incident_id": self.incident_id,
                "action_hash": self.action_hash, "risk_level": self.risk_level,
                "action_class": self.action_class, "target": self.target,
                "diff_preview": self.diff_preview,
                "expires_in_sec": round(self.expires_in(), 1)}

@dataclass
class ApprovalRegistry:
    """待批动作的登记处（`request_id → PendingApproval`）。

    ★★ 以前只有 `.get()` 会顺手回收过期项 —— **从来没被续跑的审批条目
      会永久留在内存里**（一个只会被写、几乎不会被读的字典 = 无界增长）。
      现在 `register()` 先做一次全量清扫，再按 `APPROVAL_MAX_PENDING` 封顶，
      超出时**淘汰最老的**并如实返回（调用方可以据此说"有审批被挤掉了"）。
    """

    _pending: dict[str, PendingApproval] = field(default_factory=dict)
    #: ★ 登记上限（内存与"待批列表"都要有界）。超出 → 淘汰最老的条目。
    max_pending: int = APPROVAL_MAX_PENDING
    #: ★★ 被**容量淘汰**的 request_id → 淘汰时刻（以前是只追加的 list，
    #:   本身又是一个无界容器；现在是有界 FIFO）。
    evicted: dict[str, float] = field(default_factory=dict)
    #: 淘汰记录的保留条数（够解释"最近为什么被挤掉"即可）
    max_evicted: int = APPROVAL_MAX_PENDING

    def _remember_evicted(self, request_id: str) -> None:
        """记下"这条是被挤掉的"（★ 与"过期"分开：见 `was_evicted`）。"""
        self.evicted[str(request_id)] = time.monotonic()
        while len(self.evicted) > self.max_evicted:
            oldest = min(self.evicted.items(), key=lambda item: item[1])[0]
            self.evicted.pop(oldest, None)

    def was_evicted(self, request_id: str) -> bool:
        """这条 `request_id` 是**因为容量被淘汰**，而不是过期/从未存在。"""
        return str(request_id) in self.evicted

    def _sweep(self) -> None:
        """回收**所有**过期条目（不只是被查询到的那一条），再按上限淘汰最老的。

        ★ 以前写 `while len(...) >= max_pending` —— `max_pending=0` 时
          会在空字典上 `min()` → `ValueError`。现在用 `while self._pending and ...`：
          上限 0 的语义变成"一条都不留"（淘汰干净后自然停下）。
        """
        for request_id in [key for key, item in self._pending.items() if item.expired()]:
            self._pending.pop(request_id, None)
        while self._pending and len(self._pending) >= self.max_pending:
            oldest = min(self._pending.values(), key=lambda item: item.created_at)
            self._pending.pop(oldest.request_id, None)
            self._remember_evicted(oldest.request_id)

    def register(self, approval: PendingApproval) -> PendingApproval:
        self._sweep()
        self._pending[approval.request_id] = approval
        return approval

    def get(self, request_id: str) -> Optional[PendingApproval]:
        item = self._pending.get(str(request_id))
        if item is not None and item.expired():
            self._pending.pop(str(request_id), None)      # 过期即回收
            return item
        return item

    def clear(self, request_id: str) -> None:
        self._pending.pop(str(request_id), None)

    def snapshot(self) -> dict[str, Any]:
        self._sweep()
        return {"pending": [item.to_dict() for item in self._pending.values()],
                "max_pending": self.max_pending,
                # ★ 淘汰记录本身也是**有界**的
                "evicted_count": len(self.evicted),
                "evicted_recent": list(self.evicted)[-5:]}
