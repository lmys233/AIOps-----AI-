"""三层记忆与上下文管理 → **提示词**（渲染是纯函数；压缩那一步要一次模型请求）。

★ 注入给模型的是什么

| 层 | 进上下文的东西 | 为什么不给全文 |
|---|---|---|
| **近** | 最近若干轮**逐字原文** | 指代/省略/约束都挂在原话上，转述必丢 |
| **中** | 更早历史**压成的一段摘要** | 原始轮次太多，压一次换回可读的全局 |
| **远** | **索引一行**（轮次号 + 时间 + 有界预览） | 需要旧内容时**按需查**，而不是先塞进来 |

★ 为什么"超长工具结果先裁"必须发生在**注入与压缩之前**

压缩的输入就是待压缩的原文。如果超长块不做这一道，压缩那一次模型调用
自己就可能被几十万字符撑爆 —— 于是"压缩"反而成了上下文爆炸的那一次。
所以顺序是**固定**的：先 `prune_tool_result`（并留本地副本 + `raw_ref`），再喂给摘要。

★ 留痕：`describe_context` 说清"交给了几条原文 / 几条摘要 / 几条索引"。
  没有历史时返回**空串**（不留噪声），也就什么都不注入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Final, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from ..common.tokens import estimate_tokens
from ..memory.compaction import (
    COMPACT_AT,
    CompactionResult,
    forget,
    pressure,
    should_compact,
)
from ..memory.index import index_entries
from ..memory.prune import describe_pruning, prune_tool_result
from ..memory.stores.records import ConversationTurn
from ..memory.tiers import FAR_PREVIEW_CHARS, plan_tiers
from ..tools.rawstore import save_raw

__all__ = [
    "CONTEXT_HEADING",
    "MAX_SUMMARY_TOKENS",
    "SummaryFn",
    "PrunedPayload",
    "prune_payload",
    "context_from_turns",
    "render_context_block",
    "describe_context",
    "summarize_with_gateway",
]

#: 注入小节的标题（进提示词时当小节名用）
CONTEXT_HEADING: Final[str] = "## 会话上下文分层（近=原文 / 中=摘要 / 远=索引）"

#: 摘要一次最多产出多少 token（摘要比原文短，但仍要有界）
MAX_SUMMARY_TOKENS: Final[int] = 512

#: 摘要回调：**一次**模型请求把中层原文压成一段话（与压缩层同一个形状）
SummaryFn = Callable[[str], Awaitable[str]]


@dataclass(frozen=True)
class PrunedPayload:
    """一次"先裁"的结论：进上下文的那份 + 完整原文的本地引用。"""

    text: str = ""
    raw_ref: str = ""
    original_chars: int = 0
    pruned: bool = False
    note: str = ""


class MiddleSummary(BaseModel):
    """摘要必须按这个结构取回（网关只接受结构化输出，不收自由文本）。

    ★ 摘要会被当成**事实背景**复用，所以它不能是模型随手写的一段话：
      结构里只要"这一段讲了什么"，不要它顺手给的结论。
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1,
                      description="把更早的若干轮压成一段话：做了什么 / 是否完成 / 缺什么 / 结果")


def summarize_with_gateway(gateway: Any, model: Optional[str] = None) -> SummaryFn:
    """把模型网关包成一个摘要回调（**每次调用恰好一次模型请求**）。

    ★ 网关不可用 → 返回的回调**抛异常**（不是返回空串）：压缩层据此
      如实降级并留痕。返回空串会被读成"摘要压出来是空的"，两者含义不同。
    """

    async def _summarize(payload: str) -> str:
        """把待摘要的几轮交给模型压成一段话（失败抛 `LLMError`，由上层如实降级）。"""
        if gateway is None:
            raise RuntimeError("没有模型网关：摘要需要一次模型请求（不许静默跳过）")
        from ..summary import SummaryAgent,SummaryRequest
        result=await SummaryAgent(gateway).summarize(SummaryRequest(purpose='memory_context',source=str(payload or ''),
            requirements='返回text：做了什么 / 是否完成 / 缺什么 / 结果。',output_tokens=MAX_SUMMARY_TOKENS),
            MiddleSummary,role='compaction',model=model or None)
        return result.content['text'].strip()

    return _summarize


def prune_payload(text: str, *, raw_ref: str = "", fetched_at: float = 0.0) -> PrunedPayload:
    """**压缩之前的固定动作**：超长文本先裁，完整原文落本地副本（可精确回放）。

    ★ 写副本失败**不影响**这次注入（那块原文只是"这次没留副本"），
      但留痕里必须看得出来 —— 否则"可以按 raw_ref 回放"就是一句空话。
    """
    clean = "" if text is None else str(text)
    total = len(clean)
    pruned_text = prune_tool_result(clean)
    pruned = len(pruned_text) != total
    stored = _store(raw_ref, clean, fetched_at=fetched_at)
    note = describe_pruning(total, pruned, raw_ref=stored)
    if pruned and not stored:
        note = f"{note}；★ 本地副本没写成功（这块原文无法回放）"
    return PrunedPayload(text=pruned_text, raw_ref=stored, original_chars=total,
                         pruned=pruned, note=note)


