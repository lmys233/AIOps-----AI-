"""一条 trace → **给模型看的 JSON**，以及**由代码枚举出来的错误候选**。

--------------------------------------------------------------------------------
★ 为什么要有界、要留痕
--------------------------------------------------------------------------------

一条真实链路的 span 数没有上界（网关扇出、重试、批量任务都能把它撑到几百上千）。
原样塞进提示词会同时踩两个坑：**token 预算爆掉**与**关键跨度被淹没**。
所以节点数有上限；★ 但**截断必须留痕** —— "只给了前 64 个"与"这条 trace 只有 64 个"
在报告里必须能分开，否则"没看到"会被读成"没有"。

--------------------------------------------------------------------------------
★★ 候选集**由代码枚举**，不许让模型自由发挥
--------------------------------------------------------------------------------

候选 = trace 里**所有 `status = error` 的节点**，每个带三种标记：

| 标记 | 判据（都在这里写死） | 含义 |
|---|---|---|
| `intrinsic` | 自己没有失败的出站子调用 | **自己炸**：代码异常 / DB 查询抛错 |
| `propagated` | 失败来自出站调用（`client` span 失败 / 出站子调用失败 / 错误类型是"下游的"） | 被别人拒了 |
| `slow` | `status` 正常但 `duration` 异常，且上游有 `DOWNSTREAM_TIMEOUT` / `POOL_ACQUIRE_TIMEOUT` 之类 | 慢到成了嫌疑人 |

★ 多分支并列时给**多个候选**（一次请求可能有多个原始出错位置），不硬挑一个。
★ **确定性地板（floor）**：最深的 `intrinsic` 节点；没有 intrinsic 就拿最深的
  `propagated` 节点，但**必须标注"被调方 = X"** —— 不许把调用方当根因
  （调用方只是"替下游喊了一声"，这条与日志那一路的传播语义是同一条口径）。

--------------------------------------------------------------------------------
★ 被调方怎么来的（**只能来自事实**）
--------------------------------------------------------------------------------

客户端 span 的 `service` 是**调用方**（httpx 埋点在调用方进程里），
所以"被调方"不能从它自己读。两个来源，都必须是查出来的：

  1. **子 span**：同一个 `client` span 下面挂着被调方进程里的 `server` span ——
     那个 `server` span 的 `service` 就是被调方（跨进程同一条 trace，父子关系是事实）；
  2. **拓扑**：调用方显式声明或查到的依赖表（`topology`）。

两个都拿不到就**如实留空**（`downstream=""`），绝不猜一个服务名。
"""

from __future__ import annotations

from typing import Any, Final, Mapping, Sequence

from .errors import PROPAGATED_DOWNSTREAM_ERROR_TYPES, embedded_error_types
from .fingerprint import FINGERPRINT_CHARS, error_fingerprint, normalize_detail
from .trace_spans import span_depths

__all__ = [
    "MAX_TRACE_NODES",
    "ERROR_TEXT_CHARS",
    "SLOW_FACTOR",
    "FLOOR_DEEPEST_INTRINSIC",
    "FLOOR_DEEPEST_PROPAGATED",
    "MAX_MENTIONED_SERVICES",
    "build_trace_tree",
    "trace_service_names",
    "error_candidates",
    "trace_floor",
    "service_mention_terms",
]

#: 一次交给模型 / 报告的一条 trace 里最多几个节点（超出截断并留痕）。
MAX_TRACE_NODES: Final[int] = 64

#: `error_text` 的字符上限。★ 后端已有脱敏（`sdk.redact`）就走它，
#: 本层只负责"有界"——两件事不要互相顶替。
ERROR_TEXT_CHARS: Final[int] = 300

#: "慢"的判据：节点耗时 ≥ max(绝对门槛, 中位数 × 这个倍数) 才算异常。
SLOW_FACTOR: Final[float] = 5.0
#: "慢"的绝对门槛（毫秒）—— 一条 30ms 的 trace 里 8ms 不算"异常慢"。
SLOW_MIN_MS: Final[float] = 200.0

