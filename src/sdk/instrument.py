"""自动埋点的总装：按配置把要接管的东西一次性接管掉。

★ 一句话：**在导入业务代码之前，把要接管的东西全接管掉。**

  顺序很重要。业务的连接池通常在自己的模块导入期或 lifespan 里创建，
  所以猴补必须**先于** `import_app()` 完成 —— 否则工厂函数已经被调用过了，
  我们拿不到那个对象，`db_pool_*` 就只能永远报 0。

      load_config() → 建 Runtime → activate() → import_app() → ObservedApp()

★ 这里也是"采不到就吵"的收口处：

  配置声明了 `[pool].driver = "xxx"`，但我们没有 xxx 的适配器 —— 这不能静默。
  它会 **告警一次**，说明"这个 driver 没有适配器，db_pool_* 将以值 0 暴露"。
  静默的后果是：Agent 查到 `db_pool_in_use_connections == 0`，
  于是判断"池没饱和" —— 而真相是**根本没人测**。这与"没数据"是两回事。
"""

from __future__ import annotations

from typing import Optional

import logging

from .config import KNOWN_POOL_DRIVERS, SdkConfig
from .definitions import init_gauges
from .state import Runtime

__all__ = ["activate", "ActivationReport"]

logger = logging.getLogger("sdk.instrument")

# 5 个旋钮与其"未注入"默认值。
# ★ 本项目的业务系统已不再支持故障注入，
#   所以这些值**不会生效**；SDK 如实报 0，并在被设置时告警一次。
_FAULT_KNOBS: dict[str, tuple[str, str]] = {
    "DB_POOL_SIZE": ("20", "order"),
    "DB_POOL_ACQUIRE_HOLD_MS": ("0", "order"),
    "DB_POOL_TIMEOUT_SEC": ("3", "order"),
    "ARTIFICIAL_DELAY_MS": ("0", "payment"),
    "ARTIFICIAL_ERROR_RATE": ("0.0", "payment"),
}


class ActivationReport(dict):
    """`activate()` 的返回值：做了哪些接管、跳过了什么。自检读它。

    ★ 用 dict 子类只是为了打印方便；它的**语义是"证据"**：
      "SDK 到底挂上了什么"这个问题必须能被程序回答，不能靠人读日志猜。
    """


def _activate_pool(runtime: Runtime, cfg: SdkConfig) -> Optional[str]:
    driver = cfg.pool_driver
    runtime.diagnostics.capability('sdk_pool','unconfigured','No pool declared')
    if not driver:
        # 没声明池 = 本服务没有数据库池。这是**合法的**（gateway 就是），
        # 不是错误，也不该告警。Gauge 已在 init_gauges 里报 0。
        return None

    if driver == "asyncpg":
        from .adapters import pool_asyncpg  # noqa: PLC0415
        from .adapters import query_asyncpg
        if runtime.diagnostics.enabled:query_asyncpg.patch(runtime)

        result = pool_asyncpg.patch(runtime)
        runtime.diagnostics.capability('sdk_pool','partial' if result else 'unsupported',
            'Factory patched; awaiting an actual pool' if result else 'Driver unavailable',driver)
        return result

    # ★ 未支持的驱动：不静默（见模块文档）
    runtime.warn_once(
        f"pool_driver_unsupported:{driver}",
        f"[pool].driver = {driver!r}，但 SDK 目前**只适配 asyncpg**"
        f"（允许声明的驱动名：{list(KNOWN_POOL_DRIVERS)}，其中只有 asyncpg 有适配器）—— "
        f"db_pool_* 四条将以值 0 暴露（**不是没采到，是没有适配器**）。"
        f"如需支持，请为该驱动补一个 adapters/pool_*.py",
    )
    runtime.note_patch(f"pool:{driver}", f"未适配的驱动 {driver}（报 0）")
    runtime.diagnostics.capability('sdk_pool','unsupported','No adapter for declared driver',driver)
    return None


