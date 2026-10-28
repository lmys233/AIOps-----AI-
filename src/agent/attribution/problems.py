"""全部已观测问题的清单；与单个根因候选分开，不调用模型。"""

from __future__ import annotations

from typing import Any, Sequence

from .errors import error_groups

# 错误类型的语义，与部署服务名无关。未知错误保留其真实类型及样本。
_TITLES = {
    "THREAD_POOL_REJECTED": "工作线程池已满，任务被拒绝",
    "DB_DEADLOCK": "数据库事务发生死锁",
    "POOL_QUERY_FAILED": "数据库查询失败",
    "POOL_ACQUIRE_TIMEOUT": "等待数据库连接超时",
}


def problem_inventory(evidence: Sequence[Any]) -> list[dict[str, Any]]:
    """按服务汇总自身错误；传播证据不算自身故障，重复取证不累加次数。"""
    grouped: dict[str, dict[str, Any]] = {}
    for item in evidence or ():
        for record in error_groups([item]):
            name, error = record["service"], record["error_type"]
            if not name or record["propagated"]:
                continue
            entry = grouped.setdefault(name, {})
            observation = entry.setdefault(error, {
                "error_type": error, "sample": record["sample"],
                "sample_count": 0, "first_seen": record["onset_iso"],
                "evidence_ids": [], "raw_refs": [], "time_ranges": [],
            })
            observation["sample_count"] = max(observation["sample_count"], record["count"])
            for key, value in (
                ("evidence_ids", getattr(item, "evidence_id", "")),
                ("raw_refs", getattr(item, "raw_ref", "")),
                ("time_ranges", [stamp.isoformat() for stamp in
                                 (getattr(item, "time_range", ()) or ())]),
            ):
                if value and value not in observation[key]:
                    observation[key].append(value)
    return [_problem(name, grouped[name]) for name in sorted(grouped)]


def _problem(name: str, errors: dict[str, Any]) -> dict[str, Any]:
    types = sorted(errors, key=lambda error: (
        list(_TITLES).index(error) if error in _TITLES else len(_TITLES), error))
    return {
        "problem_id": f"service:{name}", "service": name,
        "title": _TITLES.get(types[0], types[0]), "error_types": types,
        "observations": [errors[error] for error in types],
        "evidence_ids": list(dict.fromkeys(
            eid for error in types for eid in errors[error]["evidence_ids"])),
    }


def problem_rows(problems: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
    """完整问题清单的用户可见明细；保留时间与证据索引。"""
    return [{"label": f"问题 {index} · {item['service']} · {item['title']}",
             "value": "；".join(
                 f"{row['error_type']}：{row['sample']}"
                 f"（采样最多 {row['sample_count']} 条，样本首现 {row['first_seen']}）"
                 for row in item["observations"])
                 + "；证据：" + "、".join(item["evidence_ids"])}
            for index, item in enumerate(problems, 1)]


def problem_prompt_rows(summary: dict[str, Any]) -> list[str]:
    """把完整清单送进总结提示词，明确采样与根因的边界。"""
    problems = summary.get("problems") or []
    if not problems:
        return []
    return ["- 已观测问题清单（必须逐项回答，不可只保留一个主根因；这是窗口内采样，"
            "不能断言全部故障此刻仍在发生）：", *[
                f"  - {item['service']}：{item['title']}；" + "；".join(
                    f"{row['error_type']}：{row['sample']}" for row in item["observations"])
                for item in problems]]
