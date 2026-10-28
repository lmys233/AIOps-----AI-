"""候选方案的**生成**（从 `plans.py` 抽出来，判据一个字没改）。

★ 为什么单列：`plans.py` 管的是"一条方案长什么样"（数据结构 / 动作白名单 / 用户选择
  归一化），这里管的是"这一次该给哪几条"（按假设类别、证据信号、变更事件与现象词挑）。
  两者改动的理由完全不同（一个随契约变、一个随现场变）。
★ 方案**锚定**与"零证据"占位在 `gaps.py`；本模块只负责**有依据时**挑哪几条。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final, Sequence

from ..common.enums import ActionClass
from .plans import (PLAN_STEPS, RemediationPlan, _MANUAL_KEYS, _MANUAL_TEXT_KEYS,
                    _agent_plan, _blast_text, _categories_of, _has_concrete, _hits,
                    _keywords_of, _manual_plan, _manual_steps, _rationale_of, _resource,
                    _resource_tokens, _service_in, _text, _target_of,
                    _troubleshoot_plan)

__all__ = ["candidate_plans"]

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

#: 变更类信号（判"该回滚"）
_CHANGE_KEYS: Final[tuple[str, ...]] = ("change_event", "change_induced", "变更")
#: 拓扑传播类信号（判"该摘流"）
_SPREAD_KEYS: Final[tuple[str, ...]] = ("blast_radius", "dependency_graph", "topology")
#: 容量类信号
_CAPACITY_KEYS: Final[tuple[str, ...]] = ("capacity", "surge", "saturation", "qps")

#: "人工修"那条方案的**预期 / 影响面**（它治的是代码或库表，不是本阶段的写通道）
_MANUAL_EXPECTED: Final[str] = "报错的那条查询不再失败，错误率回落到异常前水平"
_MANUAL_BLAST: Final[str] = "只影响报错的那条查询路径；需要发版或跑迁移，属人工操作"

#: **表/对象不存在**这一类的明确痕迹（有它才算"该人工修"，不用再要求对象在证据里）
_SCHEMA_KEYS: Final[tuple[str, ...]] = ("does not exist", "undefinedtable", "不存在",
                                        "no such table", "relation")


def candidate_plans(*, message: str, hypotheses: Sequence[Any] = (),
                    evidence: Sequence[Any] = (), changes: Sequence[Any] = (),
                    blast_radius: Any = None, limit: int = 3,
                    services: Sequence[Any] = (),
                    deployment_form: str = "",
                    evidence_source: str = "") -> tuple[RemediationPlan, ...]:
    """按「假设类别 + 证据信号 + 变更事件 + 爆炸半径」生成候选方案（最多 `limit` 个）。

    Args:
        services: 这句话所在的**目标服务**（槽位里已确认的那一份）。
            ★ 一句话里点不出对象时，靠它把方案锚到**真实服务**上
              （否则只能给"先补齐证据"那条 —— 用户明明说过是 inventory）。
        evidence_source: 这批证据**是哪一轮的**（本轮 / 上一轮（时刻））。
            ★ 非空时写进每条方案的「依据」——用户要能一眼看出这是不是刚查的。

    ★ 构造期自检（`_make_plan`）：`agent_action` 的 `action_class` 必须在白名单里，
      且 `DEFAULT_ACTION_LEVEL != "L4"` —— **L4 一律不出现在候选里**（直接抛错，
      不是静默过滤：能走到这条说明映射写错了，那是设计缺陷不是数据问题）。
    ★ 认不出任何依据时**不编动作**：给一条"先补齐证据"的如实建议（见 `_troubleshoot_plan`）。
    """
    text = str(message or "")
    words, labels = _keywords_of((*evidence, *hypotheses))
    if str(evidence_source or "").strip():
        labels = "；".join(part for part in (f"证据出处：{evidence_source}", labels) if part)
    slot_service = _service_in(text, services) or (
        str(services[0]) if len(tuple(services)) == 1 else "")
    objects, _ = _resource_tokens((text, *evidence, *hypotheses))
    resource = _resource(text, *(list(evidence) + list(hypotheses))) or slot_service
    # ★★ **动作目标 ≠ 证据里的"对象"**：`restart_pod` / `drain_instance` 作用在**服务实体**上；
    #   而 `inventory_stock` 是证据里点出的**资源**（表名），它只属于"人工修"那条方案的步骤。
    #   混用会把方案写成"动作目标 inventory_stock"（**当前不执行任何变更**，但会误导人；
    #   一旦接上写通道，就是"对着一张表去重启 Pod"）。
    action_target = slot_service or _target_of(hypotheses) or "该服务"
    picks = _candidate_picks(_PicksContext(
        text=text, words=words, labels=labels, resource=resource, objects=tuple(objects),
        categories=_categories_of((*evidence, *hypotheses)),
        snippet=_snippet_of(evidence), deployment_form=str(deployment_form or ""),
        blast=_blast_text(blast_radius, action_target),
        concrete=_has_concrete(text, evidence, hypotheses, resource or action_target),
        changed_count=len(tuple(changes)), has_evidence=bool(evidence or hypotheses)))
    if not picks:
        return (_troubleshoot_plan("1", text),) if limit > 0 else ()
    ordered = [plan for _rank, plan in sorted(picks, key=lambda item: item[0])]
    return tuple(
        _renumber(plan, index) for index, plan in enumerate(ordered[:max(0, limit)], start=1))


@dataclass(frozen=True)
class _PicksContext:
    """挑候选方案时**共用的事实**（一次算好，各条方案共用，避免各处重算）。"""

    text: str
    words: frozenset[str]
    labels: str
    resource: str
    blast: str
    concrete: bool
    changed_count: int
    has_evidence: bool
    #: ★ **假设类别**（不含信号名）——判"该人工修"只认它，见 `manual_issue`
    categories: frozenset[str] = frozenset()
    #: ★ **像对象名**的标识（含下划线 / 带点）——"人工修"那条方案要用它当靶子
    objects: tuple[str, ...] = ()
    #: ★ 证据里**最具体的那句原文**（报错原文；放进"人工修"那条方案的依据里）
    snippet: str = ""
    #: ★ **部署形态事实**（`host_process` / `k8s` / `unknown`）—— 机制名词由它决定
    deployment_form: str = ""

    @property
    def changed(self) -> bool:
        return self.changed_count > 0 or _hits(self.words, _CHANGE_KEYS)

    @property
    def haystack(self) -> str:
        """用户原话 + 证据里最具体的那句原文（判现象词的共用输入）。"""
        return f"{self.text} {self.snippet}".lower()

    @property
    def target_in_evidence(self) -> bool:
        """证据里是不是**明说了"某个对象不存在 / 写错了"**（判"该人工修"的硬条件）。

        ★ 为什么不能只看"有没有对象名"：真机的样本原文与查询标签里到处是
          `DB_POOL_SIZE`（故障注入的标签值）、`excluded_reason` 这种词，
          它们**不是**出错的对象 —— 拿它们当靶子写出的方案是**凭空**的
          （用户实测的靶子：问 order 线程池，答"确认 DB_POOL_SIZE 是否存在"）。
          所以判据是"**报错原文里点出了对象不存在**"这个机制，而不是词形。
        """
        return any(needle in self.haystack for needle in _SCHEMA_KEYS)

    @property
    def resource_issue(self) -> bool:
        """是不是**并发承载 / 资源争用**这一类（判"该扩容、该调池容量"）。

        ★ 三个地方都要看：证据/假设里的**信号名**、用户原话、证据**摘要原文**——
          真机上"线程池拒绝"是**错误类型**（写在摘要里），不是信号名，
          只看信号名会让这一类症状落进"没有候选"。
        """
        return (_hits(self.words, _POOL_KEYS)
                or any(word in self.haystack for word in _POOL_WORDS))

    @property
    def manual_issue(self) -> bool:
        """要不要给"人工修代码 / 迁移"那一条（**判据严**，见下）。

        ★★ 三个条件要同时成立：
          ① 有具体现象或目标（`concrete`）；
          ② 命中"代码 / 表结构"那类词（用户自己点了表/SQL/迁移，或证据里有
             `does not exist` 这种**对象不存在**的痕迹，或类别就是 `error_burst` 这类
             应用层错误）；
          ③ **症状指的不是"并发承载不够"** —— 线程池/连接池已满是资源问题，
             处置是扩容或重启，不是改 SQL。
        ★ 真机实测的教训：只看"有没有 `error_burst` / `log_error_burst`"就发这条方案，
          用户问「orders 工作线程池已满」也会收到"确认 DB_POOL_SIZE 是否存在、
          修对那条 SQL" —— 方案换了话题，比不说更糟。
        """
        if not self.concrete:
            return False
        if self.resource_issue:
            return False                      # ③ 并发承载这一类不归"人工修"管
        if any(needle in self.haystack for needle in _MANUAL_TEXT_KEYS):
            return True                       # 用户自己点了表 / 库 / 迁移
        return _hits(self.words, _MANUAL_KEYS) or _hits(self.categories, _MANUAL_KEYS)


def _candidate_picks(ctx: _PicksContext) -> list[tuple[int, RemediationPlan]]:
    """按「假设类别 + 信号 + 变更 + 爆炸半径」挑候选 → `[(推荐序, 方案)]`。

    ★ 推荐序越小越靠前：**先能治根的**（人工修/回滚变更）→ 再止血 → 最后对照手段。
    """
    picks: list[tuple[int, RemediationPlan]] = []
    if ctx.manual_issue:
        picks.append((0, _manual_pick(ctx)))
    if ctx.changed:
        picks.append((1, _changed_pick(ctx)))
    if _hits(ctx.words, _CAPACITY_KEYS) or ctx.resource_issue:
        picks.append((2, _scale_pick(ctx)))
    if _hits(ctx.words, _SPREAD_KEYS):
        picks.append((2, _drain_pick(ctx)))
    if ctx.concrete or ctx.has_evidence:
        picks.append((3, _restart_pick(ctx)))
    if not ctx.changed and ctx.concrete:
        # ★ 窗口内**没有**变更事件时也留一条"核对并回滚最近一次变更"：
        #   "最近改过什么"是处置的第一步，而"没查到变更"不等于"没有变更"
        #   （变更记录可能没采到），所以它排在最像的原因之后。
        picks.append((4, _rollback_check_plan(ctx)))
    return picks


def _manual_pick(ctx: _PicksContext) -> RemediationPlan:
    """"人工修代码/迁移"这一条（表结构 / SQL 这类 Agent 治不了的问题）。

    ★★ 方案要**具体到对象与证据原文**：只说"补表结构 / 改 SQL"等于给一句放之四海皆准的话
      （用户实测的原话："太笼统，任何错误都可以这样对付过去"）。所以：
      标题带上出错的对象名，步骤第一条就是"确认它是不是真的不存在"，依据里引**报错原文**。
    """
    target = ctx.resource or "报错涉及的对象"
    title = f"人工修复代码或迁移：确认 {target} 是否存在、修对那条 SQL"
    extra = f"对象 {target}"
    if ctx.snippet:
        extra = f"{extra}；证据原文：「{ctx.snippet}」"
    return _manual_plan(
        option_id="", title=title,
        rationale=_rationale_of(ctx.labels, ctx.words, extra=extra),
        expected=_MANUAL_EXPECTED, blast_radius=_MANUAL_BLAST, resource=ctx.resource,
        steps=_manual_steps(target))


def _changed_pick(ctx: _PicksContext) -> RemediationPlan:
    """窗口内有变更事件 → 首选回滚那次变更。"""
    extra = (f"窗口内有 {ctx.changed_count} 条变更事件" if ctx.changed_count
             else "证据指向变更引入")
    return _agent_plan(
        option_id="", action_class=ActionClass.ROLLBACK_CONFIG,
        title="回滚窗口内的那次变更",
        rationale=_rationale_of(ctx.labels, ctx.words, extra=extra),
        expected="把配置/版本退回变更前状态，观察异常是否随之消失",
        blast_radius=ctx.blast, mentioned=ctx.text,
        rollback="回滚方案：回滚前先记录当前版本；失败则回到回滚前那一版并升级人工")


def _scale_pick(ctx: _PicksContext) -> RemediationPlan:
    """连接池/容量饱和 → 扩容（L3，需人工审批）。"""
    return _agent_plan(
        option_id="", action_class=ActionClass.SCALE,
        title="扩容副本（提高并发承载能力）",
        rationale=_rationale_of(ctx.labels, ctx.words, extra="证据指向连接池/容量饱和"),
        expected="并发承载能力上升，等待时间回落；★ L3 需人工审批后才可能落地",
        blast_radius=ctx.blast, mentioned=ctx.text,
        rollback="回滚方案：把副本数调回变更前的值（验证窗口 180s 内未恢复即自动回滚）")


def _drain_pick(ctx: _PicksContext) -> RemediationPlan:
    """拓扑传播/上游受影响 → 先摘流止血。"""
    return _agent_plan(
        option_id="", action_class=ActionClass.DRAIN_INSTANCE,
        title="先摘流该实例（止血）",
        rationale=_rationale_of(ctx.labels, ctx.words, extra="证据指向拓扑传播/上游受影响"),
        expected="把异常实例从上游摘掉，先止住扩散，再定位原因",
        blast_radius=ctx.blast, mentioned=ctx.text,
        rollback="回滚方案：恢复摘除前先确认剩余实例有余量；异常扩大则恢复该实例")


def _snippet_of(items: Sequence[Any]) -> str:
    """从证据摘要里挑出**最具体的那句原文**（给方案当依据、也给用户看"我看到了什么"）。

    ★ 日志证据的摘要形如 `…；样本：GET /check 失败：数据库查询失败（UndefinedTableError:
      relation "inventory_stock" does not exist）` —— 真正有用的是 `样本：` 后面那段**报错原文**
      （写 Spring 后端时控制台也是这么给的：哪个对象、哪条语句）。挑最长的一条，按 240 字截断。
    """
    best = ""
    best_rank = (-1, -1)
    for item in items:
        text = _text(item, "summary").strip()
        if not text:
            continue
        marker = text.find("样本：")
        candidate = text[marker + len("样本："):].strip() if marker >= 0 else text
        rank = (int(_text(item, "signal") in ("log_error_burst", "trace_breakpoint")), len(candidate))
        if rank > best_rank:
            best = candidate
            best_rank = rank
    return best[:240]


def _restart_pick(ctx: _PicksContext) -> RemediationPlan:
    """重启（常规处置顺序里的对照手段，排在能治根的原因之后）。

    ★★ 机制名词**随部署形态的事实**走（用户 2026-10-03 裁决）：配了 k8s 才谈 Pod/副本；
      本项目是宿主进程 → 说"实例"；**没配置** → 中性措辞 + **明说**不知道，
      而不是靠一张"禁词表"（那只是把同一个错误换个词再犯一次）。
    """
    form = str(ctx.deployment_form or "")
    step_list = PLAN_STEPS.get(ActionClass.RESTART_POD.value, ())
    if form == "k8s":
        title, note, step_list = "重启该服务的 Pod", "部署形态按配置是 k8s（Pod/副本可用）", (
            "确认副本数与滚动策略", "逐个重启 Pod 并等待就绪",
            "观察指标 180 秒直至恢复", "未恢复则执行回滚")
    elif form == "host_process":
        title, note = "重启该服务的实例", "部署形态按配置是**宿主进程**（不是 k8s 工作负载）"
    else:
        title, note = "重启该服务的实例", (
            "**部署形态未配置**（`AIOPS_DEPLOYMENT_FORM` 未设）→ 只说「实例」这个中性说法，"
            "**不猜**是哪种工作负载；配上真实的部署形态后措辞会自动跟着走")
    extra = "按常规处置顺序作为对照方案"
    if _hits(ctx.words, _MANUAL_KEYS):
        extra = "按常规处置顺序作为对照方案；★ 证据指向代码/数据结构问题，**重启只能短暂缓解**"
    return _agent_plan(
        option_id="", action_class=ActionClass.RESTART_POD,
        title=title,
        rationale=_rationale_of(ctx.labels, ctx.words, extra=extra),
        expected=(f"清掉实例上的僵死状态（{note}）；若原因在代码/数据里，重启只能短暂缓解"),
        blast_radius=ctx.blast, mentioned=ctx.text,
        rollback="回滚方案：重启无状态变更，观察 180s；未恢复则由编排告警升级人工",
        steps=step_list)


def _rollback_check_plan(ctx: _PicksContext) -> RemediationPlan:
    """"核对并回滚最近一次变更"这一条（窗口内没有采到变更事件时的兜底核对）。"""
    return _agent_plan(
        option_id="", action_class=ActionClass.ROLLBACK_CONFIG,
        title="核对并回滚最近一次变更",
        rationale=_rationale_of(ctx.labels, ctx.words,
                                extra="本次没有查到变更事件，故放在最后核对"),
        expected="若异常与某次变更同时出现，回滚后应随之消失；★ 本阶段不执行",
        blast_radius=ctx.blast, mentioned=ctx.text,
        rollback="回滚方案：回滚前先记录当前版本；失败则回到回滚前那一版并升级人工")


def _renumber(plan: RemediationPlan, index: int) -> RemediationPlan:
    """给方案编上表格里的序号（用户会说"方案 2"）。"""
    return RemediationPlan(
        option_id=str(index), kind=plan.kind, title=plan.title,
        action_class=plan.action_class, rationale=plan.rationale,
        action=plan.action, mechanism=plan.mechanism, evidence_refs=plan.evidence_refs,
        expected=plan.expected, risk_level=plan.risk_level,
        requires_approval=plan.requires_approval, blast_radius=plan.blast_radius,
        preconditions=plan.preconditions, rollback=plan.rollback, steps=plan.steps)


# ======================================================================
