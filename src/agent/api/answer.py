"""把结构化事实写成**给值班同学看的白话答案**。

★ 为什么需要它（用户原话）：

> ① "回答的答案不能带有章节号或判据编号这类内部术语（用户也不会去看什么规范文件）"
> ② "已经识别什么意图、给出拒答什么的，都不是答案，应该只在折叠显示"
> ③ "答案应该是通俗易懂的。**至少需要让大模型总结一次**"

改前 `report` 是渲染层拼的模板句，里面塞满了内部术语（章节号、`prompt_read`、
`FORENSICS_INTENTS`、"拒答"），而它被放在**答案区第一屏** —— 用户读到的是实现细节。

三条硬约束：

  ① **答案里不许有内部术语**：章节号 / 英文状态值 / 错误码 / 字段名 / 判据编号 / 反引号 ——
     它们是**折叠区**的内容（`explanation.internal_report`）；
  ② **模型只能复述给定事实**（结论必须可追溯证据）—— prompt 里写死，
     代码侧也不再从模型那里接受任何"新事实"（只取它的措辞）；
  ③ **一定要兜底，且兜底也白话**：没有模型 / 调用失败 / **模型输出违规** → 走模板。
     ★ 绝不把内部术语原样透给用户 —— 这条比"答案好看"优先。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Final, Mapping, Optional, Sequence

from pydantic import BaseModel, Field

from ..common.freshness import (METRIC_STALE_SEC, freshness_text,
                               neutralize_stale_claim)
from ..llm.gateway import LLMGateway, LLMError
from ..common.enums import CHAT_STATUS_LABELS, ChatStatus, ErrorCode
from .answer_guard import (drop_healthy_rows, fresh_evidence_row,
                           guard_answer_summary, guard_currency, label_rows)

__all__ = ["AnswerText", "AnswerWriter", "looks_internal", "template_answer",
           "status_label", "key_identifiers", "missing_identifiers", "render_details",
           "ANSWER_ROLE", "ANSWER_MIN_TOKENS"]

logger = logging.getLogger("agent.answer")

#: 这次调用的角色名（进审计/记账；`cost` 里能看出"答案花了一次模型"）
ANSWER_ROLE: Final[str] = "report"

#: 答案的**展示/落库裁剪**上限（字符）—— ★ 它**不是**模型的输出预算，别拿它当 `max_tokens`。
ANSWER_MAX_CHARS: Final[int] = 600

#: ★★ 答案总结的**输出预算**（token）。为什么不能拿 `ANSWER_MAX_CHARS` 当预算：
#:   实测（真机 2026-10-03）：`deepseek-v4-pro` 是**思考型**模型，先花掉 reasoning tokens
#:   再产出内容；给它 600（截断重试后 2400）→ `reasoning_tokens=2400` 把预算吃光、
#:   **content 为空** → `LLM_SCHEMA_INVALID` → 用户拿到的是兜底模板的证据墙。
#:   答案本身很短（≤600 字符），但它需要的是"**够想 + 够写**"的 token。
ANSWER_MIN_TOKENS: Final[int] = 4096

#: ★★ **内部术语黑名单** —— 命中即视为"这句话不该给用户看"。
#:  ★ 为什么要机器判：模型完全可能把 prompt 里的 `abstained` 这类内部词抄进答案，
#:    而这类泄漏**不会报错**，只会让用户看到一句他读不懂的话（与"美化失败"同族的
#:    诚实性缺陷）。判据写死在这里，模型输出与模板输出**同一把尺子**。
_INTERNAL: Final[re.Pattern[str]] = re.compile(
    "|".join((
        r"spec", r"§", r"`",
        # ★★ 状态词必须是**独立单词**（`\b` 那套不够：下划线算单词字符）。
        #   实测踩到过：模型按 prompt 要求原样写出错误类型 `POOL_QUERY_FAILED`，
        #   里面有 `FAILED` → 被这条黑名单命中 → **好答案被弃用**、退回模板
        #   （用户拿到的比模型本来能给的差）。边界必须是"前后都不是 ASCII 单词字符"。
        *[rf"(?<![A-Za-z0-9_]){re.escape(status.value)}(?![A-Za-z0-9_])"
          for status in ChatStatus],
        r"TOOL_[A-Z_]+", r"LLM_[A-Z_]+",
        re.escape(ErrorCode.INTERNAL_ERROR.value), r"ErrorCode",
        r"(?:promql|logql|traceql|change|topology|memory)_read",
        r"[a-z]+_agent", r"\bH\d{1,2}\b",
        r"判据", r"拒答", r"字段", r"仲裁", r"先验", r"softmax", r"必要性",
        r"unresolved", r"evidence", r"incident_id", r"token",
    )), re.IGNORECASE)


def looks_internal(text: str) -> str:
    """检查这句话里有没有**不该给用户看**的内部术语；有就返回命中的那个词。"""
    match = _INTERNAL.search(str(text or ""))
    return match.group(0) if match else ""


# ======================================================================
#  ① 模型输出的形状（网关只接受结构化输出）
# ======================================================================
class AnswerText(BaseModel):
    """定义模型要填的结构 —— **只有措辞与"明细行"，没有新事实**。

    ★★ 从"一整段话"改成**两段式** ——
      ① `summary`：一段白话总结（结论 + 最关键的那个具体目标）；
      ② `details`：明细行（结论/假设/逐条证据/下一步），`details_kind` 决定前端用
         **表格**还是 **JSON** 展示（★ 由**模型自己选**，用户明确要求）。
    ★ 为什么让模型给"行"而不是自己写 Markdown/JSON 文本：
      网关只接受结构化输出，而且**模型手写标记语法容易写坏**
      （还被内部术语黑名单禁着反引号）。渲染交给前端 = 既整齐又不会坏。
    """

    summary: str = Field(
        default="",
        description="一段白话总结：先说有没有结论，再说最关键的具体目标（表名/接口/服务），"
                    "2~4 句，不要出现任何内部术语、章节号、英文状态值或字段名")
    details_kind: str = Field(
        default="table",
        description="明细块用哪种展示：`table`（表格，推荐）或 `json`（层级化数据）。"
                    "条目多、字段规整用 table；嵌套结构用 json")
    details: list[dict[str, str]] = Field(
        default_factory=list,
        description="明细行：[{\"label\": \"最可能的原因\", \"value\": \"…\"}, …]。"
                    "只放事实里有的内容；可定位的具体标识（表名/接口/错误类型）必须原样写")
    next_action: str = Field(
        default="", description="一句可执行的下一步（没有就留空）")


# ======================================================================
#  ② 白话模板（**兜底**，也是离线单测走的路径）
# ======================================================================
_STATUS_PLAIN: Final[dict[str, str]] = CHAT_STATUS_LABELS


def status_label(status: str) -> str:
    """把英文状态值翻成中文标签（**给用户看的**；原始值仍留在折叠区）。"""
    return _STATUS_PLAIN.get(str(status or ""), "已结束")


def _evidence_lines(facts: dict[str, Any], limit: int = 3) -> list[str]:
    """挑出证据的**人话摘要**（`evidence[].summary` 本来就是人读的，直接复述）。

    ★ 摘要里通常带**样本行**（例如 `…relation "inventory_stock" does not exist`）——
      那正是"可定位的具体标识"的来源，所以**不裁剪**（模型拿到它才说得出表名）。
    """
    out: list[str] = []
    for item in (facts.get("evidence") or [])[:limit]:
        text = str(item.get("summary") or "").strip()
        if text:
            out.append(text)
    return out


#: ★ **机械兜底**：从证据摘要里挑出"可定位的标识"——
#:   ① **引号里的对象名**（`relation "inventory_stock"`）→ 表名/资源名；
#:   ② **带下划线的大写错误类型**（`POOL_QUERY_FAILED`、`UndefinedTableError` 不算）。
#:   ★ 只扫 `summary`、**不扫 `raw_ref`**（后者是查询语句，里面全是服务名与语法噪声）；
#:   ★ 大写 token **必须含下划线**：否则 `ERROR` / `FATAL` 这种日志级别也会被当成"目标"，
#:     而它们几乎永远不会出现在中文答案里 → 兜底会一直误触发。
_QUOTED: Final[re.Pattern[str]] = re.compile(r"[\"“]([A-Za-z_][\w.\-]{2,})[\"”]")
_UPPER_TOKEN: Final[re.Pattern[str]] = re.compile(r"\b([A-Z][A-Z0-9]{3,}(?:_[A-Z0-9]+)+)\b")


def key_identifiers(facts: dict[str, Any], *, limit: int = 4) -> list[str]:
    """从证据里挑出**具体目标**（表名 / 错误类型…），按出现顺序去重。"""
    found: list[str] = []
    for item in facts.get("evidence") or []:
        text = str(item.get("summary") or "")
        for pattern in (_QUOTED, _UPPER_TOKEN):
            for match in pattern.findall(text):
                if match not in found:
                    found.append(match)
    return found[:limit]


def missing_identifiers(answer: str, facts: dict[str, Any]) -> list[str]:
    """找出答案里**漏掉**的具体标识（用户要靠它去核对）。

    ★ 这是**兜底判据**（不是主路径）：prompt 已经明确要求写出来（要求 6），
      但模型可能仍概括掉 —— 实测就发生过（证据有 `inventory_stock`，答案只写「库存表」）。
      漏了就由渲染层补一句「涉及的具体目标：…」，**不重写答案**（不假装是模型说的）。
    """
    text = str(answer or "")
    return [name for name in key_identifiers(facts) if name not in text]


#: ULID 形状的证据号（`01M3V0…`）—— 给模型看的事实里**不该出现它们**：
#: 模型很可能照抄，而用户看到一串乱码般的标识符。
_ULID: Final[re.Pattern[str]] = re.compile(r"\b[0-9A-HJKMNP-TV-Z]{26}\b")


def _strip_ids(text: str) -> str:
    return _ULID.sub("（已省略编号）", str(text or ""))


def _safe_lines(lines: list[str]) -> tuple[list[str], int]:
    """挡住模板里含内部串的行 —— 摘要可能带着错误码之类的内部词，直接复述等于把内部术语透给用户。

    Returns `(可以给用户看的行, 被挡下的行数)` —— 挡下的**不静默**：
    调用方会补一句"另有 N 条线索含技术细节，展开可以看到"。
    """
    kept = [line for line in lines if not looks_internal(line)]
    return kept, len(lines) - len(kept)


def _snapshot_lines(facts: dict[str, Any], limit: int = 8) -> list[str]:
    """取现状快照（`metric_snapshot`）—— 查现状时**当前值就是答案**。"""
    return [str(item.get("summary") or "").strip()
            for item in (facts.get("evidence") or [])
            if item.get("signal") == "metric_snapshot"
            and str(item.get("summary") or "").strip()][:limit]


def _snapshot_items(facts: dict[str, Any], limit: int = 12) -> list[dict[str, Any]]:
    """取现状快照的**结构化那份**（不再靠正则拆句子：`magnitude`/`baseline` 本来就在证据里）。"""
    out: list[dict[str, Any]] = []
    for item in facts.get("evidence") or []:
        if item.get("signal") != "metric_snapshot":
            continue
        tags = [str(tag) for tag in (item.get("tags") or [])]
        metric, text = (tags[-1] if tags else ""), str(item.get("summary") or "").strip()
        if not metric or not text:
            continue
        out.append({"metric": metric, "magnitude": item.get("magnitude"),
                    "baseline": item.get("baseline"), "unit": str(item.get("magnitude_unit") or ""),
                    "service": str((item.get("entity") or {}).get("name") or "")})
    return out[:limit]


def _num_text(value: Any, unit: str) -> str:
    """把数值写成紧凑的人话（`0.2313` + `req/s`）；没有数就说没有数。"""
    if value is None or isinstance(value, bool):
        return "无数值"
    try:
        return f"{float(value):.4g}{unit}"
    except (TypeError, ValueError):
        return "无数值"


def _snapshot_digest(items: list[dict[str, Any]]) -> str:
    """把"指标现状"压成 2~4 句人话 —— **完整清单留在明细表里**。

    ★★ 用户实测的原话："AIOps 给出的答案都是一些证据罗列，没有总结出可读回答" ——
      原来的写法就是把 8 条 `… 当前 0（窗口起点 0.23，5 个采样点）` 原样 `；` 串成一堵墙。
    ★★ 还有一处**不成立的声明**要一起清掉：原模板断言"看数值都还在正常范围，**没有发现异常**"，
      而查现状走的是**轻链路**（`LIGHT_INTENTS`，单 Agent 只报数、**不判异常**）——
      没判过的事不能说成"没有异常"（"没查到"≠"没有"这条红线的同族）。
    """
    changed, zeros, others = [], [], []
    for item in items:
        label = str(item.get("metric") or "")
        cur = _num_text(item.get("magnitude"), str(item.get("unit") or ""))
        base = _num_text(item.get("baseline"), str(item.get("unit") or ""))
        if cur != base:
            # ★★ 变化项**先判**（哪怕当前值是 0）——「QPS 从 0.23 掉到 0」正是用户要的那句；
            #   修前把它和"本来就是 0"的开关混在一起，最有信息量的那条被吞掉了。
            changed.append(f"{label} 从窗口起点的 {base} 变为当前 {cur}")
            continue
        try:
            is_zero = abs(float(item.get("magnitude") or 0.0)) == 0.0
        except (TypeError, ValueError):
            is_zero = False
        (zeros if is_zero else others).append(label)
    parts: list[str] = []
    if changed:
        parts.append("；".join(changed[:4]))
    if others:
        parts.append("其余 " + "、".join(others[:4]) + " 也在同一量级")
    if zeros:
        parts.append(f"另有 {len(zeros)} 项当前值本来就是 0（{_name_counts(zeros)}）")
    head = f"查到的当前值（共 {len(items)} 项）：" + "；".join(parts) + "。" if parts else ""
    return (head + "这些是**窗口内的末次采样值**（不是此刻的读数）—— 这一路只报数、不判是否异常；"
            "要判断有没有问题，请让我按故障来排查（说一句「帮我定位根因」）。")


def _name_counts(names: list[str]) -> str:
    """把重名的指标合成 `fault_injection×2`（同名开关会有好几条，逐条列没有信息量）。"""
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return "、".join(f"{name}×{count}" if count > 1 else name
                    for name, count in list(counts.items())[:4])


def _metric_answer(facts: dict[str, Any]) -> dict[str, Any]:
    """查数值那一轮的结果。**没有就返回空 dict**（不猜）。"""
    result = facts.get("metric_answer")
    answer = (result or {}).get("answer") if isinstance(result, Mapping) else None
    return dict(answer) if isinstance(answer, Mapping) else {}

#: ★ 兼容旧名：守卫在 `answer_guard.py`（同一条判据，没有第二份实现）
_guard_currency = guard_currency


def _metric_answer_text(metric: Mapping[str, Any]) -> str:
    """把算好的结果写成**先给数**的白话答案（数字全部来自运算工具）。"""
    value = metric.get("value")
    unit = str(metric.get("unit") or "")
    name = str(metric.get("metric") or "该指标")
    if value is None:
        # ★ 算不出来 ≠ 0，也 ≠ "没有异常"：如实说"这次没取到可算的数据"
        reason = str(metric.get("caveats") or "这轮没有取到可算的序列")
        return f"这次**没有算出**{name}的数值（不是 0，也不是「没有异常」）：{reason}"
    head = (f"{name} = {value}{unit}（{metric.get('method') or '按工具算出的结果'}；"
            f"{freshness_text(metric)}）。")
    steps = [str(step) for step in (metric.get("steps") or [])][:4]
    middle = ("算法步骤：" + "；".join(steps) + "。") if steps else ""
    tail = f"注意：{metric.get('caveats')}" if metric.get("caveats") else ""
    return head + middle + tail


def _top_hypothesis(facts: dict[str, Any]) -> dict[str, Any]:
    items = facts.get("hypotheses") or []
    return items[0] if items else {}


def _join(items: list[str]) -> str:
    return "；".join(item.rstrip("。") for item in items)


def _plain_next_hint(facts: dict[str, Any]) -> str:
    """给一句**可执行的下一步**（不含任何内部名词）。"""
    if facts.get("degraded_signals"):
        return "这次有一路数据没取到，等它恢复后我再查一次就能给出结论。"
    return "如果你能补充一下现象（比如变慢、报错、从什么时候开始），我可以查得更准。"


def _conclusion_text(facts: dict[str, Any], limit: int = 3) -> str:
    """把结论条写成**一句白话**（兜底路径也走它 —— 结论是代码/候选内选出来的，不是模板编的）。

    ★ 多条并列时不合成一句含糊的话：逐条列出"服务 · 错误类型 · 位置 · 次数 · 首现"，
      传播类明确标"（替下游报错）"。
    """
    items = list(facts.get("root_cause_conclusions") or [])[:limit]
    parts: list[str] = []
    for item in items:
        bit = (f"{item.get('service')} 的 {item.get('operation') or '（未命名位置）'}"
               f"（{item.get('error_type') or '未给出错误类型'}，"
               f"{item.get('count')} 次，首现 {item.get('first_seen') or '时刻未知'}）")
        if item.get("propagated"):
            bit += "—— 这一处是**替下游报错**"
        parts.append(bit)
    return "；".join(parts)


def _answer_fixed(status: str, facts: dict[str, Any]) -> str:
    """直接给出不需要从证据里取数的几种状态的一句话答案。"""
    if status == ChatStatus.CLARIFY.value:
        questions = ((facts.get("clarify") or {}).get("questions") or [])
        first = str((questions[0] or {}).get("question") or "") if questions else ""
        return (f"还需要你补充一点信息：{first}" if first else "还需要你补充一点信息。") + \
            "回答之后我接着查。"
    if status == ChatStatus.BUDGET_EXHAUSTED.value:
        text = "这次查到一半就到了本轮上限，暂时给不出确定结论。"
        top = _top_hypothesis(facts)
        if top.get("statement"):
            text += f"目前看起来最像的是：{str(top['statement']).rstrip('。')}。"
        return text + "可以让我继续查一轮，或缩小时间范围再问一次。"
    if status == ChatStatus.REJECTED.value:
        return "这一次的处置动作被拒绝了，**没有执行任何变更**。系统状态保持原样。"
    return "这次排查在系统侧出错了，没有得到结论。稍后重试；如果是写操作，请先确认现场。"


def _answer_abstained(intent_name: str, facts: dict[str, Any], lines: list[str],
                      snapshots: list[str], more: str) -> str:
    """按证据情况给出没下结论时的说法：查现状 / 查到了但不够 / 压根没取到。"""
    top = _top_hypothesis(facts)
    if intent_name == "METRIC_QUERY" and snapshots:
        items = _snapshot_items(facts)
        if items:
            return _snapshot_digest(items) + more
        return "查到的当前值：" + _join(snapshots) + "。" + more
    if snapshots and not lines:
        return "查到的当前值：" + _join(snapshots) + "。" + more
    if lines:
        text = f"这次查到了 {len(lines)} 条线索，但还不足以确定原因。线索：{_join(lines)}。"
        if top.get("statement"):
            text += f"目前看起来最像的是「{str(top['statement']).rstrip('。')}」，但还需要更多证据。"
        return text + _plain_next_hint(facts) + more
    if facts.get("degraded_signals"):
        return "这次没能取到数据，所以给不出结论。" + _plain_next_hint(facts)
    return "这次没有查到可用的线索，所以给不出结论。" + _plain_next_hint(facts)


def _details_rows(status: str, intent_name: str, facts: dict[str, Any]) -> list[dict[str, str]]:
    """拼离线明细行（模板路径）—— 与模型那条路给出的是同一份结构。"""
    rows: list[dict[str, str]] = [{"label": "本次结论", "value": status_label(status)}]
    root = (facts.get("root_cause") or {}).get("statement") or ""
    for index, item in enumerate(facts.get("root_cause_conclusions") or []):
        relation = "替下游报错" if item.get("propagated") else "本服务自身出错"
        rows.append({
            "label": f"出错位置 {index + 1}",
            "value": (f"{item.get('service')} · {item.get('operation') or '（未命名位置）'}"
                      f"（{item.get('error_type') or '未给出错误类型'}；"
                      f"{item.get('count')} 次；首现 {item.get('first_seen') or '时刻未知'}；"
                      f"{relation}）")})
    if root:
        rows.append({"label": "最可能的原因", "value": str(root)})
    metric = _metric_answer(facts)
    if metric and metric.get("value") is not None:
        rows.append({"label": f"{metric.get('metric') or '结果'}（数值）",
                     "value": f"{metric.get('value')}{metric.get('unit') or ''}"})
        if metric.get("method"):
            rows.append({"label": "算法", "value": str(metric["method"])})
        for index, step in enumerate(metric.get("steps") or [], start=1):
            rows.append({"label": f"算法步骤 {index}", "value": str(step)})
        if metric.get("caveats"):
            rows.append({"label": "口径与局限", "value": str(metric["caveats"])})
    for index, item in enumerate(facts.get("hypotheses") or []):
        if index >= 3:
            break
        rows.append({"label": f"候选解释 {index + 1}",
                     "value": f"{item.get('statement')}（把握 {item.get('score')}）"})
    for index, line in enumerate(_evidence_lines(facts, limit=6)):
        rows.append({"label": f"证据 {index + 1}", "value": line})
    if facts.get("degraded_signals"):
        rows.append({"label": "没查成的数据路",
                     "value": "、".join(facts["degraded_signals"]) + "（不代表没有异常）"})
    if facts.get("empty_signals"):
        rows.append({"label": "查过且正常的项",
                     "value": "、".join(facts["empty_signals"][:8])})
    if intent_name == "METRIC_QUERY":
        for index, line in enumerate(_snapshot_lines(facts), start=1):
            rows.append({"label": f"当前值 {index}", "value": line})
    fresh = fresh_evidence_row(facts)                       # ★ 本轮没取证要如实说
    if fresh:
        rows.append(fresh)
    return [row for row in rows if str(row.get("value") or "").strip()]


def render_details(rows: Sequence[Mapping[str, Any]], kind: str) -> str:
    """把明细行渲染成**文本块**（`report` 要自足：非 WebUI 的调用方只读这个字符串）。

    · `table` → Markdown 表格（人读友好，前端也会把它渲染成真表格）
    · `json`  → 缩进 JSON（层级化，程序友好）
    """
    from ..presentation.formatting import details
    return details(rows,kind)


def template_answer(status: str, intent_name: str, facts: dict[str, Any]) -> str:
    """生成**兜底答案**（无模型 / 模型失败 / 模型输出违规时用）——同样是白话。

    ★ 它必须能独立读懂：不出现章节号、英文状态、工具名、字段名。
    ★ 证据摘要**逐条过安全检查**（`_safe_lines`）：摘要来自系统，可能自带内部串；
      被挡下的行不静默 —— 补一句"另有 N 条线索含技术细节"。
    """
    lines, dropped = _safe_lines(_evidence_lines(facts))
    snapshots, snap_dropped = _safe_lines(_snapshot_lines(facts))
    more = (f"（另有 {dropped + snap_dropped} 条线索含技术细节，展开可以看到）"
            if (dropped + snap_dropped) else "")
    root = (facts.get("root_cause") or {}).get("statement") or ""

    metric = _metric_answer(facts)
    if metric and metric.get("value") is not None:
        # ★★ 值问答：正文就是**那个数**（+ 算法与口径）—— 不走"证据罗列"的兜底话术
        return _metric_answer_text(metric) + more
    if metric:
        # 算了但没得出数（没有序列/引用失效）→ 如实说明，再并上现状罗列
        return _metric_answer_text(metric) + "。" + _answer_abstained(
            intent_name, facts, lines, snapshots, more)
    if status == ChatStatus.CONCLUDED.value and root:
        parts = [f"最可能的原因：{str(root).rstrip('。')}。"]
        if lines:
            parts.append(f"依据：{_join(lines)}。")
        parts.append("建议先按这个方向处理；要我继续验证或给出处置步骤，说一声。")
        return "".join(parts) + more
    # ★★ 没有 formal 根因、但有**候选内选出来的结论条**（服务/位置/次数/首现都在）
    #   时，兜底答案就照它说 —— 这是"结论由代码给、措辞可以朴素"这条口径的落点。
    conclusions = _conclusion_text(facts)
    if conclusions:
        return (f"本轮定位到的出错位置：{conclusions}。"
                + (f"这与查到的证据一致：{_join(lines[:2])}。" if lines else "")
                + _plain_next_hint(facts) + more)
    if status in {ChatStatus.CLARIFY.value, ChatStatus.BUDGET_EXHAUSTED.value,
                   ChatStatus.REJECTED.value, ChatStatus.FAILED.value}:
        return _answer_fixed(status, facts)
    return _error_table_text(facts) + _answer_abstained(intent_name, facts, lines,
                                                        snapshots, more)


def _error_table_text(facts: dict[str, Any]) -> str:
    """跨服务的「谁在报错 + 各自什么错」表（**代码算的**，不靠模型措辞）。

    ★★ 这一段存在的理由：用户问"现在哪些服务在报错？分别是什么错误？"时，
      答案曾经是一堵证据墙 —— 因为**事实里没有跨服务汇总**。现在它有了。
    ★ 传播类必须与自身错误分开列：`order 替 inventory 报错` 不能被读成"order 也炸了"。
    """
    summary = facts.get("error_summary") or {}
    rows = list(summary.get("rows") or [])
    if not rows:
        return ""
    self_rows = [row for row in rows if not row.get("propagated")]
    prop_rows = [row for row in rows if row.get("propagated")]
    lines = ["| 服务 | 错误类型 | 次数 | 首现 |", "|---|---|---|---|"]
    for row in (self_rows or rows)[:10]:
        lines.append(f"| {row.get('service')} | {row.get('error_type')} | "
                     f"{row.get('count')} | {row.get('first_seen') or '（未知）'} |")
    head = ("本报错（**本服务自身**报的错）：\n" if self_rows
            else "本报错（只看到**转发下游**的错误）：\n")
    text = head + "\n".join(lines)
    if prop_rows:
        pairs = "、".join(f"{row.get('service')}/{row.get('error_type')}"
                         for row in prop_rows[:6])
        text += f"\n\n另有服务只是在**转发下游**的报错（不是在报错）：{pairs}。"
    if summary.get("chain_text"):
        text += f"\n\n传播方向：{summary['chain_text']}。"
    return text + "\n\n"


# ======================================================================
#  ③ 让模型写一次（prompt 里写死"只依据事实 + 不许内部术语"）
# ======================================================================
_PROMPT_HEAD: Final[str] = (
    "你是一名 SRE 助手。下面是一次自动排查留下的**结构化事实**。"
    "请把它写成一段**给值班同学看的白话答案**。\n"
    "\n"
    "硬性要求：\n"
    "1. **只依据下面给出的事实**，不要补充、推测或发明任何新信息；\n"
    "2. 先说**结论是什么**（有结论就说结论；没结论就直说没结论、并说明卡在哪里）；\n"
    "3. **不许出现任何内部术语**：章节号（如 spec §6.1）、英文状态值"
    "（abstained/concluded/clarify）、错误码、工具名（如 promql_read）、"
    "字段名、以及『判据』『拒答』『仲裁』『先验』这类实现词；\n"
    "4. 不要出现反引号、代码块或 JSON；\n"
    "5. 3~5 句，中文，语气平和；不要道歉，不要空话；\n"
    "6. ★★ **可定位的具体标识必须原样写出来**：事实里出现过的**表名、接口路径、服务名、"
    "错误类型、指标名、时间点**，都要写进答案 —— 要写「表 inventory_stock 不存在」，"
    "而不是「某张表不存在」。★ 用户要靠这些具体目标去核对/复现，"
    "**笼统的说法等于没给线索**（实测踩过：证据里明明有表名，答案却只写「库存表不存在」）。\n"
    "7. ★★ 事实里若给了**结论条**（已定好的出错服务 / 位置 / 错误类型），"
    "就以它为主语说清楚；结论条有多条时**并列说出来**（不要只挑一条，也不要另选一个主语）；"
    "结论条与错误指纹里的「替下游报错」标记意思是「它只是替下游喊了一声」，"
    "**不要**把它写成「它自己炸了」。\n"
)


def _error_summary_rows(facts: dict[str, Any], limit: int = 12) -> list[str]:
    """把**跨服务错误汇总**写成 prompt 里的几行（谁在报错 + 各自什么错）。

    ★★ 这几行是"证据里有、事实里没有"那个缺口的正面补法：模型看到它们，
      才会在答案里写出**具体的错误类型**，而不是"事实里没给错误类型"。
    ★ `propagated` 必须标出来：它是"替下游报错"，不是本服务自己炸了。
    """
    summary = facts.get("error_summary") or {}
    out: list[str] = []
    for row in (summary.get("rows") or [])[:limit]:
        mark = "（替下游报错）" if row.get("propagated") else "（本服务自身报错）"
        sample = (f"；样本：{_strip_ids(row.get('sample'))}" if row.get("sample") else "")
        out.append(f"- 错误汇总：{row.get('service')} / {row.get('error_type')} {mark} "
                   f"×{row.get('count')} 行，首现 {row.get('first_seen') or '（未知）'}{sample}")
    if summary.get("note"):
        out.append(f"- 错误汇总口径：{_strip_ids(summary['note'])}")
    if summary.get("services_propagated_only"):
        out.append("- ★ 只在**转发**下游报错的服务（不许当成它在报错）："
                   f"{'、'.join(summary['services_propagated_only'])}")
    if summary.get("chain_text"):
        out.append(f"- 跨服务传播方向（每一跳都来自结构化证据）："
                   f"{_strip_ids(summary['chain_text'])}")
    return out


def _fingerprint_lines(facts: dict[str, Any], limit: int = 8) -> list[str]:
    """把**指纹合并后**的错误写成 prompt 里的几行（同一个错误只出现一次）。

    ★ 为什么单列这一段：`rows` 是按 `(服务, 错误类型)` 汇总的，
      而指纹是**更细的一层**（同一个服务的同一个错误类型可能来自不同语句）。
      模型要挑"具体位置"，就得看到这一层 —— 并且 `count` / 首现都要带上，
      那是"爆发 / 拐点"判据的原料。
    """
    summary = facts.get("error_summary") or {}
    out: list[str] = []
    for item in (summary.get("fingerprints") or [])[:limit]:
        out.append(f"- 同一错误（指纹 {item.get('fingerprint')}）：{item.get('service')} / "
                   f"{item.get('error_type')} ×{item.get('count')} 次，首现 "
                   f"{item.get('first_seen_iso') or '（未知）'} → 末次 "
                   f"{item.get('last_seen_iso') or '（未知）'}"
                   + (f"；样本链路 {item.get('sample_trace_id')}"
                      if item.get("sample_trace_id") else ""))
    return out


def _conclusion_lines(facts: dict[str, Any], limit: int = 3) -> list[str]:
    """把本轮的**结论条**写成 prompt 里的几行（服务 / 错误类型 / 位置 / 次数 / 首现 / 是否传播）。

    ★ 这几条是**候选集内选出来的**（代码枚举候选、模型排序、越界即弃用），
      所以模型在写白话答案时应当照着它们说，而不是自己再挑一个主语。
    """
    out: list[str] = []
    for item in (facts.get("root_cause_conclusions") or [])[:limit]:
        relation = "（替下游报错）" if item.get("propagated") else "（本服务自身出错）"
        extra = (f"；被调方 {item.get('downstream')}" if item.get("downstream") else "")
        out.append(f"- 结论条：{item.get('service')} / {item.get('operation') or '（未命名）'}"
                   f"｜错误类型 {item.get('error_type') or '（未给出）'}"
                   f"｜次数 {item.get('count')}｜首现 {item.get('first_seen') or '（未知）'}"
                   f"｜{relation}{extra}"
                   + (f"｜说明：{_strip_ids(item.get('reason'))}" if item.get("reason") else "")
                   + (f"｜可直接采用的措辞：{_strip_ids(item.get('wording'))}"
                      if item.get("wording") else ""))
    return out


def _facts_block(status: str, intent_name: str, facts: dict[str, Any]) -> str:
    """把结构化事实写成 prompt 里的事实区（**只给事实，不给结论模板**）。"""
    rows = [f"- 本次状态（请用白话表达）：{status_label(status)}"]
    if intent_name:
        rows.append(f"- 用户想做的事：{_intent_plain(intent_name)}")
    rows.extend(_finding_rows(facts))
    rows.extend(_measurement_rows(facts, intent_name))
    questions = ((facts.get("clarify") or {}).get("questions") or [])
    for item in questions[:2]:
        rows.append(f"- 还需要用户回答的问题：{_strip_ids(item.get('question'))}")
    rows.append(f"- 用户原话：{_strip_ids(facts.get('user_message')) or '（未记录）'}")
    return "\n".join(rows)


def _finding_rows(facts: dict[str, Any]) -> list[str]:
    """**排查类事实**：结论、候选解释、证据、结论条、指纹、跨服务错误汇总。"""
    rows: list[str] = []
    from ..attribution.problems import problem_prompt_rows
    rows.extend(problem_prompt_rows(facts.get("error_summary") or {}))
    root = (facts.get("root_cause") or {}).get("statement")
    if root:
        rows.append(f"- 已定位的原因：{_strip_ids(root)}")
    for index, item in enumerate(facts.get("hypotheses") or []):
        if index >= 3:
            break
        rows.append(f"- 候选解释 {index + 1}：{_strip_ids(item.get('statement'))}"
                    f"（把握 {item.get('score')}）")
    for index, line in enumerate(_evidence_lines(facts, limit=6)):
        rows.append(f"- 查到的证据 {index + 1}：{_strip_ids(line)}")
    rows.extend(_conclusion_lines(facts))
    rows.extend(_fingerprint_lines(facts))
    rows.extend(_error_summary_rows(facts))
    return rows


def _measurement_rows(facts: dict[str, Any], intent_name: str) -> list[str]:
    """**读数类事实**：指标当前值、已算好的结果、缺失/为空的那几路。

    ★★ `empty_signals` 的语义是「**查了、没看出异常**」，**不是**「没有数据」。
      这里原来写成「这条指标这次没有数据」→ 模型照抄，答案里就出现
      「连接池、延迟、错误率几个指标都没有数据」——而数据其实**在**
      （只是没触发异常阈值）。这正是「查了没异常」≠「没查到」这条红线，
      这次栽在**给模型的措辞**上：prompt 写错一个词，答案就会说一句不成立的话。
    ★★ 措辞按**意图**分（真机踩到过）：查现状（轻链路）**不判异常**，
      所以那些"没有可报的当前值"的项**不能**说成"查过了、没有异常" ——
      实测模型照抄后写出"qps 未见异常"，而事实是"这条指标本次没有数值"。
    """
    rows: list[str] = []
    for line in _snapshot_lines(facts):
        rows.append("- 指标当前值（★ 这些是**窗口内末次采样值**，不是此刻的读数）："
                    f"{_strip_ids(line)}")
    metric = _metric_answer(facts)
    if metric:
        rows.append(f"- 已经用工具算好的结果（★ 只能引用这些数，不要自己再算）："
                    f"{metric.get('metric')} = {metric.get('value')}{metric.get('unit') or ''}；"
                    f"采样时刻 {freshness_text(metric)}；"
                    f"算法 {metric.get('method')}；步骤 {metric.get('steps')}；"
                    f"口径 {metric.get('caveats')}")
    if facts.get("degraded_signals"):
        rows.append(f"- 这几路数据这次**没查成**（★ 注意：这不代表「没有异常」）："
                    f"{'、'.join(facts['degraded_signals'])}")
    light = intent_name == "METRIC_QUERY"
    for line in (facts.get("empty_signals") or [])[:6]:
        rows.append((f"- 这一项**本次没有可报的数值**（既不是「正常」也不是「没取到」）："
                     f"{_strip_ids(line)}") if light else
                    (f"- 这一项**查过了、没有异常**（是「正常」，**不是**「没数据」）："
                     f"{_strip_ids(line)}"))
    return rows


_INTENT_PLAIN: Final[dict[str, str]] = {
    "INCIDENT_RCA": "排查一个线上故障的原因",
    "METRIC_QUERY": "查看某些指标的当前值",
    "CHANGE_IMPACT": "评估一次变更的影响面",
    "REMEDIATION": "希望执行处置动作（重启/回滚/扩容等）",
    "KNOWLEDGE_QA": "问流程或规范",
    "OUT_OF_SCOPE": "问的问题与系统运行无关",
}


def _intent_plain(intent_name: str) -> str:
    return _INTENT_PLAIN.get(intent_name, "排查线上问题")


class AnswerWriter:
    """生成答案：**模型优先**，失败或违规退回白话模板。

    `gateway=None`（没配模型）→ 直接走模板并如实标注；`model` 为空时用配置默认值
    （不得硬编码模型名）。
    """

    def __init__(self, gateway: Optional[LLMGateway] = None, model: str = "") -> None:
        self.gateway = gateway
        self.model = (model or "").strip()

    @property
    def available(self) -> bool:
        return self.gateway is not None

    async def write(self, status: str, intent_name: str,
                    facts: dict[str, Any]) -> dict[str, Any]:
        """返回 `{"text", "summary", "details", "details_kind", "source", "note"}`。

        ★ `text` 是**自足**的（总结 + 文本明细块）—— 非 WebUI 的调用方只读它；
          `summary`/`details`/`details_kind` 给前端做整齐的呈现。

        ★ 任何一步出问题都**不影响主流程**：答案是"呈现层"的产物，
          它失败不该让一次排查失败（与 webui 的软依赖同一口径）。
        """
        fallback = template_answer(status, intent_name, facts)
        rows = _details_rows(status, intent_name, facts)
        if self.gateway is None:
            return _compose(fallback, rows, "table", source="template",
                            note="未配置模型 → 用白话模板（模板本身不含内部术语）")
        raw = await self._summarize(status, intent_name, facts)
        if isinstance(raw, dict):                  # 模型这条路失败 → 已经给好了兜底
            return raw
        summary = str(getattr(raw, "summary", "") or "").strip()
        if not summary:
            return _compose(fallback, rows, "table", source="template",
                            note="模型返回了空总结 → 退回白话模板")
        return _guarded_answer(raw, rows, fallback, facts)

    async def _summarize(self, status: str, intent_name: str,
                         facts: dict[str, Any]) -> Any:
        """喊一次模型写答案；**失败/异常一律返回现成的兜底响应体**（不抛）。"""
        prompt = f"{_PROMPT_HEAD}\n## 事实\n{_facts_block(status, intent_name, facts)}"

        def fallback(note: str) -> dict[str, Any]:
            return _compose(template_answer(status, intent_name, facts),
                            _details_rows(status, intent_name, facts), "table",
                            source="template", note=f"{note} → 退回白话模板")

        try:
            return await self.gateway.generate(prompt, AnswerText, role=ANSWER_ROLE,
                                               model=self.model or None,
                                               temperature=0.2,
                                               max_tokens=ANSWER_MIN_TOKENS)
        except LLMError as exc:
            logger.warning("答案总结失败（%s: %s）→ 退回白话模板", exc.code, exc)
            return fallback(f"模型总结失败（{exc.code}）")
        except Exception as exc:  # noqa: BLE001 —— 呈现层失败不带走排查
            logger.warning("答案总结异常（%s: %s）→ 退回白话模板",
                           type(exc).__name__, exc)
            return fallback(f"模型总结异常（{type(exc).__name__}）")


def _guarded_answer(raw: Any, rows: list[dict[str, str]], fallback: str,
                    facts: dict[str, Any]) -> dict[str, Any]:
    """过**诚实性后置校验**再合成答案（判据见 `answer_guard.py`）。

    ★★ 本轮一条证据都没有时，"没有发现异常 / 都正常"这类断言一律改写；
      用上一轮证据时如实标注是哪一轮的 —— 不靠 prompt 自觉，靠代码拦。
    """
    notes: list[str] = []
    summary, guarded_notes = guard_answer_summary(
        str(getattr(raw, "summary", "") or "").strip(),
        evidence=tuple(facts.get("evidence") or ()),
        inherited=tuple(facts.get("inherited_evidence") or ()),
        inherited_at=str(facts.get("inherited_at") or ""), facts=facts, notes=notes)
    if guarded_notes:
        # ★★ 明细行与正文**同一把尺子**：本轮零证据时，模型给的那几行里照样会写
        #   「没有发现任何错误类异常」—— 只改正文等于把同一句话换个地方说给用户。
        rows, raw.details = label_rows(rows, facts), drop_healthy_rows(raw.details)
    raw.summary, _why = guard_currency(summary, facts)
    result = _from_model(raw, rows, fallback)
    for note in [*guarded_notes, _why]:
        if note:
            result["note"] = "；".join(part for part in (result.get("note"), note) if part)
    return result


def _from_model(raw: Any, rows: list[dict[str, str]], fallback: str) -> dict[str, Any]:
    """把模型给的两段合成最终答案（**违规就弃用**，见函数体注释）。"""
    summary = str(getattr(raw, "summary", "") or "").strip()
    next_action = str(getattr(raw, "next_action", "") or "").strip()
    kind = str(getattr(raw, "details_kind", "") or "table").strip().lower()
    model_rows = [{"label": str(row.get("label") or ""), "value": str(row.get("value") or "")}
                  for row in (getattr(raw, "details", None) or [])
                  if isinstance(row, Mapping) and str(row.get("value") or "").strip()]
    # ★ 模型给的明细可能为空（它只写了总结）→ 用**模板明细**补上（结构别丢）
    if not model_rows:
        model_rows, kind = rows, "table"
    leaked = (looks_internal(summary) or looks_internal(next_action)
              or looks_internal(" ".join(f"{r['label']}{r['value']}" for r in model_rows)))
    if leaked:
        # ★★ 这条是**诚实性**判据：宁可退回模板，也不把内部术语透给用户。
        logger.warning("答案总结里出现了内部术语 %r → 弃用并退回白话模板", leaked)
        return _compose(fallback, rows, "table", source="template",
                        note=f"模型输出含内部术语 {leaked!r} → 弃用并退回白话模板")
    if next_action:
        model_rows = model_rows + [{"label": "下一步", "value": next_action}]
    return _compose(summary, model_rows, kind, source="llm", note="")


def _compose(summary: str, rows: Sequence[Mapping[str, Any]], kind: str, *,
             source: str, note: str) -> dict[str, Any]:
    """把两段拼成自足的 `text`，并保留结构化那份给前端。"""
    items = [{"label": str(row.get("label") or ""), "value": str(row.get("value") or "")}
             for row in rows if str(row.get("value") or "").strip()]
    block = render_details(items, kind)
    text = f"{summary}\n\n{block}" if block else summary
    return {"text": text[:ANSWER_MAX_CHARS * 3], "summary": summary,
            "details": items, "details_kind": kind, "source": source, "note": note}
