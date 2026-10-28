"""Trace Agent —— 耗时归因 + **client/server span 对比**。

--------------------------------------------------------------------------------
★ 为什么必须查两路（server / client）
--------------------------------------------------------------------------------

一次跨服务调用的耗时，在两个地方各有一份记录：

| span | 谁产生的 | 它包含的时间 |
|---|---|---|
| `kind = server` | **被调方**（如 payment） | 真正干活的时间（含它自己的下游） |
| `kind = client` | **调用方**（如 order） | 干活时间 **+ 排队/建连/网络** |

两者之差就是"**花在服务端之外的时间**"——连接池排队、TCP 建连、重试退避
几乎都藏在这里。只看一路的话，"order 慢"和"payment 慢"根本分不开：
这正是"耗时归因"要回答的问题。

★ **如实限制**（写在这里，避免以后有人以为它做了更多）：
  我们用的是 Tempo 的 `/api/search`（只给每条 trace 的摘要），所以对比是
  **聚合对比**（客户端最慢 vs 服务端最慢），不是"同一条 trace 内逐 span 配对"。
  逐 span 配对需要按 trace_id 拉完整链路（`/api/traces/{id}`），留到需要时再加。
  因此本 Agent 的 gap 证据置信度**低于**指标证据，且 summary 里写明是聚合口径。
"""

from __future__ import annotations

import logging
import re
from statistics import median
from typing import Any, Final, Mapping, Optional, Sequence

from ..attribution.errors import TRACE_ID_TAG_PREFIX, ordered_trace_ids
from ..attribution.trace_spans import breakpoint_of, span_label, spans_from_result
from ..common.contracts import Evidence
from ..common.enums import EvidenceDirection, EvidenceSource
from .base import (
    AgentContext,
    AgentOutcome,
    ForensicsAgent,
    ToolRequest,
    cap_note,
    target_services,
    to_datetime,
)

__all__ = ["TraceAgent", "SLOW_TRACE_MS", "CLIENT_SERVER_GAP_MS", "MAX_DRILLDOWN_TRACES",
           "MAX_STORED_TRACES"]

logger = logging.getLogger("agent.agents.trace_agent")

#: "慢到值得看一眼"的门槛（毫秒）。★ 这是**看一眼的门槛**，不是"确诊阈值"：
#: 最终"这次慢是不是根因"由 RCA 打分裁决。
SLOW_TRACE_MS: Final[float] = 500.0

#: 客户端比服务端"多花"多少毫秒才值得记一条 gap 证据
CLIENT_SERVER_GAP_MS: Final[float] = 100.0

#: 一轮排查最多按 trace_id 下钻几条 trace。
#: ★ 为什么要有界：日志聚类里 1~3 条样本各带一个 trace_id，若原样全下钻，
#:   一次排查会多出十几条 `trace_read`。取 3 条足够定位"断在哪一跳"，
#:   而且**多余的那些要在 aux 里留痕**（不能静默丢掉）。
MAX_DRILLDOWN_TRACES: Final[int] = 3

#: 本 Agent 实例上最多**存**几条下钻回来的 trace（供编排层枚举候选 / 拼 trace JSON）。
#: ★ 为什么要有界：一次排查最多下钻 `MAX_DRILLDOWN_TRACES` 条，但 Agent 实例可能在
#:   同一个进程里被多次 run 复用 —— 不设上限就越攒越多。
MAX_STORED_TRACES: Final[int] = 8

_KINDS: Final[tuple[tuple[str, str], ...]] = (
    ("server", "服务端自报（真正干活的时间）"),
    ("client", "调用方视角（含排队/建连/网络）"),
)