def _activate_http(runtime: Runtime) -> list[str]:
    from .adapters import http_httpx  # noqa: PLC0415

    return http_httpx.patch(runtime)


def _warn_fault_knobs(runtime: Runtime) -> None:
    """如果外部设了故障旋钮，告警一次说明"它不会被生效"。

    ★ 为什么必须吵：评测的人以为"设了旋钮 = 故障注入了"，
      而本项目的业务系统已把注入能力去掉。不提醒的话，
      他会把"没注入成功"误判成"Agent 没查出来"。
    """
    import os  # noqa: PLC0415

    for knob, (default, owner) in _FAULT_KNOBS.items():
        raw = os.environ.get(knob)
        if raw is None or str(raw).strip() in ("", default):
            continue
        runtime.warn_once(
            f"fault_knob_ignored:{knob}",
            f"检测到环境变量 {knob}={raw}（默认 {default}）—— "
            f"但本业务系统已**不支持故障注入**，"
            f"该值被忽略，fault_injection_active 仍为 0。"
            f"造故障请改用外部手段（停 Postgres / 限资源 / 注入网络延迟）。"
            f"该旋钮原属服务：{owner}",
        )


def _activate_diagnostics(runtime):
    if not runtime.diagnostics.enabled:return False
    from .workers import patch as patch_workers
    from .diagnostic_export import start_exporter
    from .deadlines import patch as patch_deadlines
    from .shutdown_observer import patch as patch_shutdown
    patch_workers(runtime)
    patch_deadlines(runtime);patch_shutdown(runtime)
    return start_exporter(runtime) is not None


def activate(runtime: Runtime, cfg: Optional[SdkConfig] = None) -> ActivationReport:
    """按配置接管需要接管的一切。幂等（重复调用不会重复猴补）。"""
    cfg = cfg if cfg is not None else runtime.config

    report = ActivationReport()
    report['diagnostic_exporter']=_activate_diagnostics(runtime)
    report["service"] = runtime.service_name

    # 1) Gauge 无条件初始化 —— 必须在任何请求之前
    init_gauges(runtime.service_name)
    report["gauges_initialized"] = True

    # 2) ★ 链路：入口点自动埋点
    #    ★ 放在 HTTP/池适配器**之前**：OTel 的 httpx instrumentor 会包装
    #      `httpx.AsyncClient.send`，我们的适配器随后包装**它**——
    #      我们的 patch 读的是"当前属性"而不是原始函数，所以是**链式**而非覆盖，
    #      两边都生效（少了这一条，client span 会被我们的 patch 抹掉）。
    if cfg is not None:
        from .tracing import setup_tracing  # noqa: PLC0415

        report["tracing"] = setup_tracing(cfg, runtime)

    # 3) ★ 日志：接管 root logger
    #    必须早于 import_app —— 业务模块导入期就会打日志，
    #    晚一步那些日志就是未格式化的纯文本（promtail 解析不了）。
    from .logging_setup import setup_logging  # noqa: PLC0415

    report["logging"] = setup_logging(
        runtime.service_name,
        log_dir=_scoped_log_dir(runtime,cfg),
        level=(cfg.log_level if cfg is not None else "INFO"),
    )

    # 4) 池适配器（`db_pool_*` 四条）
    if cfg is not None:
        report["pool"] = _activate_pool(runtime, cfg)
    else:
        report["pool"] = None

    # 5) HTTP 客户端适配器（`downstream_*` 两条）
    report["http"] = _activate_http(runtime)

    # 6) 旋钮状态如实上报（含"设了但不生效"的提醒）
    _warn_fault_knobs(runtime)
    report["fault_injection"] = "已按 5 个 fault_type 暴露，正常态恒 0"

    logger.debug("SDK 自动埋点完成: %s", dict(report))
    return report


def _scoped_log_dir(runtime,cfg):
    from pathlib import Path
    if cfg is None:return None
    if not getattr(cfg,'observed_name',''):return cfg.log_dir
    from .system_scope import buffer_key
    return (cfg.log_dir or Path('.run/sdklogs'))/buffer_key(runtime)
