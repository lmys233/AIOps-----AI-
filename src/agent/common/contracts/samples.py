"""契约样本工厂（供验收脚本与实现侧自检；`ALL_CONTRACTS` + 16 份合法样本）。

★ 本模块的内容原样搬自原 `common/contracts.py`（它曾有 1090 行），
  **公开导入面一字未改** —— `from agent.common.contracts import …` 照旧可用
  （包的 `__init__.py` 显式再导出全部公开名）。
"""

from __future__ import annotations

from ..enums import (
    ActionClass,
    ArbitDecision,
    ChangeStage,
    EvidenceDirection,
    EvidenceSource,
    HypothesisStatus,
    Intent,
    IntentSource,
    RiskLevel,
    ToolPermission,
    VerificationMethod,
)
from datetime import datetime, timedelta, timezone
from typing import Any, Final
from pydantic import BaseModel
from .agent_state import AgentState
from .entities import EntityRef
from .evidence import Evidence, EvidenceBoard, EvidencePlan
from .hypothesis import ArbitResult, Hypothesis
from .intent import IntentResult, Slots
from .memory import MemoryHit
from .tools import (ChangeResult, ProposedAction, RiskDecision, ToolResult,
                    ToolSpec, WriteResult)


# ==================================================================
#  ALL_CONTRACTS：全部契约对象
#  ★ 顺序固定（编号即顺序），便于人工对照
# ==================================================================
ALL_CONTRACTS: Final[tuple[type[BaseModel], ...]] = (
    EntityRef,        # 1
    Slots,            # 2
    IntentResult,     # 3
    Evidence,         # 4
    Hypothesis,       # 5
    EvidenceBoard,    # 6
    MemoryHit,        # 7
    AgentState,       # 8
    ToolResult,       # 9
    ArbitResult,      # 10
    WriteResult,      # 11
    EvidencePlan,     # 12
    ChangeResult,     # 13
    ToolSpec,         # 14
    ProposedAction,   # 15
    RiskDecision,     # 16
)

# ==================================================================
#  契约样本工厂（供验收脚本与实现侧自检使用）
#
#  ★ 为什么放在实现侧：验收脚本（tests/test_internal_contracts.py）
#    的校验器接受纯 dict，需要一个"合法样本"来验证校验器不会误报。
#    由实现侧提供样本，可保证样本与真实模型同步演进。
# ==================================================================
def _window() -> tuple[datetime, datetime, datetime]:
    """样本用的时间窗（`(start, end, onset)`；固定值，保证样本可复现）。"""
    start = datetime(2026, 9, 26, 10, 30, 0, tzinfo=timezone.utc)
    return start, start + timedelta(minutes=30), start + timedelta(minutes=1)


def _sample_evidence(order: EntityRef, onset: datetime,
                     window: tuple[datetime, datetime]) -> Evidence:
    """4. `Evidence`：连接池等待上升（样本里的"现象"）。"""
    return Evidence(
        evidence_id="ev_01J8X0000000000000000000",
        source=EvidenceSource.METRICS,
        agent_role="metrics_agent",
        entity=order,
        signal="db_pool_waiting_connections",
        direction=EvidenceDirection.UP,
        magnitude=5.0,
        magnitude_unit="count",
        baseline=0.0,
        time_range=window,
        onset=onset,
        confidence=0.9,
        raw_ref='promql: db_pool_waiting_connections{service="order"}',
        summary="order 有 5 个请求在等待连接",
        tags=["pool_saturation"],
    )


def _sample_hypothesis(order: EntityRef, evidence: Evidence) -> Hypothesis:
    """5. `Hypothesis`：一条已确认的候选（带支持/必要证据）。"""
    return Hypothesis(
        hypothesis_id="h_01",
        statement="order 连接池上限由 20 变更为 5，导致获取连接超时",
        category="config_change",
        root_entity=order,
        prior=0.62,
        evidence_likelihoods={evidence.evidence_id: 8.0},
        score=0.72,
        supporting=[evidence.evidence_id],
        contradicting=[],
        necessary_evidence=["pool_timeout_count"],
        missing_necessary=[],
        status=HypothesisStatus.CONFIRMED,
        excluded_reason=None,
    )


