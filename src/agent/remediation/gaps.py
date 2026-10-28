"""方案的**锚定**与**零证据时的占位**（从 `plans.py` 抽出来，判据一个字没改）。

★ 两条判据都属于"这条方案能不能给用户看"，而 `plans.py` 管的是"有哪些候选"：
  · `anchored_to`：候选里**哪几条**真的冲着用户点名的那件事；
  · `evidence_gap_plan`：**一条都不该给**时唯一能说的实话 —— 先取证。

★★ 为什么"零证据"要单列一类（`kind=evidence_gap`）而不是 `manual`：
  用户问「怎么解决 X」时，手里没有任何 X 的证据却给出一条"看起来可行"的动作，
  等于让人照着一个没查过的现场动手（真机实测：问 order 线程池、答 inventory 的 SQL）。
"""

from __future__ import annotations

from typing import Any, Sequence

from ..common.enums import RiskLevel
from .plans import PlanKind, RemediationPlan, _make_plan

__all__ = ["anchored_to", "evidence_gap_plan"]


def evidence_gap_plan(service_names: Sequence[Any] = ()) -> RemediationPlan:
    """**零证据**时唯一该给的一条：先补齐证据（**不是**处置方案）。"""
    target = "、".join(str(name) for name in (service_names or ()) if str(name).strip())
    where = f"**{target}**" if target else "**用户点名的那件事**"
    return _make_plan(
        option_id="1", kind=PlanKind.EVIDENCE_GAP.value,
        title=f"先取证再定处置（本轮还没有 {target or '点名服务'} 的证据）",
        action_class="",
        rationale=(f"依据：本轮没有冲着 {where} 取到证据（既没有确认过现象，"
                   f"上一轮也没有对得上的证据）—— 零证据下**不出**处置方案"),
        expected="拿到该服务的证据（错误类型 / 指标拐点 / 日志样本）后，再给出**有依据的**处置方案",
        risk_level=RiskLevel.L0.value, requires_approval=False,
        blast_radius="只读排查，不改动任何线上状态",
        rollback="回滚方案：只读排查无需回滚",
        steps=(f"取 {target or '点名服务'} 的指标（连接池 / 线程池 / 延迟 / 错误率）",
               f"取 {target or '点名服务'} 同窗口的错误日志，拿到**具体的错误类型**",
               "把证据与用户描述的症状对齐后再给处置方案"),
        preconditions=("需要先完成一轮针对该服务的取证",))


def anchored_to(plan: RemediationPlan, services: Sequence[Any]) -> bool:
    """这条方案是不是**冲着那件事**（服务 / 资源）去的。

    ★ 匹配面就是方案**给用户看的那些字**（标题 + 依据 + 预期 + 影响面 + 步骤）：
      方案表里出现的东西用户会照着做 —— 所以"锚定"必须咬在这里，而不是某个内部字段。
    ★ 点名的服务在依据里、动作目标在标题与影响面里，两种形状都算锚住。
    ★ 空 `services` = 没有锚（返回 `True`，保持"不筛"的语义）。
    """
    wanted = {str(name).strip().lower() for name in (services or ()) if str(name).strip()}
    if not wanted:
        return True
    haystack = " ".join((plan.title, plan.rationale, plan.expected,
                         plan.blast_radius, " ".join(plan.steps))).lower()
    return any(name in haystack for name in wanted)
