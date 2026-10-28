"""错误证据的**跨服务结构化汇总**与**传播语义**（纯函数，不碰遥测、不调模型）。

--------------------------------------------------------------------------------
★ 为什么需要它：证据里有、事实里没有
--------------------------------------------------------------------------------

日志取证早就把"哪个服务、什么错误、多少行"采回来了，但那些信息**只以文本形式**
躺在 `Evidence.summary` 里。答案层能看到的 `facts` 里没有 (service, error_type) 的计数，
于是模型只能说"事实里没给错误类型" —— 明明查到了却说没有。

本模块把**已经采到的证据**再算一遍，产出可直接渲染的结构：

  · `error_summary.rows`       —— `(service, error_type)` → 计数 / 首现时刻 / 样本 / trace_id
  · `error_summary.by_service` —— 每个服务：它报了什么错、是不是"替下游报错"
  · `subject`                  —— 该把归因主语落在哪个服务上（没有就**留空**）
  · `chain_text`               —— 跨服务因果链的人话描述（每一跳都来自结构化事实）

--------------------------------------------------------------------------------
★★ 传播类错误：主语不许落在"只是替下游喊了一声"的服务上
--------------------------------------------------------------------------------

被观测系统的日志形状是**下游的 error_type 嵌在上游的日志文本里**：

    order 报 `DOWNSTREAM_BAD_STATUS`，msg 里写着
    「下游 inventory /check 返回 HTTP 503：{"error_type":"POOL_QUERY_FAILED"}」

把它与"order 自己爆了"混为一谈，归因就会稳定地落在**最上游**那个服务上
（它离用户最近、错误最先被看见），而真正在报错的是下游。所以给这类证据一个
`propagated` 语义：它的含义是"**我在替下游报错**"，不算"本服务自身应用层错误爆发"，
也不足以把归因主语拉到自己身上。

判据两条（闭集 + 文本），都写死在这里：

  1. **闭集**：`error_type ∈ PROPAGATED_DOWNSTREAM_ERROR_TYPES`
     —— 这三个名字本身就是"下游出的问题"的语义，与文本无关；
  2. **文本**：`error_type` 为空或与 msg 里嵌的下游 `error_type` **不同**时，
     以文本里那个为准 —— 那是下游自己的错误类型，说明这一行是转发来的。

★ 第 2 条为什么要"不同才算"：如果 msg 里嵌的就是同一个 `error_type`
  （`{"error_type":"POOL_QUERY_FAILED"}` 而外层也是 `POOL_QUERY_FAILED`），
  那是**同一条错误的自我描述**，不是转发 —— 判成传播会把真凶洗掉。
"""

from __future__ import annotations

import re
from typing import Any, Final, Mapping, Optional, Sequence

from ..common.contracts import EntityRef

__all__ = [
    "PROPAGATED_DOWNSTREAM_ERROR_TYPES",
    "TRACE_ID_TAG_PREFIX",
    "propagated",
    "parse_error",
    "embedded_error_types",
    "has_trace_id",
    "error_groups",
    "summary_by_service",
    "subject_service",
    "chain_text",
    "audit_errors",
    "ordered_trace_ids",
]

#: "这是下游的问题"的三个错误类型（被观测系统实际产出的名字）。
#: ★ 它们统计上仍要计数（"谁在报错"必须完整），但**不计入自身错误爆发**。
PROPAGATED_DOWNSTREAM_ERROR_TYPES: Final[frozenset[str]] = frozenset({
    "DOWNSTREAM_TIMEOUT", "DOWNSTREAM_UNAVAILABLE", "DOWNSTREAM_BAD_STATUS",
})

#: `trace_id` 在证据 tags 里的前缀（日志聚类把它带出来供下游按 id 回取整条 trace）。
TRACE_ID_TAG_PREFIX: Final[str] = "trace_id="

#: 从日志文本里抠 `error_type` 的写法 —— 只认**结构化标记**，不猜散文。
#: 覆盖两种真实形态：JSON 里的 `"error_type":"X"` 与 `key=value` 写法。
_EMBEDDED_ERROR_TYPE: Final[re.Pattern[str]] = re.compile(
    r'"error_type"\s*:\s*"([A-Za-z0-9_.\-]+)"'
    r"|error_type\s*[=:]\s*[\"']?([A-Za-z0-9_.\-]+)")