def _sample_memory_hit() -> MemoryHit:
    """7. `MemoryHit`：历史相似案例（带元数据 —— 注入时必须带）。"""
    return MemoryHit(
        memory_id="mem_01",
        symptom_text="order P99 升高且伴随 connection pool timeout",
        root_cause="payment 服务 ConfigMap 中 maxConnections 被从 20 调整为 5",
        similarity=0.91,
        age_days=47.0,
        verification=VerificationMethod.VERIFIED_HUMAN,
        hit_count=6,
        superseded_by=None,
    )


def _sample_board(order: EntityRef, evidence: Evidence, hypothesis: Hypothesis,
                  memory_hit: MemoryHit, onset: datetime) -> EvidenceBoard:
    """6. `EvidenceBoard`：一次排查的黑板（样本里最完整的一个对象）。"""
    return EvidenceBoard(
        incident_id="inc_01J8X",
        slots=Slots(service=order, env="prod", cluster="prod-cn-1",
                    time_window="30m", namespace="default",
                    service_names=["order"], tenant_id="default"),
        timeline=[{"t": onset.isoformat(), "type": "signal",
                   "entity": order.id, "signal": evidence.signal}],
        evidence=[evidence],
        hypotheses=[hypothesis],
        open_questions=[],
        round=2,
        budget={"max_rounds": 3},
        cost={"tool_calls": 14},
        memory_hits=[memory_hit],
        unused_evidence_ids=[],
        supporting_evidence_ids=[evidence.evidence_id],
        excluded_hypotheses=["h_02"],
        abnormal_entities=[order.id],
        last_round_new_evidence_lr_gain=0.42,
        baseline_metrics={"order.p99_ms": 120.0},
        qps_estimates={"order": 820.0},
    )


def _sample_tool_result() -> ToolResult:
    """9. `ToolResult`：一次"查了、无异常"的工具返回。"""
    return ToolResult(
        ok=True, data=[], degraded=False,
        summary="查了，无异常",
        raw_ref='promql: up{job="aiops-services"}',
        elapsed_ms=12.0,
    )


def _sample_evidence_plan(order: EntityRef) -> EvidencePlan:
    """12. `EvidencePlan`：取证计划（查什么、预期排除什么）。"""
    return EvidencePlan(
        targets=[order.id],
        rationale="先确认是调用方资源饱和还是被调方变慢，再决定是否查变更",
        expected_exclusions=["payment 自身变慢", "流量突增"],
        signals=["metrics", "traces", "logs"],
    )


def _sample_change_result(end: datetime) -> ChangeResult:
    """13. `ChangeResult`：一次变更的最终阶段（样本取"已验证"）。"""
    return ChangeResult(
        success=True,
        stage=ChangeStage.VERIFIED,
        commit="a3f9c2d",
        recovered_at=end,
        error=None,
        rolled_back=False,
    )


def _sample_tool_spec() -> ToolSpec:
    """14. `ToolSpec`：工具声明（9 个内置工具之一）。"""
    return ToolSpec(
        name="promql_read",
        description="执行 PromQL 查询，返回异常指标与拐点",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}, "window": {"type": "string"}},
            "required": ["query"],
        },
        permission=ToolPermission.READ_ONLY,
        timeout_sec=30,
        rate_limit="60/min",
        cost_weight=1.0,
        idempotent=True,
        result_summarizer="summarize_promql_result",
    )


def _sample_proposed_action() -> ProposedAction:
    """15. `ProposedAction`：变更预案（含 dry-run diff 与回滚方案）。"""
    return ProposedAction(
        action_class=ActionClass.ROLLBACK_CONFIG,
        target=EntityRef.make("config", "payment-pool", "prod-cn-1",
                              namespace="default"),
        params={"from": 5, "to": 20},
        dry_run_diff="--- a/configmap.yaml\n+++ b/configmap.yaml\n"
                     "-maxConnections: 5\n+maxConnections: 20",
        rollback_plan="再次回滚 configmap 到当前版本",
    )


