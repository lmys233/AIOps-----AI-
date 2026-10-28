"""定义服务壳的共享常量、logger 与 LLM 调用记账（contextvar）。

★ 内容原样搬自原 `agent/server.py`（它曾有 1586 行）；
  **公开导入面一字未改**（`agent/server.py` 门面 + 本包 `__init__.py` 再导出）。
"""

from __future__ import annotations

from ..llm.gateway import LLMCallRecord
from typing import Final, Optional

import contextvars
import logging
import re


logger = logging.getLogger("agent.server")

AGENT_SERVICE_NAME: Final[str] = "agent"

DEFAULT_AGENT_PORT: Final[int] = 8000

_ENV_AGENT: Final[str] = "AIOPS_AGENT"          # 测试与 run.ps1 都用它当 base_url

#: 鉴权用的环境变量名：非回环绑定必须配它，否则拒绝启动。
_ENV_TOKEN: Final[str] = "AIOPS_AGENT_TOKEN"

#: 显式声明"本机不鉴权"的环境变量（未配置密钥时的唯一放行开关）
_ENV_OPEN: Final[str] = "AIOPS_ALLOW_UNAUTHENTICATED"

#: 共享状态的 Redis 地址（未配置 = 进程内实现）
_ENV_STATE_REDIS: Final[str] = "AIOPS_STATE_REDIS_URL"

#: 重复请求判定的**抖动窗口**基准（秒）：同一会话在这个间隔内的重复输入直接拒绝；
#: 每个 key 的实际窗口由 key 的哈希派生 ±20% 的确定性抖动（避免同一瞬间一起过期）。
#:
#: ★★ 为什么是**1 秒**而不是几十秒：这个窗口要挡的是**同一动作被提交了两次**
#:   （双击发送、客户端重试），而不是"用户过了一会儿又想问一遍" ——
#:   后者是用户自己的事，判重挡住它只会让人以为 Agent 卡住了。
_ENV_DUPLICATE_WINDOW: Final[str] = "AIOPS_DUPLICATE_WINDOW_SEC"
DUPLICATE_WINDOW_SEC: Final[float] = 1.0

#: 抖动的比例（±20%）
DUPLICATE_WINDOW_JITTER: Final[float] = 0.2

#: 上面那些开关的"真值"写法（大小写不敏感，含中文的"是"）
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on", "是"})

#: 单次请求体的上限（字节）：body 会被整份读进内存再解析，而告警条数上限是
#: **解析之后**才生效的 —— 于是超大 POST 能先造成内存压力，必须在读之前封顶。
MAX_REQUEST_BYTES: Final[int] = 262_144          # 256 KiB

#: SSE 心跳（每 15s 一次防代理超时）
SSE_HEARTBEAT_SEC: Final[float] = 15.0

#: 会话缓存上限（幂等表）。★ 本阶段是**进程内**缓存，接 Redis 后换成持久层。
SESSION_CACHE_MAX: Final[int] = 512

_TRACE_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-fA-F]{16,32}")

# ======================================================================
#  记账（Token 成本要能归因到"这次请求花了多少"）
# ======================================================================
_USAGE: contextvars.ContextVar[Optional[list[LLMCallRecord]]] = contextvars.ContextVar(
    "aiops_llm_usage", default=None
)

def _usage_sink(record: LLMCallRecord) -> None:
    """把一次模型调用记到**当前请求**的账上。

    ★ 用 `ContextVar` 而不是实例属性：同一进程并发处理多个请求时，
      实例属性会互相串味（记到别人头上），而 ContextVar 是按任务隔离的。
    """
    bucket = _USAGE.get()
    if bucket is not None:
        bucket.append(record)
