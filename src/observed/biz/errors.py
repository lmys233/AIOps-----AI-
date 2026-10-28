"""业务错误的分类与 HTTP 状态映射。

★ 这一层留在**业务侧**是刻意的：

  "库存不足"、"订单不存在"、"取连接超时" 这些分类**只有业务知道**。
  SDK 猜不出来，也不该猜。

  但 SDK 需要它 —— 日志要求带 `error_type`：
  SDK 的异常处理器会把这里的分类**原样写进日志字段**，
  于是日志聚类（Log Agent）与错误响应体用的是同一份取值，不会两边漂移。

★ 取值命名规则：`<子系统>_<失败形态>`，全大写下划线。
  集中在一处，避免出现 `pool timeout` / `POOL_TIMEOUT` / `poolTimeout`
  三种写法被当成三类故障（Log Agent 的聚类会直接失效）。
"""

from __future__ import annotations

from typing import Any, Final

__all__ = ["BizError", "ERROR_HTTP_STATUS", "ERROR_MESSAGE", "classification"]


# ------------------------------------------------------------------
#  取值集合
# ------------------------------------------------------------------
POOL_ACQUIRE_TIMEOUT: Final = "POOL_ACQUIRE_TIMEOUT"
POOL_QUERY_FAILED: Final = "POOL_QUERY_FAILED"
POOL_INIT_FAILED: Final = "POOL_INIT_FAILED"
DOWNSTREAM_TIMEOUT: Final = "DOWNSTREAM_TIMEOUT"
DOWNSTREAM_UNAVAILABLE: Final = "DOWNSTREAM_UNAVAILABLE"
DOWNSTREAM_BAD_STATUS: Final = "DOWNSTREAM_BAD_STATUS"
INVALID_PARAM: Final = "INVALID_PARAM"
NOT_FOUND: Final = "NOT_FOUND"
METHOD_NOT_ALLOWED: Final = "METHOD_NOT_ALLOWED"
ORDER_NOT_FOUND: Final = "ORDER_NOT_FOUND"
STOCK_INSUFFICIENT: Final = "STOCK_INSUFFICIENT"
INTERNAL: Final = "INTERNAL"

# ------------------------------------------------------------------
#  error_type → HTTP 状态码
#
#  ★ 映射原则（来自一次真实事故：405 被上游包装成 502，
#    排查时误判为"下游宕机"）：
#      · 客户端问题      → 4xx（明确告诉调用方"是你错了"）
#      · 依赖不可用/超时 → 503/504（明确告诉调用方"是我依赖挂了"）
#      · 没找到          → 404，**不要包成 5xx**
# ------------------------------------------------------------------
ERROR_HTTP_STATUS: Final[dict[str, int]] = {
    POOL_ACQUIRE_TIMEOUT: 503,
    POOL_QUERY_FAILED: 503,
    POOL_INIT_FAILED: 503,
    DOWNSTREAM_TIMEOUT: 504,
    DOWNSTREAM_UNAVAILABLE: 503,
    DOWNSTREAM_BAD_STATUS: 502,
    INVALID_PARAM: 422,
    NOT_FOUND: 404,
    METHOD_NOT_ALLOWED: 405,
    ORDER_NOT_FOUND: 404,
    STOCK_INSUFFICIENT: 409,
    INTERNAL: 500,
}

ERROR_MESSAGE: Final[dict[str, str]] = {
    POOL_ACQUIRE_TIMEOUT: "获取数据库连接超时（连接池已饱和或持有连接过久）",
    POOL_QUERY_FAILED: "数据库查询失败",
    POOL_INIT_FAILED: "无法建立数据库连接",
    DOWNSTREAM_TIMEOUT: "调用下游服务超时",
    DOWNSTREAM_UNAVAILABLE: "下游服务不可达",
    DOWNSTREAM_BAD_STATUS: "下游服务返回错误状态",
    INVALID_PARAM: "请求参数不合法",
    NOT_FOUND: "路径或资源不存在",
    METHOD_NOT_ALLOWED: "HTTP 方法不被该端点接受（调用方方法写错了）",
    ORDER_NOT_FOUND: "订单不存在",
    STOCK_INSUFFICIENT: "库存不足",
    INTERNAL: "内部错误",
}


def classification(error_type: str) -> tuple[int, str]:
    """返回 ``(http_status, default_message)``。

    未登记的取值**不抛异常**（错误路径不能因为分类缺失而再次失败），
    退化为 500 + INTERNAL 描述 —— 保证"绝不缺字段"。
    """
    if error_type not in ERROR_HTTP_STATUS:
        return ERROR_HTTP_STATUS[INTERNAL], ERROR_MESSAGE[INTERNAL]
    return ERROR_HTTP_STATUS[error_type], ERROR_MESSAGE[error_type]


class BizError(Exception):
    """业务的**受控失败**。

    ★ 为什么不让底层库的异常直接冒到 FastAPI：

      asyncpg / httpx 的异常一路冒上去，外层只能笼统记一条 "internal error"，
      Log Agent 拿到的就全是同一类，聚类失去意义。
      用本异常在**失败发生的那一行**就定好分类，后续所有层都能原样携带。

    ★ 业务只负责"这是什么错误"（`error_type`）与"给人看的话"（`detail`），
      **不负责 HTTP 状态码的推导**（由 :func:`classification` 统一给），
      也不负责记日志（那是 SDK 的事）。
    """

    __slots__ = ("error_type", "detail", "http_status")

    def __init__(self, error_type: str, detail: str = "",
                 http_status: int | None = None) -> None:
        status, default_message = classification(error_type)
        self.error_type = error_type
        self.detail = detail or default_message
        self.http_status = status if http_status is None else http_status
        super().__init__(f"[{self.error_type}] {self.detail}")

    def to_body(self) -> dict[str, Any]:
        """错误响应体（**不含** `_server_elapsed_ms` / `service` —— 那两字段由 SDK 补）。"""
        return {"error_type": self.error_type, "detail": self.detail}
