"""结论裁决：**模型只在候选集内排序/措辞**，代码结论是地板。

--------------------------------------------------------------------------------
★ 口径不许颠倒
--------------------------------------------------------------------------------

```
候选集 + trace JSON + 相关事实  →  一次模型调用  →  后置校验  →  结论（1~N 条）
                                      ↓ 越界 / 失败
                                 弃用它的选择，退回**代码的确定性地板**
```

  · **地板**（`error_facts.trace_floor`）：最深的 `intrinsic` 节点，纯事实、可复现；
  · **模型**：在候选里挑 1~3 条并给出措辞。它**增强**地板，不替代地板。

★★ 后置校验是**硬判据**，不是"尽量检查"：模型输出里提到的服务 / 节点 /
  `fingerprint` 必须落在**候选集与 trace JSON 之内**。越界即弃用 ——
  幻觉出来的服务名一旦进了结论，报告就成了一份**看起来很像**的假结论。

★ 为什么允许"多条结论"：一次请求可能有多个原始出错位置（多分支），
  硬挑一个会把并列的两个真实位置丢掉。
"""

from __future__ import annotations

from typing import Any, Final, Mapping, Optional, Sequence

from pydantic import BaseModel, Field

from ..common.contracts import EntityRef, EntityType
from .error_facts import fingerprint_of, service_mention_terms
from .rca import statement_for

__all__ = [
    "MAX_VERDICTS",
    "SELECTION_ROLE",
    "SELECTION_MIN_TOKENS",
    "PROMPT_TRACE_NODES",
    "ConclusionDraft",
    "SelectionDraft",
    "OutOfSet",
    "in_set",
    "selection_prompt",
    "selectable_blocks",
    "validate_selection",
    "merge_selection",
    "subject_of",
    "align_root_cause",
]

#: 最多给几条结论（"多分支并列"是允许的，但也不能变成一堵墙）。
MAX_VERDICTS: Final[int] = 3

#: 这次模型调用的角色名（进记账/审计 —— `cost` 里能看出"裁决花了一次模型"）。
SELECTION_ROLE: Final[str] = "root_cause_selection"

#: 这一次调用的**输出预算**（token）。★ 不能给太小：思考型模型会先花 reasoning
#: tokens 再产出内容，预算被吃光时 content 为空 → 结构校验失败 → 白花一次调用。
SELECTION_MIN_TOKENS: Final[int] = 4096

#: 提示词里 trace JSON 的节点上限（**比报告侧更小**：提示词预算要留给事实与候选）。
PROMPT_TRACE_NODES: Final[int] = 24


class ConclusionDraft(BaseModel):
    """模型的一次选择（**只有措辞与"挑哪条"，没有新事实**）。"""

    service: str = Field(default="", description="出错的服务名（只能从候选清单里选）")
    operation: str = Field(default="", description="出错的节点名/语句（候选清单里的原样值）")
    fingerprint: str = Field(default="", description="候选清单里的指纹（有就照抄）")
    reason: str = Field(default="", description="为什么是它（一句话，只依据给定事实）")
    wording: str = Field(default="", description="给用户看的一句话结论（白话，不带内部术语）")
    propagated: bool = Field(
        default=False, description="这条是不是「替下游报错」而非自己出错")


class SelectionDraft(BaseModel):
    """模型输出的整体形状（1~3 条并列结论）。"""

    conclusions: list[ConclusionDraft] = Field(
        default_factory=list, description="按可信度从高到低排的 1~3 条结论")
    other_services: list[str] = Field(
        default_factory=list, description="只作为**传播链上的一环**提到的服务名")
    note: str = Field(default="", description="这次判断的依据（一句话）")


class OutOfSet(dict):
    """越界留痕（`dict` 子类：可直接进 `degraded` 与响应体）。"""

    @property
    def values(self) -> list[str]:  # type: ignore[override]
        return [str(item) for item in self.get("items") or []]


def in_set(service: str, allowed: Sequence[str]) -> bool:
    """这个服务名在"允许提到"的集合里吗（**大小写不敏感的子串匹配**）。

    ★ 为什么容得下子串：模型常把 `order` 写成 `order 服务`、`order-service`
      （那不是幻觉，只是措辞）。真正的幻觉是**全链路里根本没出现过的名字**。
    """
    clean = str(service or "").strip().lower()
    if not clean:
        return False
    for name in allowed:
        known = str(name or "").strip().lower()
        if known and (known == clean or known in clean or clean in known):
            return True
    return False


