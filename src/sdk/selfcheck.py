"""SDK 接入自检：在进程内直接问"到底挂上了没有、产出的东西合不合契约"。

★ 这个模块要回答的问题只有一个：

    **"SDK 说自己对"不算数 —— 那它到底挂上了没有、产出的东西合不合契约？**

  契约套件是**黑盒**的：它只看得到 HTTP 响应与 `/metrics` 文本，
  看不到"池适配器到底有没有挂上"这种**进程内的事实**。

  所以本模块补的是这一层：**在进程内直接问自己**，并且
  **发现问题就以非 0 退出** —— 不允许"启动成功但契约不成立"。

★ 检查清单

  - 11 个指标名字 / 类型 / 标签齐全（改名等价于破坏契约）；
  - 每个 Gauge **都有 series**（值为 0 也算）；
  - 直方图分桶覆盖 5ms ~ 10s（否则故障时 P99 失去分辨力）；
  - 日志是单行 JSON 且七字段齐备；
  - 响应含 `_server_elapsed_ms` + `service`；
  - `GET /services` 形状正确、自身在列且不自探；
  - ★ **猴补真的挂上了**（池 / HTTP / 链路 / 变更）；
  - SDK 自身无裸 `except: pass`、不依赖标签顺序。

★ 最后一条为什么放在 SDK 里而不是只留在测试里：

  `find_metric_series` / `find_bare_except_pass` 这两个检查器原本沉淀在
  `tests/staticcheck.py`，但它们是**采集侧的实现约定**（标签顺序、绝不静默）。
  纳进 SDK 自检后，任何一次 `python -m sdk verify` 都会顺带守住它们 ——
  而不是只有在跑仓库测试时才守。
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, TextIO

from . import instrument
from .config import SdkConfig, load_config
from .contract import (
    HTTP_BUCKETS,
    LOG_ERROR_TYPE,
    LOG_FIELDS,
    LOG_LEVEL,
    METRICS,
    METRIC_NAMES,
    PROBE_HEALTH,
    PROBE_SERVICES,
    RESPONSE_FIELD_ELAPSED_MS,
    RESPONSE_FIELD_SERVICE,
)
from .state import Runtime, set_runtime

__all__ = [
    "Check",
    "verify",
    "verify_from_config",
    "find_metric_series",
    "find_bare_except_pass",
    "parse_prometheus_text",
]

# RFC3339（与 Promtail 的 timestamp 阶段同一形状）
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")

# 自检报告里"证据预览"的截断长度：够看出问题，又不至于把报告刷满
_PREVIEW_CHARS = 120


# ======================================================================
#  两个"沉淀下来的检查器"
# ======================================================================
def parse_prometheus_text(text: str) -> list[tuple[str, dict[str, str], float]]:
    """把 Prometheus 文本格式解析成 `[(指标名, 标签字典, 值), ...]`。

    ★ 为什么必须**解析**而不是 `in` 字符串匹配：

      Prometheus 文本里标签是**按字母序**输出的，而实现方写代码时是
      按自己的顺序写的。用字符串匹配 `metric{a="1",b="2"}` 去核对
      `metric{b="2",a="1"}` 会**判它不存在** —— 这是真实踩过的误报。
    """
    out: list[tuple[str, dict[str, str], float]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            name_part, _, value_part = line.rpartition(" ")
            value = float(value_part)
        except ValueError:
            continue
        if "{" in name_part:
            name, _, label_blob = name_part.partition("{")
            label_blob = label_blob.rstrip("}")
            labels: dict[str, str] = {}
            for match in re.finditer(r'(\w+)="((?:[^"\\]|\\.)*)"', label_blob):
                labels[match.group(1)] = match.group(2).replace('\\"', '"')
        else:
            name, labels = name_part, {}
        out.append((name, labels, value))
    return out


def find_metric_series(
    text: str, name: str, **labels: str
) -> list[float]:
    """在 Prometheus 文本里找 `name` 且**包含**给定标签的样本值（顺序无关）。

    ★ 三个刻意的设计：

      1. **不假设标签顺序** —— 解析后按字典比较（顺序假设踩过坑）
      2. **指标名精确匹配**，不做前缀匹配 —— 否则 `db_pool_max_connections`
         会误配 `db_pool_max_connections_created`（同族的另一处误报）
      3. 只要求"包含给定标签"，允许实际样本**带更多**标签
         （比如直方图的 `le`），因为调用方通常只关心其中几个
    """
    wanted = {k: str(v) for k, v in labels.items()}
    hits: list[float] = []
    for sample_name, sample_labels, value in parse_prometheus_text(text):
        if sample_name != name:
            continue
        if all(sample_labels.get(k) == v for k, v in wanted.items()):
            hits.append(value)
    return hits


def find_bare_except_pass(path: str | Path) -> list[str]:
    """定位「裸 `except ...: pass`」（绝不静默）。

    用 AST 判"`except` 块的首个语句是不是 `pass`" ——
    比正则可靠（正则分不清同一个 `except` 下是 `pass` 还是后续有真实处理）。
    """
    src = Path(path).read_text(encoding="utf-8-sig")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        # 解析不了就不是"没有裸 except"，而是**没测成** —— 报出来而不是放过
        return [f"{path}: 无法解析为 Python（语法错误）"]
    lines = src.splitlines()
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or not node.body:
            continue
        first = node.body[0]
        if isinstance(first, ast.Pass):
            text = lines[first.lineno - 1].strip() if first.lineno <= len(lines) else "pass"
            hits.append(f"{path}:{first.lineno}: {text}")
    return hits


# ======================================================================
#  检查结果
# ======================================================================
@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fatal: bool = True   # False 表示"这项不达标只提示，不影响退出码"


# ======================================================================
#  各项检查
# ======================================================================
def _metric_objects() -> dict[str, tuple[str, set[str]]]:
    """从 `sdk.definitions` 里取出**真实的指标对象**上的名字与标签。

    ★ 为什么不读源码文本：SDK 为满足"名字只有一份副本"，
      定义处写的是 `Histogram(_NAME["http_duration"], ...)`，
      文本里根本没有字面量名字 —— 文本匹配会**判正确实现为 FAIL**。
    """
    from . import definitions  # noqa: PLC0415

    found: dict[str, tuple[str, set[str]]] = {}
    for attr in dir(definitions):
        if attr.startswith("_"):
            continue
        obj = getattr(definitions, attr)
        name = getattr(obj, "_name", None)
        label_names = getattr(obj, "_labelnames", None)
        if isinstance(name, str) and label_names is not None:
            found[name] = (type(obj).__name__.lower(), set(label_names))
    return found


def check_metrics() -> list[Check]:
    """① 11 个指标：名字 / 类型 / 标签。"""
    out: list[Check] = []
    objects = _metric_objects()

    def lookup(metric_name: str) -> Optional[tuple[str, set[str]]]:
        """按对象上的名字查。

        ★ 必须处理一个细节：prometheus_client 的 `Counter` 对象会把
          `_total` 后缀**去掉**（`Counter("downstream_requests_total")._name`
          是 `"downstream_requests"`），而暴露出去的样本名才带 `_total`。
          不兜这一步，两个 Counter 会被误判成"未定义"。
        """
        if metric_name in objects:
            return objects[metric_name]
        if metric_name.endswith("_total"):
            return objects.get(metric_name[: -len("_total")])
        return None

    problems: list[str] = []
    for logic_name, (metric_name, kind, labels) in METRICS.items():
        if metric_name.endswith("_count"):
            # 直方图自带的 `_count` 不单独定义，用父指标核对
            parent = metric_name[: -len("_count")]
            hit = lookup(parent)
            if hit is None:
                problems.append(f"未定义 {parent}（{metric_name} 由它自带）")
            elif hit[0] != "histogram":
                problems.append(f"{parent} 应为 histogram，实际 {hit[0]}")
            continue
        hit = lookup(metric_name)
        if hit is None:
            problems.append(f"未定义指标 {metric_name}")
            continue
        actual_type, actual_labels = hit
        if actual_type != kind.lower():
            problems.append(f"{metric_name} 类型应为 {kind}，实际 {actual_type}")
        lack = sorted(set(labels) - actual_labels)
        if lack:
            problems.append(f"{metric_name} 缺标签 {lack}（实际 {sorted(actual_labels)}）")
    out.append(Check(
        f"{len(METRIC_NAMES)} 个指标名字/类型/标签齐备",
        not problems,
        "; ".join(problems) or f"实测 {len(objects)} 个指标对象",
    ))
    return out


def check_histogram_buckets() -> list[Check]:
    """③ 直方图分桶必须覆盖故障区间 5ms ~ 10s。"""
    lo, hi = min(HTTP_BUCKETS), max(HTTP_BUCKETS)
    ok = lo <= 0.005 and hi >= 10.0
    return [Check(
        f"直方图分桶覆盖故障区间（{lo:g}s ~ {hi:g}s）",
        ok,
        "分桶太窄会让故障样本全掉进 +Inf，P99 失去分辨力" if not ok else "",
    )]


def check_logging(service: str) -> list[Check]:
    """④ 日志：单行 JSON + 七字段 + RFC3339 + 大写 level。

    ★ 做法：**用配置好的 formatter 渲染一条合成的 LogRecord**，
      而不是去读日志文件 —— 后者依赖"恰好已经打过日志"，不稳定；
      前者直接验"格式化行为"本身，且不污染业务日志。
    """
    from . import logging_setup  # noqa: PLC0415

    out: list[Check] = []
    formatter = logging_setup.build_stdlib_formatter(service)
    # ★ `exc_info` 是 `LogRecord.__init__` 的**必填位置参数**（签名里没有默认值），
    #   所以这里显式给 None —— 这条不是"在打日志"，而是**构造一条合成日志**
    #   去验 formatter 的渲染行为。
    #
    #   ★ 曾经为了避免代码里出现 `exc_info=`，改成"构造完再赋属性"，
    #     结果构造这一行直接 `TypeError: missing 1 required positional argument`
    #     把 `sdk-verify` 崩掉。现在那条禁令改成**看语法**的判据
    #     （`staticcheck.find_logging_exc_info`：只有"日志调用里传 exc_info"
    #     才违规），所以这里可以按最自然的方式写。
    record = logging.LogRecord(
        name="sdk.selfcheck", level=logging.ERROR, pathname=__file__,
        lineno=1, msg="SDK 自检样本", args=(), exc_info=None,
    )
    # 业务用 extra 传的字段（error_type 是 Log Agent 聚类的依据）
    record.error_type = "SDK_VERIFY"  # type: ignore[attr-defined]
    text = formatter.format(record)

    single_line = "\n" not in text
    out.append(Check("日志是**单行**（多行会让采集端 json 阶段解析失败）",
                     single_line, text[:_PREVIEW_CHARS] if not single_line else ""))

    try:
        payload = json.loads(text)
    except ValueError as exc:
        out.append(Check("日志可解析为 JSON", False, f"{type(exc).__name__}: {exc}"))
        return out

    missing = [f for f in LOG_FIELDS if f not in payload]
    out.append(Check(f"日志字段齐备（{'/'.join(LOG_FIELDS)}）",
                     not missing, f"缺 {missing}" if missing else ""))
    out.append(Check("level 为**大写**（LogQL 写的是 level=\"ERROR\"）",
                     str(payload.get(LOG_LEVEL, "")).isupper(),
                     f"实际 {payload.get(LOG_LEVEL)!r}"))
    ts_ok = bool(_RFC3339.match(str(payload.get("ts", ""))))
    out.append(Check("ts 为 RFC3339（Promtail 的 timestamp 阶段按此解析）",
                     ts_ok, f"实际 {payload.get('ts')!r}"))
    out.append(Check("error_type 能从业务的 extra 透传",
                     payload.get(LOG_ERROR_TYPE) == "SDK_VERIFY",
                     f"实际 {payload.get(LOG_ERROR_TYPE)!r}"))

    # 异常必须被压成单行（业务 logger.exception 也不能破行）
    try:
        raise ValueError("sdk-verify-boom")
    except ValueError:
        err_record = logging.LogRecord(
            name="sdk.selfcheck", level=logging.ERROR, pathname=__file__,
            lineno=1, msg="带异常的日志", args=(), exc_info=sys.exc_info(),
        )
    err_text = formatter.format(err_record)
    out.append(Check("异常堆栈被压成单行（业务 logger.exception 也不破行）",
                     "\n" not in err_text and "sdk-verify-boom" in err_text,
                     err_text[:_PREVIEW_CHARS] if "\n" in err_text else ""))
    return out


class _EchoApp:
    """一个最小的 ASGI 应用，供"响应字段"检查使用（不依赖任何框架）。"""

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        body = json.dumps({"ok": True, "probe": "sdk-verify"}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii"))],
        })
        await send({"type": "http.response.body", "body": body, "more_body": False})


async def _call_asgi(app: Any, path: str) -> tuple[int, dict[str, Any]]:
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {"type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
         "query_string": b"", "headers": [], "scheme": "http", "server": ("127.0.0.1", 80),
         "client": ("127.0.0.1", 12345)},
        receive, send,
    )
    status = 200
    body = b""
    for message in messages:
        if message["type"] == "http.response.start":
            status = int(message["status"])
        elif message["type"] == "http.response.body":
            body += message.get("body", b"") or b""
    try:
        return status, json.loads(body.decode("utf-8"))
    except ValueError:
        return status, {"_raw": body[:_PREVIEW_CHARS].decode("utf-8", "replace")}


def check_response_fields(cfg: SdkConfig, runtime: Runtime) -> list[Check]:
    """⑤ 业务响应自动带 `_server_elapsed_ms` + `service`（业务侧零注入）。"""
    from .app import ObservedApp  # noqa: PLC0415

    app = ObservedApp(_EchoApp(), config=cfg, runtime=runtime, activate=False)
    status, body = asyncio.run(_call_asgi(app, "/sdk-verify-probe"))
    has_ms = isinstance(body.get(RESPONSE_FIELD_ELAPSED_MS), (int, float))
    has_svc = body.get(RESPONSE_FIELD_SERVICE) == cfg.service_name
    return [
        Check(f"响应自动注入 {RESPONSE_FIELD_ELAPSED_MS}（且 > 0）",
              has_ms and float(body[RESPONSE_FIELD_ELAPSED_MS]) > 0,
              f"实际 {body.get(RESPONSE_FIELD_ELAPSED_MS)!r}"),
        Check(f"响应自动注入 {RESPONSE_FIELD_SERVICE} = 本服务名",
              has_svc, f"实际 {body.get(RESPONSE_FIELD_SERVICE)!r}"),
        Check("注入后的响应状态与业务一致", status == 200, f"实际 {status}"),
    ]


def check_patches(cfg: SdkConfig, report: dict[str, Any]) -> list[Check]:
    """⑥ ★ 猴补/接管是否**真的挂上**。

    ★ 为什么这条最重要："启动成功"与"采集生效"是**两件事**。
      SDK 里任何一处挂载失败都只会表现成"某条数据没有"，
      而"没有数据"是最难排查的形态。
      所以这里直接问运行期对象：适配器打了没、instrumentor 装了没。
    """
    out: list[Check] = []

    # -- HTTP 客户端适配器（downstream_* 的来源）--
    # ★ 判据是"**当前的 send 上有没有补丁标记**"，而不是"report['http'] 里有没有两条"。
    #   patch() 是幂等的：第二次调用会跳过并返回空列表。
    #   用"这次调用做了什么"当判据，会让重复自检误报 —— 这与反复踩过的
    #   "拿代理当判据"是同一族问题，所以统一改成**问运行期对象的最终状态**。
    import httpx  # noqa: PLC0415

    async_patched = bool(getattr(httpx.AsyncClient.send, "__aiops_patched__", False))
    sync_patched = bool(getattr(httpx.Client.send, "__aiops_patched__", False))
    out.append(Check(
        "HTTP 客户端已接管（httpx 同步 + 异步 send）",
        async_patched and sync_patched,
        f"async={async_patched} sync={sync_patched}"
        + ("" if async_patched and sync_patched else " —— downstream_* 两条会缺数据"),
    ))

    # -- 池适配器（db_pool_* 的来源）--
    if cfg.pool_driver:
        if cfg.pool_driver == "asyncpg":
            import asyncpg  # noqa: PLC0415

            patched = bool(getattr(asyncpg.create_pool, "__aiops_patched__", False))
            out.append(Check(
                "连接池工厂已接管（asyncpg.create_pool）",
                patched,
                "" if patched else "工厂函数上没有打上补丁标记 —— db_pool_* 会恒报 0",
            ))
        else:
            out.append(Check(
                f"连接池驱动 {cfg.pool_driver} 有适配器",
                False,
                f"当前只适配 asyncpg —— db_pool_* 会以值 0 暴露（已告警一次）",
                fatal=False,
            ))
    else:
        out.append(Check("本服务未声明连接池（[pool].driver 为空）",
                         True, "gateway 这类无池服务是合法的"))

    # -- 日志--
    log_info = report.get("logging") or {}
    out.append(Check("日志层已接管（stdout + 文件双写）",
                     bool(log_info.get("stdout")) and bool(log_info.get("file")),
                     f"file={log_info.get('file') or '（未配置 log_dir）'}"))

    # -- 链路--
    tracing = report.get("tracing") or {}
    if tracing.get("endpoint"):
        # ★ 判据是"**instrumentor 真的装上了**"，不是"这一次调用返回了 initialized=True"。
        #   `initialize()` 是幂等的：同一进程里第二次调用会返回 False（意思是"已经装过了"）。
        #   把那个当成失败，会让"跑两次自检"误报 —— 自检自己必须经得起重复执行。
        instrumented = tracing.get("instrumented") or []
        out.append(Check(
            "链路自动埋点已初始化，且 instrumentor 真的装上",
            bool(instrumented),
            f"instrumented={instrumented or '（无）'} "
            f"本次初始化={tracing.get('initialized')} 端可达={tracing.get('endpoint_reachable')}",
        ))
        if not tracing.get("endpoint_reachable"):
            out.append(Check(
                "OTLP 端点可达（不可达时不导出 span，但 trace 上下文照常）",
                False,
                "Collector 未起来 —— 先 `.\\run.ps1 infra-up` 再重启服务",
                fatal=False,
            ))
    else:
        out.append(Check("未配置 [tracing].otlp_endpoint（不出链路）", True))

    # -- 变更通道--
    from .changes import resolve_dsn  # noqa: PLC0415

    out.append(Check(
        "变更留痕有可用的写库凭据（环境变量 RUNNER_DATABASE_URL）",
        bool(resolve_dsn()),
        "没有 DSN —— 「为什么突然变了」这一路证据会缺失",
        fatal=False,
    ))
    out.append(Check(
        "变更留痕的比对范围 = 本服务用到的业务参数",
        bool(cfg.env_bridge),
        f"env_bridge 声明了 {len(cfg.env_bridge)} 个键：{sorted(cfg.env_bridge)}",
        fatal=False,
    ))
    return out


def check_services(cfg: SdkConfig, runtime: Runtime) -> list[Check]:
    """⑥ `GET /services` —— 服务清单端点。

    ★ 只验**形状与自证**，不验"同伴是否都活着"：
      那是环境事实，单服务环境里同伴本来就该是 `unreachable`
      —— 把"同伴没起"当自检失败，会逼人配假数据来让检查变绿。
    """
    from .app import ObservedApp  # noqa: PLC0415

    app = ObservedApp(_EchoApp(), config=cfg, runtime=runtime, activate=False)
    status, body = asyncio.run(_call_asgi(app, PROBE_SERVICES))
    entries = body.get("services") if isinstance(body.get("services"), list) else []
    required = {"status", "service", "declared_count", "alive_count", "services", "unreachable"}
    mine = [e for e in entries if isinstance(e, dict) and e.get("name") == cfg.service_name]

    # ---- 身份与依赖自报 ----
    health_status, health_body = asyncio.run(_call_asgi(app, PROBE_HEALTH))
    identity_keys = {"cluster", "env", "namespace", "depends_on"}
    own_identity = mine[0] if mine else {}
    self_depends = set(health_body.get("depends_on") or [])
    return [
        Check("GET /services 返回 200（服务清单端点存在）",
              status == 200, f"实际 HTTP {status}"),
        Check("/services 响应含全部契约字段（status/service/declared_count/alive_count/services/unreachable）",
              required <= set(body), f"缺字段：{sorted(required - set(body))}"),
        Check("回答者自身在 services 里且标 probed=false（★ 不做 HTTP 自探）",
              bool(mine) and mine[0].get("probed") is False,
              f"services={[e.get('name') for e in entries if isinstance(e, dict)]}"),
        Check("declared_count = 自己 + 同伴数（不是猜的）",
              body.get("declared_count") == len(cfg.peer_services) + 1,
              f"declared_count={body.get('declared_count')} 同伴={len(cfg.peer_services)}"),
        Check("alive_count 与 services 条数一致（清单只列存活）",
              body.get("alive_count") == len(entries),
              f"alive_count={body.get('alive_count')} len(services)={len(entries)}"),
        # ★ 身份与依赖必须自报（Agent 侧靠它做实体对齐与拓扑）
        Check("★ GET /health 自报身份与依赖（cluster/env/namespace/depends_on）",
              health_status == 200 and identity_keys <= set(health_body),
              f"HTTP {health_status}，字段={sorted(set(health_body))}"),
        Check("★ 身份字段与配置一致（空就是空，**不自造默认值**）",
              all(health_body.get(key) == getattr(cfg, key, None) or
                  (not health_body.get(key) and not getattr(cfg, key, None))
                  for key in ("cluster", "env", "namespace")),
              f"自报={ {k: health_body.get(k) for k in ('cluster', 'env', 'namespace')} } "
              f"配置={cfg.identity()}"),
        Check("★ depends_on 与配置声明的下游一致",
              self_depends == set(cfg.depends_on),
              f"自报={sorted(self_depends)} 配置={sorted(cfg.depends_on)}"),
        Check("★ /services 里自身那条也带身份与依赖（不是只有 /health 有）",
              set(own_identity) >= identity_keys,
              f"字段={sorted(set(own_identity))}"),
    ]


def check_gauge_series(service: str) -> list[Check]:
    """② 每个 Gauge **都有 series**（值为 0 也算）。

    ★ 判据是"这个 service 标签下有没有 series"：
      Prometheus 客户端不 `set` 就不产生 series，于是"值为 0"与
      "指标不存在"无法区分，`== 0` 类告警**永不触发**。
    """
    from prometheus_client import REGISTRY  # noqa: PLC0415

    gauge_names = {name for name, kind, _ in METRICS.values() if kind == "Gauge"}
    seen: dict[str, int] = {}
    for family in REGISTRY.collect():
        if family.name not in gauge_names:
            continue
        count = sum(1 for s in family.samples
                    if s.labels.get("service") == service and s.name == family.name)
        seen[family.name] = seen.get(family.name, 0) + count

    problems = [f"{n} 没有 series" for n in sorted(gauge_names) if not seen.get(n)]
    return [Check(
        f"所有 Gauge 在 service={service} 下都有 series（值为 0 也要有）",
        not problems,
        "; ".join(problems) or f"{len(seen)} 个 Gauge 均有 series",
    )]


def check_sdk_sources() -> list[Check]:
    """⑦ SDK 自身：无裸 `except: pass`。"""
    root = Path(__file__).resolve().parent
    hits: list[str] = []
    for path in sorted(root.rglob("*.py")):
        hits += find_bare_except_pass(path)
    return [Check(
        "SDK 源码中无裸 `except ...: pass`（绝不静默）",
        not hits,
        "; ".join(hits[:5]),
    )]


# ======================================================================
#  入口
# ======================================================================
def verify(
    cfg: SdkConfig,
    *,
    out: Optional[TextIO] = None,
    activate: bool = True,
) -> int:
    """对一个已装载的配置跑自检。返回退出码（0 = 全过）。"""
    stream = out or sys.stdout
    runtime = Runtime(service_name=cfg.service_name, config=cfg)
    set_runtime(runtime)

    # ★ 先接管一遍 —— 自检要验的正是"接管之后"的状态
    report: dict[str, Any] = {}
    if activate:
        report = dict(instrument.activate(runtime, cfg))

    checks: list[Check] = []
    checks += check_metrics()
    checks += check_histogram_buckets()
    checks += check_gauge_series(cfg.service_name)
    checks += check_logging(cfg.service_name)
    checks += check_response_fields(cfg, runtime)
    checks += check_services(cfg, runtime)
    checks += check_patches(cfg, report)
    checks += check_sdk_sources()

    print("=" * 70, file=stream)
    print(f"  SDK 接入自检 · service={cfg.service_name} · 配置={cfg.config_path}", file=stream)
    print("=" * 70, file=stream)
    for item in checks:
        mark = "[OK]  " if item.ok else ("[FAIL]" if item.fatal else "[WARN]")
        print(f"  {mark} {item.name}", file=stream)
        if item.detail and not item.ok:
            print(f"          {item.detail}", file=stream)

    fatal_failed = [c for c in checks if not c.ok and c.fatal]
    warned = [c for c in checks if not c.ok and not c.fatal]
    print("=" * 70, file=stream)
    print(f"  通过 {sum(1 for c in checks if c.ok)} / {len(checks)}；"
          f"不达标 {len(fatal_failed)}；提示 {len(warned)}", file=stream)
    if fatal_failed:
        print("  结论: 契约不成立 —— 不允许「启动成功但契约不成立」", file=stream)
        return 1
    if warned:
        print("  结论: 契约成立；上面的提示项属环境或配置范围，请逐条确认", file=stream)
    else:
        print("  结论: 契约成立", file=stream)
    return 0


def verify_from_config(
    config_path: Optional[str] = None,
    service: Optional[str] = None,
    *,
    out: Optional[TextIO] = None,
) -> int:
    """`python -m sdk verify --config ... --service ...` 的实现。"""
    cfg = load_config(config_path, service=service)
    return verify(cfg, out=out)
