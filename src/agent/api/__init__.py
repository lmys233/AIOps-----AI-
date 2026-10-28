"""Agent 服务壳的公开入口（服务类、装配函数、请求模型）。

`create_app()` 建应用；`ChatService` 是请求处理核心；端点见 `app.py` 的三组
`_register_*_routes`（这里不再手抄端点清单 —— 手抄的表迟早与代码漂移）。
"""

from .alerts import ALERT_MAX_BATCH
from .app import create_app
from .approvals import ApprovalRegistry, PendingApproval
from .constants import DEFAULT_AGENT_PORT
from .requests import ChatRequest, ChatUser
from .service import ChatService
from .util import resolve_port
from .wiring import build_gateway

__all__ = [
    "ChatService",
    "create_app",
    "resolve_port",
    "build_gateway",
    "DEFAULT_AGENT_PORT",
    "ALERT_MAX_BATCH",
    "ApprovalRegistry",
    "PendingApproval",
    "ChatRequest",
    "ChatUser",
]
