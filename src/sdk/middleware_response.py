"""响应增强中间件：在 ASGI 层给业务响应补两个可观测性字段。

★ 要解决的问题：契约要求**每个业务响应**都含
  `_server_elapsed_ms` 与 `service`，而它们是**可观测性契约**、不是业务字段。

  重规划前的做法是业务代码里调 **9 次** `business_response(...)` 包一层 ——
  那就不是"零改动"了。本模块把它降到 **0**：
  业务 handler 直接 `return {...}` 普通 dict，中间件在 **ASGI 层**补齐。

★ 计时点定义（**方案 B**，写死在这里与接入指南里，不允许各处自己解释）：

  | 口径 | 说明 |
  |---|---|
  | **起点** | 请求进入本中间件那一刻（`scope["type"] == "http"`） |
  | **终点** | **应用产出响应那一刻** —— 第一次 `http.response.start` 的 `send` **之前** |
  | **包含** | 业务处理 + 响应序列化 + 中间件链开销 |
  | **不包含** | 往 socket 写出的时间、客户端读取时间 |

  ★ 为什么终点选在这里：要报的是"服务端自报的**内部**处理耗时"，
    网络写出**不属于**服务端内部处理 —— 而且那部分正是 client-server gap
    要分离出去的量。把写出时间算进来，gap 就会被系统性高估。

  ★ **4 个服务必须用同一种口径**：混用"手写"与"SDK 自动"会让 gap 算错，
    所以 SDK 显式声明口径（`TIMING_POINT` 常量 + 本文档）。

★ 必须处理的 **8 个边界**（不处理就会产出坏响应，逐条见 `_decide` 与 `__call__`）：

  | 情形 | 处理 |
  |---|---|
  | 非 `application/json`（204 / 纯文本 / 重定向） | 原样透传，不注入 |
  | `more_body=True`（流式 / SSE） | 原样透传 —— **缓冲就会破坏流式** |
  | 响应体 > 1MB | 放弃注入、原样透传、**告警一次** |
  | `Content-Encoding`（gzip 等） | 原样透传 + 告警（我们不在压缩**之内**，见下） |
  | `Content-Length` | **必须重算**，否则客户端截断 |
  | 已有这两个字段 | **保留业务的值**，不覆盖 |
  | JSON 解析失败 | 原样透传 + 告警 |
  | 4xx / 5xx | **同样注入** —— 最需要对比 gap 的恰恰是"失败那一次" |

★ 中间件顺序（写进接入指南，避免踩坑）：

  注入必须发生在**压缩之内**（即比压缩中间件更靠近应用）。
  本 SDK 是**从外部**把业务 app 包起来的，所以如果业务自己在 app 内挂了
  GZip 之类的压缩中间件，响应到这里时**已经压缩**，我们只能原样透传 + 告警。
  这不是偷懒：框架内部的中间件栈在**首次请求前**就已经构建完毕，
  从外部插进去是不安全的 —— 宁可明确告警并写进文档，
  也不做"看起来生效其实时序不确定"的事。
"""

from __future__ import annotations

import json
from time import perf_counter
from typing import Any, Awaitable, Callable, Optional

from .middleware_http import route_template
from .contract import (
    PROBE_PATHS,
    RESPONSE_FIELD_ELAPSED_MS,
    RESPONSE_FIELD_SERVICE,
)
from .definitions import SERVER_SELF_REPORTED
from .state import Runtime, safe_record

__all__ = ["ResponseEnhancer", "TIMING_POINT"]

# 计时口径的名字 —— 变更这里等于改变度量语义，必须同步文档与评测口径
TIMING_POINT = "app-produced-response (方案 B)"

# 注入上限：超过就放弃（缓冲大响应体意味着把内存交给客户端控制）
MAX_BODY_BYTES = 1 << 20  # 1MB

# 透传原因进告警去重键时截断的长度：同一种原因（前缀相同）只吵一次，
# 又不至于让"原因里带了路径/状态码"把同一个原因拆成很多条告警
_SKIP_REASON_KEY_CHARS = 24

