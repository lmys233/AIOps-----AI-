"""黑板 —— Agent 之间**唯一**的共享状态（也是**证据的唯一落点**）。

★ 只导出存储实现与它的事件类型；黑板**不是**一个可以随便塞东西的 dict，
  它的每个操作都带权限与语义（`publish` 幂等、`read_evidence` 限角色）。
"""

from __future__ import annotations

from .store import (
    BOARD_TTL_SEC,
    INCIDENT_KEY_PREFIX,
    READ_ALL_ROLES,
    BoardPermissionError,
    BoardStore,
    InMemoryBoardStore,
    PublishResult,
    RedisBoardStore,
    new_evidence_id,
)

__all__ = [
    "BOARD_TTL_SEC",
    "INCIDENT_KEY_PREFIX",
    "READ_ALL_ROLES",
    "BoardPermissionError",
    "BoardStore",
    "InMemoryBoardStore",
    "PublishResult",
    "RedisBoardStore",
    "new_evidence_id",
]
