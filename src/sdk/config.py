"""SDK 配置装载：把一份项目级配置读成 `SdkConfig`。

★ **SDK 不维护自己的配置文件**。一份**项目级配置**（`src/observed/app.yaml`）
  同时装着业务参数与 SDK 的接入清单，两者的边界是：

      业务段（db / logging / services）  →  业务的运行参数
      aiops 段                          →  SDK 怎么把它接进来

  于是"哪份配置说了算"这个问题不存在了 —— 只有一份。

★ 三种取值来源的优先级（沿用项目既有约定，环境变量最优先）：

      环境变量  >  项目级配置文件  >  内置默认值

  业务参数通过 `env_bridge` 落到环境变量（见 `launcher.apply_env_bridge`），
  所以"shell 里临时覆盖"依然有效 —— 用于演练/调试。

★ 格式按扩展名分派：`.yaml/.yml` → PyYAML，`.toml` → `tomllib`（标准库）。
  支持两种不是为了花哨：TOML 无第三方依赖，YAML 能自然承载嵌套的接入清单。

★ `${路径}` 引用：`"http://127.0.0.1:${services.inventory.port}"`。
  单一来源靠它 —— 端口只在业务段声明一次，所有引用它的地方一起变。
  ★ 变更留痕 diff 的是**解析后的值**，所以引用链上的改动照样会被识别。
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Mapping, Optional

__all__ = [
    "ConfigError",
    "SdkConfig",
    "load_config",
    "flatten",
    "resolve_refs",
    "DEFAULT_CONFIG_NAMES",
]

# 在**项目根**（= 启动时的工作目录）里按顺序找的约定文件名
DEFAULT_CONFIG_NAMES = ("app.yaml", "app.yml", "app.toml", "aiops-sdk.yaml")

# 也可以用环境变量指定配置文件（容器里挂到任意位置时用）
ENV_CONFIG_PATH = "AIOPS_SDK_CONFIG"
# 也可以用环境变量指定服务名（容器里 `--service` 不方便传时用）
ENV_SERVICE = "AIOPS_SDK_SERVICE"

# 「环境变量 > 配置文件」的映射：环境变量名 -> (段, 键)
ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "AIOPS_SDK_SERVICE_NAME": ("service", "name"),
    "AIOPS_SDK_HOST": ("service", "host"),
    "AIOPS_SDK_PORT": ("service", "port"),
    "AIOPS_SDK_LOG_DIR": ("logging", "dir"),
    "AIOPS_SDK_LOG_LEVEL": ("logging", "level"),
    "AIOPS_SDK_OTLP_ENDPOINT": ("tracing", "otlp_endpoint"),
    "AIOPS_SDK_POOL_DRIVER": ("pool", "driver"),
    # ★ 身份三件套：取值的**唯一定义处**是 `deploy/.env`，
    #   由 `run.ps1` 读进环境；这里只是把它接进配置。
    #   取不到就是空 —— SDK **不自造默认值**（造出来的值会让"未知"分不开）。
    "AIOPS_CLUSTER": ("identity", "cluster"),
    "AIOPS_ENV": ("identity", "env"),
    "AIOPS_NAMESPACE": ("identity", "namespace"),
}

# 可以在 `[pool].driver` 里声明的驱动名（**只有 asyncpg 有适配器**：
# 其余的在运行期报 0 + 告警一次，绝不静默）
KNOWN_POOL_DRIVERS = ("asyncpg", "psycopg_pool", "sqlalchemy")

# 顶层**不属于业务参数**的段：它们不进扁平视图（也就不会进配置指纹/变更 diff）。
#  ★ `aiops` = 接入清单（工具读它，业务不读它）；
#  ★ `deployment` = 部署身份（集群名/环境/命名空间），是**事实**不是**业务配置**。
#    身份按 `doc["deployment"]` 原样读，所以排除它不影响取值。
_NON_BUSINESS_SECTIONS: Final[frozenset[str]] = frozenset({"aiops", "deployment", "observed-name"})

# 变更留痕的默认来源（可用取值：gitops / manual / agent / scenario_script）
DEFAULT_CHANGE_SOURCE = "manual"

# 值里出现这些子串视为敏感 → 写进 change_events 前打码
DEFAULT_REDACT_PATTERNS = ("password", "passwd", "secret", "token", "api_key", "apikey")

# 状态文件默认目录（存"上一次的全量快照"，用于下次 diff）
DEFAULT_STATE_DIR = ".run/sdk-state"

_INTERPOLATION = re.compile(r"\$\{([^}]+)\}")


class ConfigError(Exception):
    """配置不合法或缺少必需项。**在启动阶段抛出**（fail-fast）。

    一个"缺服务名也能起来"的 SDK 会产出 `service=""` 的指标 ——
    那种数据比没有数据更坏：它看起来有效，却什么都关联不上。
    """


# ------------------------------------------------------------------
#  读文件
# ------------------------------------------------------------------
def _read_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(
            f"找不到配置文件 {path} —— 用 --config 指定，"
            f"或设置环境变量 {ENV_CONFIG_PATH}（约定名：{list(DEFAULT_CONFIG_NAMES)}）"
        )
    suffix = path.suffix.lower()
    try:
        if suffix in (".yaml", ".yml"):
            import yaml  # noqa: PLC0415 —— 只有用 YAML 时才需要它

            with path.open("r", encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
        elif suffix == ".toml":
            with path.open("rb") as fh:
                doc = tomllib.load(fh)
        else:
            raise ConfigError(
                f"不支持的配置格式 {suffix!r}（{path}）—— 支持 .yaml/.yml/.toml"
            )
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 解析失败要指明文件与原因
        raise ConfigError(f"配置文件 {path} 解析失败：{type(exc).__name__}: {exc}") from exc

    if doc is None:
        raise ConfigError(f"配置文件 {path} 是空的")
    if not isinstance(doc, dict):
        raise ConfigError(
            f"配置文件 {path} 的顶层必须是「键: 值」表，实际是 {type(doc).__name__}"
        )
    return doc


# ------------------------------------------------------------------
#  扁平化与 ${} 引用
# ------------------------------------------------------------------
def flatten(doc: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """把嵌套的 dict 压成「点号路径 → 标量」的平面表。

    ★ 为什么必须先扁平化：变更 `diff` 要求
      `field[] / old_value[] / new_value[]` 三个**对齐的数组** ——
      它们天然就是"键路径 → 值"的形态。不扁平化就没法逐键 diff
      （只能整块比较，那样一个字段变了会显示成整段变了）。
    """
    out: dict[str, Any] = {}
    for key, value in doc.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            out.update(flatten(value, path))
        else:
            out[path] = value
    return out


def _substitute(text: str, table: Mapping[str, Any], key: str,
                _seen: tuple[str, ...] = ()) -> str:
    """把 `text` 里的 `${路径}` 换成 `table` 里的值（**唯一实现**，链式引用也走这里）。

    ★ 循环引用必须报错而不是无限递归 —— 否则一个配置笔误会让进程卡死，
      而报错能让人立刻知道是哪几个键互相引用。

    ★ `_seen` 是"当前这条引用链"，只在链式解析时非空：`resolve_in_tree` 与
      `resolve_refs` 都从空链开始，链上的每一跳各自续上自己。
    """
    def _sub(match: "re.Match[str]") -> str:
        ref = match.group(1).strip()
        if ref in _seen:
            chain = " -> ".join((*_seen, ref))
            raise ConfigError(f"配置存在循环引用：{chain}")
        if ref not in table:
            raise ConfigError(
                f"{key} 引用了不存在的键 {ref!r}（写成了 ${{PATH}} 的形式）"
            )
        target = table[ref]
        if isinstance(target, str) and "${" in target:
            target = _substitute(target, table, ref, (*_seen, ref))
        return str(target)

    return _INTERPOLATION.sub(_sub, text)


def resolve_refs(flat: Mapping[str, Any], *, _seen: Optional[tuple[str, ...]] = None) -> dict[str, Any]:
    """把**扁平表**里的 `${点号路径}` 换成引用的值（逐键调 `_substitute`，口径同一处）。"""
    seen = _seen or ()
    out: dict[str, Any] = {}
    for key, value in flat.items():
        if isinstance(value, str) and "${" in value:
            value = _substitute(value, flat, key, seen)
        out[key] = value
    return out


def resolve_in_tree(node: Any, table: Mapping[str, Any], key: str = "") -> Any:
    """对**嵌套结构**递归解析 `${路径}`（用于 `aiops` 段）。

    ★ 为什么两处都要解析：`${}` 是**单一来源**的手段，业务段用它消除
      "端口写多遍"，接入段也要用它（`port: "${services.order.port}"`、
      `ready: "${services.order.downstream.inventory}/health"`）。
      引用表统一取**业务段的扁平视图** —— 接入段不允许引用自己（那会绕成环）。
    """
    if isinstance(node, Mapping):
        return {k: resolve_in_tree(v, table, f"{key}.{k}" if key else str(k))
                for k, v in node.items()}
    if isinstance(node, list):
        return [resolve_in_tree(v, table, key) for v in node]
    if isinstance(node, str) and "${" in node:
        return _substitute(node, table, key)
    return node


# ------------------------------------------------------------------
#  取值转换
# ------------------------------------------------------------------
def _require_str(where: str, what: str, value: Any) -> str:
    if value is None or not str(value).strip():
        raise ConfigError(f"{where} 缺少必需项 {what}")
    return str(value).strip()


def _as_port(value: Any, where: str) -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where} 的 port 取值非法：{value!r}") from exc
    if not (1 <= port <= 65535):
        raise ConfigError(f"{where} 的 port 超出范围：{port}")
    return port


def _as_str_map(value: Any, where: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where} 必须是「键 = 值」表，实际是 {type(value).__name__}")
    return {str(k).strip(): str(v).strip() for k, v in value.items()}


# ------------------------------------------------------------------
#  配置对象
# ------------------------------------------------------------------
@dataclass(frozen=True)
class SdkConfig:
    """一个服务在本次启动时生效的全部接入信息。"""

    service_name: str
    app_ref: str
    host: str = "127.0.0.1"
    port: int = 8000

    log_dir: Optional[Path] = None
    log_level: str = "INFO"
    otlp_endpoint: str = ""

    # target 名 -> 实际 URL（已解析业务段的引用）
    downstream: Mapping[str, str] = field(default_factory=dict)
    # ★ 同伴清单：**其它**被观测服务的 `名字 -> (host, port)`（不含自己）。
    #   来源是同一份项目级配置里的全量 `aiops.services` —— 所以任意一个服务
    #   都能回答"全系统有哪些服务"（`GET /services`）。
    #   自己不在其中：自身存活由"本请求正被执行"证明，无需 HTTP 自探（会死锁/误报）。
    peer_services: Mapping[str, tuple[str, int]] = field(default_factory=dict)
    pool_driver: Optional[str] = None
    ready: Mapping[str, str] = field(default_factory=dict)
    operations: Mapping[str, str] = field(default_factory=dict)

    # 变更留痕
    env_bridge: Mapping[str, str] = field(default_factory=dict)   # 业务段键 -> 环境变量名
    changes_source: str = DEFAULT_CHANGE_SOURCE
    changes_scenario_id: str = ""
    changes_state_dir: Path = Path(DEFAULT_STATE_DIR)
    changes_redact: tuple[str, ...] = DEFAULT_REDACT_PATTERNS

    # 部署身份 —— ★ 全部可空：取不到就是空，**不自造默认值**。
    #   取值顺序：项目级 `deployment:` 段 → 服务段 `identity:` 段 → 环境变量
    #   （`AIOPS_CLUSTER` / `AIOPS_ENV` / `AIOPS_NAMESPACE`，其唯一取值处是 `deploy/.env`）。
    cluster: Optional[str] = None
    env: Optional[str] = None
    namespace: Optional[str] = None

    # 业务段的扁平视图（diff 的数据源）
    business: Mapping[str, Any] = field(default_factory=dict)

    config_path: Optional[Path] = None
    observed_name: str = ''
    registration_url: str = ''
    sdk_advertise_url: str = ''
    registration_token_env: str = 'AIOPS_SDK_REGISTRATION_TOKEN'
    business_advertise_url: str = ''
    active_probe: Mapping[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    @property
    def depends_on(self) -> tuple[str, ...]:
        """本服务声明的**下游服务名**（`/health` 的 `depends_on`）。

        ★ 直接来自 `downstream` 的**键**（配置里已经有的事实），不额外维护一份清单。
          没有下游就是空元组 —— 它进 `/health` 时序列化成 `[]`。
        """
        return tuple(sorted(str(name) for name in self.downstream))

    def identity(self) -> dict[str, Optional[str]]:
        """身份三件套（`cluster`/`env`/`namespace`），**空就是空**。"""
        return {"cluster": self.cluster, "env": self.env, "namespace": self.namespace}

    # ------------------------------------------------------------------
    def target_for(self, url: str) -> Optional[str]:
        """按**最长前缀**把请求 URL 映射成 target 名。

        ★ 用最长前缀而不是"第一个命中"：配置里同时有
          `http://h:80` 与 `http://h:8082` 时，短的那个会误吃掉长的。
        """
        best: Optional[str] = None
        best_len = -1
        for name, target_url in self.downstream.items():
            if url.startswith(target_url) and len(target_url) > best_len:
                best, best_len = name, len(target_url)
        return best

    def watched_business_keys(self) -> dict[str, Any]:
        """**本服务真正用到的业务参数**（= `env_bridge` 声明过的那些键）。

        ★ 这是 diff 的范围，也是它的全部意义：

          - `env_bridge` 是"本服务真的要用这些参数"的**声明**，不是猜的
          - 所以 diff 它就等于 diff"**本服务实际生效的运行参数**"
          - 别的服务的参数变了**不会**记到本服务头上（否则就是假相关）
        """
        return {key: self.business.get(key) for key in sorted(self.env_bridge)}


# ------------------------------------------------------------------
#  装载
# ------------------------------------------------------------------
def _find_config_path(explicit: Optional[str], env: Mapping[str, str]) -> Path:
    raw = explicit or env.get(ENV_CONFIG_PATH)
    if raw:
        return Path(raw).resolve()
    cwd = Path.cwd()
    for name in DEFAULT_CONFIG_NAMES:
        candidate = cwd / name
        if candidate.exists():
            return candidate.resolve()
    raise ConfigError(
        f"在 {cwd} 下没找到项目级配置文件（找过 {list(DEFAULT_CONFIG_NAMES)}）。"
        f"请在项目根启动，或用 --config 指定，或设置 {ENV_CONFIG_PATH}"
    )


def load_config(
    path: Optional[os.PathLike[str] | str] = None,
    *,
    service: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> SdkConfig:
    """装载某个服务的接入配置。缺必需项或取值非法 → 抛 :class:`ConfigError`。"""
    e: Mapping[str, str] = os.environ if env is None else env
    source = _find_config_path(str(path) if path else None, e)
    doc = _read_document(source)
    where = str(source)

    services_doc = doc.get("aiops")
    if not isinstance(services_doc, Mapping) or not isinstance(services_doc.get("services"), Mapping):
        raise ConfigError(
            f"配置文件 {where} 里没有 `aiops.services` 段 —— "
            f"SDK 的接入清单应当作为这一段写进项目级配置（不是另一份文件）"
        )
    all_services: Mapping[str, Any] = services_doc["services"]

    name = (service or e.get(ENV_SERVICE) or "").strip()
    if not name:
        if len(all_services) == 1:
            name = next(iter(all_services))
        else:
            raise ConfigError(
                f"该项目配置里有 {len(all_services)} 个服务的接入段 "
                f"（{sorted(all_services)}），必须用 --service 指定跑哪个"
            )
    if name not in all_services:
        raise ConfigError(f"配置里没有服务 {name!r} 的接入段（可选：{sorted(all_services)}）")

    section = all_services[name]
    if not isinstance(section, Mapping):
        raise ConfigError(f"aiops.services.{name} 必须是表")

    app_ref = _require_str(f"aiops.services.{name}", "app", section.get("app"))

    # ---- 业务段：扁平化 + 解析 ${} 引用（`aiops` / `deployment` 段不算业务段）----
    #  ★ `deployment:`（集群名/环境/命名空间）是**部署身份**，不是业务参数：
    #    它若进了扁平视图，就会跟着进 `config_fingerprint` 和各路变更 diff ——
    #    于是"集群名从空变成 compose-cn-1"会被算成一次业务配置变更。
    #    它与身份取值无关：下面按 `doc["deployment"]` **原样**读，与扁平视图无关。
    business_doc = {k: v for k, v in doc.items() if k not in _NON_BUSINESS_SECTIONS}
    flat = resolve_refs(flatten(business_doc))

    # ---- 环境变量覆盖（优先级最高）----
    #  只覆盖 `service / logging / tracing / pool` 这几段的键；
    #  ★ **不覆盖 `env_bridge`**：它声明的是"业务参数从哪来"，
    #    属于接入契约，不该被环境里一个同名变量意外改掉。
    merged: dict[str, Any] = dict(section)
    for env_name, (sect, key) in ENV_OVERRIDES.items():
        raw = e.get(env_name)
        if not raw or not str(raw).strip():
            continue
        nested = dict(merged.get(sect) or {})
        nested[key] = str(raw).strip()
        merged[sect] = nested
    section = merged

    # ★ 接入段里的 `${}` 也在这里解析（引用表 = 业务段的扁平视图）
    section = resolve_in_tree(section, flat, f"aiops.services.{name}")
    if not isinstance(section, Mapping):
        raise ConfigError(f"aiops.services.{name} 必须是表")

    service_doc = section.get("service") if isinstance(section.get("service"), Mapping) else {}
    logging_doc = section.get("logging") if isinstance(section.get("logging"), Mapping) else {}
    tracing_doc = section.get("tracing") if isinstance(section.get("tracing"), Mapping) else {}
    pool_doc = section.get("pool") if isinstance(section.get("pool"), Mapping) else {}
    changes_doc = section.get("changes") if isinstance(section.get("changes"), Mapping) else {}

    # 端口：优先 `service` 子段的显式值（**含 `AIOPS_SDK_PORT` 覆盖**），
    # 其次 `section` 顶层的显式值，最后业务段 `${...}` 解析后的值。
    # ★★ 以前只读 `section.get("port")`（顶层），而环境变量覆盖写进的是
    #   `merged["service"]["port"]` → `AIOPS_SDK_{SERVICE_NAME,HOST,PORT}` **静默失效**：
    #   写入落在子表、读取却全在顶层，本该承接它的 `service_doc` 是从不使用的死变量。
    #   → 三个键统一"先 `service` 子段、再顶层"（配置文件两种写法都继续有效）。
    port_raw = service_doc.get("port")
    if port_raw is None:
        port_raw = section.get("port")
    if port_raw is None:
        port_raw = flat.get(f"services.{name}.port", 8000)
    log_dir_raw = section.get("log_dir") or logging_doc.get("dir")

    # ---- downstream：target 名 -> 业务段里的 URL 键（单一来源）----
    downstream: dict[str, str] = {}
    for target, ref in _as_str_map(section.get("downstream"), f"aiops.services.{name}.downstream").items():
        if ref.startswith("http://") or ref.startswith("https://"):
            # 直接写 URL 也允许（灵活性），但推荐写业务段的键
            downstream[target] = ref
            continue
        resolved = flat.get(ref)
        if resolved is None:
            raise ConfigError(
                f"aiops.services.{name}.downstream.{target} 指向业务段键 {ref!r}，"
                f"但业务段里没有这个键"
            )
        downstream[target] = str(resolved)

    # ---- ready：`pool` 或 http(s) URL（已在 YAML 里用 ${} 解析好）----
    ready = _as_str_map(section.get("ready"), f"aiops.services.{name}.ready")

    env_bridge = _as_str_map(section.get("env_bridge"), f"aiops.services.{name}.env_bridge")
    unknown = [k for k in env_bridge if k not in flat]
    if unknown:
        raise ConfigError(
            f"aiops.services.{name}.env_bridge 引用了业务段里不存在的键：{unknown}"
        )

    state_dir_raw = changes_doc.get("state_dir") or DEFAULT_STATE_DIR
    redact = changes_doc.get("redact") or list(DEFAULT_REDACT_PATTERNS)

    # ---- 同伴清单：全量 `aiops.services` 里**除自己以外**的服务 ----
    #   ★ 每个同伴的 `${}` 也要解析（它们的 port 通常写成 `${services.x.port}`）。
    #     解析不了就是配置真有错 —— **报错而不是跳过**：静默少一个同伴会让
    #     `/services` 假装"系统就这么大"，而这正是该端点要消灭的假象。
    peers: dict[str, tuple[str, int]] = {}
    for other, raw_other in all_services.items():
        if str(other) == name:
            continue
        if not isinstance(raw_other, Mapping):
            raise ConfigError(f"aiops.services.{other} 必须是表")
        resolved_other = resolve_in_tree(raw_other, flat, f"aiops.services.{other}")
        if not isinstance(resolved_other, Mapping):
            raise ConfigError(f"aiops.services.{other} 必须是表")
        other_port = resolved_other.get("port")
        if other_port is None:
            other_port = flat.get(f"services.{other}.port", 8000)
        peers[str(other)] = (
            str(resolved_other.get("host") or "127.0.0.1").strip(),
            _as_port(other_port, f"aiops.services.{other}"),
        )

    # ---- 部署身份：项目级 `deployment` → 服务段 `identity` → 环境变量 ----
    #
    #  ★ 为什么先合并两层再取值：集群名/环境/命名空间是**部署事实**，
    #    对同一个被观测系统里的所有服务都一样，所以放项目级 `deployment` 段最自然
    #    （"同一事实只留一份"）；某个服务要单独特例时，用自己的 `identity` 段覆盖。
    identity_doc: dict[str, Any] = {}
    if isinstance(doc.get("deployment"), Mapping):
        identity_doc.update(doc["deployment"])
    if isinstance(section.get("identity"), Mapping):
        identity_doc.update(section["identity"])

    def _identity(key: str) -> Optional[str]:
        raw_value = str(identity_doc.get(key) or "").strip()
        return raw_value or None            # ★ 空就是 None，不发明默认值

    return SdkConfig(
        # ★★ 同样先读 `service` 子段（`AIOPS_SDK_SERVICE_NAME` 覆盖落在这里）
        service_name=str(service_doc.get("name") or section.get("name") or name).strip(),
        app_ref=app_ref,
        host=str(service_doc.get("host") or section.get("host") or "127.0.0.1").strip(),
        port=_as_port(port_raw, where),
        log_dir=Path(str(log_dir_raw)).resolve() if log_dir_raw else None,
        log_level=str(logging_doc.get("level") or "INFO").strip().upper(),
        otlp_endpoint=str(tracing_doc.get("otlp_endpoint") or "").strip(),
        downstream=downstream,
        peer_services=peers,
        pool_driver=str(pool_doc.get("driver")).strip() if pool_doc.get("driver") else None,
        ready=ready,
        operations=_as_str_map(section.get("operations"), f"aiops.services.{name}.operations"),
        env_bridge=env_bridge,
        changes_source=str(changes_doc.get("source") or DEFAULT_CHANGE_SOURCE).strip(),
        changes_scenario_id=str(changes_doc.get("scenario_id") or "").strip(),
        changes_state_dir=(Path(str(state_dir_raw)).resolve()),
        changes_redact=tuple(str(p).lower() for p in redact),
        cluster=_identity("cluster"),
        env=_identity("env"),
        namespace=_identity("namespace"),
        business=flat,
        config_path=source,
        **_registration_settings(doc,section,name,port_raw,e),
    )


def _registration_settings(doc,section,name,port,env):
    from .registration import settings
    return settings(doc,section,name,port,env)
