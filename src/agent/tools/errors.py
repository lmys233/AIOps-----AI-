"""工具层的三类异常 —— **把"结局"变成类型**。

| 异常 | 含义 | 映射到的 ToolResult | 谁来修 |
|---|---|---|---|
| `SourceUnavailable` | **没查成**（网络/5xx/连不上/未接入） | `ok=False, degraded=True, data=None, TOOL_UNAVAILABLE` | 人（接依赖） |
| `QueryRejected` | **查询本身被拒**（400/422/语法错） | `ok=False, TOOL_INVALID_PARAMS` | **模型**（改一版查询再试） |
| `ToolRefused` | **工具主动拒绝**（未接入 / 策略不允许） | `ok=False, TOOL_UNAVAILABLE` + 说明 | 人（看说明） |

★ 为什么值得单独一个模块：这三种结局对应**三种完全不同的处置**
  （降级继续 / 让模型自修 / 放弃并说明）。用同一个 `Exception` 表示它们，
  调用方就只能靠"看错误字符串里有没有某个词"来分支 —— 本项目已经因此翻过车
  （靠关键词猜"要不要回退 JSON mode"）。
"""

from __future__ import annotations

__all__ = ["ToolError", "SourceUnavailable", "QueryRejected", "ToolRefused"]


class ToolError(Exception):
    """工具层异常的基类（便于执行器**一次**捕获后分类）。"""


class SourceUnavailable(ToolError):
    """**没查成**（对应 `degraded=True` 分支）。

    只有"数据源不可用"才允许抛它。"查了没数据"必须返回空列表 ——
    两者混淆正是最容易翻车的地方。
    """

    def __init__(self, error_type: str, message: str = "") -> None:
        super().__init__(message or error_type)
        self.error_type = error_type
        self.message = message or error_type


class QueryRejected(ToolError):
    """**查询本身被拒**（数据源说"你这个查询不合法"）→ `TOOL_INVALID_PARAMS`。

    ★ 与 `SourceUnavailable` 的区别是"该不该让模型改一版再试"：
      这一类的正确处置是**把数据源的原话喂回模型**让它改查询；
      把它归进"数据源不可用"会让 RCA 以为"这一路没有信号"，
      从而**排除掉本来正确的假设**。
    """

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class ToolRefused(ToolError):
    """工具**主动拒绝执行**（不是环境问题，是能力/策略问题）。

    例：`kb_search` 明确降级、`change_write` 要等沙箱与审批接入后才可用。
    ★ 这类必须**说清"什么时候会有"**，否则调用方只能反复重试一个永远不会成的事。
    """

    def __init__(self, error_code: str, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.error_type = error_type
        self.message = message
