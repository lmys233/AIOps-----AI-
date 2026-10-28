"""四层记忆的**记录类型**（dataclass，不含任何 I/O）。

★ 本文件由原先的单文件 `stores.py`（903 行）拆分而来；
  **公开导入面一字未改**（包的 `__init__.py` 显式再导出）。
"""

from __future__ import annotations

from ..embedding import LEXICAL_EMBEDDER_NAME
from datetime import datetime
from typing import Any, Final, Optional

from dataclasses import dataclass, field
from ...common.enums import VERIFY_WEIGHTS, VerificationMethod
from ...common.contracts import MemoryHit
from .base import PROCEDURE_ID_PREFIX, SEMANTIC_ID_PREFIX, _now


# ======================================================================
#  记录形状
# ======================================================================
#: 答案落近层时的**有界**常量（会话记忆是 TTL 24h 的 Redis 列表，不能让它无限长）
TURN_ANSWER_SUMMARY_MAX: Final[int] = 600
TURN_ANSWER_MAX_ROWS: Final[int] = 16
TURN_ANSWER_VALUE_MAX: Final[int] = 300
#: ★★ 一轮里**调过哪些工具**也要落近层（用户 2026-10-03 指示："前端展示的 tool call 也要保存到会话
#:   记忆中，AI 可以看到，前端加载会话历史时也要恢复 —— 现在刷新后就消失了，这不对"）。
#: 有界：最多 24 次调用，每次只留「工具名 / 中文短名 / 参数摘要 / 结果摘要 / 成败」。
TURN_TOOLS_MAX: Final[int] = 24
TURN_TOOL_TEXT_MAX: Final[int] = 300
#: ★★ 一轮的**执行过程时间线**（刷新后恢复「推理过程」用）：最多 60 步，每步载荷 ≤ 400 字符。
#:   以前这些行只走 SSE → 刷新后"AI 是怎么跑的"整条没了（用户实测："我想要刷新后依旧保留
#:   AIOps 执行的过程"）。
TURN_STEPS_MAX: Final[int] = 60
TURN_STEP_TEXT_MAX: Final[int] = 400

@dataclass(frozen=True)
class EpisodicRecord:
    """一条情景记忆（历史故障的完整闭环）。"""

    memory_id: str
    symptom_text: str
    root_cause: str
    verification: VerificationMethod
    confidence: float = 0.0
    category: Optional[str] = None
    entity_id: Optional[str] = None
    entity_type: Optional[str] = None
    cluster: Optional[str] = None
    occurred_at: datetime = field(default_factory=_now)
    created_at: datetime = field(default_factory=_now)
    remediation: dict[str, Any] = field(default_factory=dict)
    review: Optional[str] = None
    hit_count: int = 0
    superseded_by: Optional[str] = None
    embedding: tuple[float, ...] = ()
    embedding_model: str = LEXICAL_EMBEDDER_NAME
    batch_id: Optional[str] = None
    tenant_id: str = "default"

    @property
    def verify_weight(self) -> float:
        return VERIFY_WEIGHTS[self.verification.value]

    def age_days(self, *, now: Optional[datetime] = None) -> float:
        delta = (now or _now()) - self.occurred_at
        return max(0.0, delta.total_seconds() / 86400.0)

    def to_hit(self, *, similarity: float, now: Optional[datetime] = None) -> MemoryHit:
        """转成契约对象 `MemoryHit`（注入上下文的**唯一**形状）。"""
        return MemoryHit(
            memory_id=self.memory_id, symptom_text=self.symptom_text,
            root_cause=self.root_cause, similarity=round(min(1.0, max(0.0, similarity)), 4),
            age_days=round(self.age_days(now=now), 2), verification=self.verification,
            hit_count=self.hit_count, superseded_by=self.superseded_by,
        )