#: 上游错误类型里出现这些词 → 这条链路上有东西在**等**（超时/排队），
#: 于是同一条链路上"正常但慢"的那个出站节点值得当嫌疑人。
#: ★ `PG_SLEEP` 也在里面：被观测系统用一个"故意睡住的查询"占住连接，
#:   这条 SQL 本身就是"有东西在等"的**事实**，不是猜的。
SLOW_HINTS: Final[tuple[str, ...]] = (
    "TIMEOUT", "DEADLINE", "POOL_ACQUIRE", "POOL_WAIT", "QUEUE", "REJECT", "SLOW",
    "UNAVAILABLE", "BAD_STATUS", "PG_SLEEP",
)

#: 地板判据的名字（进结果与留痕，让"根因是推理出来的还是编码出来的"可核对）。
FLOOR_DEEPEST_INTRINSIC: Final[str] = "deepest_intrinsic"
FLOOR_DEEPEST_PROPAGATED: Final[str] = "deepest_propagated"

#: 候选里最多带出几个**服务名**（提示词给模型的"允许提到"清单，有界）。
MAX_MENTIONED_SERVICES: Final[int] = 32

#: "这个 span 报错了"的 status 取值（OTLP 的 `STATUS_CODE_ERROR` 与常见别名）。
_ERROR_STATUS: Final[frozenset[str]] = frozenset({
    "error", "2", "status_code_error", "status_code_2", "statuscodeerror"})

#: 出站 span 的 kind（被调方视角的 `server` 不算出站）。
_OUTBOUND_KINDS: Final[frozenset[str]] = frozenset({"client", "producer"})

#: `error_text` 的候选属性键（按优先级；都是 OTel 语义键）。
_ERROR_ATTRS: Final[tuple[str, ...]] = (
    "exception.message", "otel.status_description", "status.description",
    "error.message", "error.type", "http.status_text", "rpc.grpc.status_code",
)

#: 信息量太低、不该当成"错误文本"的属性值。
_LOW_VALUE_TEXTS: Final[frozenset[str]] = frozenset({"true", "false", "1", "0", "2"})


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _status_is_error(value: Any) -> bool:
    text = _text(value).strip().lower()
    return text in _ERROR_STATUS or text.endswith("_error")


def _normalized_status(value: Any) -> str:
    """状态码归一成 `error` / `ok` / `unset`（认不出来就**原样小写**，不猜）。"""
    text = _text(value).strip().lower()
    if not text:
        return "unset"
    if _status_is_error(value):
        return "error"
    if text.endswith("_ok"):
        return "ok"
    if text.endswith("_unset"):
        return "unset"
    return text


# ======================================================================
#  ① span → trace JSON（保留嵌套、有界、脱敏）
# ======================================================================
def _error_text(span: Mapping[str, Any]) -> str:
    """这个节点上的错误文本（OTLP 的 status.message 与几个语义属性；取不到就空）。

    ★ 多段来源用 ` | ` 拼接后**统一脱敏与截断**：不在这里各截一次，
      否则"截断"会散成好几处，改一处漏一处。
    """
    found: list[str] = []
    status = _mapping(span.get("status"))
    for value in (status.get("message"), span.get("status_description")):
        if _text(value).strip():
            found.append(_text(value).strip())
    attributes = _mapping(span.get("attributes"))
    for key in _ERROR_ATTRS:
        value = _text(attributes.get(key)).strip()
        if value and value.lower() not in _LOW_VALUE_TEXTS:
            found.append(f"{key}={value}")
    if not found:
        return ""
    return _shorten(" | ".join(dict.fromkeys(found)))


def _redact(text: str) -> str:
    """走**已有的那一份**脱敏实现（不在这里再写一套规则，两套迟早漂移）。"""
    try:
        from ..memory.gates import redact_text as _redact_text  # noqa: PLC0415

        return _text(_redact_text(text))
    except Exception:  # noqa: BLE001 —— 脱敏模块不可用不该让取证失败
        return _text(text)


def _shorten(text: str, limit: int = ERROR_TEXT_CHARS) -> str:
    """脱敏 + 截断（**先脱敏再截断**：反过来会把密钥截成一半而躲过规则）。"""
    out = _redact(text)
    return out if len(out) <= limit else f"{out[:limit]}…"