class TraceAgent(ForensicsAgent):
    """链路取证：慢在哪一跳、以及"服务端之外"花了多少。

    ★ 两个工具是**同一条证据链上的两个粒度**，不是两路数据源：
      `traceql_read` 扫窗口（有哪些 trace、谁慢），`trace_read` 按 id 取整条
      （这条 trace 里断在哪一跳）。下钻的入参（trace_id）来自**日志**证据，
      所以它跟着「这一轮的日志证据」走 —— 运行器把排好序的 trace_id 递进来，
      **同一轮内**就发 `trace_read`（`followup_requests` 的 `hints`）。
    """

    role = "trace_agent"
    source = EvidenceSource.TRACES
    tools = ("traceql_read", "trace_read")

    def __init__(self) -> None:
        #: ★★ 下钻回来的 span 树（**原样** `trace_read` 的产物）。
        #:   为什么要存：候选与 trace JSON 必须从**真的取回来的那条链路**枚举，
        #:   而运行器把下钻结果直接并进证据、**不保留原始返回**（证据里只有一句话的
        #:   断点结论）。没有这份留存，"候选由代码枚举"就无从谈起。
        #:   ★ 它只在本进程内、只服务本次排查：`store_trace()` 由运行器在**同一轮内**
        #:   调用，编排层跑完这一轮就取走。
        self._traces: list[dict[str, Any]] = []

    def store_trace(self, result: Any) -> None:
        """把一次 `trace_read` 的产物收下来（有界、去重、**不编** id）。"""
        data = getattr(result, "data", None)
        if not isinstance(data, Mapping):
            return
        spans = data.get("spans")
        if not isinstance(spans, Sequence) or isinstance(spans, (str, bytes)) or not spans:
            return
        trace_id = str(data.get("trace_id") or "").strip()
        record = {"trace_id": trace_id, "spans": [dict(item) for item in spans
                                                  if isinstance(item, Mapping)],
                  "truncated": bool(data.get("truncated"))}
        if not record["spans"]:
            return
        self._traces = [item for item in self._traces
                        if not (trace_id and item.get("trace_id") == trace_id)]
        self._traces.append(record)
        del self._traces[:-MAX_STORED_TRACES]

    def stored_traces(self) -> list[dict[str, Any]]:
        """本轮收下来的 trace（浅拷贝 —— 调用方拿去拼 JSON，不改这里）。"""
        return [dict(item) for item in self._traces]

    def clear_traces(self) -> None:
        """丢掉已收下的 trace（编排层跑完一轮后调用，跨轮不串味）。"""
        self._traces = []

    def plan_calls(self, ctx: AgentContext) -> list[ToolRequest]:
        services = target_services(ctx)
        if not services:
            return []
        pattern = _service_pattern(services)
        dropped = cap_note(ctx, services)
        return [ToolRequest(
            tool="traceql_read",
            params={"query": f'{{ resource.service.name =~ "{pattern}" && kind = {kind} }}',
                    **ctx.query_window(), "limit": 20},
            signal=f"trace_duration_{kind}",
            rationale=f"取 {kind} span 的耗时分布：{desc}",
            dropped=dropped)
            for kind, desc in _KINDS]

    # ------------------------------------------------------------------
    def _duration_evidence(self, ctx: AgentContext, request: ToolRequest,
                           result: Any) -> list[Evidence]:
        """耗时归因：慢在哪一跳、以及"服务端之外"花了多少。"""
        traces = _traces(result)
        if not traces:
            return []
        durations = [float(t["duration_ms"]) for t in traces
                     if isinstance(t.get("duration_ms"), (int, float))]
        if not durations:
            return []
        baseline = median(durations)
        slowest = max(traces, key=lambda t: float(t.get("duration_ms") or 0))
        slowest_ms = float(slowest.get("duration_ms") or 0)

        kind = request.signal.rsplit("_", 1)[-1]
        entity = ctx.entity(str(slowest.get("root_service") or "")) or ctx.entity()
        if entity is None:
            return []

        if slowest_ms - baseline >= SLOW_TRACE_MS:
            # 只有个别 trace 慢 → 拐点 = 这条最慢 trace 的开始时刻
            onset = to_datetime(slowest.get("start_ns"), default=ctx.window_start)
            return [self.make_evidence(
                ctx, signal=request.signal, direction=EvidenceDirection.SPIKE,
                onset=onset, baseline=round(baseline, 3),
                magnitude=round(slowest_ms - baseline, 3), magnitude_unit="ms",
                entity=entity, confidence=0.75,
                raw_ref=f"{getattr(result, 'raw_ref', '') or request.tool}"
                        f" | trace={slowest.get('trace_id')}",
                summary=(f"{kind} span：最慢 {slowest_ms:.0f}ms"
                         f"（中位 {baseline:.0f}ms，慢 {slowest_ms - baseline:.0f}ms）"
                         f" root={slowest.get('root_service')} "
                         f"span={slowest.get('root_span')}"),
                tags=["traces", kind])]

        if baseline >= SLOW_TRACE_MS:
            # 全都慢 → 不是个别毛刺，而是**这一路整体劣化**（拐点取最早的一条）
            earliest = min(traces, key=lambda t: float(t.get("start_ns") or 0))
            return [self.make_evidence(
                ctx, signal=request.signal, direction=EvidenceDirection.UP,
                onset=to_datetime(earliest.get("start_ns"), default=ctx.window_start),
                baseline=round(baseline, 3), magnitude=round(baseline, 3),
                magnitude_unit="ms", entity=entity, confidence=0.72,
                raw_ref=(f"{getattr(result, 'raw_ref', '') or request.tool}"
                         f" | traces={len(traces)}"),
                summary=(f"{kind} span **整体偏慢**：{len(traces)} 条 trace 的中位耗时"
                         f" {baseline:.0f}ms 已超过门槛 {SLOW_TRACE_MS:.0f}ms"),
                tags=["traces", kind, "widespread"])]
        return []

    # ------------------------------------------------------------------
    def combine(self, ctx: AgentContext, results: Mapping[str, Any]) -> list[Evidence]:
        """两路都拿到之后，做**客户端 vs 服务端**的对比（跨调用的唯一产物）。

        ★ 这是本 Agent 内部的调用间对比，**不是**两个 Agent 之间的通信
          （`AgentContext` 里依然没有别的 Agent 的任何东西）。
        """
        client = _stats_of(results.get("trace_duration_client"))
        server = _stats_of(results.get("trace_duration_server"))
        if not client or not server:
            return []
        client_max, server_max = client["max"], server["max"]
        gap = client_max - server_max
        if gap < CLIENT_SERVER_GAP_MS:
            return []
        entity = ctx.entity()
        if entity is None:
            return []
        pattern = _service_pattern(target_services(ctx))
        return [self.make_evidence(
            ctx, signal="client_server_gap", direction=EvidenceDirection.UP,
            onset=ctx.window_start,
            baseline=round(server_max, 3), magnitude=round(gap, 3), magnitude_unit="ms",
            entity=entity, confidence=0.65,
            # ★ `raw_ref` 必须**可回查**（契约强制带 `:` 且要能重跑）：
            #   所以把它拼成一条真的 TraceQL（两条 kind 都写进去）
            raw_ref=(f'traceql: {{ resource.service.name =~ "{pattern}" && '
                     f'kind = client|server }} | 聚合对比 client_max={client_max:.0f}ms'
                     f" server_max={server_max:.0f}ms"),
            summary=(f"客户端侧最慢 {client_max:.0f}ms，服务端自报最慢 {server_max:.0f}ms，"
                     f"差 {gap:.0f}ms —— 这段时间花在**服务端之外**"
                     f"（连接池排队/建连/网络；聚合口径，非逐 span 配对）"),
            tags=["traces", "client_server_gap"])]

    # ------------------------------------------------------------------
    #  错误 → trace 下钻（确定性：按 trace_id 精确取整条，再跑纯函数判定断点）
    # ------------------------------------------------------------------
    def followup_requests(self, ctx: AgentContext, outcome: AgentOutcome,
                          hints: Sequence[str] = ()) -> list[ToolRequest]:
        """**本轮内**就下钻：按 trace_id 精确取整条链路（不是等下一轮）。

        Args:
            hints: ★★ 同一轮里**别的取证 Agent**（日志那一路）带出来的 trace_id，
                由运行器按"错误条数降序 / 首现"排好次序递进来。**本 Agent 看不到
                别的 Agent 的证据**（隔离是接口层面的事实）—— 所以这条线索只能由
                运行器转交，而不是自己去翻黑板。
                ★ 修前这里只读 `outcome`（**本 Agent 自己**的证据，也就是耗时那两条
                  `traceql_read`）—— 而 trace_id 在**日志**的证据里，于是真机第 1 轮
                  就定案、下钻**永远不会触发**。

        ★★ 为什么"读 trace_id"这件事必须由代码做、而不是让模型从证据文本里抄：
          抄错一位就查到另一条 trace（或者什么都查不到），而结论会照样下 ——
          那一错就是"把 A 的断点安到 B 头上"。tags 里的 `trace_id=` 是结构化字段。
        ★ 有界：最多 `MAX_DRILLDOWN_TRACES` 条（次序 = 错误条数/首现），
          多出来的写进 `aux`（有界要留痕，不能静默少查）。
        """
        ids = _ranked_ids(outcome, hints)
        if not ids:
            return []
        requests = [ToolRequest(
            tool="trace_read", params={"trace_id": trace_id},
            signal="trace_breakpoint",
            rationale=("按日志聚类带出的 trace_id 精确取整条链路："
                       "要判定「错误断在哪一跳」，不是再扫一遍窗口"),
            purpose="evidence") for trace_id in ids[:MAX_DRILLDOWN_TRACES]]
        dropped = len(ids) - len(requests)
        if dropped > 0:
            # ★ 有界就要留痕：少了 N 条下钻是**口径**，不是"本来就只有这几条"
            outcome.aux.append(
                f"trace_read：另有 {dropped} 条 trace_id 未下钻"
                f"（上限 {MAX_DRILLDOWN_TRACES} 条/轮）—— 这是**有界**，不是没有")
        return requests

    def build_evidence(self, ctx: AgentContext, request: ToolRequest,
                       result: Any) -> list[Evidence]:
        """`trace_read` 的结果 → `trace_breakpoint` 证据；其余走耗时归因那条。"""
        if request.tool == "trace_read":
            return self.breakpoint_evidence(ctx, result)
        return self._duration_evidence(ctx, request, result)

    def breakpoint_evidence(self, ctx: AgentContext, result: Any) -> list[Evidence]:
        """把一条 trace 的 span 树判成 `trace_breakpoint` 证据（纯函数判定）。

        ★ `entity` = **断点 span 所在的服务** —— 不是打日志的那个服务，
          也不是上下文里的第一个服务。归因主语就靠这个字段落地。
        ★ 判定不出来（树里没有 error span / 没有可判读的叶子）→ **不产证据**
          （走 `empty`），绝不编一个"大概是这里"。
        """
        verdict = breakpoint_of(getattr(result, "data", None))
        if not verdict.get("found"):
            logger.info("trace 下钻没有判定出断点：%s", verdict.get("note"))
            return []
        span = verdict["span"]
        service = str(verdict.get("service") or "")
        entity = ctx.entity(service) if service else None
        if entity is None:
            return []
        trace_id = str((getattr(result, "data", None) or {}).get("trace_id") or "")
        kind = str(verdict["kind"])
        tags = ["traces", "trace_breakpoint", kind, f"span_name={span.get('name') or ''}"]
        if verdict.get("heuristic"):
            tags.append("heuristic")
        if trace_id:
            tags.append(f"{TRACE_ID_TAG_PREFIX}{trace_id}")
        statement = _statement_of(span)
        return [self.make_evidence(
            ctx, signal="trace_breakpoint", direction=EvidenceDirection.FLAT,
            onset=ctx.window_start, entity=entity,
            confidence=float(verdict["confidence"]),
            raw_ref=(f'traceql: {{ trace:id = "{trace_id}" }}'
                     if trace_id else str(getattr(result, "raw_ref", "") or "traceql: trace_read")),
            summary=(f"断点在 {span_label(span)}（{kind}"
                     f"{'，**启发式**' if verdict.get('heuristic') else ''}）："
                     f"{verdict.get('note')}"
                     + (f"；语句/路由：{statement}" if statement else "")),
            tags=tags)]


