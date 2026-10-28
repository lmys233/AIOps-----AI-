"""定义请求契约（四个 pydantic 模型：用户 / 上下文 / 选项 / 请求体）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator,field_validator
from ..common.enums import CHAT_MAX_ROUNDS_LIMIT, CHAT_TOKEN_BUDGET_MAX
from typing import Optional, Literal
from ..domain.tasks import ScopePatch
from ..dialogue.contracts import Target


# ======================================================================
#  请求契约（参数表）
# ======================================================================
class ChatUser(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., min_length=1, description="用于审计；不得为空")
    name: str = ""

class ChatContext(BaseModel):
    # ★★ 未知字段**不再静默丢弃**（`extra="forbid"`）——
    #   调用方写 `context.force=true` 时会拿到 422 + 明确的 details，
    #   而不是"以为传了、其实被忽略"：不许悄悄不兑现。
    model_config = ConfigDict(extra="forbid")

    env: Optional[str] = None
    cluster: Optional[str] = None
    recent_turns: int = 0
    dialogue: Optional[Target] = None
    observed_id: Optional[str] = Field(default=None,pattern=r'^obs_[a-f0-9]{32}$')
    task_patch: Optional[ScopePatch] = None
    timezone: str = Field(default='Asia/Shanghai',max_length=64)

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls,value):
        from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
        try:ZoneInfo(value)
        except (ZoneInfoNotFoundError,ValueError):raise ValueError('timezone需为IANA时区名') from None
        return value

class ChatOptions(BaseModel):
    """钳制已知字段的越界值，而不是拒绝整次请求（客户端多写一个 0 不该让排查失败）。

    ★ 但**未知字段**是另一回事：`extra="forbid"` ——
    "钳制已知字段的越界值"与"静默忽略不认识的字段"必须分开：
    前者是宽容，后者是**假装收到了**。
    """

    model_config = ConfigDict(extra="forbid")

    stream: bool = False
    response_mode: Literal['full','view'] = 'full'
    max_rounds: int = 3
    token_budget: int = 200_000
    dry_run_only: bool = False
    allow_write: bool = False
    #: ★ 用户显式要求「别问了，直接给」—— 反问被禁用，
    #:   但响应必须**标注「未经澄清」**（静默不问 = 假装信息齐了）。
    no_clarify: bool = False

    @model_validator(mode="after")
    def _clamp(self) -> "ChatOptions":
        if self.max_rounds > CHAT_MAX_ROUNDS_LIMIT:
            self.max_rounds = CHAT_MAX_ROUNDS_LIMIT
        if self.max_rounds < 1:
            self.max_rounds = 1
        if self.token_budget > CHAT_TOKEN_BUDGET_MAX:
            self.token_budget = CHAT_TOKEN_BUDGET_MAX
        if self.token_budget < 1:
            self.token_budget = 1
        return self

class ChatRequest(BaseModel):
    # ★★ 未知字段**报错**而不是静默忽略（`extra="forbid"`）。
    #   调用方发来 `options.force` / `context.force` 这类字段时会拿到 422 +
    #   逐字段的 details —— 这正是 `_validation_error_body` 存在的意义。
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., min_length=1, description="幂等键")
    #: ★ **对话级 id**（可选）：雪花风格、携带生成时间。缺省时服务端按同一规则铸一个
    #:   并在响应里回显；同一轮次 id 只允许跑一次（重复请求直接拒绝）。
    turn_id: str = Field("", description="对话级 id（缺省时服务端代铸并回显）")
    message: str = Field(..., min_length=1, description="1~4000 字符；超长截断")
    user: ChatUser
    context: ChatContext = Field(default_factory=ChatContext)
    options: ChatOptions = Field(default_factory=ChatOptions)
