"""工具入参的**语义校验**（第 3 道保护）。

--------------------------------------------------------------------------------
★ 第 1 道保护（Schema）管的是"形状"，这里管的是"后果"
--------------------------------------------------------------------------------

Schema 只能拦住"没给 query"或"limit 不是整数"。但下面这些都是**形状合法、后果严重**：

| 写法 | 形状 | 后果 |
|---|---|---|
| `rate(http_requests_total[5m])` | ✅ 合法 PromQL | **全量扫描**：几百万序列，把 Prometheus 打慢、把上下文塞爆 |
| `{job=~".+"}` | ✅ 合法选择器 | 同上（等价于"我要所有东西"） |
| `window="30d"` | ✅ 合法字符串 | 超出保留期 + 返回量爆炸（长度上限 7d） |
| `limit=100000` | ✅ 合法整数 | 一次工具调用就能撑爆上下文（摘要化的硬上限 50 点） |

所以这一层做两件事：
1. **拒绝**会造成全量扫描/超窗口/超上限的调用 → `TOOL_SCOPE_TOO_BROAD`；
2. **把时间窗归一**成 `(start_epoch, end_epoch)`，让各工具的实现不用各自解析
   —— 也顺便保证"默认窗口"只有**一处**（schema 里的 `default`）。

★ 错误码为什么用 `TOOL_SCOPE_TOO_BROAD` 而不是 `TOOL_INVALID_PARAMS`：
  前者对应"**自动缩小时间窗/加 label** 后重试"（Agent 自己能修），
  后者是"让模型改参数"。这里的修法是**确定的**（加上 label、缩短窗口），
  不需要问模型 —— 分错类会让重试策略走错分支。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Mapping, Optional

from ..common.query import MAX_LOOKBACK_SECONDS, duration_seconds
from ..common.enums import ErrorCode

__all__ = [
    "SemanticDecision",
    "check_semantics",
    "parse_duration",
    "normalize_time_range",
    "MAX_QUERY_WINDOW_SEC",
]

#: ★★ 时间窗上限从 **24h → 7d**（与 `common/query.py` 的取数预算**同一处来源**）。
#:   为什么必须一起改（实测）：默认窗改成 7 天后，凡是把窗口传给工具的地方都会
#:   触发 `TOOL_SCOPE_TOO_BROAD` → **整条取数被拒**（"pool_waiting：调用失败
#:   （TOOL_SCOPE_TOO_BROAD）"），指标那一路直接消失 —— 比"查不到"更糟：
#:   会被读成"这一路没信号"。
MAX_QUERY_WINDOW_SEC: Final[int] = MAX_LOOKBACK_SECONDS

# ★ 这里**没有**"各工具的返回上限表"：上限的**唯一来源**是各工具 schema 里
#   `limit` 的 `maximum`（第 1 道保护已经在用它）。修前另有一张 `MAX_LIMIT`
#   与 schema 双写，语义还不同（一个"拒"、一个"schema 拦住"），必须同时改两处 ——
#   漏一处的症状是"某个工具的上限看起来管着、其实没管"。

#: 只选中 `__name__` 或"全匹配"的选择器 = 伪装的裸查询
_WIDE_MATCHERS: Final[tuple[str, ...]] = ('=~".+"', '=~".*"', '=~".{0,}"', '!=""')
_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)")


@dataclass(frozen=True)
class SemanticDecision:
    """语义校验的结论：**归一后的参数** + 可选的拒绝原因。"""

    params: dict[str, Any]
    error_code: Optional[str] = None
    message: str = ""
    hint: str = ""                     # 给模型/调用方的修正建议（必须带明确信息）

    @property
    def ok(self) -> bool:
        return self.error_code is None


def parse_duration(raw: Any) -> Optional[int]:
    """把 `30m` / `2h` / `300` 解析成秒。拿到不到返回 `None`。

    ★★ 时长的**解析只有一处**：`common/query.duration_seconds`（它认 `30m` / `7d`，
      忽略大小写、允许 `30 m`）。这里**不重写解析**，只做两件它不做的事：

      · 接受数字/纯数字串（`300` = 300 秒）—— 调用方（CLI / 槽位）会这么给；
      · 把"认不出来"统一表达成 `None`（不抛、不猜）。
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw) if raw > 0 else None
    text = str(raw or "").strip()
    if not text:
        return None
    # ① 与全仓同一口径的写法（含大小写与空格）交给 `common`
    common = duration_seconds(text)
    if common is not None:
        return common if common > 0 else None
    # ② `common` 不认的：纯数字（"300" = 300 秒）
    if text.isdigit():
        return int(text) if int(text) > 0 else None
    return None