def selectable_blocks(grounding: Mapping[str, Any]) -> dict[str, Any]:
    """从裁决上下文里取出**可选的东西**（提示词与后置校验**同一份**）。

    ★ 同一份是关键：提示词给的清单与校验用的白名单若有差异，
      就会出现"模型选了清单里的、却被判越界"这种自相矛盾的失败。

    ★ `fingerprints` 是**候选的指纹集合**；模型抄错一个字符就落不进这里。
    """
    candidates = [dict(item) for item in grounding.get("candidates") or []]
    traces = [dict(item) for item in grounding.get("traces") or []]
    return {
        "candidates": candidates,
        "traces": traces,
        "services": sorted(service_mention_terms(candidates, traces)),
        "fingerprints": {fingerprint_of(item) for item in candidates
                         if fingerprint_of(item)},
    }


def same_service(left: str, right: str) -> bool:
    """两个服务名是不是**同一个**（大小写与首尾空白不敏感，其余**逐字**比）。

    ★ 为什么不能像"幻觉筛查"那样用子串匹配：`in_set` 容得下"order 服务"这种措辞，
      但**选中的节点**必须逐字对得上候选 —— 用子串匹配时
      `order/GET`（一条替下游报错的 client span）会被判成"和 `order/SELECT`
      是同一个服务"从而**通过校验**，调用方又被扣成根因。
    """
    return str(left or "").strip().lower() == str(right or "").strip().lower()


def _candidate_line(item: Mapping[str, Any]) -> str:
    marks = [name for name in ("intrinsic", "propagated", "slow") if item.get(name)]
    downstream = str(item.get("downstream") or "")
    if downstream:
        marks.append(f"被调方={downstream}")
    return (f"- [{item.get('fingerprint') or '（无指纹）'}] {item.get('service')}"
            f" / {item.get('operation') or '（未命名）'}｜错误类型 "
            f"{item.get('error_type') or '（未给出）'}｜次数 {item.get('count', 1)}"
            f"｜首现 {item.get('first_seen') or '（未知）'}｜标记 {'、'.join(marks) or '（无）'}"
            f"｜trace {item.get('trace_id') or '（未给出）'}")


def _trace_line(tree: Mapping[str, Any]) -> str:
    rows = [f"- trace {tree.get('trace_id') or '（未命名）'}（{tree.get('node_count')} 个节点"
            f"{'，已截断' if tree.get('truncated') else ''}）："]
    for node in (tree.get("nodes") or [])[:PROMPT_TRACE_NODES]:
        rows.append(f"    depth={node.get('depth')} {node.get('service')}"
                    f"/{node.get('operation')}｜kind={node.get('kind')}"
                    f"｜status={node.get('status')}｜{node.get('duration_ms')}ms"
                    + (f"｜错误文本 {node.get('error_text')}" if node.get("error_text") else ""))
    return "\n".join(rows)


_PROMPT_HEAD: Final[str] = (
    "你是一名 SRE 助手。下面给出：① **由代码枚举好的错误候选**（每条带标记）；"
    "② 相关链路的 span 树；③ 相关事实与历史记忆摘要。\n"
    "请从候选中挑出造成这次故障的 **1~3 条**结论，并给出**给用户看的白话措辞**。\n"
    "\n"
    "硬性要求：\n"
    "1. ★★ **只能从候选清单里选**：`service` 与 `operation` 必须逐字来自候选，\n"
    "   `fingerprint` 必须照抄候选里那个十六进制串；**不许**提任何清单外的服务名；\n"
    "2. `intrinsic` 标记的意思是「**它自己炸了**」（代码异常 / DB 查询抛错）；\n"
    "   `propagated` 的意思是「**它只是替下游喊了一声**」—— 后者不许当根因，\n"
    "   除非清单里没有任何 intrinsic 候选（那时必须写清「被调方 = X」）；\n"
    "3. `slow` 是「没报错但耗时异常、且链路上有超时/排队迹象」—— 它只是嫌疑人；\n"
    "4. 并列的真实位置可以给多条（最多 3 条），**不要**为了好看硬凑一条；\n"
    "5. `reason` 只写依据（必须是上面给出的内容），`wording` 用白话、不带内部术语\n"
    "   （不出现判据/字段名/英文状态值/工具名），并且要把**服务名、错误类型、\n"
    "   具体位置（表名/接口/语句）**写进去；\n"
    "6. 候选一条都解释不了时 → `conclusions` 留空，并在 `note` 里写清**缺什么**。\n"
)


