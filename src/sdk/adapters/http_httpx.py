"""`httpx` 适配器：猴补 `send`，记录下游调用与 client-server gap。

★ 猴补 `httpx.AsyncClient.send` / `httpx.Client.send` —— 也就是**同步与异步两条路**。
  业务怎么发请求都行，SDK 都能记到 `downstream_*` 两条。

★ 目标名（指标标签 `target`）来自配置的 `[downstream]`（URL 前缀 → 名字）。

  为什么不让 SDK 从 host:port 猜：`127.0.0.1:8082` 猜不出"payment"，
  而 `target="payment"` 才是 Agent 做依赖健康分析时要的语义。
  猜不出来就如实标 `unknown` 并**告警一次**。

---- ★ 关于 client-server gap：算，但**不新增第 12 个指标** ----

  gap = 客户端耗时（本地测） − 对端自报耗时（响应体里的 `_server_elapsed_ms`）

  这个量是核心归因判据，但**指标表里没有它** ——
  它是由两个**已存在**的指标相减得到的：

      downstream_duration_seconds{service,target}        ← 客户端视角（本适配器写）
      server_self_reported_duration_seconds{service,endpoint}  ← 对端自报（对端写）

  所以这里**不发明新指标**：Agent 只按契约查询，第 12 个指标它根本不会去查，
  加了等于白加，还会让"11 个指标"的契约验收变得含糊。
  我们仍然把 gap 算出来，用于**日志与自检**（DEBUG 一行 + `Runtime.counters`）。

★ 缺自报耗时**不得记为 gap=0**：

  把"对端没报"当成"gap 为 0"，会直接得出 **"gap=0，服务端正常"** 的结论 ——
  而真相可能是"对端根本没有这个能力"。**假结论比没结论更坏**：
  它会让 Agent 排除掉正确的假设。
  所以：缺就**不产出样本**，单独计数并告警一次。
"""

from __future__ import annotations

import json
import logging
from time import perf_counter
from typing import Any, Optional

from ..definitions import DOWNSTREAM_DURATION, DOWNSTREAM_TOTAL
from ..state import Runtime, get_runtime

__all__ = ["patch", "PATCH_NAME_ASYNC", "PATCH_NAME_SYNC", "extract_server_ms"]

logger = logging.getLogger("sdk.downstream")

PATCH_NAME_ASYNC = "http:httpx.AsyncClient.send"
PATCH_NAME_SYNC = "http:httpx.Client.send"

# 计数键名（给 verify() 读）
MISSING_SERVER_MS = "missing_server_ms"
GAP_SAMPLES = "gap_samples"
UNKNOWN_TARGET = "unknown_target_calls"


def _active_runtime(fallback: Runtime) -> Runtime:
    """**调用时**取当前 Runtime，取不到才退回补丁装上的那个。

    ★ 猴补挂在进程级的库函数上，而 `set_runtime` 是进程级单例：
      把构造时那个 Runtime 焊进闭包，会让同进程第二个 app 的指标继续记到第一个上。
    """
    from ..diagnostic_state import active_runtime
    return active_runtime() or fallback


def extract_server_ms(response: Any) -> Optional[float]:
    """从响应体里取对端自报的 `_server_elapsed_ms`；取不到返回 None。

    ★ 取不到的原因有很多，全都归为"没有自报"：
      非 JSON 响应 / 响应体没读完（stream=True）/ 字段缺失 / 字段不是数字。
      不要试图区分它们 —— 对 gap 而言它们等价。
    """
    from ..contract import RESPONSE_FIELD_ELAPSED_MS

    try:
        raw = response.content
    except Exception:  # noqa: BLE001 —— 流式响应未读取时会抛 ResponseNotRead
        return None
    if not raw or len(raw) > 1 << 20:
        return None
    ctype = ""
    try:
        ctype = (response.headers.get("content-type") or "").lower()
    except Exception:  # noqa: BLE001
        ctype = ""
    if "json" not in ctype:
        return None
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(body, dict):
        return None
    value = body.get(RESPONSE_FIELD_ELAPSED_MS)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _record(runtime: Runtime, url: str, status: str, elapsed: float,
            response: Any) -> None:
    """记录一次下游调用。★ 整体套 `safe_record` 护栏，异常不得外泄。"""
    from ..state import safe_record

    safe_record(
        runtime, "downstream", f"下游调用 {status}", 
        lambda: _record_inner(runtime, url, status, elapsed, response),
    )


