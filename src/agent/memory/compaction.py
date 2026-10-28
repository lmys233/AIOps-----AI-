"""历史分层的兼容压缩接口。压力取下一次提示占用的本地估算，
不是累计费用；默认受控路径使用 llm/context.py 的实际请求装配边界。
压缩只替换对应中层，失败不删除原文。旧遗忘策略只影响召回，不能证明恢复或清除持久档案。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Final, Optional, Sequence

from ..memory.stores.base import WORKING_TTL_SEC
from ..memory.stores.records import ConversationTurn
from .index import IndexEntry, at_epoch
from .tiers import NEAR_SHARE, plan_tiers

__all__ = [
    "COMPACT_AT",
    "MAX_FAR_ENTRIES",
    "FORGET_TTL_SEC",
    "SummaryFn",
    "CompactionResult",
    "pressure",
    "should_compact",
    "compact_history",
    "forget",
]

#: 自动压缩阈值：上下文预算用到这么多就该压了
COMPACT_AT: Final[float] = 0.8

#: 远层索引最多留几条（超上限丢最老的）
MAX_FAR_ENTRIES: Final[int] = 500

#: 遗忘 TTL（复用工作记忆那一个数字：24 小时，同一档会话级生命周期）
FORGET_TTL_SEC: Final[int] = WORKING_TTL_SEC

#: 摘要回调：**一次**模型请求把中层原文压成一段话
SummaryFn = Callable[[str], Awaitable[str]]


@dataclass
class CompactionResult:
    """一次压缩的结论（**没触发也算一次结论**，只是 `calls=0`）。"""

    near: tuple[ConversationTurn, ...] = ()
    summary: str = ""
    replaced: int = 0
    calls: int = 0
    triggered: bool = False
    note: str = ""
    #: ★ **压缩前**那一道裁剪的留痕（超长块裁了多少、完整原文可按哪个引用回放）
    payload_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"near": [turn.turn for turn in self.near], "summary": self.summary,
                "replaced": self.replaced, "calls": self.calls,
                "triggered": self.triggered, "note": self.note,
                "payload_note": self.payload_note}


def pressure(budget: Any=None, *, prompt_tokens: int=0, context_tokens: int=32768, output_tokens: int=0) -> float:
    """Next prompt occupancy estimate. Cumulative spending cannot supply occupancy."""
    maximum = int(context_tokens)
    if maximum <= 0:
        return 0.0
    return max(0, int(prompt_tokens)+int(output_tokens)) / maximum


def should_compact(budget: Any=None, *, threshold: float = COMPACT_AT, **occupancy) -> bool:
    """压力到阈值了吗（`>=`：正好 0.8 就算到了）。"""
    return pressure(budget,**occupancy) >= float(threshold)


async def compact_history(turns: Sequence[ConversationTurn], *, budget: Any,
                          summarize: SummaryFn,
                          near_share: float = NEAR_SHARE,context_tokens:int=32768,
                          prompt_tokens:Optional[int]=None,output_tokens:int=512) -> CompactionResult:
    """压力到阈值时把中层压成一段摘要（**恰好一次**模型请求）；否则原样返回。

    ★ 未触发时 `calls=0`，不花费摘要模型调用：
      上下文还有余量时压缩纯属浪费，而浪费的是一次完整模型请求。
    """
    from ..common.tokens import estimate_tokens
    upcoming=estimate_tokens(_render(turns)) if prompt_tokens is None else prompt_tokens
    measured=pressure(prompt_tokens=upcoming,context_tokens=context_tokens,output_tokens=output_tokens)
    plan = plan_tiers(turns, budget_tokens=context_tokens,
                      near_share=near_share)
    if measured<COMPACT_AT:
        return CompactionResult(
            near=plan.near, calls=0, triggered=False,
            note=(f"提示估算压力 {measured:.3f} < 阈值 {COMPACT_AT}"
                  f"（配置窗口 {plan.budget_tokens} token；历史输入估算 {upcoming}，输出预留 {output_tokens}）→ 本次不压缩、"
                  f"一次模型调用都没发；{plan.note}"))
    if not plan.middle:
        return CompactionResult(
            near=plan.near, calls=0, triggered=False,
            note=(f"提示估算压力 {measured:.3f} ≥ 阈值 {COMPACT_AT}，"
                  f"但没有可压缩的中层（近层原文 {len(plan.near)} 条）→ 不调模型"));
    return await _compress(plan, budget=budget, summarize=summarize,measured=measured)


async def _compress(plan: Any, *, budget: Any, summarize: SummaryFn,measured:float) -> CompactionResult:
    """真的压一次：调 `summarize` 恰好一次，成功则替换中层，失败则如实降级。

    ★ 留痕里的压力必须与**判据同源**（都读 `pressure(budget)`）：
      如果这里换成"近层 token ÷ 预算"之类的另一个口径，就会出现
      「压力 0.158 ≥ 阈值 0.8」这种自相矛盾的话 —— 而留痕正是给人核对用的。
    """
    payload = _render(plan.middle)
    try:
        summary = str(await summarize(payload) or "").strip()
    except Exception as exc:                  # noqa: BLE001 —— 降级要响，不许静默吞
        return CompactionResult(
            near=plan.near, summary="", replaced=0, calls=1, triggered=False,
            note=(f"摘要模型调用失败（{type(exc).__name__}: {exc}）→ 本次压缩**未生效**，"
                  f"中层 {len(plan.middle)} 条原文原样保留（提示估算压力 {measured:.3f} 不变）"))
    if not summary:
        return CompactionResult(
            near=plan.near, summary="", replaced=0, calls=1, triggered=False,
            note=(f"摘要返回空文本 → 视为**未生效**（不用空摘要替换掉 "
                  f"{len(plan.middle)} 条原文）"))
    return CompactionResult(
        near=plan.near, summary=summary, replaced=len(plan.middle), calls=1,
        triggered=True,
        note=(f"提示估算压力 {measured:.3f} ≥ 阈值 {COMPACT_AT} → 已把中层 "
              f"{len(plan.middle)} 条原文压成一段摘要（**1 次模型请求**）并**替换**原文；"
              f"近层 {len(plan.near)} 条仍是逐字原文，远层 {len(plan.far)} 条只留索引"))


def forget(entries: Sequence[IndexEntry], *, now: Optional[Any] = None,
           ttl_sec: int = FORGET_TTL_SEC,
           max_entries: int = MAX_FAR_ENTRIES) -> tuple[tuple[IndexEntry, ...], str]:
    """丢掉**过期**与**超上限**的索引条目，返回 `(保留的, 为什么丢的)`。

    ★ 两道判据都要生效，且都要**说出来**：TTL 丢掉的是"太久远、大概率已修复"的，
      条数上限丢掉的是**最老的**。只报总数的话，"我上周那次查询怎么查不到了"
      就只能靠猜。
    ★ 时间读不出来的条目不按过期丢（**不猜**），但仍然受条数上限约束。
    """
    items = [entry for entry in (entries or ()) if entry is not None]
    stamp = _stamp(now)
    kept, expired = _drop_expired(items, now=stamp, ttl_sec=ttl_sec)
    bounded = sorted(kept, key=lambda item: _at(item), reverse=True)[: max(0, int(max_entries))]
    overflow = len(kept) - len(bounded)
    order = {id(entry) for entry in bounded}
    survived = tuple(entry for entry in kept if id(entry) in order)
    return survived, _forget_note(len(items), len(survived), expired, overflow,
                                  ttl_sec=ttl_sec, max_entries=max_entries)


def _drop_expired(entries: Sequence[IndexEntry], *, now: float,
                  ttl_sec: int) -> tuple[list[IndexEntry], int]:
    """按 TTL 过滤：返回 `(没过期的, 丢了几条)`。"""
    window = max(0, int(ttl_sec or 0))
    kept: list[IndexEntry] = []
    expired = 0
    for entry in entries:
        stamp = at_epoch(entry.at)
        if stamp is not None and now - stamp > window:
            expired += 1
            continue
        kept.append(entry)
    return kept, expired


def _forget_note(total: int, kept: int, expired: int, overflow: int, *,
                 ttl_sec: int, max_entries: int) -> str:
    """遗忘留痕：说清**为什么**丢（过期不再召回 / 超上限丢最老的）。"""
    parts = [f"遗忘：原有 {total} 条远层索引，保留 {kept} 条"]
    if expired:
        parts.append(f"★ 过期不再召回 {expired} 条（TTL {int(ttl_sec)} 秒，"
                     f"太久远的大概率已被修复）")
    if overflow:
        parts.append(f"★ 超上限丢最老 {overflow} 条（条数上限 {int(max_entries)}）")
    if not expired and not overflow:
        parts.append("没有过期、也没超上限（**一条都没丢**）")
    return "；".join(parts)


def _render(turns: Sequence[ConversationTurn]) -> str:
    """把待摘要的那几轮渲染成一段可读文本（**原话 + 结论摘要**一起给）。"""
    lines = [f"第 {turn.turn} 轮｜用户：{turn.user_message}"
             + (f"｜结论摘要：{turn.response_digest}" if turn.response_digest else "")
             for turn in turns]
    return "\n".join(lines)


def _stamp(now: Optional[Any]) -> float:
    """`now` 归一成 epoch 秒（没给就用当前时间）。"""
    parsed = at_epoch(now) if now is not None else None
    return parsed if parsed is not None else datetime.now(timezone.utc).timestamp()


def _at(entry: IndexEntry) -> float:
    """排序用的时间（读不出来 → 负无穷，即最老）。"""
    stamp = at_epoch(entry.at)
    return float("-inf") if stamp is None else stamp
