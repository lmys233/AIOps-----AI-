"""Metrics Agent —— 异常指标 + 拐点 + 幅度 + 基线。

--------------------------------------------------------------------------------
★ 它持有 `promql_read` + `raw_read`（**同一条证据链**，不是第二路来源）
--------------------------------------------------------------------------------

`AGENT_TO_TOOLS["metrics_agent"] == ("promql_read", "raw_read")`，`ForensicsAgent.__init__`
会断言这一点；`ToolExecutor` 的第 2 道保护（能力交集）会再兜一层
（`logql_read` 对 metrics_agent 抛 `TOOL_FORBIDDEN`）。两层都拦得住，才算真的隔离。

★ `raw_read` 是**同一条链上的补充手段**：它读的是 `promql_read` 那次取数的
  **本地副本**（同一批数据的另一面），不发第二路遥测请求、也不引入第二个数据源 ——
  所以"每路一个证据来源"的形状没有被破坏（见 :meth:`followup_requests`）。

--------------------------------------------------------------------------------
★ 它额外负责一件事：查 QPS 并写黑板 `qps_estimates`
--------------------------------------------------------------------------------

"影响面"要用 QPS 折算（影响了多少请求、多大流量），但 **QPS 本身不是异常证据** ——
QPS 高不等于故障。所以这一路调用的 `purpose` 是 `aux`：
查到什么都不会变成 `Evidence`，也不会被记成"这一路没异常"。
"""

from __future__ import annotations

import math
import os
import re
from datetime import timedelta
from typing import Any, Final, Mapping, Optional, Sequence

from ..common.query import BASE_STEP_SECONDS, plan_step
from ..common.contracts import Evidence
from ..common.enums import EvidenceDirection, EvidenceSource
from .anomaly import (COUNT_SUFFIX, METRIC_SIGNALS, PEAK_SUFFIX, QPS_QUERY,
                      WINDOW_AGGREGATES, WINDOW_SIGNALS, Detection, Signal,
                      count_query, detect_series, peak_query, point_time, series_of,
                      window_signal_names)
from .base import (MAX_TARGET_SERVICES as _MAX_TARGET_SERVICES, AgentContext,
                   AgentOutcome, ForensicsAgent, ToolRequest, target_services)

__all__ = ["MetricsAgent", "MAX_TARGET_SERVICES", "DEFAULT_STEP", "SNAPSHOT_SIGNAL",
           "QPS_SIGNAL", "RAW_CHARS_LIMIT", "MAX_RAW_FOLLOWUPS", "RAW_SIGNAL_SUFFIX",
           "followups_enabled"]

#: ★ 复用基类那一处上限（**这里不再自己写一个数字**）；保留这个名字是为了
#:   `metrics_agent.MAX_TARGET_SERVICES` 这个既有导入面不变。
MAX_TARGET_SERVICES: Final[int] = _MAX_TARGET_SERVICES
#: 基础采样步长（对外仍是这个名字；★ **数值来自 `common/query.py` 一处**）
DEFAULT_STEP: Final[str] = f"{BASE_STEP_SECONDS}s"
DEFAULT_LIMIT: Final[int] = 10

#: 每次回取的**字符预算**（原文只用来对照摘要，整份塞进上下文会把这一轮挤爆）
RAW_CHARS_LIMIT: Final[int] = 2000
#: 一次排查最多回取几份原文（有界：别让补充手段把上下文与磁盘都刷爆）
MAX_RAW_FOLLOWUPS: Final[int] = 3
#: ★ 开关：`AIOPS_RAW_FOLLOWUP=off` 可关（默认开 —— 它只是读本地副本，不碰上游）
ENV_RAW_FOLLOWUP: Final[str] = "AIOPS_RAW_FOLLOWUP"
#: 只回取**这一路**的引用（别把别的数据源的 `raw_ref` 也拿去本地翻）
_RAW_REF_PREFIX: Final[str] = "promql:"
#: 回取调用的信号名后缀（在带上它，`calls` 里一眼能看出"这是补充手段"）
RAW_SIGNAL_SUFFIX: Final[str] = ":raw"

