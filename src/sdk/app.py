"""`ObservedApp` · 用一层 ASGI 中间件把业务应用包起来。

★ 为什么是**纯 ASGI**，而不是 `app.add_middleware(...)` 或 `@app.get("/health")`：

  后两种做法都要求"**能拿到那个 app 对象并调它的方法**"，也就是说
  SDK 必须认识 FastAPI/Starlette。换成纯 ASGI 就是
  **"换任意 Python ASGI 框架后 SDK 侧零改动"**。

  `__call__(scope, receive, send)` 是 ASGI 的最小公约数：
  FastAPI / Starlette / Quart / Litestar / 裸 ASGI 全都能被它包住。

★ 探针在**这里**拦截，而不是注册进业务的 router：

  · 业务代码零改动 —— 它不知道有这些路由存在
  · 也避免了"业务自己已经定义了 /health"时的路由冲突，
    以及"业务改了 router 前缀把探针带跑"这类事故

★ 共 4 个：`/health`、`/ready`、`/metrics`，以及**服务清单端点 `/services`**
  （让 Agent 侧不必自己维护服务清单）。

★ 中间件顺序在**这里**显式声明（构造时就装好）：
  指标中间件在外、响应增强在内、业务 app 在最里 —— 见 `__init__` 末尾。
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Mapping, Optional

from .config import ConfigError, SdkConfig
from .contract import (
    PROBE_HEALTH,
    PROBE_METRICS,
    PROBE_PATHS,
    PROBE_READY,
    PROBE_SERVICES,
    RESPONSE_FIELD_ELAPSED_MS,
    RESPONSE_FIELD_SERVICE,
)
from .probes import health_payload, ready_payload, services_payload
from .state import Runtime, set_runtime

__all__ = ["ObservedApp"]

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

# 请求体的丢弃上限：探针不该为了"把 body 读完"而吃内存
_DRAIN_LIMIT_BYTES = 1 << 20


async def _drain(receive: Receive) -> None:
    """把还没被消费的请求体读掉。

    ★ 不是可选项：客户端把 body 发完之前我们就回响应，
      Windows 上会表现为 `WinError 10053`（连接被对端中止），
      而调用方看到的是"端点不可达" —— **一个探针把自己伪装成了故障**。
      （这类问题已实测过。）
    """
    total = 0
    while total < _DRAIN_LIMIT_BYTES:
        try:
            message = await receive()
        except Exception:  # noqa: BLE001 —— 客户端已经走了，不是错误
            return
        total += len(message.get("body", b"") or b"")
        if not message.get("more_body", False):
            return


async def _send_bytes(send: Send, status: int, body: bytes, content_type: str) -> None:
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", content_type.encode("latin-1")),
            (b"content-length", str(len(body)).encode("latin-1")),
        ],
    })
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _send_json(send: Send, status: int, payload: Mapping[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    await _send_bytes(send, status, body, "application/json; charset=utf-8")


class ObservedApp:
    """把任意 ASGI 应用包成"满足契约"的应用。

    构造参数：

    - `app`：业务 ASGI 应用（SDK **不 import 它的模块**，由调用方传进来）；
    - `service`：服务名。★ 显式参数 > 配置文件 > 环境变量 ——
      同一进程里可能构造多个 app（进程内验收就是 4 个），读配置会串味；
    - `config`：`SdkConfig`；与 `service` 至少给一个；
    - `ready_checks`：覆盖配置里的 `[ready]` 清单（测试用）；
    - `runtime`：启动器建好的运行期状态（猴补必须早于业务导入，所以它通常先存在）；
    - `activate`：是否在构造时就完成**全局安装**（见 :meth:`activate`）。
      ★ 默认 False：构造一个 app 不该偷偷改进程全局状态；
      启动器路径自己会安装，`activate=True` 只留给"没有启动器"的嵌入式用法。
    """

    def __init__(
        self,
        app: Any,
        *,
        service: Optional[str] = None,
        config: Optional[SdkConfig] = None,
        ready_checks: Optional[Mapping[str, str]] = None,
        runtime: Optional[Runtime] = None,
        activate: bool = False,
    ) -> None:
        if app is None:
            raise ConfigError("ObservedApp 需要一个 ASGI 应用对象（app=None）")

        name = (service or "").strip()
        if not name and config is not None:
            name = config.service_name
        if not name:
            import os  # 放局部：这是最末位的兜底来源，不是主路径

            name = (os.environ.get("AIOPS_SDK_SERVICE_NAME") or "").strip()
        if not name:
            raise ConfigError(
                "无法确定服务名：请显式传 service=...，或在配置文件里写 [service].name，"
                "或设置环境变量 AIOPS_SDK_SERVICE_NAME —— "
                "服务名是所有指标与日志的关联键，不能留空"
            )

        self._app = app
        self.config = config
        self.service = name
        self._ready_checks: Mapping[str, str] = dict(
            ready_checks if ready_checks is not None
            else (config.ready if config is not None else {})
        )
        # ★ 同伴清单（`GET /services` 探测谁）：来自配置里**全量** `aiops.services`
        #   段（已排除自己）。没给 config 就是空 —— 那时清单里只有自己，
        #   **不是"猜"出来的**：宁可少报，不可编造。
        self._peer_services: Mapping[str, tuple[str, int]] = dict(
            config.peer_services if config is not None else {}
        )

        # ★ Runtime 可以由启动器先建好再传进来 —— 因为猴补必须**先于**
        #   `import_app()` 完成，那时候还没有 ObservedApp 对象。
        self.runtime = runtime if runtime is not None else Runtime(service_name=name, config=config)
        if self.runtime.service_name != name:
            raise ConfigError(
                f"Runtime 的服务名（{self.runtime.service_name}）与 ObservedApp 的（{name}）不一致 —— "
                "这会让指标与日志互相串味"
            )
        # 接管结果：由 `activate()` 填；构造阶段**不碰全局状态**（见该方法）
        self.activation: dict[str, Any] = {}
        from .endpoints import discover
        self.runtime.endpoint_inventory,self.runtime.endpoint_status=discover(app,config or self.runtime.config)
        self.runtime.diagnostics.capability('business_endpoints',
            'available' if self.runtime.endpoint_status in ('complete','configured') else 'unsupported',
            self.runtime.endpoint_status,adapter='route-introspection')
        if activate:
            self.activate()

        # ★ 中间件顺序：
        #     MetricsMiddleware（外） → ResponseEnhancer（内） → 业务 app
        #   响应增强必须在**更靠近应用**的内层：越靠内，越接近"应用产出响应那一刻"，
        #   计时越接近"服务端内部处理耗时"，
        #   而且会更早看到未被打包/改写的响应体。
        from .middleware_http import HttpMetricsMiddleware  # 局部导入：避免循环依赖
        from .middleware_response import ResponseEnhancer  # 局部导入：避免循环依赖

        enhanced = ResponseEnhancer(app, self.runtime)
        from .diagnostic_http import DiagnosticHttpMiddleware
        self._inner = DiagnosticHttpMiddleware(HttpMetricsMiddleware(enhanced, self.runtime),self.runtime)

    # ------------------------------------------------------------------
    #  显式安装（全局副作用只发生在这一步）
    # ------------------------------------------------------------------
    def activate(self) -> dict[str, Any]:
        """把本 app 的 Runtime 设为**进程当前**，并接管驱动工厂 / 日志 / 链路。

        ★ 为什么不放在 `__init__` 里：构造一个对象就改进程全局状态
          （当前 Runtime + 对 `httpx` / `asyncpg` 的猴补）太隐蔽 ——
          `activate=False` 的调用方（自检、离线测试）也会被改到。
          所以安装是显式一步，且幂等（重复调用不会重复猴补）。

        ★ 更常见的路径是**启动器**：它在 `import_app()` **之前**就建好 Runtime 并
          `activate()`，那时还没有 ObservedApp 对象（猴补必须早于业务导入，
          否则拿不到业务创建的池对象）。那条路径不需要再调本方法。
        """
        set_runtime(self.runtime)
        from .instrument import activate as _activate  # 局部导入：避免循环依赖

        self.activation = _activate(self.runtime, self.config or self.runtime.config)
        return self.activation

    # ------------------------------------------------------------------
    #  ASGI 入口
    # ------------------------------------------------------------------
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get('type')=='http':
            from .diagnostic_api import handle
            if await handle(self.runtime,scope,send):return
        if scope.get('type')=='lifespan':
            from .lifecycle import observe_lifespan
            return await observe_lifespan(self._app,self.runtime,scope,receive,send)
        if scope.get("type") != "http":
            # lifespan / websocket 一律原样交给业务应用 ——
            # SDK 不参与生命周期管理，避免"包装器改变了启动语义"
            await self._app(scope, receive, send)
            return

        path = scope.get("path") or ""
        if path == PROBE_HEALTH:
            await _drain(receive)
            # ★ 身份与依赖一起自报：只有本服务知道自己是谁、依赖谁。
            #   配置缺失时对应字段为 null/[]，**不自造默认值**。
            await _send_json(send,200,self._health_payload())
            return
        if path == PROBE_READY:
            await _drain(receive)
            ready, payload = await ready_payload(self.runtime, self._ready_checks)
            await _send_json(send, 200 if ready else 503, payload)
            return
        if path == PROBE_METRICS:
            await _drain(receive)
            from .system_scope import export_ready
            if not export_ready(self.runtime):
                await _send_json(send,503,{'error':'system_registration_pending'});return
            from .metrics import render_metrics  # 局部导入：避开 prometheus_client 的导入开销

            body, content_type = render_metrics(self.runtime)
            await _send_bytes(send, 200, body, content_type)
            return
        if path == PROBE_SERVICES:
            # ★ 服务清单端点：只敲这一扇门就能拿到"全系统谁活着"。
            #   ★ 即使有同伴不在也回 **200**：它不是就绪探针，
            #     拿它做就绪判定会把"同伴掉线"升级成"自己也重启"。
            await _drain(receive)
            if getattr(self.runtime.config,'observed_name',None):
                await _send_json(send,200,{'identity':dict(self.runtime.diagnostics.identity),
                    'services':[self.service],'scope':'local_registration','current_reachability':'unverified'})
                return
            await _send_json(send, 200, await services_payload(self.runtime, self._peer_services))
            return

        await self._inner(scope, receive, send)

    def _health_payload(self):
        payload=health_payload(self.service,identity=self.config.identity() if self.config else None,
            depends_on=self.config.depends_on if self.config else ())
        if getattr(self.runtime.config,'observed_name',None):payload['identity']=dict(self.runtime.diagnostics.identity)
        return payload

    # ------------------------------------------------------------------
    #  自省（自检的 verify() 读它）
    # ------------------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "probe_paths": list(PROBE_PATHS),
            "peer_services": sorted(self._peer_services),
            "ready_checks": dict(self._ready_checks),
            "patches": dict(self.runtime.patches),
            "pools": len(self.runtime.pools),
            "activation": dict(self.activation),
            "diagnostics": self.runtime.diagnostics.snapshot(),
            "counters": dict(self.runtime.counters),
            "response_fields_injected": [
                RESPONSE_FIELD_ELAPSED_MS, RESPONSE_FIELD_SERVICE,
            ],
        }
