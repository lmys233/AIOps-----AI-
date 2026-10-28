"""外部依赖的装配（数据源 / 工具执行器 / 编排层的零件）。

--------------------------------------------------------------------------------
★ 为什么单独一个文件
--------------------------------------------------------------------------------

"用 `.env` 里的地址装配四路数据源"这件事，**CLI（`agents/__main__.py`）与
服务壳（`server.py`）都要做一遍**。放在 `__main__.py` 里再被 server import 是
反面教材（`__main__` 模块是"入口脚本"，不是库），所以这里提成正式模块：

| 调用方 | 用途 |
|---|---|
| `python -m agent.agents`（取证 CLI） | 手工跑一次取证 |
| `agent/server.py::build_orchestrator` | `/v1/chat` 真的去查数据 |

★ 缺哪个数据源就**不装配哪个**（那一类信号如实降级）—— 这是"单路不可用
不影响整体"，也是"没查成 ≠ 没有异常"。**绝不**用空实现顶替：
空实现会让"没查成"变成"查了没异常"，从而让 RCA 排除掉正确的假设。
"""

from __future__ import annotations

import os
import sys
from typing import Any, Mapping, Optional

from ..tools.executor import InMemoryAuditSink, ToolExecutor
from ..tools.registry import build_default_registry
from ..tools.cache import LocalQueryCache, cache_dir_for
from ..tools.handlers import ToolContext
from ..tools.sources import LokiSource, PrometheusSource, SqlSource, TempoSource

__all__ = ["build_executor", "inject_topology"]


def _env(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name) or "").strip()


def _source(factory: Any, url: str) -> Any:
    """地址为空就**不装配**这个数据源（那一类信号会如实降级，而不是连到空地址上）。"""
    return factory(url) if url else None


def inject_topology(env: Mapping[str, str], *, enabled: bool = True) -> Any:
    """DB 可用时注入 `SqlTopologyStore`，否则返回 `None`（**如实降级**）。

    ★ 为什么"注入失败"不能变成"空结果"：`topology_read` 对 `None` 的处理是
      `SourceUnavailable`（**没查成**）；如果这里改成返回一个空的内存实现，
      拓扑这一路会报出"查了没有依赖关系" —— 那是**假阴性**，
      RCA 会据此排除"邻居传播"这类假设。
    """
    if not enabled:
        return None
    dsn = _env(env, "AIOPS_DATABASE_URL")
    if not dsn:
        print("  [!] 未配置 AIOPS_DATABASE_URL —— 拓扑这一路会如实降级"
              "（跑 `topology-migrate` + `topology-sync` 后可用）", file=sys.stderr)
        return None
    from ..topology.store import SqlTopologyStore

    return SqlTopologyStore(dsn=dsn)


def build_executor(env: Optional[Mapping[str, str]] = None, *,
                   topology: bool = True) -> ToolExecutor:
    """按 `.env` 里的地址装配四路数据源（缺哪个就缺哪路 —— **如实降级**）。

    Args:
        env: 环境变量映射（默认 `os.environ`；调用方通常已 `load_env_file()`）。
        topology: 是否接拓扑存储（DB 不可用时它自己会如实降级）。
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    return ToolExecutor(
        registry=build_default_registry(),
        context=ToolContext(
            prometheus=_source(PrometheusSource, _env(environ, "AIOPS_PROMETHEUS_URL")),
            loki=_source(LokiSource, _env(environ, "AIOPS_LOKI_URL")),
            tempo=_source(TempoSource, _env(environ, "AIOPS_TEMPO_URL")),
            sql=SqlSource(dsn=_env(environ, "AIOPS_DATABASE_URL"))
            if _env(environ, "AIOPS_DATABASE_URL") else None,
            topology=inject_topology(environ, enabled=topology),
            # ★★ 查询结果**本地缓存**（缓存到本地、时间戳作键）。
            #   `AIOPS_QUERY_CACHE=off` 可关；目录用 `AIOPS_QUERY_CACHE_DIR` 覆盖。
            cache=build_query_cache(environ),
            # ★★ **查询数据记忆**（索引，按会话作用域键控）。与本地缓存是两层：
            #   记忆说"这段窗口我有"，缓存负责把**数据本体**取出来（见 `handlers.cached_call`）。
            query_memory=build_query_memory(environ),
        ),
        audit=InMemoryAuditSink(),
    )


def build_query_memory(env: Optional[Mapping[str, str]] = None) -> Optional[Any]:
    """按环境装配查询数据记忆（**默认开**；`AIOPS_QUERY_MEMORY=off` 关）。"""
    from ..memory.queries import QueryMemory

    environ: Mapping[str, str] = os.environ if env is None else env
    flag = str(environ.get("AIOPS_QUERY_MEMORY") or "on").strip().lower()
    if flag in ("off", "false", "0", "no"):
        return None
    return QueryMemory()


def build_query_cache(env: Optional[Mapping[str, str]] = None) -> Optional[LocalQueryCache]:
    """按环境装配本地查询缓存（**默认开**；`off/false/0/no` 关）。

    ★ 为什么默认开：它是**纯省时**的副本（fail-open、命中留痕、开窗只给秒级 TTL），
      不开的话每次提问都要把同样的窗口重查一遍（实测 7 天窗一轮要 28~57s）。
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    flag = str(environ.get("AIOPS_QUERY_CACHE") or "on").strip().lower()
    if flag in ("off", "false", "0", "no"):
        return None
    return LocalQueryCache(cache_dir_for(dict(environ)))
