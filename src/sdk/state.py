"""SDK 进程内的**运行期状态**。

★ 为什么需要它：

  ASGI 包装器、指标中间件、日志 formatter、猴补的适配器 ——
  这些部件分布在不同的模块里，但它们共享同一批事实：
  "我是哪个服务"、"有哪些池"、"哪些告警已经打过"。

  用**一个显式对象**承载这些事实，而不是靠模块级全局变量各存一份：

  | 好处 | 说明 |
  |---|---|
  | 不串味 | 同一进程构造两个 app（进程内验收就是 4 个），各持一个 Runtime |
  | 可测 | 测试可以直接造一个 Runtime，不需要起进程 |
  | 可自检 | `verify()` 就是来读这个对象"到底挂上了什么" |

★ `warn_once` 是"绝不静默"的落点：

  "可观测路径失败必须**告警一次**" —— 一次，不是每次请求一次，
  否则日志会被刷屏，真正的信号反而被淹掉。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .config import SdkConfig

__all__ = ["Runtime", "get_runtime", "set_runtime", "warn_once", "safe_record"]

logger = logging.getLogger("sdk")


def _warn_once(registry: set[str], key: str, message: str,
               emit: Callable[[str], None]) -> None:
    """「同一个 key 只告警一次」的**唯一实现**（绝不静默，也不刷屏）。

    ★ `registry` 由调用方自带 —— 这不是两套实现，而是同一套去重逻辑用在两处：
      `Runtime` 带自己那份（同进程多 app 时互不串味），
      模块级的 `_FALLBACK_WARNED` 是 Runtime 还没建起来时的唯一落点。
      没有它，"启动早期的失败"就没有地方可报。
    """
    if key in registry:
        return
    registry.add(key)
    emit(message)


# Runtime 还没建起来时的兜底注册表（例如日志层接管期间的失败）
_FALLBACK_WARNED: set[str] = set()


def warn_once(key: str, message: str) -> None:
    """按「当前 Runtime 优先、否则进程级兜底」告警一次。

    ★ 为什么必须去重：日志文件打不开这类失败是"持续状态"，
      每个请求都吵一次会把日志刷满 —— 那样真正的信号反而看不见了。
    """
    runtime = get_runtime()
    if runtime is not None:
        runtime.warn_once(key, message)
        return
    _warn_once(_FALLBACK_WARNED, key, message, lambda text: logger.warning(text))


@dataclass
class Runtime:
    """一个被 SDK 包装的服务的运行期状态。"""

    service_name: str
    config: Optional[SdkConfig] = None

    # 已注册的连接池包装器（池适配器往里塞）
    pools: list[Any] = field(default_factory=list)
    # 已生效的猴补补丁说明（各层记录，自检的 verify() 读它）
    patches: dict[str, str] = field(default_factory=dict)
    # 运行期计数（给 verify() 回答"到底采到什么程度"用；不是 Prometheus 指标）
    counters: dict[str, int] = field(default_factory=dict)

    _warned: set[str] = field(default_factory=set)

    def __post_init__(self):
        from .diagnostic_state import DiagnosticState
        self.diagnostics = DiagnosticState(self)

    def bump(self, name: str, amount: int = 1) -> int:
        """累加一个运行期计数并返回累计值。"""
        self.counters[name] = self.counters.get(name, 0) + amount
        return self.counters[name]

    def set_counter(self, name: str, value: int) -> int:
        """把一个计数**置为**给定值（"最后一次"语义；`bump` 是累加）。"""
        self.counters[name] = value
        return value

    # ------------------------------------------------------------------
    #  告警一次
    # ------------------------------------------------------------------
    def warn_once(self, key: str, message: str) -> None:
        """同一个 key 只告警一次。**不是不告警**。"""
        _warn_once(self._warned, key, message,
                   lambda text: logger.warning("[%s] %s", self.service_name, text))

    # ------------------------------------------------------------------
    #  连接池
    # ------------------------------------------------------------------
    def register_pool(self, pool: Any) -> None:
        self.pools.append(pool)

    @property
    def pool(self) -> Optional[Any]:
        return self.pools[0] if self.pools else None

    def note_patch(self, name: str, detail: str) -> None:
        """记录一个已生效的猴补。自检靠它回答"到底挂上了没有"。"""
        self.patches[name] = detail


# ----------------------------------------------------------------------
#  当前 Runtime
#
#  ★ 用"当前"而不是"全局唯一"：猴补发生在库层面（httpx / asyncpg），
#    那些补丁只能拿到"这个进程当前在服务的 Runtime" —— 这是进程内
#    多 app 场景的已知边界，写在这里以免读者误以为它能自动分流。
#    ★ 补丁在**每次调用时**读这个指针（见 `adapters/`），所以运行期替换 Runtime
#      立刻生效；但同进程的两个 app 仍然无法各记各的 —— 那是这条边界的本质。
# ----------------------------------------------------------------------
_current: Optional[Runtime] = None


def set_runtime(runtime: Runtime) -> None:
    global _current
    _current = runtime
    # ★ 把"Runtime 建起来之前"已经报过的 key 交给它：否则同一个 key 先走进程级
    #   兜底、再走 Runtime 就会报**两次**，而"只吵一次"是硬约定（见 `_warn_once`）。
    runtime._warned |= _FALLBACK_WARNED


def get_runtime() -> Optional[Runtime]:
    return _current


def safe_record(runtime: Optional[Runtime], key: str, what: str,
                fn: Callable[[], Any]) -> Any:
    """执行一段"记录指标"的代码，**绝不让它的异常影响业务**。

    ★ 这是本 SDK 最重要的一条护栏，来自一次真实事故：

      第一个版本的适配器里有个 `NameError`（少 import 一个常量），
      本该只在"记录指标"时触发 —— 结果它从 `httpx.send` 冒泡出去，
      **把 20 次下单全部变成了 500**。

      对"零代码改动接入"的 SDK 来说，这是最坏的一类缺陷：
      业务方什么都没改，却因为装上 SDK 而挂了。可观测性代码的地位是
      **旁路**，它挂掉最多是"数据少了"，绝不能是"业务挂了"。

    ★ 但"不让它冒泡"不等于"静默"：

      失败会 `warn_once` 报出来 —— 一次，不刷屏。
      "采集悄悄不工作"比"采集报错"难排查得多。

    返回 `fn()` 的返回值；失败时返回 None。
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 —— 这就是护栏本身，必须兜住一切
        if runtime is not None:
            runtime.warn_once(
                f"record_failed:{key}",
                f"记录失败（{what}）：{type(exc).__name__}: {exc} —— "
                f"已跳过本次记录，**业务不受影响**",
            )
        return None