#: 样本 / 摘要的截断长度（进上下文的东西必须有界）。
_SAMPLE_CHARS: Final[int] = 160

#: 证据里承载"错误类型"的 tag 前缀（日志 Agent 把聚类的 `error_type` 放进 tags）。
_ERROR_TYPE_TAG_PREFIX: Final[str] = "error_type="

#: 证据里承载"样本原文"的 tag 前缀。
#: ★★ 必须单独带出来，而不是去 parse `Evidence.summary` —— 摘要里**我们自己渲染过**
#:   `error_type=…` 这一段，会被 `_EMBEDDED_ERROR_TYPE` 当成"文本里嵌着下游错误类型"，
#:   于是**每一个聚类都变成传播**（包括真正在报错的那一个；真机形状实测踩到）。
#:   样本原文里只可能有**日志系统自己写的**内容，判据才成立。
_SAMPLE_TAG_PREFIX: Final[str] = "sample="


# ======================================================================
def parse_error(error_type: str, text: str = "") -> str:
    """取出这条错误证据**真正的错误类型**（拿不到返回空串，**不编**）。

    ★★ 优先级是**结构化字段优先**：聚类自己声明的 `error_type` 就是权威，
      文本里嵌的那个只在"外层没给"时才采用。
      反过来（文本优先）会把**上游转发**的那条证据记成下游的错误类型 ——
      `order` 的日志里嵌着 `inventory` 的 `POOL_QUERY_FAILED`，于是
      "order 在报什么错"变成"order 在报 POOL_QUERY_FAILED"（事实被改写）。
      文本那条路的用途是**判传播**（见 :func:`propagated`），不是改主语。
    """
    outer = str(error_type or "").strip()
    if outer:
        return outer
    embedded = embedded_error_types(text)
    return embedded[0] if embedded else ""


def propagated(error_type: str, text: str = "") -> bool:
    """这条错误证据是不是"我在替下游报错"。

    两条判据（见模块头）：

      1. 声明的 `error_type` 落在闭集里（`DOWNSTREAM_TIMEOUT/UNAVAILABLE/BAD_STATUS`）；
      2. 文本里嵌着**闭集里的**下游错误类型（判据 1 不成立时才看它）。
    """
    outer = str(error_type or "").strip()
    if outer in PROPAGATED_DOWNSTREAM_ERROR_TYPES:
        return True
    return any(name in PROPAGATED_DOWNSTREAM_ERROR_TYPES
               for name in embedded_error_types(text))


