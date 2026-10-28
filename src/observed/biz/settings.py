"""业务侧配置装载。

★ 这里**没有故障注入旋钮**：业务系统是**正常态**系统，故障场景改由
  **外部手段**制造（停 Postgres、docker pause、限资源、网络延迟），
  更接近真实生产，也不需要在业务代码里留后门。

★ 保留的是**正常配置项**：

  · `DB_POOL_SIZE` / `DB_POOL_TIMEOUT_SEC` —— 连接池的上限与超时，是正常运维参数
  · 服务名、端口、下游地址、数据库 DSN、日志级别

★ 不依赖 Agent 侧的任何代码（`agent/common/config.py`）——
  被观测系统必须能被整体替换，两边各自实现同一份约定，
  一致性由验收脚本交叉校验，而不是靠共享代码耦合。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional

__all__ = ["ConfigError", "BizSettings", "load_settings", "SERVICES", "SERVICE_PORTS"]


class ConfigError(Exception):
    """配置装载失败。**在进程启动阶段抛出**，不允许推迟到运行期。"""


# ------------------------------------------------------------------
#  服务与端口（唯一副本）
#
#  ★ 这张表同时被三处引用：业务代码、启动器、验收脚本。
#    只留一份，避免"启动器绑 8081 而契约说 8081"这类不一致。
# ------------------------------------------------------------------
SERVICE_PORTS: dict[str, int] = {
    "gateway": 8080,
    "order": 8081,
    "payment": 8082,
    "inventory": 8083,
}

# 每个服务需要哪些下游地址（宿主视角由启动器注入）
SERVICES: dict[str, tuple[str, ...]] = {
    "gateway": ("order",),
    "order": ("inventory", "payment"),
    "payment": (),
    "inventory": (),
}

# 端口就是对外契约：改这里等于改契约，必须同步验收脚本
assert set(SERVICES) == set(SERVICE_PORTS), "服务名单与端口表不一致"


# ------------------------------------------------------------------
#  取值转换
# ------------------------------------------------------------------
def _as_int(raw: str, env_name: str) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置项 {env_name}={raw!r} 不是合法整数") from exc


def _as_positive_int(raw: str, env_name: str) -> int:
    value = _as_int(raw, env_name)
    if value <= 0:
        raise ConfigError(f"配置项 {env_name}={value} 必须为正整数")
    return value


def _as_positive_float(raw: str, env_name: str) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置项 {env_name}={raw!r} 不是合法数字") from exc
    if value <= 0:
        raise ConfigError(f"配置项 {env_name}={value} 必须为正数")
    return value


def _opt(raw: Optional[str]) -> str:
    return str(raw).strip().rstrip("/") if raw and str(raw).strip() else ""


# ------------------------------------------------------------------
#  配置对象
# ------------------------------------------------------------------
@dataclass(frozen=True)
class BizSettings:
    """单个业务服务运行期需要的配置。"""

    service: str
    port: int
    log_level: str = "INFO"

    # 数据库：用**受限角色**（故障注入脚本与 Agent 运行时都用它，权限被刻意收窄）
    database_url: str = ""

    # 连接池的**正常配置项**（不是故障旋钮）
    db_pool_size: int = 20
    db_pool_timeout_sec: float = 3.0

    # 下游地址（宿主视角，由启动器注入；缺省表示该服务不调用它）
    downstream: Mapping[str, str] = field(default_factory=dict)


def load_settings(service: str, env: Optional[Mapping[str, str]] = None) -> BizSettings:
    """装载配置。缺必需项或取值非法 → 抛 :class:`ConfigError`（启动即失败）。"""
    if service not in SERVICE_PORTS:
        raise ConfigError(f"未知服务 {service!r}，可选：{sorted(SERVICE_PORTS)}")

    e: Mapping[str, str] = os.environ if env is None else env
    needs_db = service != "gateway"

    database_url = ""
    if needs_db:
        raw = e.get("RUNNER_DATABASE_URL")
        if not raw or not str(raw).strip():
            raise ConfigError(
                f"缺少必需配置项 RUNNER_DATABASE_URL（{service} 需要连接池）"
            )
        database_url = str(raw).strip()

    downstream: dict[str, str] = {}
    for target in SERVICES[service]:
        url = _opt(e.get(f"{target.upper()}_URL"))
        if not url:
            raise ConfigError(
                f"缺少必需配置项 {target.upper()}_URL（{service} 需要调用 {target}）"
            )
        downstream[target] = url

    return BizSettings(
        service=service,
        port=SERVICE_PORTS[service],
        log_level=(_opt(e.get("LOG_LEVEL")) or "INFO").upper(),
        database_url=database_url,
        db_pool_size=_as_positive_int(
            e.get("DB_POOL_SIZE", "20"), "DB_POOL_SIZE"
        ),
        db_pool_timeout_sec=_as_positive_float(
            e.get("DB_POOL_TIMEOUT_SEC", "3"), "DB_POOL_TIMEOUT_SEC"
        ),
        downstream=downstream,
    )
