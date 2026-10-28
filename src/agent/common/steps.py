"""把「非工具类」的环节也当成一次工具调用发出去（契约修订 20）。

★ 背景（用户 2026-10-03 指示）："每次输入后系统的操作都要透明化……因为我们把每个环节当成
  tool，那么调用就是 tool call"。于是**识别意图 / 规划取证 / 证据入黑板 / 裁决 / 答案总结**
  这些环节与真正的工具调用一样，成对发 `phase="start"`（参数）/ `phase="done"`（结果）。

★ 为什么放在 `common/`：两边都要用，而依赖方向不能反 ——
  `orchestration/` 依赖 `agents/`（怎么跑取证），所以共享的这一个小工具不能住在这两处中的任一处。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Final, Mapping, Optional

__all__ = ["emit_step", "emit_step_phase", "shrink_params", "record_tool_call",
           "record_process_step", "STEP_RESULT_MAX", "STEP_PARAM_MAX", "PROCESS_STEPS_MAX"]

logger = logging.getLogger("agent.steps")

#: 进事件的**结果**与**参数**都**有界**（事件要能实时推，不能因为一条查询语句把流撑爆）
STEP_RESULT_MAX: Final[int] = 400
STEP_PARAM_MAX: Final[int] = 200
#: 列表 / 字典类参数最多留几项（多的折成一句"共 N 项"）
STEP_ITEMS_MAX: Final[int] = 8

#: 一轮里**过程时间线**最多留几条（刷新后恢复"执行过程"用；超出的丢最早的并如实说明）
PROCESS_STEPS_MAX: Final[int] = 60

#: 事件出口：`(event, data) -> None`（`None` = 这次没有订阅者，直接跳过）
Emit = Optional[Callable[[str, dict[str, Any]], None]]


def record_tool_call(state: Any, data: Mapping[str, Any]) -> None:
    """记录已完成的调用，供实时事件和历史恢复使用同一份结果。"""
    if str(data.get("phase") or "") != "done":
        return
    calls = state.flags.setdefault("tool_calls", [])
    calls.append({"name": str(data.get("name") or ""),
                  "title": str(data.get("title") or ""),
                  "params": str(data.get("params") or "")[:300],
                  "result": str(data.get("result") or "")[:300],
                  # ★ `call_id` 是"过程时间线里那次调用"与"这条记录"对齐的**唯一钥匙**
                  #   （同名调用可以并发，靠名字配对会张冠李戴）。
                  "call_id": str(data.get("call_id") or ""),
                  "ok": bool(data.get("ok", True))})
    del calls[:-24]


def record_process_step(state: Any, event: str, data: Mapping[str, Any]) -> None:
    """记录**执行过程**的一步（刷新后要能把"AI 是怎么跑的"原样恢复出来）。

    ★★ 为什么必须有它：实时那张卡的「推理过程」里除了工具卡，还有 `intent` / `agent_start` /
      `agent_done` / `report` 这些**非工具环节**（前端用 `summarizeEvent` 渲染成一行行）。
      这些行以前**只走 SSE**、不落会话记忆 → 刷新后整条时间线就没了（用户实测：
      "你是不是把思考过程也删了？刷新页面后就不见了，我想要刷新后依旧保留 AIOps 执行的过程"）。
    ★ 记的是**顺序 + 原始（有界）载荷**，渲染仍由前端 `summarizeEvent` 那**一处**做
      —— 服务端不另写一份文案（两处各写一遍必然漂移）。
    ★ 工具调用也占一个位置（在 `phase="start"` 时记，与实时建卡的时机一致），
      卡片内容靠 `call_id` 去 `tool_calls` 里取 —— 顺序因此与实时**一致**。
    """
    steps = state.flags.setdefault("process_steps", [])
    phase = str(data.get("phase") or "")
    if event == "tool":
        if phase != "start":                 # 内容那一半由 `record_tool_call` 在 done 时记
            return
        steps.append({"event": "tool", "call_id": str(data.get("call_id") or ""),
                      "name": str(data.get("name") or "")})
    else:
        steps.append({"event": str(event),
                      "data": shrink_params({key: value for key, value in data.items()
                                             if key not in ("trace_id", "incident_id")})})
    del steps[:-PROCESS_STEPS_MAX]


def shrink_params(params: Mapping[str, Any], limit: int = STEP_PARAM_MAX) -> dict[str, Any]:
    """事件参数副本：**保形 + 有界**（标量与短结构原样，长的截断并**标明**）。

    ★ 为什么不能一律 `str(value)`：`['metrics_agent', 'metrics_agent', …]` 会被印成
      Python repr，读起来像"12 个同名 Agent"（真机踩到过）—— 列表要**保持列表**。
    ★ 为什么不能只保形不设界：一次规划可能有十几个信号、查询语句也可能很长，而事件是
      **实时推**的；所以列表/字典最多留 `STEP_ITEMS_MAX` 项（并写明一共几项），字符串按 `limit` 截断。
    """
    out: dict[str, Any] = {}
    for key, value in params.items():
        name = str(key)
        if isinstance(value, (list, tuple)):
            items = list(value)[:STEP_ITEMS_MAX]
            out[name] = items + ([f"…（共 {len(value)} 项）"] if len(value) > STEP_ITEMS_MAX else [])
            continue
        if isinstance(value, Mapping):
            items = dict(list(value.items())[:STEP_ITEMS_MAX])
            if len(value) > STEP_ITEMS_MAX:
                items["…"] = f"共 {len(value)} 项"
            out[name] = items
            continue
        if value is None or isinstance(value, (bool, int, float)):
            out[name] = value
            continue
        text = str(value)
        out[name] = text if len(text) <= limit else text[:limit] + "…"
    return out


def _shrink(params: Mapping[str, Any], limit: int = STEP_PARAM_MAX) -> dict[str, Any]:
    """兼容旧名（与 :func:`shrink_params` **同一实现**，不存第二份）。"""
    return shrink_params(params, limit)


def emit_step_phase(emit: Emit, phase: str, name: str, title: str,
                    params: Mapping[str, Any], *, result: str = "", ok: bool = True,
                    call_id: str = "", role: str = "orchestrator", elapsed_ms: Optional[float] = None) -> None:
    """发**一条**步骤事件（`phase="start"` 或 `"done"`）—— 需要"先发 start、跑完再发 done"时用它。"""
    if emit is None:
        return
    payload: dict[str, Any] = {
        "phase": phase, "call_id": call_id or f"step-{name}", "name": name,
        "title": title, "params": _shrink(params), "why": "", "role": role,
        "ok": bool(ok), "degraded": not ok,
        "error_code": "" if ok else "STEP_FAILED", "elapsed_ms": elapsed_ms,
        "raw_ref": "", "result": str(result)[:STEP_RESULT_MAX] if phase == "done" else ""}
    try:
        emit("tool", payload)
    except Exception:  # noqa: BLE001 —— 事件是旁路，绝不带走排查
        logger.warning("步骤事件发送失败（name=%s phase=%s）", name, phase, exc_info=False)


def emit_step(emit: Emit, name: str, title: str, params: Mapping[str, Any], *,
              result: str = "", ok: bool = True, call_id: str = "",
              role: str = "orchestrator", elapsed_ms: Optional[float] = None) -> None:
    """成对发一条「步骤」工具事件（瞬时步骤用；要跨 await 的用 `emit_step_phase`）。"""
    emit_step_phase(emit, "start", name, title, params, call_id=call_id, role=role)
    emit_step_phase(emit, "done", name, title, params, result=result, ok=ok,
                    call_id=call_id, role=role,elapsed_ms=elapsed_ms)