def selection_prompt(grounding: Mapping[str, Any]) -> str:
    """拼出那**一次**裁决调用的提示词（候选 + trace JSON + 事实 + 记忆摘要）。"""
    blocks = selectable_blocks(grounding)
    floor = grounding.get("floor") or {}
    picked = [item for item in (floor.get("picked") or [])]
    parts = [
        _PROMPT_HEAD,
        "## 错误候选（**只能从这里选**）\n"
        + ("\n".join(_candidate_line(item) for item in blocks["candidates"])
           or "（这一轮没有枚举出任何候选）"),
        "## 相关链路\n"
        + ("\n".join(_trace_line(tree) for tree in blocks["traces"][:2])
           or "（这一轮没有取到 span 树）"),
        "## 代码给出的确定性地板（**选不出更好的时候就是它**）\n"
        + ("\n".join(f"- {item.get('service')} / {item.get('operation')}"
                     f"（{floor.get('kind') or '无'}）" for item in picked)
           or "（没有地板 —— 这一轮没有错误候选）"),
    ]
    facts = [str(line) for line in grounding.get("facts") or []]
    if facts:
        parts.append("## 相关事实\n" + "\n".join(f"- {line}" for line in facts[:12]))
    memories = [str(line) for line in grounding.get("memories") or []]
    if memories:
        parts.append("## 历史记忆摘要（只作参考，**不许**当成本轮证据）\n"
                     + "\n".join(f"- {line}" for line in memories[:4]))
    gaps = [str(line) for line in grounding.get("gaps") or []]
    if gaps:
        parts.append("## 这一轮没查成的信号（**不等于没有异常**）\n"
                     + "\n".join(f"- {line}" for line in gaps[:6]))
    return "\n\n".join(parts) + "\n"


def validate_selection(draft: Any, grounding: Mapping[str, Any]) -> dict[str, Any]:
    """**后置校验**：模型的选择必须落在候选集与 trace JSON 之内。

    Returns:
        `{"ok": bool, "verdicts": [...], "out_of_set": OutOfSet}`。

        `ok=False` 时调用方**必须**弃用 `verdicts`、退回 `floor`，
        并把 `out_of_set` 写进 `degraded`（留痕口径：`kind="out_of_set"`）。
    """
    blocks = selectable_blocks(grounding)
    allowed = blocks["services"]
    out: list[dict[str, Any]] = []
    offenders: list[str] = []
    intrinsic_exists = _has_intrinsic(grounding)
    for item in list(getattr(draft, "conclusions", None) or [])[:MAX_VERDICTS]:
        service = str(getattr(item, "service", "") or "").strip()
        operation = str(getattr(item, "operation", "") or "").strip()
        fingerprint = str(getattr(item, "fingerprint", "") or "").strip()
        if not in_set(service, allowed):
            offenders.append(f"服务 {service!r}")
            continue
        known = _match_candidate(blocks["candidates"], service, operation, fingerprint)
        if known is None:
            offenders.append(f"位置 {service}/{operation or '（未命名）'}")
            continue
        if intrinsic_exists and not known.get("intrinsic"):
            # ★★ 有 intrinsic 候选时**不许**拿 propagated 当根因 —— 与提示词里的
            #   要求是**同一条**判据（提示词写了、代码也要拦，否则只是"建议"）。
            offenders.append(f"替下游报错的位置 {service}/{operation or '（未命名）'}"
                             f"（这一轮有自身出错的候选，它不能当根因）")
            continue
        out.append(_verdict_of(known, item))
    offenders.extend(_stray_services(draft, allowed))
    return {
        "ok": bool(out) and not offenders,
        "verdicts": out,
        "out_of_set": OutOfSet({
            "kind": "out_of_set",
            "items": offenders[:6],
            "note": ("模型提到的服务/节点/fingerprint 不在候选集与链路里 → "
                     "**弃用它的选择**，退回代码的确定性地板"),
        }),
    }


