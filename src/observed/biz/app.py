"""业务应用的组装。

四个服务的 `main.py` 只写**业务**；公共的"启动 / 关停 / 错误响应 / 日志开关"
收在这里，避免四份几乎相同的样板代码各自漂移。

★ 本模块**只做业务应用自己的事**，刻意不含：

  · `/health`、`/ready`、`/metrics`、`/services` 这些探针与清单端点 —— 它们属于采集契约，
    由 SDK 统一注册。
  · 指标 / 结构化日志 / 链路 / 响应字段注入 —— 同上，全在 SDK。

★ 错误响应的形状由 :class:`observed.biz.errors.BizError` 定：

    {"error_type": "...", "detail": "..."}

  `error_type` 是**唯一取值**（业务只分类，不推导 HTTP 状态码），
  这样日志聚类与响应体用的是同一份取值，不会两边漂移。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .errors import BizError
from .settings import BizSettings

__all__ = ["create_biz_app", "configure_logging"]

# 所有业务日志共用的顶层名字；SDK 会接管这个名字下的 handler 与 formatter
LOGGER_ROOT = "observed"

# 这两家库对每一次请求都打一条 INFO，会把业务信号淹掉
_NOISY_LOGGERS = ("httpx", "httpcore", "asyncio")

# 合法级别名的清单（只用于错误话术，判定仍以 `logging` 模块的属性为准）
_LEVEL_HINT = "CRITICAL / ERROR / WARNING / INFO / DEBUG / NOTSET"

# 配置告警的去重集合：**同一条只吵一次**。
# 去重不是"允许静默"：持续状态每次都吵会把日志刷满，
# 一次都不吵则等于把"配错了"这件事永久藏起来。
_WARNED: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    """配置类告警：要吵，但**只吵一次**（与 SDK 侧的 warn-once 同一风格）。"""
    if key in _WARNED:
        return
    _WARNED.add(key)
    logging.getLogger(f"{LOGGER_ROOT}.config").warning(message)


def configure_logging(level: str = "INFO") -> None:
    """最小可用的标准库日志配置。

    ★ 为什么用标准库 `logging` 而不是直接上 structlog：

      "输出单行 JSON + 六个约定字段"是**采集端契约**，属于 SDK 的职责。
      业务侧只要**正常地打日志**，SDK 在初始化时挂上自己的 handler/formatter，
      就能把这些日志变成合规的 JSON —— 这就是"0 侵入"在日志这一路的全部含义。

    幂等：重复调用不会重复添加 handler（uvicorn reload / 多次导入都安全）。

    ★ 非法的 `level` **不崩、不静默**：回落到 `INFO`，并告警一次。
      静默吞掉拼错的级别，会让"日志级别没生效"变成一个查不出原因的悬案。
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-7s %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        root.addHandler(handler)

    resolved = getattr(logging, str(level).upper(), None)
    # bool 也要排除：`logging.raiseExceptions` 这种同名开关不是级别
    if not isinstance(resolved, int) or isinstance(resolved, bool):
        _warn_once(
            f"log_level:{level}",
            f"非法日志级别 {level!r}，已回落到 INFO（合法取值：{_LEVEL_HINT}）",
        )
        resolved = logging.INFO
    root.setLevel(resolved)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def create_biz_app(
    settings: BizSettings,
    *,
    pool: Optional[Any] = None,
    downstream: Optional[Any] = None,
) -> FastAPI:
    """装配一个业务服务。

    :param settings: 该服务的配置（服务名 / 日志级别 / 端口 / 下游地址）
    :param pool: 连接池，提供 ``start()`` / ``close()``；gateway 传 ``None``
    :param downstream: 下游客户端，提供 ``aclose()``；无下游的服务传 ``None``
    """
    configure_logging(settings.log_level)
    logger = logging.getLogger(f"{LOGGER_ROOT}.{settings.service}")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if pool is not None:
            # 预热失败不抛出：服务照常启动，把问题暴露在业务错误上，
            # 而不是变成一个起不来的进程（下游抖动不该导致进程被误杀）
            await pool.start()
            logger.info(
                "连接池就绪：pool_max=%s acquire_timeout_sec=%s",
                settings.db_pool_size,
                settings.db_pool_timeout_sec,
            )
        logger.info("%s 启动完成，监听端口 %s", settings.service, settings.port)
        try:
            yield
        finally:
            if downstream is not None:
                await downstream.aclose()
            if pool is not None:
                await pool.close()
            logger.info("%s 已停止", settings.service)

    app = FastAPI(title=f"{LOGGER_ROOT}.{settings.service}", lifespan=lifespan)
    app.state.service = settings.service

    @app.exception_handler(BizError)
    async def _biz_error(request: Request, exc: BizError) -> JSONResponse:
        """受控失败的统一出口：分类 + 话术透出，HTTP 状态码由分类表推导。"""
        logger.error(
            "%s %s 失败：%s",
            request.method,
            request.url.path,
            exc.detail,
            extra={"error_type": exc.error_type},
        )
        return JSONResponse(status_code=exc.http_status, content=exc.to_body())

    return app
