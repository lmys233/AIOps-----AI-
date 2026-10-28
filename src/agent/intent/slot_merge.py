"""槽位合并 —— "正则抽到的"与"模型抽到的"合成最终槽位。

★ 为什么单独一个模块：这段逻辑（合并优先级 + 枚举范围权威）与"三阶段路由"
  是**两件事**，各自有各自的判据。混在一个文件里时，任何一条改动都会让
  "路由"与"合并"两条口径一起移动，评审时也看不出改动落在哪一条上。

合并规则（**顺序即优先级**，写在代码里而不是配置里）：

| 字段 | 谁赢 | 为什么 |
|---|---|---|
| `service` / `service_names` | 正则顺序优先，模型**补充** | 服务名是实体：模型给的必须在观测清单里，否则丢弃 |
| `change_id` / `slo_target` / `time_window` / `cluster` / `namespace` | **正则赢** | 这些是"用户字面上说了什么"，模型改写 = 幻觉改写用户原话 |
| `symptom` | 正则优先，否则用模型的 | 症状是描述性文本，模型比词表更会读 |
| `env` | 输入上下文 > 正则 > 模型 | 环境由请求上下文决定，模型只是兜底 |

★★ 两条 `service_names[0]` 的删除（本模块的 `merge_slots` 与 `_keep_enumeration`）：

`service_names` 里放的是**这次要一起看的服务**。从中取第 0 个当"那个服务"，
等价于把"N 个服务"静默收窄成 1 个 —— 而第 0 个恰好是清单里的第一个
（本仓的服务清单头一个是 `gateway`），于是"整个系统是否正常"这类提问
会稳定地落成"只看 gateway"。所以：

· 合并阶段**不再**从服务清单里发明 `service`（模型没点名就是没点名）；
· 枚举指代（"所有服务 / 整个系统"）明确落到 `service=None` + `service_names=全部`。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from ..common.contracts import Slots
from .slots import SYMPTOM_MAX, SlotConfig, entity_is_wellformed, normalize_time_window

__all__ = ["merge_slots", "keep_enumeration", "filled_str"]

#: 症状截断长度 —— 与抽取层（`slots._symptom`）共用同一个上限，
#: 否则"用户看到的症状"与"抽出来存下的症状"会长短不一。
_SYMPTOM_MAX = SYMPTOM_MAX


def filled_str(value: Any) -> Optional[str]:
    """归一成"非空字符串或 None"（空格串按空处理 —— 不算用户说了）。"""
    text = str(value).strip() if value is not None else ""
    return text or None


def merge_slots(
    regex_slots: Slots,
    decision: Optional[Any],
    *,
    config: SlotConfig,
    regex_unresolved: tuple[str, ...] = (),
    excluded_services: tuple[str, ...] = (),
) -> tuple[Slots, tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """把"正则抽到的"与"模型抽到的"合并成最终槽位。

    Args:
        regex_unresolved: 正则层**确实没抽到**的槽位名（`SlotExtraction.unresolved`）。
            ★ 必须传：`env` 有默认值 `prod`，"用户明说了生产"与"什么都没说"在
            `regex_slots.env` 上不可区分 —— 只有这份名单能区分。

    Returns:
        `(合并后的 Slots, 冲突留痕, 丢弃留痕, 被丢弃的字段名, 说明留痕)`。
        第 4 项要进 `unresolved_entities`（否则"丢了一个槽位"是静默行为）；
        第 5 项进 `degraded`（采纳类留痕，例如 `env` 由模型兜底给出）。
    """
    conflicts: list[str] = []
    dropped: list[str] = []
    dropped_fields: list[str] = []
    notes: list[str] = []

    data = regex_slots.model_dump()
    allowed_services = set(config.service_names) - set(excluded_services)
    names = _merge_service_names(regex_slots, decision, allowed_services, dropped,
                                 dropped_fields)
    data["service_names"] = names
    _merge_entities(data, decision, config, conflicts, dropped, dropped_fields)
    _merge_time_window(data, decision, config, conflicts, dropped, dropped_fields)
    if decision is not None and not filled_str(data.get("symptom")):
        proposal = filled_str(decision.symptom)
        if proposal:
            data["symptom"] = proposal[:_SYMPTOM_MAX]
    _merge_env(data, decision, config, regex_unresolved, conflicts, dropped, dropped_fields,
               notes)
    # ★ **不**从服务清单里发明 `service`：清单有 N 个服务 ≠ 用户点了第一个。
    #   目标实体由调用方按"只有一个服务"或"上下文实参"决定（`service` 保持 None）。
    return (Slots(**data), tuple(conflicts), tuple(dropped),
            tuple(dict.fromkeys(dropped_fields)), tuple(notes))


def _merge_service_names(regex_slots: Slots, decision: Optional[Any],
                         allowed_services: set[str], dropped: list[str],
                         dropped_fields: list[str]) -> list[str]:
    """服务清单：正则顺序在前，模型补充（且必须在观测清单里）。"""
    names: list[str] = list(regex_slots.service_names)
    if decision is None:
        return names
    for raw in decision.service_names or []:
        name = filled_str(raw)
        if not name:
            continue
        if name not in allowed_services:
            dropped.append(
                f"模型给的服务 {name!r} 不在观测清单 {sorted(allowed_services)} 里 → 丢弃")
            dropped_fields.append("service")
            continue
        if name not in names:
            names.append(name)
    return names


#: 可验证实体：正则赢，模型只做补充（模型 schema 里没有 cluster/namespace → None）
_ENTITY_PROPOSALS = (("change_id", "change_id"), ("slo", "slo_target"),
                     ("cluster", "cluster"), ("namespace", "namespace"))


def _merge_entities(data: dict[str, Any], decision: Optional[Any], config: SlotConfig,
                    conflicts: list[str], dropped: list[str],
                    dropped_fields: list[str]) -> None:
    if decision is None:
        return
    proposal_of = {"change_id": decision.change_id, "slo": decision.slo_target,
                   "cluster": None, "namespace": None}
    for key, field_name in _ENTITY_PROPOSALS:
        current = filled_str(data.get(field_name))
        proposal = filled_str(proposal_of[key])
        if not proposal:
            continue
        if not entity_is_wellformed(config, key, proposal):
            dropped.append(
                f"模型给的 {field_name}={proposal!r} 不符合数据表里 patterns.{key} "
                f"的取值格式 → 丢弃")
            dropped_fields.append(field_name)
            continue
        if current and current != proposal:
            conflicts.append(
                f"{field_name}：正则抽到 {current!r}，模型给的 {proposal!r} "
                f"→ **以正则为准**（模型不得改写用户的原话）")
            continue
        if not current:
            data[field_name] = proposal


def _merge_time_window(data: dict[str, Any], decision: Optional[Any], config: SlotConfig,
                       conflicts: list[str], dropped: list[str],
                       dropped_fields: list[str]) -> None:
    """时间窗：正则赢；模型的先归一化（它常回中文写法）。"""
    if decision is None:
        return
    proposal_raw = filled_str(decision.time_window)
    if not proposal_raw:
        return
    normalized = normalize_time_window(proposal_raw, config=config)
    current = filled_str(data.get("time_window"))
    if normalized is None:
        dropped.append(f"模型给的时间窗 {proposal_raw!r} 归一不到 \\d+(s|m|h|d) → 丢弃")
        dropped_fields.append("time_window")
    elif current and current != normalized and current != config.default_time_window:
        conflicts.append(
            f"time_window：正则抽到 {current!r}，模型给的 {proposal_raw!r} "
            f"（归一为 {normalized!r}）→ **以正则为准**")
    elif current in (None, config.default_time_window):
        # ★ 正则只给了默认值（= 用户没说）时才采信模型
        data["time_window"] = normalized


def _merge_env(data: dict[str, Any], decision: Optional[Any], config: SlotConfig,
               regex_unresolved: tuple[str, ...], conflicts: list[str], dropped: list[str],
               dropped_fields: list[str], notes: list[str]) -> None:
    """环境：上下文 > 正则 > 模型。

    ★★ 只有正则**确实没抽到** env 才采纳模型值（判据是 `regex_unresolved`，
      不是 `data["env"] in (None, "prod")` —— 后者恒为真，会把
      "用户明说生产"与"什么都没说"混为一谈，让模型静默改环境）。
      ★ 采纳**既不是 conflict 也不是 dropped**，但同样必须留痕（零留痕 =
      悄悄换了一套数据源去取证，比查不到更糟）。
    """
    if decision is None:
        return
    proposal = filled_str(decision.env)
    if not proposal:
        return
    allowed_envs = {env.value for env, _ in config.envs}
    if proposal not in allowed_envs:
        dropped.append(f"模型给的环境 {proposal!r} 不在 {sorted(allowed_envs)} 里 → 丢弃")
        dropped_fields.append("env")
    elif data.get("env") is None or "env" in regex_unresolved:
        data["env"] = proposal
        notes.append(
            f"env 由**模型兜底**给出 → {proposal!r}"
            f"（用户原话与请求上下文都没给环境；环境判错会查到另一套数据）")
    else:
        conflicts.append(
            f"模型把 env 改成 {proposal!r}，而槽位已有 {data['env']!r}"
            f"（可能来自用户原话或环境）→ **不采信模型的值**")


def keep_enumeration(slots: Slots, names: Sequence[str]) -> tuple[Slots, tuple[str, ...]]:
    """把"枚举指代"落到槽位上：`service=None` + `service_names=全部`（**覆盖模型的收窄**）。

    ★ 实测（用户真机 2026-10-04）：「当前系统所有服务是否存在问题」被判成 `METRIC_QUERY`
      且 `service=gateway` —— 模型从服务清单里挑了第一个；问的是全局却只查了一个服务。
    """
    data = slots.model_dump() if hasattr(slots, "model_dump") else dict(slots)
    narrowed = ""
    raw = data.get("service")
    if isinstance(raw, Mapping):
        narrowed = str(raw.get("name") or "")
    elif raw:
        narrowed = str(raw)
    data["service"] = None
    data["service_names"] = list(names)
    note = (f"「所有服务 / 整个系统」是**枚举指代** → 按全部 {len(names)} 个服务处理"
            + (f"（模型只挑了一个 `{narrowed}`，已改回全部）" if narrowed else ""))
    return Slots(**data), (note,)
