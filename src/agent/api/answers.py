"""答案这一步的**唯一写入口**（从 `service.py` 抽出来，判据一个字没改）。

★ 为什么单列：答案这一步有自己的三条纪律 ——
  ① **只换措辞、不换结论**（模型拿到的是结构化事实，prompt 里写死"只依据事实"）；
  ② **证据必须显式带上**（`facts` 里只有 `evidence_count`，证据本身在 `state.evidence`）；
     ★ 漏这一步的后果实测过：模型只看到"这几条指标没有数据"，于是答案写成
     "这次没有可用的数据"，而**当前值其实就查到了**（真环境首跑就是这个）；
  ③ **上一轮的证据与时刻也要带上** —— 答案层要据此判"本轮到底有没有取证"，
     并把继承来的那批如实标注成"上一轮（时刻）"（见 `answer_guard.py`）。
"""

from __future__ import annotations

import time
from typing import Any

from ..common.steps import emit_step_phase, record_tool_call
from ..orchestration.evidence_scope import inherited_at_text, inherited_evidence
from .answer import AnswerWriter
from .grounding import conclusions_of, error_summary_of_result

__all__ = ["AnswerStepMixin"]


def _uses_plan_answer(state: Any) -> bool:
    flags = getattr(state, "flags", None) or {}
    return (bool(flags.get("remediation_plans"))
            and getattr(getattr(state, "intent", None), "value", "") == "REMEDIATION")


class AnswerStepMixin:
    """写白话答案（呈现层的产物，失败不影响主流程）。"""

    async def _answer_step(self, result: Any, message: str, trace_id: str,
                           on_event: Any = None) -> dict[str, Any]:
        """答案展示或模型总结成对发事件，并保存相同的完成状态供刷新恢复。"""
        state = result.state

        def own_sink(event: str, data: dict[str, Any]) -> None:
            if event == "tool":
                record_tool_call(state, data)
            if on_event is not None:
                on_event(event, {**data, "trace_id": trace_id,
                                 "incident_id": str(getattr(state, "incident_id", "") or ""),
                                 "round": int(getattr(state, "round", 0) or 0)})

        params = {"status": str(getattr(state, "status", "") or ""),
                  "intent": str(getattr(getattr(state, "intent", None), "value", "") or ""),
                  "evidence": len(getattr(state, "evidence", ()) or ())}
        title = "展示指标计算结果" if state.flags.get('metric_answer') else "展示主动核验结果" if state.flags.get('active_probe') else "展示知识回答" if state.flags.get('qa_result') else "展示处置方案" if _uses_plan_answer(state) else ("展示分层诊断" if
            (getattr(state,'flags',None) or {}).get('attribution_v2') is not None else "写白话答案（模型思考）")
        if str(getattr(getattr(state,'intent',None),'value','')) == 'OUT_OF_SCOPE':
            title = '说明意图识别结果'
        started = time.perf_counter()
        emit_step_phase(own_sink, "start", "answer", title, params)
        answer = await self._write_answer(result, message)
        emit_step_phase(own_sink, "done", "answer", title, params,
                        ok=str(answer.get("source") or "") in ("metric_tools","active_probe","fixed_wording","llm", "remediation_plans", "attribution_v2","knowledge_llm","knowledge_quotes","knowledge_no_match","knowledge_needs_reference","knowledge_failed"),
                        result=(f"来源 {answer.get('source')}"
                                f"（{answer.get('note') or '模型总结成功'}）；"
                                f"耗时 {round((time.perf_counter() - started) * 1000)}ms；"
                                f"{str(answer.get('summary') or '')[:160]}"))
        return answer

    async def _write_answer(self, result: Any, message: str) -> dict[str, Any]:
        """让模型把这一次的排查结果**总结成一句人话**。

        ★ 只换措辞、不换结论：模型拿到的是**结构化事实**，prompt 里写死
          "只依据事实、不许出现内部术语"；输出再过一遍黑名单，
          命中就**弃用**（退回白话模板）—— 宁可答案朴素，也不把内部术语印给用户。
        ★ 任何失败都不影响主流程（呈现层的产物）。
        """
        state = result.state
        intent = getattr(state, "intent", None)
        payload = dict(result.facts)
        payload["error_summary"] = error_summary_of_result(result)
        payload["root_cause_conclusions"] = conclusions_of(result)
        payload["user_message"] = message
        payload["evidence"] = [item.model_dump(mode="json")
                               for item in getattr(state, "evidence", ())]
        flags = getattr(state, "flags", None) or {}
        if flags.get('qa_result'):return flags['qa_result']['answer']
        if flags.get('metric_answer') and getattr(intent,'value','')=='METRIC_QUERY':
            from .metric_view import render
            return render(flags['metric_answer'],getattr(state.slots,'time_window',None))
        if (flags.get('active_probe') or {}).get('action')=='ask':
            return {'source':'active_probe','summary':flags['active_probe']['question'],'details':[],
                'note':'展示当前任务待补的接口/参数；没有生成业务请求授权'}
        if getattr(intent,'value','') == 'OUT_OF_SCOPE':
            return {'source':'fixed_wording','summary':state.report,'details':[],
                    'note':'直接展示识别/澄清结果；未取证，不生成故障总结'}
        route=getattr(state,'route',None)
        if str(getattr(state,'status',''))=='clarify' and getattr(getattr(route,'result',None),'clarification',None):
            return {'source':'fixed_wording','summary':state.report,'details':[],
                    'note':'展示实际待补信息，不额外调用模型总结未执行的诊断'}
        if _uses_plan_answer(state):
            return {"source": "remediation_plans", "summary": "", "details": [],
                    "note": "直接展示已校验的结构化方案，省去重复模型总结"}
        if flags.get('attribution_v2') is not None:
            from ..attribution.v2.presentation import summary
            return {'source':'attribution_v2','summary':summary(flags['attribution_v2']),
                    'details':[],'note':'展示已校验的分层诊断快照'}
        payload["inherited_evidence"] = list(inherited_evidence(flags))
        payload["inherited_at"] = inherited_at_text(flags)
        return await self._answers.write(
            str(getattr(state, "status", "") or ""),
            getattr(intent, "value", "") or "", payload)
