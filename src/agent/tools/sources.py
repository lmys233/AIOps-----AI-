"""适配各遥测数据源（Prometheus / Loki / Tempo / Postgres），把结局分成三类。

★ 三种结局必须分开，且只能由"谁能判断"的一方来判：
  · 查到了（**可能为空**）—— 适配器正常返回，`data=[]` 也算正常 → `ok=True, degraded=False, data=[...]`
  · **没查成**（网络/5xx/连不上）—— 适配器抛 `SourceUnavailable` → `ok=False, degraded=True, data=None`
  · **查询本身写错了**（400/422）—— 适配器抛 `QueryRejected` → `ok=False, error_code=TOOL_INVALID_PARAMS`（**可让模型自修**）

★ 为什么第三种要单独一类：Prometheus 返回 400 说明**模型写的 PromQL 语法不对**，
  把它归进"数据源不可用"会让 RCA 以为"指标这条路没有信号"，
  从而**排除掉本来正确的假设** —— 那是最容易翻车的地方。
  反过来，把"没查成"归进"参数错误"会让模型反复重写一个没问题的查询。

★ 空结果**绝不**抛异常：`data=[]` 是"查了，没异常"，是一条**有用的证据**
  （"这个时间窗里没有 ERROR 日志"本身就是结论的一部分）。
"""

from __future__ import annotations
from ..common.lifecycle import close_pool

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Final, Mapping, Optional, Sequence

import httpx

from ..common.query import (
    DEFAULT_SERIES_LIMIT,
    MAX_QUERY_POINTS,
    detail_truncation_note,
)

# ★ 三类"结局"的定义在 errors.py（工具层共用）；这里**再导出**，
#   方便"数据源适配器"的读者就近拿到，也避免同一个概念两处定义。
from .errors import QueryRejected, SourceUnavailable, ToolError, ToolRefused
from .fields import describe_limits


def _points_of(item: Mapping[str, Any]) -> list[list[float]]:
    """把一条序列的 `values` 归一成 `[[ts, value], ...]`（坏样本跳过，不炸）。

    ★ 抓取失败的样本 Prometheus 会写成 `"NaN"` —— 它**不是值**，
      这里连同其它非数值一起跳过。
    """
    points: list[list[float]] = []
    for pair in item.get("values") or []:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        try:
            points.append([float(pair[0]), float(pair[1])])
        except (TypeError, ValueError):
            continue
    return points

__all__ = [
    "SourceUnavailable",
    "QueryRejected",
    "ToolRefused",
    "ToolError",
    "RawOutcome",
    "HttpSource",
    "PrometheusSource",
    "LokiSource",
    "TempoSource",
    "SqlSource",
    "DEFAULT_HTTP_TIMEOUT_SEC",
    "DEFAULT_SERIES_LIMIT",
]

DEFAULT_HTTP_TIMEOUT_SEC: Final[float] = 10.0


@dataclass(frozen=True)
class RawOutcome:
    """适配器的原始产物（**尚未裁剪**，裁剪是执行器第 5 步的事）。"""

    data: Any
    raw_ref: str                       # 可回查的引用（raw_ref 必须能顺着它回查）
    note: str = ""
    #: ★★ 这份产物**本身是不是从本地副本读出来的**（默认 False）。
    #:   置 True 的一方在说："`data` 已经是那份副本的（可能被 max_chars 裁过的）文本，
    #:   **不要再落一次盘**" —— 否则拿裁过的文本覆盖原文，是不可逆的静默数据丢失。
    from_local_copy: bool = False


