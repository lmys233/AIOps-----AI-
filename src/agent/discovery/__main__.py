"""`python -m agent.discovery` —— 探测器的规范入口。

★ 为什么不是 `python -m agent.discovery.probe`：
  包初始化时会 import `.probe`，再把它当 `__main__` 执行会被解释器警告
  （"found in sys.modules after import of package ..."）。
  用 `-m agent.discovery` 走这个文件，就没有这个问题。
"""

from __future__ import annotations

import sys

from .probe import main

if __name__ == "__main__":
    sys.exit(main())
