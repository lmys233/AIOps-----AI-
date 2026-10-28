"""错误 → trace 下钻：**断点 span 的纯函数判定**（不模型、不多轮对话）。

--------------------------------------------------------------------------------
★ 为什么用"确定性下钻"而不是让两个 Agent 对话
--------------------------------------------------------------------------------

"错误日志说 order 在报错"与"trace 说断在 inventory 那一跳"是两条**同一时刻、
不同数据源**的事实。让 Log Agent 与 Trace Agent 互相问答来对齐它们，会同时引入
三个问题：① 谁问谁、问几轮、什么时候停 —— 全变成不可复现的现场判断；
② 两个 Agent 直接对话破坏**证据隔离**（它们本该只往黑板写结构化证据）；
③ 结论依赖模型措辞，同一个事故两次跑可以给出两个主语。

所以：**日志只负责把 `trace_id` 这个事实带出来，下钻完全由代码完成** ——
一条 TraceQL 精确取整条 trace，然后在这份 span 树上跑一个纯函数。可复现、
可单测、失败模式明确（取不到就如实降级，不猜）。

--------------------------------------------------------------------------------
★★ 断点判定的三档（**硬判据在前，启发式在后且必须标注**）
--------------------------------------------------------------------------------

| 档 | 判据 | 置信度 | 为什么 |
|---|---|---|---|
| ① | 有 `status = error` 的 span | 高 | 这是**被调方自己说**"我这儿出错了"，是事实不是推断 |
| ② | 最深叶子 span，且其父 span 报错/超时 | 中 | 父报错了、而错没继续往下传 → 断在这一跳（**启发式**） |
| ③ | 链路在某跳之后就没有子 span 了 | 低 | 连错误标记都没打上，只能说明"调用到此为止"（**启发式**） |

★ 三档的 `heuristic` 与 `confidence` 必须**如实分开**：把它们渲染成同一个强度，
  读者就无法区分"trace 明说了"与"我猜的" —— 那正是归因最不该含糊的地方。

--------------------------------------------------------------------------------
★★ 同一档里选哪个 span：**被调方自己的 span 优先**
--------------------------------------------------------------------------------

真机的跨进程链路是这样的：调用方进程里的 `client` span 下面挂着**被调方**进程里的
`server` span（`traceparent` 跨进程续接，父子关系是事实）。于是一条 5xx 的 trace 里
同时有 `status = error` 的：

  · 调用方的 `internal` "http send"（框架把 5xx 写出去，**不是工作 span**）；
  · 调用方的 `client`（记在**调用方**进程里 —— 把它当断点等于把调用方当出错方）；
  · 被调方的 `server`（**被调方自己说：我这儿错了**）；
  · 被调方自己的工作 span（DB 查询 / 内部 RPC）。

★ 排序就是上面这个顺序（被调方自己的 `server` → 非框架工作 span（更深优先）→
  `internal`），全部**确定性**、可复算。真机实测：不做这件事时断点会稳定落在
  `order/POST /orders http send`，读起来像"order 发响应时报错"。
"""

from __future__ import annotations

from typing import Any, Final, Mapping, Optional, Sequence

__all__ = [
    "BREAKPOINT_STATUS_ERROR",
    "BREAKPOINT_DEEPEST_LEAF",
    "BREAKPOINT_CHAIN_TRUNCATED",
    "ERROR_STATUS_CODES",
    "spans_from_result",
    "span_depths",
    "breakpoint_of",
    "breakpoint_span",
    "span_label",
]

#: 三档判据的名字（进 `Evidence.tags` 与 summary，供渲染与验收断言）
BREAKPOINT_STATUS_ERROR: Final[str] = "status_error"
BREAKPOINT_DEEPEST_LEAF: Final[str] = "deepest_leaf_under_error"
BREAKPOINT_CHAIN_TRUNCATED: Final[str] = "chain_truncated"

#: 三档的置信度 —— ★ 单调递减，且启发式那两档明显低于硬判据。
_CONFIDENCE: Final[dict[str, float]] = {
    BREAKPOINT_STATUS_ERROR: 0.85,
    BREAKPOINT_DEEPEST_LEAF: 0.60,
    BREAKPOINT_CHAIN_TRUNCATED: 0.45,
}

#: 启发式的两档（渲染时必须标注"这是启发式"）
_HEURISTIC: Final[frozenset[str]] = frozenset({
    BREAKPOINT_DEEPEST_LEAF, BREAKPOINT_CHAIN_TRUNCATED,
})

#: span 树的规模上限（一条 trace 的 span 数；超了只记前 N 个并在结果里说明）。
MAX_SPANS: Final[int] = 2000

#: "这个 span 报错了"的 status 取值（OTel 的 `STATUS_CODE_ERROR` 与常见别名）。
#: ★★ **公开**：断点判定与工具摘要（`tools/summarize.py`）共用这一份 ——
#:   各写一份的后果真机实测过：一条 11 个 span、6 个 `status_code_error` 的 trace，
#:   摘要写成「其中报错 **0** 个」（摘要把真机的写法漏了），而断点判定认它 ——
#:   同一次排查里两个数字互相打脸。
ERROR_STATUS_CODES: Final[frozenset[str]] = frozenset({
    "error", "2", "status_code_error"})

