"""编排层的共享常量与**身份**（拆文件时抽出来的"最小公共面"）。

★ 为什么单独一个文件：`orchestrator.py` 拆成 mixin 之后，`INCIDENT_ID_PREFIX` /
  `new_incident_id` / `CAN_PRODUCE_ROOT_CAUSE` / `EMPTY_SLOTS` / `logger` 这几样
  是**多个模块都要用**的。放在 `orchestrator.py` 里会形成
  "mixin → orchestrator → mixin" 的循环导入；放这里则两边都只依赖它。
★ 这些名字仍然从 `orchestration.orchestrator` 与 `orchestration` 包**照旧导出**
  （见 `orchestrator.py` 的再导出），所以外部导入面没有变化。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Final, Mapping

from ..common.enums import Intent
from ..intent.slots import ACTIVE_SLOTS

#: 事件回调：`(event, data)` —— **同步**即可（服务壳把它塞进 `asyncio.Queue` 供 SSE 读）。
EventSink = Callable[[str, dict[str, Any]], None]

logger = logging.getLogger("agent.orchestration")

INCIDENT_ID_PREFIX: Final[str] = "inc_"


def new_incident_id() -> str:
    return f"{INCIDENT_ID_PREFIX}{uuid.uuid4().hex}"


#: ★ 本阶段能不能产出**根因**（`concluded` 的前提条件）。
#:   根因裁决还没落地时置 `False`；裁决能力接上后置 `True`。
#:   语义与理由见 `orchestrator.py` 的同名再导出处。
CAN_PRODUCE_ROOT_CAUSE: Final[bool] = True

#: 构造空槽位用的兜底值（★ 与契约默认值一致，不发明新值）。
#: ★★ **默认时间窗只从 `intent/slots.yaml` 取一处** —— 这里以前硬编码 `"30m"`，
#:   与配置各写一份；改默认值时会漏改一处，于是"说一个窗口、查另一个窗口"。
EMPTY_SLOTS: Final[dict[str, Any]] = {
    "env": "prod",
    "time_window": str(ACTIVE_SLOTS.default_time_window),
}

#: **完整多 Agent 取证链路**的意图（5 路并行）。
#: ★ 注意它**不是**"要不要取证"的判据 —— 那个判据是下面的 `EVIDENCE_INTENTS`：
#:   只查现状的 `METRIC_QUERY` 也要取证，只是走**单 Agent 轻链路**。
#: ★ 用 `Intent` 枚举而不是字符串：`in` 判的是**枚举成员**，写成字符串会依赖
#:   `str` 型枚举的比较细节（能省一行 import，但把语义押在语言细节上 —— 不值）。
FORENSICS_INTENTS: Final[frozenset[Intent]] = frozenset({Intent.INCIDENT_RCA})

#: ★★ **单 Agent 轻链路**的意图（`METRIC_QUERY` 走"单 Agent 轻链路"，
#:   解锁 `promql_read`）。它**不判异常**，只把**现状**（当前值 + 窗口起点）如实报出来。
LIGHT_INTENTS: Final[frozenset[Intent]] = frozenset({Intent.METRIC_QUERY})

#: ★★ **处置轮**（用户问"怎么解决"）：要**冲着点名的那件事**取证一次，再出方案。
#:   它不进 `LIGHT_INTENTS`：轻链路的语义是"只查现状、不判异常"（`METRIC_QUERY` 专属），
#:   而处置轮既要知道现状、也要拿到该服务的错误证据。
REMEDIATION_INTENTS: Final[frozenset[Intent]] = frozenset({Intent.REMEDIATION})

#: ★★ **单轮取证**的意图（查一轮就收口，不做定向二次取证）。
#:   两族合起来才是"只跑一轮"：现状查询本来就不判异常，处置轮的目的是**出方案**
#:   （多跑几轮只会把同一批数字再查一遍）；★ 与 `EVIDENCE_INTENTS` 分开写，
#:   因为"要不要取证"与"取证跑几轮"是两个问题。
SINGLE_ROUND_INTENTS: Final[frozenset[Intent]] = LIGHT_INTENTS | REMEDIATION_INTENTS

#: 各意图**只叫哪几个角色**（从已有的 5 个角色里选，**不新增角色**）。
#: ★ 为什么在编排层定而不是让 Agent 自己声明：这是"这次查几路"的**编排决策**，
#:   与"某个 Agent 能查什么"是两件事（后者在 `AGENT_TO_TOOLS`）。
#: ★ 处置轮给 **metrics + log** 两路：点名的症状（线程池拒绝 / 连接池超时）在指标里有
#:   拐点、在日志里有错误类型；只给一路会"查了但锚不上"。
LIGHT_AGENT_ROLES: Final[Mapping[Intent, tuple[str, ...]]] = {
    Intent.METRIC_QUERY: ("metrics_agent",),
    Intent.REMEDIATION: ("metrics_agent", "log_agent"),
}

#: ★★ "这次到底要不要真的去取证"的**唯一判据**（完整链路 ∪ 轻链路 ∪ 处置轮）。
#:   把它与 `FORENSICS_INTENTS` 分开，是为了让"完整 5 路"、"单路现状"与
#:   "处置轮定向取证"三种链路各自有一个名字（混用一次的后果：给 `METRIC_QUERY`
#:   派 5 路取证，或反过来让 `INCIDENT_RCA` 只查一路）。
EVIDENCE_INTENTS: Final[frozenset[Intent]] = (
    FORENSICS_INTENTS | LIGHT_INTENTS | REMEDIATION_INTENTS)

