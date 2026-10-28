"""把"已经取到的证据 + 下钻回来的 span 树"折成**候选 / 地板 / 事实行 / 反问**。

--------------------------------------------------------------------------------
★ 这一层只做转换，不做判定
--------------------------------------------------------------------------------

判定在 `error_facts`（候选与地板）、`fingerprint`（同一个错误的合并）、
`decision`（模型排序与后置校验）里。本模块的职责是**把证据里的结构化字段取出来**，
喂给上面那三层，并把结果组装成编排层要的形状：

```
证据（logs / traces）
  ├─ trace 原始 span 树 ──► build_trace_tree ──► trace JSON（有界、嵌套、脱敏）
  │                       └─► error_candidates ──► 候选（intrinsic / propagated / slow）
  ├─ 日志聚类 / 错误证据 ─► condense_clusters ─► 指纹合并（count / 首现 / 样本 trace）
  └──────────────────────► trace_floor ────────► 确定性地板
                              └─► merge_selection ──► 结论（模型增强 or 地板）
```

★ 三条不许越界的地方：

1. **同一指纹不重复计数**：同一条错误既在日志聚类里、又在 trace 节点上时，
   指纹合并要按指纹**去重**，不能把它算两遍（否则"次数"翻倍、爆发判据失真）；
2. **没有原样 span 树就如实说**：候选只有"从 trace 枚举"这一条硬来源，
   拿不到就说"没有候选"，而不是凭错误类型编一个位置；
3. **事实行与反问都只写事实**：反问里必须带**候选与缺口**，
   不许用"信息不足"四个字打发，也不许编一个主语。
"""

from __future__ import annotations

from typing import Any, Final, Mapping, Sequence

from .decision import MAX_VERDICTS, merge_selection
from .error_facts import (MAX_TRACE_NODES, build_trace_tree, error_candidates,
                          trace_floor, trace_service_names)
from .errors import audit_errors
from .fingerprint import condense_clusters, error_fingerprint

__all__ = [
    "GROUNDING_KEY",
    "grounding_of",
    "trace_trees_of",
    "candidates_of",
    "error_summary_of",
    "verdict_questions",
    "facts_lines_of",
    "decide",
]

#: 结论落在 `state.flags` 的哪个键（编排层写、渲染层与答案层读；**唯一一处**）。
GROUNDING_KEY: Final[str] = "rca_grounding"

#: 一次裁决最多看几条 trace 的节点（它们只有 3 条上限，这里再兜一层）。
MAX_GROUNDING_TRACES: Final[int] = 3


def trace_trees_of(agents: Sequence[Any]) -> list[dict[str, Any]]:
    """从各取证角色上**取回**本轮下钻的 span 树（有界；取不到就是空列表）。

    ★ 只认实现了 `stored_traces()` 的角色，并且**取走之后清空** ——
      角色实例是进程级的，不清空会让下一次排查读到上一次的链路。
    """
    out: list[dict[str, Any]] = []
    for agent in agents or ():
        take = getattr(agent, "stored_traces", None)
        clear = getattr(agent, "clear_traces", None)
        if not callable(take):
            continue
        for item in take():
            if isinstance(item, Mapping) and item.get("spans"):
                out.append(dict(item))
        if callable(clear):
            clear()
    return out[:MAX_GROUNDING_TRACES]


