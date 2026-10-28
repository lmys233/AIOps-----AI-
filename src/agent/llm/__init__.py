"""模型调用层（`LLMGateway`）。

★ 与 `agent/common/` 的分工：`common/` 放**契约与抽象**（谁都能引用的"项目的语言"），
  本包放**实现**（某个协议的具体客户端）。这样替换供应商只动本包，
  调用方（意图层 / RCA / Critic）一行不用改。
"""

from .gateway import (
    DEFAULT_TIMEOUT_SEC,
    MAX_SCHEMA_RETRIES,
    MAX_TIMEOUT_RETRIES,
    MAX_TOKENS_CEILING,
    MAX_TRUNCATION_RETRIES,
    LLMCallRecord,
    LLMError,
    LLMGateway,
    UsageSink,
)
from .openai_compat import OpenAICompatGateway

__all__ = [
    "LLMGateway",
    "LLMError",
    "LLMCallRecord",
    "UsageSink",
    "OpenAICompatGateway",
    "DEFAULT_TIMEOUT_SEC",
    "MAX_SCHEMA_RETRIES",
    "MAX_TIMEOUT_RETRIES",
    "MAX_TRUNCATION_RETRIES",
    "MAX_TOKENS_CEILING",
]
