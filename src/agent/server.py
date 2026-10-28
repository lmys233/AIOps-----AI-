"""Agent 的兼容门面：把 `agent.api` 的公开名字再导出一次。

`create_app()` 建应用；`ChatService` 是请求处理核心。这里只保留**调用方真的在用**
的那些名字（其余请直接从 `agent.api.*` 子模块导入）。
"""

from .api.alerts import ALERT_MAX_BATCH
from .api.app import create_app
from .api.approvals import ApprovalRegistry, PendingApproval
from .api.constants import DEFAULT_AGENT_PORT
from .api.requests import ChatRequest, ChatUser
from .api.service import ChatService
from .api.util import resolve_port
from .api.wiring import build_gateway

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
