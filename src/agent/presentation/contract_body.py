"""响应体骨架：契约字段清单与"诚实的空值"的**唯一来源**。

三处都需要"字段一个都不能少、缺的给空值"：

· 正常渲染（`RenderingMixin._render`）—— 只覆盖取值，不再自己数键；
· 流式路径的降级兜底（`StreamingMixin._degraded_body`）；
· 恢复路径取不到 incident 时的兜底体（`resume._finish_confirmation` /
  `resume._finish_approval`）。

手抄第二份清单迟早漏键：客户端在错误路径上按同一份契约取值就会 `KeyError`，
而"错误路径的响应形状和正常路径不一样"是最难查的一类问题。空值一律取
**诚实的空**（`null` / `[]` / 零），不编一个看起来正常的取值。
"""

from __future__ import annotations

from typing import Any, Sequence

from ..common.enums import Intent
from ..orchestration import new_incident_id


def empty_contract_body(session_id: str, *, trace_id: str, incident_id: str = "",
                        status: str = "failed", intent_name: str = "",
                        write_allowed: bool = False,
                        turn_id: str = "",
                        degraded: Sequence[str] = (),
                        termination_reason: str = "") -> dict[str, Any]:
    """造一份字段齐全、取值诚实的空响应体（键名与顺序就是契约顺序）。

    `incident_id` 缺省时**铸一个新的**：没有事件号的 body 存不进也查不到，
    客户端拿到它也没法追问（这不是"更安全的空值"，而是一个坏值）。
    `turn_id` 缺省是空串（含义是"这份 body 不属于某一轮新排查"，例如审批收尾）；
    对话入口必须传**这一轮最终用的那个 id**（服务端代铸的也一样）。
    """
    return {
        "session_id": str(session_id or ""),
        "turn_id": str(turn_id or ""),
        "incident_id": str(incident_id or "") or new_incident_id(),
        "status": status,
        # ★ 排查没跑起来时没有意图可报 → 落兜底值（`source=fallback`），不编一个
        "intent": {"name": intent_name or Intent.OUT_OF_SCOPE.value,
                   "confidence": 0.0, "confidence_raw": 0.0,
                   "source": "fallback", "route": "rule", "explanation": ""},
        "slots": {},
        "clarification": None,
        "root_cause": None,
        "hypotheses": [],
        "evidence": [],
        "rounds": 0,
        "cost": {"input_tokens": 0, "output_tokens": 0, "tool_calls": 0,
                 "wall_ms": 0.0},
        "trace_id": trace_id,
        "unlocked_tools": [],
        "degraded": list(degraded),
        "unresolved_entities": [],
        "budget": {"limits": {}, "used": {}, "enforced": {}, "notes": []},
        "open_questions": [],
        "degraded_signals": [],
        "termination": {"reason": str(termination_reason or ""), "satisfied": False},
        "proposed_action": None,
        "risk": None,
        "action_blocked": None,
        "write_allowed": bool(write_allowed),
        "explanation": {"rules": {}, "model_calls": [], "clarification": "",
                        "intent_latency_ms": 0, "scoring": "", "scoring_kind": "",
                        "prior": None, "reference": None, "escalation": {},
                        "graph": {}, "arbitration": None, "critic": None,
                        "orchestration": []},
    }