# ======================================================================
#  HTTP 数据源
# ======================================================================
class HttpSource:
    """统一三个遥测数据源的公共部分："不可用/被拒"的判定。

    ★ 允许注入 `client`（httpx.MockTransport / 假客户端）：离线单测要能
      精确模拟"数据源挂了"与"数据源好好的但没数据"这两条分支 ——
      这两条必须能区分开。
    """

    name: Final[str] = "http"

    def __init__(
        self,
        base_url: str,
        *,
        client: Optional[httpx.AsyncClient] = None,
        timeout_sec: float = DEFAULT_HTTP_TIMEOUT_SEC,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.timeout_sec = float(timeout_sec)
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_sec), follow_redirects=False
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get_json(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """GET 一个 JSON 接口，并按上面的三分法翻译结局。"""
        url = f"{self.base_url}{path}"
        try:
            resp = await self._client.get(url, params=dict(params))
        except httpx.TimeoutException as exc:
            raise SourceUnavailable(
                "TimeoutException", f"{self.name} 请求超时（{self.timeout_sec:g}s）：{url}"
            ) from exc
        except httpx.HTTPError as exc:
            raise SourceUnavailable(
                type(exc).__name__, f"{self.name} 不可达：{type(exc).__name__}: {exc}"
            ) from exc

        if resp.status_code in (400, 404, 422):
            # ★ 4xx = "你这个查询有问题"，不是"数据源不可用"
            raise QueryRejected(
                f"{self.name} 拒绝了查询（HTTP {resp.status_code}）：{resp.text[:200]}",
                status=resp.status_code,
            )
        if resp.status_code >= 500:
            raise SourceUnavailable(
                f"HTTP{resp.status_code}", f"{self.name} 返回 HTTP {resp.status_code}"
            )
        try:
            body = resp.json()
        except ValueError as exc:      # 200 但不是 JSON —— 多半是反代/门户页
            raise SourceUnavailable(
                "InvalidResponse", f"{self.name} 返回的不是 JSON（{resp.text[:120]!r}）"
            ) from exc
        if not isinstance(body, dict):
            raise SourceUnavailable("InvalidResponse", f"{self.name} 返回的 JSON 不是对象")
        return body

    async def health(self) -> tuple[bool, str]:
        raise NotImplementedError


def _dicts(items: Any) -> list[dict[str, Any]]:
    """只留下确实是对象的条目（上游偶尔混进标量，不许因此炸掉整次取数）。"""
    return [item for item in (items or []) if isinstance(item, dict)]


def _fold_notes(notes: Sequence[str]) -> str:
    """把留痕拼成一句话括号形式（**去重**：同一条不重复出现）。"""
    joined = "；".join(dict.fromkeys(notes))
    return f"（{joined}）" if joined else ""


def _parts(*pieces: Any) -> str:
    """`raw_ref` 的拼装：**空的那几段直接不出现**（不留孤零零的分隔符）。

    ★ 为什么必须这样拼：`raw_ref` 是 `raw_read` 的钥匙，而"窗口没被截短"时
      局限那一段是空的 —— 直接字符串相加会留下一个结尾的 ` | `，
      落在副本文件名摘要里就成了一份**取不回来**的记录（内容明明存着）。
    """
    return " | ".join(str(piece) for piece in pieces if piece)


def _apply_series_limit(result: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], list[str]]:
    """按 `limit` 裁序列并给出留痕（★ 判据用**原始条数**：`>= limit` 就说明还有更多）。"""
    kept = max(1, int(limit))
    if len(result) < kept:
        return result, []
    notes = [describe_limits(result, limit=kept, unit="条序列")]
    if len(result) > kept:
        notes.append(f"序列 {len(result)} 条超过 limit={limit} → 只保留前 {kept} 条")
    return result[:kept], notes


def _series_entry(item: Mapping[str, Any], notes: list[str]) -> dict[str, Any]:
    """一条序列 → 结果条目（点数超上限时截断，并把**规模事实**留在 `points_total`）。"""
    points = _points_of(item)
    entry: dict[str, Any] = {"metric": item.get("metric") or {}, "points": points,
                             "points_total": len(points)}
    if len(points) > MAX_QUERY_POINTS:
        notes.append(f"单序列 {len(points)} 点超过上限 {MAX_QUERY_POINTS} → "
                     f"只保留最近 {MAX_QUERY_POINTS} 点（原 {len(points)} 点）")
        entry["points"] = points[-MAX_QUERY_POINTS:]
        entry["points_truncated"] = True
    return entry


