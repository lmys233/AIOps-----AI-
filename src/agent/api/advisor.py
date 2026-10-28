"""顾问回答的业务结果：已观测故障、处置方案与形成原因分别说明。"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..attribution.problems import problem_inventory
from ..common.contracts import Evidence
from ..orchestration.evidence_scope import inherited_evidence, scope_evidence


def advisor_outcome(body: Mapping[str, Any], plans: Sequence[Any], flags: Any) -> dict[str, Any]:
    """有处置方案不等于已确认根因；证据只证明查询窗口内的观测。"""
    flags = flags or {}
    source = str(flags.get("remediation_evidence_source") or "本轮取证")
    problems = list((body.get("explanation") or {}).get("problems") or ())
    if not problems and source.startswith("上一轮"):
        names = frozenset((body.get("slots") or {}).get("service_names") or ())
        problems = _inherited_problems(flags, names)
    ready = any(not plan.is_gap for plan in plans)
    observed = "；".join(_observation(item) for item in problems[:5])
    prefix = source if source.startswith("上一轮") else "本次查询窗口的采样"
    summary = f"{prefix}已观测到：{observed}。" if observed else ""
    if ready:
        uncertainty = ("已有根因结论，方案的具体前置条件仍需核对。" if body.get("root_cause") else
            "故障的形成原因尚待核查；已有证据可用于制定核查与条件成立后的修复方案。")
        summary += uncertainty
        if observed:
            summary += "窗口内的历史错误不能证明故障此刻仍在发生。"
    else:
        summary += "尚未取得足以制定修复方案的证据，需要先补齐目标服务的取证。"
    return {"status": "plans_ready" if ready else "needs_evidence",
        "label": "已提供修复方案" if ready else "需要补充证据",
        "root_cause_status": "confirmed" if body.get("root_cause") else "unconfirmed",
        "evidence_source": source, "observed_problems": problems, "summary": summary}


def _observation(problem: Mapping[str, Any]) -> str:
    samples = [str(row.get("sample") or "") for row in problem.get("observations") or ()]
    sample = next((text for text in samples if text), "")
    return f"{problem.get('service')}：{problem.get('title')}" + (f"（{sample}）" if sample else "")


def _inherited_problems(flags: Any, names: frozenset[str]) -> list[dict[str, Any]]:
    items = []
    for item in scope_evidence(inherited_evidence(flags), names):
        try:
            items.append(Evidence.model_validate(item) if isinstance(item, Mapping) else item)
        except (ValueError, TypeError):
            continue
    return problem_inventory(items)
