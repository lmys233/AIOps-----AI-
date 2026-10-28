"""Names come from validated scope, then the injected routing catalog."""
from typing import Any
def service_names_of(router: Any) -> tuple[str, ...]:
    """从路由器读**被观测服务清单**（判"是不是补了个服务名"要用）。

    ★ 取不到就返回空元组：判据退化成"只看显式接续词"（**不猜**服务名）。
      注入的假路由（单测/离线）没有 `slot_config`，那里不该炸。
    """
    from .context import current
    scope=current()
    if scope:return scope.names
    config = getattr(router, "slot_config", None)
    return tuple(getattr(config, "service_names", ()) or ())