def _promql_ref(query: str, step: str, start: float, end: float, limit: int,
                detail: str) -> str:
    """`raw_ref` 的拼装（含**窗口与明细局限**这句 —— 事后回查也要能看见它）。"""
    limits = detail_truncation_note(max(0.0, end - start))
    return _parts(f"promql: {query}", f"step={step}", f"[{start:.0f},{end:.0f}]",
                  f"limit={limit}{detail}", limits)


# ----------------------------------------------------------------------
class PrometheusSource(HttpSource):
    """只读查询 Prometheus 的 HTTP API（PromQL）。"""

    name = "Prometheus"

    async def catalogue(self,query,*,start,end,limit=64,cursor=None):
        from .metric_catalog import from_prometheus
        return await from_prometheus(self,query,start=start,end=end,limit=limit,cursor=cursor)

    async def query_range(
        self, query: str, *, start: float, end: float, step: str = "30s",
        limit: int = DEFAULT_SERIES_LIMIT,
    ) -> RawOutcome:
        """一段区间的取值。★ 两个上限都在这里兜住，**不许静默超限**：

        · `limit`：最多返回多少条**序列** —— 工具 schema 声明的就是这个语义，
          但修前这里**根本没接**（声明了参数却不生效）；
        · `MAX_QUERY_POINTS`：单条序列最多多少**点**（Prometheus 硬限 11000；
          贴着墙走会在默认 7 天窗下直接被拒：实测 `7d @ step=30s` = 20160 点）。
        ★ 两种裁剪都写进 `raw_ref` / `note` —— 裁了却不说是最坏的一种"静默降级"。
        """
        body = await self._get_json("/api/v1/query_range", {
            "query": query, "start": start, "end": end, "step": step,
        })
        if body.get("status") != "success":
            # 200 但 status=error：也是"查询有问题"（如结果太多被拒）
            raise QueryRejected(f"Prometheus 返回 status={body.get('status')!r}："
                                f"{str(body.get('error'))[:160]}")
        result = _dicts((body.get("data") or {}).get("result"))
        result, notes = _apply_series_limit(result, limit)
        series = [_series_entry(item, notes) for item in result]
        detail = _fold_notes(notes)
        return RawOutcome(
            data=series,
            # ★ 局限那句由 `detail_truncation_note` 用**原始窗口长度**现算：
            #   明细可能已被截到最近 12.5h，模型必须知道"这不是整窗的明细"。
            raw_ref=_promql_ref(query, step, start, end, limit, detail),
            note=f"{len(series)} 条时间序列{detail}",
        )

    async def health(self) -> tuple[bool, str]:
        try:
            resp = await self._client.get(f"{self.base_url}/-/healthy")
            return resp.status_code == 200, f"HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}: {exc}"


