"""order 服务 · 下单主链路。

调用拓扑中的枢纽：

    gateway → **order** ─┬→ inventory ─→ payment
                          └→ payment

对外端点：

    POST /orders              下单（query: sku, qty）
    GET  /orders/{order_id}   查询订单 —— ★ 对照端点：**不访问数据库**
    GET  /pool-stats          连接池自省

★ 两个订单读端点的**刻意不对称**（这是设计，不是遗漏）：

  ``POST /orders``                借一条数据库连接 + 调下游 → **重路径**
  ``GET  /orders/{order_id}``     **只读进程内订单表，完全不碰数据库** → 对照端点

  对照端点提供的是**可区分性证据**：

  · 两个端点都慢 → 问题在 **order 自身**（进程级：GC/CPU/锁）
  · 只有 POST 慢、GET 快 → 问题在 **数据库或连接池**（GET 不经这两者）
  · 只有 POST 慢且下游也慢 → 问题在 **下游**（gap 分析进一步区分调用方/被调方）

  没有这个对照端点，`POST /orders` 变慢时无法排除"order 自己就是慢"，只能靠猜。

★ 为什么订单表在进程内存里而不是数据库表：

  ① 存储契约**只允许 3 张表**（change_events / audit_log /
     approval_requests）。服务自建业务表会破坏"恰好 3 张表"的契约验收。
  ② ``GET /orders/{order_id}`` 明确要求**不访问数据库** ——
     若订单存在库里，这个端点就必须查库，对照性立刻消失。

  两个约束叠加后，"订单状态在进程内存"是唯一自洽的实现。
  代价（服务重启后订单丢失）对本项目无影响：订单只是产生调用链的载体。

★ 本文件里**没有任何故障注入**：连接池的**上限 / 超时是正常配置项**
  （`DB_POOL_SIZE` / `DB_POOL_TIMEOUT_SEC`），不是"故障旋钮"。
  造"池饱和"改由**外部手段**（限制资源 / 拖慢数据库 / 提高并发）。
  这样业务代码里不留后门，故障也更接近真实生产。

★ 同样地，本文件里也没有任何采集逻辑：指标 / 日志字段 / 链路 / 服务端自报耗时
  全在 SDK。业务只 `return` 普通 dict。
"""

from __future__ import annotations

from datetime import datetime, timezone
from itertools import count
from typing import Any

from fastapi import APIRouter, Query

from observed.biz import api_contract
from observed.biz.app import create_biz_app
from observed.biz.downstream import DownstreamClient
from observed.biz.errors import ORDER_NOT_FOUND, STOCK_INSUFFICIENT, BizError
from observed.biz.pool import DriverPool, execute, ping  # ▸ AIOPS-SCENARIO slow_query
from observed.biz.settings import load_settings

SERVICE = "order"

settings = load_settings(SERVICE)

# ★ 可被 SDK 的池适配器替换的侵入点（会把真实池对象接过去）。
#   本阶段它是**朴素的**：管连接、给自省读数，但不上报任何指标。
pool = DriverPool.from_settings(settings)
# ★ 另一处侵入点：SDK 的客户端适配器会替换它
downstream = DownstreamClient(SERVICE, settings.downstream)

# ▸ AIOPS-SCENARIO pool_reject begin（dev-only：`.run/scenario_faults.py off pool_reject` 还原）
import asyncio as _scenario_asyncio
import queue as _scenario_queue
import threading as _scenario_threading
import time as _scenario_time

_SCENARIO_WORKERS = 2
_SCENARIO_QUEUE_MAX = 4
_SCENARIO_QUEUE: Any = _scenario_queue.Queue(maxsize=_SCENARIO_QUEUE_MAX)


def _scenario_worker() -> None:
    while True:
        job, box = _SCENARIO_QUEUE.get()
        try:
            box["value"] = job()
        except Exception as exc:  # noqa: BLE001 —— 交回调用方分类
            box["error"] = exc
        finally:
            _SCENARIO_QUEUE.task_done()


for _ in range(_SCENARIO_WORKERS):
    _scenario_threading.Thread(target=_scenario_worker, daemon=True).start()


async def _scenario_worker_pool() -> None:
    """有界工作线程池 + **AbortPolicy**：队列满就**拒绝并抛异常**（不排队、不静默丢）。

    ★ 为什么拒绝必须"输出异常"：任务悄悄进无界队列时，症状会变成"延迟越积越高"；
      真实生产的有界池在队列满时是**立刻失败**（Java 线程池的 AbortPolicy 语义）。
    """
    box: dict[str, Any] = {}

    def _job() -> None:
        _scenario_time.sleep(0.4)          # 阻塞型工作：占住 worker

    try:
        _SCENARIO_QUEUE.put_nowait((_job, box))
    except _scenario_queue.Full as exc:
        raise BizError(
            "THREAD_POOL_REJECTED",
            f"工作线程池已满（workers={_SCENARIO_WORKERS}，队列={_SCENARIO_QUEUE_MAX}）"
            f"→ 拒绝策略=AbortPolicy：本次任务被拒绝执行（未入队）",
            http_status=503) from exc
    while "value" not in box and "error" not in box:
        await _scenario_asyncio.sleep(0.005)
    if "error" in box:
        raise box["error"]
# ▸ AIOPS-SCENARIO pool_reject end

