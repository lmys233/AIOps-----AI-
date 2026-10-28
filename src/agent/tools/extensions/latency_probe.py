"""示例扩展：`latency_probe` —— 读某服务最近一段时间的 99 分位延迟。

★ 它存在的意义有两个：
  1. **证明动态注册这条路是通的**（一个文件 + 清单一行 → 工具就能被模型看到并调通）；
  2. 作为一个**教学样本**：一个扩展工具"应该长什么样"（能力声明、裁剪器、限流、
     `raw_ref` 可回查、prompt 里带上可选服务清单）。

★ 权限上它很克制：只要 `metrics` 能力（不碰原始遥测之外的任何东西），
  只申请给 `metrics_agent` —— 与内置的 `promql_read` 同一条证据线路。
"""

from __future__ import annotations

from typing import Any, Final, Mapping

from ..definition import define_tool
from ..policy import CAP_METRICS
from ..sources import DEFAULT_SERIES_LIMIT, RawOutcome, SourceUnavailable

#: 常见服务（让模型知道该填什么；实际取值仍由调用方决定）
_SERVICES: tuple[str, ...] = ("gateway", "order", "payment", "inventory")

#: `limit` 的上限 —— **数值与 schema 的 `maximum` 同源**（改一处必须改另一处）
MAX_SERIES: Final[int] = 200


@define_tool(
    name="latency_probe",
    description=(
        "读取某个服务最近一段时间的 99 分位延迟曲线（比直接写 PromQL 更省事）。"
        "只接受最近 7 天以内的时间窗；返回统计量、峰值点与趋势。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "service": {"type": "string", "minLength": 1,
                        "enum": list(_SERVICES),
                        "description": "服务名（只允许被观测系统里已知的服务）"},
            "window": {"type": "string", "default": "15m",
                       "description": "时间窗（如 5m / 15m / 2h / 7d），最长 7d"},
            "limit": {"type": "integer", "default": DEFAULT_SERIES_LIMIT,
                      "minimum": 1, "maximum": MAX_SERIES,
                      "description": "最多返回多少条 99 分位**序列**（超出部分会被裁掉，并留痕）"},
        },
        "required": ["service"],
        "additionalProperties": False,
    },
    capabilities=(CAP_METRICS,),
    risk_level="L0",
    granted_roles=("metrics_agent",),
    version=1,
    summarizer="summarize_series",
    timeout_sec=15,
    rate_limit="30/min",
    cost_weight=0.9,
)
async def latency_probe(ctx: Any, params: Mapping[str, Any]) -> RawOutcome:
    """实现体：查 Prometheus 的 99 分位直方图。

    ★ 注意两件事（所有扩展都该这么做）：
      · 依赖缺失 → 抛 `SourceUnavailable`（"没查成"，**不是**返回空数据）；
      · 返回空列表是**合法结果**（"查了，这段时间没有数据"），由裁剪器如实表达。

    ★★ `limit` 必须**真的传给数据源**：上限只在取数端执行一次，
      这里不再自己偷偷 `series[:limit]` —— 那样会出现
      "`raw_ref` 写着 `limit=200`、实际只回 100 条"的**证据与数据不一致**；
      真被裁了，由数据源写进 `raw_ref`/`note` 的说明如实带出来（不静默）。
    """
    if ctx.prometheus is None:
        raise SourceUnavailable("NotConfigured", "Prometheus 未接入（latency_probe 需要它）")

    service = str(params["service"])
    limit = int(params.get("limit") or DEFAULT_SERIES_LIMIT)
    query = (
        "histogram_quantile(0.99, sum by (le) ("
        f'rate(http_request_duration_seconds_bucket{{service="{service}"}}[5m])))'
    )
    outcome = await ctx.prometheus.query_range(
        query, start=float(params["start"]), end=float(params["end"]),
        step=str(params.get("step") or "30s"), limit=limit,
    )
    kept = len(outcome.data) if isinstance(outcome.data, list) else 0
    return RawOutcome(
        data=outcome.data,
        # ★ 直接沿用取数端给的 `raw_ref`（它已写明**真实生效**的 limit 与任何裁剪），
        #   再补上"这次查的是哪个服务" —— 不另拼一份可能与数据不符的引用。
        raw_ref=f"{outcome.raw_ref} | 99 分位 | service={service}",
        note=f"{kept} 条 99 分位序列",
    )