# ----------------------------------------------------------------------
class LokiSource(HttpSource):
    """只读查询 Loki 的 HTTP API（LogQL）。"""

    name = "Loki"

    async def query_range(
        self, query: str, *, start: float, end: float, limit: int = 200,
        direction: str = "backward",
    ) -> RawOutcome:
        body = await self._get_json("/loki/api/v1/query_range", {
            "query": query,
            "start": int(start * 1e9),           # Loki 用纳秒
            "end": int(end * 1e9),
            "limit": limit,
            "direction": direction,
        })
        if body.get("status") not in (None, "success"):
            raise QueryRejected(f"Loki 返回 status={body.get('status')!r}："
                                f"{str(body.get('error'))[:160]}")
        result = (body.get("data") or {}).get("result") or []
        streams: list[dict[str, Any]] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            entries: list[dict[str, Any]] = []
            for pair in item.get("values") or []:
                if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                    continue
                entries.append({"ts_ns": str(pair[0]), "line": pair[1]})
            # ★ 按时间倒序（Loki 默认 backward）：最新的日志在最前面才有用
            streams.append({"stream": item.get("stream") or {}, "entries": entries})
        # ★★ **被 `limit` 截断时必须留痕**（修前这条是**静默**的：模型只看到 200 行，
        #   就以为窗口里只有 200 行 —— 这正是要修的诚实性缺口）。
        rows = sum(len(stream["entries"]) for stream in streams)
        limit_note = describe_limits(streams, limit=limit, unit="行日志",
                                    source=f"direction={direction}")
        if rows >= max(1, int(limit)):
            limit_note += "；★ 本次只取回最近这么多行，窗口内可能还有更早的日志"
        return RawOutcome(
            data=streams,
            raw_ref=_parts(f"logql: {query}", f"limit={limit}",
                           f"direction={direction}", f"[{start:.0f},{end:.0f}]",
                           limit_note, detail_truncation_note(max(0.0, end - start))),
            note=f"{len(streams)} 条流 / {rows} 行；{limit_note}",
        )

    async def health(self) -> tuple[bool, str]:
        try:
            resp = await self._client.get(f"{self.base_url}/ready")
            return resp.status_code == 200, f"HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}: {exc}"


# ----------------------------------------------------------------------
class TempoSource(HttpSource):
    """只读查询 Tempo 的 HTTP API（TraceQL）。"""

    name = "Tempo"

    async def search(
        self, query: str, *, start: float, end: float, limit: int = 20
    ) -> RawOutcome:
        body = await self._get_json("/api/search", {
            "q": query, "start": int(start), "end": int(end), "limit": limit,
        })
        traces = body.get("traces") or []
        out: list[dict[str, Any]] = []
        for item in traces:
            if not isinstance(item, dict):
                continue
            out.append({
                "trace_id": item.get("traceID") or item.get("traceId"),
                "root_service": item.get("rootServiceName"),
                "root_span": item.get("rootTraceName"),
                "duration_ms": item.get("durationMs"),
                "start_ns": item.get("startTimeUnixNano"),
            })
        # ★★ 与日志同一口径：**被 `limit` 截断时留痕**（trace 按耗时/时间取前 N 条，
        #   "只有这么多" ≠ "窗口里只有这么多"）。
        limit_note = describe_limits(out, limit=limit, unit="条 trace")
        if len(out) >= max(1, int(limit)):
            limit_note += "；★ 本次只取回最近这么多条，窗口内可能还有更早的 trace"
        return RawOutcome(
            data=out,
            raw_ref=_parts(f"traceql: {query}", f"limit={limit}",
                           f"[{start:.0f},{end:.0f}]", limit_note,
                           detail_truncation_note(max(0.0, end - start))),
            note=f"{len(out)} 条 trace；{limit_note}",
        )

    async def fetch_trace(self, trace_id: str) -> RawOutcome:
        """按 trace_id 取**整条** trace 的 span 树（错误 → trace 下钻的那一步）。

        ★★ 两条路，先试"一步到位"的那条：
          ① `GET /api/v2/traces/{id}`（Tempo 2.x，OTLP 形状的 span 树）；
          ② 退到 `GET /api/traces/{id}`（老接口，返回 `batches`）。
        两条都拿不到 → 抛 `SourceUnavailable`（**"没查成"**），
        绝不用"空 span 列表"冒充"这条 trace 没有断点"（那会把查询失败读成排除）。

        ★★ 每条路的返回形状**两种都试**：真机的 v2 顶层是 `{"trace": {...}}`
          （比旧形状多一层），老接口的 span 挂在 `batches[].scopeSpans[].spans`
          （比旧形状多一层 `scopeSpans`）。解析器内部按"真机 → 旧"的顺序试，
          谁先出 span 用谁，并在 `note` 里写清**走的是哪一种形状**。
        """
        clean = str(trace_id or "").strip()
        if not clean:
            raise QueryRejected("trace_id 为空 —— 没有 id 就没有下钻的起点")
        errors: list[str] = []
        #: ★★ 两条路各自**同时**兼容"真机形状"与"旧形状"（解析器里两种都试，谁先出
        #:   span 用谁）—— 所以这里不需要第三、第四条退路：形状差异都在解析器内部收口。
        for path, parser in ((f"/api/v2/traces/{clean}", _spans_from_v2),
                             (f"/api/traces/{clean}", _spans_from_legacy)):
            try:
                body = await self._get_json(path, {})
            except (QueryRejected, SourceUnavailable) as exc:
                errors.append(f"{path}: {type(exc).__name__}: {exc}")
                continue
            _path, _label, spans = parser(body)
            if spans:
                note = f"取回 {len(spans)} 个 span（{path}：{_label}）"
                if errors:
                    note += f"；★ 更优先的那条（没有 span）：{'、'.join(errors)}"
                return RawOutcome(
                    data={"trace_id": _body_trace_id(body) or clean, "spans": spans,
                          "truncated": False},
                    raw_ref=f'traceql: {{ trace:id = "{clean}" }} | fetch {path}',
                    note=note)
            errors.append(f"{path}（没有 span）")
        raise SourceUnavailable(
            "TraceNotFound",
            f"按 trace_id={clean} 取整条 trace 失败：{'；'.join(errors[:2])}")

    async def health(self) -> tuple[bool, str]:
        try:
            resp = await self._client.get(f"{self.base_url}/ready")
            return resp.status_code == 200, f"HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}: {exc}"