app = create_biz_app(settings, pool=pool, downstream=downstream)

router = APIRouter()

_INVENTORY_CHECK = api_contract.path_for("inventory", "/check")
_INVENTORY_RESERVE = api_contract.path_for("inventory", "/reserve")
_PAYMENT_PAY = api_contract.path_for("payment", "/pay")

# ------------------------------------------------------------------
#  进程内订单表
#
#  ★ 容量上限是刻意的：压测时无上限的 dict 会把内存吃光，
#    最后表现为"服务 OOM 重启"——那会把连接池问题伪装成内存问题，
#    归因被带偏。超出上限即淘汰最早的一批（FIFO）。
# ------------------------------------------------------------------
_MAX_ORDERS = 4096
_orders: dict[str, dict[str, Any]] = {}
_order_seq = count(1)

# 单价：纯业务取值，用来把 qty 换算成支付金额
_UNIT_PRICE = 19.9


def _new_order_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"ord_{stamp}_{next(_order_seq):05d}"


def _remember(order: dict[str, Any]) -> None:
    if len(_orders) >= _MAX_ORDERS:
        # dict 保持插入顺序，弹出最早的一批
        for key in list(_orders)[: _MAX_ORDERS // 8 or 1]:
            _orders.pop(key, None)
    _orders[order["order_id"]] = order


# ==================================================================
#  POST /orders —— 重路径：占连接池 + 调下游
# ==================================================================
@router.post("/orders")
async def create_order(
    sku: str = Query(..., min_length=1, description="商品编码"),
    qty: int = Query(..., ge=1, le=1000, description="数量"),
) -> dict[str, Any]:
    """下单。

    ★ 参数一律走 query string（便于换实现语言时保持契约不变）。
    ★ 下单链路是**真实的四跳**：order 借库 → inventory 查 → inventory 占 → payment 付。
      任何一跳失败都抛 :class:`BizError`，由应用统一转成带分类的 HTTP 响应。
    """
    # ---- ① 占用一个数据库连接（连接池压力在这里真实产生）----
    await execute(pool, "SELECT pg_sleep(1)")  # ▸ AIOPS-SCENARIO slow_query

    await _scenario_worker_pool()

    order_id = _new_order_id()

    # ---- ② 库存校验（GET /check）----
    checked = await downstream.call(
        "inventory", _INVENTORY_CHECK, params={"sku": sku, "qty": qty}
    )
    if not bool(checked.get("available", True)):
        raise BizError(
            STOCK_INSUFFICIENT,
            f"库存不足：sku={sku} qty={qty}（现有 {checked.get('on_hand')}）",
        )

    # ---- ③ 库存预占（POST /reserve）----
    reserved = await downstream.call(
        "inventory",
        _INVENTORY_RESERVE,
        params={"order_id": order_id, "sku": sku, "qty": qty},
    )

    # ---- ④ 支付（POST /pay）----
    paid = await downstream.call(
        "payment",
        _PAYMENT_PAY,
        params={"order_id": order_id, "amount": round(float(qty) * _UNIT_PRICE, 2)},
    )

    # ---- ⑤ 落订单（**进程内**，见模块文档的说明）----
    order = {
        "order_id": order_id,
        "sku": sku,
        "qty": qty,
        "status": "confirmed",
        "payment_id": paid.get("payment_id"),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    _remember(order)

    return {
        "order_id": order_id,
        "sku": sku,
        "qty": qty,
        "status": order["status"],
        "created_at": order["created_at"],
        # ★ 键名里含 "payment"，使调用链证据在响应体里**可见**
        #   （验收要求能证明 order→payment 真的发生了）
        "payment": {"payment_id": order["payment_id"], "target": "payment"},
        # 逐跳原始响应：inventory 两次 + payment 一次，全部原样带回
        "downstream": {
            "inventory_check": checked,
            "inventory_reserve": reserved,
            "payment": paid,
        },
    }


# ==================================================================
#  GET /orders/{order_id} —— 对照端点：**不访问数据库**
# ==================================================================
@router.get("/orders/{order_id}")
async def get_order(order_id: str) -> dict[str, Any]:
    """查询订单。

    ★ **对照端点**：只读进程内字典，不做任何 I/O。
      它的耗时是衡量"order 自身处理开销"的基线；
      ``POST /orders`` 与它的差值就是数据库 + 下游贡献的那部分延迟。
    """
    order = _orders.get(order_id)
    if order is None:
        raise BizError(
            ORDER_NOT_FOUND,
            f"订单 {order_id} 不存在（本服务的订单表是进程内的，重启后即清空）",
        )
    # 注意：这里**只做一次字典查找**，连日志都不额外打 ——
    # 对照端点的价值就在于"除了响应封装以外没有别的开销"。
    return dict(order)


# ==================================================================
#  GET /pool-stats —— 连接池自省
# ==================================================================
@router.get("/pool-stats")
async def pool_stats() -> dict[str, Any]:
    """连接池自省（业务端点，不是"指标"）。

    ★ ``waiting`` 必须是"**正在**等待连接的请求数"，空闲时必须为 0。
      实现口径见 :mod:`observed.biz.pool` ——
      拿队列长度当等待数是历史事故的根因，这里与队列长度无关。
    """
    return pool.stats()


app.include_router(router)