def _bridge_span(span: Mapping[str, Any]) -> dict[str, Any]:
    """一条**原样跨度**（`trace_read` 的产物）→ trace JSON 的一个节点（无子节点）。

    ★ `error_type` 在这里就从属性里取出来：否则 `_error_type_of` 会看到自己写下的
      **空串**（空串是"有值但不给"），再也不会去读 `error.type` —— 于是所有明明带了
      错误类型的 span 都判成"没有错误类型"，传播类判据（闭集那一条）直接失效。
    """
    return {
        "span_id": _text(span.get("span_id")),
        "parent_span_id": _text(span.get("parent_span_id")),
        "kind": _text(span.get("kind")),
        "service": _text(span.get("service")),
        "operation": _text(span.get("name")),
        "status": _normalized_status(span.get("status")),
        "duration_ms": round(float(span.get("duration_ms") or 0.0), 3),
        "error_type": _declared_error_type(span),
        "error_text": _error_text(span),
        "attributes": dict(_mapping(span.get("attributes"))),
        "start_ns": _text(span.get("start_ns")),
    }


def _declared_error_type(span: Mapping[str, Any]) -> str:
    """span 属性里**明文声明**的错误类型（没有就空串 —— **不猜**）。"""
    declared = _text(span.get("error_type")).strip()
    if declared:
        return declared
    attributes = _mapping(span.get("attributes"))
    for key in ("error.type", "error_type"):
        value = _text(attributes.get(key)).strip()
        if value and value.lower() not in _LOW_VALUE_TEXTS and not value.isdigit():
            return value
    return ""


def _bridge_statements(span: Mapping[str, Any]) -> list[dict[str, Any]]:
    """`error_statements` 形状（已经解析过的 span）→ 节点列表。

    ★ 为什么两种输入都要支持：`trace_read` 给的是**原样跨度**（带 `status` 结构），
      而下钻链路里也有已经解析好的扁平 span。少支持一种就会在真机上解析出 0 个节点。
    """
    from .trace_spans import spans_from_result  # noqa: PLC0415

    return [dict(item) for item in spans_from_result(_Wrap(span))]


class _Wrap:
    """把裸映射包成有 `data` 的形状，好复用**同一份** span 解析实现。"""

    def __init__(self, data: Any) -> None:
        self.data = data


def _nodes_of(trace: Mapping[str, Any]) -> list[dict[str, Any]]:
    """一条 trace（两种输入形状）→ 扁平节点列表（**顺序 = trace 里的原顺序**）。"""
    raw = trace.get("spans")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        nodes = [_bridge_span(_mapping(item)) for item in raw]
        return [node for node in nodes if node["span_id"] or node["service"]]
    return _bridge_statements(trace)