# ======================================================================
#  整条 trace → span 树（两种 Tempo 返回形状各一个解析器）
# ======================================================================
#: span 属性的取值包装（OTLP JSON 的 `{"stringValue": …}` 形状）
_ATTR_VALUE_KEYS: Final[tuple[str, ...]] = (
    "stringValue", "intValue", "doubleValue", "boolValue", "value")


def _parse_span(raw: Mapping[str, Any], trace_id: str) -> dict[str, Any]:
    """一条 span → 我们自己的扁平形状（字段名与 OpenTelemetry 的语义对齐）。"""
    start_ns = raw.get("startTimeUnixNano") or raw.get("startTime") or 0
    end_ns = raw.get("endTimeUnixNano") or raw.get("endTime") or 0
    duration_ms = _duration_ms(start_ns, end_ns, raw.get("durationMs"))
    return {
        "trace_id": str(raw.get("traceId") or raw.get("traceID") or trace_id),
        "span_id": str(raw.get("spanId") or raw.get("spanID") or ""),
        "parent_span_id": str(raw.get("parentSpanId") or raw.get("parentSpanID") or ""),
        "service": str(raw.get("service") or raw.get("serviceName") or ""),
        "name": str(raw.get("name") or raw.get("operationName") or ""),
        "kind": _span_kind(raw.get("kind")),
        "status": _status_code(raw),
        "start_ns": _as_text(start_ns),
        "duration_ms": duration_ms,
        "attributes": _flat_attributes(raw.get("attributes")),
    }


