"""处置建议（**候选方案**）—— 先给用户可选方案，再把选中的方案细化成流程。

★ 为什么单独一层：写意图以前只能给**一个**方案（预案必须绑定单一的 `action_class`），
  推不出动作就反问"你要执行的是哪个动作" —— 用户要的是**方案**，却把选题推回给他。
  这一层的职责：把"能建议哪些动作"收在白名单里，一次给出若干候选（含人工方案），
  再由用户选择。

★ 这里**没有任何 IO**：纯函数、纯数据。执行端（写通道）不在本层，本阶段一行写操作都没有。
"""

from __future__ import annotations

from .plans import (
    PLAN_STEPS,
    PlanKind,
    RemediationPlan,
    candidate_plans,
    format_plans,
    select_plan,
)

__all__ = [
    "PLAN_STEPS",
    "PlanKind",
    "RemediationPlan",
    "candidate_plans",
    "format_plans",
    "select_plan",
]
