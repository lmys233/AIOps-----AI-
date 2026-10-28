"""错误**指纹**：把"同一个错误"合并成一条，同时保住次数与首现时刻。

--------------------------------------------------------------------------------
★ 为什么必须有指纹，而不是"按 (服务, 错误类型) 聚合"
--------------------------------------------------------------------------------

`(service, error_type)` 太粗：同一个服务的 `POOL_ACQUIRE_TIMEOUT` 可能来自
三条完全不同的语句（连接池排队 / 慢查询占住连接 / 建连失败），合成一条就再也
分不出**是哪一个位置**在报错。而"哪一行日志、哪一条语句"恰恰是排障要落到的地方。

指纹 = `sha256(服务 | 操作 | 错误类型 | 归一化后的细节)[:16]`：

  · **带上服务维度** —— 只 hash 文本会让两个服务里逐字相同的报错并成一条，
    归因主语直接被抹掉（"inventory 表不存在"与"payment 表不存在"不是一回事）；
  · **归一化掉会变的量** —— 时间戳 / trace_id / span_id / 毫秒数 / 长十六进制串；
  · **绝不把 `trace_id` 放进指纹** —— 它每次请求都不同，进去就等于"永不合并"，
    `count` 恒为 1、首现恒等于末次，爆发判据全废。

--------------------------------------------------------------------------------
★★ 合并之后必须留下的四个字段
--------------------------------------------------------------------------------

| 字段 | 谁在用 |
|---|---|
| `count` | 爆发判据（多少行 / 多少次） |
| `first_seen` / `last_seen` | 拐点与传播方向的时序依据 |
| `sample_trace_id` | 回取原始 trace 的**唯一钥匙** |
| `sample_text` | 给人看的那一行原文 |

少一个都不是"合并"，而是**丢事实**：少了 trace_id 就没法下钻，
少了 first_seen 就判不了"谁先开始报错"。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Final, Mapping, Sequence

__all__ = [
    "FINGERPRINT_CHARS",
    "FINGERPRINT_TAG_PREFIX",
    "normalize_detail",
    "error_fingerprint",
    "CondensedError",
    "condense",
    "condense_clusters",
]

#: 指纹在证据 `tags` 里的前缀（与 `trace_id=` 同一手法：**结构化字段**带上，
#: 下游按前缀读，不去 parse 渲染过的摘要）。
FINGERPRINT_TAG_PREFIX: Final[str] = "fingerprint="

#: 指纹取 sha256 的前多少位（16 位十六进制 = 64 bit，碰撞概率在本项目规模下可忽略，
#: 而它要进提示词与证据标签，太长会白占预算）。
FINGERPRINT_CHARS: Final[int] = 16

#: 归一化时统一替换成的占位符（**常量**：同一形态永远替换成同一个词，
#: 否则归一化本身又成了一个随机的量）。
_PLACEHOLDER: Final[dict[str, str]] = {
    "ts": "<TS>",
    "id": "<ID>",
    "num": "<N>",
    "ms": "<MS>",
}

#: ISO8601 时刻（2026-10-04T03:07:51 / 带毫秒 / 带 Z 或偏移）。
_ISO_TIME: Final[re.Pattern[str]] = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?")

#: `trace_id=…` / `span_id=…` 这类键值对里的长标识（**整个键值一起抹掉**，
#: 只抹值会留下一串无意义的 `trace_id=`）。
_ID_KV: Final[re.Pattern[str]] = re.compile(
    r"\b(trace[_-]?id|span[_-]?id|request[_-]?id|req[_-]?id|x-request-id)\b"
    r"\s*[=:]\s*[\"']?[0-9a-zA-Z._\-]{6,}[\"']?", re.IGNORECASE)

#: 长十六进制串（≥16 位）—— trace_id / span_id 的裸形态。
_HEX_ID: Final[re.Pattern[str]] = re.compile(r"\b[0-9a-fA-F]{16,}\b")

#: 毫秒 / 微秒级耗时（`1200ms` / `1200.5 ms`）—— 每次请求都在变。
_DURATION: Final[re.Pattern[str]] = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|毫秒)\b",
                                               re.IGNORECASE)

#: 裸数字（端口号、PID、计数…）—— 只保形不保值。
_NUMBER: Final[re.Pattern[str]] = re.compile(r"\d+(?:\.\d+)?")


def normalize_detail(text: str) -> str:
    """把一段错误细节归一化：折叠空白 + 抹掉**每次都会变**的量。

    ★ 顺序有意：先抹"带名字的标识"（`trace_id=abc…`）再抹"裸长十六进制"，
      否则 `trace_id=3f8d…` 会先被裸串规则吃掉一半，留下一个残缺的 `trace_id=`。

    ★ 数字统一换成 `<N>` 而不是删掉：删掉会让
      「连接池超时 3 次」与「连接池超时」并成一条（那是**不同的事实**），
      换成占位符则保住了"这里有一个数"这个形状。
    """
    out = re.sub(r"\s+", " ", str(text or "")).strip()
    out = _ISO_TIME.sub(_PLACEHOLDER["ts"], out)
    out = _ID_KV.sub(lambda m: f"{m.group(1)}=<ID>", out)
    out = _DURATION.sub(_PLACEHOLDER["ms"], out)
    out = _HEX_ID.sub(_PLACEHOLDER["id"], out)
    out = _NUMBER.sub(_PLACEHOLDER["num"], out)
    return out


def error_fingerprint(service: str, operation: str, error_type: str,
                      detail: str = "") -> str:
    """一条错误的指纹：`sha256(服务 | 操作 | 错误类型 | 归一化细节)[:16]`。

    ★ 四个维度**一个都不能少**，尤其不能只剩文本：
      服务是"谁在报错"、操作是"哪个位置"、错误类型是"什么错"、
      细节是"具体到哪一行"。只 hash 文本会把不同服务的同文本错误并成一条。
    """
    payload = "|".join((str(service or "").strip(),
                        str(operation or "").strip(),
                        str(error_type or "").strip(),
                        normalize_detail(detail)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]


class CondensedError(dict):
    """一条**合并后**的错误（dict 子类：可直接进证据标签与响应体）。

    | 键 | 含义 |
    |---|---|
    | `fingerprint` | 指纹（合并的键） |
    | `service` / `operation` / `error_type` | 三个维度事实 |
    | `count` | **合并后**的总次数（爆发判据的一半） |
    | `first_seen` / `last_seen` | 首现与末次（**不合并就等于没有拐点**） |
    | `sample_trace_id` | 回取原始 trace 的钥匙 |
    | `sample_text` | 归一化**之前**的原文样本（给人看） |
    | `propagated` | 这条是不是"替下游报错" |
    """

    def to_dict(self) -> dict[str, Any]:
        return dict(self)


def _order_key(value: Any) -> tuple[int, float]:
    """时刻排序键：有时刻的按时刻升序在前，没时刻的排最后（**不编**一个时刻）。"""
    if value is None:
        return (1, 0.0)
    if hasattr(value, "timestamp"):
        return (0, float(value.timestamp()))
    try:
        return (0, float(value))
    except (TypeError, ValueError):
        return (1, 0.0)


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


def condense(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """把逐条记录按**指纹**合并（返回按首现升序、指纹字典序排好的列表）。

    每条记录至少要有 `service`；`operation` / `error_type` / `detail` 缺了就按空串
    参与指纹（**不编**一个操作名出来）。

    ★ 合并规则（每一条都是"少一个就丢事实"）：
      · `count`：**累加**；
      · `first_seen` / `last_seen`：取**极值**（不是"最后一个覆盖"）；
      · `sample_trace_id`：取**第一条非空**的（稳定、可复现，不会每次跑都不一样）；
      · `sample_text`：取**首次出现**那条的原文（与 first_seen 对齐）。
    """
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in records or ():
        item = _record_of(record)
        if item is None:
            continue
        key = item["fingerprint"]
        known = merged.get(key)
        if known is None:
            merged[key] = item
            order.append(key)
            continue
        _absorb(known, item)
    return [merged[key] for key in sorted(order, key=lambda k: _sort_key(merged[k]))]


def _record_of(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """一条原始记录 → 合并用的结构（拿不到服务/错误类型就跳过，**不编**）。"""
    service = str(record.get("service") or "").strip()
    error_type = str(record.get("error_type") or "").strip()
    if not service or not error_type:
        return None
    operation = str(record.get("operation") or "").strip()
    detail = str(record.get("detail") or record.get("sample") or "")
    onset = record.get("onset")
    trace_id = str(record.get("trace_id") or record.get("sample_trace_id") or "").strip()
    return {
        "fingerprint": error_fingerprint(service, operation, error_type, detail),
        "service": service,
        "operation": operation,
        "error_type": error_type,
        "count": max(1, int(record.get("count") or 1)),
        "first_seen": onset,
        "first_seen_iso": _iso(onset),
        # ★ `last_seen` 优先取记录自己给的末次时刻（日志聚类就有它）；
        #   只给首现的输入（逐条证据）退回首现 —— **不编**一个区间。
        "last_seen": record.get("last_seen", record.get("last", onset)),
        "last_seen_iso": _iso(record.get("last_seen", record.get("last", onset))),
        "sample_trace_id": trace_id,
        "sample_text": detail,
        "propagated": bool(record.get("propagated")),
        "signal": str(record.get("signal") or ""),
    }


def _absorb(known: dict[str, Any], item: Mapping[str, Any]) -> None:
    """把一条同指纹的记录并进已有那条（**只动该动的那几个字段**）。"""
    known["count"] = int(known["count"]) + int(item["count"])
    if _order_key(item["first_seen"]) < _order_key(known["first_seen"]):
        known["first_seen"] = item["first_seen"]
        known["first_seen_iso"] = item["first_seen_iso"]
        known["sample_text"] = item["sample_text"]
    if _order_key(item["last_seen"]) > _order_key(known["last_seen"]):
        known["last_seen"] = item["last_seen"]
        known["last_seen_iso"] = item["last_seen_iso"]
    if not known.get("sample_trace_id") and item.get("sample_trace_id"):
        known["sample_trace_id"] = item["sample_trace_id"]


def _sort_key(item: Mapping[str, Any]) -> tuple[tuple[int, float], str]:
    return (_order_key(item.get("first_seen")), str(item.get("fingerprint") or ""))


def condense_clusters(clusters: Sequence[Mapping[str, Any]], *,
                      service: str = "") -> list[dict[str, Any]]:
    """日志聚类（`logql_read` 的 `clusters`）→ 按指纹合并的错误列表。

    ★ 聚类的 `count` 是**行数**、`first_ts_ns` / `last_ts_ns` 是**首末时刻** ——
      三者都要用上；只取行数会让 `last_seen` 变成"首现"，传播方向就判不出来了。

    ★ 样本原文用 `samples[0].msg`（判"是不是替下游报错"只能看它，
      不看我们渲染过的摘要 —— 摘要里有我们自己写进去的 `error_type=`）。
    """
    from ..attribution.errors import propagated as _propagated

    records: list[dict[str, Any]] = []
    for cluster in clusters or ():
        if not isinstance(cluster, Mapping):
            continue
        name = str(cluster.get("service") or service or "").strip()
        declared = str(cluster.get("error_type") or "").strip()
        sample = _first_sample(cluster)
        error_type = declared or _first_embedded(sample)
        if not name or not error_type:
            continue
        records.append({
            "service": name,
            "operation": str(cluster.get("operation") or cluster.get("level") or ""),
            "error_type": error_type,
            "detail": sample,
            "count": int(cluster.get("count") or 0) or 1,
            "onset": _ns_to_dt(cluster.get("first_ts_ns")),
            "last": _ns_to_dt(cluster.get("last_ts_ns")),
            "trace_id": str(cluster.get("trace_id") or _sample_trace_id(cluster)),
            "propagated": _propagated(declared, sample),
        })
    merged = condense(records)
    for item, record in zip(merged, records):
        item["sample_text"] = record["detail"]
    return _apply_last(merged, records)


def _apply_last(merged: list[dict[str, Any]],
                records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """把每个指纹的 `last_seen` 补成**该指纹下最大的那个末次时刻**。

    ★ 为什么不在 `condense` 里做：`condense` 的输入契约只有 `onset`
      （逐条证据只有首现）。聚类额外带 `last_ts_ns`，是这一路专有的信息。
    """
    wanted: dict[str, Any] = {}
    for record in records:
        key = error_fingerprint(str(record.get("service") or ""),
                                str(record.get("operation") or ""),
                                str(record.get("error_type") or ""),
                                str(record.get("detail") or ""))
        latest = record.get("last")
        if latest is None:
            continue
        if key not in wanted or _order_key(latest) > _order_key(wanted[key]):
            wanted[key] = latest
    for item in merged:
        latest = wanted.get(str(item.get("fingerprint") or ""))
        if latest is not None:
            item["last_seen"] = latest
            item["last_seen_iso"] = _iso(latest)
    return merged


def _first_sample(cluster: Mapping[str, Any]) -> str:
    samples = cluster.get("samples") or []
    if samples and isinstance(samples[0], Mapping):
        return str(samples[0].get("msg") or "")
    return ""


def _sample_trace_id(cluster: Mapping[str, Any]) -> str:
    for sample in cluster.get("samples") or []:
        if isinstance(sample, Mapping) and str(sample.get("trace_id") or "").strip():
            return str(sample["trace_id"]).strip()
    return ""


def _first_embedded(text: str) -> str:
    from ..attribution.errors import embedded_error_types

    found = embedded_error_types(text)
    return found[0] if found else ""


def _ns_to_dt(value: Any) -> Any:
    """纳秒时间戳 → aware datetime（拿不到就 `None` —— **不编**一个时刻）。"""
    if value in (None, ""):
        return None
    try:
        from datetime import datetime, timezone

        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return datetime.fromtimestamp(number / 1e9, tz=timezone.utc)
