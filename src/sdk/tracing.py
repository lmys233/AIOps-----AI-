"""链路采集：在当前进程里装 OTel 自动埋点，业务侧零代码改动。

★ 目标：业务侧**零代码改造**出链路，且跨进程串成**一条** trace。

★ 两条路一起上，互为保险：

  - **入口点自动埋点**（`initialize()`）：启动器在**当前进程**里显式调用 ——
    读 `OTEL_*`、装 OTLP exporter、按 entry point 装全部 instrumentor；
  - **SDK 自带 `sitecustomize`**：解释器启动时若 `PYTHONPATH` 里有 `sdk/auto/`，
    给"不经启动器、直接跑业务"的场景兜底。

  ★ 为什么**不用** `opentelemetry-instrument` 命令：它内部是 `os.execl(...)`，
    **Windows 上 exec 会换 PID**（本项目已实测）。多一层进程跳转没有任何收益，
    只多一个可能失败的环节。

  ★ 为什么还要"显式调用 initialize()"而不是只靠 sitecustomize：

    `sitecustomize` **只在解释器启动时**执行一次。而启动器是在**当前进程**里
    `import_app()` 的 —— 那时解释器早就起来了，sitecustomize 不会再跑。
    只设 `OTEL_*` 环境变量**不等于链路生效**（这一点曾被检查器抓成假 PASS）。

  ★ 与"官方 sitecustomize 共存"：

    解释器**只会导入一个** `sitecustomize`。所以：

    - 若 SDK 的 `sdk/auto/` 在 `PYTHONPATH` 更前面 → 跑我们的，
      我们在里面自己调 `initialize()`（等价于官方那份做的事）；
    - 若官方的在前面 → 跑官方的，而启动器已经把 `OTEL_*` 设好了，效果相同。

    两种顺序都能工作 —— 这是刻意的，不允许出现"谁在前面谁说了算"的隐性依赖。

★ `service.name` 必须与 Prometheus 的 `service` 标签**一致**，
  否则 Trace ↔ Metrics 关联不上。两者都取自 `[service].name`。
"""

from __future__ import annotations

from typing import Any

import logging
import os
import socket
from urllib.parse import urlparse

from .state import Runtime

__all__ = ["setup_tracing", "initialize_in_process", "endpoint_reachable"]

logger = logging.getLogger("sdk.tracing")

# 幂等标记：`initialize()` 重复调用会把 instrumentor 装两层
_initialized = False

# ★ 我们自己从 OTel context 取 trace_id/span_id（用契约里的字段名），
#   所以关掉 OTel 的 logging instrumentor —— 否则它还会往每条日志里塞
#   `otelTraceID`/`otelSpanID`/`otelServiceName`/`otelTraceSampled` 四个字段，
#   与契约字段重复（同一个语义两套名字，采集端还得挑）。
_DISABLED_INSTRUMENTORS = "logging"

# 端点可达性检查的 TCP 超时：只判"能不能连上"，不该让启动等太久
_TCP_PROBE_TIMEOUT_SEC = 0.6


def endpoint_reachable(endpoint: str, timeout: float = _TCP_PROBE_TIMEOUT_SEC) -> bool:
    """OTLP 端点是否可达（只做 TCP 连接判断，不握手）。

    ★ 为什么要判：Collector 没起来时，OTLP exporter 会**每个批次都报一次错**，
      把日志刷满"Failed to export spans"。提前告警一次，
      比让人自己在噪声里找原因强。
    """
    if not endpoint:
        return False
    parsed = urlparse(endpoint if "://" in endpoint else f"//{endpoint}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 4317
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, port)) == 0
    except OSError:
        return False


