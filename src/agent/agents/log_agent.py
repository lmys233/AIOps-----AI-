"""Log Agent —— 错误模式聚类 + 首现时间 + 频次。

--------------------------------------------------------------------------------
★ 它只回答"日志里有没有错误模式"，不回答"为什么"
--------------------------------------------------------------------------------

`logql_read` 的摘要化（`summarize_logs`）已经把日志聚成了
`(service, level, error_type)` 三元的**模式**，并给出首现/末次时间与频次。
本 Agent 只做两件事：

1. **筛**：只有 `ERROR`/`FATAL`（或带 `error_type`）的模式才算证据，
   INFO/WARNING 的普通日志不进黑板 —— 否则黑板会被"正常日志"塞满，
   RCA 打分被稀释（传递的必须是结构化证据，不是原始日志）。
2. **转**：把模式转成 `Evidence`（`onset` = **首现时间**，`magnitude` = 出现行数）。

★ 首现时间必须真的是"最早那次"：Loki 默认从新到旧返回，按遍历顺序取第一条
  会拿到**最新**时间（`summarize_logs` 已修掉，这里直接信任摘要）。

★ 查了没有 ERROR → `AgentOutcome.empty`（"排除应用层报错爆发"），
  **不是**"没查成"。这条分界线必须一直守住。
"""

from __future__ import annotations

import re
from typing import Any, Final, Mapping, Sequence

from ..attribution.errors import parse_error, propagated
from ..attribution.fingerprint import FINGERPRINT_TAG_PREFIX, error_fingerprint
from ..common.contracts import Evidence
from ..common.enums import EvidenceDirection, EvidenceSource
from .base import (
    AgentContext,
    ForensicsAgent,
    ToolRequest,
    cap_note,
    target_services,
    to_datetime,
)

__all__ = ["LogAgent", "ERROR_LEVELS", "PROPAGATED_TAG", "SELF_BURST_TAG"]

#: 算"错误"的日志级别（FATAL 比 ERROR 更硬，置信度上给一点差别）
ERROR_LEVELS: Final[frozenset[str]] = frozenset({"ERROR", "FATAL", "CRITICAL"})

#: 日志一行最多引用多少字符（进摘要，不进上下文全文）
_SAMPLE_CHARS: Final[int] = 160

#: "这条错误是**替下游报错**"的证据标记（归因层据此不把它算成本服务的爆发）
PROPAGATED_TAG: Final[str] = "propagated"

#: "这条错误是**本服务自身**应用层爆发"的证据标记（与 `PROPAGATED_TAG` 互斥）
SELF_BURST_TAG: Final[str] = "self_error_burst"

#: 一条错误聚类最多带出几条**样本原文**（判"是不是传播"只能看样本，见 errors 模块头）
_MAX_SAMPLES: Final[int] = 3


class LogAgent(ForensicsAgent):
    """日志取证：错误模式 + 首现时间 + 频次。"""

    role = "log_agent"
    source = EvidenceSource.LOGS
    tools = ("logql_read",)

    def plan_calls(self, ctx: AgentContext) -> list[ToolRequest]:
        services = target_services(ctx)
        if not services:
            return []
        return [ToolRequest(
            tool="logql_read",
            params={"query": (f'{{service=~"{_service_pattern([name])}"}} | json '
                              '| level=~"ERROR|FATAL|CRITICAL"'),
                    **ctx.query_window(), "limit": 200,
                    "direction": "backward"},
            signal="log_error_burst",
            rationale=(f"单独采样 {name} 的错误日志，避免高频服务挤掉其他服务；"
                       "只传递错误聚类与样本，采样频次不等于窗口总次数"),
            dropped=cap_note(ctx, services) if index == 0 else "")
            for index, name in enumerate(services)]

    def summarize_empty(self, request: ToolRequest, result: Any) -> str:
        """空结果只说明这个服务的日志采样为空，不能排除其他服务的故障。"""
        query = str(request.params.get("query") or "")
        match = re.search(r'service=~"([^"]+)"', query)
        return f"log_error_burst[{match[1]}]" if match else request.signal

    # ------------------------------------------------------------------
    def build_evidence(self, ctx: AgentContext, request: ToolRequest,
                       result: Any) -> list[Evidence]:
        data = getattr(result, "data", None)
        clusters = (data or {}).get("clusters") if isinstance(data, Mapping) else None
        out: list[Evidence] = []
        for cluster in clusters or []:
            if not isinstance(cluster, Mapping):
                continue
            built = self._cluster_evidence(ctx, request, result, cluster)
            if built is not None:
                out.append(built)
        return out

    def _cluster_evidence(self, ctx: AgentContext, request: ToolRequest, result: Any,
                          cluster: Mapping[str, Any]) -> "Evidence | None":
        """一个日志聚类 → 一条证据（含 `propagated` / `self_error_burst` 与 trace_id）。"""
        level = str(cluster.get("level") or "").upper()
        declared = str(cluster.get("error_type") or "")
        sample_text = _sample_text(cluster)
        error_type = parse_error(declared, sample_text)
        if level not in ERROR_LEVELS and not declared:
            return None                      # 普通日志不进黑板
        service = str(cluster.get("service") or "")
        entity = ctx.entity(service) or ctx.entity()
        if entity is None:
            return None
        count = int(cluster.get("count") or 0)
        onset = to_datetime(cluster.get("first_ts_ns"), default=ctx.window_start)
        is_propagated = propagated(declared, sample_text)
        return self.make_evidence(
            ctx, signal=request.signal or "log_error_burst",
            direction=EvidenceDirection.SPIKE,
            onset=onset, magnitude=float(count), magnitude_unit="行",
            baseline=None, entity=entity,
            confidence=_confidence(level, error_type, count),
            raw_ref=(f"{getattr(result, 'raw_ref', '') or request.tool}"
                     f" | cluster={service}/{level or '-'}/{error_type or '-'}"),
            summary=_summary(service, level, error_type, count, cluster),
            tags=_cluster_tags(cluster, level, error_type, declared, sample_text,
                               is_propagated, service))

    # ------------------------------------------------------------------
    def self_error_burst(self, evidence: Sequence[Evidence]) -> list[Evidence]:
        """这批证据里**本服务自身**的应用层错误爆发（传播类被剔除）。

        ★ 为什么要有这个出口：归因层要判"谁在报错"，而"替下游报错"的那一条
          不能算作"本服务自己炸了" —— 见 `attribution/errors.py` 的模块头。
        """
        return [item for item in evidence
                if SELF_BURST_TAG in list(getattr(item, "tags", None) or ())]