def _duration_ms(start_ns: Any, end_ns: Any, explicit: Any) -> float:
    """耗时（毫秒）：优先用起止时间算，算不出来才退回上游给的字段。"""
    try:
        delta = float(end_ns) - float(start_ns)
    except (TypeError, ValueError):
        delta = 0.0
    if delta > 0:
        return round(delta / 1e6, 3)
    try:
        return float(explicit or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _span_kind(value: Any) -> str:
    """span kind → `server` / `client` / …（认不出来的原样小写，**不猜**）。"""
    text = str(value or "").strip().lower()
    for name in ("server", "client", "producer", "consumer", "internal"):
        if text == name or text == f"span_kind_{name}" or text.endswith(f"_{name}"):
            return name
    return text


def _status_code(raw: Mapping[str, Any]) -> str:
    """span 的状态码 → `error` / `ok` / `unset`（OTLP 里是 `{"code": "STATUS_CODE_ERROR"}`）。"""
    status = raw.get("status")
    if isinstance(status, Mapping):
        status = status.get("code") or status.get("message")
    return str(status or "").strip().lower()


def _flat_attributes(raw: Any) -> dict[str, Any]:
    """属性列表 → 扁平字典（只留标量；不把嵌套结构塞进证据里）。"""
    out: dict[str, Any] = {}
    if isinstance(raw, Mapping):
        return {str(key): value for key, value in raw.items()
                if isinstance(value, (str, int, float, bool))}
    for item in raw or []:
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("key") or "")
        if not key:
            continue
        value = item.get("value")
        if isinstance(value, Mapping):
            for name in _ATTR_VALUE_KEYS:
                if name in value:
                    value = value[name]
                    break
        if isinstance(value, (str, int, float, bool)):
            out[key] = value
    return out


def _as_text(value: Any) -> str:
    return "" if value in (None, "") else str(value)


def _span_list(*groups: Any) -> list[dict[str, Any]]:
    """一组组 `scopeSpans`（或它们的旧名字）→ 拉平的 span 原始列表（**两种名字都认**）。"""
    out: list[dict[str, Any]] = []
    for group in groups:
        for scope in _dicts(group):
            out.extend(_dicts(scope.get("spans")))
    return out


def _body_trace_id(body: Mapping[str, Any]) -> str:
    """返回体里的 trace id（真机 v2 的 id 在 `trace` 里，旧形状在顶层）。"""
    nested = body.get("trace")
    for source in (body, nested if isinstance(nested, Mapping) else {}):
        found = source.get("traceID") or source.get("traceId")
        if found:
            return str(found)
    return ""