def initialize_in_process() -> bool:
    """在当前进程里触发 OTel 自动埋点。返回是否**这次**真的做了初始化。

    ★ 两个必须知道的实现细节（都实测过）：

    1. `initialize()` **不在** `auto_instrumentation/__init__.py` 里，
       而在 `auto_instrumentation/sitecustomize.py`；
       并且那个模块**在导入时就会调用自己**（文件最后一行就是 `initialize()`）。
       所以这里只导入、不显式调用 —— 模块缓存保证只发生一次，
       显式再调一次会把 instrumentor 装两层。

    2. `initialize()` **内部把异常全吞了**（`except Exception: logger.exception(...)`），
       所以"调用没报错"**不等于**"链路装上了"。
       判据只能是**回头检查 instrumentor 到底装上没有**
       （`_instrumented_names()`）—— 这与本项目"不看口号看实证"的口径一致。
    """
    global _initialized
    if _initialized:
        return False
    os.environ.setdefault("OTEL_PYTHON_DISABLED_INSTRUMENTATIONS", _DISABLED_INSTRUMENTORS)
    import importlib  # noqa: PLC0415

    importlib.import_module(
        "opentelemetry.instrumentation.auto_instrumentation.sitecustomize"
    )
    _initialized = True
    return True


def _instrumented_names() -> list[str]:
    """列出**已经装上的** instrumentor（给自检用）。

    ★ 为什么要能列出来："装了"与"生效"是两件事。
      `verify()` 要能回答"哪些真的挂上了"，而不是靠人读启动日志猜。
    """
    names: list[str] = []
    try:
        from importlib.metadata import entry_points  # noqa: PLC0415

        eps = entry_points()
        selected = eps.select(group="opentelemetry_instrumentor")
        for entry in selected:
            try:
                instrumentor_cls = entry.load()
                instance = instrumentor_cls()
                if getattr(instance, "_is_instrumented_by_opentelemetry", False):
                    names.append(entry.name)
            except Exception:  # noqa: BLE001 —— 某个 instrumentor 查不了不影响其它
                continue
    except Exception:  # noqa: BLE001
        return names
    return sorted(names)


def resource_attributes(service_name: str, cfg: Any) -> str:
    """OTel 资源属性（`k=v,k=v`）：**服务名 + 配置里真有的 cluster/env**。

    ★★ 绝不写死 `cluster=local-1` / `deployment.environment=prod`。
      取值链与 `/health` **完全一致**（`cfg.cluster` / `cfg.env`），
      取不到就**省略这个键** —— 省略是"未知"，写死是"错的"。
      `deployment.environment` 是 OTel 的语义键，`env` 是采集器 `resource` processor
      用的那个：两个都带上，且都来自同一处配置（不会被 upsert 盖住的那一个也不会说谎）。
    """
    pairs = [f"service.name={service_name}"]
    cluster = str(getattr(cfg, "cluster", "") or "").strip()
    if cluster:
        pairs.append(f"cluster={cluster}")
    env = str(getattr(cfg, "env", "") or "").strip()
    if env:
        pairs.extend([f"env={env}", f"deployment.environment={env}"])
    return ",".join(pairs)


