"""被观测系统探测器：**一次** `GET /services` 拿到存活服务清单，维护 `observed_info.yaml`。

--------------------------------------------------------------------------------
★ 为什么需要它（用户要求：服务清单不该硬编码）
--------------------------------------------------------------------------------

被观测服务清单本来是**部署事实**，会变（加服务、改端口、临时下线）。
写死在 `slots.yaml` 里就等于把"当前这套业务"焊进意图识别 ——
而 SDK 的目标恰恰是"接一个**干净的外来后端**"。

--------------------------------------------------------------------------------
★ 信息从哪来（这是本文件最要紧的设计，不是随手挑的）
--------------------------------------------------------------------------------

被观测系统自己提供**服务清单端点**：

```
GET http://<任意一个服务>/services
→ 200 {"status":"ok|degraded", "service":"gateway", "declared_count":4, "alive_count":4,
       "services":   [{"name":"gateway","declared_as":"gateway","url":"...","status":200,...}, ...],
       "unreachable":[{"name":"payment","declared_as":"payment","url":"...","detail":"...",...}]}
```

于是算法缩成一句话：**敲一扇门，拿全系统**。

```
种子（先敲哪扇门） → GET /services → 解析出全部存活服务
                      （第一个应答的种子就够了，后面的种子不再打扰）
```

★ 三条刻意的取舍：

1. **探测逻辑归被观测系统，不归观测方**。清单的权威副本是它的接入配置
   （SDK 读项目级配置里的全量 `aiops.services` 段），所以任意一个服务都能回答全量清单；
   Agent 不需要知道"谁能当种子"、也不需要理解拓扑，被观测系统扩缩容时 Agent **零改动**。
2. **种子只提供"入口地址"，不提供"服务名"**。种子里写的名字若与 SDK 自报的不一致
   （比如写成 `payment-svc` 而 SDK 说 `payment`），**以自报为准**并记下 `name_mismatch`
   —— 名字是被观测系统的事实，不是我们的猜测。
3. **本文件不再推导拓扑**。曾经用 `GET /ready` 的下游字段做广度优先发现 ——
   那是**借用字段**（`/ready` 的语义是"我能不能接流量"，不是"谁是我的下游"），
   而且给每个服务多加一次往返。服务间的依赖关系以后从**链路**里读，
   那是它的正主来源。所以本文件**不推导** `depends_on`：该字段只做**照抄**
   （对方在 `/health` / `/services` 里自报什么就记什么，自报没有就是空）。

--------------------------------------------------------------------------------
★ 种子（seed）从哪来
--------------------------------------------------------------------------------

| 顺序 | 来源 | 用途 |
|---|---|---|
| 1 | 环境变量 `AIOPS_OBSERVED_SEED`（`name=url,name=url`） | **外部业务后端**（SDK 的目标场景） |
| 2 | 同仓 `src/observed/app.yaml` 的 `services.*.port`（宿主回环 127.0.0.1） | 本项目的开发便利 |

★ 只有"入口"有两个来源，**服务清单只有一个**（落盘那份）—— 两回事，不冲突。

★ 兜底：若所有种子的 `/services` 都不可用（例如被观测系统还没升级到带该端点的 SDK），
  退化成只问种子自己的 `/health`，并把降级原因写进 `seed_source` ——
  **降级要响**（清单大概率不全，读完就知道），而不是假装清单是全的。

--------------------------------------------------------------------------------
★ 输出文件是**生成物**
--------------------------------------------------------------------------------

`src/agent/observed_info.yaml` 由本探测器整份重写：**不要手改**（下次探测会覆盖）。
它入库，是为了"冷启动兜底 + 变更可 review"（否则首次启动 slots 就无表可读）。
★ **一个存活服务都探不到时不写文件** —— 宁可用上一次的已知良好结果，
  也不要用一份"全是不可达"的清单把服务词表污染掉。

用法::

    python -m agent.discovery                       # 探一次，写文件
    python -m agent.discovery --watch --interval 30  # 每 30s 探一次
    .\\run.ps1 observed-discover                      # 同上（run.ps1 封装）

★ 入口用 `-m agent.discovery`（走 `__main__.py`）而不是 `-m agent.discovery.probe`：
  后者会让解释器警告"包已 import 该模块，又把它当 __main__ 执行"。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Iterable, Mapping, Optional

import httpx
import yaml

__all__ = [
    "ObservedService",
    "ObservedInfo",
    "ObservedInfoError",
    "probe",
    "load_seed",
    "write_observed_info",
    "load_observed_info",
    "OBSERVED_INFO_FILE",
    "SERVICES_PATH",
    "HEALTH_PATH",
    "DEFAULT_TIMEOUT_SEC",
    "DEFAULT_INTERVAL_SEC",
    "MAX_SERVICES",
]

_SUPPORTED_VERSION: Final[int] = 1

#: 服务清单端点 —— **唯一**需要敲的门
SERVICES_PATH: Final[str] = "/services"
#: 兜底用：存活探针（同时自报服务名）
HEALTH_PATH: Final[str] = "/health"

#: 落盘位置：`<repo>/src/agent/observed_info.yaml`（本文件在 `<repo>/src/agent/discovery/`）
OBSERVED_INFO_FILE: Final[Path] = Path(__file__).resolve().parents[1] / "observed_info.yaml"

#: 本仓的种子来源（被观测系统的声明式配置；端口即部署事实）
REPO_OBSERVED_CONFIG: Final[Path] = Path(__file__).resolve().parents[2] / "observed" / "app.yaml"
SEED_HOST: Final[str] = "127.0.0.1"

#: 客户端超时。★ 必须**大于** SDK 内部探测同伴的超时（2s）：
#: `/services` 要等它并发问完所有同伴才回，超时给太小会把"清单接口稍慢"
#: 误判成"服务死了" —— 观测方自己制造的假故障。
DEFAULT_TIMEOUT_SEC: Final[float] = 6.0
DEFAULT_INTERVAL_SEC: Final[float] = 30.0
#: 防御性上限：探测器自己不该被"无限发现"拖垮（正常规模是 4，给到 50 足够）
MAX_SERVICES: Final[int] = 50


class ObservedInfoError(Exception):
    """`observed_info.yaml` 不合法（或缺失）—— fail-fast，别拿半份清单开工。"""


# ======================================================================
#  数据模型
# ======================================================================
@dataclass(frozen=True)
class ObservedService:
    """一个被观测服务的**事实**（名字来自 SDK 自报，URL 来自清单端点）。"""

    name: str                                    # ★ SDK 自报的名字（权威）
    url: str
    alive: bool
    self_reported_name: Optional[str] = None
    declared_as: Optional[str] = None            # 被观测系统配置里的键（可能≠自报，差异即信号）
    #: "self"（回答者自身）/ "services"（SDK 探到的同伴）/ "unreachable" / "seed-health"（兜底）
    discovered_via: str = "services"
    health_ms: Optional[float] = None
    error: Optional[str] = None                  # 探不通时记原因（不静默）
    name_mismatch: Optional[str] = None          # 配置键 ≠ SDK 自报的名字
    note: Optional[str] = None                   # 例如自身那条"为什么不 HTTP 自探"
    # ---- 部署身份与依赖 —— ★ 全部可空：自报没给就是 None/() ----
    cluster: Optional[str] = None
    env: Optional[str] = None
    namespace: Optional[str] = None
    depends_on: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "url": self.url,
            "alive": self.alive,
        }
        if self.self_reported_name is not None:
            out["self_reported_name"] = self.self_reported_name
        if self.declared_as and self.declared_as != self.name:
            out["declared_as"] = self.declared_as
        out["discovered_via"] = self.discovered_via
        if self.health_ms is not None:
            out["health_ms"] = round(float(self.health_ms), 1)
        if self.note:
            out["note"] = self.note
        if self.name_mismatch:
            out["name_mismatch"] = self.name_mismatch
        if self.error:
            out["error"] = self.error
        # ★ 身份与依赖**照抄自报**（缺失就不写这个键，或写 null/[]）：
        #   下游据此做实体对齐与拓扑；"没报"与"报了个默认值"必须能分开。
        for key in ("cluster", "env", "namespace"):
            value = getattr(self, key)
            out[key] = str(value) if value else None
        out["depends_on"] = list(self.depends_on)
        return out


@dataclass(frozen=True)
class ObservedInfo:
    """落盘内容的完整形态（AIOps 侧**唯一**的服务信息数据源）。"""

    probed_at: str
    seed_source: str
    services: tuple[ObservedService, ...]
    version: int = _SUPPORTED_VERSION

    @property
    def alive_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.services if s.alive)

    @property
    def names(self) -> tuple[str, ...]:
        """服务词表 —— **与 `alive_names` 同源**（下游 `slots.py` 用的是它）。

        ★★ 不变量：这里只可能出现存活服务。`probe()` 的 `/health` 兜底路径已经
          把 `alive=False` 的种子滤掉，`_parse_services_doc` 只产出存活条目 ——
          所以"死名字变成可选服务"这件事在入口就被挡住了，而不是靠消费方自觉。
        """
        return self.alive_names

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "probed_at": self.probed_at,
            "seed_source": self.seed_source,
            "services": [s.to_dict() for s in self.services],
        }

    def to_yaml(self) -> str:
        header = (
            "# ==================================================================\n"
            "#  observed_info.yaml —— 被观测服务的**存活清单**\n"
            "#\n"
            "#  ★ 本文件是**生成物**：由 `python -m agent.discovery` 整份重写，\n"
            "#    请**不要手改**（下一次探测会覆盖）。要改服务清单，改被观测系统。\n"
            "#\n"
            "#  ★ 它是 AIOps 侧**唯一**的服务信息来源：\n"
            "#    意图识别的槽位抽取（slots.py）、后续的取证都从这里读，\n"
            "#    不再各自维护一份服务清单（\"同一事实只留一份\"）。\n"
            "#\n"
            "#  ★ 内容来自被观测系统的 **服务清单端点** `GET /services`：\n"
            "#    `services[].name` 是**对方自报**的名字（权威）；\n"
            "#    `seed_source` 记着这次是从哪扇门、以哪种方式问出来的。\n"
            "#    只列**存活**服务 —— 不可达的不会出现在这里（清单里没有它 = 它现在不在）。\n"
            "#\n"
            "#  ★ 身份与依赖（`cluster`/`env`/`namespace`/`depends_on`）同样来自**对方自报**\n"
            "#    （SDK 在 `/health` 里报、`/services` 汇总）：\n"
            "#    · `null` / `[]` = **对方没报**（可能是老版本 SDK，也可能是真的没配置）\n"
            "#      —— 这是**合法形态**，不是错误；\n"
            "#    · ★ Agent **不会**为缺失的身份发明取值（发明出来的值会让\n"
            "#      「未知」与「真的是这个值」再也分不开）。集群名取值见 `common/identity.py`。\n"
            "# ==================================================================\n"
        )
        return header + yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=False)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


# ======================================================================
#  种子
# ======================================================================
def _seed_from_env(raw: str) -> list[tuple[str, str]]:
    """解析 `AIOPS_OBSERVED_SEED`：`gateway=http://127.0.0.1:8080,order=http://...`。"""
    pairs: list[tuple[str, str]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, sep, url = chunk.partition("=")
        if not sep or not name.strip() or not url.strip():
            raise ObservedInfoError(
                f"AIOPS_OBSERVED_SEED 的片段 {chunk!r} 格式不对 —— "
                f"应为 `name=http://host:port`（多个用逗号分隔）"
            )
        pairs.append((name.strip(), url.strip().rstrip("/")))
    return pairs


def _seed_from_repo_config(path: Path) -> list[tuple[str, str]]:
    """读同仓 `src/observed/app.yaml` 的 `services.*.port`，拼成宿主回环地址。"""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except FileNotFoundError:
        return []
    services = doc.get("services") or {}
    if not isinstance(services, dict):
        return []
    pairs: list[tuple[str, str]] = []
    for name, cfg in services.items():
        port = (cfg or {}).get("port") if isinstance(cfg, dict) else None
        if port:
            pairs.append((str(name), f"http://{SEED_HOST}:{int(port)}"))
    return pairs


def load_seed(env: Optional[dict[str, str]] = None) -> tuple[list[tuple[str, str]], str]:
    """返回 `(种子列表, 种子来源说明)`。"""
    environ = os.environ if env is None else env
    raw = (environ.get("AIOPS_OBSERVED_SEED") or "").strip()
    if raw:
        return _seed_from_env(raw), "env:AIOPS_OBSERVED_SEED"
    from_repo = _seed_from_repo_config(REPO_OBSERVED_CONFIG)
    if from_repo:
        return from_repo, f"repo:{REPO_OBSERVED_CONFIG.name}(services.*.port)"
    raise ObservedInfoError(
        "拿不到种子：既没有 AIOPS_OBSERVED_SEED，也没能从 "
        f"{REPO_OBSERVED_CONFIG} 读到 services.*.port。\n"
        "★ 接**外部**业务后端时必须显式给种子，例如：\n"
        "    $env:AIOPS_OBSERVED_SEED = 'gateway=http://127.0.0.1:8080,order=http://127.0.0.1:8081'"
    )


# ======================================================================
#  探测
# ======================================================================
async def _get(client: httpx.AsyncClient, url: str) -> tuple[int, dict[str, Any], float]:
    started = time.perf_counter()
    resp = await client.get(url)
    elapsed = (time.perf_counter() - started) * 1000
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, (body if isinstance(body, dict) else {}), elapsed


def _mismatch(name: str, declared_as: Optional[str]) -> Optional[str]:
    if declared_as and declared_as != name:
        return f"被观测系统配置里叫 {declared_as!r}，自报 {name!r}"
    return None


def _base_url(url: str) -> str:
    """把"探针地址"归一成**服务基地址**。

    ★ 契约里 `services[].url` 本就是基地址；这里是防御性归一 ——
      别的语言实现（L0 层契约）可能顺手把 `/health` 也带上，
      而我们的消费方会拿它去拼别的端点（`.../health/services` 就废了）。
    """
    trimmed = url.strip().rstrip("/")
    for suffix in (HEALTH_PATH, "/ready", "/metrics", SERVICES_PATH):
        if trimmed.endswith(suffix):
            return trimmed[: -len(suffix)]
    return trimmed


def _parse_services_doc(
    doc: dict[str, Any], *, answer_url: str = "",
) -> tuple[tuple[ObservedService, ...], str]:
    """把 `GET /services` 的响应体解析成服务事实（**只列存活**）。

    ★ 不可达的同伴**不落盘**：这份文件的语义是"现在有哪些服务"，
      一个不在的服务出现在清单里，会让槽位抽取把死名字当成可选服务。
      它们的名单与原因在探测报告里打印（人看得见），不进词表。

    Returns:
        `(服务事实, 截断说明)`；截断说明非空表示被防御性上限裁过（进 `seed_source`，
        **不静默**：清单不全与清单正常必须分得开）。
    """
    found: list[ObservedService] = []
    seen: set[str] = set()
    for item in doc.get("services") or []:
        if not isinstance(item, dict):
            continue
        declared_as = str(item.get("declared_as") or "").strip() or None
        name = str(item.get("name") or declared_as or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        # `probed: false` = 回答者自身（它不 HTTP 自探，见 sdk/probes.services_payload）
        is_self = item.get("probed") is False
        url = _base_url(str(item.get("url") or "").strip())
        if not url:
            # 自身条目按契约 url 为 null（它不知道自己对外可达的地址，探测方知道）
            # → 用**回答者的地址**（必然可达）
            url = answer_url or f"http://{SEED_HOST}"
        found.append(ObservedService(
            name=name,
            url=url,
            alive=True,
            self_reported_name=name,
            declared_as=declared_as,
            discovered_via="self" if is_self else "services",
            health_ms=item.get("latency_ms"),
            name_mismatch=_mismatch(name, declared_as),
            note=(str(item["note"]) if item.get("note") else None),
            **_identity_of(item),
        ))
    # ★★ 防御性上限**不静默**：清单被裁过就必须说出来（"降级要响"）。
    #   截断痕迹进 `_parse_services_doc` 的调用方（`seed_source` 落盘，人读得到）。
    return tuple(found[:MAX_SERVICES]), _truncation_note(len(found))


def _truncation_note(total: int) -> str:
    """被防御性上限裁掉的条数（没裁 → 空串）。"""
    dropped = total - MAX_SERVICES
    if dropped <= 0:
        return ""
    return (f" ★ 服务清单共 {total} 条，超过防御性上限 {MAX_SERVICES} → "
            f"只保留前 {MAX_SERVICES} 条（**清单不全**，被裁 {dropped} 条）")


def _identity_of(item: Mapping[str, Any]) -> dict[str, Any]:
    """从自报条目里取部署身份与依赖（**缺就是缺**，不给默认值）。

    ★ 老版本 SDK 不带这些字段 → 全部为 None/()，下游会如实报"未提供"。
      这正是"允许空"的意义：升级 SDK 前后都能跑，不会因为缺字段而崩或造假。
    """
    depends = item.get("depends_on")
    return {
        "cluster": _clean(item.get("cluster")),
        "env": _clean(item.get("env")),
        "namespace": _clean(item.get("namespace")),
        "depends_on": tuple(sorted({str(target).strip() for target in depends
                                    if str(target).strip()}))
        if isinstance(depends, (list, tuple)) else (),
    }


def _clean(value: Any) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text or None


async def _probe_health(http: httpx.AsyncClient, seed_name: str, base: str) -> ObservedService:
    """兜底路径：只问 `GET /health`（同时拿到自报名字）。"""
    try:
        status, body, elapsed = await _get(http, f"{base}{HEALTH_PATH}")
    except httpx.HTTPError as exc:
        return ObservedService(
            name=seed_name, url=base, alive=False, declared_as=seed_name,
            discovered_via="seed-health", error=f"GET {HEALTH_PATH} 失败：{type(exc).__name__}",
        )
    reported = str(body.get("service") or "").strip() or None
    alive = status == 200 and body.get("status") == "alive"
    name = reported or seed_name
    return ObservedService(
        name=name, url=base, alive=alive, self_reported_name=reported, declared_as=seed_name,
        discovered_via="seed-health", health_ms=elapsed,
        error=None if alive else f"GET {HEALTH_PATH} → HTTP {status} {str(body)[:80]}",
        name_mismatch=_mismatch(name, seed_name),
        **_identity_of(body),
    )


async def probe(
    seed: Iterable[tuple[str, str]],
    *,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    client: Optional[httpx.AsyncClient] = None,
) -> tuple[tuple[ObservedService, ...], str]:
    """探测被观测系统：**第一个应答的种子**敲一次 `/services` 就够了。

    Args:
        seed: `[(种子名, base_url), ...]` —— 只作"入口"，名字以 SDK 自报为准。
        timeout: 客户端超时（秒）。必须大于 SDK 内部探测同伴的超时。
        client: 注入用（离线单测传 `httpx.MockTransport`）。

    Returns:
        `(服务事实, 来源说明)`；来源说明会落盘成 `seed_source` 字段。

    Raises:
        ObservedInfoError: 所有种子的 `/services` 与 `/health` 都问不出东西。
    """
    seeds = list(seed)
    owns = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout), follow_redirects=False)
    problems: list[str] = []
    try:
        # ---- 一扇门：GET /services ----
        for _name, base in seeds:
            try:
                status, body, _elapsed = await _get(http, f"{base}{SERVICES_PATH}")
            except httpx.HTTPError as exc:
                problems.append(f"{base}{SERVICES_PATH} 连不上（{type(exc).__name__}）")
                continue
            if status == 200 and isinstance(body.get("services"), list):
                found, truncated = _parse_services_doc(body, answer_url=base)
                if found:
                    answerer = str(body.get("service") or "").strip() or base
                    return found, (f"{SERVICES_PATH}@{base}"
                                   f"（应答者 {answerer}，{body.get('status')}）{truncated}")
                problems.append(f"{base}{SERVICES_PATH} 回 200 但没有任何存活服务")
                continue
            problems.append(f"{base}{SERVICES_PATH} → HTTP {status}")

        # ---- 兜底：逐个 /health（被观测系统的 SDK 太旧 / 该端点被裁掉） ----
        probed = tuple([await _probe_health(http, name, base) for name, base in seeds])
        # ★★ **不存活的种子不进清单**：这份文件的语义是"现在有哪些存活服务"
        #   （文件头与 `_parse_services_doc` 都这么承诺），而下游 `slots.py` 用的是
        #   `ObservedInfo.names` —— 把 `alive=False` 写进去，死名字会变成**可选服务**，
        #   正是这份文件说要避免的事。谁被滤掉了、为什么，写进 `seed_source`。
        fallback = tuple(s for s in probed if s.alive)
        dropped = [s.name for s in probed if not s.alive]
        if fallback:
            detail = "；".join(problems[:3]) or "未记录原因"
            return fallback, (
                f"fallback:{HEALTH_PATH}（{SERVICES_PATH} 不可用：{detail}）"
                f" ★ 此时只能看见种子自己，清单**可能不全**"
                + (f"；★ 不可达被滤掉 {len(dropped)} 个：{'、'.join(dropped)}"
                   f"（**存活清单里没有它 ≠ 它没被观测过**）" if dropped else "")
            )

        raise ObservedInfoError(
            f"探测失败：{SERVICES_PATH} 与 {HEALTH_PATH} 都没问出任何东西。\n"
            + "\n".join(f"    · {p}" for p in problems[:6])
            + "\n  ★ 被观测系统在跑吗？（`docker compose ps` / `.\\run.ps1 business-up`）"
        )
    finally:
        if owns:
            await http.aclose()


def build_observed_info(services: Iterable[ObservedService], seed_source: str) -> ObservedInfo:
    return ObservedInfo(probed_at=_now_iso(), seed_source=seed_source,
                        services=tuple(services))


# ======================================================================
#  读写
# ======================================================================
def write_observed_info(info: ObservedInfo, path: Optional[Path] = None) -> Path:
    out = Path(path) if path is not None else OBSERVED_INFO_FILE
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(info.to_yaml(), encoding="utf-8")
    return out


def load_observed_info(path: Optional[Path] = None) -> ObservedInfo:
    """读回探测结果（`slots.py` 等消费方用这个）。

    ★ 缺失/为空 → `ObservedInfoError`（**fail-fast**）：
      没有服务清单就抽不出服务槽位，而"抽不出"会被下游误当成"用户没说"。
      宁可启动时就报"先跑一次探测器"。
    """
    info_path = Path(path) if path is not None else OBSERVED_INFO_FILE
    try:
        text = info_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise ObservedInfoError(
            f"观测信息不存在：{info_path}\n"
            f"★ 先跑一次探测器：`.\\run.ps1 observed-discover`"
            f"（或 `python -m agent.discovery`）"
        ) from exc
    except OSError as exc:
        raise ObservedInfoError(f"读不了 {info_path}：{exc}") from exc

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ObservedInfoError(f"{info_path} 不是合法 YAML：{exc}") from exc
    if not isinstance(doc, dict):
        raise ObservedInfoError(f"{info_path} 顶层必须是映射")
    if doc.get("version") != _SUPPORTED_VERSION:
        raise ObservedInfoError(
            f"不支持的 observed_info 版本 {doc.get('version')!r}（本实现只认 {_SUPPORTED_VERSION}）"
        )
    raw_services = doc.get("services")
    if not isinstance(raw_services, list) or not raw_services:
        raise ObservedInfoError(
            f"{info_path} 里没有任何服务 —— 上一次探测可能全军覆没；先确认被观测系统在跑"
        )

    services: list[ObservedService] = []
    for index, item in enumerate(raw_services):
        where = f"services 第 {index + 1} 项"
        if not isinstance(item, dict):
            raise ObservedInfoError(f"{where} 必须是映射")
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        if not name or not url:
            raise ObservedInfoError(f"{where} 缺 name 或 url")
        services.append(ObservedService(
            name=name,
            url=url,
            alive=bool(item.get("alive")),
            self_reported_name=(str(item["self_reported_name"])
                               if item.get("self_reported_name") else None),
            declared_as=(str(item["declared_as"]) if item.get("declared_as") else None),
            discovered_via=str(item.get("discovered_via") or "services"),
            health_ms=item.get("health_ms"),
            error=(str(item["error"]) if item.get("error") else None),
            name_mismatch=(str(item["name_mismatch"]) if item.get("name_mismatch") else None),
            note=(str(item["note"]) if item.get("note") else None),
            **_identity_of(item),
        ))
    if len({s.name for s in services}) != len(services):
        raise ObservedInfoError(f"{info_path} 里有重名的服务")

    return ObservedInfo(
        probed_at=str(doc.get("probed_at") or ""),
        seed_source=str(doc.get("seed_source") or ""),
        services=tuple(services),
    )


# ======================================================================
#  打印与调度
# ======================================================================
def render_report(info: ObservedInfo) -> str:
    lines = [
        "",
        "=" * 74,
        "  被观测系统探测结果",
        "=" * 74,
        f"  探测时间 : {info.probed_at}",
        f"  来源     : {info.seed_source}",
        f"  存活服务 : {len(info.alive_names)} / {len(info.services)}",
        "",
    ]
    for svc in info.services:
        flag = "✅" if svc.alive else "❌"
        bits = [f"  {flag} {svc.name:<12} {svc.url:<24} via={svc.discovered_via}"]
        if svc.health_ms is not None:
            bits.append(f"{float(svc.health_ms):.0f}ms")
        if svc.error:
            bits.append(f"错误={svc.error}")
        if svc.name_mismatch:
            bits.append(f"⚠ {svc.name_mismatch}")
        lines.append("  ".join(bits))
    lines.append("")
    return "\n".join(lines)


async def run_once(*, out: Optional[Path] = None, timeout: float = DEFAULT_TIMEOUT_SEC,
                   client: Optional[httpx.AsyncClient] = None) -> ObservedInfo:
    seed, seed_source = load_seed()
    services, via = await probe(seed, timeout=timeout, client=client)
    # ★ 能走到这里就说明**至少有服务在跑** —— 一个都没有时 `probe()` 已经抛错，
    #   所以这里永远不会写出"全是不可达"的清单去污染服务词表。
    info = build_observed_info(services, f"{seed_source} → {via}")
    write_observed_info(info, out)
    print(render_report(info))
    return info


async def watch(*, interval: float, out: Optional[Path], timeout: float) -> None:
    print(f"  定时探测已启动：每 {interval:g}s 一次（Ctrl+C 停止）")
    while True:
        try:
            await run_once(out=out, timeout=timeout)
        except ObservedInfoError as exc:
            # ★ 探测失败**不改坏已有文件**（保留上一次的已知良好结果），只报错
            print(f"  [!] 本次探测失败，保留上一次结果：{exc}")
        await asyncio.sleep(interval)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent.discovery",
        description="被观测系统探测器：一次 GET /services 拿到存活清单并维护 observed_info.yaml",
    )
    parser.add_argument("--watch", action="store_true", help="定时循环探测")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SEC,
                        help=f"定时间隔秒数（默认 {DEFAULT_INTERVAL_SEC:g}）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SEC,
                        help=f"客户端超时秒数（默认 {DEFAULT_TIMEOUT_SEC:g}）")
    parser.add_argument("--out", default=None, help="输出路径（默认 src/agent/observed_info.yaml）")
    args = parser.parse_args(argv)
    out = Path(args.out) if args.out else None

    try:
        if args.watch:
            asyncio.run(watch(interval=args.interval, out=out, timeout=args.timeout))
            return 0
        asyncio.run(run_once(out=out, timeout=args.timeout))
    except ObservedInfoError as exc:
        print(f"  [X] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:               # pragma: no cover
        return 0
    # 探测成功（至少一个存活服务）才走得到这里；探不到时上面已按 ObservedInfoError 返回 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
