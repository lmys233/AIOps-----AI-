"""探针的语义实现：`/health`、`/ready`、`/metrics`、`/services` 的响应体。

★ 本模块的语义最容易被做错，值得单独说清：

  | 探针 | 检查什么 | 做错的后果 |
  |---|---|---|
  | `/health` | **只检查"我这个进程还活着"** | 下游抖动会导致**误杀**，制造假故障 |
  | `/ready` | 检查自身依赖，不可用时 503 + 逐项明细 | 该摘流时不摘 |
  | `/metrics` | Prometheus 文本 | 无指标信号 |
  | `/services` | 并发探同伴的 `/health`，**只列存活** | Agent 侧只能自己维护服务清单（必然过期）或猜拓扑 |

  **前两者必须分离**：一个健康的进程（`/health` 200）完全可以处于"未就绪"
  状态（数据库断了）。把两者合成一个，K8s 就会在下游抖动时重启一个
  本来健康的进程 —— 那不是修复，那是**放大故障**。

★ `/health` 的响应体是 `{status:"alive", service:...}`。

★ `/ready` 的响应体是"逐项明细"，让人一眼看出**是哪一项没就绪**：
  只回一个 503 而不说是哪一项，排查时还得再猜一轮。

★ `/services` 是**服务清单端点**（不是探针）：它同样**不参与就绪判定** ——
  有同伴掉线也回 200 + `status:"degraded"`，否则会被当成就绪探针用，
  把"同伴掉线"升级成"自己也重启"。详见 :func:`services_payload`。

★ **身份与依赖走 `/health` 自报、由 `/services` 汇总**：
  集群/环境/命名空间与下游清单，只有**对端自己**知道（每个服务的接入配置里
  只有自己那一段），所以必须由对端说；`/services` 只是把它们并排摆出来。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Iterable, Mapping, Optional

import httpx

from .contract import IDENTITY_FIELDS, PROBE_HEALTH
from .state import Runtime

__all__ = ["health_payload", "ready_payload", "services_payload", "check_http", "check_pool"]

# 单次依赖检查的超时。取小值：探针要快，慢探针本身会被当成故障
_CHECK_TIMEOUT_SEC = 2.0

# `/ready` 里表示"检查本服务的数据库池"的保留取值
POOL_SENTINEL = "pool"


def _probe_result(kind: str, target: str, ok: bool, detail: str, *,
                  latency_ms: float = 0.0,
                  status: Optional[int] = None) -> dict[str, Any]:
    """构造一项探针结果 —— 结果字典的键集合**只有这一处**。

    ★ 对外形状固定为 `{ok, kind, target, detail, latency_ms}`，`status` 只在
      真的拿到了 HTTP 状态码时补进去。手抄五次的结果是"漏键/改名"没人拦得住，
      而响应体是给 Agent 读的契约。

    ★ `status` 插在 `target` 与 `detail` 之间：保持与既有响应体的**键顺序**一致
      （顺序不是契约，但改动它会让人工比对响应时多一层噪声）。
    """
    out: dict[str, Any] = {"ok": ok, "kind": kind, "target": target}
    if status is not None:
        out["status"] = status
    out["detail"] = detail
    out["latency_ms"] = latency_ms
    return out


def health_payload(service: str, *, identity: Optional[Mapping[str, Any]] = None,
                   depends_on: Iterable[str] = ()) -> dict[str, Any]:
    """`/health` 的响应体 —— 存活 + **身份与依赖的自报**。

    ★ 为什么身份与依赖要由**每个服务自己**报：只有对端知道自己的集群/环境/命名空间，
      以及自己配置里声明的下游（`aiops.services.<me>.downstream`）。
      探测方拿不到这些，只能靠对端说 —— 这也正是 `/services` 能汇总出全系统拓扑的原因。

    ★ **全部可空**：`cluster`/`env`/`namespace` 取不到就是 `None`，
      `depends_on` 没有依赖就是 `[]`。**空 ≠ 错误**（单集群、未接平台元数据都合法），
      而且 **SDK 绝不自造默认值** —— 造出来的值会让"未知"与"真的是这个值"再也分不开。
    """
    payload: dict[str, Any] = {"status": "alive", "service": service}
    source = dict(identity or {})
    for key in IDENTITY_FIELDS:
        value = source.get(key)
        text = str(value).strip() if value is not None else ""
        payload[key] = text or None
    payload["depends_on"] = sorted({str(name).strip() for name in depends_on if str(name).strip()})
    return payload


async def check_http(url: str, *, timeout: float = _CHECK_TIMEOUT_SEC,
                     capture_json: bool = False) -> dict[str, Any]:
    """检查一个 HTTP 依赖（打对方的 `/health`）。

    ★ 只有 2xx 算就绪。3xx 不算 —— 就绪探针不该跟着重定向走。
    ★ `capture_json=True` 时把响应体（若是 JSON 对象）一并带回：`/services`
      要靠它读对方的**自报名字**（名字以对方自己说的为准，不是我们猜的）。
      响应体不是 JSON **不算探针失败** —— 探针不该因为对方格式不合口味就炸。
    """
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
    except Exception as exc:  # noqa: BLE001 —— 探针不得因依赖异常而抛出
        # ★ 有些异常（如 httpx 的 ConnectTimeout）`str()` 是空的 ——
        #   那就只留类名，别让 detail 变成 `"ConnectTimeout: "` 这种没信息的样子
        reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        return _probe_result(
            "http", url, False, reason,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )
    ok = 200 <= resp.status_code < 300
    out = _probe_result(
        "http", url, ok, f"HTTP {resp.status_code}",
        latency_ms=round((time.perf_counter() - started) * 1000, 3),
        status=resp.status_code,
    )
    if capture_json:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 —— 非 JSON 响应体只是"读不到自报名字"
            body = None
        if isinstance(body, dict):
            out["json"] = body
    return out


async def check_pool(runtime: Runtime, name: str) -> dict[str, Any]:
    """检查本服务的数据库池：借一条连接做一次最小查询。

    ★ 池从哪来：池适配器在猴补驱动工厂时把包装器注册进 `Runtime.pools`。
      **没有池就是真的没有**（本服务没声明池驱动、或驱动没有适配器）——
      此时明确报"未就绪"并说明原因，而不是假装通过。

      "假装通过"是最坏的选择：`/ready` 说就绪、指标却是空的，
      排查时会先怀疑采集链路，而真实原因是"适配器根本没挂上"。
    """
    started = time.perf_counter()
    pool = runtime.pool
    if pool is None:
        return _probe_result(
            "pool", name, False,
            "没有已注册的连接池 —— 池适配器未生效（检查 [pool].driver 与驱动是否已安装）",
        )
    try:
        ok, detail = await pool.check()
    except Exception as exc:  # noqa: BLE001 —— 探针不得因依赖异常而抛出
        return _probe_result(
            "pool", name, False, f"{type(exc).__name__}: {exc}",
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )
    return _probe_result(
        "pool", name, bool(ok),
        str(detail.get("detail", "")) if isinstance(detail, dict) else str(detail),
        latency_ms=round((time.perf_counter() - started) * 1000, 3),
    )


async def services_payload(
    runtime: Runtime,
    peers: Mapping[str, tuple[str, int]],
    *,
    timeout: float = _CHECK_TIMEOUT_SEC,
) -> dict[str, Any]:
    """`GET /services` 的响应体 —— **当下存活的服务清单**。

    ★ `timeout` 是单次同伴探测的超时，与依赖检查取同一个值：
      探测是**并发**的，所以总耗时≈最慢的那一个，一个卡住的服务拖不久整份清单。

    ★ 三条设计约定：

      1. **只列存活**：只有 2xx 才算；声明了却没应答的进 `unreachable`
         并带上原因与耗时 —— **不静默丢弃**，否则"少了谁"无从察觉。
      2. **回答者自身必然在列，且不做 HTTP 自探**：本请求正在被处理，
         就是它存活的证明。自探自己会在同一个事件循环里等自己
         （轻则白等到超时被误判为死，重则死锁），
         而且 worker 打满时会把"忙"误报成"死" —— 那是制造假故障。
      3. **有同伴不在也回 200** + `status:"degraded"`：它不是就绪探针。
         拿它做就绪判定，会把"同伴掉线"升级成"自己也重启"，那是放大故障。
    """

    async def probe_peer(name: str, host: str, port: int) -> tuple[bool, dict[str, Any]]:
        base = f"http://{host}:{port}"
        probe_url = f"{base}{PROBE_HEALTH}"
        result = await check_http(probe_url, timeout=timeout, capture_json=True)
        raw_body = result.get("json")
        body: Mapping[str, Any] = raw_body if isinstance(raw_body, Mapping) else {}
        # ★ 名字以**对方自报**为准；配置键另存 —— 两者的差异本身就是信号
        #   （"配置里叫 order、实际跑的是 order-v2"这类事，只有并列才看得出来）
        reported = str(body.get("service") or "").strip()
        item: dict[str, Any] = {
            "name": reported or name,
            "declared_as": name,
            # ★ `url` 是**服务基地址**（不带探针路径）；探针打的是 `probe_url`。
            #   混在一起的话，消费方拿它去拼别的端点就会拼成 `.../health/services`。
            "url": base,
            "probe_url": probe_url,
            "latency_ms": result.get("latency_ms"),
        }
        if result.get("ok"):
            item["status"] = result.get("status")
            # ★ 身份与依赖**照抄对端的自报**：
            #   我们没有别的途径知道它的集群与下游 —— 猜出来的一定是假的。
            for key in IDENTITY_FIELDS:
                value = body.get(key)
                item[key] = str(value).strip() if value else None
            depends = body.get("depends_on")
            item["depends_on"] = sorted({str(target) for target in depends}
                                        if isinstance(depends, (list, tuple)) else set())
            return True, item
        item["detail"] = str(result.get("detail") or "不可达")
        return False, item

    # 并发探测：串行会让"服务多"时的清单接口慢到没人敢用
    pairs = await asyncio.gather(*(probe_peer(n, h, p) for n, (h, p) in peers.items()))

    # ★ 回答者自身那条也要带**自己的**身份与依赖（它同样只有自己知道）
    config = getattr(runtime, "config", None)
    self_item: dict[str, Any] = {
        "name": runtime.service_name,
        "url": None,
        "probed": False,
        "note": "自身：本请求正在被处理，即存活证明（不 HTTP 自探）",
    }
    for key in IDENTITY_FIELDS:
        value = getattr(config, key, None) if config is not None else None
        self_item[key] = str(value).strip() if value else None
    self_item["depends_on"] = sorted(getattr(config, "depends_on", ()) or ())

    alive: list[dict[str, Any]] = [self_item]
    alive.extend(item for ok, item in pairs if ok)
    missing = [item for ok, item in pairs if not ok]

    return {
        "status": "ok" if not missing else "degraded",
        "service": runtime.service_name,
        "declared_count": len(peers) + 1,
        "alive_count": len(alive),
        "services": sorted(alive, key=lambda item: str(item["name"])),
        "unreachable": sorted(missing, key=lambda item: str(item["name"])),
        "probe": PROBE_HEALTH,
        "timeout_sec": timeout,
    }

async def ready_payload(runtime: Runtime, checks: Optional[Mapping[str, str]] = None) -> tuple[bool, dict[str, Any]]:
    """跑完 `[ready]` 清单里的每一项，返回 `(是否就绪, 响应体)`。

    ★ 逐项明细是必须的：只说"503"等于把排查成本转嫁给读日志的人。
    """
    spec = dict(checks or {})
    if not spec:
        return True, {
            "status": "ready",
            "service": runtime.service_name,
            "checks": {},
            "note": "配置里没有 [ready] 清单，本服务未声明任何依赖",
        }

    async def run_one(name: str, kind: str) -> tuple[str, dict[str, Any]]:
        if kind == POOL_SENTINEL:
            return name, await check_pool(runtime, name)
        if kind.startswith("http://") or kind.startswith("https://"):
            return name, await check_http(kind)
        return name, _probe_result(
            "unknown", kind, False,
            f"[ready].{name} 的取值 {kind!r} 无法识别"
            f"（应为 {POOL_SENTINEL!r} 或 http(s) URL）",
        )

    # 并发检查：串行会让"依赖多"的服务探针超时，而超时本身会被当成故障
    pairs = await asyncio.gather(*(run_one(n, k) for n, k in spec.items()))
    checks_out = dict(pairs)
    ready = all(item["ok"] for item in checks_out.values())
    return ready, {
        "status": "ready" if ready else "not_ready",
        "service": runtime.service_name,
        "checks": checks_out,
    }