def setup_tracing(cfg: Any, runtime: Runtime) -> dict[str, Any]:
    """按配置打开链路。返回"做了什么"（供自检与启动横幅）。"""
    endpoint = getattr(cfg, "otlp_endpoint", "") or ""

    # ★ 环境变量在这里**再设一遍**，而不是只依赖启动器的 prepare_environment：
    #   `initialize()` 完全是按 `OTEL_*` 环境变量配置的，谁忘了设谁就静默没有链路。
    #   放在这里 = 只要调了 setup_tracing，配置就是自洽的。
    os.environ["OTEL_SERVICE_NAME"] = runtime.service_name
    # ★★ 这里以前硬编码 `deployment.environment=prod,cluster=local-1` ——
    #   而 `/health` 自报的 cluster/env 来自 `deploy/.env`（当前 `compose-cn-1`），
    #   变更事件写的是 `svc:order@compose-cn-1`：**同一个服务在 span 与 /health 上
    #   是两个集群**。Agent 做实体对齐用的正是 `svc:<name>[@cluster]` ——
    #   这正是"集群名不一致 → 误判成没有该服务的数据"那个最难发现的失败形态。
    #   → 一律从配置取；**取不到就省略该键**（沿用项目"不自造默认值"的口径）。
    os.environ["OTEL_RESOURCE_ATTRIBUTES"] = resource_attributes(
        runtime.service_name, cfg)
    identity=runtime.diagnostics.identity
    os.environ['OTEL_RESOURCE_ATTRIBUTES'] += (
        ',service.instance.id='+identity['instance']+',aiops.boot_id='+identity['boot_id'])
    # ★ 指标走 /metrics 被抓取，**不经 OTLP 再导出** ——
    #   两条路都开会让同一份指标出现两套 series，`sum()` 类查询直接翻倍。
    os.environ["OTEL_METRICS_EXPORTER"] = "none"
    os.environ["OTEL_LOGS_EXPORTER"] = "none"

    report: dict[str, Any] = {
        "endpoint": endpoint or None,
        "service_name": runtime.service_name,
    }

    if not endpoint:
        os.environ["OTEL_TRACES_EXPORTER"] = "none"
        if getattr(cfg,'observed_name',''):
            try:
                initialized=initialize_in_process()
                from opentelemetry import trace
                from .scoped_traces import install
                install(trace.get_tracer_provider(),runtime)
                return dict(report,traces_exporter='sdk_archive',initialized=initialized,instrumented=_instrumented_names())
            except Exception as exc:
                runtime.diagnostics.capability('scoped_tracing','unsupported',type(exc).__name__)
        report.update({
            "traces_exporter": "none",
            "initialized": False,
            "note": "未配置 [tracing].otlp_endpoint —— 不出链路",
        })
        return report

    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
    os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "grpc"

    if endpoint_reachable(endpoint):
        os.environ["OTEL_TRACES_EXPORTER"] = "otlp"
        report["traces_exporter"] = "otlp"
        report["endpoint_reachable"] = True
    else:
        # ★ 关键取舍（实测依据）：Collector 不可达时若照常启用 OTLP exporter，
        #   它会**每批重试并刷日志**（实测 30 秒刷了 6 条 "Transient error … retrying in Ns"），
        #   把 4 个服务的 JSONL 全灌满 —— 真正的业务日志被淹掉，
        #   正是"信号被噪声盖住"这一类失败。
        #
        # ★ 所以**不启用导出**，但**仍然初始化 instrumentor**。
        #   这一点很关键：TracerProvider 依然是真实的（只是没有 span processor），
        #   于是 ——
        #     · span 照常创建，日志的 trace_id / span_id **照常有值**
        #     · `traceparent` 照常跨进程透传（跨进程串一条 trace 依然成立）
        #     · 只是**没有落地到 Tempo**
        #   也就是说：本地不装 Docker 也能验证"链路是通的"，只是"没存下来"。
        os.environ["OTEL_TRACES_EXPORTER"] = "none"
        report["traces_exporter"] = "none"
        report["endpoint_reachable"] = False
        runtime.warn_once(
            "otlp_endpoint_unreachable",
            f"OTLP 端点 {endpoint} 不可达 —— **本次不导出 span**（业务不受影响，"
            f"也不会重试刷日志）。trace 上下文仍然生成：日志的 trace_id 与跨进程 "
            f"traceparent 照常可用，只是链路不会落到 Tempo。"
            f"要采链路请先起 Collector（`.\\run.ps1 infra-up`）再重启服务",
        )

    try:
        report["initialized"] = initialize_in_process()
        if getattr(cfg,'observed_name',''):
            from opentelemetry import trace
            from .scoped_traces import install
            report['system_scope']=install(trace.get_tracer_provider(),runtime)
    except Exception as exc:  # noqa: BLE001 —— 链路初始化失败不得影响业务
        report["initialized"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
        runtime.warn_once(
            "tracing_init_failed",
            f"OTel 自动埋点初始化失败（{type(exc).__name__}: {exc}）—— "
            f"链路不可用；业务与指标/日志不受影响",
        )
        return report

    report["instrumented"] = _instrumented_names()
    report["suppressed_trace_id_fields"] = True
    if not report["instrumented"]:
        runtime.warn_once(
            "no_instrumentor",
            "没有任何 instrumentor 被装上 —— 链路会是空的。"
            "检查 opentelemetry-instrumentation-* 是否装齐",
        )
    return report
