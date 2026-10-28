"""遥测脱敏：模式表 + 三个纯函数。**SDK 侧唯一实现。**

--------------------------------------------------------------------------------
★ 为什么 SDK 也要有一份（而不是 import agent 的那份）
--------------------------------------------------------------------------------

`agent/memory/gates.py` 的 `DEFAULT_REDACT_PATTERNS` 是**Agent 侧**（写记忆之前）的脱敏；
这里的是**被观测系统侧**（日志/链路进 Loki/Tempo 之前）的脱敏。两边是**两个发布物**
（`sdk` 是装进业务进程的库，agent 是平台侧服务）—— SDK 绝不能 import agent 的代码。

所以本项目的做法是：**两份表，但由测试强制逐字相等**
（`tests/agent_unit.py` 断言 `SDK patterns == Agent patterns`）——
漂移会当场红，而不是靠人记得同步。
"""

from __future__ import annotations

import re
from typing import Any, Final, Mapping

__all__ = ["DEFAULT_REDACT_PATTERNS", "SENSITIVE_KEYS", "REDACT_MASK",
           "redact_text", "is_sensitive_key", "redact_event_dict", "strip_query"]

#: 敏感文本的替换目标（★ 保留"这里被抹过"的可见性，不直接删字段）
REDACT_MASK: Final[str] = "***"

#: ★★ 脱敏模式（与 `agent/memory/gates.py` 的 `DEFAULT_REDACT_PATTERNS` **逐字相同**）。
#: `tests/agent_unit.py` 会断言两边相等 —— 同一类东西不该有两套规则。
DEFAULT_REDACT_PATTERNS: Final[tuple[str, ...]] = (
    r"(?i)(password|passwd|pwd)\s*[:=]\s*\S+",       # password=xxx
    r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*\S+",  # token=xxx
    r"sk-[A-Za-z0-9]{8,}",                            # 供应商 key
    r"\b[\w.+-]+@[\w-]+\.[\w.]+\b",                   # 邮箱
    r"\b1[3-9]\d{9}\b",                               # 手机号（中国大陆）
    r"\b\d{1,3}(?:\.\d{1,3}){3}\b",                   # IPv4
    r"(?i)postgres(?:ql)?://\S+",                      # 带凭据的 DSN
)

#: 键名命中即整值打码（比模式匹配更可靠：`{"dsn": "..."}` 的值里可能没有 `postgres://`）
SENSITIVE_KEYS: Final[tuple[str, ...]] = (
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
    "authorization", "auth", "credential", "credentials", "dsn", "private_key",
)

_COMPILED: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern) for pattern in DEFAULT_REDACT_PATTERNS)


def redact_text(text: object) -> str:
    """把文本里命中模式的部分换成 `***`（**唯一实现**，日志/链路共用）。"""
    out = str(text if text is not None else "")
    for pattern in _COMPILED:
        out = pattern.sub(REDACT_MASK, out)
    return out


def is_sensitive_key(key: object) -> bool:
    """键名像不像凭据（`apiKey` / `X-Api-Key` / `db_password` 都算）。"""
    lowered = str(key or "").lower()
    return any(mark in lowered for mark in SENSITIVE_KEYS)


def strip_query(url: object) -> str:
    """去掉 URL 的 query 部分（`http.target` / `url.full` 这类属性的值）。

    ★ 为什么必须去掉：`POST /orders?api_key=…` 这类请求会把凭据**原样**写进 Tempo；
      而 query string 对"哪个接口"的判定没有价值（路由模板才是）。

    ★★ **当前这条防护不在 SDK 侧生效** —— 本函数在 `src/` 内没有调用者
      （SDK 不创建自己的 TracerProvider，拿不到 span processor 的挂载点）。
      query 的剥离目前由**采集器**负责：`deploy/otel/otel-collector-config.yml`
      删掉 `http.target` / `url.full` / `http.url` / `url.query` 四个属性。
      → 保留本函数是为"**绕过采集器直连 Tempo**"（或自建 provider）的场景留的兜底，
        并由单测固定它的行为。**不要**把它读成"已经生效的防护"。
    """
    text = str(url if url is not None else "")
    return text.split("?", 1)[0] if "?" in text else text


def redact_event_dict(event_dict: Mapping[str, Any], *,
                      depth: int = 2) -> dict[str, Any]:
    """结构化日志的脱敏工序（structlog processor）。

    | 做什么 | 为什么 |
    |---|---|
    | 键名敏感 → 整值 `***` | `{"dsn": "..."}` / `{"authorization": "..."}` 这类 |
    | 字符串值过模式表 | `msg="连接 postgres://u:p@h 失败"` 这类**藏在正文里**的凭据 |
    | 嵌套 dict/list 递归（有深度上限） | 业务常把上下文塞成一层 dict |
    | **键名不改** | 日志的字段名是契约（Log Agent 按 `error_type` 聚类），只脱敏值 |
    """
    out: dict[str, Any] = {}
    for key, value in event_dict.items():
        if is_sensitive_key(key):
            out[key] = REDACT_MASK
        else:
            out[key] = _redact_value(value, depth=depth)
    return out


def _redact_value(value: Any, *, depth: int) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if depth <= 0:
        return value
    if isinstance(value, Mapping):
        return {key: (REDACT_MASK if is_sensitive_key(key)
                      else _redact_value(item, depth=depth - 1))
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_value(item, depth=depth - 1) for item in value)
    return value
