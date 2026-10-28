"""限流（工具调用链路上的**第 4 道保护**）。

★ 为什么限流放在"权限"之后、"执行"之前，而不是放在数据源侧：
  数据源的限流（Prometheus 自己的 `--query.max-samples`）只会**拒绝**，
  而我们要的是"**别把自己打进限流**" —— 一个跑飞的 Agent 在 10 秒里发 200 个查询，
  受害的不只是它自己：被观测系统的指标接口被打慢，**采集与告警一起受影响**。
  这也正是"Agent 自己成了故障源"的典型形态。

★ 键是 `(agent_role, tool)` 而不是 `tool`：
  并发时 Log Agent 的突发不该把 Metrics Agent 饿死 ——
  否则"两路证据独立采集"在资源紧张时会退化成"谁先跑谁独占"。

★ 实现成**令牌桶**而不是计数器（`n 次/分钟`）：
  计数器会在窗口边界上允许 2n 次突发（第 59 秒 n 次 + 第 61 秒 n 次），
  而令牌桶把突发**平滑**掉，且天然支持"桶容量=允许的瞬时突发"。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Final, Optional

__all__ = ["RateLimiter", "parse_rate_limit", "RateLimitDecision"]

_RATE_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(\d+)\s*/\s*(s|sec|m|min|h|hour)\s*$")
_PERIOD_SEC: Final[dict[str, int]] = {
    "s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hour": 3600,
}


class RateLimitConfigError(ValueError):
    """`rate_limit` 写法非法（格式是 `"60/min"`）。"""


def parse_rate_limit(raw: str) -> tuple[float, float]:
    """`"60/min"` → `(容量, 每秒补充速率)`。

    ★ 桶容量取 `n`（每分钟 n 次 ≈ 每秒 n/60），所以瞬时突发上限就是 n ——
      与"每分钟最多 n 次"的字面语义一致（不会因为桶满而允许超过 n 的突发）。
    """
    matched = _RATE_RE.match(str(raw or ""))
    if not matched:
        raise RateLimitConfigError(
            f"rate_limit={raw!r} 写法非法 —— 应形如 '60/min'"
        )
    count, unit = int(matched.group(1)), matched.group(2)
    if count <= 0:
        raise RateLimitConfigError(f"rate_limit={raw!r} 的额度必须为正")
    period = _PERIOD_SEC[unit]
    return float(count), count / period


@dataclass
class _Bucket:
    tokens: float
    updated_at: float
    #: 建桶时记下的补充速率（每秒令牌数）—— `available_in()` 要靠它把
    #: "还差几个令牌"换算成**秒**（以前名字叫"还要等多久"，
    #: 返回的却是令牌数，于是调用方拿它去 sleep 会睡错量级）。
    refill_per_sec: float = 0.0


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_sec: float = 0.0
    reason: str = ""


@dataclass
class RateLimiter:
    """按 `(agent_role, tool)` 的令牌桶。

    ★ `clock` 可注入（离线单测不需要 `sleep` —— 那会让测试变慢且不稳定）。
    """

    clock: Callable[[], float] = time.monotonic
    _buckets: dict[tuple[str, str], _Bucket] = field(default_factory=dict, repr=False)

    def check(self, agent_role: str, tool: str, rate_limit: str) -> RateLimitDecision:
        capacity, refill_per_sec = parse_rate_limit(rate_limit)
        now = self.clock()
        key = (agent_role, tool)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=capacity, updated_at=now,
                             refill_per_sec=refill_per_sec)
            self._buckets[key] = bucket
        else:
            # 配置改了（例如 60/min → 120/min）也要生效：速率每次刷新，
            # 但**不重置**已经扣掉的令牌（否则改一次配置就等于白送一次配额）
            bucket.refill_per_sec = refill_per_sec

        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(capacity, bucket.tokens + elapsed * refill_per_sec)
        bucket.updated_at = now

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return RateLimitDecision(allowed=True)

        need = 1.0 - bucket.tokens
        retry_after = need / refill_per_sec if refill_per_sec > 0 else float("inf")
        return RateLimitDecision(
            allowed=False,
            retry_after_sec=round(retry_after, 3),
            reason=(f"{agent_role} 调用 {tool} 超过限额 {rate_limit}"
                    f"（指数退避重试，最多 3 次）"),
        )

    def reset(self) -> None:
        """清空所有桶（测试与"新的一轮排查开始"时用）。"""
        self._buckets.clear()

    def snapshot(self) -> dict[str, float]:
        """当前各键剩余令牌（便于排障与单测断言）。"""
        return {f"{role}:{tool}": round(bucket.tokens, 3)
                for (role, tool), bucket in self._buckets.items()}

    def available_in(self, agent_role: str, tool: str) -> Optional[float]:
        """该键下一次可用还要**等多少秒**（没建过桶 → `None`；已经够 1 个令牌 → `0.0`）。

        ★ 单位是**秒**（以前返回的是令牌数，调用方会睡错量级）：换算用建桶时记下的补充速率。
        """
        bucket = self._buckets.get((agent_role, tool))
        if bucket is None:
            return None
        need = max(0.0, 1.0 - bucket.tokens)
        if need == 0.0:
            return 0.0
        if bucket.refill_per_sec <= 0:
            return float("inf")
        return round(need / bucket.refill_per_sec, 3)