def embedded_error_types(text: str) -> list[str]:
    """文本里出现的**全部** `error_type` 取值（去重、保序；拿不到就是空）。"""
    out: list[str] = []
    for match in _EMBEDDED_ERROR_TYPE.finditer(str(text or "")):
        name = (match.group(1) or match.group(2) or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def has_trace_id(tags: Sequence[str]) -> bool:
    """这条证据带没带 `trace_id`（带了才能下钻整条 trace）。"""
    return bool(_trace_id_of(tags))


def _trace_id_of(tags: Sequence[str]) -> str:
    for tag in tags or ():
        text = str(tag or "")
        if text.startswith(TRACE_ID_TAG_PREFIX):
            return text[len(TRACE_ID_TAG_PREFIX):].strip()
    return ""


def ordered_trace_ids(evidence: Sequence[Any], *, limit: int = 0) -> list[str]:
    """证据里的 trace_id，按「错误条数降序 → 首现升序 → 首次出现次序」排（去重）。

    ★★ 为什么次序是**事实**而不是口味：一轮排查只下钻几条 trace（预算有界），
      那就必须挑"最值得看的那几条"。判据用同一份聚合实现（`error_groups`）——
      条数就是那条错误证据的 `magnitude`（= 日志行数），首现就是它的 `onset`。
      于是"先看谁"这件事**可复现、可单测**，不是随机挑的。

    ★ 拿不到 `trace_id` 的条目直接跳过（**不编** id）；`limit <= 0` 表示不截断。
    """
    best: dict[str, tuple[int, Any]] = {}
    order: list[str] = []
    for record in error_groups(evidence):
        trace_id = str(record.get("trace_id") or "").strip()
        if not trace_id:
            continue
        if trace_id not in best:
            order.append(trace_id)
        count = int(record.get("count") or 0)
        onset = record.get("onset")
        known = best.get(trace_id)
        if known is None:
            best[trace_id] = (count, onset)
            continue
        earlier = onset is not None and (known[1] is None or _earlier(onset, known[1]))
        best[trace_id] = (max(known[0], count), onset if earlier else known[1])
    ranked = sorted(order, key=lambda item: (-best[item][0], _order_key(best[item][1]),
                                             order.index(item)))
    return ranked[:limit] if limit and limit > 0 else ranked


def _tagged(tags: Sequence[str], prefix: str) -> str:
    for tag in tags or ():
        text = str(tag or "")
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return ""


def _error_type_of(item: Any) -> str:
    """从证据的 tags 里读错误类型（日志 Agent 把聚类的 `error_type` 放进 tags）。"""
    return _tagged(list(getattr(item, "tags", None) or ()), _ERROR_TYPE_TAG_PREFIX)


def _sample_text_of(item: Any) -> str:
    """从证据的 tags 里读**样本原文**（判"是不是转发"只能看它，不看我们渲染的摘要）。"""
    return _tagged(list(getattr(item, "tags", None) or ()), _SAMPLE_TAG_PREFIX)


# ======================================================================
#  记录 → 汇总
# ======================================================================
def _service_of(item: Any) -> str:
    entity = getattr(item, "entity", None)
    return str(getattr(entity, "name", "") or "")


def _incident_time(item: Any) -> Any:
    return getattr(item, "onset", None)


def error_groups(evidence: Sequence[Any], *, with_trace: bool = False) -> list[dict[str, Any]]:
    """把证据里的**错误**逐条抽成结构化记录（按 `(service, error_type)` 聚合）。

    Args:
        with_trace: 为真时只保留**带 trace_id** 的那些证据。
            ★ 下钻用得上：没有 trace_id 的证据回取不到整条链路。
    """
    records: list[dict[str, Any]] = []
    for item in evidence or ():
        if item is None:
            continue
        tags = list(getattr(item, "tags", None) or ())
        trace_id = _trace_id_of(tags)
        if with_trace and not trace_id:
            continue
        summary = str(getattr(item, "summary", "") or "")
        sample_text = _sample_text_of(item) or summary
        declared = _error_type_of(item)
        error_type = parse_error(declared, sample_text)
        if not error_type:
            continue
        records.append({
            "signal": str(getattr(item, "signal", "") or ""),
            "service": _service_of(item),
            # ★ `operation` 取"信号名"——它是证据里唯一的结构化"哪个位置"字段
            #   （`log_error_burst` / `trace_breakpoint` …）。进了指纹，
            #   同一个服务的"错误爆发"与"断点"才不会被并成同一条。
            "operation": str(getattr(item, "signal", "") or ""),
            "error_type": error_type,
            "count": int(max(1.0, float(getattr(item, "magnitude", 0.0) or 0.0))),
            "onset": _incident_time(item),
            "onset_iso": _iso(_incident_time(item)),
            "sample": (sample_text or summary)[:_SAMPLE_CHARS],
            "trace_id": trace_id,
            "propagated": propagated(declared, sample_text),
            "confidence": float(getattr(item, "confidence", 0.0) or 0.0),
        })
    return records


def summary_by_service(records: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """按服务汇总：它报了什么错、是不是**只有**传播证据。

    ★ `rows` 按"首现时刻升序、错误类型字典序"排：**谁先开始报错**要一眼看得出来
      （因果方向的第一步）。
    """
    grouped: dict[str, dict[str, Any]] = {}
    for record in records or ():
        service = str(record.get("service") or "")
        error_type = str(record.get("error_type") or "")
        if not service or not error_type:
            continue
        entry = grouped.setdefault(service, {"counts": {}, "onsets": {}, "samples": {},
                                            "traces": {}, "sources": {}})
        entry["counts"][error_type] = entry["counts"].get(error_type, 0) + int(
            record.get("count") or 0)
        entry["sources"][error_type] = (entry["sources"].get(error_type)
                                        or bool(record.get("propagated")))
        onset = record.get("onset")
        known = entry["onsets"].get(error_type)
        if onset is not None and (known is None or _earlier(onset, known)):
            entry["onsets"][error_type] = onset
        entry["samples"].setdefault(error_type, str(record.get("sample") or ""))
        if record.get("trace_id"):
            entry["traces"].setdefault(error_type, []).append(str(record["trace_id"]))

    out: dict[str, dict[str, Any]] = {}
    for service, entry in grouped.items():
        types = sorted(entry["counts"], key=lambda name: (
            _order_key(entry["onsets"].get(name)), name))
        all_propagated = all(entry["sources"].get(name, False) for name in types)
        out[service] = {
            "service": service,
            "error_types": types,
            "by_error_type": {name: entry["counts"][name] for name in types},
            "first_seen": {name: _iso(entry["onsets"].get(name)) for name in types},
            "onsets": {name: entry["onsets"].get(name) for name in types},
            "samples": {name: entry["samples"].get(name, "") for name in types},
            "trace_ids": {name: list(dict.fromkeys(entry["traces"].get(name) or []))
                          for name in types},
            "self": [name for name in types if not entry["sources"].get(name, False)],
            "propagated": [name for name in types if entry["sources"].get(name, False)],
            "only_propagated": all_propagated,
        }
    return out


def _order_key(onset: Any) -> tuple[int, float]:
    """排序键：有时刻的按时刻升序排前面，没时刻的排最后（**不编**一个时刻）。"""
    if onset is None:
        return (1, 0.0)
    return (0, float(getattr(onset, "timestamp", lambda: 0.0)()))


def _earlier(left: Any, right: Any) -> bool:
    return _order_key(left) < _order_key(right)


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else ""


# ======================================================================
#  主语与因果链
# ======================================================================
def subject_service(by_service: Mapping[str, Mapping[str, Any]], *,
                    cluster: Optional[str] = None,
                    breakpoint_service: str = "") -> tuple[Optional[EntityRef], str]:
    """该把归因主语落在谁身上 → `(实体或 None, 依据)`。

    顺序（**先硬事实、后启发**）：

      1. 有 `trace_breakpoint` → 用**断点 span 的服务**（"断在哪一跳"是查出来的事实）；
      2. 否则按"**自身**错误证据的最早首现"，并列时按服务名字典序（确定性）；
      3. 一个都没有（只有传播证据）→ **返回 None**，由调用方如实降级。

    ★ 第 3 条是刻意的：宁可留空 + 写明"取不到"，也不许把主语扣在最上游那个服务头上。
    """
    if breakpoint_service:
        return (EntityRef.make("service", breakpoint_service, cluster),
                "trace_breakpoint")
    committed = [name for name, item in (by_service or {}).items()
                 if item.get("self") and not item.get("only_propagated")]
    if not committed:
        return None, "propagated_only"
    first = {name: min(_order_key(by_service[name]["onsets"].get(error_type))
                       for error_type in by_service[name]["self"])
             for name in committed}
    order = sorted(committed, key=lambda name: (first[name], name))
    return EntityRef.make("service", order[0], cluster), "self_error_earliest"


def chain_text(by_service: Mapping[str, Mapping[str, Any]]) -> str:
    """跨服务因果链的人话描述（**每一跳都来自结构化事实**）。

    形态：`先爆的服务报了什么错 → 谁只是转发它的错`。只写事实里有的关系 ——
    上游的 `propagated` 证据与下游的 `self` 错误同时存在时，才连一条边。
    """
    sources = [name for name, item in (by_service or {}).items() if item.get("self")]
    propagators = [name for name, item in (by_service or {}).items()
                   if item.get("only_propagated")]
    hops: list[str] = []
    for name in sorted(sources, key=lambda item: (_self_order(by_service[item]), item)):
        item = by_service[name]
        pairs = "、".join(f"{error}(×{item['by_error_type'][error]})" for error in item["self"])
        hops.append(f"{name} 自身报错 {pairs}")
    for name in sorted(propagators):
        for error in by_service[name]["propagated"]:
            hops.append(f"{name} 只是转发下游报错（{error}）")
    if not hops:
        return ""
    return " → ".join(hops)


def _self_order(item: Mapping[str, Any]) -> tuple[int, float]:
    """一个服务的排序键：它**自身**错误里最早的首现时刻。"""
    keys = [_order_key(item["onsets"].get(error)) for error in item["self"]]
    return min(keys) if keys else (1, 0.0)


# ======================================================================
def audit_errors(evidence: Sequence[Any], *, slots: Any = None,
                 breakpoint_service: str = "",
                 candidates: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """一次排查的**错误审计**：汇总 + 主语 + 因果链 + 降级说明。

    ★ 纯函数：只看传进来的证据，不读黑板、不调模型、不看别的 Agent 的结论。
      任何"谁是主语"的判断都必须在这里可复现。

    Args:
        candidates: ★ 由**代码枚举**的错误候选（见 `error_facts.error_candidates`：
            每个带 `intrinsic` / `propagated` / `slow` 标记）。它们与证据里的
            `(服务, 错误类型)` 汇总是**两个粒度**：汇总是"谁在报错"，候选是
            "trace 里具体断在哪个位置"。两者都进 `error_summary`，各自标明来源。
    """
    cluster = getattr(slots, "cluster", None)
    records = error_groups(evidence)
    by_service = summary_by_service(records)
    subject, because = subject_service(by_service, cluster=cluster,
                                       breakpoint_service=breakpoint_service)
    rows = [_row(record) for record in records]
    rows.sort(key=lambda row: (_order_key(row["_onset"]), row["service"], row["error_type"]))
    return {
        "error_summary": {
            "rows": [{key: value for key, value in row.items() if key != "_onset"}
                     for row in rows],
            "by_service": [by_service[name] for name in sorted(by_service)],
            "services_with_self_errors": sorted(
                name for name, item in by_service.items() if item["self"]),
            "services_propagated_only": sorted(
                name for name, item in by_service.items() if item["only_propagated"]),
            # ★★ 候选（代码枚举、带标记）+ 指纹（同一错误合并后的 count / 首现 / 样本 trace）
            "candidates": [dict(item) for item in candidates],
            "fingerprints": _fingerprints(rows),
            "note": _summary_note(by_service),
        },
        "subject": subject,
        "because": because,
        "chain_text": chain_text(by_service),
        "degraded_note": _degraded_note(by_service, because),
    }


def _fingerprints(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """把逐条证据记录按**指纹**合并成"同一个错误只留一条"。

    ★ 合并口径在 `fingerprint` 模块里（去空白、抹时间戳 / trace_id / 毫秒数 /
      长十六进制串），**不在这里再写一份** —— 两份实现漂移一次，
      "同一个错误"就有了两个定义。

    ★ `trace_ids` 是个**列表**（同一聚类里可能带出多条样本的 id）：这里取**第一条**
      作为 `sample_trace_id`。取列表本身当值会让指纹随"样本条数"变化 → 同一个错误
      在样本多的时候反而并不到一起。
    """
    from .fingerprint import condense  # noqa: PLC0415 —— 避免模块级循环引用

    normalized: list[dict[str, Any]] = []
    for row in rows:
        ids = [str(item) for item in (row.get("trace_ids") or [])]
        normalized.append({**row, "trace_id": ids[0] if ids else ""})
    return condense(normalized)


def _row(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "service": record.get("service", ""),
        "error_type": record.get("error_type", ""),
        "count": int(record.get("count") or 0),
        "first_seen": record.get("onset_iso", ""),
        "sample": record.get("sample", ""),
        "trace_ids": [record["trace_id"]] if record.get("trace_id") else [],
        "propagated": bool(record.get("propagated")),
        "signal": record.get("signal", ""),
        "_onset": record.get("onset"),
    }


def _summary_note(by_service: Mapping[str, Mapping[str, Any]]) -> str:
    if not by_service:
        return "本次没有任何错误类证据（这一路是「查了没异常」，不是「没查成」）"
    self_count = sum(1 for item in by_service.values() if item["self"])
    prop_count = sum(1 for item in by_service.values() if item["only_propagated"])
    return (f"{len(by_service)} 个服务有错误日志：{self_count} 个服务自身报错、"
            f"{prop_count} 个只是转发下游报错")


def _degraded_note(by_service: Mapping[str, Mapping[str, Any]], because: str) -> str:
    if not by_service:
        return ""
    if because == "propagated_only":
        names = sorted(name for name, item in by_service.items() if item["only_propagated"])
        return (f"证据里只有**传播类**错误（{'、'.join(names)} 都在替下游报错），"
                f"且没有可用的 trace_id 去下钻 —— **主语留空**，"
                f"不把归因扣在最上游那个服务上")
    return ""
