"""把 `prometheus_client` 默认 REGISTRY 渲染成 Prometheus 文本 —— `/metrics` 的实现。

★ 为什么渲染入口要独立存在：`/metrics` 是**契约端点**，必须存在且返回
  Prometheus 文本格式。指标的定义与记录点都在 `definitions.py`，
  这里只负责"把注册表里的东西吐出来"。
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

__all__ = ["render_metrics"]


def render_metrics(runtime=None) -> tuple[bytes, str]:
    """返回 `(Prometheus 文本, Content-Type)`。"""
    from .system_scope import identity
    from .diagnostic_state import active_runtime
    current=identity(runtime or active_runtime())
    if current:
        from .scoped_metrics import Collector
        return generate_latest(Collector(REGISTRY,current)), CONTENT_TYPE_LATEST
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