#: 内部别名（本模块的历史写法；口径就是上面那一份）。
_ERROR_STATUS: Final[frozenset[str]] = ERROR_STATUS_CODES


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "")


def _status_error(span: Mapping[str, Any]) -> bool:
    """这个 span 是不是**自己**报了错（`status` / 常见别名 / `error=true` 属性）。"""
    status = _text(span.get("status")).strip().lower()
    if status in _ERROR_STATUS:
        return True
    attributes = _mapping(span.get("attributes"))
    for key in ("error", "otel.status_code", "status.code"):
        value = _text(attributes.get(key)).strip().lower()
        if value in ("true", "error", "2"):
            return True
    return False


def _timed_out(span: Mapping[str, Any]) -> bool:
    """父 span 是"超时"（比报错弱一档的判据：调用没回来）。"""
    text = f"{_text(span.get('name'))} {_text(span.get('status'))}".lower()
    attributes = _mapping(span.get("attributes"))
    return "timeout" in text or "deadline" in text or _text(
        attributes.get("error.type")).lower() in ("timeout", "deadline_exceeded")


# ======================================================================
def spans_from_result(result: Any) -> list[dict[str, Any]]:
    """把 `trace_read` 的结果归一成 span 列表（拿不到就空列表 —— **不编**）。"""
    data = getattr(result, "data", None)
    if not isinstance(data, Mapping):
        return []
    spans = data.get("spans")
    if not isinstance(spans, Sequence) or isinstance(spans, (str, bytes)):
        return []
    out: list[dict[str, Any]] = []
    for raw in list(spans)[:MAX_SPANS]:
        item = _mapping(raw)
        if not item:
            continue
        out.append({
            "span_id": _text(item.get("span_id")),
            "parent_span_id": _text(item.get("parent_span_id")),
            "service": _text(item.get("service")),
            "name": _text(item.get("name")),
            "kind": _text(item.get("kind")),
            "status": _text(item.get("status")),
            "start_ns": _text(item.get("start_ns")),
            "duration_ms": float(item.get("duration_ms") or 0.0),
            "attributes": dict(_mapping(item.get("attributes"))),
            "error": _status_error(item),
        })
    return out


