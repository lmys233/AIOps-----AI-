"""提供与业务无关的小工具（指纹 / 时钟 / trace_id / 端口 / 错误体）。

时间源口径：**时长与窗口**用 `time.monotonic()`（或 `_now_ms()` 的 `perf_counter`），
**要跨进程持久化或给人看的时间戳**用 `time.time()` —— 两者不许混用。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from fastapi import Request
from fastapi.exceptions import RequestValidationError
import os
import time
from urllib.parse import urlparse
import uuid
from ..common.action_hash import action_fingerprint
from .constants import DEFAULT_AGENT_PORT, _ENV_AGENT, _TRACE_RE, logger
from .errors import INVALID_PARAMS


# ======================================================================
#  Web 壳
# ======================================================================
def _action_fingerprint(action: Mapping[str, Any]) -> str:
    """算预案的 `action_hash`（审批绑定的就是它）。

    ★★ 口径**唯一实现**在 `common/action_hash.action_fingerprint`，
      执行端（`tools/executor`）调的是同一个函数 —— 以前两处各写一套，
      注释却说"一致"，接执行器时审批绑定就会与执行端校验对不上。
    """
    return action_fingerprint(action)


def _now_ms() -> float:
    return time.perf_counter() * 1000.0

def _trace_id_from(request: Request) -> str:
    """trace_id：客户端给了合法值就沿用（便于跨系统串联），否则自己生成。"""
    raw = (request.headers.get("x-trace-id") or "").strip()
    if raw and _TRACE_RE.fullmatch(raw):
        return raw
    return uuid.uuid4().hex

def _env(name: str) -> Optional[str]:
    return os.environ.get(name)

def resolve_port(raw: Optional[str] = None) -> int:
    """从 `AIOPS_AGENT` 里取端口。

    ★ 这个变量在**测试与文档里是 base_url**（`http://127.0.0.1:8000`），
      而在这里它是"监听在哪里"。两种写法都支持：
      `http://127.0.0.1:8000` / `127.0.0.1:8000` / `8000`。
    """
    value = (raw if raw is not None else _env(_ENV_AGENT) or "").strip()
    if not value:
        return DEFAULT_AGENT_PORT
    if value.isdigit():
        return int(value)
    parsed = urlparse(value if "//" in value else f"//{value}")
    if parsed.port:
        return int(parsed.port)
    bare = value.rsplit(":", 1)[-1]
    if bare.isdigit():
        return int(bare)
    logger.warning("无法从 %s=%r 解析端口，回退到 %d", _ENV_AGENT, value, DEFAULT_AGENT_PORT)
    return DEFAULT_AGENT_PORT

def _validation_error_body(exc: RequestValidationError) -> dict[str, Any]:
    """把 FastAPI 的 422 变成**结构化**错误（客户端按 `code` 分支）。

    ★ 用 `INVALID_PARAMS` 而不是 `TOOL_INVALID_PARAMS`：
      后者的语义是"**工具调用**入参非法"，用在对话入口上是串味。
      错误码表没给"对话参数非法"留码，这里如实新起一个全大写码
      （已记入偏差表，待错误码表收敛时统一）。
    """
    details = [
        {"field": ".".join(str(p) for p in err.get("loc", [])), "message": err.get("msg", "")}
        for err in exc.errors()
    ]
    return {
        "code": INVALID_PARAMS,
        "message": "请求参数不合法（参数表）",
        "details": details,
    }