def _spans_from_v2(body: Mapping[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    """`/api/v2/traces/{id}` → `(路径, 形状标签, span 列表)`。

    ★★ **真机形状在 `trace` 里**（顶层只有 `trace` 一个键）：span 落在
      `trace.resourceSpans[].scopeSpans[].spans` —— 读取时**少一层就会判成"没有 span"**，
      而那会让整条下钻在真机上永远走不通（取不到 ≠ 没有断点，两者差得很远）。
    ★ 旧形状（span 直接挂在顶层 `resourceSpans`）**继续支持**：先试真机那条，再退旧形状。
    """
    trace = body.get("trace") if isinstance(body.get("trace"), Mapping) else {}
    trace_id = _body_trace_id(body)
    out: list[dict[str, Any]] = []
    for label, holder in (("v2 真机形状（trace 里）", trace),
                          ("v2 旧形状（顶层）", body)):
        for resource in _dicts(holder.get("resourceSpans")):
            service = _service_of_resource(resource)
            for raw in _span_list(resource.get("scopeSpans"),
                                 resource.get("instrumentationLibrarySpans"),
                                 [resource] if resource.get("spans") is not None else []):
                span = _parse_span(raw, trace_id)
                span["service"] = span["service"] or service
                out.append(span)
        if out:
            return "/api/v2/traces", label, out
    return "/api/v2/traces", "v2（两种形状都没有 span）", out


def _spans_from_legacy(body: Mapping[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    """老接口 `/api/traces/{id}` → `(路径, 形状标签, span 列表)`。

    ★ `service.name` 有时在 span 的 attributes 里、有时在 `batches[].resource` 上 ——
      两处都读（少读一处就会得到一大批**没有服务名**的 span，断点就没法归属）。
    ★★ **真机形状是 `batches[].scopeSpans[].spans`**（多一层 `scopeSpans`）；
      旧形状是 `batches[].spans`。两种都读，先真机、再旧形状。
    """
    trace_id = _body_trace_id(body)
    out: list[dict[str, Any]] = []
    for label, holder in (("老接口真机形状（batches[].scopeSpans）", body),
                          ("老接口旧形状（batches[].spans）", {})):
        for batch in _dicts(holder.get("batches")):
            service = _service_of_resource(batch.get("resource"))
            for raw in _span_list(batch.get("scopeSpans"),
                                 batch.get("instrumentationLibrarySpans"),
                                 [batch] if batch.get("spans") is not None else []):
                span = _parse_span(raw, trace_id)
                attrs = span["attributes"]
                span["service"] = (span["service"] or str(attrs.get("service.name") or "")
                                   or service)
                out.append(span)
        if out:
            return "/api/traces", label, out
    return "/api/traces", "老接口（两种形状都没有 span）", out


def _service_of_resource(holder: Any) -> str:
    """从资源属性里取 `service.name`。

    ★★ 两种形状都要认（各差一层）：
      · 真机 `/api/v2/traces/{id}`：`resourceSpans[].resource.attributes` ——
        `service.name` 在**内层的 `resource`** 上（只读外层会得到空串）；
      · 老接口 `batches[].resource.attributes`：传进来的就是那个 `resource` 本身。
    ★ 服务名读不出来 = 断点没法归属（归因主语丢了）—— 而且**不会报错**，只是静默变空，
      所以这一条必须两种形状都覆盖到。
    """
    wrapper = holder if isinstance(holder, Mapping) else {}
    inner = wrapper.get("resource")
    for candidate in (inner if isinstance(inner, Mapping) else {}, wrapper):
        attributes = _flat_attributes(candidate.get("attributes"))
        name = str(attributes.get("service.name") or "")
        if name:
            return name
    return ""


# ======================================================================
#  SQL 数据源（Postgres）
# ======================================================================
FetchFn = Callable[[str, tuple[Any, ...]], Awaitable[list[dict[str, Any]]]]


@dataclass
class SqlSource:
    """用只读角色查询 Postgres（Agent 的读库入口）。

    ★ 两种构造方式：
      · `SqlSource(dsn=...)`          —— 真连（asyncpg 连接池，进程内复用）
      · `SqlSource(fetch_fn=...)`     —— 注入一个假查询函数（离线单测）

    ★ 只读**由数据库角色保证**，不是靠这里不写 INSERT：
      `aiops` 读角色没有写权限，所以"忘写 WHERE"最坏也就是多读几行，
      不会改坏数据。
    """

    dsn: str = ""
    fetch_fn: Optional[FetchFn] = None
    timeout_sec: float = DEFAULT_HTTP_TIMEOUT_SEC
    _pool: Any = field(default=None, init=False, repr=False)

    async def _get_pool(self) -> Any:
        if self._pool is None:
            try:
                import asyncpg  # noqa: PLC0415
            except ImportError as exc:                     # pragma: no cover
                raise SourceUnavailable("NotInstalled", "asyncpg 未安装") from exc
            try:
                self._pool = await asyncpg.create_pool(
                    dsn=self.dsn, min_size=1, max_size=4,
                    timeout=self.timeout_sec,
                )
            except Exception as exc:  # noqa: BLE001 —— 连不上就是"没查成"
                raise SourceUnavailable(
                    type(exc).__name__, f"连不上 Postgres：{type(exc).__name__}: {exc}"
                ) from exc
        return self._pool

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if self.fetch_fn is not None:
            return await self.fetch_fn(sql, args)
        pool = await self._get_pool()
        try:
            rows = await pool.fetch(sql, *args)
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            # ★ 语法/权限类错误 = "查询有问题"（可让模型改）；连接类 = "没查成"
            if name in ("PostgresSyntaxError", "UndefinedColumnError",
                        "UndefinedTableError", "InsufficientPrivilegeError"):
                raise QueryRejected(f"SQL 被拒（{name}）：{exc}") from exc
            raise SourceUnavailable(name, f"SQL 执行失败（{name}）：{exc}") from exc
        return [dict(row) for row in rows]

    async def aclose(self) -> None:
        if self._pool is not None:
            await close_pool(self._pool)
            self._pool = None
