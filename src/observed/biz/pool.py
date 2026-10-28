"""业务侧的数据库连接池。

★ 关键决定：**用驱动自带的标准池**（`asyncpg.create_pool`），
  而不是自己拿 `asyncio.Semaphore` + 连接列表手搓一个。

  这不是省事，而是**零代码改动接入 SDK 的前提**：

  | | 手写池 | 标准驱动池（本文件） |
  |---|---|---|
  | SDK 能否零改动采到 `db_pool_*` | ❌ 它不是任何驱动工厂的产物，SDK 无从挂载 | ✅ SDK 猴补 `asyncpg.create_pool` 即可拿到真实池对象 |
  | 像不像"干净的业务后端" | ❌ 是特例 | ✅ 真实后端就是这么写的 |

  所以本文件是**普通业务代码**：它只管借还连接，**不上报任何指标**，
  也**不知道 SDK 存在**。可观测性由 SDK 在自己的适配器里完成。

★ 唯一保留的"自己算"的东西：`waiting`（正在等待连接的请求数）

  `asyncpg` 的池**不暴露等待者数量**，而 `/pool-stats` 与
  `db_pool_waiting_connections` 都需要它。

  ★ 口径：**只统计"未完成的等待者"** —— 开始等 +1、拿到或超时 -1，
    **与任何队列长度无关**。
    拿 `LifoQueue.qsize()` 当等待数，会把已完成的 future 也数进去，
    空闲时报出虚高值 → **永久误报池饱和**（这是本项目真实踩过的坑）。

  `waiting` 是**业务的统计量**（`/pool-stats` 这个业务端点要读它），
  而"把它变成 Prometheus 指标"是 SDK 的事 —— 计数 ≠ 上报。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional

import asyncpg

from .errors import (
    POOL_ACQUIRE_TIMEOUT,
    POOL_INIT_FAILED,
    POOL_QUERY_FAILED,
    BizError,
)

if TYPE_CHECKING:  # 仅用于类型标注：运行期不 import settings，避免多余耦合
    from .settings import BizSettings

__all__ = ["DriverPool", "execute", "ping"]

# 建连超时：独立于"获取连接超时"。前者是"连不上数据库"，后者是"池里没空位"。
_CONNECT_TIMEOUT_SEC = 5.0

# 池内保活的连接数
_MIN_SIZE = 2


class DriverPool:
    """`asyncpg.create_pool` 的薄封装：借还连接 + `/pool-stats` 自省。"""

    def __init__(self, dsn: str, *, max_size: int, acquire_timeout_sec: float,
                 min_size: int = _MIN_SIZE) -> None:
        if max_size <= 0:
            raise ValueError(f"pool max_size 必须为正数，实际 {max_size}")
        self._dsn = dsn
        self._max = int(max_size)
        self._timeout = float(acquire_timeout_sec)
        self._min = max(1, min(int(min_size), self._max))

        self._pool: Optional[asyncpg.Pool] = None
        self._lock = asyncio.Lock()
        self._active = 0            # 持有者 + 等待者
        self._waiting = 0           # ★ 只数"未完成"的等待者

    @classmethod
    def from_settings(cls, settings: "BizSettings") -> "DriverPool":
        """按配置造池 —— **唯一**一份"DSN / 池上限 / 获取超时"的取值口径。

        三个有库的服务（order / payment / inventory）都从这里建池，
        参数语义与各自原先的字面写法完全一致：
        DSN 取 ``database_url``，池上限取 ``db_pool_size``，
        获取超时取 ``db_pool_timeout_sec``（``min_size`` 保持默认）。
        """
        return cls(
            settings.database_url,
            max_size=settings.db_pool_size,
            acquire_timeout_sec=settings.db_pool_timeout_sec,
        )

    # ------------------------------------------------------------------
    #  生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """建立连接池。

        ★ 建池失败**不抛出**：服务仍应启动成功，把故障暴露在业务错误与
          `/ready` 上，而不是变成一个起不来的进程
          （否则"下游抖动导致进程被误杀"就会发生）。
        """
        try:
            self._pool = await asyncpg.create_pool(
                self._dsn,
                min_size=self._min,
                max_size=self._max,
                timeout=_CONNECT_TIMEOUT_SEC,
            )
        except Exception:  # noqa: BLE001 —— 启动期不抛，运行期由 acquire 分类上报
            self._pool = None

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            try:
                await pool.close()
            except Exception:  # noqa: BLE001 —— 关闭失败不阻断退出
                pass

    # ------------------------------------------------------------------
    #  借用 / 归还
    # ------------------------------------------------------------------
    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        pool = self._pool
        if pool is None:
            raise BizError(POOL_INIT_FAILED, "连接池尚未建立（可能数据库不可达）")

        # ---- 等待者计数：开始等 +1 / 拿到或超时 -1 ----
        async with self._lock:
            self._active += 1
            was_waiting = self._active > self._max
            if was_waiting:
                self._waiting += 1

        try:
            # timeout 交给驱动：它才是真正知道"什么时候算等太久"的一方
            conn = await pool.acquire(timeout=self._timeout)
        except TimeoutError as exc:
            async with self._lock:
                self._active -= 1
                if was_waiting:
                    self._waiting -= 1
            raise BizError(
                POOL_ACQUIRE_TIMEOUT,
                f"等待数据库连接超过 {self._timeout}s"
                f"（池上限 {self._max}，当前占用 {self.stats()['connections_in_use']}）",
            ) from exc
        except Exception as exc:  # noqa: BLE001 —— 统一分类后抛出
            async with self._lock:
                self._active -= 1
                if was_waiting:
                    self._waiting -= 1
            raise BizError(
                POOL_INIT_FAILED,
                f"从连接池取连接失败（{type(exc).__name__}: {exc}）",
            ) from exc

        async with self._lock:
            if was_waiting:
                self._waiting -= 1

        try:
            yield conn
        finally:
            async with self._lock:
                if self._active > 0:
                    self._active -= 1
            await pool.release(conn)

    # ------------------------------------------------------------------
    #  自省（供 `/pool-stats` 这个**业务端点**读取）
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        pool = self._pool
        if pool is None:
            total = idle = 0
        else:
            total = int(pool.get_size())
            idle = int(pool.get_idle_size())

        return {
            "pool_max": self._max,
            # ★ `in_use + idle == total` 由算式保证，不靠维护三个独立计数器
            "connections_total": total,
            "connections_idle": idle,
            "connections_in_use": total - idle,
            "waiting": self._waiting,
            # 配置类只读字段（验收会核对它们两次读取是否一致）
            "pool_size_configured": self._max,
            "acquire_timeout_sec": self._timeout,
            "acquire_hold_ms": 0,
            "available": pool is not None,
        }


async def execute(pool: DriverPool, sql: str, *args: Any) -> Any:
    """借一条连接执行一次查询，并把底层异常收敛成带 `error_type` 的受控失败。"""
    async with pool.acquire() as conn:
        try:
            return await conn.fetchval(sql, *args)
        except BizError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 统一分类后抛出
            raise BizError(
                POOL_QUERY_FAILED,
                f"数据库查询失败（{type(exc).__name__}: {exc}）",
            ) from exc


async def ping(pool: DriverPool) -> Any:
    """最轻的一次真实数据库往返：借连接 → 执行 → 归还。

    "重路径"端点用它占住连接池（池压力就是这样真实产生的），
    探活也用它。语句只有一份，口径不会在多处漂移。
    """
    return await execute(pool, "SELECT 1")