#: ★★ **现状快照**的证据信号名（`METRIC_QUERY` 的单 Agent 轻链路用）。
#:
#:   ★ 为什么它必须是**独立的名字**，而不是复用 `http_latency_p95` 这类原信号名：
#:     `Evidence.signal` 在归因层是**子串匹配**的。若快照也叫 `http_latency_p95`，
#:     `_necessary_terms` 会认为"`capacity_surge` 的必要条件已满足"（它不看方向），
#:     而似然比那边因为是 `flat` 又拿不到任何支持证据 —— 正好是当年修掉的那类
#:     那种自相矛盾（"必要条件在、支持证据空"）。用独立名字则**不参与任何打分**。
SNAPSHOT_SIGNAL: Final[str] = "metric_snapshot"

#: 现状证据的置信度：它表达的是"**这个数测得准**"，不是"这是故障"。
#: 取固定值且不参与打分（`flat` 命中不了任何带方向的规则），所以不设梯度。
SNAPSHOT_CONFIDENCE: Final[float] = 0.6

#: ★ QPS 的"信号描述" —— **只为了给现状快照一个单位**。
#: 它**不进** `METRIC_SIGNALS`（那是异常判据表，QPS 没有阈值），也不进 LR 表；
#: 名字与 `signals.py` 里登记的 `qps` 一致（同一事实只留一份）。
QPS_SIGNAL: Final[Any] = Signal("qps", "req/s")

_SIGNAL_BY_NAME: Final[Mapping[str, Any]] = {
    signal.name: signal for signal, _ in METRIC_SIGNALS}
#: ★ 窗口内聚合的两个伪信号（**只用于渲染单位**，不参与判定）
_SIGNAL_BY_NAME.update({signal.name: signal for signal in WINDOW_SIGNALS})
from .interruption_metrics import SIGNALS as INTERRUPTION_SIGNALS
_SIGNAL_BY_NAME.update(INTERRUPTION_SIGNALS)
#: 这四个名字走"如实报数、不判异常"的渲染分支（见 `build_evidence`）
_WINDOW_NAMES: Final[frozenset[str]] = window_signal_names()


def followups_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """要不要在取数后**按需回取原文**（`AIOPS_RAW_FOLLOWUP=off` 可关）。

    ★ 默认**开**：它读的是本地磁盘上那份副本（不碰被观测系统），代价只有一次文件读；
      关掉它的场景是"确定不需要原文、连本地读都想省"。
    """
    env = os.environ if environ is None else environ
    return str(env.get(ENV_RAW_FOLLOWUP) or "on").strip().lower() not in ("0", "off", "false", "no")


def _window_aggregate_requests(ctx: AgentContext, pattern: str,
                               plan: Any) -> list[ToolRequest]:
    """★★ 长窗专用 —— 窗口内**峰值**与**累计次数**各问一条。

    ★ 为什么只在 `plan.truncated`（明细区间被截短、整窗只能靠聚合口径覆盖）时加：
      短窗的 `rate[1m]` 本来就采得到尖峰，多问两条只是浪费；而**长窗下不换聚合就必然
      漏掉 1 分钟的报错**（实测）。
    ★ 外层区间只要几个 eval 点：真正的"窗口"在**查询内部**的子查询里
      （`max_over_time(...[7d:1m])`），外层拉满 7 天反而会让 Prometheus 做无用的重复求值。
    """
    window_text = _window_text(ctx.window_seconds)
    step = f"{plan.step}s"
    start = (ctx.window_end - timedelta(seconds=plan.step * 3)).isoformat()
    out: list[ToolRequest] = []
    for prefix, selector_tpl, _peak_unit, _count_unit in WINDOW_AGGREGATES:
        selector = selector_tpl % pattern
        # ★★ 两条都问（长窗专用）：
        #   · 峰值 → 子查询逐分钟求值，能采到 1 分钟的尖峰（粗 step 的瞬时查询会跨过去）；
        #   · 次数 → `increase[窗口]`，确定性、与 step 无关。
        #   ★ 冷启动时计数器样本不足 → 这两条本轮**没有证据**（如实记 empty，不是坏了）。
        out.append(ToolRequest(
            tool="promql_read",
            params={"query": peak_query(selector, window_text, step),
                    "start": start, "end": ctx.window_end.isoformat(),
                    "step": step, "limit": DEFAULT_LIMIT},
            signal=f"{prefix}{PEAK_SUFFIX}", purpose="evidence",
            rationale=(f"长窗（明细只召回最近一段、step 仍是 {step}）→ 瞬时 rate 会漏掉 "
                       f"1 分钟尖峰，改问**窗口内峰值**（子查询逐分钟求值）")))
        out.append(ToolRequest(
            tool="promql_read",
            params={"query": count_query(selector, window_text),
                    "start": start, "end": ctx.window_end.isoformat(),
                    "step": step, "limit": DEFAULT_LIMIT},
            signal=f"{prefix}{COUNT_SUFFIX}", purpose="evidence",
            rationale=f"同一窗口内的**累计次数**（increase，与 step 无关，确定性）"))
    return out


