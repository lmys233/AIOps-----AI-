"""Critic Agent —— 反事实检验。

--------------------------------------------------------------------------------
★ Critic 干什么、**不**干什么
--------------------------------------------------------------------------------

**做**：

  · 对每条假设做**反事实检验**："如果它成立，我们必须看到什么？没看到 → 排除"
  · 用 `evidence_lookup` **受限反查**已有 `evidence_id`（证据是否真的存在、能不能读回）
  · 记录 `excluded_reason`（被否决必须给理由，否则无法复核）

**不做**：

  · ★★ **不提新假设** —— 一提就变成"第四个投票者"，多 Agent 的价值（互相证伪）
    当场消失
  · 不查原始遥测（那是取证层的职责）
  · 不改分数、不改支持/反对证据列表（那些是 RCA 的证据账）

--------------------------------------------------------------------------------
★ 反事实检验的三种判据（都写在这里，避免散落）
--------------------------------------------------------------------------------

  ① **必要条件缺失**：假设类别标了 `necessary` 的证据信号，一条都没有。
     "若成立则必须看到 X" → 没看到 X → 该假设不成立。
  ② **必要条件时序倒置**：必要证据存在，但**晚于**异常 onset（只对变更类）。
     因果方向反了：故障之后的变更不能解释故障。
  ③ **引用的证据读不回来**：`evidence_lookup` 查不到该 `evidence_id`。
     结论引用了**不存在的证据** —— 结论必须可追溯。

★ 判据③ 直接打掉"幻觉证据"：RCA 引用的每个 id 都必须能在黑板上读回。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional, Sequence

from dataclasses import dataclass

from ..common.contracts import Evidence, Hypothesis
from ..common.enums import EvidenceSource, HypothesisStatus, VerificationMethod
from .likelihoods import rules_of
from .signals import source_of

__all__ = ["CriticAgent", "CriticReport", "LookupFn", "LookupFailed"]


class LookupFailed(Exception):
    """反查**没查成**（工具不可用 / 权限 / 没带 incident_id）—— 与"黑板确实没有这条"分开。

    ★★ 以前 `_lookup` 把工具失败 `return None`，与"证据不存在"共用一个返回值，
      于是 Critic 把一次**瞬时工具故障**读成"引用了不存在的证据" → 排除该假设。
      工具层本来分得清这两件事（`TOOL_UNAVAILABLE` vs `data=[]`），
      是边界上把区分抹掉了。现在失败走异常通道，**不参与排除判定**。
    """

#: 反查函数：`evidence_id → Evidence | None`。
#: ★ 是**异步**的，因为它要真的走 `evidence_lookup` 工具（工具调用是 async）；
#:   没有执行器时注入一个基于本地证据的等价实现即可。
LookupFn = Callable[[str], Awaitable[Optional[Evidence]]]


@dataclass(frozen=True)
class CriticReport:
    """一次反事实检验的结果（**假设集合是输入的子集** —— 可断言）。"""

    hypotheses: tuple[Hypothesis, ...]
    checked: int
    excluded: tuple[tuple[str, str], ...] = ()       # (hypothesis_id, reason)
    looked_up: tuple[str, ...] = ()                  # 真正反查过的 evidence_id
    unreadable: tuple[str, ...] = ()                 # 读不回来的 id（判据③）
    notes: tuple[str, ...] = ()
    #: ★ 反查**没查成**的 id（工具失败）—— 它们既不进 `unreadable`、也不参与排除
    failed_lookups: tuple[str, ...] = ()
    #: ★ "没验证成"的假设（数据源缺失 / 反查失败）→ 交给仲裁，由它决定拒答
    unverified: tuple[tuple[str, str], ...] = ()
    #: ★★ 本次**关掉了**判据③（"引用的证据必须读得回来"）—— 只允许消融实验这么用：
    #: 它一关可追溯性就没了防线，所以仲裁层会**强制拒答**（
    #: "关掉它"等于声明"这是消融实验"，那就别给出结论）。
    readable_check_disabled: bool = False

    @property
    def alive(self) -> tuple[Hypothesis, ...]:
        return tuple(item for item in self.hypotheses
                     if item.status is not HypothesisStatus.EXCLUDED)

    def to_dict(self) -> dict[str, Any]:
        return {"checked": self.checked, "excluded": [list(item) for item in self.excluded],
                "looked_up": list(self.looked_up), "unreadable": list(self.unreadable),
                "failed_lookups": list(self.failed_lookups),
                VerificationMethod.UNVERIFIED.value: [list(item) for item in self.unverified],
                "readable_check_disabled": self.readable_check_disabled,
                "alive": [item.hypothesis_id for item in self.alive],
                "notes": list(self.notes)}


@dataclass
class CriticAgent:
    """只检验、不新增（返回的假设集合必须是输入的子集）。"""

    #: 反查函数（None → 用传进来的证据列表做本地反查，并记一条 note）
    lookup: Optional[LookupFn] = None
    #: 是否要求"支持证据可反查"（判据③）。关掉它只用于消融对比（`no_critic` 组）
    require_readable_evidence: bool = True

    # ------------------------------------------------------------------
    async def review(self, *, hypotheses: Sequence[Hypothesis],
                     evidence: Sequence[Evidence] = (),
                     anomaly_onset: Any = None,
                     degraded_signals: Sequence[str] = ()) -> CriticReport:
        """对每条假设做反事实检验，返回**同一批假设**（只改状态/理由）。

        `degraded_signals` 是这一轮**没查成**的数据源（`EvidenceSource` 的取值）。
        ★★ 没有它，"这一路没查成"就会被读成"信号不存在"，
          于是数据源一挂，**最该保留的那条假设反而被强反证排除**。
        """
        by_id = {item.evidence_id: item for item in evidence}

        async def _local_lookup(evidence_id: str) -> Optional[Evidence]:
            return by_id.get(evidence_id)

        lookup = self.lookup or _local_lookup
        notes: list[str] = []
        if self.lookup is None:
            notes.append("未接入 `evidence_lookup` 工具 → 用本次黑板证据做等价反查"
                         "（能力受限，结论等价）")
        if not self.require_readable_evidence:
            # ★★ 这个开关一关，可追溯判据就没有防线了。
            #   所以它不是"配置项"，而是"**声明这是消融实验**"——
            #   必须响亮地说出来，并且让仲裁层据此拒答（见 `readable_check_disabled`）。
            notes.append("★★ 本次**关闭了**「引用的证据必须读得回来」——"
                         "只允许消融实验这么用；该次运行的结论一律不成立（仲裁会强制拒答）")

        out: list[Hypothesis] = []
        excluded: list[tuple[str, str]] = []
        looked_up: list[str] = []
        unreadable: list[str] = []
        failed_lookups: list[str] = []
        unverified: list[tuple[str, str]] = []
        degraded = {str(item) for item in (degraded_signals or ())}
        for item in hypotheses:
            reason = await self._counterfactual(
                item, lookup=lookup, by_id=by_id, anomaly_onset=anomaly_onset,
                looked_up=looked_up, unreadable=unreadable,
                failed_lookups=failed_lookups, unverified=unverified,
                degraded_sources=degraded)
            if reason is None:
                out.append(item)
                continue
            # ★ 只改状态与理由：不动分数、不动证据账（Critic 不是投票者）
            out.append(item.model_copy(update={
                "status": HypothesisStatus.EXCLUDED, "excluded_reason": reason}))
            excluded.append((item.hypothesis_id, reason))
        return CriticReport(hypotheses=tuple(out), checked=len(hypotheses),
                            excluded=tuple(excluded), looked_up=tuple(looked_up),
                            unreadable=tuple(unreadable),
                            failed_lookups=tuple(dict.fromkeys(failed_lookups)),
                            unverified=tuple(unverified), notes=tuple(notes),
                            readable_check_disabled=not self.require_readable_evidence)

    # ------------------------------------------------------------------
    async def _counterfactual(self, hypothesis: Hypothesis, *, lookup: LookupFn,
                              by_id: dict[str, Evidence], anomaly_onset: Any,
                              looked_up: list[str], unreadable: list[str],
                              failed_lookups: list[str],
                              unverified: list[tuple[str, str]],
                              degraded_sources: set[str]) -> Optional[str]:
        """返回排除理由（`None` = 这条假设通过了反事实检验）。"""
        category = hypothesis.category

        # ---- 判据①：必要条件缺失（★ 但"这一路没查成"不算缺失）----
        necessary_signals = [rule.signal for rule in rules_of(category) if rule.necessary]
        present_signals = [str(item.signal or "") for item in by_id.values()]
        missing: list[str] = []
        for signal in necessary_signals:
            if any(signal in text for text in present_signals):
                continue
            source = source_of(signal)
            if source and source in degraded_sources:
                # ★★ 不是"没有这个信号"，而是"**这一路没查成**"——
                #   不得据此排除假设；把裁决权交给仲裁的拒答逻辑。
                unverified.append((hypothesis.hypothesis_id,
                                   f"未验证：{signal} 所在的数据源（{source}）本轮没查成"))
                continue
            missing.append(signal)
        if missing:
            return (f"反事实检验未通过：这类假设成立时**必须**看到 {missing}，"
                    f"而本次证据里没有 —— 必要条件缺失（强反证）；"
                    f"★ 注：这些信号的数据源本轮**查成了**，所以「没有」是真的没有")

        # ---- 判据②：必要条件的时序倒置（只对"变更类"有意义）----
        if category == "change_induced" and anomaly_onset is not None:
            changes = [item for item in by_id.values()
                       if item.source is EvidenceSource.CHANGES]
            if changes and all(item.onset > anomaly_onset for item in changes):
                return ("反事实检验未通过：变更**晚于**异常 onset "
                        f"（{changes[0].onset.isoformat()} > {anomaly_onset.isoformat()}）"
                        "—— 因果方向反了，故障之后的变更解释不了故障")

        # ---- 判据③：引用的证据读不回来（结论必须可追溯）----
        if self.require_readable_evidence:
            missing_ids: list[str] = []
            for evidence_id in list(hypothesis.supporting) + list(hypothesis.contradicting):
                looked_up.append(evidence_id)
                try:
                    found = await lookup(evidence_id)
                except LookupFailed as exc:
                    # ★ 反查**没查成** ≠ 证据不存在 → 不排除、只留痕
                    failed_lookups.append(evidence_id)
                    unverified.append((hypothesis.hypothesis_id,
                                       f"未验证：反查工具没查成（{exc}）"))
                    continue
                if found is None:
                    missing_ids.append(evidence_id)
                    unreadable.append(evidence_id)
            if missing_ids:
                return (f"反事实检验未通过：引用的证据 {missing_ids} 在黑板上**读不回来**"
                        f"—— 结论必须可追溯，引用不存在的证据不能成立")
        return None
