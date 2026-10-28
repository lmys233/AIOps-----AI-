"""业务侧的下游调用。

一次业务调用 = 一次**真实的 HTTP 往返**。这里只做三件事：

    ① 把请求发出去（方法从契约表取，见 :mod:`observed.biz.api_contract`）
    ② 把失败**在发生的那一行**分成可聚类的 `error_type`（见 :mod:`observed.biz.errors`）
    ③ 解析响应体并返回普通 dict

★ 这里**没有**任何采集逻辑：

  · 不记指标（`downstream_*` 两条属于 SDK）
  · 不记业务日志（日志字段契约属于 SDK）
  · 不算 client-server gap（gap 的分子分母来自 SDK 的两处测量）

  SDK 会用 `obs.client("payment")` 替掉它，从而一次性拿到
  `downstream_requests_total` 与 `downstream_duration_seconds`。
  **替换前它是清白业务代码，替换后业务逻辑一行不改。**

---- 绝不把 4xx 包装成 5xx ----

真实事故是"用 GET 调 POST 端点 → 405 → 被上游包成 502 → 误判下游宕机"。
所以这里对下游返回的状态码分两类处理：

    下游 4xx  → **原样透传**那个状态码（404 就是 404，405 就是 405）
    下游 5xx  → 归成 `DOWNSTREAM_BAD_STATUS`（默认 502，语义是"下游坏了"）

如果无差别地一律包成 502，"方法写错了"与"下游挂了"在外层看起来完全一样，
排查方向从第一步就是错的。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

import httpx

from . import api_contract
from .errors import (
    DOWNSTREAM_BAD_STATUS,
    DOWNSTREAM_TIMEOUT,
    DOWNSTREAM_UNAVAILABLE,
    BizError,
)

__all__ = ["DownstreamClient"]

# 单次下游调用的超时。取值大于"连接池获取超时"（3s）是刻意的：
# 上游等下游的时间本来就该比等自己池子的时间长一点。
_DEFAULT_TIMEOUT_SEC = 5.0


class DownstreamClient:
    """按服务名调用下游的极简 HTTP 客户端（连接复用，进程内共享一个实例）。"""

    def __init__(
        self,
        service: str,
        targets: Mapping[str, str],
        *,
        timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self._service = service
        self._targets: dict[str, str] = {
            name: str(url).rstrip("/") for name, url in dict(targets).items()
        }
        self._client = httpx.AsyncClient(timeout=timeout_sec)

    async def aclose(self) -> None:
        """进程退出时释放连接（由 :func:`observed.biz.app.create_biz_app` 调用）。"""
        await self._client.aclose()

    # ------------------------------------------------------------------
    #  内部：拼 URL
    # ------------------------------------------------------------------
    def _url(
        self, target: str, path: str, path_params: Optional[Mapping[str, object]] = None
    ) -> str:
        base = self._targets.get(target)
        if not base:
            raise BizError(
                DOWNSTREAM_UNAVAILABLE,
                f"{self._service} 没有配置下游 {target!r}"
                f"（已配置：{sorted(self._targets)}）",
            )
        # path_for 会顺带校验"该路径在契约表里登记过"，render_path 负责替换占位符
        declared = api_contract.path_for(target, path)
        return base + api_contract.render_path(declared, path_params)

    # ------------------------------------------------------------------
    #  唯一对外方法
    # ------------------------------------------------------------------
    async def call(
        self,
        target: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        path_params: Optional[Mapping[str, object]] = None,
    ) -> dict[str, Any]:
        """调用下游并返回它响应体的 JSON 对象。

        失败一律抛 :class:`~observed.biz.errors.BizError`，
        由应用的异常处理器统一转成 HTTP 响应（见 :mod:`observed.biz.app`）。
        """
        url = self._url(target, path, path_params)
        # ★ 方法不允许手写 —— 只能从契约表取，这正是"不把 4xx 包装成 5xx"的防线
        method = api_contract.method_for(target, path)

        try:
            response = await self._client.request(method, url, params=dict(params or {}))
        except httpx.TimeoutException as exc:
            raise BizError(
                DOWNSTREAM_TIMEOUT,
                f"调用 {target} {path} 超时（{type(exc).__name__}: {exc}）",
            ) from exc
        except httpx.HTTPError as exc:
            raise BizError(
                DOWNSTREAM_UNAVAILABLE,
                f"调用 {target} {path} 失败（{type(exc).__name__}: {exc}）",
            ) from exc

        if response.status_code >= 400:
            status = response.status_code
            raise BizError(
                DOWNSTREAM_BAD_STATUS,
                f"下游 {target} {path} 返回 HTTP {status}：{response.text[:200]}",
                # 4xx 原样透传；5xx 交给 errors 表的默认值（502）
                http_status=status if status < 500 else None,
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise BizError(
                DOWNSTREAM_BAD_STATUS,
                f"下游 {target} {path} 的响应不是合法 JSON（状态 {response.status_code}）",
            ) from exc

        if isinstance(body, dict):
            return body
        # 契约要求下游返回 JSON 对象；真返回了数组/标量也不炸，包一层交出去
        return {"data": body}