async def context_from_turns(turns: Sequence[ConversationTurn], *, budget: Any,
                             summarize: Optional[SummaryFn] = None,
                             session_id: str = "") -> tuple[CompactionResult, tuple[Any, ...], str]:
    """一次完整的上下文整理：**先裁** → 切分 → 遗忘 → （必要时）压缩 → 索引。

    Args:
        budget: 累计任务预算；历史压力另用占用估算，最终请求由统一网关装配检查。
        summarize: 摘要回调；`None` 表示这次没接模型 → **不压缩**，其余照做。

    Returns:
        `(压缩结论, 远层索引页, 切分说明)`。
    """
    from ..llm.context import DEFAULT_CONTEXT_TOKENS
    plan = plan_tiers(turns, budget_tokens=DEFAULT_CONTEXT_TOKENS)
    payload = prune_payload(_render(plan.middle))
    result = await _compress(plan, budget=budget, summarize=summarize, payload=payload)
    page, forget_note = _page_of(turns, session_id=session_id,
                                 replaceable=_replaceable(plan, result))
    note = _note(plan, result, page, forget_note=forget_note, payload_note=payload.note)
    return result, page, note


def _note(plan: Any, result: CompactionResult, page: Sequence[Any], *,
          forget_note: str, payload_note: str) -> str:
    """把三段留痕拼成一句（**先说切分，再说压缩，最后说遗忘与先裁**）。"""
    middle = (f"中层 {len(plan.middle)} 条原文已压成一段摘要"
              if result.summary else f"中层 {len(plan.middle)} 条原文未压成摘要")
    return (f"{plan.note}；{middle}；索引页 {len(page)} 条；"
            f"{forget_note}；先裁：{payload_note}")


def render_context_block(result: CompactionResult, page: Sequence[Any], *,
                         plan_note: str = "") -> str:
    """把三层切分 + 压缩结论 + 远层索引渲染成一段提示词（**没有历史就返回空串**）。

    ★ 空串 = 不注入：提示词里每一行都要有用，"（无历史）"这种噪声会稀释真规则。
    """
    if not result.near and not result.summary and not page:
        return ""
    lines = [CONTEXT_HEADING, _near_line(result)]
    if result.summary:
        lines.append(_summary_line(result))
    lines.extend(_index_lines(page))
    if plan_note:
        lines.append(f"· 切分依据：{plan_note}")
    lines.append(_rule())
    return "\n".join(lines)


def describe_context(result: CompactionResult, page: Sequence[Any], *,
                     pruned: int = 0) -> str:
    """注入留痕（进 `degraded`/`notes`）：交给了几条原文 / 摘要 / 索引、裁了几块。"""
    if not result.near and not page:
        return ""
    middle = (f"中层摘要已生成（{len(result.summary)} 字，替换 {result.replaced} 条原文）"
              if result.summary else "中层摘要未生成")
    parts = [f"上下文分层：近层原文 {len(result.near)} 条、远层索引 {len(page)} 条、{middle}"]
    if result.calls:
        parts.append(f"压缩模型调用 {result.calls} 次")
    if pruned:
        parts.append(f"先裁超长块 {pruned} 段（完整原文留在本地副本，可按 raw_ref 回放）")
    return "；".join(parts)


# ----------------------------------------------------------------------
async def _compress(plan: Any, *, budget: Any, summarize: Optional[SummaryFn],
                    payload: PrunedPayload) -> CompactionResult:
    """按压力决定压不压；压就**恰好调一次**摘要回调（失败**如实降级**）。"""
    base = dict(near=plan.near, calls=0, triggered=False)
    measured=pressure(prompt_tokens=estimate_tokens(_render([*plan.far,*plan.middle,*plan.near])),output_tokens=MAX_SUMMARY_TOKENS)
    if measured<COMPACT_AT:
        return _result(payload, **base,
                       note=f"历史提示估算压力 {measured:.3f} < 阈值 {COMPACT_AT} → 不压缩（0 次模型调用）")
    if summarize is None:
        return _result(payload, **base,
                       note=f"压力 ≥ 阈值 {COMPACT_AT}，但这次没接摘要回调 → 不压缩")
    if not plan.middle:
        return _result(payload, **base, note="没有可压缩的中层（近层已含全部原文）")
    try:
        summary = str(await summarize(payload.text) or "").strip()
    except Exception as exc:              # noqa: BLE001 —— 降级要响，不许静默吞
        return _result(payload, near=plan.near, calls=1, triggered=False,
                       note=(f"摘要模型调用失败（{type(exc).__name__}: {exc}）→ 压缩**未生效**，"
                             f"中层 {len(plan.middle)} 条原文原样保留"))
    if not summary:
        return _result(payload, near=plan.near, calls=1, triggered=False,
                       note=f"摘要返回空文本 → 视为未生效（不用空摘要替换 {len(plan.middle)} 条原文）")
    return _result(payload, near=plan.near, summary=summary, replaced=len(plan.middle),
                   calls=1, triggered=True,
                   note=(f"中层 {len(plan.middle)} 条原文已压成一段摘要（1 次模型请求）并**替换**原文"))


