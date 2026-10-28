"""远层**索引**（不是摘要）：有界预览 + 稳定分页 + 按时间可查。

★ 为什么远层必须是索引而不是更短的摘要

摘要只保留"当时觉得重要"的那几句，而排查里真正要回看的是**细节**
（"上周那次是不是也改过这个 ConfigMap"）。把远层压成第二份摘要，
就是用一个可能有损的版本去顶替原文 —— 而且**再也查不回来了**。

索引反过来：上下文里只放"第几轮、什么时候、开头一句"，
需要旧内容时**按需查出来**（`page_entries` / `search_entries`），
查到的仍是原文，不是转述。

★ 预览**有界**：索引是给模型判断"值不值得回查"用的，不是把内容换个地方塞回去。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Optional, Sequence

from ..memory.stores.records import ConversationTurn
from .tiers import FAR_PREVIEW_CHARS

__all__ = ["IndexEntry", "index_entries", "page_entries", "search_entries", "at_epoch"]

#: 分页的默认每页条数（索引是"翻着看"，一页给太多就退化成塞上下文）
DEFAULT_PAGE_SIZE: Final[int] = 20


@dataclass(frozen=True)
class IndexEntry:
    """一条索引：哪一轮、什么时候、开头一句（**有界预览**）。"""

    session_id: str = ""
    turn: int = 0
    at: str = ""
    preview: str = ""
    incident_id: str = ""
    status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "turn": self.turn, "at": self.at,
                "preview": self.preview, "incident_id": self.incident_id,
                "status": self.status}


def index_entries(turns: Sequence[ConversationTurn], *, session_id: str,
                  preview_chars: int = FAR_PREVIEW_CHARS) -> tuple[IndexEntry, ...]:
    """把若干轮对话做成索引条目（顺序照旧，**预览各自有界**）。

    ★ `incident_id` / `status` 由轮次台账另行补齐（`SessionStore.index_put`
      入库时带上）：索引本身只回答"哪一轮、什么时候、讲了什么开头"。
    """
    bound = max(1, int(preview_chars or FAR_PREVIEW_CHARS))
    return tuple(
        IndexEntry(session_id=str(session_id or ""), turn=int(turn.turn or 0),
                   at=str(turn.at or ""),
                   preview=_preview(turn, bound), incident_id="", status="")
        for turn in (turns or ()) if turn is not None)


def page_entries(entries: Sequence[IndexEntry], *, offset: int = 0,
                 limit: int = DEFAULT_PAGE_SIZE) -> tuple[IndexEntry, ...]:
    """稳定分页：按 `(turn, at)` 排序后切片（同样输入**每次得到同样的一页**）。

    ★ 排序键必须**确定**：直接按传入顺序切片的话，两个来源拼起来的序列
      （本地表按 `turn` 升序 + 内存里按时间正序）在轮次号并列时会给出不同的页，
      翻页就会**重复或漏条**。
    """
    ordered = sorted((entry for entry in (entries or ()) if entry is not None),
                     key=lambda item: (int(item.turn or 0), str(item.at or "")))
    start = max(0, int(offset or 0))
    size = max(0, int(limit or 0))
    return tuple(ordered[start:start + size]) if size else ()


def search_entries(entries: Sequence[IndexEntry], *, start: Any = None, end: Any = None,
                   limit: int = DEFAULT_PAGE_SIZE) -> tuple[IndexEntry, ...]:
    """按**时间区间**查索引（`[start, end]` 闭区间；边界给 `None` 表示不限）。

    ★ 时间读不出来的条目不参与区间过滤（**不猜**）：把它当"落在区间内"
      会让区间查询给出一个没有依据的结果。
    """
    low, high = _bound(start), _bound(end)
    ordered = page_entries(entries, offset=0, limit=len(entries or ()))
    found = [entry for entry in ordered
             if _within(at_epoch(entry.at), low=low, high=high)]
    size = max(0, int(limit or 0))
    return tuple(found[:size]) if size else ()


def at_epoch(value: Any) -> Optional[float]:
    """把轮次时间归一成 epoch 秒（认 ISO 字符串与数字；读不出来 → `None`，不猜）。"""
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ----------------------------------------------------------------------
def _preview(turn: ConversationTurn, bound: int) -> str:
    """这一轮的**有界**预览（优先用结论摘要，没有就用用户原话的开头）。"""
    text = str(turn.response_digest or "").strip() or str(turn.user_message or "").strip()
    flat = " ".join(text.split())
    return flat[:bound]


def _bound(value: Any) -> Optional[float]:
    """区间边界（`None`/读不出来 → `None`，表示这一侧不限）。"""
    return None if value is None else at_epoch(value)


def _within(stamp: Optional[float], *, low: Optional[float],
            high: Optional[float]) -> bool:
    """这一条落在闭区间内吗（时间读不出来 → **不参与过滤**，不猜）。"""
    if stamp is None:
        return False
    return not ((low is not None and stamp < low) or (high is not None and stamp > high))
