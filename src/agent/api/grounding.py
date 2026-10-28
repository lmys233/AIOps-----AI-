"""**候选内排序**的接线：模型在代码枚举的候选里挑 1~3 条，越界即弃用。

--------------------------------------------------------------------------------
★ 为什么单独一个模块
--------------------------------------------------------------------------------

它只做一件事：把"代码算出来的地板"与"模型在候选内的选择"合成这一轮的结论。
判定逻辑在 `attribution/decision.py`（纯函数、可单测），本模块只负责**接线**：
拿网关、构造提示词、调一次模型、写回 `flags`。

★ 三条不许破的口径：

1. **只有可挑的东西才调模型**：没有候选、也没有地板时不调 ——
   没有可挑的对象，这一次调用只会白花预算，并把"这一轮没有可判读的错误链路"
   伪装成"模型参与了裁决"；
2. **任何失败都退回地板**：没网关 / 调用失败 / 结构非法 / 越界 → 结论用地板，
   并如实留痕（"这一次模型没有参与排序"）；
3. **结论只写在 `flags[GROUNDING_KEY]` 里**（唯一出口），渲染层与答案层都从那里读。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..attribution.decision import (SELECTION_MIN_TOKENS, SELECTION_ROLE,  # noqa: F401
                                    SelectionDraft, align_root_cause,
                                    selection_prompt)
from ..attribution.evidence_facts import GROUNDING_KEY, decide, error_summary_of
from ..attribution.problems import problem_inventory
from ..llm.gateway import LLMError

__all__ = ["SELECTION_ROLE", "conclusions_of", "error_summary_of_result",
           "has_choices", "select_root_causes"]


def has_choices(grounding: Mapping[str, Any]) -> bool:
    """这一轮有没有**可挑的东西**（候选或地板至少有一个）。"""
    if list(grounding.get("candidates") or []):
        return True
    return bool((grounding.get("floor") or {}).get("picked"))


def conclusions_of(result: Any) -> list[dict[str, Any]]:
    """本轮结论条（从 `flags` 里取；没跑裁决就是空列表）。"""
    state = getattr(result, "state", None)
    grounding = (getattr(state, "flags", None) or {}).get(GROUNDING_KEY)
    if not isinstance(grounding, Mapping):
        return []
    decision = grounding.get("decision")
    if not isinstance(decision, Mapping):
        return []
    return [dict(item) for item in (decision.get("verdicts") or [])]


def error_summary_of_result(result: Any) -> dict[str, Any]:
    """跨服务错误汇总（含候选与指纹）—— **重新算一次**，好把裁决阶段的候选带上。"""
    state = getattr(result, "state", None)
    if state is None:
        return {}
    grounding = (getattr(state, "flags", None) or {}).get(GROUNDING_KEY)
    candidates = list((grounding or {}).get("candidates") or ())
    evidence = getattr(state, "evidence", ()) or ()
    return {**error_summary_of(evidence, slots=getattr(state, "slots", None),
                              candidates=candidates),
            "problems": problem_inventory(evidence)}


async def select_root_causes(service: Any, result: Any, degraded: list[str]) -> None:
    """在**代码枚举的候选**里让模型挑 1~3 条；越界就退回地板。

    ★ 一次调用：候选 + trace JSON + 相关事实 + 记忆摘要一起注入。
    ★ 任何失败（没网关 / 调用失败 / 结构非法）都**不**影响主流程：
      结论退回**地板**，并如实留痕（"这一次模型没有参与排序"）。
    """
    state = result.state
    if (getattr(state,'flags',None) or {}).get('attribution_v2') is not None:
        return
    grounding = (getattr(state, "flags", None) or {}).get(GROUNDING_KEY)
    if not isinstance(grounding, Mapping):
        return
    if not has_choices(grounding):
        state.add_note("本轮没有错误候选（也没有确定性地板）→ 不做候选内排序")
        return
    gateway = getattr(getattr(service, "_answers", None), "gateway", None)
    if gateway is None:
        state.add_note("未配置模型 → 结论只用代码的确定性地板（没有候选内排序）")
        store_decision(state, grounding, None)
        return
    draft: Any = None
    try:
        draft = await gateway.generate(
            selection_prompt(grounding), SelectionDraft, role=SELECTION_ROLE,
            model=getattr(getattr(service, "_router", None), "main_model", "") or None,
            temperature=0.0, max_tokens=SELECTION_MIN_TOKENS)
    except LLMError as exc:
        degraded.append(f"候选内排序失败（{exc.code}）→ 结论退回代码的确定性地板")
    except Exception as exc:  # noqa: BLE001 —— 排序是增强，不许带走排查
        degraded.append(f"候选内排序异常（{type(exc).__name__}）"
                        f"→ 结论退回代码的确定性地板")
    store_decision(state, grounding, draft)


def store_decision(state: Any, grounding: Mapping[str, Any], draft: Any) -> None:
    """把结论写回 `flags`（**唯一出口**：越界弃用、主语对齐与留痕都在这里收口）。

    ★★ 结论**只有一条服务维度的口径**：候选内裁决选出的主语（`decision.subject`）
      同时决定叙事主语与 `root_cause` 的实体。对齐在 `decision.align_root_cause`
      里（纯函数、可单测），本函数只负责写回；**没有候选时它一个字都不改**
      （老路径不回归）。
    """
    decided = decide(grounding, draft)
    aligned = align_root_cause(getattr(state, "arbitration", None), decided,
                               evidence=getattr(state, "evidence", ()) or ())
    if aligned.get("changed"):
        _apply_alignment(state, aligned["root_cause"])
    decided["degraded"] = [*(decided.get("degraded") or ()),
                           *(aligned.get("degraded") or ())]
    stored = dict(grounding)
    stored["decision"] = decided
    stored["subject"] = aligned.get("subject")
    state.flags[GROUNDING_KEY] = stored
    for line in decided.get("degraded") or ():
        state.degraded_detail.append(str(line))
    state.add_note(f"结论 {len(decided.get('verdicts') or [])} 条"
                   f"（来源 {decided.get('source')}）")
    if aligned.get("changed"):
        state.add_note(
            f"`root_cause` 的实体按候选集对齐为 "
            f"{aligned['root_cause'].root_entity.name}（与叙事主语同源）")


def _apply_alignment(state: Any, root: Any) -> None:
    """把对齐后的根因写回**仲裁结果与 `hypotheses` 两处**（不许一处新、一处旧）。

    ★ 为什么不只改 `arbitration`：响应里 `hypotheses[rank=1].status` 是 `confirmed`
      的那条（客户端按它渲染证据链），`root_cause` 是同一件事的另一种形状。
      只改一处，用户会看到"根因是 X、确认假设却是 Y"这种自相矛盾（实测踩到过）。
    """
    arbitration = getattr(state, "arbitration", None)
    if arbitration is None:
        return
    state.arbitration = arbitration.model_copy(update={"root_cause": root})
    state.hypotheses = [root if item.hypothesis_id == root.hypothesis_id else item
                        for item in (getattr(state, "hypotheses", None) or ())]
