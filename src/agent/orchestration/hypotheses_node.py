"""⑤ hypotheses 节点：打分 → Critic → 仲裁。

★ 本模块的方法原样搬自 `orchestrator.py` 的 `Orchestrator`，
  作为 mixin 参与 `Orchestrator` 的组装（**没有第二份实现**）。
"""

from __future__ import annotations

import dataclasses
import inspect
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from ..attribution import CriticAgent, LookupFailed, arbitrate
from ..attribution.evidence_facts import GROUNDING_KEY
from ..board.store import InMemoryBoardStore
from ..common.contracts import Evidence
from ..common.steps import emit_step_phase
from ..tools.handlers import ToolContext
from .constants import LIGHT_INTENTS, logger
from .state import EMPTY_PRIOR_SNAPSHOT, GraphState, board_store


def _series_of(state: GraphState) -> list[dict[str, Any]]:
    """这轮**可算的序列清单**（给模型看、也给它点工具用的 `raw_ref`）。

    ★ 数据来自现状证据：`metric_snapshot` 的 `tags[-1]` 是信号名、`raw_ref` 是本地副本的钥匙、
      `magnitude`/`baseline` 是当前值与窗口起点 —— 足够让模型判断"该算什么"。
    """
    out: list[dict[str, Any]] = []
    for item in state.evidence:
        if str(getattr(item, "signal", "")) not in ("metric_snapshot", "native_metric_snapshot"):
            continue
        tags = [str(tag) for tag in (getattr(item, "tags", None) or [])]
        metadata = dict(tag.split('=',1) for tag in tags if '=' in tag)
        out.append({"metric": metadata.get('metric_name', tags[-1] if tags else ""),
                    "series_id": f"S{len(out)+1}",
                    "type": metadata.get('metric_type', 'unknown'),
                    "statistic": metadata.get('statistic', 'sample'),
                    "as_of": item.onset.isoformat() if item.onset else "",
                    "service": getattr(getattr(item, "entity", None), "name", ""),
                    "raw_ref": str(getattr(item, "raw_ref", "") or ""),
                    "first": getattr(item, "baseline", None),
                    "last": getattr(item, "magnitude", None),
                    "unit": str(getattr(item, "magnitude_unit", "") or ""),
                    **({'observation':item.summary,'time_range':[t.isoformat() for t in item.time_range],
                        'probe_stage':next(t.split('=',1)[1] for t in tags if t.startswith('active_probe_metric_stage='))}
                        if any(t.startswith('active_probe_metric_stage=') for t in tags) else {}),
                    "points": int(metadata['sample_count']) if metadata.get('sample_count','').isdigit() else None})
    return out


def _analysis_result(result: Mapping[str, Any]) -> str:
    """这一步的结果一句话（给了什么数 / 怎么算的）。"""
    answer = result.get("answer") or {}
    value = answer.get("value")
    if value is None:
        return f"没有算出数值（{answer.get('caveats') or result.get('why') or '—'}）"
    runs = "、".join(str(item.get("tool")) for item in (result.get("computations") or []))
    return (f"{answer.get('metric') or '指标'} = {value}{answer.get('unit') or ''}"
            f"（算法：{answer.get('method') or runs or '—'}）")


def _hypotheses_result(state: GraphState, review: Any) -> str:
    """这一步的**结果**一句话（候选几个 / 反事实验证排除几条 / 仲裁结论）。"""
    excluded = len(getattr(review, "excluded", ()) or ())
    root = state.arbitration.root_cause if state.arbitration is not None else None
    verdict = f"已定位根因：{str(getattr(root, 'statement', ''))[:120]}" if root else "没有可下定论的根因"
    return (f"候选 {len(state.hypotheses or ())} 个；反事实验证排除 {excluded} 条；{verdict}")


