"""表示请求处理的**可预期**拒绝（带 HTTP 状态码与错误码）。

`ResumeError` 供恢复路径抛，`InvalidAlerts` 供告警接入抛 —— 端点只捕它认识的那一种，
于是"内部任何一个 `ValueError` 都变成 422 + 内部原文回客户端"这条宽捕获面不存在。

★ 下面这几张码是**同一族**（HTTP 请求级，见 `ErrorCode` 的后半段）：它们表示"这次请求本身
  不成立"，由 HTTP 层直接返回，**不进** `ToolResult.error_code`（那一族是工具/模型级）。
  取值只从枚举取，本模块不再各写一份字面量。
"""

from __future__ import annotations

from typing import Any, Final

from ..common.enums import ErrorCode

#: 对话入口参数非法（422）。
INVALID_PARAMS: Final[str] = ErrorCode.INVALID_PARAMS.value

#: 共享密钥缺失或不对（401/403）。
UNAUTHORIZED: Final[str] = ErrorCode.UNAUTHORIZED.value

#: `GET /v1/incidents/{id}` 的 404：事故不在留存期/从未存在（**不是**"没有异常"）。
INCIDENT_NOT_FOUND: Final[str] = ErrorCode.INCIDENT_NOT_FOUND.value

#: 本地会话表的 404（会话不存在 / 删除幂等）。
NOT_FOUND: Final[str] = ErrorCode.NOT_FOUND.value

#: **重复请求**（同一轮次 id，或抖动窗口内同内容）—— 机器可读的 409。
CONFLICT: Final[str] = ErrorCode.CONFLICT.value


class ResumeError(Exception):
    """可预期的拒绝（带 HTTP 状态码与错误码）。

    ★ 为什么不用通用异常：审批红线的拒绝必须是**结构化**的（`code` 可供客户端分支），
      而且不能走"内部错误"那条路（那会变成 500 + 通用提示，红线就查不出原因了）。
    """

    def __init__(self, status_code: int, code: str, message: str,
                 detail: str = "") -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = str(code)
        self.message = str(message)
        #: ★ 机器可读的细分原因：错误码表不能自己扩，
        #:   于是"同一个码下的不同原因"放在这里（例如 `evicted_by_capacity`
        #:   vs `absent_or_expired`）。空 = 没有细分。
        self.detail = str(detail or "")

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail:
            body["detail"] = self.detail
        return body


class DuplicateRequest(Exception):
    """**重复请求**的直接拒绝（409 + `CONFLICT`）：不回放上一次的响应体。

    ★ 为什么不复用 `ResumeError`：那个异常是恢复/审批路径的拒绝，判据与对话入口不同 ——
      混用会让 `except ResumeError` 的捕获面悄悄变大（抓到不归它管的东西）。
    ★ 响应体里**不带** `incident_id`：重复请求不产出新事故，旧事故号也不该被当成
      "这一次的结果"发回去。
    """

    status_code: int = 409
    code: str = CONFLICT

    def __init__(self, message: str, *, turn_id: str = "", detail: str = "") -> None:
        super().__init__(message)
        self.message = str(message)
        #: 被拒的是哪一轮（客户端据此知道"换一个 id 再问"）
        self.turn_id = str(turn_id or "")
        #: 机器可读的细分原因（`dup_turn_id` / `dup_content`）
        self.detail = str(detail or "")

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.turn_id:
            body["turn_id"] = self.turn_id
        if self.detail:
            body["detail"] = self.detail
        return body


class InvalidAlerts(Exception):
    """`POST /v1/alerts` 的**可预期**拒绝（入参不合法）。

    ★ 为什么不让端点捕 `ValueError`：那个捕获面**比需要宽** —— 内部任何一次
      `int()` / 解析抛出的 `ValueError` 都会以 422 + 内部原文回客户端
      （等于把"入参非法"这个判断权交给任意内部代码）。
      只抛这一种、只捕这一种，宽窄就一致了。
    """
