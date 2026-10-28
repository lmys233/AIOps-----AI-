"""SDK 自带的 `sitecustomize`：解释器启动时自动装上链路埋点。

★ 解释器启动时会自动导入名为 `sitecustomize` 的模块（只要它在 `sys.path` 上）。
  把它所在目录放进 `PYTHONPATH`，就等于"**不碰业务代码**也能在进程起来的那一刻
  装好自动埋点" —— 这正是"零代码改动"在链路这一路的落点。

  用法（不经启动器、直接跑业务时）：
      $env:PYTHONPATH = "<repo>\\src\\sdk\\auto;<repo>\\src"
      $env:OTEL_SERVICE_NAME = "order"
      $env:OTEL_TRACES_EXPORTER = "otlp"
      python -m uvicorn myapp.main:app

★ 三条硬性自我约束：

  1. **绝不阻断解释器启动**。这个文件的异常会让**每一个** python 进程都起不来
     （包括 `pip`、包括我们自己的启动器）—— 后果远比"没有链路"严重。
     所以整段包在 try/except 里，失败只往 stderr 打一行。
  2. **绝不静默**：失败那行必须打出来，不能吞。
  3. **只在"确实是 SDK 让这么干的"时候才动手**：判据是
     `AIOPS_SDK_AUTOTRACE=1`（由启动器/文档设置）。否则任何 python 进程
     只要 `PYTHONPATH` 里带了这一层就会被埋点 —— 那是越权。

★ 与官方那份 `sitecustomize` 的关系：解释器只会导入**一个**。
  所以本文件要么自己在里面调 `initialize()`（下面就是），
  要么让官方的先被找到 —— 两种顺序都能工作，见 `sdk/tracing.py` 的模块文档。
"""

from __future__ import annotations

import os
import sys


def _bootstrap() -> None:
    if os.environ.get("AIOPS_SDK_AUTOTRACE") != "1":
        return

    # 幂等：启动器自己也会初始化一次（那条路径更可靠），重复调用要无害
    if os.environ.get("AIOPS_SDK_AUTOTRACE_DONE") == "1":
        return

    from sdk.tracing import initialize_in_process

    initialize_in_process()
    os.environ["AIOPS_SDK_AUTOTRACE_DONE"] = "1"


try:
    _bootstrap()
except Exception as exc:  # noqa: BLE001 —— 见模块文档第 1 条：绝不让解释器起不来
    print(
        f"[aiops-sdk] sitecustomize 自动埋点未生效：{type(exc).__name__}: {exc}"
        f"（业务不受影响，但没有链路）",
        file=sys.stderr,
    )