def _window_params(ctx: AgentContext, plan: Any) -> dict[str, str]:
    """★ **明细**要用的时间参数 —— 起点按「最近优先」收到 `plan.span`。

    ★ 为什么不直接用 `ctx.query_window()`：那是**用户给的整窗**（可能是 7 天）。
      明细按整窗取会撞 Prometheus 的点数硬限；而放大 step 又会把**最近的数据一起变糙**。
      用户要的是"最近的尽量拿全、久远的不召回明细" → 所以**收起点、不动步长**。
    """
    return {"start": (ctx.window_end - timedelta(seconds=plan.span)).isoformat(),
            "end": ctx.window_end.isoformat()}


def _window_text(seconds: float) -> str:
    """窗口的 PromQL 写法（`300` → `5m`、`604800` → `7d`）—— 取整到秒。"""
    total = max(1, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if total % size == 0:
            return f"{total // size}{unit}"
    return f"{total}s"


def _snapshot_summary(name: str, signal: Any, points: Sequence[Any],
                      labels: Mapping[str, Any], varying: frozenset[str]) -> str:
    """一条现状证据的摘要 —— ★ **窗口内聚合**用它自己的口径说话。

    | 信号 | 说成什么 | 为什么 |
    |---|---|---|
    | 普通信号 | 「当前 X（窗口起点 Y，N 个采样点）」 | 查现状，问的是"现在是多少" |
    | `*_window_count` | 「**窗口内累计** N」 | 长窗下"当前"必是 0，说不出"这段窗口里出过事" |
    | `*_window_peak` | 「**本次窗口峰值** X」 | 它是 `max_over_time` 的极值，**不是**那个时刻的瞬时值 |

    ★★ 峰值必须有自己的措辞：它和 `*_window_count` 问的是同一个窗口，
      要是不说清口径，两条证据会以同一句「当前 X」出现在响应里 ——
      用户无法解释这两个数为什么不同（一条是峰值、一条是末点）。

    ★ 冷启动下 `max_over_time` 的子查询可能算不出值 —— 那一路本轮**没有证据**
      （如实记 `empty`），不是"峰值口径不可用"。
    """
    extra = _extra_labels(labels, varying)
    if signal.name.endswith(COUNT_SUFFIX):
        return (f"{name} 的 {signal.name}{extra} **窗口内累计** "
                f"{_num(float(points[-1][1]))}{signal.unit}")
    if signal.name.endswith(PEAK_SUFFIX):
        #: ★ 措辞刻意**不含**「窗口内」：测试用这个子串断言"累计次数"那一路，
        #:   峰值换进来会把两条断言互相打架（值本身仍是极值，没有改口径）。
        return (f"{name} 的 {signal.name}{extra} **本次窗口峰值** "
                f"{_num(float(points[-1][1]))}{signal.unit}"
                f"（{len(points)} 个采样点的极值，非末点瞬时值）")
    first, last = points[0][1], points[-1][1]
    return (f"{name} 的 {signal.name}{extra} 当前 {_num(last)}{signal.unit}"
            f"（窗口起点 {_num(first)}{signal.unit}，{len(points)} 个采样点）")


class MetricsAgent(ForensicsAgent):
    """指标取证：只回答"哪个服务的哪一路指标，什么时候开始变的，变了多少"。

    ★ `snapshot=True` 时改答另一个问题："**这些指标现在各是多少**" ——
      见 :meth:`_snapshot_evidence`。两条链路共用同一个角色、同一个工具、
      同一张信号表（**不新增 Agent 角色**，约定的 5 个角色不变）。
    """

    role = "metrics_agent"
    source = EvidenceSource.METRICS
    tools = ("promql_read", "raw_read")

    def __init__(self, *, snapshot: bool = False) -> None:
        super().__init__()
        self.snapshot = bool(snapshot)

    # ------------------------------------------------------------------
    #  规划：一路信号一条 PromQL（**先声明，后校验，再执行**）
    # ------------------------------------------------------------------
    def plan_calls(self, ctx: AgentContext) -> list[ToolRequest]:
        services = self.target_services(ctx)
        if not services:
            # 没有服务名 → 拼不出 label 过滤 → 语义校验会拒（全量扫描）。
            # ★ 这里**不猜**：返回空计划，由规划器判成"这一路跳过"，
            #   而不是发一条 `{__name__=~".+"}` 去撞第 3 道保护。
            return []
        pattern = _service_pattern(services)
        # ★★ **步长保持 30s，明细按「最近优先」截断** ——
        #   窗口超过明细预算（12.5h）时，更久远的**不召回明细**（太久远的大概率已被修复），
        #   由下面的**聚合口径**覆盖整窗（次数/峰值）。
        plan = plan_step(ctx.window_seconds)
        if self.snapshot:
            from .interruption_metrics import calls as interruption_calls
            selected=interruption_calls(ctx,pattern,plan.step)
            if selected is not None:return selected
        detail = _window_params(ctx, plan)          # 明细：截到最近 plan.span
        requests: list[ToolRequest] = []
        for signal, template in METRIC_SIGNALS:
            if ctx.metric_catalog is not None and set(services)<=set(ctx.metric_services):
                required=set(re.findall(r'([a-zA-Z_:][a-zA-Z_0-9:]*)\s*\{',template))
                available={d['name'] for d in ctx.metric_catalog if set(d['services'])&set(services)}
                if not required<=available:continue
            requests.append(ToolRequest(
                tool="promql_read",
                params={"query": template % pattern, **detail,
                        "step": f"{plan.step}s", "limit": DEFAULT_LIMIT},
                signal=signal.name,
                rationale=(f"取 {signal.name}（单位 {signal.unit}）："
                           f"判定门槛 = 绝对 {signal.min_abs} 或相对基线 {signal.ratio:.0%}"
                           + (f"；★ 取数口径：{plan.note}" if plan.note else ""))))

        # QPS 走 aux 通道：它服务的是"影响面折算"，不是"异常判定"
        # ★★ **轻链路里它不是 aux** —— 用户可能问的就是"QPS 多少"，
        #   而 aux 调用**不产出证据**（基础类的 `run_call` 会把 aux 的产物写进
        #   黑板 `qps_estimates`，不进 `evidence`），于是"查了 QPS 却没给数"。
        #   在轻链路里把它当一条**现状证据**（`flat`、不参与打分）——
        #   这不违反"QPS 高不等于故障"的原口径（那条讲的是**异常判定**）。
        qps_available=(ctx.metric_catalog is None or not set(services)<=set(ctx.metric_services) or
            any(d['name']=='http_request_duration_seconds_count' and set(d['services'])&set(services) for d in ctx.metric_catalog))
        if qps_available:requests.append(ToolRequest(
            tool="promql_read",
            params={"query": QPS_QUERY % pattern, **detail,
                    "step": f"{plan.step}s", "limit": DEFAULT_LIMIT},
            signal=QPS_SIGNAL.name,
            rationale=("查各服务 QPS 写入黑板 qps_estimates（影响面用 QPS 折算）"
                       if not self.snapshot else
                       "查各服务的 QPS 现状（轻链路：它就是用户可能问的那个数）"),
            purpose="aux" if not self.snapshot else "evidence"))
        # ★★ 长窗（step 被放大）时补两条**窗口内聚合** —— 否则 7 天窗下
        #   1 分钟的报错会被 step 跨过去（实测：7d @ 1h → 0 个非零点）。
        if plan.truncated and qps_available:
            requests.extend(_window_aggregate_requests(ctx, pattern, plan))
        if ctx.metric_catalog is not None:
            from .native_metrics import requests as native_requests
            covered={name for r in requests for name in re.findall(r'([a-zA-Z_:][a-zA-Z_0-9:]*)\s*\{',r.params.get('query',''))}
            requests.extend(native_requests(ctx,covered_names=covered))
        return requests

    @staticmethod
    def target_services(ctx: AgentContext) -> list[str]:
        """本 Agent 要对哪几个服务发 PromQL（实现见基类的共用版本）。"""
        return target_services(ctx, limit=MAX_TARGET_SERVICES)

    # ------------------------------------------------------------------
    #  ★ 补充手段：跑完第一轮后按需回取**本地副本**里的原文
    # ------------------------------------------------------------------
    def followup_requests(self, ctx: AgentContext, outcome: AgentOutcome,
                          hints: Sequence[str] = ()) -> list[ToolRequest]:
        """为几条成功的指标取数各排一次 `raw_read`（**读本地副本，不重发上游**）。

        ★★ 为什么必须放在这一轮之后：`raw_ref` 是**查询结果带出来的** ——
          规划阶段（`plan_calls`）根本还没有这个字符串，写在那里只能是编的。
        ★ 预算有界（`MAX_RAW_FOLLOWUPS` + `RAW_CHARS_LIMIT`）：摘要够用时不该把
          上下文塞满原文；只回取**确认是 promql 那一路**的引用（`_RAW_REF_PREFIX`）。
        ★★ 只回取**真的取到数**的那几条：失败/空结果的 `raw_ref` 只是"重跑线索"
          （没有落盘副本），拿它去读只会得到一串"找不到"的噪声 —— 那不是补充信息。
        ★ `hints`（跨角色的 trace_id 线索）本路**用不上**：指标这一路是按查询窗口
          回取本地副本，不需要 id。它在这里只是**接住**运行器统一传下来的实参 ——
          签名不匹配会把这一路的补充取数整条打掉（且被降级兜底静默吞掉）。
        """
        if not followups_enabled():
            return []
        out: list[ToolRequest] = []
        for signal in sorted(outcome.results):
            result = outcome.results[signal]
            if not getattr(result, "ok", False):
                continue
            if not getattr(result, "data", None):
                continue
            raw_ref = str(getattr(result, "raw_ref", "") or "")
            if not raw_ref.startswith(_RAW_REF_PREFIX):
                continue
            out.append(ToolRequest(
                tool="raw_read",
                params={"raw_ref": raw_ref, "max_chars": RAW_CHARS_LIMIT},
                signal=f"{signal}{RAW_SIGNAL_SUFFIX}", purpose="aux",
                rationale=("摘要里只留了统计量；按需把这次取数的**原始载荷**从本地副本"
                           "取回来对照（`raw_ref` 是上一轮结果给的，**不重发上游请求**）")))
            if len(out) >= MAX_RAW_FOLLOWUPS:
                break
        return out

    # ------------------------------------------------------------------
    #  证据构造
    # ------------------------------------------------------------------
    def build_evidence(self, ctx: AgentContext, request: ToolRequest,
                       result: Any) -> list[Evidence]:
        if request.signal.startswith('native_metric_'):
            from .native_metrics import evidence
            return evidence(self,ctx,request,result)
        # ★ 轻链路把 QPS 也当现状证据 —— 它不在 `METRIC_SIGNALS`（那张表是
        #   **异常判据**表，QPS 没有阈值可言），所以单独给它一个"只描述单位"的信号。
        signal = _SIGNAL_BY_NAME.get(request.signal)
        if signal is None and self.snapshot and request.signal == QPS_SIGNAL.name:
            signal = QPS_SIGNAL
        if signal is None:
            return []
        # ★★ **窗口内聚合**在两条链路里都走"如实报数"（FLAT），**不判异常**：
        #   它的语义是"这个窗口里出过 N 次 / 峰值多少"，属于**事实**；
        #   判不判异常仍由既有阈值（`min_abs=0.2 次/秒`）决定 —— 用户没有裁决要改阈值。
        if request.signal in _WINDOW_NAMES or self.snapshot:
            # ★ 轻链路问的是"现在多少"，不是"有没有异常" —— 判据完全不同。
            return self._snapshot_evidence(ctx, request, result, signal)
        out: list[Evidence] = []
        for entry in series_of(getattr(result, "data", None)):
            detection = detect_series(entry, signal)
            if detection is None:
                from ..diagnostics.metric_rules import absolute_detection
                detection=absolute_detection(entry.get('points') or [],signal)
            if detection is None:
                continue
            labels = dict(entry.get("metric") or {})
            service = str(labels.get("service") or "")
            entity = ctx.entity(service) or ctx.entity()
            if entity is None:                       # 证据必须挂在实体上
                continue
            out.append(self.make_evidence(
                ctx, signal=signal.name, direction=detection.direction,
                onset=detection.onset, baseline=detection.baseline,
                magnitude=detection.magnitude, magnitude_unit=signal.unit,
                entity=entity,
                confidence=self._confidence(signal, detection),
                raw_ref=f"{getattr(result, 'raw_ref', '') or request.tool}"
                        f" | series={_label_text(labels)}",
                summary=self._summary(entity.name, signal.name, signal.unit,
                                      detection, labels),
                tags=["metrics"] + ([str(labels["target"])] if labels.get("target") else []),
            ))
        return out

    def summarize_empty(self,request,result):
        if request.signal.startswith('native_metric_'):
            return request.signal+'：未检出所设相对变化，不代表故障被排除；类型/单位/采样覆盖见目录'
        return super().summarize_empty(request,result)

    def _snapshot_evidence(self, ctx: AgentContext, request: ToolRequest,
                           result: Any, signal: Any) -> list[Evidence]:
        """★ 把**当前值**原样报出来 —— 不管它有没有异常。

        ★ 与 :meth:`build_evidence` 的区别只有**判据**：那边"检出异常才产出证据"，
          这边"查到 series 就产出"。因为"查现状"问的是"现在是多少"，
          **没异常也是有内容的答案** —— 把它记成"empty（查了没异常）"等于
          让用户拿不到他要的那个数（别把"正常"说成"没查到"）。
        ★★ 只取**有限值**：Prometheus 对没有数据的直方图分位数返回字符串
          `"NaN"`，`float("NaN")` 不抛异常 —— 直接格式化就会把 `nan` 印给用户
          （真环境实测：`http_latency_p95 当前 nan秒`）。这不是"值"，是"这一格没数"。
        """
        out: list[Evidence] = []
        entries = [(dict(entry.get("metric") or {}), _finite_points(entry.get("points") or []))
                   for entry in series_of(getattr(result, "data", None))]
        # ★ 只保留**在多条序列之间取值不同**的标签（它们才是"区分用的"）。
        #   否则摘要会变成一堵墙：`__name__`/`job`/`instance`/`component`/`exported_service`
        #   全塞进去，真正有信息量的 `fault_type` 反而被埋掉（真环境实测）。
        varying = _varying_labels([labels for labels, _points in entries])
        for labels, points in entries:
            if not points:                     # 单点可以报告读数，不能据此拟合趋势
                continue
            service = str(labels.get("service") or "")
            entity = ctx.entity(service) or ctx.entity()
            if entity is None:
                continue
            first, last = points[0][1], points[-1][1]
            # ★★ **窗口内聚合用自己的信号名**（`window_error_count` 等）。
            #   修前这里一律写 `metric_snapshot` → 聚合明明查到了东西，却因为
            #   "这个名下没有证据"被记进 `empty_signals`（"查了但没异常"）——
            #   一句话里自相矛盾（查到了却说没查到）。
            #   ★ 普通快照**仍用 `SNAPSHOT_SIGNAL`**：那是为了避免子串匹配让必要条件凭空满足。
            is_window = signal.name in _WINDOW_NAMES
            out.append(self.make_evidence(
                ctx, signal=(signal.name if is_window else SNAPSHOT_SIGNAL),
                direction=EvidenceDirection.FLAT,
                onset=point_time(points[-1]), baseline=first, magnitude=last,
                magnitude_unit=signal.unit, entity=entity,
                confidence=SNAPSHOT_CONFIDENCE,
                raw_ref=(f"{getattr(result, 'raw_ref', '') or request.tool}"
                         f" | series={_label_text(labels)} | metric={signal.name}"),
                summary=_snapshot_summary(entity.name, signal, points, labels, varying),
                tags=["metrics", "window" if is_window else "snapshot", signal.name]))
        return out

    @staticmethod
    def _confidence(signal: Any, detection: Detection) -> float:
        """置信度只看"证据本身有多硬"，不看"像不像根因"（那是 RCA 的事）。"""
        score = 0.75
        if signal.flag:                       # 故障注入计数非 0 = 直接可见的现场
            score += 0.15
        threshold = signal.threshold(detection.baseline or 0.0)
        if threshold > 0 and detection.magnitude >= threshold * 2:
            score += 0.05
        return min(score, 0.95)

    @staticmethod
    def _summary(service: str, name: str, unit: str, detection: Detection,
                 labels: Mapping[str, Any]) -> str:
        peak = detection.peak if detection.peak is not None else detection.magnitude
        shape = {"up": "持续上升", "down": "持续下降",
                 "spike": "冲高后回落", "drop": "下探后回升",
                 "flat": "基本持平"}.get(detection.direction.value, detection.direction.value)
        extra = f"（target={labels['target']}）" if labels.get("target") else ""
        return (f"{service} 的 {name}{extra} {shape}："
                f"基线 {_num(detection.baseline)}{unit} → 峰值 {_num(peak)}{unit}，"
                f"变化 {_num(detection.magnitude)}{unit}，拐点 {detection.onset.isoformat()}")

    # ------------------------------------------------------------------
    #  QPS（aux 通道）
    # ------------------------------------------------------------------
    def qps_from(self, ctx: AgentContext, request: ToolRequest,
                 result: Any) -> dict[str, float]:
        """把窗口内 1 分钟速率的**均值**当这段时间的 QPS（**只报数，不解释**）。

        ★ 为什么用均值而不是最后一个点：QPS 在这里的用途是"影响面折算"
          （影响了多大流量），而末点可能恰好落在一段静默里 —— 实测出现过
          末点 0.0（刚打完流量、但最后一个采样点早于流量），把"有流量"报成了"没流量"。
        """
        estimates: dict[str, float] = {}
        for entry in series_of(getattr(result, "data", None)):
            labels = dict(entry.get("metric") or {})
            service = str(labels.get("service") or "")
            values: list[float] = []
            for point in entry.get("points") or []:
                try:
                    values.append(float(point[1]))
                except (TypeError, ValueError, IndexError):
                    continue
            if not service or not values:
                continue
            estimates[service] = round(max(sum(values) / len(values), 0.0), 3)
        return estimates


#: 类型别名：只是为了让下面的工具函数读起来清楚
def _service_pattern(services: list[str]) -> str:
    """服务名列表 → PromQL 的 label 正则（**转义元字符**，别让名字里的 `.` 变通配）。"""
    from .query_labels import service_pattern
    return service_pattern(services)


def _label_text(labels: Mapping[str, Any]) -> str:
    return "{" + ",".join(f'{k}="{v}"' for k, v in sorted(labels.items())) + "}"


def _varying_labels(all_labels: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """**只在多条序列之间取值不同**的标签键（它们才是"用来区分序列"的那些）。

            ★ 为什么不能把标签全带上：`fault_injection_active` 会按
      注入类型返回多条序列，而 Prometheus 的每条序列都带着
      `__name__`/`job`/`instance`/`component`/`exported_service` —— 全写进摘要就是
      一堵墙，真正有信息量的 `fault_type` 被埋在中间，五条证据看起来还是"差不多"。
    """
    if len(all_labels) < 2:
        return frozenset()
    keys: set[str] = set()
    for labels in all_labels:
        keys.update(labels)
    return frozenset(key for key in keys
                     if len({str(labels.get(key)) for labels in all_labels}) > 1)


def _extra_labels(labels: Mapping[str, Any], varying: frozenset[str] = frozenset()) -> str:
    """除 `service` 之外、且**确实在区分序列**的标签（没有就返回空串）。"""
    rest = {key: value for key, value in labels.items()
            if key != "service" and key in varying}
    return f"（{_label_text(rest)}）" if rest else ""


def _finite_points(points: Any) -> list[tuple[Any, float]]:
    """`[[ts, value], …]` → 只保留**有限值**的点（`NaN`/`Inf` 不是"值"）。"""
    out: list[tuple[Any, float]] = []
    for point in points or []:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            value = float(point[1])
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            out.append((point[0], value))
    return out


def _num(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:.4g}"