_JSON_CT = "application/json"


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> Optional[bytes]:
    for key, value in headers:
        if key.lower() == name:
            return value
    return None


def _set_header(headers: list[tuple[bytes, bytes]], name: bytes, value: bytes) -> list[tuple[bytes, bytes]]:
    """替换（或追加）一个 header，保持其它 header 相对顺序。"""
    lowered = name.lower()
    replaced = False
    out: list[tuple[bytes, bytes]] = []
    for key, val in headers:
        if key.lower() == lowered:
            if not replaced:
                out.append((key, value))
                replaced = True
            continue
        out.append((key, val))
    if not replaced:
        out.append((name, value))
    return out


class ResponseEnhancer:
    """在 ASGI 层给 JSON 响应补 `_server_elapsed_ms` 与 `service`。"""

    def __init__(self, app: Any, runtime: Runtime) -> None:
        self._app = app
        self._runtime = runtime
        self._service = runtime.service_name

    # ------------------------------------------------------------------
    async def __call__(self, scope: dict[str, Any],
                       receive: Callable[[], Awaitable[dict[str, Any]]],
                       send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if path in PROBE_PATHS:
            # 探针由 SDK 自己生成（`ObservedApp` 已直接应答），走不到这里；
            # 留着这道判断是为了"万一将来探针改成经业务 app"也不会被注入。
            await self._app(scope, receive, send)
            return

        started = perf_counter()
        state: dict[str, Any] = {
            "started": started,
            "elapsed_ms": None,
            "start": None,
            "buffered": [],
            "buffered_bytes": 0,
            "passthrough": False,
            "reason": None,
        }

        async def send_wrapper(message: dict[str, Any]) -> None:
            mtype = message.get("type")
            if mtype == "http.response.start":
                await self._on_start(message, scope, state)
                return
            if mtype == "http.response.body":
                await self._on_body(message, send, state)
                return
            await send(message)

        await self._app(scope, receive, send_wrapper)

        # 安全兜底：应用发过 start 却没发 body（协议违约）——
        # 补一个空 body，否则客户端会一直等下去。
        if state["start"] is not None and not state.get("flushed"):
            self._runtime.warn_once(
                "response_body_missing",
                "应用发过 http.response.start 却没有发 http.response.body —— "
                "已补一个空 body（协议违约，业务侧应检查）",
            )
            await self._flush(send, state)

    # ------------------------------------------------------------------
    #  ★ 计时终点：第一次 http.response.start 的 send **之前**
    # ------------------------------------------------------------------
    async def _on_start(self, message: dict[str, Any], scope: dict[str, Any],
                        state: dict[str, Any]) -> None:
        elapsed_sec = perf_counter() - state["started"]
        elapsed_ms = round(elapsed_sec * 1000, 3)
        state["elapsed_ms"] = elapsed_ms
        state["start"] = message

        # ★ 指标与响应体**同源**：同一个 elapsed_sec 既进直方图、又变成响应字段。
        #   否则"拿指标做基线"与"拿响应算 gap"会得出互相矛盾的结论。
        endpoint = self._endpoint_label(scope)
        safe_record(
            self._runtime, "server_self_reported", "服务端自报耗时直方图",
            lambda: SERVER_SELF_REPORTED.labels(
                service=self._service, endpoint=endpoint,
            ).observe(elapsed_sec),
        )
        state["endpoint"] = endpoint

        # 决定这条响应能不能注入（8 个边界里的一部分在这里判掉）
        state["passthrough"], state["reason"] = self._decide(message)

    def _endpoint_label(self, scope: dict[str, Any]) -> str:
        """`endpoint` 标签：**必须是有界的**（要么路由模板，要么一个常量）。

        ★★ 这里以前返回 `scope["path"]`（**原始请求路径**），
          而注释还写着"与 HTTP 指标中间件保持一致…退化为有界值" —— 做的是反的。
          `prometheus_client` **从不淘汰标签组合**，于是每个不同的 404 / 扫描路径
          （`/wp-login.php`、`/.env`、`/admin`…）都会变成一个独立 series，
          一个扫描器就能让**被观测业务进程自己**内存持续增长直到 OOM。
          → **直接复用** `middleware_http.route_template()`：同一个口径只有一处实现
            （它还负责 `warn_once`，所以"有人在扫接口"这件事照样看得见）。
        """
        return route_template(scope, self._runtime)

    def _decide(self, message: dict[str, Any]) -> tuple[bool, Optional[str]]:
        """返回 `(是否原样透传, 原因)`。原因用于告警文案。"""
        headers = list(message.get("headers") or [])
        status = int(message.get("status", 200))

        ctype = (_header(headers, b"content-type") or b"").decode("latin-1").lower()
        if _JSON_CT not in ctype:
            return True, f"content-type 不是 JSON（{ctype or '未声明'}，status={status}）"

        # ★ 我们是从**外部**包住业务 app 的，所以业务自己挂的压缩中间件在内层，
        #   响应到这里已经压缩 —— 注入会破坏它。告警让人知道"这条没注入"。
        if _header(headers, b"content-encoding"):
            return True, "响应已被压缩（Content-Encoding）—— 注入必须在压缩之内" \
                         "（更靠近应用），本 SDK 从外部包装，做不到；详见接入指南"

        # Content-Length 这里**不判**：即使存在也要重算（见 _flush）
        return False, None

    # ------------------------------------------------------------------
    async def _on_body(self, message: dict[str, Any], send: Callable[..., Awaitable[None]],
                       state: dict[str, Any]) -> None:
        more = bool(message.get("more_body", False))
        body = message.get("body", b"") or b""

        # ---- 已决定透传：把 start（若还没发）发掉，然后这条原样转发 ----
        if state["passthrough"]:
            if not state.get("start_sent"):
                await self._warn_skip(state)
                await send(state["start"])
                state["start_sent"] = True
            await send(message)
            if not more:
                state["flushed"] = True
            return

        # ---- 边界：流式。第一条分片就带 more_body → 立刻放弃缓冲、原样透传 ----
        #      ★ 缓冲流式响应 = 把 SSE / 大文件下载变成"等最后一次性吐出来"，
        #        客户端会以为服务挂了。宁可放弃注入。
        if more and not state["buffered"]:
            state["passthrough"] = True
            state["reason"] = "流式响应（more_body=True）—— 缓冲会破坏流式"
            await self._warn_skip(state)
            await send(state["start"])
            state["start_sent"] = True
            await send(message)
            return

        # ---- 缓冲 ----
        state["buffered"].append(body)
        state["buffered_bytes"] += len(body)

        # ---- 边界：超大。放弃注入，把已缓冲的一次性发出去（字节流等价）----
        if state["buffered_bytes"] > MAX_BODY_BYTES:
            state["passthrough"] = True
            state["reason"] = f"响应体超过 {MAX_BODY_BYTES} 字节上限"
            await self._warn_skip(state)
            await send(state["start"])
            state["start_sent"] = True
            await send({
                "type": "http.response.body",
                "body": b"".join(state["buffered"]),
                "more_body": more,
            })
            state["buffered"] = []
            if not more:
                state["flushed"] = True
            return

        if more:
            return  # 还没到齐，继续等

        await self._flush(send, state)

    async def _warn_skip(self, state: dict[str, Any]) -> None:
        """透传时告警一次（采不到就要吵，但不刷屏）。"""
        reason = state.get("reason")
        if not reason:
            return
        self._runtime.warn_once(
            f"response_inject_skipped:{reason[:_SKIP_REASON_KEY_CHARS]}",
            f"未注入 {RESPONSE_FIELD_ELAPSED_MS}/{RESPONSE_FIELD_SERVICE}：{reason}",
        )

    async def _flush(self, send, state) -> None:
        """把 start + 完整 body 发出去（必要时注入并重算 Content-Length）。"""
        if state.get("flushed"):
            return
        state["flushed"] = True
        state["start_sent"] = True

        start = state["start"]
        body = b"".join(state["buffered"])
        state["buffered"] = []

        if state["passthrough"]:
            await self._warn_skip(state)
            await send(start)
            await send({"type": "http.response.body", "body": body, "more_body": False})
            return

        new_body, injected = self._inject(body, state["elapsed_ms"])
        if not injected:
            await send(start)
            await send({"type": "http.response.body", "body": body, "more_body": False})
            return

        # ★ 边界：Content-Length **必须重算**，否则客户端按旧长度读取 → 截断
        headers = _set_header(list(start.get("headers") or []),
                              b"content-length", str(len(new_body)).encode("ascii"))
        await send({**start, "headers": headers})
        await send({"type": "http.response.body", "body": new_body, "more_body": False})

    # ------------------------------------------------------------------
    def _inject(self, body: bytes, elapsed_ms: Optional[float]) -> tuple[bytes, bool]:
        """补两个字段。返回 `(新 body, 是否真的改动了)`。

        ★ **已有则保留**（刻意的兼容语义）：业务若自己报了更精确的值
          （在 handler 内部打点），SDK **不覆盖**。于是"手写"与"SDK 自动"
          可以共存，不需要一次性全改完 —— 这也是本任务能被增量落地的前提。

        ★ 但有一种情况**必须覆盖**（本项目真实形态逼出来的，见下）：

          **业务把下游的完整响应原样转发了。** 例如 gateway 的响应顶层
          就是 `dict(order_body)` —— 于是 order 注入的 `service="order"` 与
          order 的 `_server_elapsed_ms` 一起被带进了 gateway 的响应。

          按"已有则保留"照单全收的后果很严重：

          | 后果 | 说明 |
          |---|---|
          | 上游算 gap 会用错数 | 拿 gateway 的响应算 gap，减掉的却是 **order** 的自报耗时 |
          | `service` 自相矛盾 | 一个从 gateway 出去的响应写着 `service="order"` |
          | 逐跳证据反而被抹平 | 两层耗时变成同一个值，看不出"哪一跳慢" |

          ★ 判据：`service` 字段就是那个**归属标记** —— 它存在且**不等于本服务**时，
            说明这两个字段是**下游带回来的**，不是本服务测的。
            此时两者都覆盖成本服务的值，并**告警一次**
            （这会影响所有上游的 gap 计算，值得让人知道）。
        """
        if not body:
            return body, False
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            # 边界：JSON 解析失败 → 原样透传 + 告警（绝不改坏别人的响应）
            self._runtime.warn_once(
                "response_inject_parse_failed",
                f"响应声明是 JSON 但解析失败（{type(exc).__name__}: {exc}）—— "
                f"原样透传，未注入 {RESPONSE_FIELD_ELAPSED_MS}",
            )
            return body, False

        if not isinstance(payload, dict):
            # JSON 但不是对象（数组/标量）：无处可挂字段，原样透传
            return body, False

        foreign = payload.get(RESPONSE_FIELD_SERVICE)
        forwarded = isinstance(foreign, str) and bool(foreign) and foreign != self._service
        if forwarded:
            self._runtime.warn_once(
                "response_foreign_service_fields",
                f"响应体里带着**别的服务**的可观测字段（service={foreign!r}）—— "
                f"业务把下游响应原样转发了。已覆盖为本服务（{self._service}）的值；"
                f"否则上游用这条响应算 client-server gap 会减错数。"
                f"（下游的原始自报值仍在响应体自己的嵌套结构里，未受影响）",
            )
            payload[RESPONSE_FIELD_ELAPSED_MS] = elapsed_ms
            payload[RESPONSE_FIELD_SERVICE] = self._service
            return json.dumps(payload, ensure_ascii=False).encode("utf-8"), True

        changed = False
        # `setdefault` 语义：**已有就保留**，缺了才补
        if RESPONSE_FIELD_ELAPSED_MS not in payload:
            payload[RESPONSE_FIELD_ELAPSED_MS] = elapsed_ms
            changed = True
        if RESPONSE_FIELD_SERVICE not in payload:
            payload[RESPONSE_FIELD_SERVICE] = self._service
            changed = True
        if not changed:
            return body, False

        return json.dumps(payload, ensure_ascii=False).encode("utf-8"), True