def _trace_ids(evidence: Sequence[Any]) -> list[str]:
    """证据里带出的 trace_id（按**证据出现次序**去重 —— 不排序、不猜）。"""
    out: list[str] = []
    for item in evidence or ():
        for tag in list(getattr(item, "tags", None) or ()):
            text = str(tag or "")
            if not text.startswith(TRACE_ID_TAG_PREFIX):
                continue
            trace_id = text[len(TRACE_ID_TAG_PREFIX):].strip()
            if trace_id and trace_id not in out:
                out.append(trace_id)
    return out


def _ranked_ids(outcome: AgentOutcome, hints: Sequence[str]) -> list[str]:
    """下钻次序：**先别的 Agent 按错误条数/首现排好的 trace_id**，再补自己证据里的。

    ★ 两条来源都要，且**都不许编**：`hints` 是运行器从**同一轮的全部证据**里按
      `ordered_trace_ids` 抽出来并排好序的（trace_id 长在日志那一路的证据里，
      本 Agent 看不到别的 Agent 的证据 —— 线索只能由运行器转交）；
      自己证据里的那些作为兜底 —— 否则"这一轮只有 trace 没有 log"时反而一条都不下钻。
    """
    out: list[str] = []
    for trace_id in list(hints or ()) + _trace_ids(
            list(getattr(outcome, "evidence", None) or [])):
        clean = str(trace_id or "").strip().lower()
        if clean and clean not in out and _looks_like_trace_id(clean):
            out.append(clean)
    return out