def _result(payload: PrunedPayload, **fields: Any) -> CompactionResult:
    """组装结论（**压缩前那一道裁剪的留痕**一并带上）。"""
    return CompactionResult(payload_note=payload.note, **fields)


def _page_of(turns: Sequence[ConversationTurn], *, session_id: str,
             replaceable: Sequence[int]) -> tuple[tuple[Any, ...], str]:
    """远层索引页 + 遗忘留痕（TTL 与条数上限都在 `forget` 里生效）。

    ★ 页里只放**没进上下文**的那些轮（远层 + 中层）：近层是逐字原文，
      把它的索引也列出来就是同一件事在同一段提示词里说两遍。
      中层在这里出现是**故意的** —— 它可能还没压成摘要（模型没接上/调用失败），
      那时"查得到"就是它唯一还能被用上的方式。
    """
    wanted = {int(turn) for turn in replaceable}
    entries = [entry for entry in index_entries(turns, session_id=session_id,
                                                preview_chars=FAR_PREVIEW_CHARS)
               if int(entry.turn or 0) in wanted]
    kept, note = forget(entries)
    return kept, note


def _replaceable(plan: Any, result: CompactionResult) -> tuple[int, ...]:
    """该进索引的那几轮：远层全部 + 中层（已压成摘要的原文被替换，没压成的也要**查得到**）。"""
    return (tuple(int(turn.turn or 0) for turn in plan.far)
            + tuple(int(turn.turn or 0) for turn in plan.middle))


def _render(turns: Sequence[ConversationTurn]) -> str:
    """把待摘要的那几轮渲染成一段可读文本（**原话 + 结论摘要**一起给）。"""
    return "\n".join(
        f"第 {turn.turn} 轮｜用户：{turn.user_message}"
        + (f"｜结论摘要：{turn.response_digest}" if turn.response_digest else "")
        for turn in turns)


def _near_line(result: CompactionResult) -> str:
    """近层的说明行（**条数 + 估算 token**，并写明这是逐字原文）。"""
    used = sum(estimate_tokens(turn.user_message) + estimate_tokens(turn.response_digest)
               for turn in result.near)
    return f"· 近层（逐字原文，**不要改写**）：{len(result.near)} 条，约 {used} token"


def _summary_line(result: CompactionResult) -> str:
    """中层摘要的说明行（被**替换**掉的原文不再出现）。"""
    return (f"· 中层（由更早的 {result.replaced} 条原文压成的摘要，"
            f"原文已被**替换**、不再重复出现）：\n{result.summary}")


def _index_lines(page: Sequence[Any]) -> list[str]:
    """远层索引行（有界预览 + 时间）：要旧内容时**按需查**，不要先塞进来。"""
    if not page:
        return []
    head = f"· 远层（只给索引，共 {len(page)} 条；需要旧内容时按时间/序号**查回来**）："
    return [head, *(_index_line(entry) for entry in page)]


def _index_line(entry: Any) -> str:
    """索引里的一行（轮次号 + 时间 + 预览）。"""
    turn = _field(entry, "turn", "?")
    at = _field(entry, "at", "")
    preview = _field(entry, "preview", "")
    return f"  - 第 {turn} 轮｜{at}｜{preview}"


def _field(entry: Any, name: str, default: Any) -> Any:
    """索引条目的一个字段（对象与 dict 都认；读不到 → 默认值，不猜）。"""
    if isinstance(entry, dict):
        return entry.get(name, default)
    return getattr(entry, name, default)


def _rule() -> str:
    """段末那条使用规则（模型据此决定"是回查还是直接答"）。"""
    return ("★ 用法：近层可以直接引用（那就是用户原话）；中层摘要只当**背景**，"
            "引用具体数字/文件名前必须回查远层索引；远层没查过的内容**不许当成事实**。")


def _store(raw_ref: str, text: str, *, fetched_at: float) -> str:
    """把完整原文写到本地副本，返回真正可用的 `raw_ref`（写不进去 → 空串）。"""
    reference = str(raw_ref or "").strip()
    if not reference:
        return ""
    try:
        save_raw(reference, text, fetched_at=fetched_at)
    except Exception:                     # noqa: BLE001 —— 没留副本不该带走这次注入
        return ""
    return reference