def _has_intrinsic(grounding: Mapping[str, Any]) -> bool:
    """这一轮有没有"自己炸"的候选（有的话它才是根因，propagated 只是传话）。"""
    return any(bool(item.get("intrinsic"))
               for item in (grounding.get("candidates") or []))


def _verdict_of(known: Mapping[str, Any], picked: Any) -> dict[str, Any]:
    """候选 + 模型的措辞 → 一条结论（**事实全部取自候选**，措辞才来自模型）。"""
    return {
        "service": known["service"],
        "operation": known["operation"],
        "error_type": known["error_type"],
        "fingerprint": fingerprint_of(known),
        "count": known.get("count", 1),
        "first_seen": known.get("first_seen") or "",
        "propagated": bool(known.get("propagated")),
        "intrinsic": bool(known.get("intrinsic")),
        "slow": bool(known.get("slow")),
        "downstream": known.get("downstream") or "",
        "reason": str(getattr(picked, "reason", "") or "")[:300],
        "wording": str(getattr(picked, "wording", "") or "")[:300],
    }


def _stray_services(draft: Any, allowed: Sequence[str]) -> list[str]:
    """模型在"旁证服务"里提到的越界名字（同样算幻觉）。"""
    return [f"旁证服务 {name!r}"
            for name in list(getattr(draft, "other_services", None) or [])
            if not in_set(str(name), allowed)]


def _match_candidate(candidates: Sequence[Mapping[str, Any]], service: str,
                     operation: str, fingerprint: str) -> dict[str, Any] | None:
    """在候选里按 **指纹 → (服务, 位置)** 两级匹配（**只认候选里有的**）。

    ★ 为什么**没有**"只按服务名兜底"那一级：那会让"模型挑了同一个服务里
      **另一个**节点"也通过校验 —— 真机实测的形态是模型把 `order/GET`
      （一条**替下游报错**的 client span）挑出来当根因，而真正炸的是
      `inventory/SELECT`。服务名兜底会把它判成"合法选择"，
      于是调用方又被扣成根因 —— 这正是整个设计要挡住的那件事。
    """
    clean = str(fingerprint or "").strip().lower()
    if clean:
        for item in candidates:
            if fingerprint_of(item).lower() == clean:
                return dict(item)
    for item in candidates:
        if (same_service(str(item.get("service") or ""), service)
                and str(item.get("operation") or "").strip() == operation):
            return dict(item)
    return None


def merge_selection(grounding: Mapping[str, Any], draft: Any = None) -> dict[str, Any]:
    """把"模型的选择"与"代码的地板"合成本轮结论（**模型是增强，地板是底**）。

    Returns:
        `{"verdicts", "subject", "source", "floor", "degraded", "out_of_set"}`。

        · 校验通过 → `source="model"`，用模型挑的那几条（**都是候选里的**）；
        · 越界 / 空 / 抛错 → `source` 回到 `floor` 的判据名，并把越界写进 `degraded`。

    ★★ `subject` 是**服务维度的唯一事实来源**：`root_cause` 的实体（以及叙事主语）
      都从它取。它按"模型给老师的次序"（模型在候选内选中的第一条、`intrinsic` 优先）
      或"地板"（模型没得用时）确定 —— 与 `verdicts` 的**展示次序**（按服务名重排）
      是两件事，所以显式记下来，而不是让下游去猜 `verdicts[0]`。
    """
    floor = dict(grounding.get("floor") or {})
    check = validate_selection(draft, grounding) if draft is not None else {
        "ok": False, "verdicts": [],
        "out_of_set": OutOfSet({"kind": "no_selection", "items": [],
                                "note": "模型没有给出选择 → 直接用代码的地板"})}
    if check["ok"]:
        return {"verdicts": _order(check["verdicts"]), "source": "model",
                "subject": _subject_of_verdicts(check["verdicts"]),
                "floor": floor, "out_of_set": None, "degraded": []}
    fallback = _floor_verdicts(floor)
    degraded = _fallback_notes(floor, check, draft)
    return {"verdicts": fallback, "subject": (dict(fallback[0]) if fallback else None),
            "source": str(floor.get("kind") or "floor"),
            "floor": floor, "out_of_set": check["out_of_set"] or None,
            "degraded": degraded}


