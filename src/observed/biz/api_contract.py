"""端点与方法契约的唯一事实来源。

★ 为什么单独抽一张表：

  用 GET 调 POST 端点返回 405，上游把它包装成 502/503，
  排查时**误判为"下游宕机"** —— 真正的原因（方法写错）被完全掩盖。

  事故根因是"方法与路径分别写在两处"：调用方写 GET、实现方写 POST，
  两边各自看起来都对。所以这里让**路径与方法只有一个副本**：

    · 服务端用 :data:`ENDPOINTS` 里的 path 注册路由；
    · 调用方用 :func:`method_for` 取方法，**没有地方可以手写方法**。

  `tests/verify_phase1.py --task t6` 会静态核对"服务端的装饰器"与"这张表"逐条一致。

★ 本表放在**业务侧**（`observed.biz`）而不是 SDK：
  它是被观测系统的业务契约，与"谁来采集"无关。
"""

from __future__ import annotations

from typing import Mapping

__all__ = ["ENDPOINTS", "method_for", "path_for", "render_path", "endpoints_of"]

# service -> ((path, method), ...) —— 服务端与调用方共用的唯一端点表
ENDPOINTS: dict[str, tuple[tuple[str, str], ...]] = {
    "gateway": (
        ("/api/orders", "POST"),
        ("/api/orders/{order_id}", "GET"),
    ),
    "order": (
        ("/orders", "POST"),
        ("/orders/{order_id}", "GET"),      # ★ 对照端点：不访问数据库
        ("/pool-stats", "GET"),
    ),
    "inventory": (
        ("/check", "GET"),
        ("/reserve", "POST"),
    ),
    "payment": (
        ("/pay", "POST"),
        ("/refund-policy", "GET"),          # ★ 对照端点：不访问数据库
    ),
}


def _lookup(service: str) -> dict[str, str]:
    return {path: method for path, method in ENDPOINTS.get(service, ())}


def method_for(service: str, path: str) -> str:
    """取某个服务某个路径的**唯一正确方法**。查不到即报错（fail-fast）。

    ★ 刻意不提供默认值：若允许"查不到就用 GET"，
      那么新增端点时忘了登记，就会静默地产生一次 405 ——
      正是"把方法写错包装成下游宕机"的现场。
    """
    table = _lookup(service)
    if path not in table:
        raise KeyError(
            f"{service} 未登记端点 {path!r}（方法契约要求先登记再用）"
        )
    return table[path]


def path_for(service: str, path: str) -> str:
    """校验路径存在并原样返回（调用方语义更清晰的写法）。"""
    method_for(service, path)
    return path


def render_path(path: str, path_params: Mapping[str, object] | None = None) -> str:
    """把 ``/orders/{order_id}`` 渲染成实际路径。缺参数即报错。"""
    if not path_params:
        if "{" in path:
            raise KeyError(f"路径 {path!r} 需要 path_params，但未提供")
        return path
    rendered = path
    for key, value in path_params.items():
        placeholder = "{" + key + "}"
        if placeholder not in rendered:
            raise KeyError(f"路径 {path!r} 中没有占位符 {placeholder!r}")
        rendered = rendered.replace(placeholder, str(value))
    if "{" in rendered:
        raise KeyError(f"路径 {path!r} 仍有未替换的占位符：{rendered!r}")
    return rendered


def endpoints_of(service: str) -> tuple[tuple[str, str], ...]:
    return ENDPOINTS.get(service, ())
