"""日志采集：接管 root logger，把业务日志变成采集端要的单行 JSON。

★ 机制：**接管 `logging` 根 logger** —— 这就是"零代码改动"在日志这一路的全部含义。

  业务原来怎么写 `logger.info(...)` 就还怎么写，**一行不改**；
  SDK 在启动时给 root 挂上自己的 handler 与 formatter，输出自动变成
  采集端要的单行 JSON。业务甚至不需要知道 SDK 在改它的日志格式。

★ 为什么走 `structlog.stdlib.ProcessorFormatter` 而不是自己写 formatter：

  日志有两个入口 —— ① 业务的 `logging`（第三方库也用这个）、② SDK 自己的
  structlog。如果各写一套 formatter，两边的字段**迟早会漂移**
  （promtail 上已经踩过一次同类问题）。

  `ProcessorFormatter` 把"stdlib 的 LogRecord"和"structlog 的 event_dict"
  汇进**同一条 processor 链**，于是：

      同一个 formatter → 同一份字段 → 双写（stdout 与文件）也不可能不一致

★ 字段契约（采集端按此提取，缺一不可）：

  - `msg`：正文。★ structlog 默认叫 `event`，**必须重命名**（真实踩过的坑）；
  - `ts`：UTC 时间，RFC3339；Promtail 的 `timestamp` 阶段按此解析；
  - `level`：日志级别，**必须大写** —— LogQL 写的是 `level="ERROR"`，小写一条都匹配不到；
  - `service`：配置/启动参数注入。多服务共用一份代码，必须外部注入；
  - `trace_id` / `span_id`：★ 从 **OTel context** 取，**不许自己生成** ——
    自己生成就和 Tempo 对不上；
  - `error_type`：业务的 `extra={...}`，仅错误日志；Log Agent 聚类的依据。

★ 单行是**硬要求**：多行 traceback 会让采集端的 `json` 阶段解析失败、
  **整条日志被丢弃**。所以异常也走 processor：转成字符串放进 `detail`，
  由 `json.dumps` 把换行转义成 `\\n`（这样"有堆栈"与"单行"可以兼得）。
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import structlog

from .redact import redact_event_dict
from .contract import (
    LOG_FIELDS,
    LOG_LEVEL,
    LOG_MSG,
    LOG_SERVICE,
    LOG_SPAN_ID,
    LOG_TS,
    LOG_TRACE_ID,
    LOG_SEVERITY,
)
from .state import warn_once

__all__ = ["setup_logging", "build_stdlib_formatter"]

# 逐请求打 INFO 的库：不压噪会把真正的信号淹掉
_NOISY_LOGGERS = ("httpx", "httpcore", "hpack", "urllib3", "asyncio", "uvicorn.access")


# ----------------------------------------------------------------------
#  processor 链
# ----------------------------------------------------------------------
# stdlib LogRecord 自带的属性 —— 合并 extra 时要跳过这些，
# 否则日志行里会混进 pathname/lineno/thread 等一堆采集端不需要的噪声
_STDLIB_RESERVED = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "asctime", "message",
    # ★ 第三方库塞进来的"给人看"的字段，不该进结构化日志：
    #   uvicorn 的 `color_message` 带 ANSI 转义码（`\u001b[1m`），
    #   落进 JSONL 后既污染日志正文，也会让 Loki 里的关键词匹配变得莫名其妙。
    "color_message",
    # ★ OTel 的 logging instrumentor 会塞这四个字段。我们**刻意关掉了**它
    #   （见 sdk/tracing.py 的 `_DISABLED_INSTRUMENTORS`），自己用契约字段名
    #   `trace_id`/`span_id` 从 context 取。这里再挡一道，防止将来有人把
    #   instrumentor 打开后，同一语义在日志里出现两套字段名。
    "otelTraceID", "otelSpanID", "otelServiceName", "otelTraceSampled",
})


def _merge_record_extras(logger: Any, method_name: str, event_dict: dict) -> dict:
    """把业务用 `extra={...}` 传的字段并进 event_dict。

    ★ 为什么必须自己做（实测 structlog 24.4.0）：

      `ProcessorFormatter.format()` 对 stdlib 记录只构造
      `{"event": ..., "_record": ..., "_from_structlog": False}` ——
      **不合并 `record.__dict__` 里的 extra**。

      于是业务的 `logger.error("...", extra={"error_type": "STOCK_INSUFFICIENT"})`
      里的 `error_type` 会**整个丢掉**，而它正是 Log Agent 聚类的依据。
      丢掉它不会报错，只会让聚类永远聚不起来 ——
      属于"安静地缺数据"这一类，最难发现。

    ★ 放在 `foreign_pre_chain` 的**第一位**：那时 `_record` 还在。
    """
    record = event_dict.get("_record")
    if record is None:
        return event_dict
    for key, value in vars(record).items():
        if key in _STDLIB_RESERVED or key.startswith("_"):
            continue
        event_dict.setdefault(key, value)
    return event_dict


def _rename_event_to_msg(logger: Any, method_name: str, event_dict: dict) -> dict:
    """把 structlog 的 `event` 重命名成采集端要的 `msg`。

    这个坑是真实的：structlog 默认字段叫 `event`，而 Promtail/Loki 的
    提取规则按 `msg` 写。不做这一层，Loki 里永远查不到正文。
    """
    if "event" in event_dict:
        event_dict[LOG_MSG] = event_dict.pop("event")
    return event_dict


def _add_service(service: str):
    """`service` 是 **SDK 自有的事实** —— ★ 必须**直接赋值**。

    以前这里是 `setdefault`，而业务 `extra={...}` 由 `_merge_record_extras`
    （链首）先写入 → **业务值胜出**：`logger.info("x", extra={"service": "payment"})`
    会让这条日志自称属于另一个服务（Log Agent 按 `service` 聚类 → 归因到错的服务）。
    业务若把请求体直接当 extra 传（`extra=request.json()`），**外部调用方**就能写它。
    """

    def processor(logger: Any, method_name: str, event_dict: dict) -> dict:
        if LOG_SERVICE in event_dict and event_dict[LOG_SERVICE] != service:
            # ★ 留痕但**不采信**：有人试图伪造服务归属，这件事本身要能看见
            event_dict["forged_service_ignored"] = str(event_dict[LOG_SERVICE])[:64]
        event_dict[LOG_SERVICE] = service
        from .diagnostic_state import active_runtime,current_correlation
        runtime=active_runtime()
        if runtime is not None and runtime.service_name==service:
            identity=runtime.diagnostics.identity
            event_dict.update(service_instance_id=identity['instance'],boot_id=identity['boot_id'])
            if identity.get('observed_id'):
                event_dict.update(observed_id=identity['observed_id'],tenant=identity['tenant'],env=identity['env'])
            else:
                event_dict.pop('observed_id',None)
                event_dict.pop('tenant',None)
                event_dict['aiops_scope']='registration_pending' if getattr(runtime.config,'observed_name','') else 'legacy_unattributed'
            event_dict.update(current_correlation())
            from .log_capture import capture
            capture(runtime,event_dict,method_name)
        return event_dict

    return processor


def _add_timestamp(logger: Any, method_name: str, event_dict: dict) -> dict:
    """RFC3339 时间戳，键名 `ts`（Promtail 的 timestamp 阶段按此解析）。

    ★ 用 `isoformat(timespec="milliseconds")` 而不是 structlog 的 TimeStamper：
      前者输出带毫秒的 UTC ISO8601，且显式带时区；后者默认格式
      与 Promtail 的解析约定不完全对齐（少毫秒、时区写法不同）。
    """
    now = datetime.now(timezone.utc)
    # ★★ `ts` **直接赋值**。自己编的 `ts` 会把 Promtail 的时间戳阶段带偏 ——
    #   Loki 里这条日志的时间因此是错的，而"时间错了"在排障时最难发现。
    event_dict[LOG_TS] = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return event_dict


def _add_trace_context(logger: Any, method_name: str, event_dict: dict) -> dict:
    """trace_id / span_id **从 OTel context 取**。

    ★ **绝不自己生成**：自己生成的是"另一个 id"，
      和 Tempo 里的 trace 对不上 —— 日志与链路就永远关联不起来。
      没有活跃 span 时留空字符串（而不是编一个），
      "空"与"编造的 id"是完全不同的两种信息。
    """
    trace_id = ""
    span_id = ""
    try:
        from opentelemetry import trace  # noqa: PLC0415

        span = trace.get_current_span()
        ctx = span.get_span_context() if span is not None else None
        if ctx is not None and ctx.is_valid:
            trace_id = format(ctx.trace_id, "032x")
            span_id = format(ctx.span_id, "016x")
    except Exception as exc:  # noqa: BLE001 —— 取不到 trace 不能让日志写不出去
        # ★ 但**不许沉默**：缺了 trace_id，
        #   日志就永远和 Tempo 关联不上 —— 那是"安静地缺一路能力"，
        #   比报错难查得多。吵一次，然后继续（字段留空）。
        warn_once(
            "trace_context_unavailable",
            f"取不到 OTel trace context（{type(exc).__name__}: {exc}）—— "
            f"日志的 trace_id/span_id 将为空，无法与 Tempo 链路关联",
        )
    # ★★ `trace_id` / `span_id` **直接赋值** —— 伪造的 trace_id 会指向
    #   **不存在的链路**（与 Tempo 永远对不上），而"关联断了"是最难排查的一类问题。
    event_dict[LOG_TRACE_ID] = trace_id
    event_dict[LOG_SPAN_ID] = span_id
    return event_dict


def _normalize_level(logger: Any, method_name: str, event_dict: dict) -> dict:
    """`level` 一律**大写**（LogQL 写的是 `level="ERROR"`，小写一条都匹配不到）。"""
    level = event_dict.get(LOG_LEVEL)
    if level is None:
        level = method_name
    event_dict[LOG_LEVEL] = str(level).upper()
    return event_dict


def _render_exception_single_line(logger: Any, method_name: str, event_dict: dict) -> dict:
    """把异常压成**单行**字符串放进 `detail`。

    ★ 为什么不用 `structlog.processors.format_exc_info`：
      它把堆栈放进 `exception` 字段，而本项目约定用 `detail`
      （与业务错误的 `detail` 同一个键，Log Agent 不必分辨两种形状）。
      两者都能靠 `json.dumps` 转义成单行，但键名统一更省事。

    ★ 为什么必须由 SDK 兜住：业务可能直接 `logger.exception(...)` ——
      **不能指望业务不用**。压单行是 SDK 的责任，不是业务的自律。
    """
    exc_info = event_dict.pop("exc_info", None)
    if not exc_info:
        return event_dict
    import traceback  # noqa: PLC0415

    if exc_info is True:
        exc = sys.exc_info()
    elif isinstance(exc_info, BaseException):
        exc = (type(exc_info), exc_info, exc_info.__traceback__)
    else:
        exc = exc_info  # 已经是 (type, value, tb)
    text = "".join(traceback.format_exception(*exc)) if exc and exc[0] else "（无异常信息）"
    # 末尾换行去掉；内部换行**保留**，由 JSONRenderer 转义成 \n
    event_dict.setdefault(LOG_SEVERITY, text.rstrip())
    return event_dict



def _redact_telemetry(logger: Any, method_name: str, event_dict: dict) -> dict:
    """遥测脱敏工序。

    ★ 为什么必须自己做：项目为"变更 diff"建了脱敏（`changes._redact`），但**遥测三条
      出口一条都没接** —— 业务 `logger.info("dsn", extra={"dsn": "postgres://u:p@h"})`
      会把凭据原样写进 Loki；`?api_key=…` 也会跟着 span 进 Tempo。
      这类泄漏**不报错**，只会安静地把密钥写进可长期保存的存储。
    ★ 口径与 Agent 侧 `memory/gates.py` **同一套正则**（两个发布物各一份表，
      由测试强制逐字相等 —— 见 `sdk/redact.py` 的模块头）。
    """
    return redact_event_dict(event_dict)

# ----------------------------------------------------------------------
#  装配
# ----------------------------------------------------------------------
def build_stdlib_formatter(service: str) -> logging.Formatter:
    """业务（stdlib logging）与 SDK（structlog）共用的那**一个** formatter。"""
    shared_processors: list[Any] = [
        # ★ 第一个：业务用 extra 传的字段（error_type 等）靠它才能进来
        _merge_record_extras,
        structlog.contextvars.merge_contextvars,
        _rename_event_to_msg,
        _add_service(service),
        _add_timestamp,
        _add_trace_context,
        _normalize_level,
        _render_exception_single_line,
        # ★★ **脱敏放在最后**——它必须看到全部字段
        #   （业务 extra 已并进来、service/trace_id 也已注入）。
        #   位置早于 renderer：抹掉的是**值**，不是已经序列化好的 JSON 文本。
        _redact_telemetry,
    ]
    return structlog.stdlib.ProcessorFormatter(
        # structlog 直接调用时走的链
        processors=[
            structlog.stdlib.add_log_level,
            _rename_event_to_msg,
            _add_service(service),
            _add_timestamp,
            _add_trace_context,
            _normalize_level,
            _render_exception_single_line,
            # ★★ 同理，脱敏在 renderer 之前（见 `_redact_telemetry`）
            _redact_telemetry,
            # ★ 必须放在 renderer **之前**：它摘掉 ProcessorFormatter 自己的
            #   内部键（`_record` / `_from_structlog`）——
            #   不摘的话每一行日志里都会多出 `"<LogRecord: ...>"` 这种噪声。
            #   又要放在 `add_log_level` **之后**：后者需要 `_record.levelname`。
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        # stdlib 的 LogRecord 进来时先过这一段（之后并入上面的 processors）
        foreign_pre_chain=shared_processors,
    )


# 旧名保留为同一函数对象的别名：既有调用点仍按这个名字取用（行为完全一致）
_build_stdlib_formatter = build_stdlib_formatter


def _make_file_handler(path: Path, formatter: logging.Formatter) -> Optional[logging.Handler]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8", delay=True)
        handler.setFormatter(formatter)
        return handler
    except OSError as exc:
        # ★ 不阻断业务启动：日志写不了是"数据少了"，
        #   不是"业务该挂"。但必须吵。
        warn_once(
            "log_file_unavailable",
            f"日志文件 {path} 打不开（{type(exc).__name__}: {exc}）—— "
            f"仅输出到 stdout；promtail 将采不到 {path.parent} 下的日志",
        )
        return None


def setup_logging(
    service: str,
    *,
    log_dir: Optional[Path] = None,
    level: str = "INFO",
) -> dict[str, Any]:
    """接管 root logger。返回"做了什么"（供启动横幅与自检用）。

    ★ 幂等：重复调用会先摘掉自己上次挂的 handler，不会重复输出。
    ★ 必须在 `import_app()` **之前**调用：业务模块导入期就可能打日志
      （`observed.biz.app` 就会在启动时打一条）。
    """
    formatter = build_stdlib_formatter(service)
    root = logging.getLogger()

    # 摘掉 root 上**所有**现有 handler（上次 SDK 挂的 + 业务自己 basicConfig 加的）。
    #
    # ★★ 旧实现只摘带 `__aiops_sdk__` 标记的 → 业务那个**纯文本** handler
    #   留在 root 上：同一条日志输出两遍，且纯文本行会被 promtail 的 json 阶段丢弃。
    #
    # ★★ 曾经的回归：当时的写法是
    #   `if handler not in (stdout_handler, file_handler): root.removeHandler(handler)`，
    #   而这两个名字**在几行之后才被赋值** → 只要 root 上存在业务 handler，
    #   这里就抛 `UnboundLocalError`；`instrument.py` 又没包 try →
    #   **业务进程直接起不来**（正是注释承诺要支持的那条路径）。
    #   根因：把"排除自己的 handler"写成了"运行时比较"，而此刻自己的还没建。
    #   → 顺序反过来：**先把旧的都摘掉，再创建自己的**。这样既不需要引用未赋值的名字，
    #     也不会误摘（新 handler 是后加的）。幂等性不变（重复调用结果一致）。
    #
    # ★★ 摘掉**业务自己的** handler 会**静默改变它的日志去处**
    #   （业务的 SyslogHandler / 审计文件从此收不到任何日志）。摘是对的
    #   （否则同一条日志输出两遍、且纯文本行被 promtail 的 json 阶段丢弃），
    #   但"不许安静地失效"是本项目的口径 —— 所以如实回报 + 吵一次。
    removed: list[str] = []
    for handler in list(root.handlers):
        root.removeHandler(handler)
        removed.append(type(handler).__name__)
        if not getattr(handler, "__aiops_sdk__", False):
            warn_once(
                "business_handler_removed",
                f"setup_logging 摘掉了业务自己的 handler（{type(handler).__name__}）—— "
                f"日志改由 SDK 的 stdout + 文件双写接管。"
                f"若业务依赖该 handler（Syslog / 审计文件 / 自建收集器），"
                f"请在 setup_logging 之后重新挂上，否则那个去处会**静默失效**",
            )

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.__aiops_sdk__ = True          # type: ignore[attr-defined]
    root.addHandler(stdout_handler)

    wrote_file = False
    if log_dir is not None:
        # ★ 文件名用 <service>.jsonl：promtail 按文件名识服务，
        #   也便于"一个服务一个文件"地排查（不用先过滤）。
        file_handler = _make_file_handler(Path(log_dir) / f"{service}.jsonl", formatter)
        if file_handler is not None:
            file_handler.__aiops_sdk__ = True    # type: ignore[attr-defined]
            root.addHandler(file_handler)
            wrote_file = True

    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    # 压噪：这些库逐请求打 INFO，不压会把业务信号淹掉
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    # structlog 的输出也汇进同一个 formatter（`wrap_for_formatter` 是桥梁）
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    return {
        "service": service,
        "stdout": True,
        "file": str(Path(log_dir) / f"{service}.jsonl") if wrote_file else None,
        "level": str(level).upper(),
        "fields": list(LOG_FIELDS),
        "suppressed_loggers": list(_NOISY_LOGGERS),
        # ★★ 如实回报"摘掉了哪些 handler"（以前返回体里没有这一项，
        #   业务的日志去处被静默改变、事后无据可查）。
        "removed": removed,
    }