# ======================================================================
def _sample_text(cluster: Mapping[str, Any]) -> str:
    """聚类里第一条样本的正文（判"是不是替下游报错"只能看它，不看我们渲染的摘要）。"""
    samples = cluster.get("samples") or []
    if samples and isinstance(samples[0], Mapping):
        return str(samples[0].get("msg") or "")[:_SAMPLE_CHARS]
    return ""


def _trace_ids(cluster: Mapping[str, Any]) -> list[str]:
    """聚类样本行里的 `trace_id`（结构化字段；取不到就空 —— **不许编**）。"""
    out: list[str] = []
    for sample in (cluster.get("samples") or [])[:_MAX_SAMPLES]:
        if not isinstance(sample, Mapping):
            continue
        trace_id = str(sample.get("trace_id") or "").strip()
        if trace_id and trace_id not in out:
            out.append(trace_id)
    return out


def _cluster_tags(cluster: Mapping[str, Any], level: str, error_type: str,
                  declared: str, sample_text: str, is_propagated: bool,
                  service: str = "") -> list[str]:
    """一条聚类的 tags：**结构化字段**（错误类型 / 样本 / trace_id / 传播标记 / 指纹）。

    ★ 为什么这些要进 tags 而不是只留在 summary：下游的判据（传播语义、按 id 下钻、
      指纹合并）必须读**结构化字段**。"从渲染好的摘要文本里再 parse 一遍"既脆又会
      踩到我们自己写进去的那几个标记（见 `errors.py` 里那段说明）。

    ★★ 指纹进 tags 的意义：跨轮 / 跨证据源去重时，指纹是**唯一的合并键**。
      没有它，同一条错误在"日志聚类"与"trace 断点"里各算一次，次数就翻倍了。
    """
    tags = ["logs"]
    for value in (level or None, error_type or None):
        if value:
            tags.append(str(value))
    tags.append(f"error_type={declared or error_type}")
    if sample_text:
        tags.append(f"sample={sample_text}")
    tags.append(PROPAGATED_TAG if is_propagated else SELF_BURST_TAG)
    # ★ 指纹用**这条聚类自己的**归一化键：服务 + 级别 + 错误类型 + 归一化后的样本原文
    tags.append(f"{FINGERPRINT_TAG_PREFIX}{error_fingerprint(service, level, error_type, sample_text)}")
    tags.extend(f"trace_id={trace_id}" for trace_id in _trace_ids(cluster))
    return tags


# ======================================================================
def _service_pattern(services: list[str]) -> str:
    from .query_labels import service_pattern
    return service_pattern(services)


def _confidence(level: str, error_type: str, count: int) -> float:
    """日志是**有噪声**的证据源：同样的频次，置信度上限比指标低。"""
    score = 0.70
    if error_type:
        score += 0.10
    if level == "FATAL":
        score += 0.05
    if count >= 3:
        score += 0.05
    return min(score, 0.90)


def _summary(service: str, level: str, error_type: str, count: int,
             cluster: Mapping[str, Any]) -> str:
    first = _short_time(cluster.get("first_ts_ns"))
    last = _short_time(cluster.get("last_ts_ns"))
    sample = ""
    samples = cluster.get("samples") or []
    if samples and isinstance(samples[0], Mapping):
        sample = str(samples[0].get("msg") or "")[:_SAMPLE_CHARS]
    parts = [f"{service} 在窗口内出现 {count} 行 {level or '（未标级别）'} 日志"]
    if error_type:
        parts.append(f"error_type={error_type}")
    parts.append(f"首现 {first} / 末次 {last}")
    if sample:
        parts.append(f"样本：{sample}")
    return "；".join(parts)


def _short_time(raw: Any) -> str:
    if not raw:
        return "（未知）"
    return to_datetime(raw).isoformat()