def subject_of(decision: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """候选内裁决选出的**主语**（服务维度的唯一事实来源；没有就是 `None`）。"""
    found = (decision or {}).get("subject") if isinstance(decision, Mapping) else None
    return dict(found) if isinstance(found, Mapping) else None


def _subject_of_verdicts(verdicts: Sequence[Mapping[str, Any]]
                         ) -> Optional[dict[str, Any]]:
    """在一批**已经通过校验**的结论里挑主语：**自己炸的优先**，全都是传播时才用它。

    ★ "`propagated` 不许单独成为主语"这条在这里落地：只要有一条不是"替下游报错"，
      主语就是它；只有当这一轮**根本没有**自身出错的节点时，才把传播位置当主语
      （那时它会被标注成"传播/影响面 + 被调方"，见 :func:`align_root_cause`）。
    """
    items = [dict(item) for item in verdicts]
    committed = [item for item in items if not item.get("propagated")]
    chosen = committed or items
    return chosen[0] if chosen else None


def _support_services(evidence_ids: Sequence[str], evidence: Sequence[Any]) -> set[str]:
    """这批支持证据分别**属于哪些服务**（用来核对"类别 × 实体"是否同源）。"""
    wanted = {str(item) for item in evidence_ids}
    out: set[str] = set()
    for item in evidence or ():
        if str(getattr(item, "evidence_id", "")) not in wanted:
            continue
        name = str(getattr(getattr(item, "entity", None), "name", "") or "")
        if name:
            out.add(name)
    return out


def _propagation_note(subject: Mapping[str, Any]) -> str:
    """传播/影响面的标注（**只能来自事实**：被调方拿不到就如实写"取不到"）。"""
    downstream = str(subject.get("downstream") or "").strip()
    where = (f"被调方 = {downstream}（来自拓扑/子 span）" if downstream
             else "被调方**取不到**（trace 与拓扑都没有给出）")
    return ("；★ 它只是**传播 / 影响面**（替下游报错，自己没炸），"
            f"真正的出错方是{where}")


def align_root_cause(arbitration: Any, decision: Mapping[str, Any], *,
                     evidence: Sequence[Any] = (),
                     cluster: Optional[str] = None) -> dict[str, Any]:
    """把仲裁的 `root_cause` **实体**对齐到候选内裁决的主语（服务维度单一事实来源）。

    Returns:
        `{"root_cause", "changed", "subject", "degraded"}`。规则（顺序即口径）：

          1. 裁决算不出主语（**没有候选 / 没有地板**）→ **原样返回**：老路径
             （trace 取不到）的行为一字不变；
          2. 仲裁本来就没有根因 → **不凭空造一个**（`concluded ⟺ root_cause 非空`）；
          3. 实体 = 主语服务；命中 `propagated` 时必须写成"传播/影响面（被调方 = X）"；
          4. **类别仍取自既有打分框架**（`category` / `score` / `supporting` / `prior`
             一律不动），只按同一份实现重写 `statement` —— 于是"类别所附着的服务"
             与 `entity` 必然一致；该类别的支持证据不在主语服务上时**不换假类别**、
             只如实留痕（见 `_alignment_notes`）。
    """
    subject = subject_of(decision)
    root = getattr(arbitration, "root_cause", None) if arbitration is not None else None
    service = str((subject or {}).get("service") or "").strip()
    if subject is None or root is None or not service:
        return {"root_cause": root, "changed": False, "subject": subject, "degraded": []}
    statement, changed = _aligned_statement(root, subject, service)
    if not changed:
        return {"root_cause": root, "changed": False, "subject": subject, "degraded": []}
    entity = EntityRef.make(EntityType.SERVICE.value, service,
                            cluster or root.root_entity.cluster)
    return {"root_cause": root.model_copy(update={"root_entity": entity,
                                                  "statement": statement}),
            "changed": True, "subject": subject,
            "degraded": _alignment_notes(root, subject, service, evidence)}


def _aligned_statement(root: Any, subject: Mapping[str, Any],
                       service: str) -> tuple[str, bool]:
    """对齐后的陈述句 + **是否真的改了**（实体没换又没有传播标注 → 一个字都不动）。

    ★ 只有实体真的换了才重写整句：重用 `statement_for`（全仓唯一一份生成实现），
      "类别所附着的服务"与实体才不会各说各话；实体没换时不动原句（免得只是措辞漂移）。
    """
    same = same_service(root.root_entity.name, service)
    statement = (str(root.statement) if same else
                 statement_for(str(root.category), service, root.supporting,
                               root.missing_necessary))
    if subject.get("propagated") and "传播 / 影响面" not in statement:
        # ★ 幂等：同一条仲裁结果被对齐两次时，不许把标注叠成两遍
        statement += _propagation_note(subject)
    return statement, (not same) or statement != root.statement


def _alignment_notes(root: Any, subject: Mapping[str, Any], service: str,
                     evidence: Sequence[Any]) -> list[str]:
    """对齐的留痕（**做了什么、为什么、哪里不同源** —— 含糊等于没留痕）。"""
    notes: list[str] = []
    if subject.get("propagated"):
        notes.append(f"候选集里**没有自身出错**的节点 → 主语落在**传播/影响面**位置 "
                     f"{service}"
                     + (f"（被调方 = {subject.get('downstream')}）"
                        if subject.get("downstream") else "（被调方取不到）")
                     + "，★ 不把调用方当成「自己炸了」")
    if not same_service(root.root_entity.name, service):
        notes.append(f"`root_cause` 的**实体**按候选集对齐为 {service}"
                     f"（打分框架原先落在 {root.root_entity.name}）—— "
                     f"叙事主语与 `root_cause` 现在同源")
    services = _support_services(root.supporting, evidence)
    if services and not any(same_service(name, service) for name in services):
        notes.append(f"★ 该类别（{root.category}）的支持证据来自 "
                     f"{'、'.join(sorted(services))}，与对齐后的实体 {service} "
                     f"**不在同一服务** —— 类别仍取自既有打分框架（不改公式、不编类别），"
                     f"此处如实留痕")
    return notes


def _fallback_notes(floor: Mapping[str, Any], check: Mapping[str, Any],
                    draft: Any) -> list[str]:
    """降级留痕（**越界必须写明"已弃用模型选择"**，含糊等于没留痕）。"""
    out: list[str] = []
    out_of_set = dict(check.get("out_of_set") or {})
    items = [str(item) for item in (out_of_set.get("items") or [])]
    if out_of_set.get("kind") == "out_of_set":
        out.append("模型选择越界（"
                   + ("、".join(items) if items else "未给出越界项")
                   + "）→ **已退回确定性结论**（代码的地板）")
    if str(floor.get("degraded") or ""):
        out.append(str(floor["degraded"]))
    if not out and draft is not None:
        out.append("模型没有给出可用结论（空结果或全部越界）→ 已退回确定性结论")
    return out


def _floor_verdicts(floor: Mapping[str, Any]) -> list[dict[str, Any]]:
    """地板 → 结论形状（与模型那条**同一个形状**：服务/错误类型/位置/次数/首现/是否传播）。"""
    out: list[dict[str, Any]] = []
    for item in floor.get("picked") or []:
        out.append({
            "service": item.get("service") or "",
            "operation": item.get("operation") or "",
            "error_type": item.get("error_type") or "",
            "fingerprint": fingerprint_of(item),
            "count": item.get("count", 1),
            "first_seen": item.get("first_seen") or "",
            "propagated": bool(item.get("propagated")),
            "intrinsic": bool(item.get("intrinsic")),
            "slow": bool(item.get("slow")),
            "downstream": item.get("downstream") or "",
            "reason": "代码判据：" + str(floor.get("kind") or "（无）"),
            "wording": "",
        })
    return out


def _order(verdicts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """排序：**自己炸的在前**，其次按服务名与位置（确定性；不按模型给的顺序）。"""
    return sorted((dict(item) for item in verdicts),
                  key=lambda item: (0 if item.get("intrinsic") else 1,
                                    str(item.get("service") or ""),
                                    str(item.get("operation") or "")))