def _make_lookup(run_executor: Any) -> Any:
    """造一个"反查黑板证据"的闭包（★ 独立成函数：`_critic_for` 曾有 57 行）。

    它**必须**区分两件事：
      · 工具/形状失败 → 抛 `LookupFailed`（"没查成"，不参与排除）；
      · `data=[]`     → 返回 `None`（"黑板上确实没有这条"，判据③ 要的正是它）。
    """

    async def _lookup(evidence_id: str) -> Optional[Evidence]:
        result = await run_executor.call(
            "critic_agent", "evidence_lookup", {"evidence_id": evidence_id})
        if not result.ok:
            # ★★ 工具**没查成**（权限 / 黑板没接 / 没带 incident_id /
            #   数据源故障）必须走异常通道 —— 与"黑板确实没有这条"分开。
            #   以前统一 `return None`，于是 Critic 把一次**瞬时工具故障**
            #   读成"引用了不存在的证据"（判据③）→ 把对的假设排除了。
            logger.warning("Critic 反查失败：evidence_id=%s code=%s summary=%s",
                           evidence_id, result.error_code, result.summary[:120])
            raise LookupFailed(
                f"{result.error_code}：{result.summary[:80] or '工具未给出原因'}")
        # ★ `ToolResult.data` 是**摘要化之后**的形状
        #   （`{"summary", "evidence": [...], "count_total", "truncated"}`），
        #   不是裸列表 —— 按裸列表解析会永远拿不到证据（实测踩到）。
        payload = result.data
        rows: Any = ()
        if isinstance(payload, Mapping):
            rows = payload.get("evidence") or ()
        elif isinstance(payload, list):
            rows = payload
        if not rows:
            # ★ 这是"黑板上**确实没有**这条"（`data=[]`）—— 判据③ 要的正是这个事实
            return None
        try:
            return Evidence.model_validate(rows[0])
        except Exception as exc:  # noqa: BLE001 —— 形状读不出来同样是"没查成"
            logger.warning("Critic 反查结果解析失败：evidence_id=%s err=%s",
                           evidence_id, exc)
            raise LookupFailed(f"返回形状无法解析：{type(exc).__name__}") from exc

    return _lookup


@dataclass(frozen=True)
class _CriticOutcome:
    """Critic 这一轮的结果 —— ★ 把三件事**分开**传给仲裁层：

    | 字段 | 含义 | 仲裁怎么用 |
    |---|---|---|
    | `failed` | 检验**没跑成**（异常 / 关了可读性判据） | 规则⓪：强制拒答 |
    | `failure_reason` | 没跑成的原因（给人看） | 进拒答报告的正文 |
    | `unverified_ids` | **没验证成**的假设（数据源挂 / 反查没查成） | 规则⓪b：最高分那条没验证成 → 拒答 |
    """

    failed: bool = False
    failure_reason: str = ""
    unverified_ids: tuple[str, ...] = ()


def _prior_snapshot(scorer: Any) -> Optional[dict[str, Any]]:
    """取"**本次**先验产物"的快照。

    ★ 打分器是**可替换点**，所以按鸭子类型取：提供 `snapshot()` 就用它，
      没有就返回 `None`（渲染侧回退到实例字段，并在那里如实说明这是兜底）。
      默认装配（`RcaAgent`）永远走 `snapshot()` 这一支。
    """
    take = getattr(scorer, "snapshot", None)
    if not callable(take):
        return None
    return dict(take())


def _subject_service(state: GraphState) -> str:
    """候选集给出的**主语服务**（代码枚举的地板，取证阶段就已经算好）。

    ★ 它只用来定**打分时的实体**：模型在候选内的选择发生在打分之后，
      若模型选了别的（仍在候选内），最终 `root_cause` 的实体由对齐层改成它的
      （见 `api/grounding.py`）。这里**不调模型、不碰遥测**。
    """
    grounding = (getattr(state, "flags", None) or {}).get(GROUNDING_KEY)
    floor = (grounding or {}).get("floor") if isinstance(grounding, Mapping) else {}
    picked = list((floor or {}).get("picked") or [])
    first = picked[0] if picked else {}
    return str((first or {}).get("service") or "")


