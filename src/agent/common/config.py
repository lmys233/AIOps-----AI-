"""配置装载。

设计要点：

1. **配置项必须显式列出，不用 `**kwargs` 兜底**。
   这样漏配会在**启动时**报错，而不是运行期才炸。

2. **两类配置项语义严格区分**：
   - `REQUIRED`：无默认值，**缺失即启动失败**（fail-fast）
   - 有默认值的项：缺失时使用下表的默认值

3. **5 个故障旋钮默认值必须精确**：
   ``DB_POOL_SIZE=20`` / ``DB_POOL_ACQUIRE_HOLD_MS=0`` / ``DB_POOL_TIMEOUT_SEC=3``
   / ``ARTIFICIAL_DELAY_MS=0`` / ``ARTIFICIAL_ERROR_RATE=0.0``

4. **边界校验**：类型与取值范围在装载时就检查（`ARTIFICIAL_ERROR_RATE` 必须在 [0,1]），
   避免"配置写错但直到故障注入时才暴露"。

★ 为什么不用 pydantic-settings：本模块要在"最小依赖"下可用，
  且 REQUIRED/默认值的语义需要完全自控。纯标准库实现，零依赖。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

import os
from dataclasses import dataclass, field

from .envfile import load_env_file
from .enums import DeploymentForm

__all__ = [
    "ConfigError",
    "ObservedConfig",
    "AgentConfig",
    "Config",
    "load_config",
    "load_observed_config",
    "load_agent_config",
    "SPEC_8_8_DEFAULTS",
    # 环境变量名 —— ★ 全系统唯一来源：其它模块**一律从这里 import 常量**，
    #   不许再手写 `"AIOPS_..."` 字面量（写错一个字母就是"配了却不生效"，
    #   而且没有任何地方会报错）。
    "ENV_DATABASE_URL",
    "ENV_REDIS_URL",
    "ENV_PROMETHEUS_URL",
    "ENV_LOKI_URL",
    "ENV_TEMPO_URL",
    "ENV_OTLP_ENDPOINT",
    "ENV_LLM_BASE_URL",
    "ENV_LLM_API_KEY",
    "ENV_LLM_MODEL",
    "ENV_ROUTER_MODEL",
    "ENV_LOG_LEVEL",
    "ENV_SERVICE_NAME",
    "ENV_CLUSTER",
    "ENV_ENV",
    "ENV_NAMESPACE",
    "ENV_MAX_ROUNDS",
    "ENV_MAX_LLM_CALLS",
    "ENV_MAX_TOOL_CALLS",
    "ENV_MAX_TOKENS",
    "ENV_MAX_WALL_TIME_SEC",
    "ENV_KNOB_POOL_SIZE",
    "ENV_KNOB_POOL_HOLD_MS",
    "ENV_KNOB_POOL_TIMEOUT_SEC",
    "ENV_KNOB_ARTIFICIAL_DELAY_MS",
    "ENV_KNOB_ARTIFICIAL_ERROR_RATE",
]


def _resolve_env(environ: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    """决定"从哪读配置"。

    ★ 三条规则（`.env.example` 承诺了 `.env`，但此前没人读它）：

      1. **调用方注入了 environ** → 用它，**不碰 `.env`**。
         测试必须完全不受本机 `.env` 影响，否则"本机碰巧配了 key"会让测试
         时绿时红 —— 那是比失败更糟的不确定性。
      2. **没注入** → 先把仓库根的 `.env` 装载进 `os.environ`（已存在的键不覆盖），
         再读 `os.environ`。这样"照文档把 key 放进 `.env`"是真的生效的。
      3. 装载本身**幂等且廉价**（缺文件直接返回），所以每次装载都调用无妨；
         不引入模块级"只加载一次"的全局状态，少一处可被测试污染的地方。
    """
    if environ is not None:
        return environ
    load_env_file()
    return os.environ


class ConfigError(Exception):
    """配置装载失败。

    ★ 这个异常在**进程启动阶段**抛出，不允许推迟到运行期 ——
      缺配置时启动即失败。
    """


# ------------------------------------------------------------------
#  故障注入旋钮的权威默认值
#
#  ★ 这五个值是唯一基准，任何改动先改这里。
# ------------------------------------------------------------------
SPEC_8_8_DEFAULTS: dict[str, Any] = {
    "DB_POOL_SIZE": 20,
    "DB_POOL_ACQUIRE_HOLD_MS": 0,
    "DB_POOL_TIMEOUT_SEC": 3,
    "ARTIFICIAL_DELAY_MS": 0,
    "ARTIFICIAL_ERROR_RATE": 0.0,
}

# ------------------------------------------------------------------
#  环境变量名（集中一处，杜绝散落的字符串字面量）
# ------------------------------------------------------------------
ENV_DATABASE_URL = "AIOPS_DATABASE_URL"
ENV_REDIS_URL = "AIOPS_REDIS_URL"
ENV_PROMETHEUS_URL = "AIOPS_PROMETHEUS_URL"
ENV_LOKI_URL = "AIOPS_LOKI_URL"
ENV_TEMPO_URL = "AIOPS_TEMPO_URL"
ENV_OTLP_ENDPOINT = "AIOPS_OTLP_ENDPOINT"
ENV_LLM_BASE_URL = "AIOPS_LLM_BASE_URL"
ENV_LLM_API_KEY = "AIOPS_LLM_API_KEY"
ENV_LLM_MODEL = "AIOPS_LLM_MODEL"
ENV_ROUTER_MODEL = "AIOPS_ROUTER_MODEL"
ENV_LOG_LEVEL = "AIOPS_LOG_LEVEL"
ENV_SERVICE_NAME = "AIOPS_SERVICE_NAME"
#: ★ 被观测系统的**部署形态事实**（`host_process` / `k8s` / 未配置=unknown）
ENV_DEPLOYMENT_FORM = "AIOPS_DEPLOYMENT_FORM"
# 部署身份（`identity.py` 的阶梯第 3 级也读这三个名字）
ENV_CLUSTER = "AIOPS_CLUSTER"
ENV_ENV = "AIOPS_ENV"
ENV_NAMESPACE = "AIOPS_NAMESPACE"

# 编排预算上限（键名在此唯一定义；默认值与边界校验见 `load_agent_config`）
ENV_MAX_ROUNDS = "AIOPS_MAX_ROUNDS"
ENV_MAX_LLM_CALLS = "AIOPS_MAX_LLM_CALLS"
ENV_MAX_TOOL_CALLS = "AIOPS_MAX_TOOL_CALLS"
ENV_MAX_TOKENS = "AIOPS_MAX_TOKENS"
ENV_MAX_WALL_TIME_SEC = "AIOPS_MAX_WALL_TIME_SEC"

# 故障旋钮沿用原始名（不加前缀，被观测系统侧直接读这几个名字）
ENV_KNOB_POOL_SIZE = "DB_POOL_SIZE"
ENV_KNOB_POOL_HOLD_MS = "DB_POOL_ACQUIRE_HOLD_MS"
ENV_KNOB_POOL_TIMEOUT_SEC = "DB_POOL_TIMEOUT_SEC"
ENV_KNOB_ARTIFICIAL_DELAY_MS = "ARTIFICIAL_DELAY_MS"
ENV_KNOB_ARTIFICIAL_ERROR_RATE = "ARTIFICIAL_ERROR_RATE"


# ------------------------------------------------------------------
#  取值转换与校验
# ------------------------------------------------------------------
def _as_int(raw: str, env_name: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"配置项 {env_name}={raw!r} 不是合法整数"
        ) from exc


def _as_non_negative_int(raw: str, env_name: str) -> int:
    value = _as_int(raw, env_name)
    if value < 0:
        raise ConfigError(f"配置项 {env_name}={value} 不能为负数")
    return value


def _as_positive_int(raw: str, env_name: str) -> int:
    value = _as_int(raw, env_name)
    if value <= 0:
        raise ConfigError(f"配置项 {env_name}={value} 必须为正整数")
    return value


def _as_float(raw: str, env_name: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"配置项 {env_name}={raw!r} 不是合法数字"
        ) from exc


def _as_ratio(raw: str, env_name: str) -> float:
    """取值为 [0,1] 的比例（如错误率）。"""
    value = _as_float(raw, env_name)
    if not (0.0 <= value <= 1.0):
        raise ConfigError(
            f"配置项 {env_name}={value} 超出取值范围 [0,1]"
            f"（人为错误率必须在 0.0~1.0）"
        )
    return value


def _as_non_empty(raw: str, env_name: str) -> str:
    if not str(raw).strip():
        raise ConfigError(f"配置项 {env_name} 不能为空字符串")
    return str(raw)


# ------------------------------------------------------------------
#  被观测系统配置
# ------------------------------------------------------------------
@dataclass(frozen=True)
class ObservedConfig:
    """被观测系统的配置。

    服务间调用只通过 HTTP，因此下游地址在此集中声明。
    """

    # ---- 服务标识 ----
    service_name: str
    env: str

    # ---- 下游依赖地址 ----
    order_url: str
    inventory_url: str
    payment_url: str

    # ---- 数据源 ----
    database_url: str
    redis_url: str

    # ---- ★ 故障注入旋钮 ----
    db_pool_size: int
    db_pool_acquire_hold_ms: int
    db_pool_timeout_sec: int
    artificial_delay_ms: int
    artificial_error_rate: float


# ------------------------------------------------------------------
#  Agent 配置
# ------------------------------------------------------------------
@dataclass(frozen=True)
class AgentConfig:
    """AIOps Agent 的配置。"""

    # ---- 观测与存储 ----
    database_url: str
    redis_url: str
    prometheus_url: str
    loki_url: str
    tempo_url: str
    otlp_endpoint: str

    # ---- LLM 网关 ----
    llm_base_url: str
    llm_model: str
    router_model: str
    llm_api_key: Optional[str]

    # ---- 编排预算（终止条件）----
    max_rounds: int
    max_llm_calls: int
    max_tool_calls: int
    max_tokens: int
    max_wall_time_sec: int

    # ---- 部署形态（**事实**，不是假设）----
    #: 被观测系统跑在什么形态里：`host_process` / `k8s` / `unknown`。
    #: ★ 用户可见文案里出现的机制名词（Pod / 副本 / 命名空间…）**必须**由它决定：
    #:   配了 k8s 才谈 Pod；没配就用中性措辞并说明"部署形态未配置"——
    #:   靠"禁词表"是治标，靠**事实**才是治本（用户 2026-10-03 裁决）。
    deployment_form: str

    # ---- 日志 ----
    log_level: str


# ------------------------------------------------------------------
#  合并配置
# ------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    """Agent + 被观测系统的合并配置。

    ★ `observed` 与 `agent` 分开成两个子对象，因为：
      Agent 只依赖被观测系统的**接口**，不依赖它的内部配置；
      混在一个扁平结构里会让"Agent 用了被观测系统的旋钮"这种越界变得容易。
    """

    observed: ObservedConfig
    agent: AgentConfig
    env_snapshot: Mapping[str, str] = field(default_factory=dict)


# ------------------------------------------------------------------
#  装载入口
# ------------------------------------------------------------------
_REQUIRED_AGENT_ENV = (
    ENV_DATABASE_URL,
    ENV_REDIS_URL,
    ENV_PROMETHEUS_URL,
    ENV_LOKI_URL,
    ENV_TEMPO_URL,
    ENV_OTLP_ENDPOINT,
    ENV_LLM_BASE_URL,
    ENV_LLM_MODEL,
)

_REQUIRED_OBSERVED_ENV = (
    ENV_DATABASE_URL,
    ENV_REDIS_URL,
)
# ★ 注意：AIOPS_SERVICE_NAME **不在必需环境变量里**，理由：
#   service_name 必须作为**函数参数**显式传入（见 load_observed_config 的签名）。
#   同一镜像要跑 4 个服务，若从环境变量读服务名，容器间极易串味；
#   而参数化后调用方必须明写，无法"忘了设环境变量"。
#   因此把 ENV_SERVICE_NAME 保留为"可选覆盖"，不列入必需项，避免设计冗余。


def _collect_missing(
    environ: Mapping[str, str],
    names: tuple[str, ...],
    where: str,
) -> list[str]:
    """返回缺失或为空的**必需**环境变量名。"""
    missing: list[str] = []
    for name in names:
        raw = environ.get(name)
        if raw is None or not str(raw).strip():
            missing.append(name)
    if missing:
        raise ConfigError(
            f"{where} 缺少必需配置项: {', '.join(missing)}。"
            f"这些项没有默认值，必须在启动前显式提供"
            f"（漏配必须在启动时报错，而不是运行期才炸）"
        )
    return missing


def load_observed_config(
    environ: Optional[Mapping[str, str]] = None,
    *,
    service_name: str,
    order_url: str = "http://order:8080",
    inventory_url: str = "http://inventory:8080",
    payment_url: str = "http://payment:8080",
) -> ObservedConfig:
    """装载被观测系统配置。

    `environ` 是环境变量映射（默认取 `os.environ`，便于测试注入）；
    `service_name` 必须显式传入 —— ★ 同镜像跑 4 个服务时**不能从环境变量读服务名**，
    否则极易串味。
    """
    env = _resolve_env(environ)

    if not str(service_name).strip():
        raise ConfigError("service_name 不能为空 —— 服务标识用于日志与指标标签")

    _collect_missing(env, _REQUIRED_OBSERVED_ENV, "被观测系统配置")

    # 旋钮：有默认值，缺失时用默认（不是错误）
    pool_size_raw = env.get(ENV_KNOB_POOL_SIZE, str(SPEC_8_8_DEFAULTS["DB_POOL_SIZE"]))
    hold_ms_raw = env.get(
        ENV_KNOB_POOL_HOLD_MS, str(SPEC_8_8_DEFAULTS["DB_POOL_ACQUIRE_HOLD_MS"])
    )
    timeout_raw = env.get(
        ENV_KNOB_POOL_TIMEOUT_SEC, str(SPEC_8_8_DEFAULTS["DB_POOL_TIMEOUT_SEC"])
    )
    delay_raw = env.get(
        ENV_KNOB_ARTIFICIAL_DELAY_MS, str(SPEC_8_8_DEFAULTS["ARTIFICIAL_DELAY_MS"])
    )
    rate_raw = env.get(
        ENV_KNOB_ARTIFICIAL_ERROR_RATE, str(SPEC_8_8_DEFAULTS["ARTIFICIAL_ERROR_RATE"])
    )

    return ObservedConfig(
        service_name=str(service_name).strip(),
        env=str(env.get(ENV_ENV, "prod")).strip() or "prod",
        order_url=str(order_url).rstrip("/"),
        inventory_url=str(inventory_url).rstrip("/"),
        payment_url=str(payment_url).rstrip("/"),
        database_url=_as_non_empty(env[ENV_DATABASE_URL], ENV_DATABASE_URL),
        redis_url=_as_non_empty(env[ENV_REDIS_URL], ENV_REDIS_URL),
        db_pool_size=_as_positive_int(pool_size_raw, ENV_KNOB_POOL_SIZE),
        db_pool_acquire_hold_ms=_as_non_negative_int(hold_ms_raw, ENV_KNOB_POOL_HOLD_MS),
        db_pool_timeout_sec=_as_positive_int(timeout_raw, ENV_KNOB_POOL_TIMEOUT_SEC),
        artificial_delay_ms=_as_non_negative_int(delay_raw, ENV_KNOB_ARTIFICIAL_DELAY_MS),
        artificial_error_rate=_as_ratio(rate_raw, ENV_KNOB_ARTIFICIAL_ERROR_RATE),
    )


def load_agent_config(
    environ: Optional[Mapping[str, str]] = None,
) -> AgentConfig:
    """装载 Agent 配置。缺少必需项时抛 `ConfigError`（fail-fast）。"""
    env = _resolve_env(environ)
    _collect_missing(env, _REQUIRED_AGENT_ENV, "Agent 配置")

    # 预算默认值：轮次 3 / LLM 40 / 工具 80 / Token 20 万 / 300s
    max_rounds = _as_positive_int(env.get(ENV_MAX_ROUNDS, "3"), ENV_MAX_ROUNDS)
    if max_rounds > 5:
        raise ConfigError(
            f"{ENV_MAX_ROUNDS}={max_rounds} 超过上限 5"
            f"（max_rounds 上限强制 5）"
        )

    return AgentConfig(
        database_url=_as_non_empty(env[ENV_DATABASE_URL], ENV_DATABASE_URL),
        redis_url=_as_non_empty(env[ENV_REDIS_URL], ENV_REDIS_URL),
        prometheus_url=_as_non_empty(env[ENV_PROMETHEUS_URL], ENV_PROMETHEUS_URL).rstrip("/"),
        loki_url=_as_non_empty(env[ENV_LOKI_URL], ENV_LOKI_URL).rstrip("/"),
        tempo_url=_as_non_empty(env[ENV_TEMPO_URL], ENV_TEMPO_URL).rstrip("/"),
        otlp_endpoint=_as_non_empty(env[ENV_OTLP_ENDPOINT], ENV_OTLP_ENDPOINT),
        llm_base_url=_as_non_empty(env[ENV_LLM_BASE_URL], ENV_LLM_BASE_URL).rstrip("/"),
        llm_model=_as_non_empty(env[ENV_LLM_MODEL], ENV_LLM_MODEL),
        router_model=str(env.get(ENV_ROUTER_MODEL, "")).strip(),
        llm_api_key=(env.get(ENV_LLM_API_KEY) or None),
        max_rounds=max_rounds,
        max_llm_calls=_as_positive_int(env.get(ENV_MAX_LLM_CALLS, "40"), ENV_MAX_LLM_CALLS),
        max_tool_calls=_as_positive_int(env.get(ENV_MAX_TOOL_CALLS, "80"), ENV_MAX_TOOL_CALLS),
        max_tokens=_as_positive_int(env.get(ENV_MAX_TOKENS, "200000"), ENV_MAX_TOKENS),
        max_wall_time_sec=_as_positive_int(
            env.get(ENV_MAX_WALL_TIME_SEC, "300"), ENV_MAX_WALL_TIME_SEC
        ),
        deployment_form=resolve_deployment_form(env),
        log_level=str(env.get(ENV_LOG_LEVEL, "INFO")).strip().upper() or "INFO",
    )


def resolve_deployment_form(environ: Optional[Mapping[str, str]] = None) -> str:
    """被观测系统跑在什么形态里 —— **读配置，不猜**（未配置 → `unknown`）。

    ★ 为什么要有这个"事实"：用户可见的方案文案里会出现机制名词（Pod / 副本 / 命名空间…）。
      这些名词**必须**来自事实：配了 k8s 才谈 Pod；没配就用中性措辞并**明说**"部署形态未配置"。
      靠禁词表是治标（换个词还会错），靠事实才治本（用户 2026-10-03 裁决）。
    """
    env = _resolve_env(environ) if environ is None else environ
    raw = str(env.get(ENV_DEPLOYMENT_FORM) or "").strip().lower()
    return raw if raw in {member.value for member in DeploymentForm} \
        else DeploymentForm.UNKNOWN.value


def load_config(
    environ: Optional[Mapping[str, str]] = None,
    *,
    service_name: str = "gateway",
) -> Config:
    """装载合并配置（Agent + 被观测系统）。

    ★ 这是进程启动时应当调用的入口。任何配置问题都在此暴露，
      不会推迟到运行期。
    """
    env = _resolve_env(environ)
    return Config(
        observed=load_observed_config(env, service_name=service_name),
        agent=load_agent_config(env),
        env_snapshot={k: v for k, v in env.items() if str(k).startswith("AIOPS_")},
    )
