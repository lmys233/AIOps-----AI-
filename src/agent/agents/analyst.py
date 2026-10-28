"""**指标数值问答**：取数 → （模型选算法）→ 工具算数 → 结构化回答（契约修订 21）。

★★ 为什么单开这一路（用户 2026-10-03 的方案与实测）：
> "查指标意图，去查指标的时候，获取到的数据肯定是一些同类的数据，AI 拿到数据后大概率要做
>  平均、加权等数学运算。那 AIOps 先写一些数据运算的 tool，然后 AI 拿到数据后，把这些
>  tool list 注入提示词，让 AI 自己分析该怎么计算这些数据，自己 tool call 计算好数据后，
>  返回结构化回答就行。"

以及用户实测的痛点："我问 QPS 是多少……回答是：未得出结论，无法回答……"
—— 查数值**不是**故障归因：它不该走 8 类候选与仲裁（那条路的终点必然是"未定论"），
它的答案是**一个数**（以及这个数是怎么算出来的）。

三步（每步都可核对）：

| 步 | 谁做 | 产物 |
|---|---|---|
| ① 选算法 | **模型**（提示词里注入运算工具清单 + 这轮取到的数据清单） | `ToolPlan`：要跑哪几个工具、什么参数、为什么 |
| ② 算数 | **工具**（`series_stats`/`series_rate`/… 纯本地，可审计） | 每个工具的结果（数字） |
| ③ 写答案 | **模型**（只依据 ② 的结果写结构化答案） | `MetricAnswer`：值 / 单位 / 算法 / 步骤 / 口径 / 注意事项 |

★★ 两条纪律：

1. **数字只能来自 ②**：①③ 两次模型调用都不许自己算。答案里的数值若在工具结果里找不到，
   直接**弃用**改走确定性兜底（宁可答案朴素，也不能编一个数）。
2. **没有模型也能答**：走确定性兜底（对用户问的那个指标做 `series_stats` + `series_rate`），
   并如实标注"这次没有模型参与，用的是固定算法"。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final, Mapping, Optional, Sequence

from ..common.freshness import (METRIC_STALE_SEC, freshness_of,
                                join_caveat, neutralize_stale_claim)

from pydantic import BaseModel, Field

from ..llm.gateway import LLMGateway, LLMError

__all__ = ["MetricAnswer", "ToolPlan", "ToolPick", "MetricAnalyst",
           "ANALYST_ROLE", "describe_series"]

logger = logging.getLogger("agent.analyst")

#: 这次调用的角色名（进记账；两次调用分别是选算法与写答案）
ANALYST_ROLE: Final[str] = "metric_analyst"

#: 一次最多跑几个运算（有界：别让模型把 20 个工具全点一遍）
MAX_PICKS: Final[int] = 4

#: 参与运算的数值容差（答案里的数与工具结果比对时用；浮点格式差异不该判成"编的"）
_REL_TOLERANCE: Final[float] = 0.02


class ToolPick(BaseModel):
    """模型选的一次运算。"""

    tool: str = Field(..., description="运算工具名（只能从给定清单里选）")
    metric: str = Field(default="", description="对哪条指标算（数据清单里的指标名）")
    series_id: str = Field(default="", description="数据清单的唯一序列ID；同名指标必须区分服务/标签")
    right_series_id: str = Field(default="", description="series_compare 的第二条序列ID，必须明确选择")
    op: str = Field(default="", description="仅 math_eval 用：weighted_mean/sum/mean/ratio/pct_change")
    values: list[float] = Field(default_factory=list,
                                description="仅 math_eval 用：参与运算的数字（必须来自数据清单）")
    weights: list[float] = Field(default_factory=list,
                                 description="仅 math_eval 的加权平均用：与 values 等长的权重")
    why: str = Field(default="", description="为什么这样算（一句话，给用户看）")


class ToolPlan(BaseModel):
    """① 的产物：这次要跑哪几次运算。"""

    picks: list[ToolPick] = Field(default_factory=list,
                                  description="要跑的运算（1~4 个；问什么就算什么，不要凑数）")
    note: str = Field(default="", description="一句话说明你的算法思路（给用户看）")


class MetricAnswer(BaseModel):
    """③ 的产物：**结构化回答** —— 值 + 算法 + 口径 + 注意事项。"""

    value: Optional[float] = Field(default=None, description="结论数值（算不出来就给 null）")
    unit: str = Field(default="", description="单位，如 req/s、条连接、秒")
    metric: str = Field(default="", description="这是哪个指标")
    method: str = Field(default="", description="用什么算法得到的（如「窗口内均值」「每秒速率」）")
    steps: list[str] = Field(default_factory=list, description="算法步骤（每条一句，含具体数字）")
    caveats: str = Field(default="", description="需要注意的口径/局限（没有就留空）")
    answer: str = Field(default="", description="一句白话回答：先给数值，再说算法与口径")
    #: 这个值是**什么时候**的读数（ISO 时刻；取不到就留空 —— 不编）
    as_of: str = Field(default="", description="结论数值对应的**末次采样时刻**（ISO8601）")
    #: 末次采样距"现在"多少秒（判"能不能说此刻正常"）
    staleness_sec: Optional[float] = Field(default=None, description="末次采样距今秒数")

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "unit": self.unit, "metric": self.metric,
                "method": self.method, "steps": list(self.steps),
                "caveats": self.caveats, "answer": self.answer,
                "as_of": self.as_of, "staleness_sec": self.staleness_sec}


def describe_series(items: Sequence[Mapping[str, Any]]) -> str:
    """把"这轮取到了哪些数据"写成给模型看的清单（**含 raw_ref**：模型靠它点工具）。"""
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        lines.append(f"{item.get('series_id') or 'S'+str(index)}. 指标 {item.get('metric') or '（未命名）'}"
                     f"｜服务 {item.get('service') or '-'}"
                     f"｜末次采样值 {item.get('last')}{item.get('unit') or ''}"
                     f"｜末次采样时刻 {item.get('as_of') or item.get('last_ts') or '（未知）'}"
                     f"｜窗口起点 {item.get('first')}{item.get('unit') or ''}"
                     f"｜点数 {item.get('points')}"
                     f"｜类型 {item.get('type','unknown')}｜口径 {item.get('statistic','sample')}"
                     f"｜raw_ref={item.get('raw_ref')}")
    return "\n".join(lines) if lines else "（这轮没有取到可算的序列）"


def _tool_menu(tools: Sequence[Mapping[str, Any]]) -> str:
    """运算工具清单（**注入提示词**：名称 + 说明 + 参数）。"""
    lines: list[str] = []
    for tool in tools:
        spec = tool.get("function") or {}
        params = (spec.get("parameters") or {}).get("properties") or {}
        keys = "、".join(str(name) for name in params) or "（无参数）"
        lines.append(f"- {spec.get('name')}：{spec.get('description')}｜参数：{keys}")
    return "\n".join(lines)


_PLAN_PROMPT: Final[str] = (
    "你是一名 SRE 数据分析助手。用户问的是**一个指标的数值/现状**（不是故障根因）。\n"
    "下面给出：① 这轮从监控里取到的数据清单；② 可用的**数据运算工具**。\n"
    "请判断：为了回答用户的问题，应该对这数据做哪些运算？**只选必要的**（问什么算什么）。\n"
    "\n"
    "硬性要求：\n"
    "1. series_id 必须照抄数据清单ID；同名指标按服务分别选，不能把一个服务代替另一个；\n"
    "2. 只能从工具清单里选工具；参数要符合它的说明；\n"
    "3. **不要自己算数**：你只负责选算法，数字由工具给出；\n"
    "4. QPS、req/s、rate_1m 已是速率，用 series_stats 读取，不能再求一次速率。\n"
    "   只有原始累计计数才可 series_rate；趋势用 series_slope；\n"
    "   问「平均/加权」用 series_stats 取到数之后再考虑 math_eval；\n"
    "5. 最多选 4 个；数据清单为空时返回空的 picks。\n"
)

_ANSWER_PROMPT: Final[str] = (
    "你是一名 SRE 数据分析助手。用户的提问、数据清单、以及**已经算好的结果**都在下面。\n"
    "请写一个**结构化答案**：先给数值，再说用什么算法、什么窗口算出来的。\n"
    "\n"
    "硬性要求：\n"
    "1. ★★ **只使用「运算结果」里出现过的数字** —— 不许补充、推测或自己再算一次；\n"
    "2. `value` 填结论数值（同一个数的不同写法可以，但**不许出现结果里没有的数**）；\n"
    "3. `method` 说清算法（如「窗口内均值」「首末差分/时长 = 每秒速率」）；\n"
    "4. `steps` 每条带具体数字；`caveats` 写口径与局限（如「窗口内无请求样本」）；\n"
    "5. 查不到就说查不到（`value` 给 null 并在 `caveats` 说明），**不要编一个 0**；\n"
    "6. ★★ 必须写清**末次采样时刻**（`as_of` 填 ISO 时刻，`staleness_sec` 填距今秒数）；\n"
    f"7. ★★ 若末次采样距今超过 {METRIC_STALE_SEC:.0f} 秒，**不许**说「当前正常 / 没有异常」——\n"
    "   只能说「最近一次采样（时刻 X）没有异常迹象；此后没有新采样，**不能据此判断此刻状态**」；\n"
    "8. 不要出现内部术语、代码块或 JSON 之外的标记。\n"
)


class MetricAnalyst:
    """指标数值问答的分析器（① 选算法 → ② 工具算数 → ③ 写答案）。"""

    def __init__(self, gateway: Optional[LLMGateway] = None, *, model: str = "",
                 executor: Any = None) -> None:
        self.gateway = gateway
        self.model = (model or "").strip()
        self.executor = executor

    @property
    def available(self) -> bool:
        return self.gateway is not None and self.executor is not None

    async def analyze(self, *, question: str, series: Sequence[Mapping[str, Any]],
                      tools: Sequence[Mapping[str, Any]], role: str = "metrics_agent",
                      on_event: Optional[Any] = None) -> dict[str, Any]:
        """跑完三步，返回 `{"answer": dict, "computations": [...], "source": ...}`。"""
        if not self.available:
            return await self._fallback(question, series, role, on_event=on_event,
                                        why="这次没有模型参与（未配置网关）")
        try:
            plan = await self._plan(question, series, tools)
        except LLMError as exc:
            logger.warning("选算法失败（%s）→ 走确定性兜底", exc.code)
            return await self._fallback(question, series, role, on_event=on_event, why=f"选算法失败（{exc.code}）")
        results = await self._compute(plan, series, role, on_event)
        if not results:
            return await self._fallback(question, series, role, on_event=on_event, why="模型没有选出可执行的运算")
        try:
            answer = await self._write(question, series, plan, results)
        except LLMError as exc:
            logger.warning("写答案失败（%s）→ 用工具结果直接成文", exc.code)
            return self._from_results(question, results, source="tools_only",
                                      why=f"写答案失败（{exc.code}）")
        invented = _invented_numbers(answer, results)
        if invented:
            logger.warning("答案里出现了工具没算过的数 %s → 弃用，改用工具结果直接成文", invented)
            return self._from_results(question, results, source="tools_only",
                                      why=f"模型答案含未计算的数 {invented[:3]}")
        return {"answer": answer.to_dict(), "computations": results, "source": "llm",
                "why": plan.note}

    # ------------------------------------------------------------------
    async def _plan(self, question: str, series: Sequence[Mapping[str, Any]],
                    tools: Sequence[Mapping[str, Any]]) -> ToolPlan:
        """① 让模型选算法（提示词里注入**工具清单**与数据清单）。"""
        prompt = (f"{_PLAN_PROMPT}\n## 用户的问题\n{question}\n\n"
                  f"## 这轮取到的数据\n{describe_series(series)}\n\n"
                  f"## 可用的数据运算工具\n{_tool_menu(tools)}\n")
        raw = await self.gateway.generate(prompt, ToolPlan, role=ANALYST_ROLE,
                                         model=self.model or None, temperature=0.0,
                                         max_tokens=2048)
        picks = list(getattr(raw, "picks", None) or [])[:MAX_PICKS]
        return ToolPlan(picks=picks, note=str(getattr(raw, "note", "") or ""))

    async def _compute(self, plan: ToolPlan, series: Sequence[Mapping[str, Any]],
                       role: str, on_event: Optional[Any] = None) -> list[dict[str, Any]]:
        """② **工具算数**（走执行器：权限、审计、留痕一个不少）。"""
        by_id = {str(item.get('series_id') or f'S{i+1}'):item for i,item in enumerate(series)}
        out: list[dict[str, Any]] = []
        for pick in plan.picks:
            if pick.tool=='math_eval':
                known=[v for item in series for v in (item.get('first'),item.get('last')) if isinstance(v,(int,float))]
                known.extend(v for item in out if item.get('ok') for v in _floats_of(item.get('data')))
                if not pick.values or any(v not in known for v in [*pick.values,*pick.weights]):continue
                params=self._params_of(pick,{})
                _emit(on_event,'start',pick.tool,params,why=pick.why)
                outcome=await self._call(pick.tool,params,role)
                _emit(on_event,'done',pick.tool,params,outcome=outcome)
                if outcome is not None:out.append(outcome)
                continue
            selected=by_id.get(pick.series_id) if pick.series_id else None
            if not pick.series_id:
                matches=[item for item in series if item.get('metric')==pick.metric]
                selected=matches[0] if len(matches)==1 else None
            if selected is None:continue
            if pick.tool not in ('series_stats','series_rate','series_change','series_slope','series_compare'):continue
            if pick.tool=='series_rate' and (selected.get('type')!='counter' or selected.get('statistic')=='rate_1m'):continue
            params = self._params_of(pick, {str(selected.get('metric')):str(selected.get('raw_ref') or '')})
            if pick.tool=='series_compare':
                right=by_id.get(pick.right_series_id)
                if right is None or right.get('unit')!=selected.get('unit'):continue
                params={'left_ref':selected['raw_ref'],'right_ref':right['raw_ref'],'metric':''}
            if params is None:
                continue
            _emit(on_event, "start", pick.tool, params, why=pick.why)
            outcome = await self._call(pick.tool, params, role)
            _emit(on_event, "done", pick.tool, params, outcome=outcome)
            if outcome is not None:
                outcome['series']=dict(selected)
                out.append(outcome)
        return out

    def _params_of(self, pick: ToolPick, by_metric: Mapping[str, str]) -> Optional[dict[str, Any]]:
        """把模型的选择翻成工具入参；**引用必须来自数据清单**（防它编一个 raw_ref）。"""
        if pick.tool == "math_eval":
            return {"op": pick.op or "mean", "values": list(pick.values),
                    "weights": list(pick.weights)}
        raw_ref = by_metric.get(pick.metric) or (next(iter(by_metric.values()), "") if not pick.metric and len(by_metric)==1 else "")
        if not raw_ref:
            return None
        if pick.tool == "series_compare":
            return {"left_ref": raw_ref, "right_ref": raw_ref, "metric": pick.metric}
        return {"raw_ref": raw_ref, "metric": pick.metric}

    async def _call(self, tool: str, params: Mapping[str, Any],
                    role: str) -> Optional[dict[str, Any]]:
        try:
            result = await self.executor.call(role, tool, dict(params))
        except Exception as exc:  # noqa: BLE001 —— 一次运算失败不该带走整轮
            logger.warning("运算 %s 执行异常：%s", tool, type(exc).__name__)
            return {"tool": tool, "params": dict(params), "ok": False,
                    "summary": f"运算没跑成（{type(exc).__name__}）", "data": None}
        return {"tool": tool, "params": dict(params), "ok": bool(getattr(result, "ok", False)),
                "summary": str(getattr(result, "summary", "") or ""),
                "data": getattr(result, "data", None)}

    async def _write(self, question: str, series: Sequence[Mapping[str, Any]],
                     plan: ToolPlan, results: Sequence[Mapping[str, Any]]) -> MetricAnswer:
        """③ 让模型**只依据工具结果**写结构化答案。"""
        lines = [f"- {item['tool']}｜参数 {item['params']}｜结果 {item['summary']}"
                 f"｜数 {item['data']}" for item in results]
        prompt = (f"{_ANSWER_PROMPT}\n## 用户的问题\n{question}\n\n"
                  f"## 这轮取到的数据\n{describe_series(series)}\n\n"
                  f"## 运算结果（数字的唯一来源）\n" + "\n".join(lines) + "\n")
        raw = await self.gateway.generate(prompt, MetricAnswer, role=ANALYST_ROLE,
                                         model=self.model or None, temperature=0.0,
                                         max_tokens=2048)
        answer = MetricAnswer(**raw.model_dump())
        # ★★ 采样时刻**以工具结果为准**（模型说了不算）：取不到就留空，不编
        as_of, staleness = freshness_of(results)
        answer = answer.model_copy(update={
            "as_of": as_of,
            "staleness_sec": staleness,
            "answer": neutralize_stale_claim(answer.answer, staleness, as_of),
        })
        if staleness is not None and staleness > METRIC_STALE_SEC:
            answer = answer.model_copy(update={
                "caveats": join_caveat(answer.caveats, staleness, as_of)})
        return answer

    # ------------------------------------------------------------------
    async def _fallback(self, question: str, series: Sequence[Mapping[str, Any]],
                        role: str, *, why: str,
                        on_event: Optional[Any] = None) -> dict[str, Any]:
        """确定性兜底：对"用户问的那个指标"（认不出就第一条）做统计 + 速率。"""
        target = _match_metric(question, series) or (series[0] if series else None)
        if target is None:
            return {"answer": {"value": None, "unit": "", "metric": "", "method": "",
                               "steps": [], "caveats": "这轮没有取到任何可算的序列",
                               "answer": "这次没有取到可算的数据。",
                               "as_of": "", "staleness_sec": None},
                    "computations": [], "source": "none", "why": why}
        results: list[dict[str, Any]] = []
        targets=[item for item in series if item.get('metric')==target.get('metric')][:MAX_PICKS]
        for selected in targets:
            tool='series_stats';params={"raw_ref":selected.get('raw_ref'),"metric":selected.get('metric')}
            _emit(on_event, "start", tool, params, why="确定性兜底：分别报告各服务的样本统计")
            outcome = await self._call(tool, params, role)
            _emit(on_event, "done", tool, params, outcome=outcome)
            if outcome is not None:
                outcome['series']=dict(selected)
                results.append(outcome)
        return self._from_results(question, results, source="fallback", why=why)

    def _from_results(self, question: str, results: Sequence[Mapping[str, Any]], *,
                      source: str, why: str) -> dict[str, Any]:
        """不靠模型成文：把工具结果直接说清楚（**数字仍然是工具算的**）。"""
        stats = next((item for item in results
                      if item["tool"] == "series_stats" and item.get("data")), None)
        rate = next((item for item in results
                     if item["tool"] == "series_rate" and item.get("data")), None)
        if stats is None and rate is None:
            first = results[0] if results else {"summary": "没有跑成任何运算"}
            return {"answer": {"value": None, "unit": "", "metric": "", "method": "",
                               "steps": [str(first.get("summary"))],
                               "caveats": str(first.get("summary")),
                               "answer": f"这次没能算出结果：{first.get('summary')}",
                               "as_of": "", "staleness_sec": None},
                    "computations": list(results), "source": source, "why": why}
        # ★ 取哪个数要与**取哪份 data** 对齐（踩过：判定用 rate、取数却用了 stats 的 data → value 恒为 None）
        picked = stats or rate
        data = (picked or {}).get("data") or {}
        value = data.get("per_second") if picked is rate else data.get("last")
        unit = "每秒" if picked is rate else str((picked.get('series') or {}).get('unit') or '')
        steps = [f"{(item.get('series') or {}).get('service','')} {(item.get('series') or {}).get('metric','')}：{item.get('summary')} {(item.get('series') or {}).get('unit','')}" for item in results if item.get("summary")]
        answer = (f"按工具算出来的结果：{steps[0] if steps else ''}"
                  + (f"；{steps[1]}" if len(steps) > 1 else ""))
        as_of, staleness = freshness_of(results)
        caveat = f"（{why}）"
        if staleness is not None and staleness > METRIC_STALE_SEC:
            caveat += join_caveat("", staleness, as_of)
        return {"answer": {"value": value, "unit": unit,
                           "metric": str(_metric_name_of(results)),
                           "method": "；".join(str(item["tool"]) for item in results),
                           "steps": steps, "caveats": caveat,
                           "answer": neutralize_stale_claim(answer, staleness, as_of),
                           "as_of": as_of, "staleness_sec": staleness},
                "computations": list(results), "source": source, "why": why}


def _emit(on_event: Optional[Any], phase: str, tool: str, params: Mapping[str, Any], *,
          why: str = "", outcome: Optional[Mapping[str, Any]] = None) -> None:
    """把**每一次运算**也当成工具调用发出去（与取数那一路同形：start 带参数、done 带结果）。"""
    if on_event is None:
        return
    payload = {"phase": phase, "call_id": f"math-{tool}-{abs(hash(str(params))) % 10 ** 6}",
               "name": tool, "title": f"运算 · {tool}", "role": "metrics_agent",
               "params": dict(params), "why": why, "ok": True, "degraded": False,
               "error_code": "", "elapsed_ms": 0.0, "raw_ref": "",
               "result": str((outcome or {}).get("summary") or "")[:400]}
    if outcome is not None:
        payload["ok"] = bool(outcome.get("ok", True))
        payload["degraded"] = not payload["ok"]
    on_event("tool", payload)


def _metric_name_of(results: Sequence[Mapping[str, Any]]) -> str:
    for item in results:
        metric = str((item.get("params") or {}).get("metric") or "")
        if metric:
            return metric
    return ""


def _match_metric(question: str, series: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """问题里提到的指标名（QPS→qps、连接池→pool_*…）—— 认不出就交给第一条。"""
    text = str(question or "").lower()
    aliases = {"qps": ("qps", "每秒", "请求量", "吞吐"), "pool_waiting": ("等待", "waiting"),
               "pool_in_use": ("占用", "in_use", "使用中"), "pool_acquire_p95": ("获取连接", "acquire"),
               "http_latency_p95": ("延迟", "latency", "耗时"), "http_error_rate": ("错误率", "error"),
               "fault_injection": ("注入", "开关")}
    for item in series:
        metric = str(item.get("metric") or "").lower()
        words = aliases.get(metric, (metric,))
        if any(word in text for word in words):
            return item
    return None


_NUMBER: Final[re.Pattern[str]] = re.compile(r"-?\d+(?:\.\d+)?")


def _invented_numbers(answer: MetricAnswer,
                      results: Sequence[Mapping[str, Any]]) -> list[float]:
    """答案里有没有**工具没算过**的数（有 → 这份答案不可信）。

    ★ 只比对"数值"，且允许 2% 相对误差：模型写 `0.2313` 与工具给的 `0.23129…` 是同一个数，
      而写一个凭空的 `0.85` 会被抓出来 —— 这是"数字只能来自工具"这条纪律的判据。
    """
    corpus: list[float] = []
    for item in results:
        data = item.get("data")
        if item.get('ok',True) and isinstance(data, Mapping):
            corpus.extend(_floats_of(data))
        elif item.get('ok',True) and isinstance(data, (int, float)):
            corpus.append(float(data))
    text = " ".join([answer.answer, answer.method, answer.caveats, *answer.steps])
    bad: list[float] = []
    if answer.value is not None and not any(abs(answer.value-known)<=max(abs(known),1e-9)*_REL_TOLERANCE for known in corpus):
        bad.append(float(answer.value))
    for token in _NUMBER.findall(text):
        number = float(token)
        if number in corpus:
            continue
        if any(abs(number - known) <= max(abs(known), 1e-9) * _REL_TOLERANCE for known in corpus):
            continue
        if number in (0.0, 1.0, 2.0, 100.0):        # 口径类常数（0/1/2/100%）不算编造
            continue
        bad.append(number)
    return bad


def _floats_of(source: Any, depth: int = 0) -> list[float]:
    """递归取一个结构里的所有有限数（用于"答案里的数是否算过"的比对）。"""
    if depth > 3:
        return []
    if isinstance(source, bool) or source is None:
        return []
    if isinstance(source, (int, float)):
        return [float(source)]
    if isinstance(source, Mapping):
        out: list[float] = []
        for value in source.values():
            out.extend(_floats_of(value, depth + 1))
        return out
    if isinstance(source, (list, tuple)):
        out = []
        for value in source:
            out.extend(_floats_of(value, depth + 1))
        return out
    return []
