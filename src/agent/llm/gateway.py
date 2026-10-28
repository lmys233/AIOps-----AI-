"""`LLMGateway` · 模型调用统一入口。

--------------------------------------------------------------------------------
★ 五条行为约束，逐条落到这里的哪个位置
--------------------------------------------------------------------------------

  · **强制结构化输出**：必须传 `schema`，走约束解码，**不接受自由文本返回**。
    `generate()` 的签名**强制** `schema: type[BaseModel]`；实现里优先用
    **function calling**（OpenAI 兼容协议里等价于约束解码），
    被拒时退回 JSON mode + 把 schema 写进提示词。
  · **二次校验**：返回后必须 `schema.model_validate()`，失败重试一次，
    再失败报 `LLM_SCHEMA_INVALID`。落点是实现里的 `_validate()` + `retry` 计数
    （把校验错误**喂回**给模型）。
  · **角色与模型解耦**：`role` 决定预算归属与审计字段；`model` 可替换，
    **不得硬编码模型名**。`role` 与 `model` 都是**参数**；模型名只来自配置。
  · **Token 记账**：每次调用记录 `input_tokens`/`output_tokens` 到 `board.cost`，
    并写入 OTel span 属性。落点是 `LLMCallRecord` + 注入的 `usage_sink`。
  · **超时**：默认 60s；超时抛 `LLM_TIMEOUT`，**不得静默重试超过 1 次**。
    落点是 `timeout_sec=60.0` 与常量 `MAX_TIMEOUT_RETRIES`。

--------------------------------------------------------------------------------
★ 为什么返回 `BaseModel` 而不是"信封"，token 记账怎么办
--------------------------------------------------------------------------------

签名是 `-> BaseModel`（返回值就是**校验过的结构化结果**）。
所以**不能**把返回值改成 `{value, usage}` 那种信封 —— 那会让每个调用方都多一层解包。
Token 记账改走**注入的 `usage_sink`**（接到 `board.cost`）
与 **OTel span 属性**。

★ 记账失败绝不影响调用本身（`usage_sink` 抛异常只记一次警告）——
   与 SDK 那边 `safe_record()` 是同一条口径：观测侧的问题不能拖垮主流程。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Final, Optional

from pydantic import BaseModel

from ..common.enums import ErrorCode

__all__ = [
    "LLMError",
    "LLMCallRecord",
    "UsageSink",
    "LLMGateway",
    "MAX_SCHEMA_RETRIES",
    "MAX_TIMEOUT_RETRIES",
    "MAX_TRUNCATION_RETRIES",
    "MAX_TOKENS_CEILING",
    "DEFAULT_TIMEOUT_SEC",
]

# 超时默认 60s；"不得静默重试超过 1 次"
DEFAULT_TIMEOUT_SEC: Final[float] = 60.0
MAX_TIMEOUT_RETRIES: Final[int] = 1
# `model_validate` 失败"重试一次"
MAX_SCHEMA_RETRIES: Final[int] = 1
# ★ 输出被 max_tokens 截断（思考型模型）时的重试：**放大预算**再试，而不是原样重试
MAX_TRUNCATION_RETRIES: Final[int] = 1
MAX_TOKENS_CEILING: Final[int] = 8192


class LLMError(Exception):
    """模型调用失败。`code` 必须是已定义的错误码之一。

    ★ 两条硬约束：

      1. **绝不把 API Key 带进消息** —— 消息会被日志、审计、前端看到。
         实现侧对 provider 返回体做了截断；这里再兜一层：`__str__` 会过滤
         形如 `sk-...` / `Bearer ...` 的片段（防的是"上游把请求头回显在错误里"）。
      2. **错误码决定重试语义**，不是调用方拍脑袋：`retryable` / `limit`
         一律查 `ERROR_RETRY_POLICY`。
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        role: str = "",
        model: str = "",
        attempts: int = 1,
        failure_kind: str = "",
    ) -> None:
        super().__init__(_redact(message))
        self.code = code
        self.role = role
        self.model = model
        self.attempts = attempts
        self.failure_kind = failure_kind or getattr(self,'failure_kind','')

    def __str__(self) -> str:  # pragma: no cover - 由 super().__init__ 已处理
        return _redact(super().__str__())


_LEAK_MARKERS = ("sk-", "Bearer ", "api_key", "apikey", "authorization")


def _redact(text: str) -> str:
    """把疑似密钥的片段抹掉。

    ★ 宁可把一段有用的调试信息抹成 `***`，也不能让 Key 出现在日志里 ——
      Key 泄露是**不可回滚**的，而丢失一点调试信息是可回滚的。

    ★ 这里踩过一次：第一版写成 ``while marker in out:`` 循环替换，
      而**替换后的字符串里 marker 本身还在**（`Bearer sk-x` → `Bearer ***`，
      仍然含 `Bearer `）→ **死循环**。
      一个"防泄露"的函数把自己变成挂死，比它要防的问题更糟。
      所以改成"从上次位置之后继续找"，并用 `idx` 跳过刚插入的 `***`。
    """
    out = str(text)
    for marker in _LEAK_MARKERS:
        idx = 0
        while True:
            start = out.find(marker, idx)
            if start < 0:
                break
            end = start + len(marker)
            while end < len(out) and not out[end].isspace() and out[end] not in '",}':
                end += 1
            out = f"{out[:start]}{marker}***{out[end:]}"
            # ★ 跳过 marker + 刚插入的 `***`，否则会在原地反复命中
            idx = start + len(marker) + 3
    return out


@dataclass(frozen=True)
class LLMCallRecord:
    """一次模型调用的记账。

    ★ 字段要足够让"成本爆炸"可归因：谁花的（`role`）、花的哪个模型（`model`）、
      花在几次尝试上（`attempts` —— 重试也是钱）。
    """

    role: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    attempts: int
    ok: bool
    error_code: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)
    call_id: str = ''
    usage_known: bool = True
    reserved_tokens: int = 0


# 记账出口：接到 `board.cost`
UsageSink = Callable[[LLMCallRecord], None]


class LLMGateway(ABC):
    """模型调用统一入口。

    ★ 为什么是一个 ABC 而不是直接一个 httpx 客户端：
      要求"主力模型可替换（API ↔ 本地），不得硬编码模型名"。
      把"调用契约"与"某个供应商的 HTTP 细节"分开，替换时调用方一行不用改。
    """

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        schema: type[BaseModel],
        *,
        role: str,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> BaseModel:
        """让模型产出一个**符合 `schema` 的结构化结果**。

        `prompt` 是用户/上游给的输入（本层不做模板拼接的艺术加工）；
        `schema` 是 Pydantic 模型类，**必传**，不接受自由文本；
        `role` 是调用角色（`intent` / `rca` / `critic` …），决定预算归属与审计字段；
        `model` 是模型名，`None` 表示用配置里的默认模型 ——
        ★ 不得在代码里硬编码模型名；`temperature` / `max_tokens` 是采样参数。

        失败一律抛 `LLMError`：超时（`LLM_TIMEOUT`）/ 结构非法（`LLM_SCHEMA_INVALID`）/
        供应商不可用（暂借 `INTERNAL_ERROR` —— 该场景暂无专属错误码）。
        """

    @abstractmethod
    async def health(self) -> tuple[bool, str]:
        """探活：返回 `(是否可用, 说明)`。

        ★ 用途有两个，都很重要：
          1. **启动时**决定"要不要启用模型那两级"（不可用就纯规则层，并说明原因）；
          2. 给运维一条**不消耗 token** 的连通性检查命令。
        """