#: trace_id 的形状（32 位小写十六进制；Accept 了采样器给出的两种长度）。
_TRACE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{16}$|^[0-9a-f]{32}$")


def _looks_like_trace_id(value: str) -> bool:
    """这个字符串**长得像** trace_id 吗 —— 不像就别拿去查（fail-closed）。

    ★ 为什么要有这一条：拿一个"看着像 id"的字符串（服务名、错误码、空串的兄弟）
      去发一次查询，只会把一次**注定失败**的调用算进工具预算，而节点上什么也不会多。
      真正的 trace_id 是十六进制定长串；**校验形状**比"先查一次看看"便宜得多。
    """
    return bool(_TRACE_ID_RE.match(str(value or "").strip().lower()))


def _statement_of(span: Mapping[str, Any]) -> str:
    """断点 span 上的"哪条语句/路由"（拿不到就空 —— 不编）。"""
    attrs = span.get("attributes") or {}
    for key in ("db.statement", "http.route", "rpc.method", "messaging.destination"):
        value = str(attrs.get(key) or "").strip()
        if value:
            return value
    return ""


# ★ `spans_from_result` 在本模块只被 `breakpoint_of` 间接用到；显式引用一次，
#   让"下钻链路依赖哪个解析器"在导入面上看得见（也在别处复用它做判据）。
_SPANS = spans_from_result


# ======================================================================
def _traces(result: Any) -> list[Mapping[str, Any]]:
    data = getattr(result, "data", None)
    items = (data or {}).get("traces") if isinstance(data, Mapping) else None
    return [item for item in (items or []) if isinstance(item, Mapping)]


def _stats_of(result: Any) -> Optional[dict[str, float]]:
    if result is None or not getattr(result, "ok", False):
        return None
    data = getattr(result, "data", None)
    stats = (data or {}).get("duration_ms") if isinstance(data, Mapping) else None
    if not isinstance(stats, Mapping) or not stats:
        return None
    return {"max": float(stats.get("max") or 0.0),
            "avg": float(stats.get("avg") or 0.0),
            "count": float(stats.get("count") or 0.0)}


def _service_pattern(services: Sequence[str]) -> str:
    from .query_labels import service_pattern
    return service_pattern(services)
