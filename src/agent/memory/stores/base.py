"""共享异常/常量与两个纯函数（`MemoryUnavailable` / 命中层判据 / id 前缀）。

★ 本包由原先的单文件 `stores.py`（903 行）拆分而来；
  **公开导入面一字未改**（包的 `__init__.py` 显式再导出）。
"""

from __future__ import annotations

from typing import Any, Final, Sequence

from datetime import datetime, timezone
import dataclasses
from difflib import SequenceMatcher
import logging


class MemoryUnavailable(Exception):
    """记忆存储**不可用**（连不上 / 超时）—— 与"查了没命中"严格区分。"""

    def __init__(self, layer: str, message: str = "") -> None:
        super().__init__(message or f"{layer} 不可用")
        self.layer = layer
        self.message = message or f"{layer} 不可用"

logger = logging.getLogger("agent.memory.stores")

#: 工作记忆是"会话级 TTL 24h"
WORKING_TTL_SEC: Final[int] = 24 * 3600

SESSION_KEY_PREFIX: Final[str] = "aiops:sess:"

#: ★★ "这条 `MemoryHit` 属于哪一层"的**唯一判据**：
#: 非情景记忆的两层在 `memory_id` 上带前缀，`MemoryService` 据此
#: 决定"要不要 bump 情景记忆的 hit_count"与"要不要走程序性反馈"。
#: 前缀以前是各写各的字符串字面量，于是**写的一端漏了前缀、读的一端就永久判错**
#: （SOP 的"按成功率自我进化"因此从来没生效过）。现在只有这一处定义。
PROCEDURE_ID_PREFIX: Final[str] = "proc:"

SEMANTIC_ID_PREFIX: Final[str] = "semantic:"

def hit_layer(memory_id: str) -> str:
    """`MemoryHit.memory_id` → 它属于哪一层（`episodic` / `procedure` / `semantic`）。"""
    text = str(memory_id or "")
    if text.startswith(PROCEDURE_ID_PREFIX):
        return "procedure"
    if text.startswith(SEMANTIC_ID_PREFIX):
        return "semantic"
    return "episodic"

def bare_procedure(record: "ProcedureRecord") -> "ProcedureRecord":
    """把 `ProcedureRecord.procedure_id` 归一成**裸 id**（不带 `proc:` 前缀）。

    ★ 为什么要有这一步：`procedure_id` 这一侧历史上存在
      两种写法 —— 命令行的 `memory-check` 用裸名（`proc_check_xxx`），
      单元测试用带前缀的 `proc:pool`。而"前缀"是**命中层**的判据
      （`hit_layer` / `service.feedback` 只剥一层），库里却可能存着带前缀的键 ——
      于是 `feedback("proc:x")` 剥成 `x`、在库里按 `proc:x` 找不到，**静默 no-op**：
      SOP 的成功率永远不动。
    ★ 取值只有一种：库里存**裸 id**，`memory_id` 才带前缀。
      带前缀进来时**归一 + 记日志**（不抛异常：库里可能还有历史行）。
    """
    text = str(record.procedure_id or "")
    if not text.startswith(PROCEDURE_ID_PREFIX):
        return record
    logger.warning("程序的 procedure_id 带了命中层前缀（%s）—— 已归一为裸 id；"
                   "前缀只属于 `MemoryHit.memory_id`", text)
    return dataclasses.replace(record, procedure_id=text[len(PROCEDURE_ID_PREFIX):])

def _now() -> datetime:
    return datetime.now(timezone.utc)

def _as_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return _now()
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return _now()


def _keyword_score(query: str, text: str) -> float:
    """关键词路的**假实现**打分：`difflib` 比值。

    ★ 真实现是 Postgres 的 `pg_trgm`；这里只是让离线单测能跑，
      所以刻意选一个**确定性、无依赖**的算法，而不是"假装"成 trgm。
    """
    return SequenceMatcher(None, query.lower(), str(text).lower()).ratio()


def _vec_literal(embedding: Sequence[float]) -> str:
    """pgvector 的文本字面量：`[0.1,0.2,...]`（配合 `$n::vector` 使用）。"""
    return "[" + ",".join(f"{value:.6f}" for value in embedding) + "]"
