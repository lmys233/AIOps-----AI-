"""答案层的**后置校验**：模型写了不成立的话 → 改写或换措辞（不靠 prompt 自觉）。

★ 为什么必须有代码级兜底：prompt 里写死"没查到不许说没有异常"，但真机上模型照抄
  一段"这次排查没有发现任何错误类异常，也就是说相关链路都查到了"—— 而这一轮
  **一次取证都没有发**（没有指标、没有日志、没有 trace）。这类话**不会报错**，
  只会让值班同学以为"排查过了，没问题"（本项目的诚实性红线：
  **没查成 ≠ 没有异常**）。prompt 是要求，这里是判据。

★ 三条判据（都由**结构化事实**给，不由措辞猜）：

  ① 本轮没有任何取证 → 不许出现"没有异常 / 都正常 / 查过没问题"这类断言；
     顺带把"（这次）查到了"改成"上一轮（时刻）查到过"（本轮的功劳不能算到继承上）。
  ② 本轮只有继承来的证据 → 不许说"这一轮查过"，要说清是哪一轮的。
  ③ 台账/内部状态话术（"还在等 X 这个目标的结果"）不许进正文 —— 它属于
     `degraded` 那一栏。

★ 口径：**改写在先、弃用兜底**。改写失败（正则不命中任何东西）时调用方仍会把
  `guarded != summary` 当作"动过它"的依据，所以这里**只在真的改了**的时候才返回新文案。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final, Mapping, Sequence

from ..common.freshness import neutralize_stale_claim
from ..common.thresholds import METRIC_STALE_SEC

__all__ = ["guard_answer_summary", "guard_currency", "rewrite_round_claim",
           "neutralize_ledger_talk", "label_rows", "drop_healthy_rows",
           "fresh_evidence_row", "NO_FRESH_EVIDENCE_NOTE", "HEALTHY_CLAIM"]

logger = logging.getLogger("agent.answer.guard")

#: 本轮一条证据都没有时，写进**用户能看到的明细分**的那句话
NO_FRESH_EVIDENCE_NOTE: Final[str] = (
    "★ 这一轮**没有取证**（没有查到任何数据）—— 「没查成」不等于「没有异常」，"
    "所以这一轮不下「正常 / 没有异常」的判断")

#: 「正常 / 没有异常」这类断言（只在**本轮没有新鲜证据**时才拦）。
#: ★★ 两个约束（都是被"改写后的句子又被自己命中"教出来的）：
#:   ① 换个说法**不许**命中：`得不出「正常 / 没有异常」的判断` 是把话说对了，
#:      所以"没有异常"后面必须**收句**（句末 / 分句末），不能接名词；
#:   ② 常见措辞要**在**：`一切正常 / 都正常 / 未见异常 / 查过没问题` 都要拦得住。
HEALTHY_CLAIM: Final[re.Pattern[str]] = re.compile(
    r"(没有|没|未|无)[^。；，,\n]{0,6}?(发现|查到|看到|观察|检出|出现)?"
    r"[^。；，,\n]{0,6}?(异常|错误|问题|故障)(?=[。；;！!，,\n]|$)"
    r"|一切正常|全部正常|都正常|均正常|均无异常|未见异常|未发现异常"
    r"|查过(了)?没问题|没有问题")

#: 零证据时**整句替换**成的那个说法（同族缺陷的正面口径）
_NO_EVIDENCE_SENTENCE: Final[str] = (
    "这一轮**没有取证**（一条数据都没查成），所以这一轮**得不出**"
    "「正常 / 没有异常」的判断 —— 「没查成」不等于「没有异常」")
#: 「这一轮查过了」这类把功劳记在本轮头上的说法（本轮没取证时不许说）
_THIS_ROUND_CLAIM: Final[re.Pattern[str]] = re.compile(
    r"(这次|本轮|本次)(排查|检查|核查|查询|取证|核对|排查了|查了|检查了)?")

#: 改写成**成立的**说法：把"这次查了/这次排查"退回"上一轮那一次查到过"
_ROUND_REWRITE: Final[re.Pattern[str]] = re.compile(
    r"(这次|本轮|本次)(排查|检查|核查|查询|取证|核对|查(?:到|过|了|一遍)?)?")

#: 台账/内部状态话术（进程在等哪个目标、等什么结果）—— 只在 `degraded` 里出现
_LEDGER_TALK: Final[re.Pattern[str]] = (
    re.compile(r"[^。\n]{0,60}还在等[^。\n]{0,60}?(?=[。\n]|$)"),
    re.compile(r"[^。\n]{0,60}(这个目标|该目标|这个任务)[^。\n]{0,40}结果(?=[。\n]|$)"),
    re.compile(r"[^。\n]{0,60}台账[^。\n]{0,60}(?=[。\n]|$)"),
)

#: 台账话术被清掉之后补的一句（**不是删掉不表**：如实说"我还没查完"）
_LEDGER_REPLACEMENT: Final[str] = (
    "（系统内部的调度状态已挪到「降级明细」一栏，正文只说结论与还缺什么。）")


def guard_answer_summary(summary: str, *, evidence: Sequence[Any] = (),
                         inherited: Sequence[Any] = (),
                         inherited_at: str = "",
                         facts: Mapping[str, Any] | None = None,
                         notes: list[str] | None = None) -> tuple[str, list[str]]:
    """后置校验一段白话总结，返回 `(可给用户看的文案, 改动说明)`。

    Args:
        evidence: **本轮**取到的证据（空 = 这一轮没取证）。
        inherited: 上一轮那批证据（空 = 没有可继承的）。
        inherited_at: 上一轮的抓取时刻（写进"上一轮（时刻）"这句话）。
    """
    text = str(summary or "")
    if not text.strip():
        return text, []
    current = [item for item in (evidence or ())]
    carried = [item for item in (inherited or ())]
    if current:
        return _guard_ledger(text, notes), []
    steps: list[str] = []
    stale = _stale_reason(facts or {})
    if HEALTHY_CLAIM.search(text):
        text, why = _reachable_rewrite(text, note=_NO_EVIDENCE_REASON)
        if why:
            steps.append(why)
    elif stale:
        guarded = neutralize_stale_claim(text, METRIC_STALE_SEC + 1.0, str(stale[1]))
        if guarded != text:
            text = guarded
            steps.append(f"模型写了「当前正常」但本轮**没有新鲜读数**支撑"
                         f"（{stale[0]}）→ 已改写为「按末次采样说」")
    if carried:
        text, why = _rewrite_to_inherited(text, inherited_at)
        if why:
            steps.append(why)
    return _guard_ledger(text, steps), steps

#: 本轮零证据时那段断言的**改写目标**（前半句留白、后半句说清"这一轮没查"）
_NO_EVIDENCE_REASON: Final[str] = (
    "★ 模型写了「没有异常 / 正常」而**这一轮没有任何取证** → 已改写成"
    "「这一轮没有查成，不能据此说正常」")


def _reachable_rewrite(text: str, *, note: str) -> tuple[str, str]:
    """把含"没有异常"这类**不成立**断言的句子整句换掉。

    ★ 为什么整句换而不是删那几个字：删字会留下"这次排查类异常"这种**语法坏掉、
      语义还反着**的句子（真机原句："这次排查没有发现任何错误类异常，也就是说
      相关链路都查到了、只是没查出问题，不是没查成"）。
    ★ 同一句里**别的**内容不丢（按行拼接）：改写不是"删掉不表"。
    ★★ 判据是"**这一句确实命中**那个断言"（`HEALTHY_CLAIM.search`），
      **不**再排除"否定式表述" —— 真机的那一句里同时有"没有发现异常"与
      "不是没查成"，任何基于措辞的排除都会把该改的那句放过去。
      改写后的那句话本身不会被再改（它只在本函数里生成一次）。
    """
    kept: list[str] = []
    for chunk in text.split("\n"):
        for sentence in re.split(r"(?<=[。；;！!])", chunk):
            if not sentence.strip():
                continue
            kept.append(_NO_EVIDENCE_SENTENCE + "。"
                        if HEALTHY_CLAIM.search(sentence) else sentence)
    guarded = "\n".join(kept).strip()
    return (guarded, note) if guarded and guarded != text.strip() else (text, "")


def _rewrite_to_inherited(text: str, inherited_at: str) -> tuple[str, str]:
    """本轮没取证、但用了上一轮的证据 → 把"这一轮查过"改成"上一轮（时刻）查到过"。"""
    when = f"（{inherited_at}）" if inherited_at else ""
    result: list[str] = []
    cursor, hits = 0, 0
    for match in _ROUND_REWRITE.finditer(text):
        # ★ 动词短语后面的"了/过"属于**被替换掉的那个说法**，一起吃掉：
        #   否则会留下"上一轮（T0）了 order …"这种语法坏掉的句子。
        end = match.end()
        while end < len(text) and text[end] in "了过":
            end += 1
        result.append(text[cursor:match.start()])
        result.append(f"上一轮{when}查到过")
        cursor, hits = end, hits + 1
    if not hits:
        return text, ""
    result.append(text[cursor:])
    return "".join(result), (f"本轮**没有取证**、用的是上一轮的证据 → 正文里的"
                             f"「这一轮查过」已改写成「上一轮{when}查到过」"
                             f"（不把继承算成本轮）")


def rewrite_round_claim(text: str, inherited_at: str = "") -> str:
    """把"这一轮查过了"改写成"上一轮（时刻）查到过"（明细行也走这一处）。"""
    return _rewrite_to_inherited(str(text or ""), inherited_at)[0]


def label_rows(rows: Sequence[Mapping[str, Any]],
               facts: Mapping[str, Any]) -> list[dict[str, str]]:
    """给明细行**补上出处标注**（本轮没取证时）：证据行与"这一轮查过"的说法都改。

    ★ 用户看的明细表里若把上一轮的证据写成本轮的，那与总结里的错误是同一件事 ——
      两处必须同一把尺子（都在这一处改，免得只改一处）。
    """
    at = str(facts.get("inherited_at") or "")
    has_fresh = bool(tuple(facts.get("evidence") or ()))
    out: list[dict[str, str]] = []
    for row in rows or ():
        label = str(row.get("label") or "")
        value = str(row.get("value") or "")
        if not has_fresh:
            value = rewrite_round_claim(value, at)
            if label.startswith("证据 ") and "上一轮" not in value:
                value = f"上一轮{('（' + at + '）') if at else ''}查到的：{value}"
        out.append({"label": label, "value": value})
    return out


def drop_healthy_rows(details: Any) -> list[Any]:
    """丢掉**明细行**里"没有异常 / 都正常"那类断言（本轮零证据时的同一把尺子）。

    ★ 为什么明细也要过：用户看到的是一整块（总结 + 明细表），
      只改正文而留着明细行里的同一句话，等于把错误换个地方说了一遍。
    """
    return [row for row in (details or ())
            if not HEALTHY_CLAIM.search(str(getattr(row, "value", "")
                                            or (row.get("value") if isinstance(row, Mapping)
                                                else "")))]


def fresh_evidence_row(facts: Mapping[str, Any]) -> dict[str, str]:
    """本轮没取证时那一条**如实说明**的明细行（有取证时返回空 dict）。

    ★ `report` 的正文之外还有一屏明细行 —— 只改正文的话，"本轮没查成"这件事
      在明细里仍然看不出来（用户会把它读成"查过了、没问题"）。
    """
    if facts.get("evidence"):
        return {}
    return {"label": "这一轮的取证情况",
            "value": ("本轮**没有取证**（一条数据都没查成）—— 「没查成」不等于"
                      "「没有异常」；下面若有证据，标注的是**上一轮**查到的那批")}


def neutralize_ledger_talk(text: str, note: str = "") -> tuple[str, str]:
    """把**台账/内部状态话术**从正文里清掉；命中就返回 `(清干净的文案, 说明)`。

    ★ `note` 为空时用内置那句（"系统内部的调度状态已挪到降级明细"）。
    """
    original = str(text or "")
    cleaned = original
    for pattern in _LEDGER_TALK:
        cleaned = pattern.sub("。", cleaned)
    if cleaned == original:
        return original, ""
    cleaned = re.sub(r"。[。；\s]*", "。", cleaned).strip()
    cleaned = f"{cleaned}{note or _LEDGER_REPLACEMENT}"
    return cleaned, ("正文里出现了**台账/内部状态**话术（在等哪个目标的结果一类）→ "
                     "已从正文移除（这类信息只在「降级明细」里）")


def guard_currency(summary: str, facts: Mapping[str, Any]) -> tuple[str, str]:
    """模型写「当前正常」时的**后置校验**：这句话能不能被事实支撑？

    ★ 判据只有一条：本轮**有没有新鲜的读数**。有（值问答给了数且 `staleness_sec`
      在阈值内）就不拦；没有就把"当前正常"改写成"按末次采样说" ——
      改写**不是删掉不表**。
    ★ 为什么必须后置校验：prompt 里已经写死规矩，但"模型照抄一个错措辞"实测发生过
      （见 prompt 里那些踩坑注释）；诚实性这条线要有**代码级**的兜底。
    """
    metric = _metric_answer(facts)
    stale = metric.get("staleness_sec")
    fresh = (bool(metric) and metric.get("value") is not None
             and (stale is None or float(stale or 0.0) <= METRIC_STALE_SEC))
    if fresh:
        return summary, ""
    as_of = str(metric.get("as_of") or "")
    guarded = neutralize_stale_claim(summary, METRIC_STALE_SEC + 1.0, as_of)
    if guarded == summary:
        return summary, ""
    return guarded, ("模型写了「当前正常」但本轮**没有新鲜读数**支撑"
                     "（末次采样 " + (as_of or "时刻未知") + "）→ 已改写为「按末次采样说」")


def _metric_answer(facts: Mapping[str, Any]) -> dict[str, Any]:
    """查数值那一轮的结果。**没有就返回空 dict**（不猜）。"""
    result = facts.get("metric_answer")
    answer = (result or {}).get("answer") if isinstance(result, Mapping) else None
    return dict(answer) if isinstance(answer, Mapping) else {}


def _guard_ledger(text: str, notes: list[str] | None) -> str:
    """台账话术清理的**小出口**（`steps` 可有可无时用它）。"""
    cleaned, why = neutralize_ledger_talk(text)
    if why and notes is not None:
        notes.append(why)
    return cleaned


def _stale_reason(facts: Mapping[str, Any]) -> tuple[str, str]:
    """读数值那一轮的**新鲜度**：返回 `(人话原因, 采样时刻)`；没有新鲜读数就是空。"""
    result = facts.get("metric_answer")
    answer = (result or {}).get("answer") if isinstance(result, Mapping) else None
    if not isinstance(answer, Mapping) or answer.get("value") is None:
        return ("本轮没有算出数值", "")
    stale = answer.get("staleness_sec")
    if stale is None:
        return ("", "")
    try:
        seconds = float(stale)
    except (TypeError, ValueError):
        return ("", "")
    if seconds <= METRIC_STALE_SEC:
        return ("", "")
    return (f"末次采样 {answer.get('as_of') or '时刻未知'}（距今 {seconds:.0f}s）",
            str(answer.get("as_of") or ""))
