"""被观测系统探测层。

★ 职责单一：回答"**现在有哪些被观测服务**"，并把答案落到
  `src/agent/observed_info.yaml` —— AIOps 侧唯一的服务信息来源。

  意图识别的槽位抽取（`agent/intent/slots.py`）、后续的拓扑与取证都从那份文件读，
  不再各自维护服务清单（"同一事实只留一份"）。

  ⚠️ 注意与仓库里的 `src/observed/` 区分：那是**被观测系统本身**，
     本包是**观测方**用来发现它的探测器。

  ★ 探测函数**故意不从本包再导出**：包属性 `probe` 要留给子模块
    `agent.discovery.probe`（否则 `import agent.discovery.probe as P` 会拿到函数，
    是个很费时间的坑）。要调它请写 `from ..discovery.probe import probe`。
"""

from .probe import (
    DEFAULT_INTERVAL_SEC,
    DEFAULT_TIMEOUT_SEC,
    OBSERVED_INFO_FILE,
    ObservedInfo,
    ObservedInfoError,
    ObservedService,
    build_observed_info,
    load_observed_info,
    load_seed,
    write_observed_info,
)

__all__ = [
    "ObservedService",
    "ObservedInfo",
    "ObservedInfoError",
    "load_seed",
    "build_observed_info",
    "write_observed_info",
    "load_observed_info",
    "OBSERVED_INFO_FILE",
    "DEFAULT_TIMEOUT_SEC",
    "DEFAULT_INTERVAL_SEC",
]
