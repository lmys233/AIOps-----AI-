"""被观测系统包（4 个微服务：gateway / order / payment / inventory）。

本包只提供被观测系统，不依赖 Agent 的任何代码；
Agent 通过 HTTP 与遥测与它交互（服务间也只通过 HTTP 交互）。
"""

__version__ = "0.1.0"