def candidates_of(trees: Sequence[Mapping[str, Any]], *,
                  error_rows: Sequence[Mapping[str, Any]] = (),
                  fingerprints: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """候选 = **trace 里所有 error 节点**（带标记）+ **日志侧有、trace 侧没有**的错误。

    ★ 两边都保留的理由：trace 下钻一次只有 3 条（有界），有些错误只在日志里有；
      把它们丢掉会让"某个服务在报错"这件事在候选里凭空消失。
      trace 侧的候选带位置与标记，日志侧的候选只有 `(服务, 错误类型)` 与指纹 ——
      `source` 字段把两者分开，模型与校验都能看出"这条没有 span 位置"。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in fingerprints or ():
        out.append(_log_candidate(item))
        seen.add(str(item.get("fingerprint") or ""))
    for row in error_rows or ():
        key = error_fingerprint(row.get("service", ""), row.get("operation", ""),
                                row.get("error_type", ""), row.get("sample", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(_log_candidate({**dict(row), "fingerprint": key}))
    for tree in trees or ():
        for item in error_candidates(build_trace_tree(tree, max_nodes=MAX_TRACE_NODES)):
            out.append({**item, "source": "trace", "count": 1,
                        "first_seen": "", "last_seen": ""})
    return out


def _log_candidate(item: Mapping[str, Any]) -> dict[str, Any]:
    """一条日志/证据侧的候选（**没有 span 位置**，如实标出来）。"""
    record = dict(item)
    return {
        "service": str(record.get("service") or ""),
        "operation": str(record.get("operation") or ""),
        "error_type": str(record.get("error_type") or ""),
        "span_id": "",
        "status": "error",
        "kind": "",
        "duration_ms": 0.0,
        "intrinsic": not bool(record.get("propagated")),
        "propagated": bool(record.get("propagated")),
        "slow": False,
        "downstream": "",
        "trace_id": str(record.get("sample_trace_id") or record.get("trace_id") or ""),
        "fingerprint": str(record.get("fingerprint") or ""),
        "error_text": str(record.get("sample_text") or record.get("sample") or ""),
        "depth": 0,
        "count": int(record.get("count") or 1),
        "first_seen": str(record.get("first_seen_iso") or record.get("first_seen") or ""),
        "last_seen": str(record.get("last_seen_iso") or record.get("last_seen") or ""),
        "source": "logs",
    }


def error_summary_of(evidence: Sequence[Any], *, slots: Any = None,
                     candidates: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """跨服务错误汇总（含候选与指纹）—— 与 `audit_errors` 是**同一份实现**。"""
    return audit_errors(evidence, slots=slots, candidates=candidates)["error_summary"]


def facts_lines_of(error_summary: Mapping[str, Any], *, limit: int = 12) -> list[str]:
    """给裁决提示词用的**事实行**（谁在报错、在哪、什么错、几次、首现）。"""
    out: list[str] = []
    for item in (error_summary.get("fingerprints") or [])[:limit]:
        mark = "（替下游报错）" if item.get("propagated") else "（本服务自身报错）"
        out.append(f"错误：{item.get('service')} / {item.get('error_type')}{mark} "
                   f"×{item.get('count')} 次，首现 "
                   f"{item.get('first_seen_iso') or '（未知）'}"
                   + (f"，样本链路 {item.get('sample_trace_id')}"
                      if item.get("sample_trace_id") else ""))
    if error_summary.get("services_propagated_only"):
        out.append("★ 只在转发下游报错的服务（不许当成它在报错）："
                   + "、".join(error_summary["services_propagated_only"]))
    return out


def grounding_of(*, evidence: Sequence[Any], slots: Any, trees: Sequence[Mapping[str, Any]],
                 memories: Sequence[str] = (), gaps: Sequence[str] = (),
                 max_verdicts: int = MAX_VERDICTS) -> dict[str, Any]:
    """组装这一轮的**裁决上下文**（候选 / trace JSON / 地板 / 事实 / 记忆 / 缺口）。

    ★ 顺序有意：**先合并错误（拿到指纹）→ 再枚举候选 → 最后算地板**。
      反过来会让候选里出现同一条错误的两份（带位置的那份与不带位置的那份）。
    """
    summary = error_summary_of(evidence, slots=slots)
    trees_json = [build_trace_tree(tree, max_nodes=MAX_TRACE_NODES) for tree in trees or ()]
    candidates = candidates_of(trees, error_rows=summary.get("rows") or (),
                              fingerprints=summary.get("fingerprints") or ())
    floor = trace_floor(candidates, memories=memories)
    return {
        "candidates": candidates,
        "traces": trees_json,
        "floor": floor,
        "facts": facts_lines_of(summary),
        "memories": [str(line) for line in memories][:4],
        "gaps": [str(line) for line in gaps][:6],
        "services": sorted({str(item.get("service") or "") for item in candidates
                            if item.get("service")}
                           | {name for tree in trees_json
                              for name in trace_service_names(tree)}),
        "trace_ids": [str(tree.get("trace_id") or "") for tree in trees_json],
        "max_verdicts": int(max_verdicts),
        "error_summary": summary,
    }


def verdict_questions(grounding: Mapping[str, Any], *,
                      limit: int = 2) -> list[str]:
    """候选解释不了 / 记忆不支持时**要反问的问题**（**必须带候选与缺口**）。

    ★ 不许写"信息不足"四个字：用户需要知道"我看到了哪几个可能位置、还缺什么"
      才能回答。也**不许**编一个主语 —— 问题里只能出现候选与事实里有的服务名。

    Returns:
        人话问题列表（没有候选、也没有缺口时返回空列表 —— 由调用方走既有的
        "没有结论"话术，而不是硬凑一个问题）。
    """
    candidates = list(grounding.get("candidates") or [])
    gaps = [str(line) for line in grounding.get("gaps") or []]
    if not candidates:
        if not gaps:
            return []
        return [f"这次没能取到可判读的错误链路（{'、'.join(gaps[:2])}），"
                "你那边看到的错误现象是什么（报错信息 / 什么时候开始 / 哪个入口）？"]
    # ★ 按**服务**去重：用户要回答的是"哪个服务"，同一个服务的多个节点
    #   算一个可能位置（否则会把"3 个服务"说成"8 个位置"，读起来像在虚张声势）。
    names: list[str] = []
    for item in candidates:
        name = str(item.get("service") or "").strip()
        if name and name not in names:
            names.append(name)
    head = (f"我在链路上看到 {len(names)} 个可能出错的服务："
            f"{'、'.join(names[:MAX_VERDICTS])}")
    tail = (f"，但缺 {'、'.join(gaps[:2])}（这一路没查成，**不等于没有异常**）"
            if gaps else "，但它们各自能不能单独解释你看到的现象，我这边还缺一条判据")
    return [f"{head}{tail}；你那边看到的错误现象是什么？"][:max(1, int(limit))]


def decide(grounding: Mapping[str, Any], draft: Any) -> dict[str, Any]:
    """把模型的选择与地板合成结论（唯一出口：**越界必弃用**）。"""
    return merge_selection(grounding, draft)