def _children_of(spans: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {span["span_id"]: [] for span in spans}
    for span in spans:
        parent = span["parent_span_id"]
        if parent and parent in children:
            children[parent].append(span["span_id"])
    return children


def _leaf_deepest(spans: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """最深、最晚开始的叶子 span（没有子 span 的那些里挑一个）。"""
    children = _children_of(spans)
    leaves = [span for span in spans if not children.get(span["span_id"])]
    if not leaves:
        return None
    return max(leaves, key=lambda span: (_start_value(span), _text(span["span_id"])))


def _root(spans: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    for span in spans:
        if not span["parent_span_id"]:
            return span
    return spans[0] if spans else None


def _start_value(span: Mapping[str, Any]) -> float:
    try:
        return float(span.get("start_ns") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def span_depths(spans: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """每个 span 在树里的**层数**（根 0；父不可见就按根算 —— 被截断的树也排得出）。

    ★ 全仓唯一一份：`error_facts` 的候选/地板排序也调它（"谁更深"只有一种答案）。
    """
    parents = {_text(span.get("span_id")): _text(span.get("parent_span_id"))
               for span in spans}
    out: dict[str, int] = {}

    def _depth(span_id: str, seen: set[str]) -> int:
        if span_id in out:
            return out[span_id]
        parent = parents.get(span_id) or ""
        if not parent or parent not in parents or parent in seen:
            out[span_id] = 0
            return 0
        value = _depth(parent, seen | {span_id}) + 1
        out[span_id] = value
        return value

    for span in spans:
        _depth(_text(span.get("span_id")), set())
    return out


def _is_callee_own(spans: Sequence[Mapping[str, Any]],
                   span: Mapping[str, Any]) -> bool:
    """这个 span 是不是**被调方自己**记的 `server` span（跨进程同一条 trace）。

    ★ 判据：`kind = server` 且**父 span 的服务名与它不同** —— 跨进程续接的那个
      `server` span 才满足；同进程里嵌套的 `server` span（服务名相同）不算。
    """
    if _text(span.get("kind")).strip().lower() != "server":
        return False
    parent = _parent_of(spans, span)
    if parent is None:
        return False
    return _text(parent.get("service")) != _text(span.get("service"))


def _errored_rank(spans: Sequence[Mapping[str, Any]], depths: Mapping[str, int],
                  span: Mapping[str, Any]) -> tuple[int, int, int, str]:
    """报错 span 的优先次序（**越小越优先**；全部确定性、可复算）。

    口径见模块头「被调方自己的 span 优先」：① 被调方自己的 `server` →
    ② 非 `internal` 的工作 span（**更深**优先，越深越接近现场）→ ③ `internal`。
    """
    framework = 1 if _text(span.get("kind")).strip().lower() == "internal" else 0
    depth = int(depths.get(_text(span.get("span_id")), 0))
    return (0 if _is_callee_own(spans, span) else 1, framework, -depth,
            _text(span.get("span_id")))


def _result(evidence_kind: str, span: Mapping[str, Any], *,
            note: str, evidence: str) -> dict[str, Any]:
    """把"选中了哪个 span"打包成**可渲染、可断言**的结果。

    ★ 参数名刻意带 `evidence_` 前缀：`span` 自己也有一个 `kind`（server/client），
      两者重名会让"判据档位"与"span 种类"互相覆盖。
    """
    return {
        "found": True,
        "kind": evidence_kind,
        "heuristic": evidence_kind in _HEURISTIC,
        "confidence": _CONFIDENCE[evidence_kind],
        "span": {
            "span_id": _text(span.get("span_id")),
            "service": _text(span.get("service")),
            "name": _text(span.get("name")),
            "kind": _text(span.get("kind")),
            "status": _text(span.get("status")),
            "duration_ms": float(span.get("duration_ms") or 0.0),
            "attributes": dict(_mapping(span.get("attributes"))),
        },
        "service": _text(span.get("service")),
        "note": note,
        "reason": evidence,
    }


def _not_found(note: str) -> dict[str, Any]:
    return {"found": False, "kind": "", "heuristic": False, "confidence": 0.0,
            "span": {}, "service": "", "note": note, "reason": ""}


def breakpoint_span(tree: Any) -> dict[str, Any]:
    """在一条 trace 的 span 树上判定**断点 span**（三档判据，见模块头）。

    Args:
        tree: 形状为 `{"trace_id": ..., "spans": [...]}` 的映射（`trace_read` 的产物）。

    Returns:
        `{"found", "kind", "heuristic", "confidence", "span", "service", "note", "reason"}`。
        `found=False` 时**不许**编一个 span 出来 —— 由调用方如实降级。
    """
    spans = [span for span in spans_from_result(_Wrap(tree)) if span]
    if not spans:
        return _not_found("这条 trace 里没有 span（取不到就如实说，不猜）")
    errored = [span for span in spans if span["error"]]
    if errored:
        # ★★ 同一档里**被调方自己的 span 优先**（口径见模块头）：不再按"名字最长"挑 ——
        #   那会稳定选中框架的 `*/http send`（名字最长的那条），把调用方读成出错方。
        depths = span_depths(spans)
        chosen = min(errored, key=lambda span: _errored_rank(spans, depths, span))
        return _result(BREAKPOINT_STATUS_ERROR, chosen,
                       note=("span 自己报了错（被调方说明「我这儿出错」）"
                             if _is_callee_own(spans, chosen) else
                             "span 自己报了错；★ 这条链路上没有「被调方自己的 server span」，"
                             "取的是最深的工作 span（`internal` 的框架 span 排在最后）"),
                       evidence=f"status={chosen['status'] or 'error'}")
    deepest = _leaf_deepest(spans)
    if deepest is not None:
        parent = _parent_of(spans, deepest)
        if parent is not None and (parent["error"] or _timed_out(parent)):
            return _result(BREAKPOINT_DEEPEST_LEAF, deepest,
                           note="最深叶子 span，而它的父 span 报错/超时 —— **启发式**",
                           evidence=f"父 span {parent['span_id']} 报错/超时")
    truncated = _chain_truncated(spans)
    if truncated is not None:
        return _result(BREAKPOINT_CHAIN_TRUNCATED, truncated,
                       note="链路在某跳之后就没有子 span 了 —— **启发式**",
                       evidence="该 span 没有子 span（调用到此为止）")
    return _not_found("树里既没有 error span、也没有可判读的叶子/断链（不猜）")


class _Wrap:
    """把"裸 dict"包成有 `data` 的形状，好让 `spans_from_result` 复用一份实现。"""

    def __init__(self, tree: Any) -> None:
        self.data = tree


def _parent_of(spans: Sequence[Mapping[str, Any]],
               span: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    parent_id = span["parent_span_id"]
    return next((item for item in spans if item["span_id"] == parent_id), None)


def _chain_truncated(spans: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """链路在某跳之后就没有子 span：取**最深那个非根 span**（根还在说明链路开过头）。

    ★ 只有一个 span（根）时**不算**断链 —— 那更可能是"这次只采到一个 span"，
      判成断链等于把采集缺失说成断点。
    """
    if len(spans) < 2:
        return None
    children = _children_of(spans)
    leaves = [span for span in spans if not children.get(span["span_id"])]
    root = _root(spans)
    candidates = [span for span in leaves if root is None or span is not root]
    if not candidates:
        return None
    return max(candidates, key=lambda span: (_start_value(span), _text(span["span_id"])))


#: 兼容别名：`breakpoint_of` 在判据里按这个名字用（语义同"判定断点"）。
breakpoint_of = breakpoint_span


def span_label(span: Mapping[str, Any]) -> str:
    """断点 span 的一句话标签（进 tags 与 summary）：`服务/名字`。"""
    service = _text(span.get("service"))
    name = _text(span.get("name"))
    if service and name:
        return f"{service}/{name}"
    return service or name or "（未命名 span）"