def _record_inner(runtime: Runtime, url: str, status: str, elapsed: float,
                  response: Any) -> None:
    from ..contract import RESPONSE_FIELD_ELAPSED_MS

    service = runtime.service_name
    cfg = runtime.config
    target = (cfg.target_for(url) if cfg is not None else None) or "unknown"
    if target == "unknown":
        calls = runtime.bump(UNKNOWN_TARGET)
        runtime.warn_once(
            "downstream_target_unknown",
            f"有下游调用未命中 [downstream] 映射（示例：{url}）—— "
            f"指标里 target 记为 'unknown'，依赖健康分析会失去区分度"
            f"（已发生 {calls} 次）",
        )

    DOWNSTREAM_TOTAL.labels(service=service, target=target, status=status).inc()
    DOWNSTREAM_DURATION.labels(service=service, target=target).observe(elapsed)
    from ..diagnostic_state import current_correlation
    runtime.diagnostics.emit('downstream_call',dict(target=target,status=status,
        elapsed_ms=elapsed*1000),'sdk_downstream',current_correlation())

    if response is None:
        return
    server_ms = extract_server_ms(response)
    if server_ms is None:
        # ★ 不产出 gap 样本（见模块文档）
        missing = runtime.bump(MISSING_SERVER_MS)
        runtime.warn_once(
            "downstream_no_server_ms",
            f"下游 {target} 的响应里没有 {RESPONSE_FIELD_ELAPSED_MS} —— "
            f"这次调用**不产出** client-server gap（而不是记成 0）。"
            f"常见原因：对端没接入 SDK（或不是本项目的实现）"
            f"（已发生 {missing} 次）",
        )
        return
    gap_ms = round(elapsed * 1000 - server_ms, 3)
    runtime.bump(GAP_SAMPLES)
    logger.debug(
        "client-server gap: target=%s client_ms=%.3f server_ms=%.3f gap_ms=%.3f",
        target, elapsed * 1000, server_ms, gap_ms,
        extra={"target": target, "gap_ms": gap_ms, "server_ms": server_ms},
    )


def patch(runtime: Runtime) -> list[str]:
    """猴补 httpx 的同步与异步 `send`。返回已生效的补丁说明列表。"""
    try:
        import httpx  # noqa: PLC0415
    except ImportError:
        runtime.warn_once(
            "httpx_missing",
            "环境里没有 httpx —— downstream_* 两条将以值 0 暴露",
        )
        return []

    applied: list[str] = []

    # ---- 异步 ----
    if not getattr(httpx.AsyncClient.send, "__aiops_patched__", False):
        original_async = httpx.AsyncClient.send

        async def send_async(self: Any, request: Any, **kwargs: Any) -> Any:
            started = perf_counter()
            try:
                from ..diagnostic_stage import stage
                with stage(_active_runtime(runtime),'downstream_call'):
                    response = await original_async(self, request, **kwargs)
            except Exception:
                # 连不上/超时也是下游调用结果，必须计数（否则错误率偏低）
                _record(_active_runtime(runtime), str(request.url), "error",
                        perf_counter() - started, None)
                raise
            _record(_active_runtime(runtime), str(request.url), str(response.status_code),
                    perf_counter() - started, response)
            return response

        send_async.__aiops_patched__ = True        # type: ignore[attr-defined]
        send_async.__aiops_original__ = original_async  # type: ignore[attr-defined]
        httpx.AsyncClient.send = send_async        # type: ignore[assignment]
        applied.append(PATCH_NAME_ASYNC)
        runtime.note_patch(PATCH_NAME_ASYNC, "已接管 httpx.AsyncClient.send")

    # ---- 同步 ----
    if not getattr(httpx.Client.send, "__aiops_patched__", False):
        original_sync = httpx.Client.send

        def send_sync(self: Any, request: Any, **kwargs: Any) -> Any:
            started = perf_counter()
            try:
                from ..diagnostic_stage import stage
                with stage(_active_runtime(runtime),'downstream_call'):
                    response = original_sync(self, request, **kwargs)
            except Exception:
                _record(_active_runtime(runtime), str(request.url), "error",
                        perf_counter() - started, None)
                raise
            _record(_active_runtime(runtime), str(request.url), str(response.status_code),
                    perf_counter() - started, response)
            return response

        send_sync.__aiops_patched__ = True         # type: ignore[attr-defined]
        send_sync.__aiops_original__ = original_sync   # type: ignore[attr-defined]
        httpx.Client.send = send_sync              # type: ignore[assignment]
        applied.append(PATCH_NAME_SYNC)
        runtime.note_patch(PATCH_NAME_SYNC, "已接管 httpx.Client.send")

    return applied
