"""会话记忆清单 → **提示词**（纯函数，零 IO）。

★★ 为什么要有它

「记忆是唯一的数据复用层」的判据是**数据的时间戳**，不是"输入是不是一样"。
而"要不要再取一次数"这个判断是**模型**做的：只有把记忆里每一段的
**抓取时间**、**覆盖区间**摆到它面前，它才知道这段数据还新不新、要不要再发 tool call。
所以清单里必须同时有：覆盖窗口、抓取时间、相对年龄，以及那条**时间规则**。

★★ 规则（与工具层同一口径，数字只有一处来源）

`now - 抓取时间 ≤ 60s` 可以直接复用（允许 1 分钟脏数据）；
超过 60s 必须重新取数；已经闭合的历史区间（`end` 已过去）可以长期复用。

★ 没有记忆 → 返回**空串**：不输出"（无记忆）"这类噪声（提示词里每一行都要有用）。
★ 条目形状**双通道读**：既可能是契约对象（`QueryRecord`），也可能是 dict。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Optional, Sequence

from ..tools.cache import (
    memory_freshness_sec,
    memory_is_fresh,
    memory_window_closed,
)

__all__ = [
    "DEFAULT_MEMORY_LIMIT",
    "MEMORY_HEADING",
    "describe_injection",
    "render_memory_block",
]

#: 清单最多列几条（最近抓取优先）—— 清单是给模型看的，不是日志
DEFAULT_MEMORY_LIMIT: Final[int] = 8

#: 清单的标题行（注入提示词时当小节名用）
MEMORY_HEADING: Final[str] = "## 会话记忆清单（判断要不要重新取数，依据是**时间**）"


def render_memory_block(records: Sequence[Any], *, now: float,
                        limit: int = DEFAULT_MEMORY_LIMIT) -> str:
    """把会话记忆清单渲染成一段提示词（**没有记录就返回空串**）。"""
    rows = _ordered(records, limit=limit)
    if not rows:
        return ""
    lines = [MEMORY_HEADING]
    lines.extend(_line(index, record, now=now)
                 for index, record in enumerate(rows, 1))
    lines.extend(["", _rule()])
    return "\n".join(lines)


def describe_injection(records: Sequence[Any], *, now: float,
                       limit: int = DEFAULT_MEMORY_LIMIT) -> str:
    """注入留痕（进 `degraded`/`notes`）：交给了几条、最旧一条多少秒；没记录 → 空串。

    ★ "模型看到了什么"必须可追溯：否则"它为什么没重取"只能靠猜。
    """
    rows = _ordered(records, limit=limit)
    if not rows:
        return ""
    oldest = max(_age_seconds(record, now) for record in rows)
    return f"已把 {len(rows)} 条会话记忆（最旧 {oldest:.0f} 秒）交给模型判断是否复用"


# ----------------------------------------------------------------------
def _rule() -> str:
    """段末那条**时间规则**（门槛从 `AIOPS_MEMORY_FRESHNESS_SEC` 取，默认 60 秒）。"""
    limit = memory_freshness_sec()
    budget = "1 分钟" if abs(limit - 60.0) < 0.5 else f"{limit:.0f} 秒"
    return "\n".join([
        "★ 复用规则（判断依据是**时间**，不是「输入是不是一样」）：",
        f"· `now - 抓取时间 ≤ {limit:.0f}s` 可以直接复用（允许 {budget}脏数据），"
        "**不要**再发 tool call 取最新数据；",
        f"· 超过 {limit:.0f}s 必须重新取数（**强硬要求**：旧数据不许冒充当前状态）；",
        "· 已经闭合的历史区间（end 已过去）可以长期复用（数据不可变），"
        "复用时写明「命中哪一段、原始抓取于何时」。",
    ])


def _line(index: int, record: Any, *, now: float) -> str:
    """清单里的一条：实体/信号 + 覆盖窗口 + 抓取时间 + 相对年龄 + 复用结论。"""
    entity = _text(record, "entity") or "（无实体）"
    signal = _text(record, "signal") or "（无信号）"
    source = _text(record, "source") or "?"
    step = _text(record, "step") or "?"
    start, end = _number(record, "start"), _number(record, "end")
    window = (f"{_clock(start)} ~ {_clock(end)}"
              if start is not None and end is not None else "（覆盖区间未知）")
    fetched = _number(record, "fetched_at")
    stamp = (f"{_rfc3339(fetched)}（{_age_text(_age_seconds(record, now))}）"
             if fetched is not None else "（抓取时间未知）")
    return (f"- {index}. {entity} / {signal}（{source}，step {step}）："
            f"覆盖 {window}｜抓取 {stamp}｜{_verdict(record, now=now)}")


def _verdict(record: Any, *, now: float) -> str:
    """这一条现在能不能直接用（**判据只有一处**：`memory_is_fresh`）。"""
    if memory_is_fresh(record, now=now):
        return f"★ 可直接复用（{_reuse_reason(record, now=now)}）"
    return (f"★ **必须重新取数**（开窗，且抓取时间已超过 "
            f"{memory_freshness_sec():.0f}s 门槛）")


def _reuse_reason(record: Any, *, now: float) -> str:
    """"为什么可以复用"的一句话（闭合窗 / 开窗但抓取时间够新）。"""
    if memory_window_closed(_field(record, "end"), now=now):
        return "闭合窗：数据不可变"
    fetched = _number(record, "fetched_at")
    if fetched is None:
        return "开窗，抓取时间未知但未超过门槛"
    return f"开窗但 {_age_text(max(0.0, now - fetched))}抓取（≤门槛）"


# ----------------------------------------------------------------------
def _ordered(records: Sequence[Any], *, limit: int) -> list[Any]:
    """最近抓取优先、有界（抓取时间读不出来的排在最后）。"""
    items = [item for item in (records or ()) if item is not None]
    items.sort(key=_stamp, reverse=True)
    return items[: max(0, int(limit))]


def _stamp(record: Any) -> float:
    """排序键 = 抓取时间（读不到 → 负无穷，排在最后）。"""
    fetched = _number(record, "fetched_at")
    return float("-inf") if fetched is None else fetched


def _field(record: Any, name: str) -> Any:
    """双通道读一条字段：`getattr` 优先，读不到退 `obj[name]`（dict 也认）。"""
    try:
        value = getattr(record, name)
    except Exception:                      # noqa: BLE001 —— 记录形状不该让渲染失败
        value = None
    if value is not None:
        return value
    try:
        return record[name]
    except Exception:                      # noqa: BLE001
        return None


def _text(record: Any, name: str) -> str:
    """读一个文本字段（读不到 → 空串，不猜）。"""
    value = _field(record, name)
    return "" if value is None else str(value).strip()


def _number(record: Any, name: str) -> Optional[float]:
    """读一个 epoch 秒字段（读不到/认不出 → `None`，不猜）。"""
    try:
        return float(_field(record, name))
    except (TypeError, ValueError):
        return None


def _age_seconds(record: Any, now: float) -> float:
    """一条记忆的抓取年龄（秒）：`now - fetched_at`（时间读不到 → 0，不猜）。"""
    fetched = _number(record, "fetched_at")
    return 0.0 if fetched is None else max(0.0, float(now) - fetched)


def _age_text(seconds: float) -> str:
    """相对年龄的人话（"30 秒前" / "5 分钟前" / "2.0 小时前"）。"""
    if seconds < 60:
        return f"{seconds:.0f} 秒前"
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分钟前"
    return f"{seconds / 3600:.1f} 小时前"


def _clock(epoch: Optional[float]) -> str:
    """epoch 秒 → 本地 `MM-DD HH:MM:SS`（窗口可读化）。"""
    try:
        return datetime.fromtimestamp(float(epoch)).strftime("%m-%d %H:%M:%S")
    except (OSError, OverflowError, TypeError, ValueError):
        return "（时间未知）"


def _rfc3339(epoch: float) -> str:
    """epoch 秒 → RFC3339（带本地时区偏移）—— 抓取时间必须可核对。"""
    try:
        return datetime.fromtimestamp(float(epoch)).astimezone().isoformat(
            timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return "（时间未知）"
