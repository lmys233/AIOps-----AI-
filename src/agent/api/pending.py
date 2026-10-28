"""**待确认状态**（`PendingConfirmation`）—— 多轮确认与跨话题恢复的**结构化依据**（T59a）。

★★ 为什么要它（用户 2026-10-03 方案的核心）："确认"不是普通全局意图，而是**带目标的对话行为**；
执行/接力对象**必须由结构化 pending 决定**，模型只能辅助判断"是不是在确认 / 更像哪一个"。
修前的缺口（真机实录）：`怎么解决？` 那轮问了"你确认要执行处置动作吗？"，
但登记条件要求有唯一 `proposed_action`（只有候选方案时是 `null`）→ **什么都没记**；
用户回「确认」时既没有待确认动作、台账也不认肯定词 → 越界澄清 / 重复确认。

本模块只做**状态**（T59a）：

| 概念 | 说明 |
|---|---|
| `confirmation_type` | `proposition_confirmation`（命题/结论）/ `action_confirmation`（行动）/ `option_selection`（选项）/ `meta_confirmation`（元） |
| `target` | 确认对象：`{"type": "action"|"conclusion"|"option"|"assistant_conclusion", …}` |
| `on_affirm` / `on_deny` | 确认/否认之后**该做什么**（字符串动作名，由上层解释；本层不执行任何东西） |
| `expected_responder` | 谁该回答（`user` / `assistant`）—— ★ **角色隔离**：AI 的话不得被当成用户确认 |
| `status` | `pending` / `suspended`（切话题后挂起，**不删**）/ `resolved` / `cancelled` / `expired` |
| `expires_at` / `last_active_at` | TTL 与"最近活跃"（多 pending 消歧的依据之一） |

★ 三条纪律：① 只存**结构化事实**，不存自然语言猜测；② 有界（每会话最多 8 条、全局 512 条）；
③ 过期即 `expired`（**不静默删**，匹配时能如实说"那条已经过期了"）。
"""

from __future__ import annotations
from ..observed.context import session_key


import time
from dataclasses import dataclass, field, replace
from typing import Any, Final, Mapping, Optional, Sequence

#: 方案摘要有**两种来源**：响应体里的方案表（台账同源）与编排层产物（对象）——
#: 两者字段同名，所以读取统一走 `_plan_field`（不另写一套形状）。
from .tasks import plans_of_body

__all__ = ["PendingConfirmation", "PendingRegistry", "PendingKind",
           "register_pending_from_body",
           "PENDING_TTL_SEC", "PENDING_PRUNE_GRACE_SEC",
           "MAX_PENDING_PER_SESSION", "MAX_PENDING_TOTAL"]

#: 待确认状态的默认存活时间（用户方案：默认 TTL 5~10 分钟）
PENDING_TTL_SEC: Final[int] = 600
#: 过期 / 已结束的记录**再留多久**才真正清掉。
#: ★ 不能一过期就删：用户很可能正是在这时候回「确认」，那时必须能如实说"那条已经过期了"；
#:   宽限期过了再清（清理只是省内存，不是判据）。
PENDING_PRUNE_GRACE_SEC: Final[int] = 300
#: 每会话最多几条（多的挂起/淘汰由 `register` 处理）
MAX_PENDING_PER_SESSION: Final[int] = 8
#: 进程内实现的全局上限（超出淘汰最久未活跃的）
MAX_PENDING_TOTAL: Final[int] = 512


class PendingKind:
    """`confirmation_type` 的取值（**字符串常量**，便于落 JSON 与人读）。"""

    PROPOSITION: Final[str] = "proposition_confirmation"
    ACTION: Final[str] = "action_confirmation"
    OPTION: Final[str] = "option_selection"
    META: Final[str] = "meta_confirmation"

    ALL: Final[tuple[str, ...]] = (PROPOSITION, ACTION, OPTION, META)


