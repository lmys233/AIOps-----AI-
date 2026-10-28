"""**取数即记忆**：查询结果作为可召回的**记忆条目**。

★★ 设计意图（用户提案要点）

> 「我们可以让**查数据做成 tool call**，那么得到的数据**跟记忆存到一起**就行。在 AI 来看，
> 它如果看记忆会看到"**某时间窗口的查询数据**"类似这样的摘要；当需要查询数据时，AI 知道意图，
> 根据提示词，**先看自己的记忆是否存在查询数据的缓存，并且时间窗口符合要求**，
> 符合就不用发网络请求获取数据了，**直接用记忆中的数据**；否则也可以**减少时间窗口
> （缓存最新时间到当前时间）来查询数据**。」

★★ 它比取数缓存（`tools/cache.py`）多走的一步

| | `tools/cache.py` | 本模块 |
|---|---|---|
| 命中粒度 | **键完全一致**（同 query/start/end/step 摘要） | **窗口覆盖**即可：记忆里有 `[T0,T1]`，要 `[T0,T2]` → **只补 `[T1,T2]`** |
| 增量 | 无（整段重取） | ★ **只取缺口那一段**，与记忆里的旧段**拼接** |
| 可见性 | 留痕在 `raw_ref`（给人看） | ★ 条目本身**可被召回**（给 AI 看）：`describe()` 就是 AI 读到的摘要 |

★★ 三条不可破的红线（不写清楚就会做成"用旧数据冒充新数据"）

1. **老窗口命中 ≠ 当前状态**：用户问"现在如何"时，只有**覆盖到"现在"**（且够新）的记忆才可用；
   否则**必须补数**。把"没查"说成"没异常"是典型翻车。
2. **记忆里的数据必须带原始抓取时间**，答案里不得把它说成"刚查的"（结论可追溯）。
3. **接缝必须可见**：两段数据（不同 step / 不同抓取时间）拼起来时，接缝写进留痕 ——
   不许悄悄并成一条"看起来连续"的序列。
"""

from __future__ import annotations

from dataclasses import dataclass
from contextvars import ContextVar
from typing import Any, Callable, Final, Iterable, Mapping, Optional, Sequence
import time

from ..common.query import duration_seconds

__all__ = [
    "MEMORY_KEY",
    "OPEN_MARGIN_SEC",
    "MAX_RECORDS",
    "DEFAULT_STEP_SEC",
    "QueryRecord",
    "Coverage",
    "QueryMemory",
    "CURRENT_SCOPE",
    "coverage",
    "describe",
    "load_records",
    "records_payload",
    "scope_of",
    "use_scope",
]

#: 会话状态里存"查询数据记忆"的键（与任务台账同一层：会话工作记忆）
MEMORY_KEY: Final[str] = "query_data"

#: 一段窗口算不算"覆盖到现在"的容差（秒）：`end` 距现在小于它 = 还没结束
OPEN_MARGIN_SEC: Final[float] = 15.0

#: 一个会话最多留几条查询记忆（有界：多了按"最近抓取"淘汰）
MAX_RECORDS: Final[int] = 200

#: `step` 认不出来时的兜底秒数（与 :attr:`QueryRecord.step` 的默认写法 `30s` 同源）
DEFAULT_STEP_SEC: Final[float] = 30.0


