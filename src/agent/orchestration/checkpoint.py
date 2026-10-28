"""检查点 —— 让"中断后可恢复、进程崩溃不丢进度"。

--------------------------------------------------------------------------------
★ 恢复的是**进度**，不是"全部留痕"
--------------------------------------------------------------------------------

"进度"必须落盘：审批中断后能从 Checkpointer 恢复，**进程崩溃不丢进度**。

"进度"具体指：这次排查查到第几轮、攒了哪些证据与假设、预算花了多少、问过哪些缺口。
这些都能无损落盘（证据与假设本身是 Pydantic 契约 → `model_dump` 就是完整事实）。

**不能**无损落盘的是"解释性留痕"（路由 `RouteOutcome`、计划 `PlanOutcome`、
`AgentOutcome` 的中间态）。它们的处理是：**如实标记为"未运行/已丢失"**，
而不是塞一份看起来像真的摘要 —— 伪造留痕比没有留痕更坏（复盘时会指向不存在的调用）。

--------------------------------------------------------------------------------
★ 两种实现，各自有明确用途
--------------------------------------------------------------------------------

| 实现 | 用途 | 为什么需要它 |
|---|---|---|
| `InMemoryCheckpointer` | 单测 / 单进程内恢复 | 快、无外部依赖；但**进程一崩就没了** —— **不满足**"崩溃不丢进度" |
| `FileCheckpointer` | 默认（生产） | 写到磁盘 → 进程崩溃后新进程能读回；不引入 Redis/PG 依赖 |

★ 原子写：先写 `*.tmp` 再 `os.replace` —— 直接覆盖时，"崩在写一半"会留下半个 JSON，
下次恢复拿到的是**损坏的检查点**（比没有更危险：会当成"进度是空的"）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Optional, Protocol, runtime_checkable

__all__ = ["Checkpointer", "InMemoryCheckpointer", "FileCheckpointer",
           "DEFAULT_CHECKPOINT_DIR", "build_checkpointer"]

logger = logging.getLogger("agent.orchestration.checkpoint")

#: 默认落盘目录（与 SDK 的状态目录同一风格：`.run/` 下的运行期产物）
DEFAULT_CHECKPOINT_DIR: Final[str] = ".run/orchestrator-state"


@runtime_checkable
class Checkpointer(Protocol):
    """检查点读写（`run_id` 一般就是 `incident_id`）。"""

    async def save(self, run_id: str, payload: dict[str, Any]) -> None: ...

    async def load(self, run_id: str) -> Optional[dict[str, Any]]: ...

    async def clear(self, run_id: str) -> None: ...


@dataclass
class InMemoryCheckpointer:
    """进程内检查点（**单测用**；做不到"进程崩溃不丢进度"）。"""

    store: dict[str, dict[str, Any]] = field(default_factory=dict)
    writes: int = 0

    async def save(self, run_id: str, payload: dict[str, Any]) -> None:
        self.store[str(run_id)] = json.loads(json.dumps(payload, ensure_ascii=False,
                                                        default=str))
        self.writes += 1

    async def load(self, run_id: str) -> Optional[dict[str, Any]]:
        item = self.store.get(str(run_id))
        return json.loads(json.dumps(item, ensure_ascii=False)) if item else None

    async def clear(self, run_id: str) -> None:
        self.store.pop(str(run_id), None)


@dataclass
class FileCheckpointer:
    """磁盘检查点（默认）：一个 incident 一个 JSON 文件，**原子替换**。"""

    directory: Path = field(default_factory=lambda: Path(DEFAULT_CHECKPOINT_DIR))
    keep_sec: int = 24 * 3600                 # 与黑板/工作记忆同一档 TTL
    writes: int = 0

    def path_of(self, run_id: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(run_id))
        return Path(self.directory) / f"{safe or 'run'}.json"

    async def save(self, run_id: str, payload: dict[str, Any]) -> None:
        target = self.path_of(run_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(payload, ensure_ascii=False, default=str, indent=1)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(body, encoding="utf-8")
        # ★ 原子替换：崩在写一半也不会留下损坏的检查点
        os.replace(tmp, target)
        self.writes += 1

    async def load(self, run_id: str) -> Optional[dict[str, Any]]:
        target = self.path_of(run_id)
        if not target.exists():
            return None
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # ★ 损坏的检查点**不装作没有**：如实报"读不出来"，让上层决定要不要重跑
            logger.warning("检查点 %s 读不出来（%s: %s）—— 当作**不可恢复**处理",
                           target, type(exc).__name__, exc)
            return None
        if self.keep_sec > 0 and isinstance(payload, dict):
            saved_age = time.time() - target.stat().st_mtime
            if saved_age > self.keep_sec:
                logger.info("检查点 %s 已过期（%.0fs > %ds），不再使用",
                            target, saved_age, self.keep_sec)
                return None
        return payload

    async def clear(self, run_id: str) -> None:
        target = self.path_of(run_id)
        target.unlink(missing_ok=True)


def build_checkpointer(directory: Optional[str] = None) -> Checkpointer:
    """按目录建检查点（空字符串 → 内存实现；便于"关掉落盘"的部署）。"""
    text = (directory if directory is not None else DEFAULT_CHECKPOINT_DIR).strip()
    if not text:
        logger.info("检查点目录为空 → 用内存实现（**进程崩溃会丢进度**，spec §10.2 不满足）")
        return InMemoryCheckpointer()
    return FileCheckpointer(directory=Path(text))
