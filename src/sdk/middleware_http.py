"""HTTP 指标中间件：记录 `http_request_duration_seconds` 与业务操作计数。

★ 纯 ASGI 实现，和 `ObservedApp` 同一个理由：
  不依赖 FastAPI/Starlette 的具体类型，换框架也不用改。

★ 三个必须讲清的口径：

  1. **`path` 取路由模板**（`scope["route"].path`）。
     ★ 它**只有在路由匹配之后**才写进 scope —— 所以必须在
       `http.response.start` 那一刻读，不能在中间件入口读（那里还是 `None`）。
     实测（`starlette 0.41.3` + FastAPI）：
     `scope["route"]` = `APIRoute(path='/orders/{order_id}', ...)`。

  2. **匹配不到的路径用 `<unmatched>` 兜底，而不是用实际路径**。
     直觉上"没匹配到就用真实路径"很方便，但那正是要防的
     标签基数爆炸 —— 404 扫描器打一万个不存在的路径，就会产生一万条 series。
     宁可把它们归成一桶，并**告警一次**（告警是因为"看不到是哪个路径 404"
     会削弱排查能力，这个取舍必须让人知道）。

  3. **探针流量不计入业务指标**。
     `/health`、`/ready`、`/metrics` 是采集/编排用的，不是业务请求。
     把它们混进 `http_request_duration_seconds` 会让 P99 被"很快的探针"
     拉低 —— 真实故障时的延迟被低估，**这比没有数据更危险**。
"""

from __future__ import annotations

from time import perf_counter
from typing import Any, Awaitable, Callable, Final

from .contract import PROBE_PATHS
from .definitions import BUSINESS_OPERATIONS, HTTP_DURATION
from .state import Runtime

__all__ = ["HttpMetricsMiddleware", "UNMATCHED_PATH", "KNOWN_METHODS",
           "OTHER_METHOD", "bounded_method", "route_template"]

# 匹配不到路由时使用的 path 标签值（有界，不会随 URL 增长）
UNMATCHED_PATH: Final[str] = "<unmatched>"

Message = dict[str, Any]
Scope = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


def route_template(scope: Scope, runtime: Runtime) -> str:
    """取路由模板；取不到就兜底成 `<unmatched>` 并告警一次。"""
    route = scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template:
        return template

    runtime.warn_once(
        "route_template_missing",
        f"有请求未匹配到路由（scope['route'] 缺失），其 path 标签统一记为 "
        f"{UNMATCHED_PATH!r} —— 这样不会造成标签基数爆炸，"
        f"但也就看不出具体是哪个路径。若这类流量很大，说明有人在扫接口",
    )
    return UNMATCHED_PATH


#: 允许出现在指标标签里的 HTTP 方法（标签必须**有界**）
KNOWN_METHODS: Final[frozenset[str]] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"})
#: 未识别方法统一归到它（与 `UNMATCHED_PATH` 同一思路：**有界**优先于**精确**）
OTHER_METHOD: Final[str] = "<other>"


def bounded_method(raw: Any) -> str:
    """方法名 → **有界**标签值（未识别归 `<other>`）。

    ★★ h11 允许**任意 token** 作方法名，而 `prometheus_client` 从不淘汰
      标签组合 —— 一个只会发 `FOO /x` 的客户端就能让 series 无限增长
      （与 `path` 那一侧是同一个失败形态，只是换了一个标签）。
    """
    text = str(raw or "GET").strip().upper()
    return text if text in KNOWN_METHODS else OTHER_METHOD


class HttpMetricsMiddleware:
    """产出 `http_request_duration_seconds` 与 `business_operations_total`。"""

    def __init__(self, app: Any, runtime: Runtime) -> None:
        self._app = app
        self._runtime = runtime
        self._service = runtime.service_name
        self._operations = dict(runtime.config.operations) if runtime.config else {}

    # ------------------------------------------------------------------
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        path = scope.get("path") or ""
        if path in PROBE_PATHS:
            # 探针流量不是业务流量（见模块文档第 3 条）
            await self._app(scope, receive, send)
            return

        # ★★ `method` 也要**白名单化**。h11 允许任意 token 作方法名
        #   （`FOO`、扫描器随便编一个就是一个新 series），而以前只修了 `path` 这一侧。
        #   `prometheus_client` 从不淘汰标签组合 → 同族的逐 series 内存增长。
        method = bounded_method(scope.get("method"))
        started = perf_counter()
        recorded = False

        async def send_wrapper(message: Message) -> None:
            nonlocal recorded
            if message.get("type") == "http.response.start" and not recorded:
                recorded = True
                self._record(method, scope, int(message.get("status", 0)), started)
            await send(message)

        try:
            await self._app(scope, receive, send_wrapper)
        except Exception:
            # ★ 业务抛异常时**没有** response.start，但这次调用确实发生了、
            #   而且失败了 —— 不记就会让错误率看起来比真实值低。
            #   记完照样往外抛，绝不吞。
            if not recorded:
                recorded = True
                self._record(method, scope, 500, started)
            raise

    # ------------------------------------------------------------------
    def _record(self, method: str, scope: Scope, status: int,
                started: float) -> None:
        """记录一次请求。★ 套 `safe_record` 护栏：观测侧异常不得影响业务响应。"""
        from .state import safe_record

        safe_record(
            self._runtime, "http_metrics", "HTTP 指标记录",
            lambda: self._record_inner(method, scope, status, started),
        )

    def _record_inner(self, method: str, scope: Scope, status: int,
                      started: float) -> None:
        template = route_template(scope, self._runtime)
        elapsed = perf_counter() - started

        HTTP_DURATION.labels(
            service=self._service, method=method, path=template, status=str(status),
        ).observe(elapsed)

        # 操作名：配置里映射优先，否则退化为「方法 + 路由模板」——
        # ★ SDK 猜不出业务操作叫什么（"下单"是 create_order 还是 place_order？），
        #   所以默认值只保证"有界且可读"，语义化留给配置。
        operation_key = f"{method} {template}"
        operation = self._operations.get(operation_key, operation_key)
        BUSINESS_OPERATIONS.labels(
            service=self._service,
            operation=operation,
            result="success" if status < 400 else "error",
        ).inc()