def _sample_risk_decision() -> RiskDecision:
    """16. `RiskDecision`：风险分级（样本取 L2：低危可回滚、不需审批）。"""
    return RiskDecision(
        level=RiskLevel.L2,
        reasons=["low_risk_reversible", "single_service"],
        blast_radius={"services_affected": 1, "qps_affected": 820.0,
                      "upstream_count": 1},
        requires_approval=False,
        auto_rollback_window_sec=180,
    )


def _sample_tools(order: EntityRef, window: tuple[datetime, datetime]
                  ) -> dict[str, Any]:
    """9/12/13/14/15/16：工具与变更相关的六个对象（一次构造、一次取用）。"""
    return {
        "ToolResult": _sample_tool_result(),
        "EvidencePlan": _sample_evidence_plan(order),
        "ChangeResult": _sample_change_result(window[1]),
        "ToolSpec": _sample_tool_spec(),
        "ProposedAction": _sample_proposed_action(),
        "RiskDecision": _sample_risk_decision(),
    }


def _sample_agent_state(board: EvidenceBoard) -> AgentState:
    """8. `AgentState`：一次排查的完整状态（样本取"正在跑"）。"""
    return AgentState(
        board=board,
        slots=board.slots,
        intent=Intent.INCIDENT_RCA.value,
        round=2,
        budget={"max_rounds": 3, "max_tool_calls": 80},
        cost={"tool_calls": 14, "wall_ms": 18500},
        proposed_action=None,
        approval=None,
        status="running",
        trace_id="cdf0714fcb046ad23f85a9d28cc5c186",
        degraded_signals=[],
    )


def _sample_intent_result(board: EvidenceBoard) -> IntentResult:
    """3. `IntentResult`：意图 + 槽位（样本取"走完整 RCA 管线"）。"""
    return IntentResult(
        intent=Intent.INCIDENT_RCA.value,
        confidence=0.92,
        confidence_raw=0.87,
        slots=board.slots,
        unresolved_entities=[],
        clarification=None,
        source=IntentSource.SMALL_MODEL,
        route="full_rca_pipeline",
        latency_ms=118,
    )


def _sample_arbit_result(hypothesis: Hypothesis) -> ArbitResult:
    """10. `ArbitResult`：仲裁结论（样本取"收敛"，带根因）。"""
    return ArbitResult(
        decision=ArbitDecision.CONCLUDED,
        root_cause=hypothesis,
        confidence=0.72,
        alternatives=[],
        open_questions=[],
        report=None,
        reason="top-1 得分 0.72 ≥ 0.70 且未被 Critic 否决",
    )


def build_contract_samples() -> dict[str, dict[str, Any]]:
    """构造每个契约对象的**合法样本**（dict 形式）。

    返回：``{对象名: dict}``，供 ``tests/test_internal_contracts.py``
    的 16 个校验器验收。

    ★ 本函数曾有 194 行（16 个对象一口气手写）。现在按对象分组，
      细节在 `_sample_*` 里 —— **样本的每个字段值与顺序都没有变**（它们是契约的一部分）。
    """
    start, end, onset = _window()
    order = EntityRef.make("service", "order", "prod-cn-1", namespace="default")
    evidence = _sample_evidence(order, onset, (start, end))
    hypothesis = _sample_hypothesis(order, evidence)
    memory_hit = _sample_memory_hit()
    board = _sample_board(order, evidence, hypothesis, memory_hit, onset)
    samples: dict[str, Any] = {
        "EntityRef": order,
        "Slots": board.slots,
        "IntentResult": _sample_intent_result(board),
        "Evidence": evidence,
        "Hypothesis": hypothesis,
        "EvidenceBoard": board,
        "MemoryHit": memory_hit,
        "AgentState": _sample_agent_state(board),
        "ArbitResult": _sample_arbit_result(hypothesis),
        "WriteResult": WriteResult(
            memory_id="mem_02", batch_id="batch_01", skipped=False, reason=None,
        ),
        **_sample_tools(order, (start, end)),
    }
    return {name: model.model_dump(mode="json") for name, model in samples.items()}