@dataclass(frozen=True)
class QueryRecord:
    """一条"**某时间窗口的查询数据**"记忆（AI 看到的就是 :meth:`describe`）。

    ★★ 为什么这里**只放索引、不放数据本体**：会话工作记忆是**整份读-改-写**的
      （任务台账就是这个用法），往里塞几万个点会让每一轮都变慢；而"远记忆应当是
      **索引**"这条口径要求的是**可查得到**，不是把原文塞进上下文。所以：

        · **索引/摘要在这里**（小、可召回、AI 看得见）；
        · **数据本体在取数缓存（`tools/cache.py`）的本地落盘里**（`data_ref` = 那份缓存的键材料）。

      `data_available` 由取数层核实（文件被淘汰了就退回整段重取，**不装**）。
    """

    signal: str = ""            # 语义信号（`pool_waiting` / `http_error_rate` / `log_error_burst`…）
    entity: str = ""            # 挂在哪个实体上（`inventory`）
    source: str = ""            # prometheus / loki / tempo
    query: str = ""             # 原样查询语句（**同一条记忆只对同一条语句有效**）
    step: str = "30s"
    start: float = 0.0          # epoch 秒（这段数据的起点）
    end: float = 0.0            # epoch 秒（**这段数据实际覆盖到哪**）
    fetched_at: float = 0.0     # 什么时候抓的（红线 2：必须留着）
    point_count: int = 0        # 有多少个点（规模事实；数据本体不在这里）
    #: ★★ 接缝**就落在这里**（红线：拼过的数据必须看得出是拼的）。
    #: 每段是 `(起点, 终点)`；`_merge_records` 合并同一条语句的记忆时把两段都留下，
    #:   `stitched` / `describe()` 再把它说出来 —— 于是"看起来连续"骗不了读的人。
    segments: tuple[tuple[float, float], ...] = ()
    raw_ref: str = ""
    summary: str = ""

    @property
    def span(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def stitched(self) -> bool:
        """是不是**拼**出来的（多段）—— 答案里不许把它说成"一次查下来的"。"""
        return len(self.segments) > 1

    def to_payload(self) -> dict[str, Any]:
        return {"signal": self.signal, "entity": self.entity, "source": self.source,
                "query": self.query, "step": self.step, "start": self.start,
                "end": self.end, "fetched_at": self.fetched_at,
                "point_count": self.point_count,
                "segments": [list(segment) for segment in self.segments],
                "raw_ref": self.raw_ref, "summary": self.summary}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "QueryRecord":
        segments: list[tuple[float, float]] = []
        for pair in payload.get("segments") or ():
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                try:
                    segments.append((float(pair[0]), float(pair[1])))
                except (TypeError, ValueError):
                    continue
        return cls(
            signal=str(payload.get("signal") or ""),
            entity=str(payload.get("entity") or ""),
            source=str(payload.get("source") or ""),
            query=str(payload.get("query") or ""),
            step=str(payload.get("step") or "30s"),
            start=float(payload.get("start") or 0.0),
            end=float(payload.get("end") or 0.0),
            fetched_at=float(payload.get("fetched_at") or 0.0),
            point_count=int(payload.get("point_count") or 0),
            segments=tuple(segments),
            raw_ref=str(payload.get("raw_ref") or ""),
            summary=str(payload.get("summary") or ""))


@dataclass(frozen=True)
class Coverage:
    """一次"记忆够不够用"的判定（**判据只有这一个**，取数层照它办事）。"""

    state: str = "none"           # full / partial / stale / none
    record: Optional[QueryRecord] = None
    gap_start: float = 0.0        # 需要补的那一段（`partial` / `stale` 时有效）
    gap_end: float = 0.0
    reason: str = ""

    @property
    def usable(self) -> bool:
        """能不能**完全不发请求**。"""
        return self.state == "full"

    @property
    def needs_fetch(self) -> bool:
        return self.state in ("partial", "stale", "none")


def _covers(record: QueryRecord, start: float, end: float,
            *, tolerance: float) -> bool:
    """时间上盖住了吗（容差给"分桶对齐"留一点余地）。"""
    return record.start <= start + tolerance and record.end >= end - tolerance


def coverage(records: Iterable[QueryRecord], *, query: str, start: float, end: float,
             now: float, step: str = "30s",
             open_margin: float = OPEN_MARGIN_SEC) -> Coverage:
    """这段窗口，记忆里够不够用？→ `full` / `partial` / `stale` / `none`。

    ★★ 四条判据（顺序即优先级）：

      ① **同一条语句**才谈复用（`query` 逐字相同）——不同语句的数据不是一回事；
      ② **整段盖住**（`[start,end] ⊆ [rec.start,rec.end]`，留容差）→ `full`（**零请求**）；
      ③ ★★ **只盖住头部**（`rec.start ≤ start` 且 `rec.end > start`，但够不到 `end`）
         → **`partial` / `stale`**，缺口 = `[rec.end, end]` ——
         这正是用户要的「**减少时间窗口（缓存最新时间到当前时间）**来查询数据」；
         ★ `end` 碰到"现在"叫 `stale`（老数据不许冒充当前状态，红线 1），
           完全在过去叫 `partial`（历史不会改写，补上缺口即可）；
      ④ 头部都没盖住 → `none`（整段重取）。
    """
    # ★ 时长解析**不另写一套**：用 `common.query.duration_seconds`（认不出 → `None` = 不猜），
    #   认不出时按本层口径退回 `DEFAULT_STEP_SEC`（取数层默认 30s）。
    step_sec = duration_seconds(step)
    tolerance = max(1.0, (DEFAULT_STEP_SEC if step_sec is None else float(step_sec)) / 2.0)
    wanted = [record for record in records if record.query == query]
    if not wanted:
        return Coverage(state="none", gap_start=start, gap_end=end,
                        reason="记忆里没有这条语句的数据")
    covering = [record for record in wanted
                if _covers(record, start, end, tolerance=tolerance)]
    if covering:
        # ★ 到这里的记录**必然**盖住了 `end`（`_covers` 已判定），所以 `full` 是唯一结论：
        #   不需要再补一条"万一没盖住"的兜底分支（那分支永远走不到）。
        record = max(covering, key=lambda item: item.end)
        return Coverage(state="full", record=record,
                        reason=(f"记忆已覆盖 {_clock(start)}~{_clock(end)}"
                                f"（抓取于 {_clock(record.fetched_at)}）"))
    head = [record for record in wanted
            if record.start <= start + tolerance and record.end > start + tolerance]
    if head:
        return _gap(max(head, key=lambda item: item.end), end,
                    now=now, open_margin=open_margin)
    best = max(wanted, key=lambda item: item.end)
    return Coverage(state="none", record=best, gap_start=start, gap_end=end,
                    reason=(f"记忆里有这条语句的数据，但只覆盖到 {_clock(best.end)}"
                            f"（要的是 {_clock(start)}~{_clock(end)}）"))


def _gap(record: QueryRecord, end: float, *, now: float,
         open_margin: float) -> Coverage:
    """**只补尾部**：缺口 = `[记忆末端, end]`；碰到"现在"就叫 `stale`。"""
    wants_now = end >= now - open_margin
    state = "stale" if wants_now else "partial"
    tail = ("而你要的是到" + _clock(end) + "（含「现在」）→ **只补缺口**，"
            "不用旧数据冒充当前") if wants_now else f"要的是到 {_clock(end)} → 只补缺口"
    return Coverage(state=state, record=record, gap_start=record.end, gap_end=end,
                    reason=(f"记忆只到 {_clock(record.end)}，{tail}"
                            f"（已有 {record.point_count} 点，抓取于 "
                            f"{_clock(record.fetched_at)}）"))


def describe(record: QueryRecord) -> str:
    """★ **AI 读到的摘要**（"某时间窗口的查询数据"）：窗口 + 抓取时间 + 点数。"""
    tail = "；★ 该数据由多段拼接" if record.stitched else ""
    return (f"{record.entity or '（无实体）'} 的 {record.signal or '（无信号）'}："
            f"已有 **{_clock(record.start)} ~ {_clock(record.end)}** 的数据"
            f"（{record.point_count} 点、step {record.step}、"
            f"抓取于 {_clock(record.fetched_at)}，源 {record.source or '?'}）{tail}")


def records_payload(records: Sequence[QueryRecord]) -> list[dict[str, Any]]:
    """按"最近抓取优先"截断后有界地序列化（会话状态里不许无界增长）。"""
    ordered = sorted(records, key=lambda item: item.fetched_at, reverse=True)
    return [record.to_payload() for record in ordered[:MAX_RECORDS]]


def load_records(state: Mapping[str, Any]) -> list[QueryRecord]:
    """从会话状态里读回查询记忆（读不到 → 空列表，**不猜**）。"""
    raw = state.get(MEMORY_KEY) if isinstance(state, Mapping) else None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    out: list[QueryRecord] = []
    for item in raw:
        if isinstance(item, Mapping):
            out.append(QueryRecord.from_payload(item))
    return out


def _clock(epoch: float) -> str:
    """epoch 秒 → 本地 `MM-DD HH:MM`（留痕用；拿不到就原样）。"""
    try:
        import time

        return time.strftime("%m-%d %H:%M", time.localtime(float(epoch or 0.0)))
    except (OSError, ValueError):        # pragma: no cover —— 极端时间戳
        return str(epoch)


# ======================================================================
#  ★ 作用域（哪次会话）：ContextVar —— 与 `api/constants._USAGE` 同一手法
# ======================================================================
#  ★ 为什么用 ContextVar 而不是给 `ToolContext` 加一个可变字段：执行器是**跨会话共享**的，
#    可变字段在并发下会串味（A 会话的查询记录写进 B 会话）。ContextVar 的值在
#    `asyncio` 任务创建时**被拷贝**，所以"每次 run 设一次、子任务里读"是正确的。
CURRENT_SCOPE: ContextVar[Optional[str]] = ContextVar("aiops_query_scope", default=None)


def scope_of() -> Optional[str]:
    """当前作用域（没有就 `None` → 取数层**不碰记忆**，行为与没有取数记忆时一致）。"""
    return CURRENT_SCOPE.get()


def use_scope(scope: Optional[str]) -> Any:
    """把当前作用域设成 `scope`，退出时还原（`with` 用）。"""
    token = CURRENT_SCOPE.set(scope or None)
    return _ScopeReset(token)


class _ScopeReset:
    __slots__ = ("_token",)

    def __init__(self, token: Any) -> None:
        self._token = token

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> None:
        CURRENT_SCOPE.reset(self._token)


class QueryMemory:
    """**查询数据记忆**：按作用域（会话）键控的索引 + 可选落进会话工作记忆。

    ★ 只存**索引**（窗口 / 抓取时间 / 点数 / 接缝），数据本体在取数缓存的本地落盘里 ——
      这样"AI 看得见有哪些数据"与"数据很重"这两件事就分开了。
    ★ 失败一律 fail-open：取不到就当没有记忆（去查真实数据），**不许**让取数失败。
    """

    def __init__(self, *, now: Optional[Callable[[], float]] = None) -> None:
        self._records: dict[str, list[QueryRecord]] = {}
        self._now = now or time.time

    # ------------------------------------------------------------------
    def records(self, scope: str) -> list[QueryRecord]:
        return list(self._records.get(str(scope), ()))

    def remember(self, scope: str, record: QueryRecord, *, replace: bool = False) -> None:
        """记下/更新一条（**同一条语句合并成一条**：窗口取并集、接缝保留）。"""
        key = str(scope)
        items = [item for item in self._records.get(key, ())
                 if item.query != record.query or item.source != record.source]
        previous = next((item for item in self._records.get(key, ())
                         if item.query == record.query and item.source == record.source),
                        None)
        merged = _merge_records(previous, record) if previous is not None and not replace else record
        items.append(merged)
        items = sorted(items, key=lambda item: item.fetched_at, reverse=True)[:MAX_RECORDS]
        self._records[key] = items

    def hydrate(self, scope: str, state: Mapping[str, Any]) -> int:
        """从会话工作记忆里读回索引（返回读回条数；读不到 → 0，**不猜**）。"""
        loaded = load_records(state)
        if loaded:
            self._records[str(scope)] = loaded
        return len(loaded)

    def payload(self, scope: str) -> list[dict[str, Any]]:
        """给会话工作记忆的**有界**序列化（`MAX_RECORDS` 条、最近抓取优先）。"""
        return records_payload(self.records(scope))

    def describe_all(self, scope: str) -> list[str]:
        """★ 要被召回/进提示词的摘要行（AI 就是靠这些知道"记忆里有哪些窗口的数据"）。"""
        return [describe(record) for record in self.records(scope)]


def _merge_records(older: Optional[QueryRecord], newer: QueryRecord) -> QueryRecord:
    """把新旧两条并成一条（**接缝保留**，红线 3）。"""
    if older is None:
        return newer
    start = min(older.start, newer.start)
    end = max(older.end, newer.end)
    segments = tuple(dict.fromkeys(tuple(older.segments or ((older.start, older.end),))
                                   + tuple(newer.segments or ((newer.start, newer.end),))))
    return QueryRecord(
        signal=newer.signal or older.signal, entity=newer.entity or older.entity,
        source=newer.source, query=newer.query, step=newer.step,
        start=start, end=end, fetched_at=max(older.fetched_at, newer.fetched_at),
        point_count=older.point_count + newer.point_count,
        segments=segments,
        raw_ref=(f"{older.raw_ref} || {newer.raw_ref}" if older.raw_ref else newer.raw_ref),
        summary=newer.summary or older.summary)