@dataclass(frozen=True)
class PendingConfirmation:
    """一条待确认状态（**不可变**：状态迁移用 `evolve()` 造新的一份）。"""

    pending_id: str
    session_id: str
    confirmation_type: str
    target: Mapping[str, Any] = field(default_factory=dict)
    options: tuple[Mapping[str, Any], ...] = ()
    expected_signals: tuple[str, ...] = ("AFFIRM", "DENY")
    on_affirm: str = ""
    on_deny: str = ""
    risk: str = "low"
    status: str = "pending"
    requested_by: str = "assistant"
    expected_responder: str = "user"
    created_at: float = 0.0
    last_active_at: float = 0.0
    expires_at: float = 0.0
    source_turn_id: str = ""

    # ------------------------------------------------------------------
    @property
    def alive(self) -> bool:
        """还没被解决/取消，且**没过期**。"""
        return self.status in ("pending", "suspended")

    def expired(self, *, now: Optional[float] = None) -> bool:
        return self.expires_at > 0 and (now if now is not None else time.time()) >= self.expires_at

    def evolve(self, **changes: Any) -> "PendingConfirmation":
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {"pending_id": self.pending_id, "session_id": self.session_id,
                "confirmation_type": self.confirmation_type,
                "target": dict(self.target), "options": [dict(x) for x in self.options],
                "expected_signals": list(self.expected_signals),
                "on_affirm": self.on_affirm, "on_deny": self.on_deny,
                "risk": self.risk, "status": self.status,
                "requested_by": self.requested_by,
                "expected_responder": self.expected_responder,
                "created_at": self.created_at, "last_active_at": self.last_active_at,
                "expires_at": self.expires_at, "source_turn_id": self.source_turn_id}

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> "PendingConfirmation":
        """从存下来的 dict 还原（形状不对的字段**取默认**，不抛 —— 一条坏记录不该带走整轮）。"""
        def _num(key: str) -> float:
            try:
                return float(doc.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        return cls(pending_id=str(doc.get("pending_id") or ""),
                   session_id=str(doc.get("session_id") or ""),
                   confirmation_type=str(doc.get("confirmation_type") or PendingKind.PROPOSITION),
                   target=dict(doc.get("target") or {}),
                   options=tuple(dict(x) for x in (doc.get("options") or ())
                                 if isinstance(x, Mapping)),
                   expected_signals=tuple(str(s) for s in (doc.get("expected_signals") or ())),
                   on_affirm=str(doc.get("on_affirm") or ""),
                   on_deny=str(doc.get("on_deny") or ""),
                   risk=str(doc.get("risk") or "low"),
                   status=str(doc.get("status") or "pending"),
                   requested_by=str(doc.get("requested_by") or "assistant"),
                   expected_responder=str(doc.get("expected_responder") or "user"),
                   created_at=_num("created_at"), last_active_at=_num("last_active_at"),
                   expires_at=_num("expires_at"),
                   source_turn_id=str(doc.get("source_turn_id") or ""))


class PendingRegistry:
    """每会话一个 pending **列表**（进程内实现；共享存储版在 T59a 的后续里接同一接口）。

    ★ 与既有的"单槽待确认动作"（`PendingActionStore`）**并存**而不是替换：
      那条路是写操作审批链的入口，本层是"对话行为 → 目标"的依据 —— 两者职责不同，
      合起来才是用户方案里的完整状态（动作审批仍在既有链路上，红线不动）。
    """

    def __init__(self, *, ttl_sec: int = PENDING_TTL_SEC,
                 max_per_session: int = MAX_PENDING_PER_SESSION,
                 max_total: int = MAX_PENDING_TOTAL) -> None:
        self.ttl_sec = int(ttl_sec)
        self.max_per_session = int(max_per_session)
        self.max_total = int(max_total)
        self._items: dict[str, list[PendingConfirmation]] = {}
        self._seq = 0

    # ------------------------------------------------------------------
    def register(self, *, session_id: str, confirmation_type: str,
                 target: Mapping[str, Any], options: Sequence[Mapping[str, Any]] = (),
                 on_affirm: str = "", on_deny: str = "", risk: str = "low",
                 expected_signals: Sequence[str] = ("AFFIRM", "DENY"),
                 requested_by: str = "assistant", expected_responder: str = "user",
                 source_turn_id: str = "", now: Optional[float] = None) -> PendingConfirmation:
        """登记一条待确认状态；**把该会话里旧的 pending 标成 `suspended`（不删）**。"""
        moment = float(now if now is not None else time.time())
        self._seq += 1
        record = PendingConfirmation(
            pending_id=f"p_{int(moment * 1000)}_{self._seq}", session_id=str(session_id),
            confirmation_type=str(confirmation_type), target=dict(target),
            options=tuple(dict(x) for x in options),
            expected_signals=tuple(str(s) for s in expected_signals),
            on_affirm=str(on_affirm), on_deny=str(on_deny), risk=str(risk),
            status="pending", requested_by=str(requested_by),
            expected_responder=str(expected_responder), created_at=moment,
            last_active_at=moment, expires_at=moment + self.ttl_sec,
            source_turn_id=str(source_turn_id))
        self.prune(now=moment)
        session_items = self._items.setdefault(session_key(str(session_id)), [])
        for index, item in enumerate(session_items):
            if item.alive:
                session_items[index] = item.evolve(status="suspended", last_active_at=moment)
        session_items.insert(0, record)
        del session_items[self.max_per_session:]
        self._trim_total()
        return record

    def candidates(self, session_id: str, *, responder: str = "user",
                   now: Optional[float] = None,
                   include_expired: bool = False) -> list[PendingConfirmation]:
        """可匹配的候选：本会话、**未解决**、`expected_responder` 对得上（★ 角色隔离）。

        ★ 过期的不静默丢：`include_expired=True` 时一并返回（调用方据此提示"那条已过期"）。
        """
        moment = float(now if now is not None else time.time())
        out: list[PendingConfirmation] = []
        for item in self._items.get(session_key(str(session_id)), []):
            if item.expected_responder != str(responder):
                continue
            if item.expired(now=moment):
                if include_expired:
                    out.append(item.evolve(status="expired"))
                continue
            if item.alive:
                out.append(item)
        return out

    def resolve(self, session_id: str, pending_id: str, *, status: str = "resolved",
                now: Optional[float] = None) -> Optional[PendingConfirmation]:
        """把一条 pending 落成 `resolved` / `cancelled`（**只动这一条**）。"""
        moment = float(now if now is not None else time.time())
        items = self._items.get(session_key(str(session_id)), [])
        for index, item in enumerate(items):
            if item.pending_id == str(pending_id):
                items[index] = item.evolve(status=str(status), last_active_at=moment)
                return items[index]
        return None

    def note(self, session_id: str, pending_id: str, **changes: Any) -> Optional[PendingConfirmation]:
        """给一条 pending **补标记**（如"二次确认已问过"）—— 不改变状态机本身。"""
        items = self._items.get(session_key(str(session_id)), [])
        for index, item in enumerate(items):
            if item.pending_id != str(pending_id):
                continue
            target = dict(item.target)
            target.update(dict(changes.pop("target", {}) or {}))
            items[index] = item.evolve(target=target, **changes)
            return items[index]
        return None

    def suspend_all(self, session_id: str, *, now: Optional[float] = None) -> int:
        """把该会话所有 pending 挂起（**不删**）—— 用户切话题时用，回来仍能确认。"""
        moment = float(now if now is not None else time.time())
        count = 0
        items = self._items.get(session_key(str(session_id)), [])
        for index, item in enumerate(items):
            if item.status == "pending":
                items[index] = item.evolve(status="suspended", last_active_at=moment)
                count += 1
        return count

    def prune(self, *, now: Optional[float] = None,
              grace_sec: int = PENDING_PRUNE_GRACE_SEC) -> int:
        """清掉**已经没用**的记录（过期 / 已解决 / 已取消，且过了宽限期）。

        ★ 判据只两条：**状态已经结束**（或过期）**且**距最近活跃超过宽限期。
          还在等的、宽限期内的（用户可能正要回「确认」）一律留着。
        ★ 返回清掉的条数 —— 清理是**可核对**的动作，不是悄悄发生的事。
        """
        moment = float(now if now is not None else time.time())
        removed = 0
        for sid, items in list(self._items.items()):
            keep: list[PendingConfirmation] = []
            for item in items:
                ended = item.expired(now=moment) or item.status in ("resolved", "cancelled")
                if ended and moment - item.last_active_at > grace_sec:
                    removed += 1
                    continue
                keep.append(item)
            if keep:
                self._items[sid] = keep
            else:
                self._items.pop(sid, None)
        return removed

    def clear(self, session_id: str) -> None:
        self._items.pop(session_key(str(session_id)), None)

    # ------------------------------------------------------------------
    def _trim_total(self) -> None:
        total = sum(len(items) for items in self._items.values())
        if total <= self.max_total:
            return
        flat = sorted(((item.last_active_at, sid, index, item)
                       for sid, items in self._items.items()
                       for index, item in enumerate(items)))
        for _stamp, sid, _index, item in flat[:total - self.max_total]:
            items = self._items.get(sid) or []
            self._items[sid] = [x for x in items if x.pending_id != item.pending_id]


def _questions_of(body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """这一轮**问了什么**（澄清问题列表；形状不对的一律不看）。"""
    raw = ((body.get("clarification") or {}).get("questions") or [])
    return [item for item in raw if isinstance(item, Mapping)]


def _target_id_of(body: Mapping[str, Any]) -> str:
    """这次要动的对象（服务 id）—— 从已定槽位里取；取不到就空串（不猜）。"""
    slots = body.get("slots")
    if isinstance(slots, Mapping):
        for key in ("service", "target", "entity", "service_name"):
            if slots.get(key):
                return str(slots[key])
    entities = body.get("unresolved_entities") or ()
    for item in entities:
        if isinstance(item, str) and item:
            return item
    return ""


def _plan_field(plan: Any, name: str, default: Any = "") -> Any:
    """方案字段：台账摘要与编排产物**同名字段**，两种形状都读。"""
    if isinstance(plan, Mapping):
        return plan.get(name, default)
    return getattr(plan, name, default)


def _plan_payload(first: Any, body: Mapping[str, Any]) -> dict[str, Any]:
    """把首选方案摊成**可确认的载荷**（动作 / 风险 / 回滚）—— 确认时据此走既有结局。

    ★ 为什么要存：`如何解决？` 那一轮可能只有候选方案、没有唯一动作（`proposed_action=null`），
      不存的话下一句「确认」就无据可依，只能反问"你要执行哪一条"（真机实测）。
    """
    if first is None:
        return {}
    title = str(_plan_field(first, "title") or "")
    action_class = str(_plan_field(first, "action_class") or "manual")
    kind = str(_plan_field(first, "kind") or "")
    if not kind:            # 方案表里没有 `kind` 这一列 → 按动作类/标题如实判"要不要人工"
        kind = "manual" if (action_class == "manual" or "人工" in title) else "agent_action"
    return {"kind": kind, "title": title,
            "option_id": str(_plan_field(first, "option_id") or ""),
            "risk": {"level": str(_plan_field(first, "risk_level") or "L2"),
                     "requires_approval": bool(_plan_field(first, "requires_approval", False))},
            "action": {"action_class": action_class,
                       "target": {"kind": "service", "id": _target_id_of(body)},
                       "params": {}, "dry_run": True, "dry_run_diff": "",
                       "rollback_plan": str(_plan_field(first, "rollback") or "")}}


def _risk_level(facts: Mapping[str, Any], target: Mapping[str, Any]) -> str:
    """风险等级：先看这一轮的 `facts`，没有就看**首选方案**自己带的等级。"""
    risk = facts.get("risk")
    if isinstance(risk, Mapping) and risk.get("level"):
        return str(risk.get("level"))
    plan = target.get("plan") if isinstance(target, Mapping) else None
    return str(((plan or {}).get("risk") or {}).get("level") or "L2")


def _pending_target(body: Mapping[str, Any], facts: Mapping[str, Any],
                    questions: Sequence[Mapping[str, Any]]) -> tuple[str, dict[str, Any]]:
    """这一轮该登记成**哪种待确认**、**目标是什么**（类型与目标成对决定）。"""
    incident_id = str(body.get("incident_id") or "")
    action = facts.get("proposed_action")
    if action and isinstance(action, Mapping):
        return PendingKind.ACTION, {
            "type": "action", "action": str(action.get("action_class") or ""),
            "object_id": str(action.get("target") or ""), "incident_id": incident_id,
            "content": str(action.get("summary") or action.get("reason") or "")}
    if any(str((item or {}).get("slot") or "") == "confirm" for item in questions):
        # ★ 处置确认门（可能只有候选方案、没有唯一动作）→ 目标取**首选方案**
        # ★ 先读**用户真正看到的那张方案表**（台账同源），再退回编排层产物
        plans = list(plans_of_body(body) or ()) or list(facts.get("remediation_plans") or ())
        first = plans[0] if plans else None
        return PendingKind.ACTION, {
            "type": "action", "action": str(_plan_field(first, "action_class") or "manual"),
            "object_id": "", "incident_id": incident_id,
            "content": str(_plan_field(first, "title") or "处置动作"),
            "plan": _plan_payload(first, body)}
    return PendingKind.OPTION, {"type": "option", "content": "澄清：待补槽位",
                                "incident_id": incident_id}


def register_pending_from_body(registry: PendingRegistry, *, session_id: str,
                               body: Mapping[str, Any],
                               facts: Mapping[str, Any]) -> None:
    """每一次反问 / 每个确认门都登记一条 **`PendingConfirmation`**（生成钩子）。

    ★★ 为什么必须"每次都记"：真机实录：确认门**问了却没登记**，
      于是下一句「确认」无人认领。这里把三类情形都收成结构化 pending：
        · 处置确认门 + 有预案/候选方案 → `action_confirmation`（目标=动作或首选方案）
        · 澄清问题 → `option_selection`（选项=待补的槽位）
    ★ 登记**不改变**任何执行语义：写能力仍在 `allow_write` 与审批链上，本层只描述"在等什么"。
    """
    session_id = str(session_id or "")
    if not session_id or str(body.get("status") or "") != "clarify":
        return
    questions = _questions_of(body)
    if not questions:
        return
    kind, target = _pending_target(body, facts, questions)
    level = _risk_level(facts, target)
    registry.register(
        session_id=session_id, confirmation_type=kind, target=target,
        options=tuple({"id": str((item or {}).get("slot") or ""),
                       "label": str((item or {}).get("question") or "")[:80]}
                      for item in questions),
        on_affirm=("finish_confirmation" if kind == PendingKind.ACTION else "resume_task"),
        on_deny=("cancel_pending" if kind == PendingKind.ACTION else "drop_question"),
        risk=("high" if level in ("L3", "L4") else "low"),
        source_turn_id=str(body.get("turn_id") or ""))