def _children_of(nodes: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    ids = {str(node["span_id"]) for node in nodes}
    children: dict[str, list[str]] = {str(node["span_id"]): [] for node in nodes}
    for node in nodes:
        parent = str(node["parent_span_id"])
        if parent and parent in ids and children.get(parent) is not None:
            children[parent].append(str(node["span_id"]))
    return children


def _walk_order(nodes: Sequence[Mapping[str, Any]]) -> tuple[list[str], dict[str, str],
                                                             dict[str, int]]:
    """**广度优先**的节点次序 + 每个节点**可达的最近祖先** + 层数。

    ★ 为什么按层序：截断的目的是"保住整棵树的形状"，层序把同一层一起保留，
      下游读者仍然能还原父子关系；而"只留最深那条链"会把兄弟分支全砍掉。
    ★ 环（`parent` 指回后代）不会让这里转圈：`reached` 集合保证每个节点只入队一次，
      兜底的第三轮再把**谁都没到达**的节点（环里的 / 缺父的）按原顺序补上 —— 不静默丢。

    Returns:
        `(次序, {span_id: 可达祖先}, {span_id: 层数})` —— 祖先只含**真的在这批节点里**的，
        层数就是祖先链的长度（根为 0）。
    """
    children = _children_of(nodes)
    ids = [str(node["span_id"]) for node in nodes]
    parent = {str(node["span_id"]): str(node["parent_span_id"]) for node in nodes}
    order: list[str] = []
    reached: dict[str, str] = {}
    depth: dict[str, int] = {}
    roots = [span_id for span_id in ids if not (parent[span_id] and parent[span_id] in children)]
    frontier = list(roots)
    while frontier:
        nxt: list[str] = []
        for span_id in frontier:
            if span_id in reached or span_id not in children:
                continue
            known = parent[span_id] in reached
            reached[span_id] = parent[span_id] if known else ""
            depth[span_id] = depth[parent[span_id]] + 1 if known else 0
            order.append(span_id)
            nxt.extend(kid for kid in children[span_id] if kid not in reached)
        frontier = nxt
    for span_id in ids:                    # 环 / 缺父：按原顺序补上（不静默丢）
        if span_id not in reached:
            reached[span_id] = ""
            depth[span_id] = 0
            order.append(span_id)
    return order, reached, depth


def build_trace_tree(trace: Mapping[str, Any], *,
                     max_nodes: int = MAX_TRACE_NODES) -> dict[str, Any]:
    """一条 trace → **有界、脱敏、保留父子嵌套**的 JSON。

    Returns:
        `{"trace_id", "service", "nodes", "node_count", "max_nodes",
          "truncated", "truncation"}`。

        `truncation` = `{"dropped": n, "kept": m, "max_nodes": k, "rule": 人话}`；
        没截断时 `dropped=0`、`rule=""` —— **留痕是显式的，不是靠推断**。
    """
    nodes = _nodes_of(_mapping(trace))
    by_id = {str(node["span_id"]): node for node in nodes}
    order, reached, depth = _walk_order(nodes)
    limit = max(0, int(max_nodes))
    kept = set(order[:limit])
    out_nodes = [_node_json(by_id[span_id], by_id, kept, reached, depth)
                 for span_id in order[:limit]]
    dropped = len(order) - len(kept)
    return {
        "trace_id": _text(trace.get("trace_id")),
        "service": _text(trace.get("service") or _root_service(out_nodes)),
        "nodes": out_nodes,
        "node_count": len(out_nodes),
        "max_nodes": int(max_nodes),
        "truncated": dropped > 0,
        "truncation": {
            "dropped": dropped,
            "kept": len(kept),
            "max_nodes": int(max_nodes),
            "rule": ("按**层序**取前 N 个节点（同层按开始时间），超出部分丢弃"
                     if dropped > 0 else ""),
        },
    }


def _root_service(nodes: Sequence[Mapping[str, Any]]) -> str:
    return _text(nodes[0].get("service")) if nodes else ""


def _node_json(node: Mapping[str, Any], by_id: Mapping[str, Mapping[str, Any]],
               kept: set[str], reached: Mapping[str, str],
               depth: Mapping[str, int]) -> dict[str, Any]:
    """一个节点 → trace JSON 里的那一条（**带 `children` 嵌套**）。

    ★ `parent_span_id` 只写**真的会被写出**的那个父亲：父节点被截断掉时写空串，
      并把它标成根（`is_root=True`）—— 否则读者会把"父被截断"误读成"这棵树就长这样"。
    """
    span_id = str(node["span_id"])
    ancestor = str(reached.get(span_id, ""))
    parent = ancestor if ancestor in kept else ""
    return {
        "span_id": span_id,
        "parent_span_id": parent,
        "is_root": not parent,
        "kind": _text(node.get("kind")),
        "service": _text(node.get("service")),
        "operation": _text(node.get("operation")),
        "status": _text(node.get("status")),
        "duration_ms": float(node.get("duration_ms") or 0.0),
        "error_type": _text(node.get("error_type")),
        "error_text": _text(node.get("error_text")),
        # ★ 属性也要给（按上限脱敏）：`db.statement` / `http.route` 是"哪个位置"
        #   最具体的证据，也是"这条链路上有东西在等"的判据来源。
        "attributes": dict(_mapping(node.get("attributes"))),
        "depth": int(depth.get(span_id, 0)) if parent else 0,
        "children": [str(item["span_id"]) for item in by_id.values()
                     if str(item.get("parent_span_id")) == span_id
                     and str(item["span_id"]) in kept],
    }


def trace_service_names(tree: Mapping[str, Any]) -> list[str]:
    """这棵树里出现过的**全部服务名**（去重、稳定排序）。

    ★ 它是后置校验的"允许提到"集合：模型只能提这棵树与候选里出现过的服务。
    """
    names = {_text(node.get("service")) for node in tree.get("nodes") or []}
    return sorted(name for name in names if name)


# ======================================================================
#  ② 候选与标记
# ======================================================================
def _child_nodes(tree: Mapping[str, Any], span_id: str) -> list[Mapping[str, Any]]:
    return [node for node in tree.get("nodes") or []
            if _text(node.get("parent_span_id")) == span_id]


def _subtree(tree: Mapping[str, Any], span_id: str) -> list[Mapping[str, Any]]:
    """某个节点的**全部后代**（含跨层；父子关系可能被截断，所以按可达闭包算）。"""
    out: list[Mapping[str, Any]] = []
    frontier = [span_id]
    seen: set[str] = set()
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        for child in _child_nodes(tree, current):
            out.append(child)
            frontier.append(_text(child.get("span_id")))
    return out


def _outbound_children(tree: Mapping[str, Any],
                       span_id: str) -> list[Mapping[str, Any]]:
    """这个节点的**出站子调用**（它的失败意味着"我被下游拒了"）。

    ★ 四种形态里只有三种算（真机形状实测）：

      · `kind = client` / `producer` —— 调用方进程里记的"我去调别人"；
      · `kind = server` 且**服务名与父不同** —— 被调方进程里记的"别人调我"，
        跨进程同一条 trace，所以它挂在这个出站调用下面。

    ★ **`internal` 一律不算**：FastAPI 自动埋点的 "http send" span 服务名与父相同、
      又不是出站调用；把它算成出站，会把"发响应时报错"读成"我被下游拒了"，
      于是真正自己炸的那个节点（DB 查询）也变成 propagated —— **根因被洗掉**。
    ★ 服务名**相同**的 `server` 子 span 也不算（同进程嵌套，不是出站调用）。
    """
    service = _service_of_span(tree, span_id)
    out: list[Mapping[str, Any]] = []
    for child in _child_nodes(tree, span_id):
        kind = _text(child.get("kind"))
        if kind in _OUTBOUND_KINDS or (kind == "server"
                                       and _text(child.get("service")) != service):
            out.append(child)
    return out


def _service_of_span(tree: Mapping[str, Any], span_id: str) -> str:
    for node in tree.get("nodes") or []:
        if _text(node.get("span_id")) == span_id:
            return _text(node.get("service"))
    return ""


def _downstream_service(tree: Mapping[str, Any], service: str,
                        span_id: str) -> str:
    """这个节点的**被调方**服务（只认事实）：

      ① 子树里第一个服务名与调用方不同的 span（同一条 trace 里被调方的 `server` span）；
      ② 拿不到就空串 —— **不猜**。
    """
    for node in _subtree(tree, span_id):
        name = _text(node.get("service"))
        if name and name != service:
            return name
    return ""


def _is_slow(node: Mapping[str, Any], median_ms: float, hinted: bool) -> bool:
    """这个节点"正常但慢"吗（阈值见 `SLOW_*`；`hinted` = 链路上有超时/排队迹象）。

    ★ 只有一条判据：**耗时异常且链路上有"在等"的迹象**。
      没有迹象的慢是"业务本来就慢"，判成嫌疑犯会把报告带偏。
    """
    if _text(node.get("status")) == "error":
        return False
    duration = float(node.get("duration_ms") or 0.0)
    if duration <= 0.0:
        return False
    return hinted and duration >= max(SLOW_MIN_MS, median_ms * SLOW_FACTOR)


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _normal_ms(values: Sequence[float]) -> float:
    """这条 trace 的"**平时**耗时"基准 —— 只用**最快那一半**（含 0）算中位数。

    ★ 为什么不能用全体中位数：一条线上最慢的那个节点的耗时常常比正常值大两个
      数量级（真机实测：正常 3~8ms、`pg_sleep` 1003ms），它会把中位数**整个抬起来**，
      于是"异常慢"的门槛（中位数 × 倍数）比异常本身还高 —— 判据自动失效。
      丢掉上半之后，基准回到"平时"，异常那一个才浮得出来。
    """
    if not values:
        return 0.0
    ordered = sorted(float(item) for item in values)
    half = max(1, len(ordered) // 2)
    return _median(ordered[:half])


def _hint_text(tree: Mapping[str, Any], span_id: str) -> str:
    """一条链路上"有东西在等"的证据文本：**自己 + 全部后代**的"链路证据文本"。

    ★ 只从后代里取不够：`DOWNSTREAM_TIMEOUT` 这种提示往往写在**父**（调用方）
      的 `status.message` 上，而"慢"的是那个出站子节点。
    """
    own = [_chain_text_of(tree, span_id)]
    return " ".join([*own, *(_chain_text_of(tree, _text(node.get("span_id")))
                             for node in _subtree(tree, span_id))]).upper()


def _chain_text_of(tree: Mapping[str, Any], span_id: str) -> str:
    """一个节点的**链路证据文本**：错误文本 + 它实际执行的语句/路由。

    ★ 为什么要把 `db.statement` / `http.route` 也算进来：一条
      `SELECT pg_sleep(1)` 本身就是"有东西在占着连接"的**事实**（被观测系统
      用一个故意睡住的查询打满连接池），只看 `status.message` 会漏掉它。
    """
    for node in tree.get("nodes") or []:
        if _text(node.get("span_id")) != span_id:
            continue
        attributes = _mapping(node.get("attributes"))
        parts = [_text(node.get("error_text"))]
        for key in ("db.statement", "http.route", "rpc.method", "error.type"):
            value = _text(attributes.get(key))
            if value:
                parts.append(f"{key}={value}")
        return " ".join(part for part in parts if part)
    return ""


def _ancestor_hint(tree: Mapping[str, Any], span_id: str) -> str:
    """**祖先链**上的"链路证据文本"（`DOWNSTREAM_TIMEOUT` / `POOL_ACQUIRE_TIMEOUT`
    常写在调用方的 `status.message` 上）。

    ★ 为什么必须有这一路：真机里"出站调用超时"的提示写在**调用方**的
      `status.message` 上，而慢的是它下面那个 client span —— 只看自己的子树，
      这个嫌疑人就永远找不出来。
    """
    parents = {_text(node.get("span_id")): _text(node.get("parent_span_id"))
               for node in tree.get("nodes") or []}
    texts: list[str] = []
    current = parents.get(span_id, "")
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        texts.append(_chain_text_of(tree, current))
        current = parents.get(current, "")
    return " ".join(texts).upper()


def _chain_hint(tree: Mapping[str, Any], span_id: str) -> bool:
    """这条链路（自己 + 后代 + **祖先**）上有没有"在等"的迹象。"""
    text = f"{_hint_text(tree, span_id)} {_ancestor_hint(tree, span_id)}"
    return any(hint in text for hint in SLOW_HINTS)


def _candidate_of(tree: Mapping[str, Any], node: Mapping[str, Any], *,
                  intrinsic: bool, propagated: bool, slow: bool,
                  downstream: str = "", error_type: str = "") -> dict[str, Any]:
    service = _text(node.get("service"))
    operation = _text(node.get("operation"))
    return {
        "service": service,
        "operation": operation,
        "error_type": error_type,
        "span_id": _text(node.get("span_id")),
        "status": _text(node.get("status")),
        "kind": _text(node.get("kind")),
        "duration_ms": float(node.get("duration_ms") or 0.0),
        "intrinsic": intrinsic,
        "propagated": propagated,
        "slow": slow,
        "downstream": downstream,
        "trace_id": _text(tree.get("trace_id")),
        # ★ 指纹**总是**算：错误类型拿不到时它仍然是"这个位置"的稳定标识
        #   （服务 + 操作 + 链路错误文本）—— 模型回填与去重都靠它。
        "fingerprint": error_fingerprint(service, operation, error_type,
                                         _text(node.get("error_text"))),
        "error_text": _text(node.get("error_text")),
        "depth": 0,
    }


def error_candidates(tree: Mapping[str, Any]) -> list[dict[str, Any]]:
    """一棵 trace 树 → **由代码枚举**的错误候选（+ 标记）。

    候选 = 所有 `status=error` 的节点；另外把"正常但慢、且链路上有超时/排队迹象"
    的出站节点作为 `slow` 候选（它没有报错，但恰好是"在等谁"的那个位置）。

    `depth` = 该节点在树里的层数（根为 0）—— **地板的"最深"按它排**。
    """
    nodes = list(tree.get("nodes") or [])
    durations = [float(node.get("duration_ms") or 0.0) for node in nodes]
    median_ms = _normal_ms([item for item in durations if item > 0.0])
    depths = _depths(tree, nodes)
    out: list[dict[str, Any]] = []
    for node in nodes:
        span_id = _text(node.get("span_id"))
        depth = depths.get(span_id, 0)
        if _text(node.get("status")) == "error":
            out.append(_error_candidate(tree, node, depth=depth))
        elif _is_slow(node, median_ms, _chain_hint(tree, span_id)):
            item = _candidate_of(tree, node, intrinsic=False, propagated=False, slow=True,
                                 downstream=_downstream_service(
                                     tree, _text(node.get("service")), span_id))
            item["depth"] = depth
            item["slow_reason"] = "耗时异常，且这条链路上有超时/排队迹象"
            out.append(item)
    out.sort(key=lambda item: (item["depth"], item["span_id"]))
    return out


def _error_candidate(tree: Mapping[str, Any], node: Mapping[str, Any], *,
                     depth: int) -> dict[str, Any]:
    """一个 error 节点 → 候选（含 `intrinsic` / `propagated` 判定）。"""
    span_id = _text(node.get("span_id"))
    service = _text(node.get("service"))
    error_type = _error_type_of(node)
    outbound = _outbound_children(tree, span_id)
    failed_call = next((child for child in outbound
                        if _text(child.get("status")) == "error"), None)
    downstream = _downstream_service(tree, service, span_id)
    nested = _is_downstream_error(error_type, _text(node.get("error_text")))
    propagated = failed_call is not None or nested
    item = _candidate_of(tree, node, intrinsic=not propagated, propagated=propagated,
                         slow=False, downstream=downstream if propagated else "",
                         error_type=error_type)
    item["depth"] = depth
    if propagated and failed_call is not None:
        item["propagating_call"] = {
            "span_id": _text(failed_call.get("span_id")),
            "operation": _text(failed_call.get("operation")),
            "downstream": _text(failed_call.get("service")),
        }
    return item


def _error_type_of(node: Mapping[str, Any]) -> str:
    """节点的错误类型：属性里声明的优先，其次 span 的 `error.type`，最后从文本里抠。"""
    declared = _text(node.get("error_type")).strip()
    if declared:
        return declared
    attributes = _mapping(node.get("attributes"))
    for key in ("error.type", "error_type"):
        value = _text(attributes.get(key)).strip()
        if value and value.lower() not in _LOW_VALUE_TEXTS:
            return value
    found = embedded_error_types(_text(node.get("error_text")))
    return found[0] if found else ""


def _is_downstream_error(error_type: str, text: str) -> bool:
    """错误类型是不是"被下游拒了"那一类（闭集 + 文本里嵌的下游错误类型）。"""
    if error_type in PROPAGATED_DOWNSTREAM_ERROR_TYPES:
        return True
    return any(name in PROPAGATED_DOWNSTREAM_ERROR_TYPES
               for name in embedded_error_types(text))


def _depths(tree: Mapping[str, Any], nodes: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """每个节点在树里的层数（根 0；父子关系被截断的节点按"父不可见 = 根"处理）。

    ★ 实现只有一份（`trace_spans.span_depths`）：断点判定与地板排序都要用"谁更深"，
      两处各算一次就等于同一棵树有两个深度表。
    """
    del tree                                # 旧签名保留（本模块内的调用点不因此变动）
    return span_depths(nodes)


def trace_floor(candidates: Sequence[Mapping[str, Any]], *,
                memories: Sequence[str] = ()) -> dict[str, Any]:
    """**确定性地板**：选不出更好的时候，结论就是它。

    顺序（**先硬事实、后降级**）：

      1. 最深的 `intrinsic` 节点 —— "自己炸"且离现场最近的那个；
      2. 没有 intrinsic → 最深的 `propagated` 节点，并标注**被调方 = X**；
      3. 两者都没有 → `picked=[]` + `degraded`（如实降级，由调用方去反问）。

    `memories` 是三层记忆摘要（用来解释"记忆支不支持这个判断"）；它**不改变**
    地板的选取（地板必须是纯事实），只在升降级说明里留痕。
    """
    intrinsic = [item for item in candidates if item.get("intrinsic")]
    if intrinsic:
        return _floor_of(intrinsic, FLOOR_DEEPEST_INTRINSIC, memories)
    propagated = [item for item in candidates if item.get("propagated")]
    if propagated:
        return _floor_of(propagated, FLOOR_DEEPEST_PROPAGATED, memories)
    return {
        "picked": [],
        "kind": "",
        "downstream_only": False,
        "degraded": ("这一轮**没有错误候选**（trace 里没有 error 节点）"
                     "→ 不编一个根因，改用已有证据与记忆给排查方向"),
    }


def _floor_of(items: Sequence[Mapping[str, Any]], kind: str,
              memories: Sequence[str]) -> dict[str, Any]:
    """在一批候选里取**最深**那个（并列时**确定性**地择优，见排序键）。

    ★ 用 `min` 而不是 `max`：排序键的第一项是**深度**（越深越是现场），
      而第二项是"非框架 span 优先"（`0` 优先于 `1`）—— 两个方向不一致，
      用 `max` 会在第二项上取反（真机实测：选中了 FastAPI 的 "http send"）。
    """
    deepest = min(items, key=_floor_sort_key)
    propagated_only = kind == FLOOR_DEEPEST_PROPAGATED
    downstream = _text(deepest.get("downstream"))
    note = ""
    if propagated_only:
        note = (f"没有自身出错（intrinsic）的节点 → 取最深的**被调方失败**位置"
                f"（{deepest.get('service')}/{deepest.get('operation')}），"
                f"被调方=" + (downstream or "**取不到**（trace 与拓扑都没有给出被调方）"))
        if not downstream:
            note += "；★ 不把调用方当根因"
    return {
        "picked": [dict(deepest)],
        "kind": kind,
        "downstream_only": propagated_only,
        "downstream": downstream,
        "memories": [str(item) for item in memories][:3],
        "degraded": "" if not propagated_only else note,
        "note": note,
    }


def _floor_sort_key(item: Mapping[str, Any]) -> tuple[int, int, str]:
    """地板排序键（**越小越优先**）：深度取 `-depth`、非框架 span 取 0、再按 span_id。

    ★ 中间那一项为什么必要：真机实测（一次 DB 查询失败的 trace）里，同一个深度上
      既有 `client` 的 `SELECT`（**有 `db.statement`，那才是出错的位置**），
      也有 FastAPI 自动埋点产的 `internal` "http send" span（它只是把 5xx 写出去）。
      两者都 `status=error` 且深度相同，纯按 id 排序会选中后者 ——
      结论落在"发响应时报错"，而不是"那条 SQL 查不到表"。
      所以深度相同时**优先非 internal**（`server` / `client` 是真正的工作 span）。
    """
    kind = _text(item.get("kind")).lower()
    return (-int(item.get("depth") or 0), 0 if kind and kind != "internal" else 1,
            _text(item.get("span_id")))


def service_mention_terms(candidates: Sequence[Mapping[str, Any]],
                          traces: Sequence[Mapping[str, Any]]) -> set[str]:
    """后置校验用的"**允许提到**的服务名"集合（候选 ∪ 全部 trace 的服务）。

    ★ 为什么要给模型一份清单：这不是限制它措辞，而是**抓幻觉** ——
      模型写出一个全链路里都没出现过的服务名，就是它自己编的，
      此时必须弃用它的选择、退回代码的确定性地板。
    """
    names = {_text(item.get("service")) for item in candidates}
    for tree in traces:
        names.update(trace_service_names(tree))
    return {name for name in names if name}


def fingerprint_of(candidate: Mapping[str, Any]) -> str:
    """候选的指纹（缺就用四个维度现场算一个 —— 与合并层**同一份实现**）。"""
    found = _text(candidate.get("fingerprint"))
    if found and len(found) == FINGERPRINT_CHARS:
        return found
    return error_fingerprint(candidate.get("service"), candidate.get("operation"),
                             candidate.get("error_type"), candidate.get("error_text"))


def normalize_for_fingerprint(text: str) -> str:
    """给别处复用归一化（进出口只此一处，免得各写一份）。"""
    return normalize_detail(text)