def _score_kwargs(state: GraphState, scorer: Any) -> dict[str, Any]:
    """打分调用要传的关键字（★ `subject_service` 是**加性**参数：只给认它的实现）。

    ★ 为什么不无条件传：打分器是**可替换点**（消融对照的临时基线、单测替身都实现
      同一个协议），凭空多一个关键字会让它们全部 `TypeError`。
    """
    out: dict[str, Any] = {"evidence": state.evidence, "slots": state.slots,
                           "round_no": state.round}
    subject = _subject_service(state)
    if subject and _accepts_subject(getattr(scorer, "score", None)):
        out["subject_service"] = subject
    return out


def _accepts_subject(score: Any) -> bool:
    """这个 `score` 实现收不收 `subject_service`（**加性参数**，老实现照旧能跑）。"""
    try:
        params = inspect.signature(score).parameters
    except (TypeError, ValueError):        # 内建 / 装饰过的实现：按"不收"处理
        return False
    return "subject_service" in params or any(
        item.kind is inspect.Parameter.VAR_KEYWORD for item in params.values())


class AttributionMixin:
    """⑤ hypotheses 节点：打分 → Critic → 仲裁。"""

    # ------------------------------------------------------------------
    #  ⑤ hypotheses：候选假设 + 打分（可换真公式）
    # ------------------------------------------------------------------
    async def _node_hypotheses(self, state: GraphState) -> str:
        """按 ① 打分 → ② Critic → ③ 仲裁 → ④ 回写/发事件/落黑板 走一遍。

        ★ 本方法曾有 99 行。现在它只写**这一步做了什么**，
          每一步的细节在下面各自的方法里（顺序就是编号顺序）。
        """
        if state.flags.get("forensics_skipped"):
            return "decide"
        from ..observed.sdk_verification import run as verify_sdk
        await verify_sdk(self,state)
        from ..webprobe.workflow import run as active_verify
        await active_verify(self,state)
        # ★★ 查数值（METRIC_QUERY）**不走 8 类候选与仲裁**（契约修订 21）：
        #   那条路的终点必然是"未定论" —— 而"QPS 是多少"要的是一**个数**。
        #   于是这一路改成：取到的数据 → 模型选算法 → **工具算数** → 结构化回答。
        if state.intent in LIGHT_INTENTS:
            await self._metric_analysis(state)
            return "decide"
        if self.attribution_v2_enabled:
            from ..attribution.v2.runtime import diagnose
            await diagnose(self,state)
            return "decide"
        state.flags['attribution_path']='legacy：V2 开关关闭'
        # ★★ 这一步是 CPU 计算（打分 + Critic + 仲裁），但用户同样要看得到：
        #   跑之前发 `start`、跑完发 `done`（契约修订 20）—— 不留"过程黑洞"。
        emitter = lambda event, data: self._emit(state, event, data)   # noqa: E731
        emit_step_phase(emitter, "start", "hypotheses", "生成候选解释与打分",
                        {"evidence": len(state.evidence), "round": state.round})
        self._score_candidates(state)
        review = await self._review_hypotheses(state)
        self._arbitrate_hypotheses(state, review)
        emit_step_phase(emitter, "done", "hypotheses", "生成候选解释与打分",
                        {"evidence": len(state.evidence)},
                        result=_hypotheses_result(state, review))
        self._emit_hypothesis_events(state)
        await self._publish_hypotheses(state)
        return "decide"

    async def _metric_analysis(self, state: GraphState) -> None:
        """查数值这一路：数据清单 + **运算工具清单** → 模型选算法 → 工具算数 → 结构化回答。

        ★ 它**完全不产生假设**（`state.hypotheses` 保持空）：查数不是归因，
          给候选/打分/仲裁只会把答案挤到"未得出结论"上去（用户实测原话：
          "这是很明显了…指标查询应该返回具体的值"）。
        """
        emitter = lambda event, data: self._emit(state, event, data)   # noqa: E731
        series = _series_of(state)
        from ..agents.metric_targets import select
        series,missing_metrics=select(state.user_message,series,state.slots.service_names)
        tools = self._math_tools()
        emit_step_phase(emitter, "start", "metric_analysis", "分析数据并计算",
                        {"series": len(series), "tools": len(tools)})
        analyst = self._analyst()
        result = await analyst.analyze(question=state.user_message, series=series,
                                       tools=tools, on_event=emitter)
        result['missing_metrics']=missing_metrics
        state.flags["metric_answer"] = result
        emit_step_phase(emitter, "done", "metric_analysis", "分析数据并计算",
                        {"series": len(series)},
                        result=_analysis_result(result))
        state.add_note(f"查数值：{result.get('source')}（{result.get('why') or '—'}）")

    def _analyst(self) -> Any:
        """分析师（网关 + 执行器都从编排层现有装配里取，不另建客户端）。"""
        from ..agents.analyst import MetricAnalyst  # noqa: PLC0415

        gateway = getattr(getattr(self, "router", None), "gateway", None)
        model = str(getattr(getattr(self, "router", None), "main_model", "") or "")
        return MetricAnalyst(gateway=gateway, model=model, executor=getattr(self, "executor", None))

    def _math_tools(self) -> list[dict[str, Any]]:
        """把**运算工具清单**导出给模型（"注册即可见"口径：能调用的才给它看）。"""
        from ..tools.math_tools import MATH_TOOL_NAMES  # noqa: PLC0415

        executor = getattr(self, "executor", None)
        if executor is None:
            return []
        return [tool for tool in executor.llm_tools("metrics_agent")
                if str((tool.get("function") or {}).get("name")) in MATH_TOOL_NAMES]

    def _score_candidates(self, state: GraphState) -> None:
        """① 生成候选 + 打分（默认是 RCA Agent：D1 先验 + D2 对数空间 softmax）。

        ★★ 实体（服务维度）由**候选集的地板**给定（`subject_service`）：
          打分框架只回答"哪一类"，"哪个服务"是候选集查出来的事实 ——
          两处各定一次，就会出现"叙事落在 inventory、`root_cause` 停在 order"。
        """
        try:
            state.hypotheses = list(self.scorer.score(
                **_score_kwargs(state, self.scorer)))
            # ★★ **紧接着**取一份"本次先验产物"的快照存进 state。
            #   这里到渲染（`facts._prior_explanation`）之间隔着**多次 await**
            #   （Critic 的工具调用、检查点、会话记忆…），而打分器是**进程级单例** ——
            #   直接读它的实例字段会在并发请求下读到**另一个请求**的值
            #   （实测：A 的响应里 `hypotheses` 是 A 自己的，`prior.by_category` 却是 B 的）。
            #   本行与 `score()` 之间**没有 await**，所以取到的必然是本次的产物。
            state.flags["prior_snapshot"] = _prior_snapshot(self.scorer)
        except Exception as exc:  # noqa: BLE001 —— 打分器是替换点，不许它带崩流程
            state.degraded_detail.append(
                f"打分器失败（{type(exc).__name__}: {exc}）→ 本轮不给候选")
            state.hypotheses = []
            # ★ 打分失败时**不能**留下上一次的快照（否则渲染层会把上一轮的
            #   先验分解当成本轮的 —— 与第 6/7 轮同一条"不说假话"的口径）。
            state.flags["prior_snapshot"] = dict(EMPTY_PRIOR_SNAPSHOT)

    async def _review_hypotheses(self, state: GraphState) -> _CriticOutcome:
        """② Critic 反事实检验（只排除、不新增）—— **失败与"没验证成"分开**。"""
        if not state.hypotheses:
            return _CriticOutcome()
        try:
            report = await self._critic_for(state).review(
                hypotheses=state.hypotheses, evidence=state.evidence,
                anomaly_onset=self._anomaly_onset(state),
                # ★★ **必须**把"哪几路没查成"告诉做排除的那个组件。
                #   否则数据源一挂 → 必要条件"缺失" → 最该保留的假设被排除。
                degraded_signals=state.degraded_signals)
        except Exception as exc:  # noqa: BLE001 —— Critic 失败不该带走排查
            # ★★ Critic 是**唯一**的反证机制。"它没跑成"绝不能等于
            #   "没有反证" —— 那会让组件故障反而提高结论自信度。标成失败，
            #   由仲裁的规则⓪强制拒答。
            reason = f"{type(exc).__name__}: {exc}"
            state.critic = {"error": reason}
            state.degraded_detail.append(
                f"Critic 反事实检验失败（{type(exc).__name__}）→ 本轮未做排除，"
                f"且**不得**据此下结论")
            return _CriticOutcome(failed=True, failure_reason=reason)
        return self._apply_critic_report(state, report)

    def _apply_critic_report(self, state: GraphState, report: Any) -> _CriticOutcome:
        """把 `CriticReport` 写进 state，并翻译成仲裁要的三件事。"""
        state.hypotheses = list(report.hypotheses)
        state.critic = report.to_dict()
        # ★ 这些是"没验证成"的假设（不是"被证伪"）——
        #   它们不许被当成"通过了检验"直接坐实根因，裁决权交仲裁层。
        unverified = tuple(item[0] for item in report.unverified)
        if report.excluded:
            state.add_note(
                f"Critic 排除了 {len(report.excluded)} 条假设："
                + "；".join(f"{hid}（{reason[:40]}…）"
                            for hid, reason in report.excluded[:2]))
        if report.unverified:
            state.add_note(
                f"Critic 有 {len(report.unverified)} 条假设**没验证成**"
                f"（数据源缺失/反查没查成）—— 不算被排除，但也不等于通过")
            state.degraded_detail.extend(reason for _hid, reason in report.unverified[:3])
        if not report.readable_check_disabled:
            return _CriticOutcome(unverified_ids=unverified)
        # ★★ 关掉判据③ = "引用的证据必须读得回来"这条红线没了 → 本次结论**一律不成立**。
        #   把它当"Critic 没跑成"处理（仲裁规则⓪ 强制拒答），
        #   于是这个开关在生产里**拿不到结论**，只能用来做消融对照。
        reason = ("本次关闭了「引用的证据必须读得回来」（spec §2 H2）—— "
                  "消融实验专用开关，结论不成立")
        state.degraded_detail.append(reason)
        state.add_note(reason)
        return _CriticOutcome(failed=True, failure_reason=reason,
                              unverified_ids=unverified)

    def _arbitrate_hypotheses(self, state: GraphState, review: _CriticOutcome) -> None:
        """③ 仲裁（规则流水线见 `attribution/arbiter.py`）+ ④ 把选中的那条回写成 `confirmed`。"""
        try:
            state.arbitration = arbitrate(
                state.hypotheses, missing_signals=state.degraded_signals,
                evidence=state.evidence,
                critic_failed=review.failed,
                critic_failure_reason=review.failure_reason,
                unverified_hypotheses=review.unverified_ids)
            state.add_note(f"仲裁：{state.arbitration.decision.value}"
                           f"（{state.arbitration.reason}）")
        except Exception as exc:  # noqa: BLE001
            state.arbitration = None
            state.degraded_detail.append(
                f"仲裁失败（{type(exc).__name__}: {exc}）→ 退回启发式判据")
        #   ★ 必须回写：响应里 `hypotheses[0].status` 是 `confirmed`
        #     （"已定位根因"的那条），而判据③ 又要求 confirmed 必须带支持证据。
        #     只在 `root_cause` 里 confirmed、而 `hypotheses` 里还是 candidate，
        #     客户端按 rank 渲染时会看到自相矛盾的两个状态（实测踩到）。
        if state.arbitration is not None and state.arbitration.root_cause is not None:
            chosen = state.arbitration.root_cause
            state.hypotheses = [chosen if item.hypothesis_id == chosen.hypothesis_id
                                else item for item in state.hypotheses]

    def _emit_hypothesis_events(self, state: GraphState) -> None:
        """事件：假设与 Critic 的裁决。"""
        for rank, item in enumerate(sorted(state.hypotheses, key=lambda h: -h.score), 1):
            self._emit(state, "hypothesis", {
                "rank": rank, "hypothesis_id": item.hypothesis_id,
                "statement": item.statement, "score": round(item.score, 4),
                "category": item.category, "status": item.status.value})
        for hypothesis_id, reason in (state.critic or {}).get("excluded") or []:
            self._emit(state, "critic", {"hypothesis_id": hypothesis_id,
                                         "verdict": "excluded", "reason": reason})

    @staticmethod
    async def _publish_hypotheses(state: GraphState) -> None:
        """把假设落到黑板（失败只留痕：黑板上少一条不影响本次结论）。"""
        board = board_store(state, need="propose_hypotheses")
        if board is None or not state.hypotheses:
            return
        try:
            await board.propose_hypotheses(state.incident_id, "rca_agent",
                                           state.hypotheses)
        except Exception as exc:  # noqa: BLE001
            state.add_note(f"假设落黑板失败（{type(exc).__name__}）")

    def _critic_for(self, state: GraphState) -> CriticAgent:
        """给这次 run 的 Critic 绑上**真的反查通道**（`evidence_lookup` 工具）。

        ★ 走工具而不是直接读黑板：`evidence_lookup` 的权限矩阵只给 `critic_agent`，
          而且它查不到时返回 `data=[]`（"黑板上没有这条"）而不是抛错 ——
          这正是 Critic 需要知道的事实（有人引用了不存在的证据）。
        ★ 没有执行器时退回"本地等价反查"（`CriticAgent` 自己会记一条 note），
          结论等价、只是没走权限通道。
        ★ 兜底链与 `forensics_node._forensics_runner` **逐项一致**
          （flags → 实例字段 → 内存黑板）：两处若各少一层，就会出现
          "取证读得到黑板、Critic 却反查不到"这种**同一块黑板两个答案**的怪象。
        """
        if self.executor is None:
            return self.critic
        board = (board_store(state, self.board, need="get_evidence")
                 or InMemoryBoardStore())
        # ★★ 每次 run 派生一个**独立的 ToolContext**：
        #   执行器是进程级的，而 `board` / `incident_id` 是**本次排查独有的**事实。
        #   曾经写成"只在字段为空时赋值" → 第二次请求复用了上一次的 incident，
        #   `evidence_lookup` 去上一块黑板找证据 → 报"证据读不回来"，
        #   表现为"所有结论都不可追溯"，而真相是"问错了事故"。
        # ★★ 只覆盖**本次排查独有**的两件事实（黑板与事故号）。
        #   ⚠️ 不许再传 `notes=`：取数留痕早已改成**会话级 ContextVar**（不再挂在
        #   `ToolContext` 上），多传这一个字段会让 `dataclasses.replace` 直接抛
        #   `TypeError: ToolContext.__init__() got an unexpected keyword argument 'notes'`
        #   —— 而 Critic 的失败被仲裁规则⓪翻译成"**拒答**"，
        #   表现为"**所有 RCA 都下不了结论**"（真机实测：`explanation.critic.error`
        #   就是这一行 TypeError）。
        base_context = getattr(self.executor, "context", None) or ToolContext()
        run_context = dataclasses.replace(
            base_context, board=board, incident_id=state.incident_id)
        run_executor = (self.executor.with_context(run_context)
                        if hasattr(self.executor, "with_context") else self.executor)
        return CriticAgent(lookup=_make_lookup(run_executor),
                           require_readable_evidence=self.critic.require_readable_evidence)
