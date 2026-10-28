"""共享设施包：配置 / 枚举 / 契约 / 抽象接口。

★ 本包是"项目的语言"所在地 —— 所有跨组件共享的类型都定义在这里，
  业务代码不得再定义同类常量（散落的魔法数字是坑）。
"""

from .config import Config, ConfigError, load_agent_config, load_config, load_observed_config
from .envfile import DEFAULT_ENV_FILE, load_env_file, parse_env_text
from .enums import (
    ERROR_CODE_SET,
    ERROR_RETRY_POLICY,
    EVIDENCE_SOURCE_SET,
    INTENT_SET,
    RISK_LEVEL_SET,
    TOOL_NAMES,
    TOOL_NAME_SET,
    TOOL_UNLOCK_MAP,
    READ_ONLY_TOOLS,
    CHANGE_WRITE_INTENT,
    VERIFY_GATE_THRESHOLD,
    VERIFY_WEIGHTS,
    ArbitDecision,
    ChangeStage,
    ErrorCode,
    EvidenceDirection,
    EvidenceSource,
    HypothesisStatus,
    Intent,
    IntentSource,
    RiskLevel,
    ToolPermission,
    VerificationMethod,
    is_retryable,
)
from .thresholds import ABSTAIN_BELOW, CONFIRM_SCORE, TOP2_CLOSE

__all__ = [
    # config
    "Config",
    "ConfigError",
    "load_config",
    "load_agent_config",
    "load_observed_config",
    # .env 装载
    "DEFAULT_ENV_FILE",
    "load_env_file",
    "parse_env_text",
    # enums
    "VerificationMethod",
    "Intent",
    "RiskLevel",
    "EvidenceSource",
    "EvidenceDirection",
    "HypothesisStatus",
    "ArbitDecision",
    "IntentSource",
    "ChangeStage",
    "ErrorCode",
    "ToolPermission",
    "TOOL_UNLOCK_MAP",
    "READ_ONLY_TOOLS",
    "CHANGE_WRITE_INTENT",
    # constants
    "VERIFY_WEIGHTS",
    "VERIFY_GATE_THRESHOLD",
    "ERROR_RETRY_POLICY",
    "TOOL_NAMES",
    "TOOL_NAME_SET",
    "EVIDENCE_SOURCE_SET",
    "INTENT_SET",
    "RISK_LEVEL_SET",
    "ERROR_CODE_SET",
    "is_retryable",
    # 判据阈值（唯一来源；终止判据与仲裁判据都读它）
    "CONFIRM_SCORE",
    "TOP2_CLOSE",
    "ABSTAIN_BELOW",
]
