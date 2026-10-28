"""候选处置方案 —— **按假设类别 + 证据信号 + 变更事件 + 爆炸半径**生成，纯函数、零 IO。

--------------------------------------------------------------------------------
★ 为什么候选由**代码**生成，模型只负责排序与措辞
--------------------------------------------------------------------------------

"能建议哪些动作"必须收在白名单（`ActionClass`）里。让模型自由生成，就等于让它
有机会编出一个**做不到**的动作 —— 而处置建议一旦不可执行，用户按它操作只会更糟。
所以：**候选集由本模块算出来**，`L4`（禁止类）一个都不许出现，且构造期就断言这件事。

--------------------------------------------------------------------------------
★ 两类方案必须分开标注（如实原则）
--------------------------------------------------------------------------------

| 类别 | 含义 | 本模块怎么产出 |
|---|---|---|
| `agent_action` | 白名单里的变更动作（重启/回滚/扩容/摘流），可生成预案 + 风险分级 | `PLAN_STEPS` 给标准步骤模板 |
| `manual` | **Agent 做不到、必须人工在代码/迁移里修**（例如"表不存在 → 修 SQL 或补迁移"） | 绝不假装配一个动作来"显得能执行" |

★ 真实现场（`UndefinedTableError: relation "inventory_stock" does not exist`）必须落到
  **人工修**这一类：只给重启/回滚，治不了"表不存在"。

--------------------------------------------------------------------------------
★ 没认出动作但有证据 → 仍然要给候选
--------------------------------------------------------------------------------

用户问"怎么解决"时，写意图路径**没有**认出具体动作（`action` 为空）。以前那条路
把"确认"换成"问动作"。现在改成：**生成候选方案** → 要么出表格让用户选，
要么只有一个方案时直接出 `A -> B -> C` 流程。一个候选都算不出来时（既无证据也无假设），
给一条**如实**的建议：先补齐证据再定处置 —— 不猜原因、不编动作。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final, Mapping, Sequence

from ..common.contracts import EntityRef, ProposedAction
from ..common.enums import (ActionClass, DEFAULT_ACTION_LEVEL, RiskLevel, _StrEnum)
from ..tools.risk import classify_risk

__all__ = [
    "DEFAULT_PLAN_LIMIT",
    "PLAN_STEPS",
    "PlanKind",
    "RemediationPlan",
    "candidate_plans",
    "class_of_title",
    "format_plans",
    "plan_from_ledger",
    "plan_steps_for",
    "select_plan",
    "selected_from_flags",
]

#: 一次最多给几条方案（多给只会让用户更难选）。
#: ★ 取值只有这一处：模型生成的草稿与代码候选**共用**这个上限。
DEFAULT_PLAN_LIMIT: Final[int] = 3

#: 动作名 → 用户可能说出的中文说法（`select_plan` 认动作名要用）
ACTION_WORDS: Final[dict[str, tuple[str, ...]]] = {
    ActionClass.RESTART_POD.value: ("重启", "重启 pod", "restart"),
    ActionClass.ROLLBACK_CONFIG.value: ("回滚", "rollback"),
    ActionClass.SCALE.value: ("扩容", "缩容", "加副本", "scale"),
    ActionClass.DRAIN_INSTANCE.value: ("摘流", "摘除", "下线实例", "drain"),
}


class PlanKind(_StrEnum):
    """方案的三类：Agent 能生成预案的动作 / **必须人工做**的修复 / **还查不出来**。"""

    AGENT_ACTION = "agent_action"
    MANUAL = "manual"
    #: ★★ 零证据时的"先补齐证据"：它**不是**一条处置方案 —— 单独一类，
    #:   免得被当成"人工修"混进方案表（那正是"没查到说成查过了"的同族）。
    EVIDENCE_GAP = "evidence_gap"


@dataclass(frozen=True)
class RemediationPlan:
    """一条候选方案（自带四件事实：依据 / 预期 / 风险 / 回滚）。

    ★★ `mechanism` 与 `evidence_refs` 是**对症**那两条判据的落点：
      · `mechanism` 回答"**为什么是这个原因**"（有界线程池被阻塞型工作占满 →
        新任务被拒绝执行）。没有它，方案表就退化成"扩容/重启/回滚"三件套 ——
        用户看不出哪一条对着自己说的那个毛病（真机实测的靶子）；
      · `evidence_refs` 是**本轮证据**的 id —— 方案必须能追溯到这一次查到的东西，
        而不是一句放之四海皆准的话。
    """

    option_id: str
    kind: str
    title: str
    action_class: str
    rationale: str
    expected: str
    risk_level: str
    requires_approval: bool
    blast_radius: str
    #: **要做什么**（一句话，可执行的动作）—— 白名单内的方案这里就是它的动作说法，
    #: 人工方案这里是"只能人工做"的那件事（用户要照着它去操作）
    action: str = ""
    #: 对应哪个原因/机制（用户点名的症状 ⇄ 这条方案，必须对得上）
    mechanism: str = ""
    #: 这条方案引用的**本轮证据** id（可核对；不引别的轮次、不编）
    evidence_refs: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    rollback: str = ""
    steps: tuple[str, ...] = ()

    @property
    def is_manual(self) -> bool:
        return self.kind == PlanKind.MANUAL.value

    @property
    def is_gap(self) -> bool:
        """这条是不是**"还查不出来、先补齐证据"**（不是处置方案）。"""
        return self.kind == PlanKind.EVIDENCE_GAP.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "option_id": self.option_id, "kind": self.kind, "title": self.title,
            "action_class": self.action_class, "rationale": self.rationale,
            "action": self.action, "mechanism": self.mechanism,
            "expected": self.expected, "risk_level": self.risk_level,
            "requires_approval": self.requires_approval,
            "blast_radius": self.blast_radius,
            "evidence_refs": list(self.evidence_refs),
            "preconditions": list(self.preconditions),
            "rollback": self.rollback, "steps": list(self.steps),
        }


# ======================================================================
#  一、动作的标准步骤模板（流程用）
# ======================================================================
#: 每个变更动作的**标准处置步骤**（人工方案不用它 —— 它的步骤是"人来做"的那些）。
PLAN_STEPS: Final[dict[str, tuple[str, ...]]] = {
    ActionClass.RESTART_POD.value: (
        "确认当前有几个实例在跑、上游怎么路由", "逐个重启实例并等待就绪",
        "观察指标 180 秒直至恢复", "未恢复则执行回滚"),
    ActionClass.ROLLBACK_CONFIG.value: (
        "确认窗口内的变更单", "核对变更前后的差异",
        "回滚到变更前版本", "观察指标 180 秒直至恢复"),
    ActionClass.SCALE.value: (
        "确认当前副本数与资源余量", "调整副本数并等待就绪",
        "观察指标 180 秒直至恢复", "未恢复则调回原副本数"),
    ActionClass.DRAIN_INSTANCE.value: (
        "从上游摘除该实例", "确认在途请求排空",
        "观察剩余实例的指标 180 秒", "未恢复则回滚配置并恢复摘除"),
}

#: 关键词语表：命中"代码/表结构问题"这一类（判"该走人工修"）。
#: ★★ 这张表既用来比**假设类别**、也用来比**信号名**，所以两边都要顾到：
#:   真机日志那一路的信号叫 `log_error_burst` —— 它**任何故障里都有**，
#:   拿它当"这是代码问题"的依据，用户问「orders 工作线程池已满」也会收到
#:   "人工修 SQL"（实测的靶子）。所以判据的那一侧（`manual_issue`）只拿它比**类别**。
_MANUAL_KEYS: Final[tuple[str, ...]] = (
    "table", "sql", "migration", "schema", "不存在", "does not exist",
    "undefinedtable", "pool_query_failed", "error_burst", "code_error")
_CAPACITY_KEYS: Final[tuple[str, ...]] = ("capacity", "surge", "saturation", "qps")
#: 资源/连接池争用这一类（判"该扩容 / 该调池"）。
#: ★★ 线程池拒绝（`THREAD_POOL_REJECTED`）与连接池取不到（`POOL_ACQUIRE_TIMEOUT`）
#:   是**同一族**：都是"并发承载不够"。不认它们，用户问「工作线程池已满」时
#:   候选里会只剩"重启/回滚"这类通用动作（真机实测）。
_POOL_KEYS: Final[tuple[str, ...]] = (
    "pool_waiting", "pool_in_use", "pool_acquire", "pool_reject",
    "thread_pool", "worker_pool", "线程池", "拒绝执行", "reject")
#: 现象词 → 资源争用（写在**用户原话或证据摘要**里也算，不只认信号名）
_POOL_WORDS: Final[tuple[str, ...]] = ("线程池", "连接池", "池已满", "池满", "拒绝执行",
                                       "reject", "queue", "队列已满", "并发")
#: **用户自己点了"这是代码/库表问题"**的说法（判"该走人工修"的另一半依据）
_MANUAL_TEXT_KEYS: Final[tuple[str, ...]] = ("表", "sql", "迁移", "库表", "数据库")
_CHANGE_KEYS: Final[tuple[str, ...]] = ("change_event", "change_induced", "变更")
_SPREAD_KEYS: Final[tuple[str, ...]] = ("blast_radius", "dependency_graph", "topology")

#: 从证据摘要里挑**具体对象名**（引号里的标识符最可信）
_QUOTED: Final[re.Pattern[str]] = re.compile(r"[\"“]([A-Za-z_][\w.\-]{2,})[\"”]")
_SNAKE: Final[re.Pattern[str]] = re.compile(r"\b([A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+)\b")
#: 查询语句 / 标签里的 `key="VALUE"` —— `VALUE` 是**标签值**，不是出错的对象
_LABEL_VALUE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\s*=\s*\"([^\"]+)\"")


def _get(source: Any, key: str, default: Any = "") -> Any:
    """双通道读一个字段：**先属性、后下标**（契约对象与 dict 两种形状都吃）。"""
    if source is None:
        return default
    try:
        value = getattr(source, key)
    except (AttributeError, TypeError):
        value = None
    if value is not None:
        return value
    try:
        return source[key] if key in source else default
    except (TypeError, KeyError, IndexError):
        return default


def _text(source: Any, key: str) -> str:
    return str(_get(source, key, "") or "").strip()


def _keywords_of(items: Sequence[Any]) -> tuple[frozenset[str], str]:
    """把证据 / 假设里的**类别与信号名**汇总成关键词集合 + 一条人话依据。"""
    words: set[str] = set()
    labels: list[str] = []
    for item in items:
        category = _text(item, "category")
        signal = _text(item, "signal")
        if category:
            words.add(category.lower().replace(" ", "_"))
        if signal:
            words.add(signal.lower())
        label = _label_of(item)
        if label:
            labels.append(label)
    return frozenset(word for word in words if word), "、".join(dict.fromkeys(labels))


def _categories_of(items: Sequence[Any]) -> frozenset[str]:
    """只要**假设类别**（不含信号名）。

    ★★ 为什么要单独一份：`log_error_burst` 这种**信号名**几乎在任何故障里都出现
      （日志那一路的聚类信号就叫这个），拿它当"这是代码问题"的依据，
      任何问题都会被算成"该人工修 SQL"（真机实测的靶子）；
      而**类别** `error_burst` 是打分框架对"应用层错误爆发"的判断，那才是有信息量的。
    """
    return frozenset(_text(item, "category").lower().replace(" ", "_")
                     for item in items if _text(item, "category"))


def _label_of(item: Any) -> str:
    """这条证据/假设的**可核对标识**（有 id 写 id，没有就写信号名）。"""
    for key in ("evidence_id", "hypothesis_id", "id"):
        value = _text(item, key)
        if value:
            return f"{key}={value}"
    return _text(item, "signal") or _text(item, "category")


def _hits(words: frozenset[str], needles: Sequence[str]) -> bool:
    return any(needle in word for word in words for needle in needles)


def _resource_tokens(sources: Sequence[Any],
                     ignore: Sequence[str] = ()) -> tuple[list[str], list[str]]:
    """从这些来源里挑**对象名候选**：返回 `(像对象名的, 其他)`,**引号里的优先**。

    ★ 分成两堆是因为"有没有具体对象"这个判据必须**严**：真机证据里有一句
      `…error_type=DOWNSTREAM_BAD_STATUS…`，其中 `detail` 出现在**引号内**（样本原文），
      于是"人工修"那条方案被写成「确认 detail 是否存在」—— 而用户问的是 order 的线程池。
      所以 `_has_concrete` 只认**像对象名**的那一堆（含下划线 / 带点），
      而方案标题仍可用"其他"里最具体的一个（它至少比"报错涉及的对象"具体）。
    Args:
        ignore: 额外要排除的**已知非对象名**（信号名 / 指标名 / 标签键 —— 真机证据里
            `fault_type="DB_POOL_SIZE"` 这种**标签值**也会被引号规则抓成"对象"）。
    """
    signals = {_text(item, "signal") for item in sources if item is not None}
    signals |= {str(name).strip() for name in ignore if str(name).strip()}
    for item in sources:
        # ★ 查询语句/标签里的 `key="VALUE"` 是**标签值**（`fault_type="DB_POOL_SIZE"`），
        #   不是出错的对象 —— 它们出现在 `raw_ref` 里，一并排除。
        signals |= set(_LABEL_VALUE.findall(_text(item, "raw_ref")))
    objects: list[str] = []
    others: list[str] = []
    for source in sources:
        text = (_text(source, "summary") or _text(source, "statement")
                or (source if isinstance(source, str) else ""))
        signals |= set(re.findall(r"\b([A-Za-z_][\w]*)\s*=", text))
        for token in _QUOTED.findall(text):
            # ★ 标签值（`fault_type="DB_POOL_SIZE"`）**两个池子都不进** ——
            #   它不是出错的对象，让它进 `others` 会让方案标题点名一个查询参数。
            if token and token not in signals:
                if _is_object(token):
                    objects.append(token)
                elif not _is_label_value(text, token):
                    others.append(token)
        for token in _SNAKE.findall(text):
            if token and token not in signals and _is_object(token):
                objects.append(token)
    return objects, others


def _is_label_value(text: str, token: str) -> bool:
    """这个引号里的词是不是**某个键的值**（`fault_type="DB_POOL_SIZE"`）。"""
    return bool(re.search(rf"[A-Za-z_][A-Za-z0-9_]*\s*=\s*\"{re.escape(token)}\"", text))


def _resource(*sources: Any) -> str:
    """挑一个**具体对象名**（表名 / 资源名）—— 依次看摘要、原话、假设陈述。

    ★★ 三条纪律（都是实测踩出来的）：
      ① **引号里的标识符优先**：真机摘要形如
         `…（UndefinedTableError: relation "inventory_stock" does not exist）` —— 引号里那个才是出错的对象；
         没有引号时才退到裸的 snake_case 词。
      ② **信号名不算对象**：`window_error_peak` / `http_error_rate` 这类是**证据的信号名**，
         不是资源名。取错会让方案写成"确认 window_error_peak 是否存在"（用户实测就是这么被误导的）。
      ③ **句子里的普通英文词不算对象**：样本原文里的 `detail` 只是个词，
         不是出错的对象 —— 判据是"像对象名"（含下划线 / 带点），见 `_is_object`。
    """
    objects, others = _resource_tokens(sources)
    for source in sources:
        text = _text(source, "summary") or (source if isinstance(source, str) else "")
        found = re.search(r'(?:relation|table|column|schema)\s+["“]([^"”]+)["”]', text, re.I)
        if found:
            return found.group(1)
    pool = objects or others
    return pool[-1] if pool else ""


#: 裸（无引号）标识符**至少要有下划线**才算对象名：`inventory_stock` 算，
#: `detail` / `orders` 这类句子里的普通词不算（真机靶子：「修对那条 detail」）。
_BARE_OBJECT: Final[re.Pattern[str]] = re.compile(r"[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+")


def _is_object(token: str) -> bool:
    """这个裸词像不像**对象名**（表名 / 资源名）：必须含下划线。"""
    return bool(_BARE_OBJECT.fullmatch(str(token or "")))


def _has_concrete(message: str, evidence: Sequence[Any], hypotheses: Sequence[Any],
                  target: str = "") -> bool:
    """有没有**够具体**的现象或目标可作依据（没有就只能给"先补齐证据"这一条）。

    ★ 点不出对象、也没有证据与假设时**不编动作**：一条"补齐证据"的如实建议
      比"猜一个重启"诚实（处置建议必须能追溯到证据或用户说过的目标）。
    ★★ 这里只认**像对象名**的标识（含下划线 / 带点）：样本原文里的 `detail`
      只是个普通英文词，拿它当"出错的对象"会写出「确认 detail 是否存在」这种方案
      （真机实测的靶子 —— 用户问的是 order 线程池）。
    """
    if target and target != "该服务":
        return True
    objects, _ = _resource_tokens((message, *evidence, *hypotheses))
    if objects:
        return True
    for source in (message, *evidence, *hypotheses):
        text = _text(source, "summary") or _text(source, "statement") or str(source or "")
        if any(needle in str(text).lower() for needle in _MANUAL_KEYS):
            return True
    return bool(evidence or hypotheses)


def _risk_for(action_class: ActionClass, *, mentioned: str,
              target: EntityRef) -> tuple[str, bool, str]:
    """风险等级（起点经升档后）→ `(等级, 是否需审批, 分级理由的人话)`。

    ★ 分级用**同一个** `classify_risk`：处置建议的风险等级与真正走确认门的那条路径
      必须一致，否则用户看到的等级与安全门的判定会各说一套。
    """
    action = ProposedAction(action_class=action_class, target=target)
    decision = classify_risk(action, mentioned=mentioned)
    level = str(decision.level.value)
    approval = bool(decision.requires_approval)
    why = f"风险等级 {level}（该类变更的起点等级）"
    if approval:
        why += "，需人工审批后才可能落地"
    return level, approval, why


def _blast_text(blast_radius: Any, target: str) -> str:
    """影响面（人话）：给不出结构化影响面时**如实说"未评估"**，不编一个。"""
    text = _text(blast_radius, "summary") or _text(blast_radius, "note")
    if not text and isinstance(blast_radius, str):
        text = blast_radius.strip()
    if not text:
        return (f"影响面未评估（本阶段没有执行端，无法给出真实影响面）；"
                f"动作目标 {target}")
    return text


def _target_of(hypotheses: Sequence[Any], slots: Any = None) -> str:
    """动作目标（最像那个假设的根实体 / 槽位里已确认的服务）。"""
    for item in hypotheses:
        root = _get(item, "root_entity", None)
        value = _text(root, "id") or _text(root, "name")
        if value:
            return value
    return _text(_get(slots, "service", None), "name") or "该服务"


def _service_in(message: str, services: Sequence[Any]) -> str:
    """用户这句话里点到的服务名（槽位里已确认的那一份；点不到就是空）。"""
    text = str(message or "")
    return next((str(name) for name in services if name and str(name) in text), "")


#: L4（禁止类）动作集合 —— 候选生成与构造自检**共用这一份**
_L4_ACTIONS: Final[frozenset[str]] = frozenset(
    member.value for member in ActionClass
    if DEFAULT_ACTION_LEVEL.get(member.value, "") == RiskLevel.L4.value)


def _make_plan(*, option_id: str, kind: str, title: str, action_class: str,
               rationale: str, expected: str, risk_level: str, requires_approval: bool,
               blast_radius: str, rollback: str, steps: Sequence[str],
               action: str = "", mechanism: str = "",
               evidence_refs: Sequence[str] = (),
               preconditions: Sequence[str] = ()) -> RemediationPlan:
    """构造一条候选方案（**唯一**构造点，自检也在这里）。

    ★ `action`（要做什么）没给时**退回标题**：这些方案的标题本身就是祈使句
      （"扩容副本…" / "先摘流该实例"），比留空强 —— 方案表里"动作"那一栏不能是空的。
    """
    step_list = tuple(str(item) for item in steps if str(item).strip())
    if kind == PlanKind.AGENT_ACTION.value:
        allowed = {member.value for member in ActionClass}
        if action_class not in allowed:
            raise ValueError(f"候选方案的动作 {action_class!r} 不在白名单里")
        if action_class in _L4_ACTIONS:
            raise ValueError(
                f"L4 动作 {action_class!r} 不许出现在处置候选里（禁止类只能记录审计）")
    return RemediationPlan(
        option_id=option_id, kind=kind, title=title, action_class=action_class,
        rationale=rationale, expected=expected, risk_level=risk_level,
        requires_approval=requires_approval, blast_radius=blast_radius,
        action=str(action or "").strip() or str(title or "").strip(),
        mechanism=str(mechanism or "").strip(),
        evidence_refs=tuple(str(item) for item in evidence_refs if str(item).strip()),
        preconditions=tuple(str(item) for item in preconditions if str(item).strip()),
        rollback=rollback, steps=step_list)


def _agent_plan(*, option_id: str, action_class: ActionClass, title: str,
                rationale: str, expected: str, blast_radius: str,
                rollback: str, mentioned: str = "",
                steps: Sequence[str] = ()) -> RemediationPlan:
    """构造一条 `agent_action` 候选（风险由起点经升档算出）。"""
    target = EntityRef.make("service", "该服务", None)
    level, approval, why = _risk_for(action_class, mentioned=mentioned, target=target)
    return _make_plan(
        option_id=option_id, kind=PlanKind.AGENT_ACTION.value, title=title,
        action_class=action_class.value, rationale=rationale, expected=expected,
        risk_level=level, requires_approval=approval, blast_radius=blast_radius,
        rollback=rollback, steps=steps or PLAN_STEPS.get(action_class.value, ()),
        preconditions=(why,))


def _manual_plan(*, option_id: str, title: str, rationale: str, expected: str,
                 blast_radius: str, resource: str,
                 steps: Sequence[str] = ()) -> RemediationPlan:
    """构造一条 `manual` 候选（**没有** action_class —— 不假装能执行）。"""
    return _make_plan(
        option_id=option_id, kind=PlanKind.MANUAL.value, title=title, action_class="",
        rationale=rationale, expected=expected, risk_level=RiskLevel.L2.value,
        requires_approval=False, blast_radius=blast_radius,
        rollback="回滚方案：改动前留好版本；改完观察指标，未恢复则先还原改动再走回滚变更",
        steps=steps or _manual_steps(resource), preconditions=_MANUAL_PRECONDITIONS)


def _rationale_of(labels: str, words: frozenset[str], extra: str = "") -> str:
    """依据：**指向具体证据 / 假设**（有 id 写 id，没有写命中的信号名）。"""
    parts = [part for part in (labels, extra) if part]
    if not parts and words:
        parts.append("信号 " + "、".join(sorted(words)[:3]))
    return "依据：" + ("；".join(parts) if parts else "（本次没有可依据的证据，按常规处置顺序）")


def _manual_steps(target: str) -> tuple[str, ...]:
    """人工修那一类的**具体步骤**（第一条就是"确认它是不是真的不存在"）。

    ★ 步骤要能照着做：说"检查一下"等于没说 —— 第一句给出**查什么**（库表清单 / 迁移记录），
      第二句给出**改哪里**（迁移脚本，而不是线上手改）。
    """
    return (
        f"确认 {target} 是否真的不存在或写错了（查库表清单 / 迁移记录；报错原文见「依据」）",
        "补建缺失的表结构或改对那条查询（走迁移脚本，不在线上手改）",
        "在预发环境按同样路径验证",
        "灰度后再全量，观察 180 秒")

_MANUAL_PRECONDITIONS: Final[tuple[str, ...]] = (
    "需要代码/迁移变更权限（Agent 没有写通道，本方案必须由人工执行）",
    "先在预发验证，不在生产直接改")
_MANUAL_EXPECTED: Final[str] = "报错的那条查询不再失败，错误率回落到异常前水平"
_MANUAL_BLAST: Final[str] = "只影响报错的那条查询路径；需要发版或跑迁移，属人工操作"


def _troubleshoot_plan(option_id: str, mentioned: str) -> RemediationPlan:
    """**没有任何依据**时的如实建议：先补齐证据，再定处置（不猜原因、不编动作）。"""
    return _make_plan(
        option_id=option_id, kind=PlanKind.MANUAL.value,
        title="先补齐证据再定处置（当前没有可依据的证据）",
        action_class="",
        rationale="依据：本轮没有任何证据或假设（既没有确认过现象，也没有候选解释）",
        expected="补齐服务与现象后，可以给出**有依据的**处置方案，而不是猜一个动作",
        risk_level=RiskLevel.L0.value, requires_approval=False,
        blast_radius="只读排查，不改动任何线上状态",
        rollback="回滚方案：只读排查无需回滚",
        steps=("确认是哪个服务、什么现象、从什么时候开始",
               "取该服务的指标 / 日志 / 变更记录",
               "给出有依据的候选方案与处置流程"),
        preconditions=("需要用户补充目标服务与现象",))


# ======================================================================
#: 中文数字 → 序号（用户会说「第二个」）
_NUMBER_WORDS: Final[dict[str, int]] = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def select_plan(plans: Sequence[RemediationPlan], answer: str, *,
                by_title: bool = True) -> RemediationPlan | None:
    """把用户的下一轮输入归一化命中候选；**命中不了就返回 `None`**（不猜）。

    认四种说法：① 序号（「方案 2」/「第 2 个」/「2」）；② 动作名（回滚/重启/扩容/摘流）；
    ③ 方案标题里的关键词；④ 序号的中文写法（「第二个」）。

    Args:
        by_title: 允不允许第③路（标题关键词）。**本轮刚算出方案**时必须关掉它：
            用户这句话是**提问**（「怎么解决 X 的问题」），而方案标题里必然出现 X 的字面 ——
            拿标题去认"用户选了什么"会把提问误判成选择（真机实测：方案刚给出来就写成
            "你选的方案是…"，用户根本没看过方案表，序号/动作名那两路照旧可用）。
    """
    items = list(plans or ())
    text = _choice_text(answer)
    if not items or not text:
        return None
    by_number = _pick_by_number(items, text)
    if by_number is not None:
        return by_number
    by_action = _pick_by_action(items, text)
    if by_action is not None:
        return by_action
    return _pick_by_title(items, text) if by_title else None


def _pick_by_number(plans: Sequence[RemediationPlan], text: str) -> RemediationPlan | None:
    """按序号命中（「方案 2」/「第2个」/纯数字「2」/中文「第二个」）。"""
    number = _choice_index(text)
    if number is None:
        return None
    return next((plan for plan in plans if plan.option_id == str(number)), None)


def _looks_like_choice(text: str, match: re.Match[str]) -> bool:
    """像"在选方案"而不是句子里恰好有数字（「今天 25 度」不该命中）。"""
    if any(word in text for word in ("方案", "第", "选", "个", "条")):
        return True
    return text.strip() == match.group(1)


def _pick_by_action(plans: Sequence[RemediationPlan], text: str) -> RemediationPlan | None:
    """按动作名命中（用户说「回滚」「重启」也能选中对应方案）。"""
    lowered = text.lower()
    if re.search(r"给出|提供|生成|制定|怎么|如何|不要|不用|别|不回滚|不重启", text):
        return None
    for plan in plans:
        if plan.kind != PlanKind.AGENT_ACTION.value:
            continue
        for word in ACTION_WORDS.get(plan.action_class, ()):
            if word in lowered:
                return plan
    return None


def _pick_by_title(plans: Sequence[RemediationPlan], text: str) -> RemediationPlan | None:
    """只有完整标题或明确选择加完整标题才命中；泛化提问不算选择。"""
    text = _choice_text(text)
    for plan in plans:
        title = plan.title.strip()
        if title and (text == title or title in text and re.search(r"选|采用|按|就用", text)):
            return plan
    return None


# ======================================================================
#  三、产出形态：多方案出表 / 单方案出流程
# ======================================================================
def format_plans(plans: Sequence[RemediationPlan]
                 ) -> tuple[str, list[dict[str, str]]]:
    """`(details_kind, 明细行)` —— 多方案一行一个方案；单方案额外给一行「处置流程」。

    行结构固定 `{"label": ..., "value": ...}`；`details_kind` 只用既有的
    `table` / `json` / `none` 三种取值（**不新增契约字段**）。
    ★ 流程行（`A -> B -> C`）只对 `agent_action` 方案给：人工方案的步骤由人来走，
      不套用"动作流程"的说法（但也照样列出来，见方案行）。
    """
    items = [plan for plan in (plans or ()) if isinstance(plan, RemediationPlan)]
    if not items:
        return "none", []
    rows = [_plan_row(plan) for plan in items]
    if len(items) == 1:
        rows.append(_flow_row(items[0]))
    return "table", rows


def _plan_row(plan: RemediationPlan) -> dict[str, str]:
    """一行方案：标签含序号/标题/风险，值含**机制** | 依据 | 证据 | 预期 | 前置 | 回滚。

    ★★ 「机制」与「证据」必须排在最前面：用户第一眼要看到的是"这条方案为什么对症"
      （对上的是哪个原因）与"它引用的是这一轮查到的哪几条" —— 排在后面等于没有。
      ★ 没有机制/没有引用时**如实留空**（不写"机制：无"这种噪声，也不编一句顶上）。
    """
    kind = "需人工操作" if plan.is_manual else "Agent 可生成预案（本阶段仍不执行）"
    level = plan.risk_level + ("，需人工审批" if plan.requires_approval else "")
    parts = []
    if plan.action:
        parts.append(f"动作：{plan.action}")
    if plan.mechanism:
        parts.append(f"机制：{plan.mechanism}")
    parts.append(plan.rationale)
    if plan.evidence_refs:
        parts.append("引用证据：" + "、".join(plan.evidence_refs))
    parts.extend((
        f"预期：{plan.expected}",
        f"前置：{'；'.join(plan.preconditions) or '（无）'}",
        plan.rollback,
        f"影响面：{plan.blast_radius}",
        f"类型：{kind}",
    ))
    return {"label": f"方案 {plan.option_id} · {plan.title}（{level}）",
            "value": " ｜ ".join(parts)}


def _flow_row(plan: RemediationPlan) -> dict[str, str]:
    """单方案时的那一行处置流程（`A -> B -> C`）。"""
    return {"label": "处置流程", "value": " -> ".join(plan_steps_for(plan))}


def plan_steps_for(plan: RemediationPlan) -> tuple[str, ...]:
    """这条方案的处置步骤（agent_action 走模板，manual 用自己的）。"""
    return plan.steps or PLAN_STEPS.get(plan.action_class, ()) or (plan.title,)


def class_of_title(title: str) -> str:
    """从方案标题反查动作类别（台账里只存了标题时，用它把动作认回来）。

    ★ 复用 `ACTION_WORDS` 这一份"动作名 → 中文说法"的映射 —— 台账复原与
      用户选择归一化必须认**同一套词**，否则"用户选的方案"与"复原出来的动作"会分叉。
    """
    text = str(title or "")
    return next((action for action, words in ACTION_WORDS.items()
                 if any(word in text for word in words)), "")


def selected_from_flags(flags: Any) -> RemediationPlan | None:
    """从编排层留下的 flags 里取**用户已选中的**那条方案（没有就返回 `None`）。"""
    if not isinstance(flags, dict):
        return None
    selected = flags.get("selected_plan")
    return selected if isinstance(selected, RemediationPlan) else None


def plan_from_ledger(tasks: Sequence[Any], answer: str) -> RemediationPlan | None:
    """从**会话台账**里把用户选中的那条方案复原出来（`_select_plan` 的兜底）。

    ★ 为什么需要它：这一轮未必重算得出与上一轮**同一批**候选（证据/假设可能已经不在
      手上），而台账里存着上一轮真正给出过的方案序号与标题 —— 用户回「方案 2」时，
      按那里的序号复原才与用户看到的表格一致（不猜、也不改序号）。
    """
    for item in (tasks or ()):
        plans = _get(item, "plans", ())
        if not isinstance(plans, Sequence) or isinstance(plans, (str, bytes)):
            continue
        picked = select_plan(tuple(_plan_from_entry(entry) for entry in plans
                                   if isinstance(entry, Mapping)), answer)
        if picked is not None:
            return picked
    return None


def _plan_from_entry(entry: Mapping[str, Any]) -> RemediationPlan:
    """只复原真正展示过的候选，保留人工类型、步骤和前置条件。"""
    action = str(entry.get("action_class") or "")
    sequence = lambda key: tuple(str(x) for x in (entry.get(key) or ()) if str(x).strip())
    return RemediationPlan(
        option_id=str(entry.get("option_id") or ""),
        kind=str(entry.get("kind") or (PlanKind.AGENT_ACTION.value if action else PlanKind.MANUAL.value)),
        title=str(entry.get("title") or ""), action_class=action,
        rationale=str(entry.get("rationale") or "依据：上一轮给出的候选方案（见台账）"),
        expected=str(entry.get("expected") or ""), risk_level=str(entry.get("risk_level") or ""),
        requires_approval=bool(entry.get("requires_approval", False)),
        blast_radius=str(entry.get("blast_radius") or ""), action=str(entry.get("action") or ""),
        mechanism=str(entry.get("mechanism") or ""), evidence_refs=sequence("evidence_refs"),
        preconditions=sequence("preconditions"), rollback=str(entry.get("rollback") or ""),
        steps=sequence("steps") or PLAN_STEPS.get(action, ()))


def _choice_text(answer: str) -> str:
    """选择判定只看本轮用户原话，去掉 UI 时间窗和系统追加的接续上下文。"""
    text = str(answer or "")
    if "\n（补充：" in text:
        text = text.rsplit("\n（补充：", 1)[1].rsplit("；", 1)[0]
    text = text.split("\n（当前任务", 1)[0]
    return re.sub(r"[（(]时间窗\s+[^）)]*[）)]", "", text).strip()


def _choice_index(answer: str) -> int | None:
    """用户话里的**序号**（「方案 2」/「第2个」/「2」/「第二个」）—— 认不出返回 `None`。"""
    text = _choice_text(answer)
    if not text:
        return None
    match = re.search(r"(?:方案|选项|第)\s*([0-9一二两三四五六七八九]+)\s*(?:个|条|号)?", text)
    if match is None:
        match = re.fullmatch(r"\s*([0-9一二两三四五六七八九]+)\s*(?:个|条|号)?\s*", text)
    if match:
        word = match.group(1)
        return int(word) if word.isdigit() else _NUMBER_WORDS.get(word)
    return None


#: ★ `candidate_plans` 搬到了 `picks.py`（同一份实现，没有第二个副本）——
#:   放在**文件末尾**导入：`picks` 要用本模块的数据结构与构造器，
#:   在文件头部导入会形成"plans ↔ picks"的循环。
from .picks import candidate_plans  # noqa: E402,F401
