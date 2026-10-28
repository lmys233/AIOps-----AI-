"""gateway 服务 · 唯一对外入口。

    外部 → **gateway** → order ─┬→ inventory ─→ payment
                                 └→ payment

对外端点：

    POST /api/orders              下单（query: sku, qty）
    GET  /api/orders/{order_id}   查询订单

★ gateway 与其他三个服务的**结构性差异**：它**没有数据库**。

  这不是省事，而是刻意的：gateway 是纯转发层。
  所以它不建连接池，配置里也就没有数据库 DSN（见 `observed.biz.settings`）。

★ gateway 还是整条链路上**唯一同时看得见"自己"和"order"的位置**：

  它把 order 的原始响应原样放进 `upstream.gateway_to_order`，
  于是"逐跳证据"在**一次响应**里就是完整的：

      {order 的业务结果} + upstream{order 的原始响应（内含 inventory / payment）}

  少了这一层，只拿到最终结果的调用方就无法判断
  "慢/错在网关到 order 之间"还是"在 order 内部"。

★ 本文件里**没有任何采集逻辑**：

  响应体里不会出现服务端自报耗时、不会出现服务名 —— 那两个字段由 SDK 的
  响应增强中间件在 ASGI 层自动补。业务只 `return` 普通 dict。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from observed.biz import api_contract
from observed.biz.app import create_biz_app
from observed.biz.downstream import DownstreamClient
from observed.biz.settings import load_settings

SERVICE = "gateway"

settings = load_settings(SERVICE)
downstream = DownstreamClient(SERVICE, settings.downstream)

app = create_biz_app(settings, downstream=downstream)

router = APIRouter()

# 本服务调用的下游路径（从契约表取，杜绝手写路径/方法）
_ORDER_CREATE = api_contract.path_for("order", "/orders")
_ORDER_GET = api_contract.path_for("order", "/orders/{order_id}")


def _with_upstream(body: dict[str, Any]) -> dict[str, Any]:
    """把 order 的原始响应挂到 `upstream.gateway_to_order` 下。

    ★ 两个转发端点共用这一处，避免"顶层怎么挂"这件事出现两份写法后各自漂移。
      形状不变：先浅拷贝 order 的业务字段，再挂原始响应（**同一份对象**，不深拷贝）。
    """
    result = dict(body)
    result["upstream"] = {"gateway_to_order": body}
    return result


# ==================================================================
#  POST /api/orders
# ==================================================================
@router.post("/api/orders")
async def create_order(
    sku: str = Query(..., min_length=1, description="商品编码"),
    qty: int = Query(..., ge=1, le=1000, description="数量"),
) -> dict[str, Any]:
    """网关下单：转发到 order，并把 order 的原始响应一并带回。

    ★ 不做任何"包装"：order 怎么答就怎么带给调用方，
      只在顶层多挂一个 `upstream` 说明"这是谁答的"。
      多余的重写会让"逐跳证据"与实际发生的事脱节。
    """
    body = await downstream.call("order", _ORDER_CREATE, params={"sku": sku, "qty": qty})
    return _with_upstream(body)


# ==================================================================
#  GET /api/orders/{order_id}
# ==================================================================
@router.get("/api/orders/{order_id}")
async def get_order(order_id: str) -> dict[str, Any]:
    body = await downstream.call(
        "order", _ORDER_GET, path_params={"order_id": order_id}
    )
    return _with_upstream(body)


app.include_router(router)
