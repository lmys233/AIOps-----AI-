"""inventory 服务 · 库存。

    order → **inventory** → payment

对外端点：

    GET  /check     库存查询（query: sku, qty）    —— **会**访问数据库
    POST /reserve   库存预占（query: order_id, sku, qty）

★ 本服务存在的意义，不只是"多一个微服务"：

  它让 order→payment 之间插入了一个**可独立变慢的中间节点**。
  这样三跳链路（gateway→order→inventory→payment）才能产生
  "哪一跳慢"的归因题目 —— 两跳链路的归因太容易蒙对，
  无法证明 Agent 真的在做逐跳 gap 分析。

★ `/check` 与 payment 的 `/refund-policy` 构成一组**方法相同、行为不同**的对照：

    inventory GET /check           GET 且**查库**  → 重
    payment   GET /refund-policy   GET 且不查库    → 轻

  两者都慢 ⇒ 排除 HTTP 方法本身的开销；只有 /check 慢 ⇒ 问题在数据库侧。

★ 库存状态放在进程内存（理由同 order）：

  存储契约只允许 3 张表，服务自建业务表会破坏契约。
  本服务的数据库使用方式是"每次业务请求借一条连接做一次真实往返"，
  目的是产生**真实的连接池压力**，而不是持久化库存。

★ 本文件里没有任何采集逻辑：业务只 `return` 普通 dict / 抛 `BizError`。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query

from observed.biz.app import create_biz_app
from observed.biz.errors import STOCK_INSUFFICIENT, BizError
from observed.biz.pool import DriverPool, execute, ping
from observed.biz.settings import load_settings

SERVICE = "inventory"

settings = load_settings(SERVICE)

# ★ 可被 SDK 的池适配器替换的侵入点（会把真实池对象接过去）
pool = DriverPool.from_settings(settings)

app = create_biz_app(settings, pool=pool)

router = APIRouter()

# 每个 SKU 的初始库存。取值刻意留足余量，保证正常流量下
# "库存不足"这条业务分支不会被误触发（否则会把业务拒绝误报成系统故障）。
_INITIAL_STOCK = 100_000

_stock: dict[str, int] = {}


def _stock_of(sku: str) -> int:
    return _stock.setdefault(sku, _INITIAL_STOCK)


# ==================================================================
#  GET /check —— 库存查询（查库）
# ==================================================================
@router.get("/check")
async def check(
    sku: str = Query(..., min_length=1, description="商品编码"),
    qty: int = Query(..., ge=1, le=1_000_000, description="数量"),
) -> dict[str, Any]:
    """库存查询 —— 每次请求都做一次**真实的数据库往返**。

    ★ 这个 GET 是"重路径"，与 payment 的 `/refund-policy`（不查库的 GET）配对，
      提供"慢在数据库还是慢在进程"的可区分性证据。
    """
    await execute(pool, "SELECT 1 FROM inventory_stock WHERE sku = 'SKU-A'")

    on_hand = _stock_of(sku)
    return {
        "sku": sku,
        "requested_qty": qty,
        "on_hand": on_hand,
        "available": on_hand >= qty,
    }


# ==================================================================
#  POST /reserve —— 库存预占
# ==================================================================
@router.post("/reserve")
async def reserve(
    order_id: str = Query(..., min_length=1, description="订单号"),
    sku: str = Query(..., min_length=1, description="商品编码"),
    qty: int = Query(..., ge=1, le=1_000_000, description="数量"),
) -> dict[str, Any]:
    """库存预占。"""
    await ping(pool)

    on_hand = _stock_of(sku)
    if on_hand < qty:
        raise BizError(
            STOCK_INSUFFICIENT,
            f"库存不足：sku={sku} 需要 {qty}，现有 {on_hand}",
        )

    _stock[sku] = on_hand - qty
    return {
        "order_id": order_id,
        "sku": sku,
        "reserved_qty": qty,
        "on_hand_after": _stock[sku],
        "reserved": True,
        "reserved_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }


app.include_router(router)