@dataclass(frozen=True)
class ProcedureRecord:
    """一条程序性记忆（SOP 套路）。"""

    procedure_id: str
    name: str
    symptom_text: str
    steps: tuple[dict[str, Any], ...] = ()
    success_rate: float = 0.5
    used_count: int = 0
    updated_at: datetime = field(default_factory=_now)
    embedding: tuple[float, ...] = ()
    tenant_id: str = "default"

    def to_hit(self, *, similarity: float) -> MemoryHit:
        """SOP 也以 `MemoryHit` 形状返回：`symptom_text` 放"什么时候用它"，
        `root_cause` 放步骤摘要 —— 这样**注入路径只有一处**（不必为每层各写一套渲染）。

        ★★ `memory_id` **必须带 `PROCEDURE_ID_PREFIX`**：
          它同时是"这条命中属于哪一层"的**唯一判据** ——
          `MemoryService.feedback()` 靠它分流到 `procedural.feedback`（按成功率自我进化），
          `recall()` 靠它跳过 `episodic.bump_hit`。
          旧实现写的是裸 `procedure_id`，于是两处判断**永远不成立**：
          真实的 SOP 命中既不会自我进化，还会去打一张根本没有该 id 的表。

        ★ 加前缀要**幂等**：`procedure_id` 这一侧历史上既有 `"pool"` 也有 `"proc:pool"`
          两种写法（`upsert` 原样入库），无脑拼会得到 `"proc:proc:pool"` ——
          `feedback` 只剥一层前缀，于是"命中里的 id"和"库里的 id"再也对不上。
        """
        steps = "；".join(str(step.get("action") or step) for step in self.steps[:4])
        prefixed = (self.procedure_id if self.procedure_id.startswith(PROCEDURE_ID_PREFIX)
                    else f"{PROCEDURE_ID_PREFIX}{self.procedure_id}")
        return MemoryHit(
            memory_id=prefixed,
            symptom_text=f"[SOP] {self.name}：{self.symptom_text}",
            root_cause=f"[SOP 步骤] {steps}" if steps else "[SOP] （无步骤明细）",
            similarity=round(min(1.0, max(0.0, similarity)), 4),
            age_days=round(max(0.0, (_now() - self.updated_at).total_seconds() / 86400.0), 2),
            # SOP 的"验证方式"用 unverified 表示"这是经验而非已验证结论"（会让它排在有验证的案例之后）
            verification=VerificationMethod.UNVERIFIED,
            hit_count=self.used_count,
        )

@dataclass(frozen=True)
class SemanticFact:
    """一条语义记忆事实（拓扑/Owner/SLO）。"""

    subject: str                 # 例如 `service:order@compose-cn-1`
    statement: str               # 例如 "order 依赖 inventory、payment"
    source: str = "topology"
    similarity: float = 0.5

    def to_hit(self) -> MemoryHit:
        return MemoryHit(
            memory_id=f"{SEMANTIC_ID_PREFIX}{self.source}:{self.subject}",
            symptom_text=f"[语义记忆] {self.subject}",
            root_cause=self.statement,
            similarity=self.similarity,
            age_days=0.0,
            # 拓扑是"随 CMDB 同步"的结构化事实，可靠性高于任何一次故障结论
            verification=VerificationMethod.VERIFIED_METRICS,
            hit_count=0,
        )

@dataclass(frozen=True)
class ConversationTurn:
    """一轮对话（会话历史的最小单元）。

    ★★ `answer_*` 三件是**这一轮给用户看过的答案原文**（用户 2026-10-03 实测上报：
      "刷新后对话显示就变了……**回答原文为什么不给看**？"）。
      修前近层只存 `response_digest`（**截断到 200 字符**）→ 刷新后只剩一句摘要。
      近层是"原文"那一层，答案就是这一轮的正文，所以它必须落在这里（有界，见下面常量）。
    """

    turn: int
    user_message: str
    response_digest: str = ""
    service_names: tuple[str, ...] = ()
    at: str = ""
    #: 答案的**白话总结**（落库前按 `TURN_ANSWER_SUMMARY_MAX` 截断）
    answer_summary: str = ""
    #: 答案的**明细行**（`(label, value)`，最多 `TURN_ANSWER_MAX_ROWS` 行）
    answer_rows: tuple[tuple[str, str], ...] = ()
    #: 明细的展示形态（`table` / `json`）
    answer_kind: str = ""
    #: ★ 这一轮**调过的工具**（`(name, title, params, result, ok, call_id)`；有界）。
    #:   它同时服务三件事：前端刷新后恢复工具卡、下一轮的提示词里能看到"上一轮查过什么"、
    #:   以及**执行过程时间线**里那一步按 `call_id` 找到自己的内容。
    tools: tuple[tuple[str, str, str, str, bool, str], ...] = ()
    #: ★★ 这一轮的**执行过程时间线**（`(event, payload_json)`；有界）——刷新后要能把
    #:   「推理过程」原样恢复（工具卡靠 `call_id` 与 `tools` 对齐）。以前它只走 SSE。
    steps: tuple[tuple[str, str], ...] = ()
    answer_advisor_status: str = ""
    answer_snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"turn": self.turn, "user_message": self.user_message,
                "response_digest": self.response_digest,
                "service_names": list(self.service_names), "at": self.at,
                "answer_summary": self.answer_summary,
                "answer_rows": [list(row) for row in self.answer_rows],
                "answer_kind": self.answer_kind,
                "answer_advisor_status": self.answer_advisor_status,
                "answer_snapshot": self.answer_snapshot,
                "tools": [list(item) for item in self.tools],
                "steps": [list(item) for item in self.steps]}
