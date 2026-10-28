"""`asyncpg` 池适配器：猴补工厂，透明代理池并记录 `db_pool_*` 四条。

★ 猴补的对象是**工厂函数** `asyncpg.create_pool`，不是业务对象。

  业务代码里写的是：

      pool = await asyncpg.create_pool(dsn, min_size=..., max_size=...)

  SDK 把 `asyncpg.create_pool` 换成一个"先调原函数、再把结果包一层"的版本，
  于是业务拿到的是**我们的包装器**，而它自己一行都不用改。

  这样做的意义：`in_use` / `waiting` / `acquire_seconds` 这些量
  **只有池的实现者知道** —— 不接管对象就只能靠猜。
  （这正是 OTel / Datadog 那一类 APM 的通用手法。）

★ 等待者计数的口径（本项目真实踩过的坑）

      开始等 +1 …… 拿到或超时 -1

  **与任何队列长度无关**。以前拿 `LifoQueue.qsize()` 当等待数，
  而 `qsize()` 统计的是队列里的全部元素，**包括 future 已经完成的那些** ——
  于是空闲态也报出虚高值，`waiting > 0` 类告警**永久误报池饱和**。

★ 包装器必须**尽量透明**：

  业务的 `/pool-stats` 会调 `get_size()` / `get_idle_size()`；
  业务还可能直接 `pool.fetchval(...)`。所以包装器用 `__getattr__` 转发
  一切未显式覆盖的属性/方法，只覆盖需要我们计数的几个。

  "透明"不是可选项：一旦业务发现池的行为变了，这个 SDK 就没法叫"零改动"了。
"""

from __future__ import annotations

from time import perf_counter
from typing import Any, Optional

from ..definitions import POOL_ACQUIRE, POOL_MAX, record_pool_state
from ..state import Runtime, get_runtime, safe_record

__all__ = ["patch", "PoolProxy", "PATCH_NAME"]

PATCH_NAME = "pool:asyncpg"


class _AcquireContext:
    """`pool.acquire()` 的返回值：既能 `await`，也能 `async with`（与 asyncpg 一致）。"""

    def __init__(self, proxy: "PoolProxy", ctx: Any, started: float,
                 was_waiting: bool) -> None:
        self._proxy = proxy
        self._ctx = ctx
        self._started = started
        self._was_waiting = was_waiting
        self._conn: Any = None

    # -- async with 路径 --
    async def __aenter__(self) -> Any:
        try:self._conn = await self._borrow(True)
        except BaseException as error:
            safe_record(self._proxy._runtime,'pool_failure','acquire failure',
                        lambda:self._proxy._acquire_failed(self._started,self._was_waiting,error))
            raise
        self._proxy._on_acquired(self._started, self._was_waiting)
        safe_record(self._proxy._runtime,'pool_hold','connection hold',lambda:self._proxy._hold(self._conn,self._started))
        return self._conn

    async def __aexit__(self, *exc_info: Any) -> Any:
        try:
            return await self._ctx.__aexit__(*exc_info)
        finally:
            safe_record(self._proxy._runtime,'pool_release','connection release',lambda:self._proxy._unhold(self._conn))

    # -- await 路径 --
    def __await__(self) -> Any:
        return self._await_impl().__await__()

    async def _await_impl(self) -> Any:
        try:self._conn = await self._borrow(False)
        except BaseException as error:
            safe_record(self._proxy._runtime,'pool_failure','acquire failure',
                        lambda:self._proxy._acquire_failed(self._started,self._was_waiting,error))
            raise
        self._proxy._on_acquired(self._started, self._was_waiting)
        safe_record(self._proxy._runtime,'pool_hold','connection hold',lambda:self._proxy._hold(self._conn,self._started))
        return self._conn

    async def _borrow(self,context):
        from ..diagnostic_stage import stage
        with stage(self._proxy._runtime,'pool_acquire',self._proxy.pool_id):
            return await self._ctx.__aenter__() if context else await self._ctx


