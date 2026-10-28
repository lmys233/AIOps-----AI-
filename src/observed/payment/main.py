"""payment 服务 · 支付。

    ... → payment

对外端点：

    POST /pay              支付（query: order_id, amount）
    GET  /refund-policy    退款政策 —— ★ 对照端点：**不访问数据库**

★ 为什么 `/refund-policy` 不查库：

    POST /pay            借数据库连接 → 重路径
    GET  /refund-policy  纯计算，无 I/O → 对照端点

  于是"payment 变慢"可以立刻区分成"数据库侧问题"还是"payment 进程自身问题"。

  再往前一步：inventory 的 ``GET /check`` 也是 GET，但**会**查库。
  两者都是 GET，唯一差别是"碰不碰库" —— 这是可区分性证据的另一种形态，
  用来证明"慢"不是由 HTTP 方法本身带来的。

★ 本服务**没有故障旋钮**：业务系统是正常态系统，业务代码里不留注入后门。
  对照实验改由**外部手段**制造（对进程限 CPU、注入网络延迟、停掉 Postgres），
  效果等价而更接近真实生产。

★ 本文件里没有任何采集逻辑：业务只 `return` 普通 dict / 抛 `BizError`。
"""

from __future__ import annotations

from datetime import datetime, timezone
from itertools import count
from typing import Any

from fastapi import APIRouter, Query

from observed.biz.app import create_biz_app
from observed.biz.pool import DriverPool, ping
from observed.biz.settings import load_settings

SERVICE = "payment"

settings = load_settings(SERVICE)

# ★ 可被 SDK 的池适配器替换的侵入点（与 order 的同名对象一样）
pool = DriverPool.from_settings(settings)

# ▸ AIOPS-SCENARIO deadlock begin（dev-only：`.run/scenario_faults.py off deadlock` 还原）
import asyncio as _scenario_asyncio

from observed.biz.errors import BizError as _ScenarioBizError


async def _scenario_take(conn: Any, key: int) -> None:
    await conn.fetchval("SELECT pg_advisory_lock($1)", key)


async def _scenario_deadlock(pool: DriverPool) -> None:
    """两个连接**各自**以相反顺序取同一对咨询锁 → Postgres 判定死锁（真的 deadlock detected）。

    ★★ 第二次加锁必须打在**同一个会话**上：环要形成，两边各自的"第二个请求"必须
      正卡在服务端（客户端排队不算环）。实测反例：把第二个请求发到**另一个**连接上，
      两个会话的 (会话, 键) 配对就完全一样了 —— 咨询锁同会话可重入 → 两个锁都到手、没有死锁。
    ★ 用咨询锁而不是业务表：这个故障**不需要动 schema**（存储契约仍只有那 3 张表）。
    ★ 死锁必须**分类上报**（`DB_DEADLOCK`），不能落成 500 INTERNAL ——
      否则日志聚类会把"死锁"和"代码炸了"混成一类。
    """
    async with pool.acquire() as left, pool.acquire() as right:
        _pid_left = await left.fetchval("SELECT pg_backend_pid()")
        _pid_right = await right.fetchval("SELECT pg_backend_pid()")
        print(f"[scenario] deadlock conns: left={_pid_left} right={_pid_right} "
              f"same={_pid_left == _pid_right}", flush=True)

        async def _forward() -> None:
            await _scenario_take(left, 7001)
            await _scenario_asyncio.sleep(0.3)
            await _scenario_take(left, 7002)      # ★ 还是 left：让请求卡在服务端

        async def _backward() -> None:
            await _scenario_take(right, 7002)
            await _scenario_asyncio.sleep(0.3)
            await _scenario_take(right, 7001)     # ★ 还是 right

        try:
            await _scenario_asyncio.gather(_forward(), _backward())
            print("[scenario] deadlock: 两个锁都拿到了 —— 没构成环（场景失效）", flush=True)
        except Exception as exc:  # noqa: BLE001 —— 统一分类后抛出
            raise _ScenarioBizError(
                "DB_DEADLOCK",
                f"数据库事务死锁（{type(exc).__name__}: {exc}）",
                http_status=503) from exc
        finally:
            for _conn in (left, right):
                try:
                    await _conn.fetchval("SELECT pg_advisory_unlock_all()")
                except Exception:  # noqa: BLE001 —— 连接已坏时的释放失败不该盖住死锁本身
                    pass
# ▸ AIOPS-SCENARIO deadlock end

app = create_biz_app(settings, pool=pool)

router = APIRouter()

_payment_seq = count(1)

# 退款政策按金额档位给出（纯计算，无外部依赖）
_FULL_REFUND_WINDOW_DAYS = 7
_PARTIAL_REFUND_WINDOW_DAYS = 30


def _new_payment_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"pay_{stamp}_{next(_payment_seq):05d}"


# ==================================================================
#  POST /pay —— 重路径
# ==================================================================
@router.post("/pay")
async def pay(
    order_id: str = Query(..., min_length=1, description="订单号"),
    amount: float = Query(..., ge=0.0, description="金额"),
) -> dict[str, Any]:
    """支付。

    ★ 注意：本端点声明为 **POST**。真实事故就是
      "调用方用 GET 调这个 POST 端点 → 405 → 被包装成 502 → 误判下游宕机"。
      所以方法**只能**从契约表取（`observed.biz.api_contract`），不允许手写。
    """
    # 真实的数据库往返（占连接池）—— payment 的"重"就重在这一步
    await _scenario_deadlock(pool)

    payment_id = _new_payment_id()
    payment = {
        "payment_id": payment_id,
        "order_id": order_id,
        "amount": round(float(amount), 2),
        "status": "succeeded",
        "paid_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }

    return {
        "payment_id": payment_id,
        "order_id": order_id,
        "amount": payment["amount"],
        "status": payment["status"],
        "paid_at": payment["paid_at"],
    }


# ==================================================================
#  GET /refund-policy —— 对照端点：**不访问数据库**
# ==================================================================
@router.get("/refund-policy")
async def refund_policy(
    order_id: str = Query("", description="订单号（可选，用于定位政策档位）"),
) -> dict[str, Any]:
    """退款政策查询。

    ★ **对照端点**：整条路径没有任何 I/O —— 不查库、不调下游。
      它与 ``POST /pay`` 的耗时差，就是"数据库往返"贡献的那部分延迟。

      验收会直接调用它，并在**停掉 Postgres 之后**再调一次，
      确认它依然返回 200（这才证明它真的没碰数据库）。
    """
    return {
        "policy_version": "2026.01",
        "full_refund_within_days": _FULL_REFUND_WINDOW_DAYS,
        "partial_refund_within_days": _PARTIAL_REFUND_WINDOW_DAYS,
        "requires_original_payment": True,
        "applies_to_order": order_id or None,
    }


app.include_router(router)
