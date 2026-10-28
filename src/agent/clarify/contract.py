"""反问（澄清）体系的契约。

★ 五条设计理念（逐条对应代码）：
  ① **插件式进入**：任何环节都可以交一个 `ClarificationRequest`（"我缺什么"），
     而不是各自现写问句；
  ② **解耦**：提问方不关心最终问什么、答案怎么回填 —— 那是插件的事；
  ③ **统一出口**：所有反问都从 `clarification` 出去、从 `/v1/chat/resume` 回来；
  ④ **可靠（高频路径）**：问句来自**模板**（不依赖模型可用）；预算有终点；
  ⑤ **可扩展**：新增场景 = 新增一个插件（不动仲裁器、不动 resume 接线）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

__all__ = ["PATCHABLE_GAPS", "CLARIFY_BUDGET", "SlotPatch", "ClarificationRequest",
           "AskContext", "ClarificationPlugin"]

#: 同一次排查**最多问几个问题**。★ 只此一处：仲裁器默认值、路由入口默认值都引用它，
#:   免得"上限"在几个文件里各写一个数字，改一处漏两处。
CLARIFY_BUDGET: int = 3

#: 允许被"澄清答案"回填的槽位白名单。
#: ★ 为什么不放开：`/v1/chat/resume` 的澄清分支**不许夹带写动作**（写动作只能走审批，
#:   只能走审批）—— 白名单是这条红线的机械实现。
#: ★ 补 `remediation`：写意图**没认出具体动作**时，缺的是"用户在候选方案里选哪一条"
#:   （台账里如实写成"在等：选方案"，而不是"在等 action 槽位"）。
PATCHABLE_GAPS = ("env", "cluster", "service", "time_window", "intent", "reference",
                  "confirm", "remediation", "tool_failure", "sdk_unavailable",
                  "missing_target")


@dataclass(frozen=True)
class SlotPatch:
    """答案怎么回填：槽位名 → 值。"""

    values: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {str(key): value for key, value in self.values.items()}


@dataclass(frozen=True)
class ClarificationRequest:
    """任何环节提出来的"我想问一句"。

    ★ `fallback` 是**必填**：不写清"不问的话我打算怎么兜底"，就会出现
      "为了问而问"。
    """

    gap: str
    why: str
    evidence: tuple[str, ...] = ()
    fallback: str = ""
    #: ★★ 这条缺口**值得升级成反问**（即使既有判据说"不用问"）。
    #:   ★ 为什么要显式声明而不是放宽全局口径：`_promote` 的默认规则是"只升级 env"，
    #:     因为放宽会让"帮我写首诗"也变成追问（实测）。这里改成
    #:     **谁提出缺口谁声明"这条必须问"** —— 口径收窄到具体场景，而不是整体放宽。
    promote: bool = False

    def __post_init__(self) -> None:
        if self.gap not in PATCHABLE_GAPS:
            raise ValueError(f"gap {self.gap!r} 不在可澄清白名单 {PATCHABLE_GAPS} 里")
        if not str(self.fallback).strip():
            raise ValueError(f"gap={self.gap} 必须写清 fallback（不问的话怎么兜底）")


@dataclass(frozen=True)
class AskContext:
    """仲裁与出题需要知道的**事实**（都是可核对的，不含猜测）。

    ★ 只放**真被读**的字段：这里每多一个"只写不读"的字段，读者就会以为仲裁看了它。
    """

    intent: str = ""
    unresolved: tuple[str, ...] = ()
    service_options: tuple[str, ...] = ()
    env_options: tuple[str, ...] = ()
    asked_gaps: tuple[str, ...] = ()
    budget_left: int = CLARIFY_BUDGET
    no_clarify: bool = False
    #: ★ 用户原话 —— 出题时**看语境**用（例如"泛化的健康询问"要用启发式的措辞，
    #:   而不是干巴巴一句"你想查哪个服务？"）。
    message: str = ""
    #: ★ 失败类插件靠它选手册小节（例如 `TOOL_UNAVAILABLE` → 「端口被占」那节）
    error_code: str = ""


@runtime_checkable
class ClarificationPlugin(Protocol):
    """一个场景一个插件（见 `plugins.py` 的内置清单）。"""

    id: str
    applies_to: tuple[str, ...]
    priority: int

    def question(self, request: ClarificationRequest, ctx: AskContext) -> Any:
        """给出可展示的问句（含选项；不带选项的问句在下游无法渲染）。"""

    def apply(self, answer: Any) -> SlotPatch:
        """把用户的答案变成槽位回填（**只回填白名单里的槽位**）。"""
