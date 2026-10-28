"""工具 schema 里**参数说明**的唯一来源（数字全部从 `common/query.py` 现算）。

★★ 为什么单独一个模块：这些说明里有数字（7d / 12.5h / N 条），而数字一变，
   「schema 说的」与「取数端真做的」就会分裂 —— 模型按 schema 行事，于是它会
   **以为自己看到了全部历史**。把文案收在一处、数字只从预算常量取，
   改口径时两边一起动（`sources.py` 的 `note` 用的是同一批常量）。

★ 一句话讲作用：给三个查询工具生成带**取数局限**的参数说明与结果留痕文本。
"""

from __future__ import annotations

from typing import Any, Final, Mapping

from ..common.query import (
    BASE_STEP_SECONDS,
    MAX_DETAIL_SECONDS,
    MAX_LOOKBACK_SECONDS,
    MAX_QUERY_POINTS,
)

__all__ = ["TIME_LIMITS", "RANGE_DESCRIPTION", "RAW_READ_SCHEMA",
           "readable", "limit_description", "describe_limits"]

_UNIT_SECONDS: Final[dict[str, int]] = {"d": 86400, "h": 3600, "m": 60}


def readable(seconds: float) -> str:
    """时长的可读写法（`12.5h` / `7d`）—— 与 `common/query.py` 同一口径，只用于文案。"""
    total = max(0.0, float(seconds))
    for unit, size in _UNIT_SECONDS.items():
        if total >= size:
            return f"{total / size:.1f}{unit}".replace(".0", "")
    return f"{total:.0f}s"


#: 「窗口 + 明细」这条局限的**完整说法**（schema 说明与结果 `note` / `raw_ref` 共用同一句）。
#: ★ 它同时是"机器可查"的那份文本：三处都能 grep 到同一串。
TIME_LIMITS: Final[str] = (
    f"★ 取数局限：窗口最长 {readable(MAX_LOOKBACK_SECONDS)}（超出直接拒）；"
    f"明细只召回最近 {readable(MAX_DETAIL_SECONDS)}"
    f"（{MAX_QUERY_POINTS} 点 × {BASE_STEP_SECONDS}s，step 恒 {BASE_STEP_SECONDS}s 不放大）；"
    f"更久远的只在窗口内**聚合**（累计次数 / 峰值）里体现，不召回明细"
)

#: 三个查询工具 `start` / `end` / `window` 的说明（"窗口 ≤ 7d"这一条对三者相同）。
RANGE_DESCRIPTION: Final[str] = (
    f"相对当前的时间窗（如 30m / 2h / 1d / {readable(MAX_LOOKBACK_SECONDS)}）。" + TIME_LIMITS
)

#: `row_count` 认得的"这是第几条"的键名（不同数据源的条数藏在不同字段里）。
_ROW_KEYS: Final[tuple[str, ...]] = ("entries", "points", "traces", "rows", "items")


def limit_description(what: str, *, default: int, maximum: int) -> str:
    """`limit` 参数说明 / 工具 `description` 里的**条数局限**（与结果的 `note` 同一句）。

    ★ `what` 用"带量词的说法"（如 `行日志` / `条 trace`）—— 结果 `note` 里的截断
      留痕用的是同一个词，两处不许各说一套。
    """
    return (f"★ 取数局限：最多只取**最近的** {default} {what}"
            f"（上限 {maximum}）；被它截断时结果 `note` 会写明"
            f"「本次只取回最近 {default} {what}，窗口内可能还有更早的{what}」")


def row_count(data: Any) -> int:
    """这份结果里有多少"条/点"（认不出的结构按 1 条算，**不猜**）。"""
    if not isinstance(data, (list, tuple)):
        return 1
    total = 0
    for item in data:
        if not isinstance(item, Mapping):
            total += 1
            continue
        found = 0
        for key in _ROW_KEYS:
            value = item.get(key)
            if isinstance(value, (list, tuple)):
                found = len(value)
                break
        total += found or 1
    return total


def describe_limits(data: Any, *, limit: int, unit: str, source: str = "") -> str:
    """结果的**取数局限一句话**：永远写清"取回多少"，顶到上限时再点明"可能还有更早的"。

    ★ 判据只看**条数是否顶到 `limit`** —— 顶到了说明数据源那边**还有更多的可能**
      （宁可多说一句，也不许让模型以为这是全部）；没顶到也照样交代上限是多少，
      免得它下一条就忘了这件事。
    """
    cap = max(1, int(limit))
    tail = f"，{source}" if source else ""
    if row_count(data) >= cap:
        return (f"★ 取数局限：本次只取回最近 {cap} {unit}{tail}，"
                f"窗口内可能还有更早的{unit}（要更早的把 start 往前挪并缩小窗口）")
    return f"★ 取数局限：本次取回 {row_count(data)} {unit}{tail}（上限 {cap}）"


#: `raw_read` 的入参 schema（只读本地副本；**不给任何"重发上游请求"的开关**）。
RAW_READ_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "raw_ref": {
            "type": "string", "minLength": 1,
            "description": "要回取的那份原始数据的引用 —— 用查询类工具结果里的 "
                           "`raw_ref`（形如 `promql: <query> | step=30s | [t0,t1]`）。"
                           "★ 只读**本地副本**（当时抓下来落盘的那一份），"
                           "**不会**重新请求上游",
        },
        "max_chars": {
            "type": "integer", "default": 20000, "minimum": 1, "maximum": 200000,
            "description": "最多返回多少字符（默认 20000、上限 200000）；"
                           "被它截断时结果 `note` 会写明砍掉了多少",
        },
    },
    "required": ["raw_ref"],
    "additionalProperties": False,
}