class PoolProxy:
    """透明代理一个 asyncpg 池，顺便把 `db_pool_*` 四条记录下来。"""

    def __init__(self, pool: Any, runtime: Runtime) -> None:
        self._pool = pool
        self._runtime = runtime
        runtime.diagnostics.capability('sdk_pool','available',adapter='asyncpg')
        self._service = runtime.service_name
        from uuid import uuid4
        self.pool_id=uuid4().hex
        self._holders={}

        self._active = 0      # 持有者 + 等待者
        self._in_use = 0
        self._waiting = 0

        max_size = int(getattr(pool, "get_max_size", lambda: 0)() or 0)
        self._max = max_size
        POOL_MAX.labels(service=self._service).set(max_size)
        self._publish()

    # ------------------------------------------------------------------
    #  计数
    # ------------------------------------------------------------------
    def _publish(self) -> None:
        # ★ 护栏：Gauge 刷新失败绝不能影响借还连接
        safe_record(
            self._runtime, "pool_gauges", "池 Gauge 刷新",
            lambda: record_pool_state(
                self._service,
                total=self._total(),
                idle=self._idle(),
                waiting=self._waiting,
            ),
        )

    def _total(self) -> int:
        try:
            return int(self._pool.get_size())
        except Exception:  # noqa: BLE001 —— 池已关闭时不要把指标路径搞崩
            return 0

    def _idle(self) -> int:
        try:
            return int(self._pool.get_idle_size())
        except Exception:  # noqa: BLE001
            return 0

    def _on_acquired(self, started: float, was_waiting: bool) -> None:
        # 等待结束（拿到连接了）：等待者 -1，占用 +1
        if was_waiting and self._waiting > 0:
            self._waiting -= 1
        self._in_use += 1
        safe_record(
            self._runtime, "pool_acquire_hist", "获取连接耗时直方图",
            lambda: POOL_ACQUIRE.labels(service=self._service)
            .observe(perf_counter() - started),
        )
        self._publish()

    def _on_released(self) -> None:
        if self._in_use > 0:
            self._in_use -= 1
        self._publish()

    def _hold(self,conn,started):
        from ..diagnostic_state import current_correlation
        from .query_asyncpg import safe_binding
        item=safe_binding(getattr(conn,'_con',conn))
        correlation=current_correlation()
        if item is not None:correlation['session_id']=item['session_id']
        holder=dict(pool_id=self.pool_id,started=perf_counter(),correlation=correlation,
                    server_pid=conn.get_server_pid(),phase='held')
        if item is not None:item.update(holder=holder,pool_id=self.pool_id)
        self._holders[id(conn)]=holder
        self._runtime.diagnostics.holders[str(id(conn))]=holder
        self._runtime.diagnostics.emit('pool_acquire',dict(pool_id=self.pool_id,
            acquire_ms=(perf_counter()-started)*1000,capacity=self._max,in_use=self._in_use,
            waiting=self._waiting),'sdk_pool',correlation)

    def _unhold(self,conn):
        holder=self._holders.pop(id(conn),None)
        self._runtime.diagnostics.holders.pop(str(id(conn)),None)
        if holder is None:return
        self._active=max(0,self._active-1);self._on_released()
        self._runtime.diagnostics.emit('pool_release',dict(pool_id=self.pool_id,
            hold_ms=(perf_counter()-holder['started'])*1000,in_use=self._in_use,
            waiting=self._waiting),'sdk_pool',holder['correlation'])

    def _acquire_failed(self,started,was_waiting,error):
        self._active=max(0,self._active-1)
        if was_waiting:self._waiting=max(0,self._waiting-1)
        self._publish()
        self._runtime.diagnostics.emit('pool_timeout' if isinstance(error,TimeoutError) else 'pool_acquire_failed',dict(pool_id=self.pool_id,
            acquire_ms=(perf_counter()-started)*1000,capacity=self._max,
            in_use=self._in_use,waiting=self._waiting,exception_type=type(error).__name__),'sdk_pool')

    def _on_failed(self) -> None:
        """取连接失败/超时：把等待者减回去（**只减我们自己加的那个**）。"""
        if self._waiting > 0:
            self._waiting -= 1
        self._publish()

    # ------------------------------------------------------------------
    #  覆盖：借 / 还 / 关
    # ------------------------------------------------------------------
    def acquire(self, *, timeout: Optional[float] = None) -> _AcquireContext:
        # ★ 只在"真的要等"时才把等待者 +1。
        #   判据是"当前活跃数（持有者 + 等待者）已超过池上限" ——
        #   这与队列长度无关。
        #   显式记成 `was_waiting`：它同时被"拿到连接"和"取连接失败"两条
        #   出口用来决定要不要把计数减回去，避免出现"等过了但没减"的泄漏。
        self._active += 1
        was_waiting = self._active > self._max
        if was_waiting:
            self._waiting += 1
            self._publish()

        started = perf_counter()
        try:
            ctx = self._pool.acquire(timeout=timeout) if timeout is not None \
                else self._pool.acquire()
        except Exception:
            self._active -= 1
            if was_waiting:
                self._waiting -= 1
            self._publish()
            raise
        return _AcquireContext(self, ctx, started, was_waiting)

    async def release(self, conn: Any) -> None:
        try:
            await self._pool.release(conn)
        finally:
            safe_record(self._runtime,'pool_release','connection release',lambda:self._unhold(conn))

    async def close(self) -> None:
        try:
            await self._pool.close()
        finally:
            # 池关了：把所有 Gauge 归零，而不是留着最后一次的读数 ——
            # "已关闭"与"占用中"必须可区分
            self._in_use = 0
            self._waiting = 0
            self._active = 0
            self._publish()

    # ------------------------------------------------------------------
    #  `/ready` 用的检查（probes.check_pool 调它）
    # ------------------------------------------------------------------
    async def check(self) -> tuple[bool, dict[str, Any]]:
        from ..diagnostic_state import CURRENT
        token=CURRENT.set(dict(CURRENT.get(),purpose='health'))
        try:return await self._check()
        finally:CURRENT.reset(token)

    async def _check(self) -> tuple[bool, dict[str, Any]]:
        started = perf_counter()
        try:
            async with self.acquire() as conn:
                await conn.fetchval("SELECT 1")
        except Exception as exc:  # noqa: BLE001 —— 探针不得抛出，要给出明细
            return False, {
                "detail": f"{type(exc).__name__}: {exc}",
                "latency_ms": round((perf_counter() - started) * 1000, 3),
            }
        return True, {
            "detail": "SELECT 1 成功",
            "latency_ms": round((perf_counter() - started) * 1000, 3),
        }

    # ------------------------------------------------------------------
    #  透明转发
    # ------------------------------------------------------------------
    def __getattr__(self, name: str) -> Any:
        # 只对"包装器自己没有的属性"生效 —— 于是 get_size / fetchval /
        # is_closing 等全部原样落到真实池上
        if name in ('execute','executemany','fetch','fetchrow','fetchval'):
            async def call(*args,**kwargs):
                async with self.acquire() as conn:return await getattr(conn,name)(*args,**kwargs)
            return call
        return getattr(self._pool, name)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PoolProxy service={self._service} max={self._max}>"


