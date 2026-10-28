"""三层切分：近层留**原文**、中层待摘要、远层转索引。

★ 为什么近层必须**逐字**（不是摘要）

多轮对话里"最近几句"是唯一能保证语义无损的部分：指代、省略、语气、约束
（"只查 30 分钟"）都挂在原话上。把最近几轮也压成摘要，模型就会**基于一段转述**
去决定下一步 —— 它连"用户刚才到底说了什么"都看不到了。

★ 为什么按**预算占比**切（不是按条数）

"留最近 5 轮"在短句上浪费预算、在长句上撑爆上下文。占比口径下，
切分点跟着真实可用预算走：预算变小，近层自动变薄；预算变大，能多留几轮原文。

★★ 三条不变量（都可测）

1. **一条不丢**：`len(near) + len(middle) + len(far) == len(turns)`；
2. **最新一轮一定在近层** —— 预算再小也不能把"用户刚说的那句"挤到中层去；
3. 层与层**不重叠**：`near` 是最近的连续一段，`middle` 紧接着它，`far` 是更老的。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Sequence

from ..common.tokens import estimate_tokens
from ..memory.stores.records import ConversationTurn

__all__ = ["NEAR_SHARE", "FAR_PREVIEW_CHARS", "TierPlan", "plan_tiers"]

#: 近层占上下文预算的比例（逐字保留最近的这么多）
NEAR_SHARE: Final[float] = 0.16

#: 远层预览的字符上限（索引是"看得出是哪一轮"，不是内容本身）
FAR_PREVIEW_CHARS: Final[int] = 120


@dataclass(frozen=True)
class TierPlan:
    """一次三层切分的结论（`near` 原文 / `middle` 待摘要 / `far` 转索引）。"""

    near: tuple[ConversationTurn, ...] = ()
    middle: tuple[ConversationTurn, ...] = ()
    far: tuple[ConversationTurn, ...] = ()
    budget_tokens: int = 0
    near_tokens: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转成响应形状（正文照旧逐字给近层，中层/远层只给轮次号）。"""
        return {
            "near": [turn.to_dict() for turn in self.near],
            "middle": [turn.turn for turn in self.middle],
            "far": [turn.turn for turn in self.far],
            "budget_tokens": self.budget_tokens,
            "near_tokens": self.near_tokens,
            "note": self.note,
        }


def plan_tiers(turns: Sequence[ConversationTurn], *, budget_tokens: int,
               near_share: float = NEAR_SHARE,
               preview_chars: int = FAR_PREVIEW_CHARS) -> TierPlan:
    """把会话历史切成三层（**从最新往老**走，一条不丢）。

    Args:
        turns: 会话历史（按时间正序，最新的在后）。
        budget_tokens: 本次上下文预算（`options.token_budget` 折进来的那个上限）。
        near_share: 近层占比（默认 :data:`NEAR_SHARE`）。
        preview_chars: 远层预览的字符上限（索引渲染时用，这里只写进说明）。
    """
    clean = [turn for turn in (turns or ()) if turn is not None]
    budget = max(0, int(budget_tokens or 0))
    if not clean:
        return TierPlan(budget_tokens=budget, note="没有历史：近层留空、没有待摘要、没有索引条目")
    allowance = _allowance(budget, near_share)
    near, rest, near_tokens = _take_near(clean, allowance)
    # ★ 中层是"紧接着近层的那一段"，远层是更老的；两层都不进原文上下文
    middle_size = len(rest) - len(rest) // 2
    middle, far = rest[-middle_size:], rest[:-middle_size]
    share = _share(near_share)
    note = (f"三层切分：近层逐字保留 {len(near)} 条原文（约 {near_tokens} token，"
            f"用满预算 {int(share * 100)}%（{allowance} token））；中层 {len(middle)} 条待摘要"
            f"（一次模型请求压成一段）；远层 {len(far)} 条只留索引"
            f"（预览 ≤ {max(0, int(preview_chars))} 字符，可按时间/序号查回来）")
    return TierPlan(near=tuple(near), middle=tuple(middle), far=tuple(far),
                    budget_tokens=budget, near_tokens=near_tokens, note=note)


# ----------------------------------------------------------------------
def _share(near_share: float) -> float:
    """近层占比的**生效值**（非法取值退回默认 —— 留痕要写实际用的那个数）。"""
    share = float(near_share)
    return share if 0.0 < share <= 1.0 else NEAR_SHARE


def _allowance(budget_tokens: int, near_share: float) -> int:
    """近层的 token 额度（预算 × 占比，向下取整；占比非法就退回默认值）。"""
    return int(max(0, int(budget_tokens or 0)) * _share(near_share))


def _take_near(turns: Sequence[ConversationTurn],
               allowance: int) -> tuple[list[ConversationTurn], list[ConversationTurn], int]:
    """从最新往老取原文，直到额度用满；返回 `(近层, 其余的, 近层 token)`。

    ★ **最新的那条无论如何都留在近层**：额度只装得下半条时，
      宁可恶近层超一点点，也不能让"用户刚说的那句"掉进待摘要的中层。
    """
    taken: list[ConversationTurn] = []
    used = 0
    index = len(turns)
    while index > 0:
        turn = turns[index - 1]
        cost = turn_tokens(turn)
        if taken and used + cost > allowance:
            break
        taken.append(turn)
        used += cost
        index -= 1
    near = list(reversed(taken))
    return near, list(turns[:index]), used


def turn_tokens(turn: ConversationTurn) -> int:
    """一轮对话占多少 token（原话 + 摘要；都按同一口径估算）。"""
    return estimate_tokens(turn.user_message) + estimate_tokens(turn.response_digest)
