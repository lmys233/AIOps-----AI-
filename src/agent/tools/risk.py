"""风险分级与变更预案 —— L0~L4（产出 `RiskDecision` / `ProposedAction`）。

★ 这一层的职责边界：**只做判定与预案**，不执行任何变更。

1. 从用户原话里认出**想做什么**（`action_class`）与**对谁做**（`target`）；
2. 按 `DEFAULT_ACTION_LEVEL` 给出**起点等级**，再**只升不降**；
3. 生成 `ProposedAction`（含 `dry_run_diff` 与 `rollback_plan`）；
4. 由 `RiskDecision.requires_approval` 决定"要不要人批"。

★ 执行端（GitOps/Argo CD）**不在**这里 —— 审批通过之后只会得到
  "执行端未接入、没有应用任何变更"的**如实结论**，
  而不是一个"看起来执行了"的假成功。

★ 为什么动作识别用规则而不是模型：
  "把 order 重启一下"里的动作与目标，是**用户字面说了什么**
  （字面事实归正则，文本理解归模型）。而且这条路径直连写能力 ——
  让模型来决定 `action_class` 等于让它有机会把 L4 说成 L2。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final, Optional, Sequence

from ..common.contracts import EntityRef, ProposedAction, RiskDecision
from ..common.enums import ActionClass, DEFAULT_ACTION_LEVEL, RiskLevel

__all__ = ["classify_risk", "propose_action", "ACTION_KEYWORDS", "ActionMatch",
           "match_action", "ESCALATE_TO_L3", "FORBIDDEN_MENTIONS", "ProposalResult",
           "higher_risk", "RISK_RANK"]

#: 用户原话 → `action_class`（**只认字面**）。
#:
#: 三元组第 3 位 `front=True` = **前置词**：它必须比其它关键词先判定。
#: ★★ 以前"先长词后短词"只靠**书写次序**维持 —— 而 `跨命名空间` 含子串
#:   `命名空间`，一旦顺序被改动（或新增词）就会静默走错分支，且没有任何测试报警。
#:   现在把这件事变成**结构**：前置标记 + 文件末尾的遮蔽断言（谁都吃不掉谁）。
ACTION_KEYWORDS: Final[tuple[tuple[str, ActionClass, bool], ...]] = (
    # ---- 前置词：含其它关键词的子串，必须排在最前（顺序内部仍有讲究）----
    ("删命名空间", ActionClass.DELETE_NAMESPACE, True),
    ("跨命名空间", ActionClass.CROSS_NAMESPACE, True),
    ("删 pvc", ActionClass.DELETE_PVC, True),
    ("改 rbac", ActionClass.MODIFY_RBAC, True),
    ("资源 limit", ActionClass.PATCH_RESOURCE_LIMIT, True),
    ("网络策略", ActionClass.MODIFY_NETWORK_POLICY, True),
    ("控制面", ActionClass.CONTROL_PLANE, True),
    # ---- 普通词 ----
    ("命名空间", ActionClass.DELETE_NAMESPACE, False),
    ("删库", ActionClass.DELETE_PVC, False),
    ("pvc", ActionClass.DELETE_PVC, False),
    ("rbac", ActionClass.MODIFY_RBAC, False),
    ("扩容", ActionClass.SCALE, False),
    ("缩容", ActionClass.SCALE, False),
    ("改路由", ActionClass.PATCH_ROUTE, False),
    ("数据操作", ActionClass.DATA_OPERATION, False),
    ("重启", ActionClass.RESTART_POD, False),
    ("回滚", ActionClass.ROLLBACK_CONFIG, False),
    ("摘流", ActionClass.DRAIN_INSTANCE, False),
    ("摘除", ActionClass.DRAIN_INSTANCE, False),
    ("dry run", ActionClass.DRY_RUN, False),
    ("干跑", ActionClass.DRY_RUN, False),
)

#: 出现这些说法时，哪怕动作名没写全也要**按更谨慎的等级**处理（只升不降）
ESCALATE_TO_L3: Final[tuple[str, ...]] = ("立即", "马上", "立刻", "生产", "prod", "全量")

#: 必须**最先判定**的说法 —— ★ 由 `ACTION_KEYWORDS` 的 `front` 标记**派生**（同一份事实只写一次），
#: 不再手抄一份关键词清单（手抄的那份曾经既漏词、又带着"它等于 L4 集合"的错误理由：
#: 实际上 `跨命名空间` 是 **L3**，只因为它含 `命名空间` 子串才必须前置）。
#: 名字沿用 `FORBIDDEN_MENTIONS`，但语义已收窄为
#: "**前置判定**集合" —— 是不是禁止类由 `DEFAULT_ACTION_LEVEL` 决定，不是由这个集合决定。
FORBIDDEN_MENTIONS: Final[tuple[str, ...]] = tuple(
    keyword for keyword, _cls, front in ACTION_KEYWORDS if front)

#: 实际扫描顺序 = **前置词在前** + 其余词按书写次序。
#: ★ `match_action` 只认这一个顺序 —— 顺序是**数据**，不是散在函数体里的 if。
SCAN_ORDER: Final[tuple[tuple[str, ActionClass], ...]] = (
    tuple((keyword, cls) for keyword, cls, front in ACTION_KEYWORDS if front)
    + tuple((keyword, cls) for keyword, cls, front in ACTION_KEYWORDS if not front))


@dataclass(frozen=True)
class ActionMatch:
    """从一句话里认出来的动作（`None` = 没认出来）。"""

    action_class: Optional[ActionClass]
    keyword: str = ""
    target_name: str = ""

    @property
    def found(self) -> bool:
        return self.action_class is not None


#: 风险等级的**序**（L0 < L1 < L2 < L3 < L4）—— 取较大值时的唯一依据
RISK_RANK: Final[dict[str, int]] = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "L4": 4}


def higher_risk(*levels: str) -> str:
    """取**更严**的那一档；认不出来的等级按 **L3** 处理（不因为"看不懂"就放松）。

    ★★ `_effective_risk` 用它保证"**工具声明的下限不可被入参压低**"——
      以前只查 `DEFAULT_ACTION_LEVEL[action_class]`，于是给 `action_class="read"`
      就能让一个声明 L2 的写工具变成 L0、直接绕过审批闸门（等级只升不降）。
    """
    return max((str(item or "L3") for item in levels),
               key=lambda level: RISK_RANK.get(level, 3))


def match_action(message: str, *, service_names: Sequence[str] = ()) -> ActionMatch:
    """认动作 + 认目标（服务名来自意图层的**槽位清单**校验过的那一份）。

    ★ 只认 `SCAN_ORDER` 这一个顺序（前置词先行）—— 见 `ACTION_KEYWORDS` 的说明。
    """
    text = str(message or "").lower()
    for keyword, action_class in SCAN_ORDER:
        if keyword.lower() in text:
            target = next((name for name in service_names
                           if str(name).lower() in text), "")
            return ActionMatch(action_class=action_class, keyword=keyword,
                               target_name=str(target))
    return ActionMatch(action_class=None)


def _assert_no_shadowing() -> None:
    """import 期断言：**任何一个关键词都不会被更早的词吃掉**。

    判据：拿关键词自己当输入，第一个命中的必须是它自己。
    ★ 用 `assert` 之外的形式（`raise`），因为 `-O` 会剥掉 assert —— 这类"红线级"
      不变量不能只在开发环境成立（见 `likelihoods.py` 同一做法）。
    """
    for index, (keyword, action_class) in enumerate(SCAN_ORDER):
        winner = next((other for other, _cls in SCAN_ORDER
                       if other.lower() in keyword.lower()), None)
        if winner != keyword:
            raise AssertionError(
                f"动作关键词被遮蔽：{keyword!r}（第 {index + 1} 位）永远匹配不到 —— "
                f"更早的 {winner!r} 会先命中。请给 {keyword!r} 标 front=True 或调整顺序"
                f"（顺序必须由结构保证，不能靠书写次序巧合）")


_assert_no_shadowing()


def classify_risk(action: ProposedAction, *,
                  mentioned: str = "") -> RiskDecision:
    """给动作定级（**只升不降**）：写动作按高危处理，只读动作不设审批闸门。

    | 情形 | 结果 |
    |---|---|
    | `action_class` 的起点等级是 L4 | `L4` 直接拒绝（不因任何理由降级） |
    | 原话里出现"立即/马上/生产/全量" | **升到 L3**（要求人工审批） |
    | L3 起点 | L3（强制人工审批） |
    | L2 起点 | L2（自动 + 事后审计 + 通知） |
    | L0/L1 | 自动 |

    ★ 分级判据是"这条动作会不会改变线上状态"：只读动作撤销代价为零，所以自动放行；
      写动作的起点等级由 `DEFAULT_ACTION_LEVEL` 决定，只升不降，绝不能因入参变松。
    """
    base = action.default_level
    reasons = [f"`{action.action_class.value}` 的起点等级是 {base}"
               f"（DEFAULT_ACTION_LEVEL）"]
    level = base
    if base == RiskLevel.L4.value:
        return RiskDecision(level=RiskLevel.L4, reasons=reasons + [
            "L4 是**禁止类**：Agent 一律不得执行，只记录审计"],
            requires_approval=False, auto_rollback_window_sec=0)
    text = str(mentioned or "").lower()
    hits = [word for word in ESCALATE_TO_L3 if word.lower() in text]
    if hits and base in (RiskLevel.L0.value, RiskLevel.L1.value, RiskLevel.L2.value):
        level = RiskLevel.L3.value
        reasons.append(f"原话里出现 {hits} → **升到 L3**（紧急/生产影响面，宁严不宽）")
    elif base == RiskLevel.L3.value:
        reasons.append("L3 属**高危**：强制人工审批")
    return RiskDecision(
        level=RiskLevel(level), reasons=reasons,
        requires_approval=level in (RiskLevel.L3.value, RiskLevel.L4.value),
        auto_rollback_window_sec=180)


@dataclass(frozen=True)
class ProposalResult:
    """预案生成的结果：**要么有预案，要么如实说明为什么没有**。

    ★★ 以前 `propose_action` 返回 `Optional[ProposedAction]`，
      于是"没认出动作"和"**认出了动作但没认出对谁做**"共用 `None` ——
      而 `_target_entity` 为了不返回 `None`，会在用户没点名时取 `service_names[0]`
      （观测清单里的第一个服务），生成"重启 gateway"这种**用户没说**的预案。
      现在两种情况分开，且**一个都不猜**。
    """

    action: Optional[ProposedAction] = None
    blocked_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.action is not None


def propose_action(match: ActionMatch, *, slots: Any) -> ProposalResult:
    """把认出来的动作变成 `ProposedAction`（含 diff 预览与回滚方案）。

    ★ 目标只能来自**用户说出来的服务名**或**槽位里已确认的 `service`**；
      两者都没有 → 不提案（`blocked_reason` 写明"需要确认对谁做"）。
    """
    if not match.found:
        return ProposalResult(blocked_reason="没认出具体动作")
    target = _target_entity(match.target_name, slots)
    if target is None:
        return ProposalResult(blocked_reason=(
            f"认出了动作「{match.action_class.value}」但**没认出对谁做**"
            f"（用户没点名服务，槽位里也没有已确认的目标）→ "
            f"不生成预案，先确认目标（不猜服务名）"))
    params: dict[str, Any] = {}
    if match.action_class is ActionClass.SCALE:
        params["replicas"] = _scale_replicas(str(getattr(slots, "symptom", "") or ""))
    return ProposalResult(action=ProposedAction(
        action_class=match.action_class,
        target=target,
        params=params,
        dry_run_diff=_diff_preview(match, target),
        rollback_plan=_rollback_plan(match),
    ))


def _target_entity(name: str, slots: Any) -> Optional[EntityRef]:
    """动作目标：**只认**显式点名的服务，或槽位里已确认的 `service`。

    ★★ 这里以前是 `chosen = name or (names[0] if names else "")` ——
      `names[0]` 是**观测清单里的第一个服务**，不是用户想动的那个。用户说"重启一下"
      （没点名）会得到"重启 gateway"的预案。这与本函数的兜底注释（"不猜一个服务名"）
      自相矛盾：那条路只在 `service_names` 也为空时才走到。
    ★ 也**不再**产出 `svc:unknown` 占位实体：占位值会让"未知"和"真叫 unknown 的服务"
      再也分不开（`contracts.py` 对占位集群名就是这条理由禁掉的）。
    """
    service = getattr(slots, "service", None)
    if service is not None and (not name or service.name == name):
        return service
    if name:
        return EntityRef.make("service", name, getattr(slots, "cluster", None))
    return None


def _scale_replicas(text: str) -> int:
    matched = re.search(r"(\d+)\s*(个)?\s*(副本|实例|replica)", text or "")
    return int(matched.group(1)) if matched else 0


def _diff_preview(match: ActionMatch, target: EntityRef) -> str:
    verb = {"restart_pod": "重启服务实例", "rollback_config": "回滚配置",
            "scale": "调整副本数", "drain_instance": "摘流",
            "patch_route": "改路由", "patch_resource_limit": "改资源 limit",
            "cross_namespace": "跨命名空间操作", "data_operation": "数据操作",
            "delete_namespace": "删除命名空间", "modify_rbac": "修改 RBAC",
            "modify_network_policy": "修改网络策略", "control_plane": "操作控制面",
            "delete_pvc": "删除 PVC"}.get(match.action_class.value if match.action_class
                                          else "", "变更")
    return (f"（dry-run 预览，**未执行**）对 {target.id} 执行「{verb}」；"
            f"命中关键词 {match.keyword!r}。"
            f"★ 实际 diff 需要执行端（GitOps）生成 —— 本阶段不假装有 diff")


def _rollback_plan(match: ActionMatch) -> str:
    if match.action_class is ActionClass.RESTART_POD:
        return "回滚方案：重启无状态变更，观察 180s；未恢复则由编排告警升级人工"
    if match.action_class is ActionClass.ROLLBACK_CONFIG:
        return "回滚方案：回滚前先记录当前版本；失败则回滚到回滚前那一版并升级人工"
    if match.action_class is ActionClass.SCALE:
        return "回滚方案：把副本数调回变更前的值（验证窗口 180s 内未恢复即自动回滚）"
    return ("回滚方案：**未定义** —— 这类动作没有可自动回滚的路径，"
            "因此必须人工审批且人工准备回滚（变更 5 步）")