def _parse_timestamp(raw: Any) -> Optional[float]:
    """接受 epoch 秒（数字或数字串）与 ISO8601。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _default_of(schema: Mapping[str, Any], key: str) -> Any:
    """从 schema 里取参数的默认值（**默认值只有这一处**）。"""
    props = schema.get("properties") or {}
    node = props.get(key) or {}
    return node.get("default")


def normalize_time_range(
    params: Mapping[str, Any], schema: Mapping[str, Any], *, now: Optional[float] = None
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """把 `start`/`end`/`window` 归一成 epoch 秒。

    Returns:
        `(归一结果, 错误信息)`；错误信息非空表示拒绝（时间窗 > 长度上限 7d 或格式不可解析）。
        没有时间参数的工具返回 `(None, None)`。
    """
    if "window" not in (schema.get("properties") or {}) and not (
        {"start", "end"} & set(schema.get("properties") or {})
    ):
        return None, None

    now_ts = float(now if now is not None else time.time())
    start = _parse_timestamp(params.get("start"))
    end = _parse_timestamp(params.get("end"))
    window_raw = params.get("window") or _default_of(schema, "window")
    window_sec = parse_duration(window_raw)

    if start is not None or end is not None:
        if start is None or end is None:
            return None, "start 与 end 必须**成对**给（否则无法判断查哪一段）"
        if end <= start:
            return None, f"end（{end:.0f}）必须晚于 start（{start:.0f}）"
    else:
        if window_sec is None:
            return None, f"时间窗 {window_raw!r} 不合法（应为 30m / 2h / 1d 这类写法）"
        end, start = now_ts, now_ts - window_sec

    if end - start > MAX_QUERY_WINDOW_SEC:
        hours = (end - start) / 3600
        return None, (
            f"时间窗 {hours:.1f}h 超过上限 {MAX_QUERY_WINDOW_SEC // 3600}h"
            f"（防全量扫描）"
        )
    return {"start": start, "end": end}, None


def _selector_has_label_filter(query: str) -> tuple[bool, str]:
    """`{...}` 里是否有**真的** label 过滤（只写 `__name__` 或全匹配不算）。"""
    selectors = re.findall(r"\{([^}]*)\}", query)
    if not selectors:
        return False, "查询里没有 `{...}` 选择器"
    for body in selectors:
        matchers = _LABEL_RE.findall(body)
        if not matchers:
            continue
        meaningful = [
            (name, op) for name, op in matchers
            if name != "__name__" and not any(wide in body for wide in _WIDE_MATCHERS)
        ]
        if meaningful:
            return True, ""
    if any(wide in query for wide in _WIDE_MATCHERS):
        return False, '选择器等价于"全都要"（如 `=~".+"`）'
    return False, "选择器里只过滤了 `__name__`（等价于按指标名全量扫）"


def _has_selector_query(props: Mapping[str, Any]) -> bool:
    """这个工具的入参是不是"**选择器形态**的遥测查询" —— 决定要不要查 label 过滤。

    ★★ 判据取自 **schema 的能力**，不取自工具名、也不取自"查询里有没有 `{`"：
      · 按名字硬编码（修前写死三个内置工具名）会让扩展工具**完全落在这道保护之外**；
      · 按"查询里有没有 `{`"会把**最该拦的那一种**放过去 ——
        `rate(http_requests_total[5m])` 恰恰**没有**选择器，它就是要被拦的"没有 label 过滤"。
    所以口径是：**声明了 `query` 且声明了时间范围**（`window` 或 `start`/`end` 成对）
    的是遥测查询工具，必须有 label 过滤；只声明 `query` 的是自然语言检索（如知识库），不受此约束。
    """
    if "query" not in props:
        return False
    return "window" in props or {"start", "end"} <= set(props)


def _selector_cap(schema: Mapping[str, Any]) -> Optional[int]:
    """返回条数的上限 —— **从 schema 的 `maximum` 读**（第 1 道保护用的就是它）。

    ★★ 上限**只有一个来源**：schema。修前第 3 道保护另有一张 `MAX_LIMIT` 表与它双写，
      语义还不同（一个"拒"、一个"schema 拦住"），必须同时改两处。现在两处合一处：
      `maximum` 写多少，第 1 道就先拦；第 3 道再用同一个数兜住"绕过 schema 的内部调用"。
    """
    props = schema.get("properties") or {}
    for key in ("limit", "top_k"):
        node = props.get(key) or {}
        cap = node.get("maximum")
        if isinstance(cap, int) and not isinstance(cap, bool):
            return cap
    return None


def check_semantics(
    tool: str,
    params: Mapping[str, Any],
    schema: Mapping[str, Any],
    *,
    now: Optional[float] = None,
) -> SemanticDecision:
    """第 3 道保护的实现：语义校验 + 时间窗归一。"""
    normalized: dict[str, Any] = dict(params)
    props = schema.get("properties") or {}

    # ---- 查询类工具：必须有足够的 label 过滤 ----
    # ★ 只对**选择器形态**的遥测查询生效（判据见 `_has_selector_query` 的 docstring）：
    #   像 `kb_search` 那样把自然语言放进 `query` 的工具不该被要求写 `{service="order"}`。
    if _has_selector_query(props):
        query = str(params.get("query") or "")
        ok, why = _selector_has_label_filter(query)
        if not ok:
            return SemanticDecision(
                normalized, error_code=ErrorCode.TOOL_SCOPE_TOO_BROAD.value,
                message=f"{tool} 的查询缺少必要的 label 过滤：{why}",
                hint=("请加上具体标签（如 service=\"order\"）后重试；"
                      "不允许不带过滤条件地全量扫描"),
            )

    # ---- 变更查询：必须限定实体或变更单号（防全表扫）----
    if tool == "change_read":
        if not any(str(params.get(key) or "").strip()
                   for key in ("service", "change_id")):
            return SemanticDecision(
                normalized, error_code=ErrorCode.TOOL_SCOPE_TOO_BROAD.value,
                message="change_read 至少要给 service 或 change_id 之一",
                hint="按服务或变更单号缩小范围后重试（禁止全表扫描）",
            )

    # ---- 时间窗：≤ 长度上限（7d），且归一成 epoch ----
    time_range, why = normalize_time_range(params, schema, now=now)
    if why:
        return SemanticDecision(
            normalized, error_code=ErrorCode.TOOL_SCOPE_TOO_BROAD.value, message=why,
            hint=f"把时间窗缩到 7d 以内（默认 {_default_of(schema, 'window')!r}）",
        )
    if time_range:
        normalized.update(time_range)
        # 既然已经换算成 start/end，就把人类写法去掉，避免下游出现两份"时间"
        normalized.pop("window", None)

    # ---- 返回上限（**按 schema 声明的上限**，对扩展工具同样生效）----
    key = "limit" if "limit" in props else "top_k"
    cap = _selector_cap(schema)
    limit = params.get(key)
    if limit is None and key in props:
        limit = _default_of(schema, key)
        if limit is not None:
            normalized[key] = limit
    if cap is not None and limit is not None:
        try:
            limit_int = int(limit)
        except (TypeError, ValueError):
            return SemanticDecision(
                normalized, error_code=ErrorCode.TOOL_INVALID_PARAMS.value,
                message=f"{key}={limit!r} 不是整数",
                hint=f"{key} 必须是整数",
            )
        if limit_int > cap:
            return SemanticDecision(
                normalized, error_code=ErrorCode.TOOL_SCOPE_TOO_BROAD.value,
                message=f"{key}={limit_int} 超过 {tool} 的上限 {cap}",
                hint=f"把 {key} 降到 {cap} 以内（结果会在摘要化时进一步裁剪到 50 点）",
            )
        normalized[key] = limit_int

    return SemanticDecision(normalized)