# ----------------------------------------------------------------------
#  猴补
# ----------------------------------------------------------------------
def patch(runtime: Runtime) -> Optional[str]:
    """把 `asyncpg.create_pool` 换成包装版。返回补丁说明，失败返回 None。

    ★ 幂等：重复调用只生效一次（用模块级标记），避免多次 `activate()`
      把工厂函数套娃成好几层 —— 那会让每次指标记录重复计数。
    """
    try:
        import asyncpg  # noqa: PLC0415
    except ImportError:
        runtime.warn_once(
            "asyncpg_missing",
            "[pool].driver=asyncpg，但环境里没有 asyncpg —— db_pool_* 将以值 0 暴露",
        )
        return None

    if getattr(asyncpg.create_pool, "__aiops_patched__", False):
        return "asyncpg.create_pool 已是包装版（幂等跳过）"

    original = asyncpg.create_pool

    async def create_pool(*args: Any, **kwargs: Any) -> Any:
        pool = await original(*args, **kwargs)
        # ★ **调用时**取当前 Runtime：猴补挂在进程级的驱动工厂上，而
        #   `set_runtime` 是进程级单例 —— 把补丁装上的那个 Runtime 焊死在闭包里，
        #   会让同进程第二个 app 的池指标继续记到第一个上。取不到才退回它。
        active = get_runtime() or runtime
        proxy = PoolProxy(pool, active)
        active.register_pool(proxy)
        return proxy

    create_pool.__aiops_patched__ = True          # type: ignore[attr-defined]
    create_pool.__aiops_original__ = original     # type: ignore[attr-defined]
    create_pool.__doc__ = original.__doc__
    asyncpg.create_pool = create_pool             # type: ignore[assignment]

    detail = f"已接管 asyncpg.create_pool（{PATCH_NAME}）"
    runtime.note_patch(PATCH_NAME, detail)
    return detail
