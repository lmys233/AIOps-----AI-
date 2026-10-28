"""证据的**出处**与**锚点**：这一轮查的，还是上一轮继承来的？

★ 为什么单列一层：同一条证据在不同地方会被问三种问题 ——
  ① "它是本轮取到的吗？"（答案层据此判"没查成 ≠ 没有异常"，
     处置建议据此判"这一轮到底有没有依据"）；
  ② "它说的是哪个服务？"（处置建议据此把方案锚到**用户点名的那件事**上）；
  ③ "它是不是上一轮的？"（要如实标注成"上一轮（时刻）"，不许伪装成本轮）。

★ 判据只有一条：**本轮取到的证据只来自 `state.evidence`**（派生自本轮各角色的
  `AgentOutcome`），继承来的那批**从不写进** `state.evidence`（见 `actions.py`
  的继承口径）—— 所以"在不在 `state.evidence` 里"就是"是不是这一轮的"。

★ 空 `evidence` 的语义是"不知道"，**不是**"对谁都算数"：拿不到实体名的证据
  一律**原样保留**（宁可多留一条，不可把"查到了"当成"没查到"）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Final, Mapping, Sequence

__all__ = ["SOURCE_CURRENT", "SOURCE_INHERITED", "SOURCE_UNKNOWN",
           "service_names_of", "scope_evidence", "labeled_evidence",
           "inherited_record", "inherited_evidence", "inherited_at_text",
           "anchoring_names"]

#: 证据出处（进"依据"的人话里，用户要看得出这是哪一轮的）
SOURCE_CURRENT: Final[str] = "本轮取证"
SOURCE_INHERITED: Final[str] = "上一轮取证"
SOURCE_UNKNOWN: Final[str] = "取证轮次未知"


def service_names_of(item: Any) -> set[str]:
    """这条证据/假设**说的是哪个服务**（小写；取不到就是空集）。

    ★ 三种形状都要认：契约对象（`Evidence.entity` 是 `EntityRef`）、
      结构化 dict（`{"entity": {"name": …}}`）、以及单测里那些"只给 id 与信号"的
      最小 dict（它们**没有**实体 —— 那时返回空集，调用方按"不知道"处理）。
    """
    if item is None or isinstance(item, (str, bytes)):
        return set()
    entity = getattr(item, "entity", None)
    if entity is None and isinstance(item, Mapping):
        entity = item.get("entity")
    names: set[str] = set()
    for source in (entity, item):
        name = ""
        if isinstance(source, Mapping):
            name = str(source.get("name") or "").strip()
        elif source is not None and not isinstance(source, (str, bytes)):
            name = str(getattr(source, "name", "") or "").strip()
        if name:
            names.add(name.lower())
    return names


def anchoring_names(names: Sequence[Any],
                    evidence: Sequence[Any]) -> frozenset[str]:
    """明确点名的服务优先；未给目标时才从证据实体推导范围。"""
    wanted = {str(name).strip().lower() for name in names if str(name).strip()}
    if wanted:
        return frozenset(wanted)
    for item in evidence or ():
        wanted |= service_names_of(item)
    return frozenset(wanted)


def scope_evidence(evidence: Sequence[Any], names: Any) -> list[Any]:
    """只留**锚定服务**上的证据（拿不到实体的证据原样保留）。

    ★ 这是"方案不许锚错对象"的第一道：上一轮查的是 inventory、这一轮问的是 order 时，
      把 inventory 的证据喂给方案生成器，就会生出"确认 inventory_stock 是否存在"
      这种与问题无关的方案（真机实测的靶子）。
    ★ 空 `names` = 没有锚 → 原样返回（不知道就不筛）。
    """
    wanted = {str(name).strip().lower() for name in (names or ()) if str(name).strip()}
    if not wanted:
        return list(evidence or ())
    kept: list[Any] = []
    for item in evidence or ():
        item_names = service_names_of(item)
        if not item_names or (item_names & wanted):
            kept.append(item)
    return kept


def inherited_record(flags: Any) -> Mapping[str, Any]:
    """本轮的**上一轮排查记录**（没继承到就是空映射）。"""
    if not isinstance(flags, Mapping):
        return {}
    carried = flags.get("inherited_investigation")
    return carried if isinstance(carried, Mapping) else {}


def inherited_evidence(flags: Any) -> tuple[Any, ...]:
    """上一轮那批证据（用于生成方案与如实标注；**不是**本轮的证据）。"""
    return tuple(inherited_record(flags).get("evidence") or ())


def inherited_at_text(flags: Any) -> str:
    """上一轮那次排查的**抓取时刻**（人话）；取不到就给空串（不编时刻）。"""
    carried = inherited_record(flags)
    try:
        stamp = float(carried.get("at") or 0.0)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")


def labeled_evidence(evidence: Sequence[Any], current: Sequence[Any],
                     *, inherited_at: str = "") -> list[Any]:
    """给每条证据附上**出处**，返回 `[(证据, 出处短语)]`（顺序与输入一致）。

    - 在本轮证据里的 → `本轮取证`；
    - 在 `current` 之外的 → `上一轮取证`（带抓取时刻又给不出时刻时只说"上一轮"）；
    - 两者都判不出来 → `取证轮次未知`（**不猜**成本轮）。
    """
    round_keys = {id(item) for item in (current or ())}
    by_id = {str(getattr(item, "evidence_id", "") or
                 (item.get("evidence_id") if isinstance(item, Mapping) else "") or ""): item
             for item in (current or ())}
    out: list[Any] = []
    for item in evidence or ():
        if id(item) in round_keys:
            out.append((item, SOURCE_CURRENT))
            continue
        key = str(getattr(item, "evidence_id", "") or
                  (item.get("evidence_id") if isinstance(item, Mapping) else "") or "")
        if key and key in by_id:
            out.append((item, SOURCE_CURRENT))
        elif round_keys:
            out.append((item, f"{SOURCE_INHERITED}（{inherited_at}）" if inherited_at
                        else SOURCE_INHERITED))
        else:
            out.append((item, SOURCE_UNKNOWN))
    return out
